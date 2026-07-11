# Commands

## Telegram Commands

| Command | Purpose |
|---|---|
| `/start` | Start status and persistent keyboard |
| `/help` | Command help |
| `/status` | Runtime status (incl. pinned model and full session id) |
| `/clear` | Reset Claude session (full teardown, re-spawns SDK with a fresh `session_id`) |
| `/interrupt` | Interrupt active Claude task |
| `/browser-close` | Close Playwright browsers (direct psutil kill inside the SDK tree) |
| `/keyboard` | Re-send keyboard |
| `/stats` | Usage metrics (messages, fetches, reply captures, recent errors) |
| `/resume [--force]` | Continue the most recent *desktop* Claude Code session headlessly (`claude -p --resume`), in its original project folder |
| `/handoff <instruction>` | Brief the resident SDK session on the desktop session's tail and hand it the instruction (streaming, read-only, safe while desktop is open) |
| `/exec <cmd>` | Run a PowerShell command directly (bypasses Claude) |
| `/ps` | Key local process status (python / claude / node) |
| `/cclog [keyword] [n]` | Last message(s) of the most recent Claude Code session; keyword targets a project, n = message count (max 3) |
| `/tasklog` | Recent Windows Task Scheduler events |
| `/bridge` | Recent bridge log |
| `/uptime` | Bridge process uptime |

`/resume` and `/handoff` are deliberately not on the shortcut keyboard — both
spend real model calls, so they require a typed command. Shortcut PowerShell
commands stay local (no Claude round-trip), so they're cheap to spam.

## Normal Use

Send links or text directly. The bridge automatically preprocesses supported
URLs, keeps the Claude SDK session alive across messages, and lets Claude use
configured MCP tools such as Playwright when needed. While a task is running,
new messages queue up (FIFO) with a "queued, N ahead" notice; the queue
bounces only past `QUEUE_MAX_WAITING`.

## Two Claudes, three ways in

The bridge distinguishes between the **resident SDK session** (always
connected, runs in `WORKING_DIR`, answers normal messages) and your
**desktop Claude Code session** (its own transcript, its own project folder):

- Plain message → resident SDK session (fast, streaming).
- `/handoff <instruction>` → resident session, pre-briefed with the desktop
  session's recent context (read-only over the desktop transcript).
- `/resume` → wake the desktop session itself, headless, same transcript,
  original working directory (slow but full context).

## Reply Capture

Send an X/Twitter URL together with a phrase that contains a fetch verb
("收錄" / "抓取") and a reply noun ("留言" / "回覆"). With `TWIKIT_COOKIES`
configured the bridge pulls replies via twikit, runs an AI filter against
your free-form criteria, and returns the cleaned result. Without cookies it
generates a Playwright prompt so Claude can scrape the replies through the
browser instead.
