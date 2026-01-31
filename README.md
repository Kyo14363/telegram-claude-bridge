# Telegram Claude Code Bridge

A lightweight bridge that allows you to control [Claude Code CLI](https://claude.ai/code) from your phone via Telegram, with conversation history support.

**[繁體中文說明](#繁體中文)**

## Features

- **Conversation Context Memory** - Maintains conversation history across messages, so Claude understands references like "this", "that one", "the three items mentioned above"
- **Daily Log Rotation** - Automatic log file rotation with configurable retention period
- **Simple Deployment** - Single Python file, no Docker required
- **Windows Native** - Built and tested on Windows, works out of the box

## ⚠️ Security Notice

**Please read before using:**

1. **This bot can execute commands on your computer.** Only allow your own Telegram User ID.

2. **The bot uses `--dangerously-skip-permissions` flag** which bypasses Claude Code's safety confirmations. This is necessary for remote operation but means Claude can perform actions without asking.

3. **Keep your `.env` file private.** Never commit it to Git or share your bot token.

4. **Trust the dependencies:** This project only uses well-known, trusted packages:
   - `python-telegram-bot` - Official Telegram Bot API wrapper (13M+ downloads/month)
   - `python-dotenv` - Environment variable loader (30M+ downloads/month)

5. **Review the code yourself** if you have concerns. It's a single file, easy to audit.

## Prerequisites

- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- A Telegram account

> **Note:** This works with both free and paid Claude accounts. Free users will use Claude Sonnet, Pro users can access Opus.

## Quick Start

### 1. Install Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude  # Follow prompts to authenticate
```

### 2. Get Your Telegram Bot Token

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` to create a new bot
3. Follow the prompts to name your bot
4. Copy the **API token** (looks like `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`)

### 3. Get Your Telegram User ID

1. Open Telegram and search for **@userinfobot**
2. Send any message to the bot
3. Copy your **User ID** (a number like `123456789`)

### 4. Clone and Configure

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/telegram-claude-bridge.git
cd telegram-claude-bridge

# Install dependencies
pip install -r requirements.txt

# Copy environment template
copy .env.example .env    # Windows
# cp .env.example .env    # macOS/Linux

# Edit .env with your credentials
notepad .env              # Windows
# nano .env               # macOS/Linux
```

Edit `.env` file:
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
ALLOWED_USER_ID=123456789
```

### 5. Run the Bridge

```bash
python telegram_claude_bridge.py
```

Or use the batch file (Windows):
```bash
start_bridge.bat
```

### 6. Start Chatting!

Open Telegram, find your bot, and send a message!

## Commands

| Command | Description |
|---------|-------------|
| `/start` | Show welcome message |
| `/help` | Show help information |
| `/status` | Show system status |
| `/history` | Show conversation history |
| `/clear` | Clear conversation history |
| `/exec <cmd>` | Execute PowerShell command directly |

## Configuration

All configuration is done via environment variables in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | (required) | Your Telegram bot token from @BotFather |
| `ALLOWED_USER_ID` | (required) | Your Telegram user ID from @userinfobot |
| `LOG_RETENTION_DAYS` | 14 | Days to keep log files |
| `MAX_HISTORY_ROUNDS` | 10 | Conversation rounds to maintain as context |
| `TIMEOUT` | 300 | Claude execution timeout in seconds |

## Project Structure

```
telegram-claude-bridge/
├── telegram_claude_bridge.py   # Main script
├── .env.example                # Environment template
├── .env                        # Your configuration (not in git)
├── requirements.txt            # Python dependencies
├── LICENSE                     # MIT License
├── logs/                       # Log files (auto-created)
│   └── bridge.log              # Current day log
│   └── bridge.log.YYYY-MM-DD   # Historical logs
└── conversation_history.json   # Conversation history (auto-created)
```

## Why This Project?

There are many Telegram-Claude bridges on GitHub, but most of them:
- Require Docker
- Are overly complex
- Don't maintain conversation context properly
- Don't support Windows well

This project aims to be **the simplest possible solution** that actually works.

## Troubleshooting

### "Claude CLI not found"
Make sure Claude Code is installed:
```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### "Unauthorized user"
- Make sure your User ID in `.env` is correct
- Get your ID from @userinfobot on Telegram

### Bot doesn't respond
- Check if the bridge is running
- Check `logs/bridge.log` for errors

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Issues and pull requests are welcome!

---

# 繁體中文

## Telegram Claude Code 橋接器

一個輕量級的橋接工具，讓你可以透過手機 Telegram 遠端操控電腦上的 Claude Code CLI，並支援對話歷史記憶功能。

## 功能特色

- **對話脈絡記憶** - 自動維護對話歷史，Claude 能理解「這個」、「那三項」等指代詞
- **每日 Log 輪換** - 自動產生每日 log 檔案，並定期清理舊檔
- **簡單部署** - 單一 Python 檔案，不需要 Docker
- **Windows 原生支援** - 專為 Windows 環境設計

## ⚠️ 安全性注意事項

**使用前請詳閱：**

1. **此 Bot 可以在你的電腦上執行指令。** 請只允許你自己的 Telegram User ID。

2. **Bot 使用 `--dangerously-skip-permissions` 參數**，這會跳過 Claude Code 的安全確認。這對遠端操作是必要的，但表示 Claude 可以不經詢問就執行動作。

3. **妥善保管你的 `.env` 檔案。** 絕對不要上傳到 Git 或分享你的 bot token。

4. **關於依賴套件的信任：** 本專案只使用知名且受信任的套件：
   - `python-telegram-bot` - 官方 Telegram Bot API 套件（每月 1300 萬+ 下載）
   - `python-dotenv` - 環境變數載入器（每月 3000 萬+ 下載）

5. **如有疑慮，請自行審閱程式碼。** 只有一個檔案，很容易檢查。

## 系統需求

- Python 3.10+
- 已安裝並認證的 [Claude Code CLI](https://claude.ai/code)
- Telegram 帳號

> **注意：** 免費和付費 Claude 帳號都可以使用。免費用戶會使用 Sonnet 模型，Pro 用戶可使用 Opus。

## 快速開始

### 1. 安裝 Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
claude  # 依照提示完成認證
```

### 2. 取得 Telegram Bot Token

1. 開啟 Telegram 搜尋 **@BotFather**
2. 發送 `/newbot` 建立新 bot
3. 依照提示為 bot 命名
4. 複製 **API token**（格式像 `1234567890:ABCdefGHIjklMNOpqrsTUVwxyz`）

### 3. 取得你的 Telegram User ID

1. 開啟 Telegram 搜尋 **@userinfobot**
2. 發送任意訊息給它
3. 複製你的 **User ID**（一串數字如 `123456789`）

### 4. 下載並設定

```bash
# Clone 專案
git clone https://github.com/YOUR_USERNAME/telegram-claude-bridge.git
cd telegram-claude-bridge

# 安裝依賴
pip install -r requirements.txt

# 複製環境變數範本
copy .env.example .env

# 編輯 .env 填入你的資訊
notepad .env
```

編輯 `.env` 檔案：
```env
TELEGRAM_BOT_TOKEN=你的_bot_token
ALLOWED_USER_ID=你的_user_id
```

### 5. 啟動橋接器

```bash
python telegram_claude_bridge.py
```

或使用批次檔（Windows）：
```bash
start_bridge.bat
```

### 6. 開始使用！

開啟 Telegram，找到你的 bot，發送訊息即可！

## 指令列表

| 指令 | 說明 |
|------|------|
| `/start` | 顯示歡迎訊息 |
| `/help` | 顯示幫助資訊 |
| `/status` | 顯示系統狀態 |
| `/history` | 顯示對話歷史 |
| `/clear` | 清空對話歷史 |
| `/exec <指令>` | 直接執行 PowerShell 指令 |

## 設定選項

所有設定都在 `.env` 檔案中：

| 變數 | 預設值 | 說明 |
|------|--------|------|
| `TELEGRAM_BOT_TOKEN` | (必填) | 從 @BotFather 取得的 bot token |
| `ALLOWED_USER_ID` | (必填) | 從 @userinfobot 取得的 user ID |
| `LOG_RETENTION_DAYS` | 14 | Log 檔案保留天數 |
| `MAX_HISTORY_ROUNDS` | 10 | 對話歷史保留輪數 |
| `TIMEOUT` | 300 | Claude 執行超時秒數 |

## 疑難排解

### 「Claude CLI not found」
確保已安裝 Claude Code：
```bash
npm install -g @anthropic-ai/claude-code
claude --version
```

### 「Unauthorized user」
- 確認 `.env` 中的 User ID 正確
- 從 @userinfobot 取得你的 ID

### Bot 沒有回應
- 確認橋接器正在執行
- 檢查 `logs/bridge.log` 的錯誤訊息
