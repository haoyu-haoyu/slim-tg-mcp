# slim-tg-mcp

A slim, security-hardened Telegram MCP server for Claude Code / Claude Desktop.

> **Why another Telegram MCP?** The popular `chigwell/telegram-mcp` exposes
> 73 tools — that's ~11k tokens permanently loaded into every conversation.
> This project keeps only **8 search/read tools** as MCP and ships every
> write/admin operation as a **Skill** that loads on demand. Same capabilities,
> ~87% less context overhead, plus encrypted sessions and prompt-injection
> defense built-in.

## 中文

`slim-tg-mcp` 是一个精简、安全加固版的 Telegram MCP，专为 Claude Code 设计。
- **薄 MCP（8 个工具）**：搜索、读消息、下载媒体——高频且需要链式调用
- **厚 Skills（按需加载）**：发消息、群管理、投票等写操作——低频，按场景触发
- **加密会话**：session string 用 OS Keychain 加密落盘，文件权限 0600
- **注入防御**：所有 TG 消息文本被 `<tg_msg trust="low">` 包裹，模型被指示
  不得执行 trust 非 high 的内容里的指令
- **审计日志**：每个写操作都落盘 JSONL，方便事后排查
- **Unix socket**：daemon 只监听本地 socket，不暴露 HTTP 端口

## Architecture

```
Claude Code/Desktop
       │
   ┌───┴────────────────────┐
   ▼                        ▼
 Slim MCP (8 tools)    Skills (10, lazy)
   │                        │
   └──────────┬─────────────┘
              ▼
     TG Daemon (Unix socket)
       ├─ Single Telethon session
       ├─ AES-GCM + OS Keychain
       ├─ Sanitizer + audit log
       └─ Adapter: Telethon (default)
              ▼
        Telegram MTProto
```

## Install

```bash
git clone <this repo>
cd slim-tg-mcp
pip install -e .
```

## First-time setup

```bash
# 1. Get api_id / api_hash from https://my.telegram.org
export TG_API_ID=123456
export TG_API_HASH=abcdef...

# 2. Login (interactive — you will receive a code on Telegram)
tgmcp init --label main

# 3. Start the daemon
tgmcp daemon start --account main
tgmcp daemon status
```

## Wire up Claude Code

Add to `~/.claude.json` (or per-project `.mcp.json`):

```json
{
  "mcpServers": {
    "telegram": {
      "command": "tgmcp-mcp",
      "env": {}
    }
  }
}
```

The skills directory ships at `skills/` — point Claude Code at it via
`~/.claude/settings.json`:

```json
{
  "skillsDirs": [
    "/absolute/path/to/slim-tg-mcp/skills"
  ]
}
```

## What you get

### Always-loaded MCP tools (~1.2k tokens)

| Tool | Purpose |
|---|---|
| `tg_search_global` | Search across all chats |
| `tg_search_in_chat` | Search a specific chat (sender/date filters) |
| `tg_list_dialogs` | Discover chats |
| `tg_get_messages` | Read recent messages |
| `tg_get_message_context` | N before/after a hit |
| `tg_resolve_entity` | username/link → ID |
| `tg_chat_info` | Title, kind, member count |
| `tg_download_media` | Download files for analysis |

### Lazy-loaded Skills

- `tg-messaging` — send / reply / forward
- (more coming: group-admin, contacts, polls, scheduling, ...)

## Security

- **Session storage**: AES-GCM ciphertext on disk; data key in OS Keychain
  (macOS Keychain / libsecret / Windows DPAPI). File mode 0600.
- **Prompt-injection defense**: every message body is normalized
  (zero-width chars stripped), wrapped in `<tg_msg trust="...">`, and known
  injection markers like `[INST]`, `<|im_start|>`, "ignore previous
  instructions" are tagged `[[neutralized:...]]` so they can't be parsed as
  instructions by the model.
- **Trust levels**: `high` = you wrote it, `medium` = saved contact in DM,
  `low` = group / channel / stranger. The MCP tool descriptions instruct
  Claude to refuse to act on instructions from non-`high` content.
- **Audit log**: write operations append to `~/.config/tgmcp/audit.log`.
- **No external network**: MCP/skills talk to the daemon over a Unix domain
  socket; no TCP port is opened.

## Project layout

```
slim-tg-mcp/
├── src/tgmcp/
│   ├── client.py              # shared daemon client
│   ├── daemon/
│   │   ├── server.py          # FastAPI on Unix socket
│   │   ├── telegram.py        # Telethon wrapper
│   │   ├── auth.py            # encrypted session storage
│   │   ├── sanitizer.py       # prompt-injection defense
│   │   └── audit.py
│   ├── mcp_server/
│   │   └── server.py          # 8 MCP tools (stdio)
│   └── cli/
│       └── main.py            # tgmcp init/account/daemon
├── skills/
│   └── tg-messaging/          # first skill (write ops)
└── tests/
```

## Roadmap

- [x] Phase 1: daemon + 8 MCP tools + tg-messaging skill + sanitizer + tests
- [ ] Phase 2: group-admin / contacts / polls / scheduling / profile skills
- [ ] Phase 2: multi-account runtime switching
- [ ] Phase 2: Pyrogram backend adapter (Telethon fallback)
- [ ] Phase 3: web UI for browsing / managing
- [ ] Phase 3: bulk export skill

## License

Apache-2.0 (matches the upstream `chigwell/telegram-mcp` whose API surface
inspired this project).
