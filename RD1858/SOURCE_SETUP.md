ETF Trading Bot  —  Source Setup Guide
=======================================

This folder contains the full source code of the ETF Trading Bot.
Run it directly with Python (no compiled executable needed).


PREREQUISITES
─────────────
  • Python 3.10 or newer
  • pip (comes with Python)


STEP 1: Install Dependencies
──────────────────────────────
Open a terminal in this folder and run:

    pip install -r requirements.txt


STEP 2: Configure Your Zerodha Login  (one time only)
──────────────────────────────────────────────────────
Copy the example credentials file and fill in your details:

    copy config\credentials.json.example config\credentials.json

Then edit config\credentials.json with your real values:

    {
        "user_id":   "YOUR_ZERODHA_USER_ID",
        "password":  "YOUR_ZERODHA_PASSWORD",
        "totp_key":  "YOUR_TOTP_SECRET_KEY"
    }

  HOW TO GET YOUR TOTP SECRET KEY:
  1. Go to https://console.zerodha.com → Account → Security
  2. Under "Two-factor authentication" click  Reset
  3. Select "Use an authenticator app"
  4. Copy the SECRET KEY shown below the QR code
     (long alphanumeric string, NOT the 6-digit OTP)
  5. Also add it to your authenticator app when prompted.

  Alternatively, run the guided setup wizard:
      python scripts/setup_auth.py


STEP 3: Launch the Bot
────────────────────────
Run the interactive menu:

    python run.py

From the menu you can:
  [0]  Change login credentials
  [1]  Launch the dashboard  →  open http://localhost:5000 in your browser
  [2]  Exit

Or launch the dashboard directly:

    python dashboard.py


TRADING MODE
─────────────
The bot starts in  DRY RUN  mode by default (config/settings.json).
No real orders are placed until you switch to  LIVE  mode from
the dashboard Settings panel.


FILE STRUCTURE
──────────────
  run.py                  ← Main menu entry point
  dashboard.py            ← Web dashboard + trading loop
  launcher.py             ← PyInstaller entry (for building .exe)
  requirements.txt        ← Python dependencies
  backend/                ← Core application logic
    auth/                   Auth & Zerodha session management
    core/                   Config, constants
    data/                   Historical + real-time data fetch
    indicators/             Technical indicator calculations
    orders/                 Order placement & management
    portfolio/              Portfolio tracking
    strategy/               Signal generation & trade execution
    utils/                  Logging helpers
  frontend/               ← Flask web dashboard
    app.py                  Flask application factory
    routes.py               API routes
    templates/              HTML templates
    static/                 CSS + JavaScript
  scripts/
    setup_auth.py           Guided credential setup wizard
    trading_bot.py          Standalone CLI trading bot
  config/
    settings.json           Strategy settings (safe to edit)
    credentials.json        YOUR credentials (create from .example)
    credentials.json.example   Template — fill in and rename
  data/
    daily/                  Daily historical price CSVs
    weekly/                 Weekly historical price CSVs
  logs/                   Bot activity logs (auto-created)


SECURITY NOTES
──────────────
  • config/credentials.json is listed in .gitignore — it will NOT
    be committed to git automatically.
  • Never share credentials.json or enctoken.json with anyone.
  • The .example file contains only placeholder text — it is safe to share.
