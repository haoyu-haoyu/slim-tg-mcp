# Real-account E2E validation

> **Why this exists.** The 501 unit tests pin code paths; they do
> *not* prove that Telethon's behavior on the live MTProto API
> matches what those tests mock. v0.3+ added ~30 new daemon endpoints
> (topics, stories, location, bot, etc.) — none have ever been hit
> against real Telegram. Run this before depending on any of those
> features in production.

The script lives at [`scripts/e2e_smoke.py`](../scripts/e2e_smoke.py).
It refuses to run without `TGMCP_E2E_CONFIRM=yes`.

---

## 1. Prepare a burner account

**Do not use your main Telegram account.** Some test groups
create-then-delete real artifacts (topics, location pins). Even the
read-only paths read your actual messages — you don't want the agent
ever touching that surface.

1. Get a SIM you don't mind associating with a test account, OR use
   a service like Google Voice / Twilio for a separate number.
2. Install the official Telegram app, register the new number.
3. Apply for `api_id` / `api_hash` at https://my.telegram.org/apps
   (you only need to do this once per account).

If the burner is ever compromised: log out from all devices in
Telegram → Settings → Devices → Terminate all other sessions.

---

## 2. Initialize the e2e session

```bash
export TG_API_ID=...
export TG_API_HASH=...

# Logs in interactively (Telegram SMSes a code).
tgmcp init --label e2e
```

The script expects label `e2e` exactly (the constant `LABEL` at the
top of `scripts/e2e_smoke.py`).

If you also want the bot-mode tests, initialize a bot label:

```bash
# Get a bot token from @BotFather first.
printf %s 'your-bot-token' | tgmcp init --label e2e-bot --bot-token-stdin
```

---

## 3. Run the script

### 3.1 Default — read-only + self-only safe set

```bash
export TGMCP_E2E_CONFIRM=yes
python scripts/e2e_smoke.py
```

Runs: core / metrics / privacy / folders / stories. None touch
external peers; the only writes are to your own Saved Messages and
get cleaned up before exit.

### 3.2 Add destructive groups

```bash
# Send a static location pin to Saved Messages, then delete it.
python scripts/e2e_smoke.py --location

# Validate bot-mode RPCs against the e2e-bot label.
python scripts/e2e_smoke.py --bot

# Forum topic CRUD against a forum supergroup you own. Creates and
# (best-effort) deletes a test topic. NEVER point at a shared group.
python scripts/e2e_smoke.py --topics-chat "@yourforumgroup"

# Run everything except --topics-chat (which needs a chat).
python scripts/e2e_smoke.py --all
```

### 3.3 Pick specific groups

Each flag enables its group; passing any flag disables the default
set. So `--core --bot` runs ONLY core and bot, not the read-only
groups.

---

## 4. Reading the output

Each step prints either:
- `✓ <message>` — the step succeeded.
- `WARN: <message>` — non-fatal; the script continues. Common for
  things like "topic edit failed (may need admin perms)".
- `FAIL: <message>` — fatal; the script exits non-zero.

Group-level failures (an exception inside one group's code) are
collected and reported at the bottom rather than aborting the run,
so a broken `--bot` group doesn't mask a working `--core` group.

---

## 5. Reporting issues

When you find a failure that **isn't** documented as a known
limitation:

1. Re-run with `TG_API_ID`/`TG_API_HASH` masked from the report
   (they shouldn't appear in script output anyway, but double-check).
2. Open a GitHub issue with:
   - The exact failing group (e.g., `topics`, `stories pinned`).
   - The full traceback or `WARN`/`FAIL` line.
   - Your Telethon version: `pip show telethon | grep Version`.
   - Whether the burner has Telegram Premium (some endpoints need it).

We take real-account regressions seriously — every confirmed e2e
bug becomes a regression test that pins the fix, even if we can't
re-run the e2e in CI.

---

## 6. Limitations

- **Stories `--all`/`--stories` only reads.** Story creation needs
  privacy rules + media + period and is intentionally not in the
  Skill set yet.
- **Topics need a forum supergroup you own.** A regular supergroup
  won't have topics enabled. Toggle "Topics" on in the official
  Telegram client first.
- **Bot mode `--bot` only validates state RPCs** (poll, switch).
  Sending a keyboard requires a chat that the bot is a member of —
  not exercised here to keep the script self-contained. Test that
  manually if you change `bot_send_keyboard`.
- **Premium-only endpoints (`emoji-status`, custom-emoji reactions)
  are NOT in this script.** They need a Premium account and would
  fail loudly on a free burner. If you're testing Premium changes,
  add them to your local copy and run them by hand.
- **The script does NOT exit cleanly on Ctrl-C** mid-step — the
  daemon stays up. Run `tgmcp daemon stop` if that happens.

---

## 7. What success looks like

A clean default run on a fresh burner:

```
==> 0. Check env
    ✓ TG_API_ID / TG_API_HASH present
==> 1. Check session for label=e2e
    ✓ found encrypted session for 'e2e'
==> 2. Start daemon
    ✓ daemon up
==> 3. /health
    ✓ pid=12345 account=e2e me_id=1234567890
==> core: list_dialogs (read-side)
    ✓ got 5 dialogs
==> core: send to Saved Messages
    ✓ sent msg_id=42
==> core: search to confirm
    ✓ search found it (ids=[42])
==> core: edit
    ✓ edited
==> core: delete and verify via readback
    ✓ deletion confirmed (msg 42 no longer searchable)
==> privacy: read all 10 privacy keys
    ✓ status: 1 rule(s)
    ...
==> folders: list
    ✓ (no chat folders configured — that's fine)
==> stories: list your own active stories
    ✓ found 0 active stories on your own account
==> stories: list your pinned stories
    ✓ found 0 pinned stories
==> metrics: GET /metrics
    ✓ metrics endpoint serving prometheus-shaped output
==> Stop daemon

╔══ ALL E2E STEPS PASSED ══╗
```

A burner with no stories, no folders, and no privacy customizations
will hit zeros in those reads — that's expected and counts as
success; the test is "the call succeeded", not "data was returned".
