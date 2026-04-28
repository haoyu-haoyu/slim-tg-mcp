---
name: tg-folders
description: |
  List, create/update, and delete chat folders (a.k.a. dialog filters)
  on the user's Telegram account — the side rails in the official
  client like "Personal", "Work", "Unread". Triggers (EN): "list
  folders", "create folder", "rename folder", "delete folder",
  "organize chats". Triggers (中文): "列文件夹", "建文件夹", "改文件夹
  名", "删文件夹", "整理聊天".
---

# Telegram: Chat folders

## Safety checklist

1. **`update` overwrites the folder definition entirely.** It's not a
   merge. Show the user the new include/exclude lists explicitly.
2. **`delete` cannot be undone via this API.** The official client
   has a Recently Deleted UX path; we do not.

## Subcommands

```bash
# List
python ${CLAUDE_SKILL_DIR}/folders.py list

# Create or update (same RPC; new id = create, existing id = replace)
python ${CLAUDE_SKILL_DIR}/folders.py update \
  --id 2 --title "Work" \
  --include "@team_chat,@boss" \
  --exclude "@spam_group" \
  --groups       # also include all groups
  --broadcasts   # also include all channels

# Delete
python ${CLAUDE_SKILL_DIR}/folders.py delete --id 2
```

Folder IDs 0 and 1 are reserved by Telegram; the dispatcher refuses
them.

## Bool flags for "kind-of-chats" inclusion

`--contacts`, `--non-contacts`, `--groups`, `--broadcasts`, `--bots` —
each ON adds **all chats of that category** to the folder, regardless
of `--include`. They overlap, not replace.

## On failure

- `folder_id must be 2..255` — Telegram reserves 0/1.
- `chat not found` for an `--include` peer — run
  `tg_resolve_entity` to confirm the username/id first.
