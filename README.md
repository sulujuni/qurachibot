# 🎉 QurachiBot — Telegram Giveaway & Contest Bot

A full-featured multilingual Telegram bot for organizing **giveaways** and **contests**, with a web dashboard, loyalty system, referrals, moderation, and more.

**Languages:** 🇬🇧 English | 🇷🇺 Русский | 🇺🇿 O'zbekcha

---

## Features

### 🎁 Giveaways
- Create with title, description, prize, winner count, and duration
- One-click join via inline button
- **Auto-draw** when timer expires
- Manual draw support
- **Bonus entries** through referrals and loyalty points

### 🏅 Contests
- Text, photo, or any submission types
- **Submission deadlines** with auto-close
- Community voting system
- Phase management (submissions → voting → results)
- Configurable max submissions per user

### 👥 Referral System
- `/referral` — Generate a personal referral link
- Bonus entries in giveaways for referrers
- Loyalty points awarded for successful referrals
- Tracked in admin panel

### 💎 Loyalty Points & Leaderboard
- Points for: joining, submitting, voting, winning, referring
- `/points` — View balance and stats
- `/leaderboard` — Top 15 users by points
- `/redeem <giveaway_id>` — Spend points for extra entries

### 🛡 Admin Panel
- `/admin` — Full admin dashboard with inline buttons
- **Live stats**: giveaways, contests, users, referrals, moderation
- **Blacklist management**: `/ban`, `/unban`
- **Content flags** review
- **Top referrers** tracking

### 🔒 Moderation & Safety
- **Blacklist** — Ban users from all participation
- **Rate limiting** — Prevents spam (configurable per action)
- **Content moderation** — Auto-flags banned patterns, spam URLs, repeated chars
- **Captcha** — Math-based verification (utility ready for integration)

### 🔔 Notifications & Alerts
- `/subscribe` — Get notified of new giveaways/contests
- **Ending-soon reminders** (1 hour before deadline)
- **Winner DMs** — Automatic private messages to winners
- **New event alerts** — Subscribers notified of new events

### 🌐 Web Dashboard
- Real-time statistics page
- Recent giveaways & contests tables
- Leaderboard display
- Moderation overview (blacklisted, flagged)
- **Auto-refresh** every 30 seconds
- REST API endpoints (`/api/stats`, `/api/leaderboard`, `/api/giveaways`, `/api/contests`)
- Dark theme, responsive design

### 🌍 Multilingual (i18n)
- Per-user language preference (stored in DB)
- `/lang` command with inline keyboard
- Easy to add new languages (just a JSON file)

---

## Quick Start

### Prerequisites
- Python 3.10+
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### 1. Install

```bash
git clone https://github.com/sulujuni/qurachibot.git
cd qurachibot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env:
#   BOT_TOKEN=your_token
#   ADMIN_IDS=your_telegram_user_id
```

### 3. Run the Bot

```bash
python3 main.py
```

### 4. Run the Web Dashboard (optional)

```bash
python3 web_server.py
# Open http://localhost:8080
```

---

## All Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/help` | Full command list |
| `/lang` | Change language |
| **Giveaways** | |
| `/newgiveaway` | Create a giveaway |
| `/draw [id]` | Draw winners |
| `/mygiveaways` | List your giveaways |
| `/cancelgiveaway [id]` | Cancel a giveaway |
| **Contests** | |
| `/newcontest` | Create a contest |
| `/submit [id] [text]` | Submit entry |
| `/vote [id]` | Vote for submission |
| `/submissions [id]` | View submissions |
| `/startvoting [id]` | Start voting phase |
| `/endcontest [id]` | End & announce winners |
| `/mycontests` | List your contests |
| `/cancelcontest [id]` | Cancel a contest |
| **Loyalty & Social** | |
| `/points` | View your points |
| `/leaderboard` | Top users |
| `/redeem [gw_id]` | Spend points for extra entry |
| `/referral [gw_id]` | Get your referral link |
| **Notifications** | |
| `/subscribe` | Subscribe to alerts |
| `/unsubscribe` | Unsubscribe from alerts |
| **Admin** | |
| `/admin` | Admin panel |
| `/ban [user_id] [reason]` | Ban a user |
| `/unban [user_id]` | Unban a user |

---

## Project Structure

```
qurachibot/
├── main.py                    # Bot entry point
├── web_server.py              # Web dashboard entry point
├── requirements.txt
├── .env.example
├── bot/
│   ├── config.py
│   ├── jobs.py                # Scheduled tasks (auto-draw, reminders)
│   ├── handlers/
│   │   ├── common.py         # /start, /help, /lang
│   │   ├── giveaway.py       # Giveaway CRUD
│   │   ├── contest.py        # Contest CRUD
│   │   ├── admin.py          # Admin panel
│   │   ├── loyalty_handler.py # /points, /leaderboard, /redeem
│   │   ├── referral_handler.py # /referral
│   │   └── alerts.py         # /subscribe, /unsubscribe
│   ├── i18n/
│   │   ├── core.py           # Translation engine
│   │   └── locales/{en,ru,uz}/messages.json
│   ├── models/
│   │   ├── giveaway.py       # Giveaway, Participant, Winner
│   │   ├── contest.py        # Contest, Submission, Vote
│   │   ├── loyalty.py        # LoyaltyPoints, PointsTransaction
│   │   ├── referral.py       # Referral tracking
│   │   ├── moderation.py     # Blacklist, RateLimitLog, ContentFlag
│   │   ├── notification.py   # AlertSubscription, ScheduledReminder
│   │   └── user_settings.py  # Language preferences
│   └── utils/
│       ├── lang.py            # User language helpers
│       ├── captcha.py         # Math captcha generator
│       ├── rate_limit.py      # Rate limiting
│       ├── loyalty.py         # Points award/spend logic
│       ├── moderation.py      # Blacklist & content checks
│       └── referral.py        # Referral link generation
└── web/
    ├── app.py                 # FastAPI application
    ├── templates/dashboard.html
    └── static/css/style.css
```

---

## Adding a New Language

1. Create `bot/i18n/locales/<code>/messages.json`
2. Translate all keys from the English file
3. Add code to `SUPPORTED_LANGUAGES` in `bot/i18n/core.py`
4. Add display name to `LANG_NAMES` in `bot/handlers/common.py`

---

## Tech Stack

- **[python-telegram-bot](https://python-telegram-bot.org/)** v21 — Async Telegram API
- **[SQLAlchemy](https://www.sqlalchemy.org/)** v2 — Async ORM
- **[FastAPI](https://fastapi.tiangolo.com/)** — Web dashboard & REST API
- **[aiosqlite](https://github.com/omnilib/aiosqlite)** — Async SQLite
- **[Jinja2](https://jinja.palletsprojects.com/)** — HTML templates
- **[uvicorn](https://www.uvicorn.org/)** — ASGI server

---

## License

MIT
