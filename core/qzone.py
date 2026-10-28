"""QQ 空间说说发布客户端。

协议要点如下：
    1. 认证 —— 通过 OneBot 扩展接口 get_cookies(domain=user.qzone.qq.com)
       拿到 skey / p_skey，无需扫码、无需密码。
    2. 令牌 —— g_tk 由 p_skey 经 djb2 变体哈希后取低 31 位得到。
    3. 发布 —— POST emotion_cgi_publish_v6，正文放在 con 字段，
       可见范围由 ugc_right 控制（1 公开 / 4 好友 / 64 仅自己）。

协议细节参考了 Zhalslar/astrbot_plugin_qzone（MIT）的公开实现。
"""

from __future__ import annotations

import asyncio
import json
import re
from http.cookies import SimpleCookie
from time import monotonic
from typing import Any

import aiohttp

from astrbot.api import logger

QZONE_DOMAIN = "user.qzone.qq.com"

PUBLISH_URL = (
    "https://user.qzone.qq.com/proxy/domain/"
    "taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

# qzone 判定登录失效的返回码
LOGIN_EXPIRED_CODES = {-3000, -10000, 1000}


class QzoneAuthError(RuntimeError):
    """Cookie 取不到或不可用。"""


class QzoneContext:
    """一次请求所需的 uin / skey / p_skey。"""

    def __init__(self, uin: int, skey: str, p_skey: str) -> None:
        self.uin = uin
        self.skey = skey
        self.p_skey = p_skey

    @property
    def gtk2(self) -> str:
        """由 p_skey 计算 g_tk。"""
        hash_val = 5381
        for ch in self.p_skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    def cookies(self) -> dict[str, str]:
        return {
            "uin": f"o{self.uin}",
            "skey": self.skey,
            "p_skey": self.p_skey,
        }

    def headers(self) -> dict[str, str]:
        return {
            "User-Agent": USER_AGENT,
            "referer": f"https://user.qzone.qq.com/{self.uin}",
            "origin": "https://user.qzone.qq.com",
            "Host": "user.qzone.qq.com",
            "Connection": "keep-alive",
        }


class QzonePublisher:
    def __init__(
        self,
        *,
        manual_cookie: str = "",
        cookie_ttl: int = 600,
        timeout: int = 20,
    ) -> None:
        self._manual_cookie = (manual_cookie or "").strip()
        self._cookie_ttl = max(0, int(cookie_ttl))
        self._timeout = max(5, int(timeout))
        self._bot: Any = None
        self._ctx: QzoneContext | None = None
        self._last_refresh = 0.0
        self._lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None

    # ---------- 依赖注入 ----------

    def attach_bot(self, bot: Any) -> None:
        """绑定 aiocqhttp 的 CQHttp 实例，用于取 Cookie。"""
        if bot is not None:
            self._bot = bot

    # ---------- Cookie ----------

    async def _fetch_cookie_string(self) -> str:
        if self._manual_cookie:
            return self._manual_cookie

        bot = self._bot
        if bot is None:
            raise QzoneAuthError("拿不到 OneBot 客户端实例，无法自动取 Cookie")

        getter = getattr(bot, "get_cookies", None)
        if getter is None:
            raise QzoneAuthError("当前 OneBot 实现不支持 get_cookies 接口")

        payload = await getter(domain=QZONE_DOMAIN)
        raw = ""
        if isinstance(payload, dict):
            raw = str(payload.get("cookies") or "").strip()
        elif isinstance(payload, str):
            raw = payload.strip()
        if not raw:
            raise QzoneAuthError(
                "get_cookies 返回空。通常是 QQ 客户端还没访问过空间，"
                "先在电脑上用那个号打开一次 qzone.qq.com 再试；"
                "或者把 Cookie 填进 manual_cookie 配置项。"
            )
        return raw

    async def _refresh_ctx(self) -> QzoneContext:
        cookie_str = await self._fetch_cookie_string()
        jar = {k: v.value for k, v in SimpleCookie(cookie_str).items()}

        uin_text = str(jar.get("uin", "0")).strip()
        if uin_text[:1].lower() == "o":
            uin_text = uin_text[1:]
        if not uin_text.isdigit() or not int(uin_text):
            raise QzoneAuthError(f"Cookie 里的 uin 不合法：{jar.get('uin')!r}")

        skey = str(jar.get("skey", "")).strip()
        p_skey = str(jar.get("p_skey", "")).strip()
        if not p_skey:
            # p_skey 缺失时退化用 skey，很多环境下仍可发布，但可能被拒
            logger.warning(
                "QQ 空间 Cookie 里没有 p_skey，退化为 skey 计算 g_tk。"
                "如果发布报权限错误，请先手动访问一次 qzone.qq.com 再重试。"
            )
            p_skey = skey
        if not skey:
            raise QzoneAuthError("Cookie 里既没有 skey 也没有 p_skey")

        self._last_refresh = monotonic()
        return QzoneContext(uin=int(uin_text), skey=skey, p_skey=p_skey)

    def _expired(self) -> bool:
        if self._ctx is None:
            return True
        if self._cookie_ttl <= 0:
            return True
        return monotonic() - self._last_refresh >= self._cookie_ttl

    async def get_ctx(self, *, force: bool = False) -> QzoneContext:
        async with self._lock:
            if force or self._expired():
                self._ctx = await self._refresh_ctx()
            assert self._ctx is not None
            return self._ctx

    async def invalidation(self) -> None:
        async with self._lock:
            self._ctx = None
            self._last_refresh = 0.0

    # ---------- HTTP ----------

    async def _get_http(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._http

    async def close(self) -> None:
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    # ---------- 发布 ----------

    async def publish(self, text: str, ugc_right: int = 1, *, retry: int = 0) -> dict:
        """发布一条说说，返回 {ok, code, message, tid}。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("日记正文为空，拒绝发布")

        ctx = await self.get_ctx(force=False)
        data = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": str(ugc_right),
            "to_sign": "0",
            "hostuin": str(ctx.uin),
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{ctx.uin}",
        }

        http = await self._get_http()
        async with http.post(
            PUBLISH_URL,
            params={"g_tk": ctx.gtk2, "uin": str(ctx.uin)},
            data=data,
            headers=ctx.headers(),
            cookies=ctx.cookies(),
        ) as resp:
            status = resp.status
            raw = await resp.text()

        result = self._parse(raw, status)

        # 登录失效则换一次 Cookie 重试
        if not result["ok"] and result["code"] in LOGIN_EXPIRED_CODES and retry < 1:
            logger.warning(f"QQ 空间返回登录失效（code={result['code']}），重新获取 Cookie 后重试")
            await self.invalidation()
            return await self.publish(text, ugc_right, retry=retry + 1)

        return result

    @staticmethod
    def _parse(raw: str, status: int) -> dict:
        payload: dict[str, Any] = {}
        text = (raw or "").strip()

        if text:
            try:
                loaded = json.loads(text)
                if isinstance(loaded, dict):
                    payload = loaded
            except Exception:
                # 可能是 JSONP 回调或夹带 HTML，退回正则捞字段
                match = re.search(r"\{.*\}", text, re.S)
                if match:
                    try:
                        loaded = json.loads(match.group(0))
                        if isinstance(loaded, dict):
                            payload = loaded
                    except Exception:
                        payload = {}

        code = payload.get("code")
        if not isinstance(code, int):
            try:
                code = int(code)
            except Exception:
                code = -1

        message = str(payload.get("message") or payload.get("msg") or "").strip()

        tid = ""
        data_field = payload.get("data")
        if isinstance(data_field, dict):
            tid = str(data_field.get("tid") or data_field.get("feedid") or "").strip()
        if not tid:
            match = re.search(r'"tid"\s*:\s*"([^"]+)"', raw or "")
            if match:
                tid = match.group(1)

        ok = code == 0 and bool(tid)
        if code == 0 and not tid:
            message = message or "接口返回成功但没拿到 tid，可能未真正发布"
        if status >= 400 and not message:
            message = f"HTTP {status}"

        logger.debug(f"QQ 空间发布响应: status={status} code={code} tid={tid} msg={message}")
        return {"ok": ok, "code": code, "message": message, "tid": tid}
