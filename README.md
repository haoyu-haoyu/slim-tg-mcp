# slim-tg-mcp

[![CI](https://github.com/haoyu-haoyu/slim-tg-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/haoyu-haoyu/slim-tg-mcp/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](https://github.com/haoyu-haoyu/slim-tg-mcp/blob/main/LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org)

A slim, security-hardened **Telegram MCP server** for [Claude Code](https://claude.com/claude-code) and any MCP-compatible client.

> 一个精简、安全加固的 **Telegram MCP 服务器**，专为 Claude Code 与任何兼容 MCP 的客户端设计。

---

## English

### Why slim-tg-mcp?

The popular `chigwell/telegram-mcp` exposes **73 tools** — that's roughly
**11 000 tokens** permanently consumed in every conversation. This project
keeps only **8 search/read tools** as MCP and ships every write/admin
operation as a **lazy-load Skill**. Same capabilities (now with feature
parity at v0.4.0 across **11 Skills**), **~87 % less context overhead**,
and a security model designed from day one to survive prompt injection.

### Architecture at a glance

```
┌───────────────────────┐
│ Claude Code / Desktop │
└───────────┬───────────┘
            │
   ┌────────┴─────────┐
   ▼                  ▼
[Slim MCP — 8 tools]  [Skills — 11 lazy-loaded]
  search/read           messaging / group-admin (13 ops)
  download              contacts / media-upload
  resolve               polls (incl. edit) / scheduling (incl. edit)
                        profile (incl. 2FA) / export
                        privacy / folders / stickers-gifs
   │                  │
   └─────────┬────────┘
             ▼
   [TG Daemon (Unix socket)]
     ├─ multi-account session pool
     ├─ AES-GCM session storage + Keychain
     ├─ prompt-injection sanitizer
     ├─ flock-based singleton + audit log
     └─ Telethon (MTProto)
             ▼
        Telegram
```

### What's in the box (v0.4.0)

**Always-loaded MCP tools** (~1.5 k tokens):

| Tool | Purpose |
|---|---|
| `tg_search_global` | Search keyword across all chats |
| `tg_search_in_chat` | Search a specific chat (sender / date filters) |
| `tg_list_dialogs` | Discover chat IDs |
| `tg_get_messages` | Read recent messages |
| `tg_get_message_context` | N before/after a hit |
| `tg_resolve_entity` | username / link → marked peer ID |
| `tg_chat_info` | Title, kind, member count |
| `tg_download_media` | Download media into the daemon's safe dir |

**Lazy-loaded Skills** (described, not preloaded):

- `tg-messaging` — send / edit / delete / forward / pin / react / mark-read
- `tg-group-admin` — create groups, add/kick/ban, invites, rename, leave; **channel admin** (participants, signatures, slow-mode, discussion, admin-log)
- `tg-contacts` — add (E.164 enforced), delete, block, unblock, search
- `tg-media-upload` — send local files (photo / video / document / voice)
- `tg-polls` — create / close / inspect / **edit** (anonymous, public, multiple, quiz)
- `tg-scheduling` — schedule / list / cancel / **edit** + draft save/get/clear
- `tg-profile` — update name / bio / username / photo / status / **2FA** (set / change / remove)
- `tg-export` — export chat history to local disk as JSON + media
- `tg-privacy` — read / change all 10 privacy keys with the full 6-rule grammar
- `tg-folders` — list / create-or-update / delete chat folders (dialog filters)
- `tg-stickers-gifs` — saved-GIF list+send, sticker-pack list / resolve / send

### Security highlights

For the full threat model see [`docs/security.md`](docs/security.md).
Headline defenses:

- **Encrypted sessions** — AES-GCM at rest, data key in OS Keychain (macOS
  Keychain / libsecret / Windows DPAPI), with scrypt-based passphrase
  fallback when the keychain is unavailable.
- **Prompt-injection defense** — every Telegram message is sanitized
  (zero-width chars stripped, common injection markers neutralized) and
  wrapped in `<tg_msg trust="high|medium|low">` tags with provenance.
  Forwards are automatically downgraded so an attacker can't reach
  high-trust by getting the user to forward their message.
- **TOCTOU-resistant file ops** — all caller-supplied paths
  (`tg-media-upload`, `tg-export`, `tg-profile photo`) go through
  `lstat → O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC open → fstat (st_dev,
  st_ino) match`. Telethon receives the **fd**, never the path. Export
  uses `dir_fd`-relative ops the whole way down so the export root is
  never re-resolved by name.
- **Singleton daemon** — `fcntl.flock` is the authoritative liveness
  signal (kernel auto-releases on process exit). Pid-based shutdown is
  replaced with a daemon-side `/shutdown` RPC bound to a per-process
  `instance_id`; SIGTERM is only a fallback for transport failure.
- **Path safety** — runtime artifacts (socket, lock, pid) live in
  `$XDG_RUNTIME_DIR` (validated for ownership / 0700) or `/tmp/tgmcp-<uid>`.
  Persistent paths come from `pwd.getpwuid(getuid())`, **ignoring `$HOME`**.
- **Uniform 400 schema surface** — every Pydantic / model-validator
  failure returns `400 {"error":"ValidationError", ...}`, replacing
  FastAPI's mixed 400/422. Single status code for CLI / Skill /
  `DaemonClient` consumers to branch on.
- **Audit log** — every write operation appends to
  `~/.config/tgmcp/audit.log`. Sensitive content (passphrases, full
  paths, message bodies, phone numbers, 2FA passwords, privacy
  user-id allowlists) is never logged.

### Install

From PyPI (recommended):

```bash
pip install slim-tg-mcp
```

Or from source (for hacking on it):

```bash
git clone https://github.com/haoyu-haoyu/slim-tg-mcp.git
cd slim-tg-mcp
pip install -e .
```

### First-time setup

```bash
# 1. Get api_id / api_hash from https://my.telegram.org
export TG_API_ID=123456
export TG_API_HASH=abcdef0123456789...

# 2. Login (interactive — Telegram will SMS a code)
tgmcp init --label main

# 3. Start the daemon
tgmcp daemon start --account main
tgmcp daemon status
```

### Wire up Claude Code

`~/.claude.json` (or per-project `.mcp.json`):

```json
{
  "mcpServers": {
    "telegram": {
      "command": "tgmcp-mcp"
    }
  }
}
```

`~/.claude/settings.json`:

```json
{
  "skillsDirs": [
    "/absolute/path/to/slim-tg-mcp/skills"
  ]
}
```

### CLI cheat sheet

```bash
tgmcp init [--label X] [--passphrase]      # First-time login
tgmcp account list                         # Saved accounts (* = active)
tgmcp account add <label>                  # Add another account
tgmcp account use <label> [--passphrase]   # Switch the running daemon
tgmcp account remove <label>

tgmcp daemon start [--account X] [--passphrase] [--foreground]
tgmcp daemon status
tgmcp daemon stop
```

### Multi-account

Sessions are loaded lazily on first switch and cached server-side, so
subsequent switches are instant. The passphrase for `--passphrase`
accounts is read via hidden prompt or `--passphrase-stdin` — never on
argv. Per-label `asyncio.Lock` serializes concurrent switches against the
same cold label so two parallel callers can never end up with two live
`TGSession` objects pointing at the same account.

### Real-account end-to-end test

```bash
TGMCP_E2E_CONFIRM=yes python scripts/e2e_smoke.py
```

This walks daemon-up → list → send to Saved Messages → search → edit →
delete (verified via post-delete search) → daemon-down. It refuses to run
without the explicit `TGMCP_E2E_CONFIRM=yes` so you don't fire it by
accident; use a burner account, not your main.

### Development

```bash
pip install -e ".[dev]"
pytest tests/                # 365 unit tests at v0.4.0
ruff check src/ tests/
```

CI runs lint and the full suite on Python 3.10 / 3.11 / 3.12 on every
push to `main` and on every PR.

### Project layout

```
slim-tg-mcp/
├── src/tgmcp/
│   ├── client.py                   # Daemon Unix-socket client
│   ├── daemon/
│   │   ├── server.py               # FastAPI on Unix socket
│   │   ├── telegram.py             # Telethon wrapper (TGSession)
│   │   ├── auth.py                 # Encrypted session storage
│   │   ├── paths.py                # Runtime/persistent path resolution
│   │   ├── sanitizer.py            # Prompt-injection defense
│   │   └── audit.py                # Append-only audit log
│   ├── mcp_server/server.py        # 8 MCP tools (stdio)
│   └── cli/main.py                 # `tgmcp` CLI
├── skills/                         # 11 lazy-load Skills
│   ├── tg-messaging/
│   ├── tg-group-admin/
│   ├── tg-contacts/
│   ├── tg-media-upload/
│   ├── tg-polls/
│   ├── tg-scheduling/
│   ├── tg-profile/
│   ├── tg-export/
│   ├── tg-privacy/
│   ├── tg-folders/
│   └── tg-stickers-gifs/
├── docs/
│   └── security.md                 # Full threat model
├── scripts/
│   ├── smoke.sh                    # Local sanity check (no real account)
│   └── e2e_smoke.py                # Real-account end-to-end
└── tests/                          # 365 unit tests at v0.4.0
```

### License

[Apache-2.0](LICENSE). The Phase 1 MCP-tool surface was inspired by
`chigwell/telegram-mcp` (also Apache-2.0); the architecture, security
model, skill split, and implementation are independent.

---

## 中文

### 为什么是 slim-tg-mcp？

社区里热门的 `chigwell/telegram-mcp` 暴露 **73 个工具**——也就是说每次
对话都会**常驻消耗约 11 000 token**。本项目只把 **8 个搜索/读取工具**做
成 MCP，把所有写入/管理操作做成 **按需加载的 Skill**。v0.4.0 起共 **11
个 Skill**，能力与 chigwell 全面对齐；**上下文开销减少约 87%**，并且从
第一天起就按"防 prompt injection"思路设计。

### 架构总览

```
┌───────────────────────┐
│ Claude Code / Desktop │
└───────────┬───────────┘
            │
   ┌────────┴─────────┐
   ▼                  ▼
[Slim MCP - 8 工具]  [Skills - 11 个，按需加载]
  搜索/读取            messaging / group-admin (13 个子命令)
  下载                 contacts / media-upload
  解析                 polls (含 edit) / scheduling (含 edit)
                       profile (含 2FA) / export
                       privacy / folders / stickers-gifs
   │                  │
   └─────────┬────────┘
             ▼
   [TG Daemon - Unix socket]
     ├─ 多账号 session pool
     ├─ AES-GCM 会话存储 + Keychain
     ├─ Prompt-injection 清洗器
     ├─ flock 单例锁 + 审计日志
     └─ Telethon (MTProto)
             ▼
          Telegram
```

### 功能清单（v0.4.0）

**常驻 MCP 工具**（约 1.5k tokens）：

| 工具 | 用途 |
|---|---|
| `tg_search_global` | 跨所有对话关键字搜索 |
| `tg_search_in_chat` | 单对话内搜索（按 sender / 日期过滤） |
| `tg_list_dialogs` | 发现 chat ID |
| `tg_get_messages` | 读最近消息 |
| `tg_get_message_context` | 命中前后 N 条 |
| `tg_resolve_entity` | username / 链接 → marked peer ID |
| `tg_chat_info` | 标题、类型、人数 |
| `tg_download_media` | 下载媒体到 daemon 的安全目录 |

**按需加载 Skill**（仅描述常驻，触发时才载入）：

- `tg-messaging` — 发 / 编辑 / 删 / 转发 / 置顶 / 反应 / 已读
- `tg-group-admin` — 建群、加/踢/封禁/解除、邀请链接、改名、退群；**频道高级管理**（成员分页、署名开关、慢速模式、绑定讨论组、admin-log）
- `tg-contacts` — 加（强校验 E.164）/ 删 / 屏蔽 / 解除 / 搜索
- `tg-media-upload` — 上传本地文件（图片 / 视频 / 文档 / 语音）
- `tg-polls` — 创建 / 关闭 / 看结果 / **编辑**（匿名、公开、多选、Quiz）
- `tg-scheduling` — 定时消息（含**编辑**）+ 草稿管理
- `tg-profile` — 改名字 / 简介 / 用户名 / 头像 / 在线状态 / **2FA**（设置 / 修改 / 移除）
- `tg-export` — 导出聊天历史为 JSON + 媒体
- `tg-privacy` — 读取 / 修改全部 10 个隐私键（6 条规则的完整语法）
- `tg-folders` — 列出 / 新增或更新 / 删除 对话文件夹（dialog filters）
- `tg-stickers-gifs` — 收藏 GIF list+send，贴纸包 list / 解析 / send

### 安全特性

完整威胁模型见 [`docs/security.md`](docs/security.md)，要点如下：

- **会话加密**：AES-GCM 落盘，数据密钥存 OS Keychain（macOS Keychain /
  libsecret / Windows DPAPI），keychain 不可用时降级到 scrypt 加密的
  passphrase 模式。
- **Prompt-injection 防御**：每条 TG 消息都先清洗（剥零宽字符、中性化常
  见注入标记），再用 `<tg_msg trust="high|medium|low">` 包裹并附 sender
  / chat 元数据。**转发自动降权**——攻击者无法借"让用户帮我转发一下"达
  到 high trust。
- **TOCTOU 防御**：所有调用者传入的文件路径
  （`tg-media-upload` / `tg-export` / `tg-profile photo`）都过
  `lstat → O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC open → fstat (st_dev,
  st_ino) 对比`。Telethon 收到的是 **fd**，不再二次按名字解析。导出全
  程用 `dir_fd` 相对操作，导出根目录不会被重新解析。
- **单例 daemon**：用 `fcntl.flock` 作为权威活性信号（内核在进程退出时
  自动释放）。停止 daemon 不再依赖 SIGTERM；改用 daemon 自带 `/shutdown`
  RPC 并绑定每进程随机 `instance_id`，SIGTERM 仅作 transport 失败兜底。
- **路径安全**：运行时产物（socket / lock / pid）放 `$XDG_RUNTIME_DIR`
  （检查 owner / 0700）或 `/tmp/tgmcp-<uid>`。持久路径用
  `pwd.getpwuid(getuid())`，**忽略 `$HOME`**（防注入）。
- **统一 400 校验面**：所有 Pydantic / model-validator 失败统一返回
  `400 {"error":"ValidationError", ...}`，替代 FastAPI 默认的 400/422
  混合。CLI / Skill / `DaemonClient` 一个状态码全搞定。
- **审计日志**：每个写操作 append 到 `~/.config/tgmcp/audit.log`。敏感
  内容（passphrase、绝对路径、消息正文、电话全号、2FA 密码材料、隐私规
  则的 user_id 白名单）从不明文记录。

### 安装

PyPI（推荐）：

```bash
pip install slim-tg-mcp
```

源码（如果想改代码）：

```bash
git clone https://github.com/haoyu-haoyu/slim-tg-mcp.git
cd slim-tg-mcp
pip install -e .
```

### 首次配置

```bash
# 1. 去 https://my.telegram.org 申请 api_id / api_hash
export TG_API_ID=123456
export TG_API_HASH=abcdef0123456789...

# 2. 登录（交互式，Telegram 会发短信验证码）
tgmcp init --label main

# 3. 启动 daemon
tgmcp daemon start --account main
tgmcp daemon status
```

### 接到 Claude Code

`~/.claude.json`（或项目级 `.mcp.json`）：

```json
{
  "mcpServers": {
    "telegram": {
      "command": "tgmcp-mcp"
    }
  }
}
```

`~/.claude/settings.json`：

```json
{
  "skillsDirs": [
    "/absolute/path/to/slim-tg-mcp/skills"
  ]
}
```

### CLI 速查表

```bash
tgmcp init [--label X] [--passphrase]       # 首次登录
tgmcp account list                          # 列已存账号（*=活跃）
tgmcp account add <label>                   # 加另一个账号
tgmcp account use <label> [--passphrase]    # 切换运行中 daemon 的账号
tgmcp account remove <label>

tgmcp daemon start [--account X] [--passphrase] [--foreground]
tgmcp daemon status
tgmcp daemon stop
```

### 多账号

切换时 daemon 才**懒加载**对应 session 并缓存，后续切换是常数时间。
`--passphrase` 账号通过隐藏 prompt 或 `--passphrase-stdin` 读密码——**永
不进 argv**。每个 label 一把 `asyncio.Lock`，并发切换不会让同一账号被起
两份 `TGSession`。

### 真实账号 e2e 验证

```bash
TGMCP_E2E_CONFIRM=yes python scripts/e2e_smoke.py
```

该脚本：启 daemon → 列对话 → 发到「我的消息」→ 搜回来 → 编辑 → 删除
（删后重搜确认）→ 关 daemon。强制要 `TGMCP_E2E_CONFIRM=yes` 防误触；
**强烈建议用小号**，不要用主账号。

### 开发

```bash
pip install -e ".[dev]"
pytest tests/                # v0.4.0 共 365 个单元测试
ruff check src/ tests/
```

CI 在 main 推送和 PR 时跑 lint + 全测试，覆盖 Python 3.10 / 3.11 / 3.12。

### 项目结构

```
slim-tg-mcp/
├── src/tgmcp/
│   ├── client.py                   # Daemon Unix socket 客户端
│   ├── daemon/
│   │   ├── server.py               # FastAPI on Unix socket
│   │   ├── telegram.py             # Telethon 包装（TGSession）
│   │   ├── auth.py                 # 加密会话存储
│   │   ├── paths.py                # 运行时/持久路径解析
│   │   ├── sanitizer.py            # 注入防御
│   │   └── audit.py                # 仅追加审计日志
│   ├── mcp_server/server.py        # 8 个 MCP 工具（stdio）
│   └── cli/main.py                 # `tgmcp` CLI
├── skills/                         # 11 个按需加载 Skill
├── docs/
│   └── security.md                 # 完整威胁模型
├── scripts/
│   ├── smoke.sh                    # 本地 sanity check（不联网）
│   └── e2e_smoke.py                # 真账号端到端
└── tests/                          # v0.4.0 共 365 个单元测试
```

### 协议

[Apache-2.0](LICENSE)。Phase 1 的 MCP 工具面（搜索/读取 8 件套）参考了
`chigwell/telegram-mcp`（同样 Apache-2.0），其余架构、安全模型、Skill 拆
分、所有实现均为独立完成。
