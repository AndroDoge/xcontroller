# XController 🛡️🤖

**Powerful Telegram admin bot for always-on group management.**  
Automates moderation, forwarding, bans, and more — fast, secure, and easy to set up!

---

## ✨ Features

- 🔐 **Username Check**: Configurable auto-kick for new members without @username
- 🚫 **Content Moderation**: Deletes messages with banned words, bans repeat offenders
- 🌐 **Global Ban**: Bans propagate to all managed groups
- 📡 **Message Forwarding**: Forwards plain text to up to 20 groups, with per-user 24h cooldown
- 🧹 **Automatic Cleanups**: Periodically removes deleted accounts from groups
- ⚡ **Performance & Security**: Rate limiting, secure user tracking, persistent SQLite storage

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Telegram API credentials: `API_ID`, `API_HASH`
- Bot token from [@BotFather](https://t.me/BotFather)
- Secure random `SALT` string (for user tracking)

### Setup

1. **Clone repo**
   ```bash
   git clone https://github.com/AndroDoge/xcontroller.git
   cd xcontroller
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   ```
   Fill in `.env` with your credentials:
   ```env
   API_ID=your_api_id
   API_HASH=your_api_hash
   BOT_TOKEN=your_bot_token
   SALT=your_secure_random_salt
   BANNED_WORDS=spam,scam,virus
   ```

3. **Install & run**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   python bot.py
   ```

---

## ⚙️ Configuration

| Variable               | Required | Description                                      |
|------------------------|----------|--------------------------------------------------|
| API_ID                 | ✅       | Telegram API ID                                  |
| API_HASH               | ✅       | Telegram API Hash                                |
| BOT_TOKEN              | ✅       | Bot token from @BotFather                        |
| SALT                   | ✅       | Secure random string for user tracking          |
| BANNED_WORDS           | ❌       | Comma-separated banned words                     |
| ENFORCE_USERNAME       | ❌       | Kick users without @username (1=yes, 0=no)      |
| USERNAME_KICK_NOTICE   | ❌       | Message sent before kicking (if enforcement on) |

---
## 🔐 About `SALT` Security**
>
**SALT** is used to hash Telegram user IDs in the database (HMAC-SHA256).
- This means user IDs are never stored in plain text, so even if someone gets access to your database, they can't easily see, enumerate, or link users or admins to Telegram accounts.
 - Choose a long, random string (16+ characters, ideally 32+) for `SALT`. Example:
   ```
   SALT=V4t9$2Lrx!pQ7wX8t#bG3zF6eH1jK0uM
   ```
 - Never share your SALT publicly, and do not use simple or guessable values.
 - If SALT is kept secret, user data and admin actions remain private, even if database files leak.
 - If you ever need to rotate/revoke the SALT, create a new one and re-hash the database as needed.

**Bottom line:** SALT ensures user privacy and prevents anyone (including admins) from trivially linking IDs to real Telegram accounts.  
 Always keep your SALT safe and secret!

## 📝 Usage

1. **Add bot to your groups**
2. **Make bot admin** (delete messages, ban users, view members, etc)
3. **Bot works automatically!**
   - Checks new members for usernames
   - Moderates messages
   - Forwards plain text
   - Cleans up deleted accounts

---

## 🔐 Username Enforcement

Control whether the bot automatically kicks new members who don't have a public @username.

### Configuration

- **`ENFORCE_USERNAME`** (default: `1`)
  - `1` or `true` → Kick users without @username
  - `0` or `false` → Allow all users regardless of username
  
- **`USERNAME_KICK_NOTICE`** (default: empty)
  - If set, bot sends this message before kicking
  - Best-effort delivery (failures are ignored)
  - Example: `"Please set a public @username to participate."`

### Behavior

- **When enabled** (`ENFORCE_USERNAME=1`):
  - New users without @username are immediately kicked
  - Optional notice message sent before kick
  - Structured logging: `[USERNAME ENFORCE] kicked user_id=<id> group=<group_id>`
  
- **When disabled** (`ENFORCE_USERNAME=0`):
  - All users allowed to join regardless of username
  - No enforcement actions taken

### Examples

```env
# Strict enforcement with notice
ENFORCE_USERNAME=1
USERNAME_KICK_NOTICE=Please set a public @username to participate in this group.

# Enforcement without notice
ENFORCE_USERNAME=1

# No enforcement
ENFORCE_USERNAME=0
```

---

## 📁 Data & Logging

- Data stored in `/data` (container) or `./data` (local)
- Files: `bot_session*`, `bot.db`, `bot.log`
- Log output: file + console

---

## 💡 Tips

- Banned words are set via `.env`
- Bot manages up to 20 groups for forwarding
- Forwarding respects per-user cooldowns

---

## 📜 License

MIT — see [LICENSE](LICENSE)

---

**Made with ❤️, curiosity and Linux by AndroDoge**
