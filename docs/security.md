# Security Model & Threat Model

> Last updated for v0.4.0. If something here disagrees with the code,
> the code is right and this doc is stale — please open an issue.

This document describes what `slim-tg-mcp` is *designed to defend
against*, what it explicitly is *not*, and the mechanism behind each
defense so you can reason about residual risk for your own deployment.

---

## 1. Scope

`slim-tg-mcp` brokers a real Telegram user-account session to an LLM
agent (Claude Code or any MCP-compatible client). The LLM can read,
search, and — via lazy-loaded Skills — write, send media, change
profile, manage groups/folders, etc.

The agent is **untrusted in the threat model**: we assume it will
encounter prompt-injected content (Telegram messages from strangers,
forwarded posts, group chats) and may itself be persuaded to take
actions the user did not intend. Every defense in this project starts
from that assumption.

### 1.1 Trust boundaries

```
┌──────────────────────────────────────────────────────────────────┐
│ User's machine (single Unix user)                                │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌────────────────────┐ │
│  │   LLM agent  │───▶│ slim-tg-mcp  │───▶│ Telegram (MTProto) │ │
│  │  (untrusted) │    │   (TCB)      │    └────────────────────┘ │
│  └──────────────┘    └──────────────┘                            │
│       MCP stdio /         Unix socket                            │
│       Skill exec           + flock                               │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

- **Trusted Computing Base (TCB):** the daemon, the CLI, the MCP
  server, and the Skill scripts in this repository. We assume code in
  this repo is what you ran (verify via git tag + commit signature if
  you care).
- **Same Unix user:** TCB and agent run as the same UID. We do **not**
  defend against a co-resident process running as the same UID — that
  attacker can ptrace the daemon, read `/proc/<pid>/mem`, or just
  exfiltrate `~/.config/tgmcp/sessions/*`. Our defenses are against
  the *agent* through the *MCP/Skill protocol surface*, not against an
  arbitrary local process.
- **Network:** Telegram's MTProto handles transport security. We do
  not re-implement or audit it; we trust Telethon's binding.

### 1.2 What this project is *not* designed to defend against

- **Compromised `api_id` / `api_hash`** — these are your credentials;
  if they leak, the attacker can re-implement the client.
- **Compromised host (root or same-UID attacker)** — see above. If the
  attacker can read your home directory or attach a debugger, this
  project gives no extra protection.
- **Physical access** to the unlocked machine while the daemon is
  running — the running daemon holds an in-memory MTProto session.
- **Side-channel attacks** (timing, power analysis, cache).
- **Telegram-side vulnerabilities** (MTProto bugs, server-side data
  exposure, rogue Telegram operators).
- **Supply-chain compromise** of upstream Python packages (Telethon,
  cryptography, etc.). We pin major versions but cannot vet every
  transitive dep.
- **The user explicitly approving destructive actions.** Many Skills
  require `--yes` or `--confirm-X` flags; if the user — or the LLM
  acting on the user's behalf — passes those flags, the action goes
  through. The defense here is making destructive actions *visible*
  and *non-default*, not preventing them.

---

## 2. Threat actors

| # | Actor | Capabilities | Defenses below |
|---|---|---|---|
| T1 | **Prompt-injecting Telegram peer** — sends crafted text/forwards trying to make the LLM exfiltrate data, send DMs, change settings | Inject text via any chat the user can read; forward arbitrary content | §3.1, §3.7 |
| T2 | **Confused-deputy LLM** — the agent itself, persuaded by injected content to misuse high-privilege Skills | Call any MCP tool / dispatch any Skill subcommand | §3.2, §3.3, §3.4, §3.7 |
| T3 | **At-rest disk attacker** — laptop is stolen, drive is imaged | Read every byte under the user's home | §3.5 |
| T4 | **Co-located filesystem racer** — another process on the box (different UID) trying TOCTOU on shared dirs (`/tmp`, `$XDG_RUNTIME_DIR`) | Create/swap/symlink files in shared dirs between the daemon's check and use | §3.6 |
| T5 | **Misconfigured environment** — poisoned `$HOME`, `$TMPDIR`, `$XDG_RUNTIME_DIR` redirecting writes to attacker dirs | Set env vars before daemon launch | §3.6 |

---

## 3. Defenses

### 3.1 Prompt-injection wrapping (T1)

**Mechanism:** every Telegram message body returned to the LLM passes
through `src/tgmcp/daemon/sanitizer.py` and is wrapped:

```
<tg_msg trust="low" sender_id="123" chat_id="-100..." is_forward="1">
[[neutralized:'ignore previous instructions']] click here pls
</tg_msg>
```

Three things happen before the model sees the content:

1. **Unicode normalization (NFKC) + zero-width strip** — homoglyph and
   bidi-override tricks (`\u200e`, `\u202e`, …) are removed so the model
   doesn't see hidden characters that change apparent string identity.
2. **Injection-marker neutralization** — known patterns
   (`ignore previous instructions`, `you are now`, `[INST]`,
   `<|im_start|>`, `<system>` tags, etc.) are wrapped in
   `[[neutralized:...]]`. We deliberately do **not delete** the
   pattern: the model still needs to see what was attempted, but the
   surrounding `[[ ]]` makes it un-parseable as instructions.
3. **Provenance + trust label** — every `<tg_msg>` carries
   `sender_id`, `chat_id`, and a `trust` ∈ `{high, medium, low}`:
   - `high` = self (Saved Messages, your own outbound)
   - `medium` = a contact in a 1:1 chat
   - `low` = everything else (group chats, channels, non-contact DMs)

   `low` is the default. **Forwards are downgraded to `low`**
   regardless of who forwarded them — an attacker cannot reach `high`
   by getting the user to forward their message into Saved Messages.

**What it doesn't stop:** an LLM that ignores the trust label and
treats `low`-trust content as instructions. The wrapping is a hint to
the model, not a hard gate. The hard gate is §3.7 (schema validation
+ explicit confirmation flags on destructive actions).

### 3.2 Skill split (T2 — confused deputy)

**Mechanism:** the LLM's MCP surface contains only **8 read/search
tools** (~1.5k tokens always loaded). Every write, admin, or
destructive operation is a **lazy-load Skill** (`skills/tg-*/`) that
the model has to explicitly invoke as a subprocess.

This produces two anti-confused-deputy properties:

1. **The dangerous capabilities are not in the tool list** — a
   prompt-injected agent can't call them by accident, because they
   aren't there. The model has to read the Skill description and
   explicitly dispatch it.
2. **Each Skill is one Python script with a `--yes` requirement.**
   Destructive Skills (`tg-media-upload`, `tg-export`) require either
   `--yes` OR matching `--confirm-chat` AND `--confirm-file` /
   `--confirm-out-dir` (typo-resistant double keystroke). The model
   sending the wrong chat ID by accident is rejected at the
   dispatcher.

### 3.3 Path safety (T2, T5)

**Mechanism (`src/tgmcp/daemon/paths.py`):**

- **Home from `/etc/passwd`, never `$HOME`.** `pwd.getpwuid(getuid())`
  gives the authoritative home; `$HOME` is environment and can be
  poisoned by a sloppy `sudo` config or a malicious shell init. All
  persistent paths (`~/.config/tgmcp/...`) start from the passwd-DB
  home.
- **Hardcoded `/tmp` runtime fallback, not `tempfile.gettempdir()`.**
  `gettempdir()` honors `$TMPDIR`, which on shared boxes may point at
  an NFS mount — and Unix sockets / `flock` are unreliable on NFS
  (silent split-brain). We refuse the fallback and use a known-local
  path.
- **`$XDG_RUNTIME_DIR` validation.** When the env var is set, the
  daemon `lstat()`s it: must be a directory (not symlink), owned by
  the current UID, and `0700`. **Failing any of those checks falls
  back to `/tmp/tgmcp-<uid>`** rather than trusting an
  attacker-influenced path. (`UnsafeRuntimeDir` is raised only if
  neither the XDG path nor the `/tmp` fallback can be made safe — a
  pre-existing `tgmcp-<uid>` with wrong owner/perms, for instance.)
- **Caller-supplied paths** (`tg-media-upload`, `tg-export`,
  `tg-profile photo`) go through a layered validator:
  - Reject symlinks at the leaf **and at every parent component** (a
    parent symlink could re-target after we look at the leaf).
  - Realpath-based containment check rejects any path inside
    `RUNTIME_DIR` (uploads / profile photo / export) so the LLM
    cannot cause the daemon to overwrite its own socket / lock /
    downloads. **Export additionally rejects paths inside `CONFIG_DIR`**
    (where session files and the audit log live); uploads do not, so
    a user can legitimately upload, e.g., a screenshot already saved
    under `~/.config/`. The asymmetry is intentional — uploads only
    *read* the path, exports *write* into it.
  - Reject foreign-owned dirs (export only).

### 3.4 TOCTOU-resistant file ops (T2, T4)

**Mechanism (`_open_validated_upload`, `_open_validated_export_dir` in
`src/tgmcp/daemon/server.py`, plumbed through to
`TGSession.send_media` / `TGSession.export_chat`):**

The classic Telethon-style API would be `client.send_file(path)` —
which means Telethon reopens the path *by name* internally, after
we've validated. Between our check and Telethon's open, an attacker
in a shared dir can swap the path. We close that gap with
**fd-based, openat-style** I/O:

1. Caller passes a path → daemon validates (§3.3) and `lstat`s it,
   capturing `(st_dev, st_ino, st_mode)`.
2. Daemon opens with `O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`
   (`O_DIRECTORY|O_NOFOLLOW` for export dir).
   - `O_NOFOLLOW` defeats post-validate symlink swap.
   - `O_NONBLOCK` defeats FIFO/device swap that would otherwise hang
     the daemon on open.
   - `O_CLOEXEC` keeps the fd from leaking into any subprocess.
3. Daemon `fstat`s the *opened fd* and compares
   `(st_dev, st_ino)` against the pre-open `lstat`. Mismatch ⇒ refuse.
   This catches an attacker who replaced a regular file between
   `lstat` and `open`.
4. Daemon re-checks `S_ISREG` (or `S_ISDIR`) on the fstatted struct.
5. **Telethon receives the fd / file object — never the path.**
   No re-resolution by name happens after validation.
6. For the export dir, **every subsequent operation** is relative to
   the validated `dir_fd`: `os.mkdir(name, dir_fd=fd)`,
   `os.open(name, O_CREAT|O_EXCL|O_NOFOLLOW, dir_fd=fd)`. The
   filesystem never re-resolves the export root.
7. Per-message JSON / media files open `O_CREAT|O_EXCL|O_NOFOLLOW` so
   a pre-existing entry **fails** rather than silently clobbers.

Errno divergence note: `O_NOFOLLOW` on a symlink-to-dir gives
`ENOTDIR` on macOS but `ELOOP` on Linux. We pre-check with
`os.stat(name, dir_fd=, follow_symlinks=False)` so the caller-visible
error is consistent.

### 3.5 Encryption at rest (T3)

**Mechanism (`src/tgmcp/daemon/auth.py`):**

- **AES-GCM** for session bytes (Telethon's `StringSession`).
- **Data key** stored in the OS keychain (macOS Keychain / libsecret /
  Windows Credential Manager) via `keyring`. The data key never
  touches disk in plaintext.
- **Passphrase fallback** when no keychain is available: the data key
  is wrapped with a scrypt-derived KEK from a user passphrase. The
  passphrase is read via hidden prompt or `--passphrase-stdin`,
  **never on argv**.
- **Per-account label** so multi-account stores are namespaced.

**What it stops:** the laptop-thief / disk-imaging adversary cannot
read the session by examining `~/.config/tgmcp/sessions/*` because the
data key lives outside the encrypted blob.

**What it doesn't stop:** a same-UID running attacker. Once the
daemon is up, the session is in process memory.

### 3.6 Daemon singleton & lifecycle

**Mechanism (`src/tgmcp/daemon/server.py` + `paths.py`):**

- **`fcntl.flock` is the authoritative liveness signal.** Lockfile
  presence and pid file content are *advisory*; the kernel
  auto-releases `flock` on process exit, so a stale pidfile after a
  hard kill cannot block restart.
- **Singleton enforcement** at startup: the daemon `flock(LOCK_EX |
  LOCK_NB)`s the lockfile before binding the socket; second instance
  exits with a clear error.
- **Shutdown via daemon-side `/shutdown` RPC** bound to a per-process
  random `instance_id`. The CLI sends `POST /shutdown` with the id it
  read from the daemon's pid file at start; the daemon refuses if the
  id doesn't match. This eliminates the SIGTERM-races-against-startup
  class. SIGTERM is kept only as a fallback for transport failure.
- **Per-account `asyncio.Lock`** on `accounts/switch` so two parallel
  switches to the same cold label can't both call `_open_session`
  (the second assignment would orphan the first live `TGSession`).

### 3.7 Schema validation surface

**Mechanism:**

- **Pydantic v2 models** on every daemon endpoint. Failures map to
  HTTP **400** with `{"error": "ValidationError", "detail": ...}` via
  a `RequestValidationError` exception handler — uniform across the
  whole API instead of FastAPI's default 400/422 split, so CLI / Skill
  dispatchers / `DaemonClient` consumers can branch on a single
  status code.
- **Bounded inputs** at the schema layer:
  - Folder `folder_id` ∈ `[2, 255]` (Telegram reserves 0/1).
  - Folder title ≤ 12 UTF-8 chars (real Telegram limit, not 64).
  - Slow-mode seconds ∈ the exact Telegram-allowed slot set.
  - Username regex `^[a-zA-Z][a-zA-Z0-9_]{3,30}[a-zA-Z0-9]$`
    (rejects trailing underscore — matches Telegram's actual rule).
  - Schedule timestamps tz-aware, 10 s ≤ Δ ≤ 365 days, plus a
    just-before-dispatch recheck.
  - Phone numbers: E.164 enforced for `tg-contacts add`.
- **Model validators** for cross-field invariants:
  - `PrivacyRule`: `*_users` rules require non-empty `user_ids`; non-`_users` rules forbid it (silent drop would surprise callers).
  - `FolderPeerSpec`: at least one inclusion source (else Telegram
    bounces with `FILTER_INCLUDE_EMPTY` upstream — we 400 here).
  - `Change2faReq`: at least one of `current_password` / `new_password` (anything else is a no-op).

### 3.8 Audit logging

**Mechanism (`src/tgmcp/daemon/audit.py`):**

- Append-only log at `~/.config/tgmcp/audit.log`. The daemon does not
  explicitly `chmod` the log file — it inherits the user's umask
  (typically 0644). Confidentiality of the audit log relies on the
  parent dir `~/.config/tgmcp/` being mode 0700 (created by
  `auth.py` / `paths.ensure_safe_subdir`); since the audit log
  records only non-sensitive metadata, file-mode hardening is
  defense-in-depth, not the primary control.
- Every write/admin operation logs: timestamp, account label, action,
  *non-sensitive* metadata only.
- Never logged: passphrases, full file paths, message bodies, full
  phone numbers (last 4 digits only), 2FA password material (only the
  transition kind: `set` / `change` / `remove`), privacy
  user-id allowlists (only the rule count).
- Audit logging is best-effort and non-blocking — a logging failure
  must not block the user-visible operation, but it's surfaced to
  daemon stderr.

---

## 4. Residual risks & known limitations

These are limitations we *know about* and have decided to live with —
either the fix isn't possible without unacceptable trade-offs, or the
risk is below the project's defended threshold.

### 4.1 Unsanitized media metadata
We sanitize **text** content before showing it to the model, but we
do **not** OCR images or transcribe audio. A prompt injection
embedded in an image's pixels or in a voice note will reach the model
unchanged if the model itself OCRs or transcribes it. Treat
multimodal model attachments with at least the same trust level as
the surrounding `<tg_msg>`.

### 4.2 `tg-export` writes attacker-controlled bytes
The export Skill writes the *Telegram peer's* messages (which can
contain attacker-controlled content) to a directory the user picked.
We defend the **directory** (no symlinks, no clobber, no escape) but
the file *contents* are whatever the peer sent. If you then feed
the export back to an LLM without re-applying §3.1 wrapping, you've
re-exposed the prompt-injection surface.

### 4.3 Telethon reconnect/resume races
Telethon manages the MTProto connection and may transparently reopen
sockets. We trust its session string handling; if Telethon mishandles
a malformed update we get whatever Telethon does (typically a raised
exception we propagate as a 5xx).

### 4.4 LLM ignoring trust labels
The `<tg_msg trust="low">` wrapping is a *signal*. If the model
treats `low`-trust content as instructions anyway, the only remaining
defenses are the Skill split (§3.2), confirmation flags (§3.7), and
the user's eyes on the action. We test this by structuring
destructive operations so a single mis-step doesn't fire a destructive
RPC.

### 4.5 No mutual-authentication on the Unix socket
The daemon trusts any same-UID caller on its socket. Filesystem
permissions on the socket parent (`0700`) keep other UIDs out, but a
same-UID malicious binary placed in `$PATH` ahead of `tgmcp` would
talk to the daemon. The mitigation is to install `tgmcp` from a
trusted source and not to `pip install`-from-untrusted.

### 4.6 Skill-side argument parsing is the agent's responsibility
Skills validate their own inputs (e.g., `--chat` resolution, file
existence) but the *argparse layer itself* is standard Python
argparse. We don't defend against argument-injection-via-shell —
Skills do not call shells; they hit the daemon over HTTP-on-socket
with structured JSON.

---

## 5. Reporting vulnerabilities

If you find a bug that meets any of these criteria:

- An agent can cause an action *with no `--yes`/`--confirm-X` flag* that
  shouldn't be possible without one.
- Session bytes reachable on disk in plaintext.
- TOCTOU window in any path-validating endpoint.
- Schema bypass causing a request to reach Telethon with values the
  schema claims it rejects.
- Audit log captures sensitive content (passphrase, full path,
  message body, full phone, 2FA password material).

…please **do not open a public GitHub issue**. Email
`haoyuwang88888@gmail.com` with:
- The minimal reproducer (commit hash + steps).
- What you expected vs. what happened.
- Any patch you have in mind.

For non-security bugs, regular GitHub issues are fine.

---

## 6. Verification: did I get the version with all of the above?

```bash
tgmcp --version          # must report 0.4.0 or later
git -C "$(pip show slim-tg-mcp | awk '/Location:/{print $2}')/.." log -1 --format=%H
# compare against the tag on the repo
```

The full test suite — including regression tests pinning every
defense above — runs on every push / PR via GitHub Actions.

```bash
pytest tests/ -q          # 365 tests at v0.4.0
```
