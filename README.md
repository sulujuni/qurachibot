# 🎉 QurachiBot — Telegram Giveaway & Contest Bot

A multilingual Telegram bot for organizing **giveaways** (random draws) and **contests** (submission-based competitions with voting).

**Supported Languages:** 🇬🇧 English | 🇷🇺 Русский | 🇺🇿 O'zbekcha

## Features

### 🎁 Giveaways
- Create giveaways with title, description, prize, and winner count
- Configurable duration (1h to 7 days, or no limit)
- One-click join via inline button
- Random winner drawing
- Live participant count updates

### 🏅 Contests
- Create contests accepting text, photo, or any submissions
- Configurable max submissions per user
- Community voting system
- Submission/voting phase management
- Winners announced by vote count

### 🌐 Multilingual
- Per-user language preference stored in database
- Easy `/lang` command to switch between languages
- All messages fully translated (EN, RU, UZ)
- Easy to add new languages (just add a JSON file)

---

## Quick Start

### Prerequisites
- Python 3.10+
- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### 1. Install

```bash
git clone https://github.com/sulujuni/qurachibot.git
cd qurachibot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env and set your BOT_TOKEN
```

### 3. Run

```bash
python3 main.py
```

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Full command list |
| `/lang` | Change language |
| `/newgiveaway` | Create a giveaway |
| `/draw [id]` | Draw winners |
| `/mygiveaways` | List your giveaways |
| `/cancelgiveaway [id]` | Cancel a giveaway |
| `/newcontest` | Create a contest |
| `/submit [id] [text]` | Submit entry |
| `/vote [id]` | Vote for submission |
| `/submissions [id]` | View submissions |
| `/startvoting [id]` | Start voting phase |
| `/endcontest [id]` | End & announce winners |
| `/mycontests` | List your contests |
| `/cancelcontest [id]` | Cancel a contest |

---

## Adding a New Language

1. Create folder: `bot/i18n/locales/<code>/`
2. Copy `bot/i18n/locales/en/messages.json` into it
3. Translate all values
4. Add the language code to `SUPPORTED_LANGUAGES` in `bot/i18n/core.py`
5. Add the display name to `LANG_NAMES` in `bot/handlers/common.py`

---

## Project Structure

```
qurachibot/
├── main.py
├── requirements.txt
├── .env.example
├── .gitignore
└── bot/
    ├── config.py
    ├── handlers/
    │   ├── common.py      # /start, /help, /lang
    │   ├── giveaway.py    # Giveaway commands
    │   └── contest.py     # Contest commands
    ├── i18n/
    │   ├── core.py        # Translation engine
    │   └── locales/
    │       ├── en/messages.json
    │       ├── ru/messages.json
    │       └── uz/messages.json
    ├── models/
    │   ├── base.py
    │   ├── database.py
    │   ├── giveaway.py
    │   ├── contest.py
    │   └── user_settings.py
    └── utils/
        └── lang.py        # User language helpers
```

---

## License

MIT
