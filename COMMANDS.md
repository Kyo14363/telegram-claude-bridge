# Commands

## Telegram Commands

| Command | Purpose |
|---|---|
| `/start` | Start status and persistent keyboard |
| `/help` | Command help |
| `/status` | Runtime status |
| `/session` | Claude SDK session details |
| `/clear` | Reset Claude session |
| `/interrupt` | Interrupt active Claude task |
| `/browser-close` | Close Playwright browsers |
| `/keyboard` | Re-send keyboard |
| `/history` | Local reference history |
| `/stats` | Usage metrics |
| `/ps` | Key local process status |
| `/cclog` | Recent Claude Code log |
| `/tasklog` | Recent Windows Task Scheduler events |
| `/bridge` | Recent bridge log |
| `/uptime` | Local uptime |

The public v3.1 command set is intentionally limited to conversation, browser, status, and diagnostics commands.

## Normal Use

Send links or text directly. The bridge automatically preprocesses supported URLs, keeps the Claude SDK session alive across messages, and lets Claude use configured MCP tools such as Playwright when needed.
