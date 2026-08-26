# Phantasm

Phantasm 是一款面向 **AstrBot** 的发帖监听插件：它会定时检查一批配置好的 **Bilibili**
动态账号与 **X / Twitter** 推文账号，把每一条**新帖**渲染成对应平台 UI 风格的**卡片图片**，
然后自动投递到每个账号各自配置的 **QQ 群聊 / 私聊** 目标。

- 平台：**Bilibili（动态）** 与 **X / Twitter（推文）**。
- 投递：AstrBot 主动消息（`context.send_message`），支持群聊 / 私聊。
- 去重：本地持久化已处理帖子 ID，同一帖绝不重复投递。
- 容错：单个账号抓取失败不影响其它账号；网络失败自动重试、降级、不崩溃。

## 功能

| 能力 | 说明 |
|---|---|
| 账号监听 | 可配置轮询间隔（含随机抖动），后台异步任务定时抓取 |
| Bilibili | REST API（用户动态接口 `feed/space`），需 Cookie |
| X/Twitter | 官方 API v2（`/2/users/…/tweets`，需 Bearer Token/额度）**或 RSSHub 第三方**（`credentials.x.mode=rss`，免费不占官额） |
| 卡片渲染 | **html（Playwright/Chromium，推荐，彩色 emoji）** 或 Pillow 兜底；B 站粉 / X 黑白主题、圆角、媒体网格、统计条 |
| 去重 | `processed.json` 持久化已处理帖子 ID，有界历史，杜绝重复投递 |
| 独立目标 | 每个账号可配置一个或多个 `group` / `private` 目标 |
| 管理命令 | `/phantasm` 状态 / 立即检查 / 订阅列表 / 增删账号与目标 / 暂停恢复 |
| 设置页 | 插件自带 Web 设置页（凭据脱敏，不回显明文） |
| 容错 | 代理、超时、重试、错误分类；单账号失败不影响整体 |

> **监听范围（Bilibili）**：插件监听的是用户的**动态流**（`feed/space`），覆盖 B 站的**动态**
> 与**投稿**——UP 主发布新视频时，B 站会自动生成一条「视频动态」（`DYNAMIC_TYPE_AV`），
> 因此**新视频投稿也会被监听到**并渲染成视频卡（含封面/标题/播放量/时长）。但它不是独立监听
> 「投稿列表」接口，而是从动态流捕获。若某条投稿未生成动态（个别情况），则不会触发。
> **X/Twitter** 监听的是推文（`tweets`），含普通推文/转发。

## 环境要求

- AstrBot `>=4.16,<5`，`aiocqhttp`（OneBot v11）平台
- Python 3.10+；依赖：`httpx`、`Pillow`（见 `requirements.txt`）
- Bilibili：需要一个可用的 Cookie（至少 `buvid3`/`buvid4`；**建议含登录态 `SESSDATA`** 以降低风控）
- X/Twitter：官方 API v2 的 **Bearer Token**（App-only token，读公开推文）；**或**用 RSSHub 第三方（`credentials.x.mode=rss`，需一个可达的 RSSHub 实例，无需官方额度）

> 说明：默认用 **HTML→图片（`render.backend=html`，Playwright + Chromium）** 渲染卡片，
> 能原生输出**彩色 emoji**，布局也更精细。若容器未装 Playwright/Chromium，插件会**自动回退到
> Pillow**（此时 emoji 被移除）。关于字体：
> - CN 插件**内置 Noto Sans CJK SC 子集**（约 3.5MB，OFL），中文开箱即显示；
> - HTML 后端通过 `@font-face` 引入内置中文字体 + 下载的 Noto Color Emoji（彩色 emoji）；
> - 手动安装 HTML 后端依赖（Docker 容器内）：
>   ```bash
>   pip install playwright && playwright install --with-deps chromium
>   ```
>   （若网络受限，可用镜像源或先 `playwright install chromium` 再补系统字体。）

## 安装

```bash
# 1. 进入 AstrBot 插件目录（Docker 部署请确认该目录已挂载）
cd /path/to/AstrBot/data/plugins

# 2. 放置插件（目录名建议为 astrbot_plugin_phantasm）
#    - 把本仓库内容放进去即可（目录名 astrbot_plugin_phantasm）

# 3. 安装依赖（AstrBot 若无 Pillow）
pip install -r astrbot_plugin_phantasm/requirements.txt

# 4. 在 AstrBot WebUI 重载插件
```

首次使用建议把 `config.example.json` 复制为插件数据目录下的 `config.json` 再编辑，或直接使用
设置页 / `/phantasm` 命令来管理。

## 配置说明

插件的唯一权威配置来源是：

```text
data/plugin_data/astrbot_plugin_phantasm/config.json
```

以下为完整字段。`config.example.json` 是可运行的示例：

```jsonc
{
  "enabled": true,                     // 插件是否启用（false 时后台不轮询）
  "poll_interval_seconds": 300,        // 轮询间隔（秒）
  "poll_jitter_seconds": 10,           // 间隔随机抖动（秒），避免固定节奏被风控

  "image": {
    "output_mode": "keep",             // keep=保留卡片 / temp=发送后删除
    "dir": "cards",                    // 卡片输出目录（相对 data_dir）
    "format": "png",                   // png / jpg
    "aspect_ratio": "1:1",             // 媒体宽高比：1:1 / 4:3 / 16:9 / 3:4 …
    "corner_radius": 24                // 媒体图圆角半径
  },

  "render": {
    "backend": "html",                 // html=Playwright/Chromium(彩色emoji,推荐) / pillow=Pillow
    "font_path": "",                   // 中文字体路径，空则自动探测
    "font_size_scale": 1.0,            // 整体字号缩放
    "emoji_mode": "keep",              // keep=保留(html后端原生渲染彩色emoji) / strip=移除
    "name_color": "#000000",
    "handle_color": "#536471",
    "verified_badge": true,            // X 蓝V徽标
    "theme": {                          // 每平台色板，可覆盖
      "bilibili": { "primary":"#FB7299", "background":"#FFFFFF", "text":"#18191C", "subtext":"#9499A0", "accent":"#00AEEC" },
      "x":        { "primary":"#000000", "background":"#FFFFFF", "text":"#0F1419", "subtext":"#536471", "accent":"#1D9BF0" }
    }
  },

  "send": {
    "caption_format": "{platform} · {author} · {time}", // 卡片附带的文字说明模板
    "send_caption": true,              // 是否附带文字说明
    "link_to_post": true,              // 在「文字说明」末尾附上原贴链接（QQ 内可点击）；卡片图片内不再显示链接
    "media_max": 4,                    // 单帖最多渲染图片数
    "max_jobs_per_account": 10,        // 每账号每轮最多投递新帖数
    "send_text_when_no_media": true
  },

  "network": {
    "proxy_enabled": false,            // 是否走代理（抓 B 站/X 常用）
    "proxy": "http://127.0.0.1:7897",
    "timeout_seconds": 15,             // 请求超时
    "connect_timeout_seconds": 8,
    "max_retries": 3,                  // 网络/5xx 重试次数
    "retry_backoff_base": 1.5,
    "max_concurrency": 3,              // 多账号并发抓取上限
    "platform_id": ""                  // 主动投递平台前缀，空=自动取运行中平台（aiocqhttp）
  },

  "credentials": {
    "bilibili": {
      "cookie": "",                    // Cookie 值：至少含 buvid3/buvid4，建议含 SESSDATA
      "enable_risk_control_retry": true
    },
    "x": {
      "mode": "api",                   // api（官方 API v2，需额度）/ rss（RSSHub 第三方，免费不占额度）
      "bearer_token": "",              // 官方 API v2 Bearer Token（mode=api 需要）
      "rss_base": "https://rsshub.app",// mode=rss 时的 RSSHub 实例地址（可换自建/其它实例）
      "api_key": "", "api_secret": "",
      "access_token": "", "access_token_secret": ""
    }
  },

  "storage": { "history_limit": 5000, "prune_on_load": true },

  "accounts": [
    {
      "platform": "bilibili",          // bilibili | x
      "account_id": "2",               // bilibili UID；X 可为数字 user_id 或 screen_name
      "display_name": "",              // 可选覆盖显示名
      "enabled": true,
      "targets": [                      // 每个账号独立的投递目标
        { "type": "group", "id": "123456789" },     // QQ 群号
        { "type": "private", "id": "987654321" }    // 私聊对方 QQ 号
        // { "type": "umo", "id": "aiocqhttp:GroupMessage:123456789" } // 高级：直接给 unified_msg_origin
      ]
    }
  ]
}
```

> **凭据安全**：请勿把真实 Cookie / Token 写入并提交 git。`config.json` 在插件数据目录内，
> 已加入 `.gitignore`。日志与设置页均不输出明文（设置页返回的是 `****` 掩码，保存时掩码不会被写回）。

## 使用 / 命令

| 命令 | 作用 |
|---|---|
| `/phantasm` | 查看状态（启用、轮询、去重数、账号数） |
| `/phantasm check` | 立即手动触发一轮检查并投递新帖 |
| `/phantasm list` | 列出订阅账号及其投递目标 |
| `/phantasm history [n]` | 最近 n 条已处理记录（默认 10） |
| `/phantasm add <bilibili\|x> <id> [名称]` | 添加账号（管理员） |
| `/phantasm del <bilibili\|x> <id>` | 删除账号（管理员） |
| `/phantasm target <p> <id> add group <群号>` | 为账号添加群聊目标（管理员） |
| `/phantasm target <p> <id> add private <QQ号>` | 为账号添加私聊目标（管理员） |
| `/phantasm target <p> <id> clear` | 清空该账号的所有目标（管理员） |
| `/phantasm pause` / `/phantasm resume [秒]` | 暂停 / 恢复轮询（管理员） |
| `/phantasm help` | 查看用法 |

示例：

```text
/phantasm add bilibili 2
/phantasm target bilibili 2 add group 123456789
/phantasm target bilibili 2 add private 987654321
/phantasm check
```

## 实现细节

### 抓取（REST API）

- **Bilibili**：`GET https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space?host_mid=<uid>`。
  插件先通过 `finger/spi` 获取 `buvid3`/`buvid4` 并与用户 Cookie 合并，再请求动态。解析
  `data.items[]`，按 `type`（AV/转发/图文/…）提取正文、配图/封面、统计（点赞/评论/转发/播放/弹幕）、
  作者、时间，并处理「转发」场景下的原动态（`orig`）摘要。
- **X**：`GET https://api.twitter.com/2/users/<id>/tweets?tweet.fields=…&expansions=…&media.fields=…`。
  `account_id` 是数字 user_id 直接使用；是 screen_name 先经 `/2/users/by/username/<name>` 解析并缓存。
  提取 `includes.media` 与 `includes.users` 得到媒体与作者，`public_metrics` 得到回复/转发/点赞/引用数。
- **X（RSS 模式）**：`credentials.x.mode=rss` 时改为抓 `GET <rss_base>/twitter/user/<screen_name>`
  （RSSHub），解析 title/描述/pubDate/link/内嵌媒体，**不消耗 X 官方额度**；依赖 RSSHub 实例可用性。

### 鉴权与限流

- **Bilibili**：需 Cookie。匿名 buvid 对**同一 IP 的频繁请求**会触发风控（返回 `-352 / -412`）。
  插件遇到风控会给出提示并建议：填入登录态 `SESSDATA`、增大 `poll_interval_seconds`、关闭频繁手动检查。
- **X**：官方 API v2 严格按项目/用户配额限流（如 read 项目通常 500 req/15min 量级）。请保守设置
  `poll_interval_seconds`，超出配额会得到 429（插件按限流处理，不崩溃）。

### 卡片渲染（HTML / Pillow）

默认用 **HTML（Playwright + Chromium）** 渲染，`render.backend` 可切 `html` / `pillow`。
渲染器按 `post.platform` 选择主题色板与版式，固定宽度、高度随内容自适应：

- **Bilibili 卡片**：粉色平台标识条、圆形头像、昵称 + UID、动态时间、标题、正文、
  媒体网格、统计条（播放/点赞/评论/转发）、平台水印。
- **X 卡片**：黑白主题、头像 + 名称 + 蓝V徽标 + `@handle` + 时间、正文、媒体网格、
  统计条（回复/转发/喜欢/引用）、平台水印。

HTML 后端用 CSS 排版，**彩色 emoji 原生渲染**；若容器未装 Playwright/Chromium（系统依赖未装），
插件自动回退到 Pillow（此时 emoji 移除）。长文本自适应，多图网格，中英文正常。卡片输出到
`data/plugin_data/…/cards/`。

### 投递（AstrBot 主动消息）

用 `event.unified_msg_origin` 的约定拼装目标：

```text
群聊：<platform>:GroupMessage:<群号>
私聊：<platform>:FriendMessage:<QQ号>
UMO ：<platform>:<MessageType>:<target_id>（直接指定）
```

然后 `context.send_message(umo, MessageChain().message(caption).file_image(card_path))`。
平台前缀默认取当前运行中的平台实例（如 `aiocqhttp`），也可用 `network.platform_id` 覆盖。

### 去重

每次抓取后，`Poller` 会把「尚未处理」的新帖（按 `kebab_id = <platform>_<post_id>` 判断）逐条渲染并
投递；**至少有一个目标成功投递后**才写入 `processed.json`，因此同一帖不会重复投递。若某帖连续 3 轮
全部目标投递失败，为避免无限重试，也会标记并记录告警。历史有界（`storage.history_limit`）。

> **添加账号不刷屏**：当某个监听账号第一次被轮询到（尚未基线化）时，插件会先把当前抓到的
> **历史动态全部标记为已处理**（基线化），本轮不投递；之后只投递**新增**的帖子。这样新加账号
> 不会把历史所有动态一次性刷到群里。基线化记录同样持久化在 `processed.json` 的 `baselined` 字段。

## 数据目录

```text
data/plugin_data/astrbot_plugin_phantasm/
├── config.json        # 插件配置（唯一权威来源）
├── processed.json     # 已处理帖子去重记录（有界）
└── cards/             # 生成的卡片图片（output_mode=temp 时发送后删除）
```

## 注意事项 / 潜在限制

- **Bilibili 风控**：匿名 buvid 高频抓取易触发 `-352/-412`。强烈建议配置登录后的 `SESSDATA`
  并增大轮询间隔；插件已做重试与降级，但平台侧限制无法完全绕过。
- **X API 配额**：官方 API v2 有严格限流与费用。请按 `poll_interval_seconds` 保守配置；匿名网页抓取
  （`mode=scrape`）官方不支持，也**未实现**，会明确提示使用 `mode=api`。
- **emoji**：Pillow 无法渲染彩色 emoji。默认 `render.emoji_mode=strip` 移除；如需保留可改 `keep`，
  但可能显示为方框（取决于字体）。
- **中文字体**：Linux/Docker 环境常缺中文字体。插件会在首次渲染前**自动下载 Noto Sans CJK SC**
  到插件 data 目录 `fonts/`；也可设 `render.font_path` 或手动放字体，否则中文显示为方框。
- **主动投递平台支持**：`context.send_message` 不支持所有平台（如 QQ 官方 API 平台不支持）。当前
  以 `aiocqhttp`（OneBot v11）为主，其它平台请先用 `/phantasm list` 确认目标能否命中运行中的平台。
- **单进程约束**：轮询任务运行于插件所在 AstrBot 进程；多实例/重启后以当前配置为准。
- **多图与长文本**：卡片高度随内容自适应，不会裁切正文；媒体最多 `media_max` 张。
- **Cookie 写法**：`credentials.bilibili.cookie` 既可填**纯 Cookie 值**（`SESSDATA=…; buvid3=…`），
  也可直接粘贴 **Cookie Editor 导出的整段请求头**（多行 `:authority: …` / `cookie: …`），插件会自动提取 `cookie:` 行。
- **网络连接（ConnectError）**：连接失败通常是**容器/环境网络**问题，不是代码问题：
  - 插件**默认不读取环境变量代理**（`trust_env=False`），只使用你在 `network` 里显式配置的代理，
    避免容器里某个指向 `127.0.0.1` 的残留 `HTTP(S)_PROXY` 导致 `ConnectError`。
  - 若 AstrBot 运行在 **Docker**：AstrBot 内部访问宿主机的代理，`network.proxy` 应填
    `http://host.docker.internal:7897`（或宿主机局域网 IP），并设 `network.proxy_enabled=true`；
    **不要**用 `127.0.0.1`（在容器里指容器自身，连不上宿主代理）。
  - 若本机可直连 B 站 / X，保持 `proxy_enabled=false` 即可。
  - 排查顺序：全局配置里的 `http_proxy`、容器的 `env | grep -i proxy`、`network.proxy` 三者是否一致可达。

> **开发说明**：AstrBot 通过 `get_handlers_by_module_name` 按**模块路径精确匹配**把
> `@filter.command` / `@filter.event_message_type` 等处理器关联到插件。因此这类**装饰器必须写在
> `main.py` 的 Star 类里**（而非子包 `phantasm/` 中），否则命令不会被注册。本插件的命令实现放在
> `phantasm/commands.py` 的 `CommandsMixin._phantasm_command()`，由 `main.py` 的 `phantasm()`
> 转发调用；子包模块路径会落在插件前缀下，其余模块可正常导入。

## 测试/验收

1. 插件能被 AstrBot 正常加载并注册 `phantasm` 命令 → `/phantasm` 能响应。
2. 配置若干账号（Bilibili + X 各一个）并分别指定不同目标 → 有新帖时只投递到各自目标。
3. 同一帖不会重复投递；无新帖时不产生多余消息（观察日志 / `history`）。
4. 将某账号 `account_id` 填错 → 该账号抓取报错，但不影响其它账号正常投递。
5. 卡片美观、中文正常、信息完整、尺寸合理（见 `cards/` 生成文件）。

## 许可

本项目基于 [MIT License](LICENSE) 开源。
