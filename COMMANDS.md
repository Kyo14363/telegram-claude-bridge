# Commands

## Telegram Commands

| Command | Purpose |
|---|---|
| `/start` | Start status and persistent keyboard |
| `/help` | Command help |
| `/status` | Runtime status |
| `/session` | Claude SDK session details |
| `/clear` | Reset Claude session (re-spawns SDK with a fresh `session_id`) |
| `/interrupt` | Interrupt active Claude task |
| `/browser-close` | Close Playwright browsers |
| `/keyboard` | Re-send keyboard |
| `/history` | Local reference history (not the Claude context — that's SDK-managed) |
| `/stats` | Usage metrics (messages, fetches, reply captures, recent errors) |
| `/exec <cmd>` | Run a PowerShell command directly (bypasses Claude) |
| `/ps` | Key local process status (python / claude / node) |
| `/cclog` | Recent Claude Code log |
| `/tasklog` | Recent Windows Task Scheduler events |
| `/bridge` | Recent bridge log |
| `/uptime` | Local uptime |

The public command set is intentionally limited to conversation, browser,
status, and diagnostics commands. Shortcut PowerShell commands stay local
(no Claude round-trip), so they're cheap to spam.

## Normal Use

Send links or text directly. The bridge automatically preprocesses supported
URLs, keeps the Claude SDK session alive across messages, and lets Claude use
configured MCP tools such as Playwright when needed.

## Reply Capture

Send an X/Twitter URL together with a phrase that contains a fetch verb
("收錄" / "抓取") and a reply noun ("留言" / "回覆"). With `TWIKIT_COOKIES`
configured the bridge pulls replies via twikit, runs an AI filter against
your free-form criteria, and returns the cleaned result. Without cookies it
generates a Playwright prompt so Claude can scrape the replies through the
browser instead.
