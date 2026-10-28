# 日记本 · QQ空间

> 对她说一句「晚安」，她以当前人格的口吻写一篇日记，安静地发到 QQ 空间。

一个 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件。日记不是聊天记录的总结，而是独白——
她自己的一天、她注意到的细节、她说不出口的话。

写完之后，那条说说就躺在空间里，第二天你翻到会想起那天。

---

## 效果

```
你：晚安，今天早点睡

（她照常回你一句晚安）

（后台：读人格 → 写日记 → 发说说）
    22:41 今天雨下了一整天，窗户上全是水痕……

（空间里出现一条新说说，可见范围由你决定）
```

成功发布时她**不会**再回复一次「已发布」——睡前不该被打断。发失败了才会告诉你原因。

---

## 特性

| 能力 | 说明 |
| --- | --- |
| 跟着人格走 | 直接读取 AstrBot 当前生效的人格提示词，换人格就换口吻，不用重新配置 |
| 晚安触发 | 命中触发词才写，每天最多一篇 |
| 不打扰 | 成功静默，失败才通知；写日记的过程不会吞掉她原本的回复 |
| 防丢稿 | 发布失败或关机前没发出去的正文会存档，下次开机自动补发 |
| 避免雷同 | 把前几篇的开头传给模型，明确要求不要重复意象和句式 |
| 定时兜底 | Cron + 随机浮动秒数，适合不停机的机器；默认关闭 |
| 可见范围 | 公开 / 好友可见 / 仅自己可见 |
| 可联动 | 装了 [`astrbot_plugin_observe_life`](https://github.com/baiao218/astrbot_plugin_observe_life) 时，日记会带上「今天他做了什么」的底色 |

---

## 安装

在 AstrBot WebUI 的**插件市场**搜索「日记本」，或者：

```bash
cd AstrBot/data/plugins
git clone https://github.com/baiao218/astrbot_plugin_diary_qzone.git
```

重启 AstrBot（或在插件页点「重载插件」），然后在插件配置里填两项必要设置。

### 前置条件

1. **AstrBot ≥ 4.17.0**
2. **QQ 走 OneBot 适配器**（NapCat / Lagrange / LLOneBot 等均可）
3. 机器人账号已经能正常收发消息

Cookie 不需要你手动填：插件通过 OneBot 的 `get_cookies` 扩展接口自动拿。
如果取到的是空值，通常是因为那个 QQ 号还没访问过空间——在电脑上打开一次
`qzone.qq.com` 再试，或者把 Cookie 填进 `manual_cookie`。

---

## 最小配置

装好之后只有两项**必须**确认：

```yaml
# 触发词，逗号分隔
goodnight_keywords: "晚安,睡了,睡觉,睡啦"

# 说说可见范围：64 = 仅自己可见，4 = 好友可见，1 = 所有人可见
ugc_right: "1"
```

其余都有合理默认值。想先看看她写什么，用 `/日记预览`，不会发布。

---

## 命令

| 命令 | 说明 | 权限 |
| --- | --- | --- |
| `/写日记` | 立刻写一篇并发布到 QQ 空间 | 管理员 |
| `/日记预览` | 只生成不发布，检查口吻和长度 | 管理员 |
| `/日记状态` | 触发方式、下次执行时间、Cookie 登录状态、当前人格、可见范围 | 管理员 |

管理员在 AstrBot 的 `cmd_config.json` → `admins_id` 里配置。

---

## 配置项

<details>
<summary>展开完整配置表</summary>

### 触发

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `goodnight_enable` | bool | `true` | 听到「晚安」就写日记 |
| `goodnight_keywords` | string | `晚安,睡了,睡觉,睡啦` | 触发词，逗号分隔，命中任意一个即触发 |
| `goodnight_notify` | bool | `false` | 晚安触发时也回一条发布结果（默认关，别破坏睡前气氛） |
| `owner_ids` | string | 空 | 只在这些 QQ 的私聊里响应晚安，逗号分隔。留空回退到 AstrBot 的 `admins_id`；填 `*` 不限制。**别人对她说晚安不会占用你今天的名额，也不会发到你空间** |

### 内容

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `persona_id` | string | 空 | 用哪个人格，留空跟随当前默认人格 |
| `diary_prompt` | text | 见默认值 | 任务提示词，支持 `{min_chars}` `{max_chars}` `{recent}` `{activity}` `{today}` |
| `min_chars` | int | `150` | 最短字数 |
| `max_chars` | int | `400` | 最长字数，超出按句号截断 |
| `avoid_repeat_count` | int | `3` | 传给模型避免重复的历史条数 |
| `provider_id` | string | 空 | 指定 LLM 提供商，留空用当前 |
| `activity_feed_enable` | bool | `true` | 读「观测生活」的今日摘要作为素材 |

### 发布

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `ugc_right` | string | `1` | `1` 公开 / `4` 好友 / `64` 仅自己 |
| `cookie_ttl` | int | `600` | Cookie 缓存秒数，`0` 表示每次都重取 |
| `manual_cookie` | string | 空 | 自动取 Cookie 失败时的兜底 |
| `timeout` | int | `20` | 网络超时秒数 |
| `notify_umo` | string | 空 | 把日记同步一份到某个会话，填 `unified_msg_origin` |

### 定时兜底

| 键 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `publish_cron` | string | 空 | Cron（分 时 日 月 周），留空关闭。例：`15 22 * * *` |
| `publish_offset` | int | `600` | 前后随机浮动秒数，避免每天同一秒发帖 |
| `dry_run` | bool | `true` | 定时任务只生成不发布（**不影响**晚安触发和 `/写日记`） |

</details>

---

## 它是怎么发出去的

```
私聊出现「晚安」
   ↓  filter.regex 不拦截事件，她照常回复
读当前人格提示词 + 拼任务提示词
   ↓
调用 LLM 生成日记 → 清洗（去代码块、去标题、去日期行、按句号截断）
   ↓
先存一份 pending.json（防止关机丢稿）
   ↓
OneBot get_cookies(domain=user.qzone.qq.com) → skey / p_skey
   ↓  djb2 变体哈希算出 g_tk
POST emotion_cgi_publish_v6
   ↓
成功 → 清 pending；失败 → 保留 pending，下次开机补发
```

几个不那么显然的设计：

- **为什么不做「每日 23 点定时」**：会关机的机器上，定时任务根本跑不到那个时间点。
  所以主路径挂在你说晚安的动作上——人都要睡了，日记也该写了。
- **为什么要存 pending**：关机瞬间网络断了、Cookie 过期、接口抽风，正文不能就这么没了。
  补发窗口是 36 小时，超过就丢弃，避免把三天前的日记当成今天的发出去。
- **为什么用 `get_cookies` 而不是自己扫码**：复用机器人已经登录的会话，零额外操作。
- **为什么改口吻不用改代码**：人格提示词是运行时读的，换人格日记就跟着变。

发布接口的协议细节参考了 [Zhalslar/astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone)（MIT）
的公开实现，在此致谢。

---

## 常见问题

**发不出去，提示「get_cookies 返回空」**

那个 QQ 号还没访问过空间。在电脑上用它打开一次 `qzone.qq.com`，再发 `/写日记`。
如果一直不行，从浏览器开发者工具复制 `uin` / `skey` / `p_skey` 填进 `manual_cookie`。

**提示登录失效（code=-3000 之类）**

插件会自动换一次 Cookie 重试。仍然失败说明机器人登录态本身有问题，检查 OneBot 那边。

**她写得很像聊天总结**

说明人格提示词里"你在和用户对话"的味道太重。可以在 `diary_prompt` 里加重一句：
「这是写给自己看的日记，不是在回复他」。

**每天只有一篇吗**

晚安触发是每天一篇（按本地时区）。想多写就手动 `/写日记`。

**能发到别的平台吗**

发布走的是 QQ 空间协议，只有 QQ。生成部分与平台无关，但发布这一环是 QQ 专属。

---

## 许可

MIT。见 [LICENSE](LICENSE)。
