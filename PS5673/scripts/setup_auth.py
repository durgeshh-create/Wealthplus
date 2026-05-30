"""
Authentication Setup Script
===========================
Run this FIRST before starting the trading bot.

Two options:
  1. Automatic Login  — provide credentials, bot logs in every day by itself
  2. Manual Enctoken  — copy-paste enctoken from your browser (must refresh daily)
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Make sure imports work whether run directly or from project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.config import Config
from backend.auth.manager import AuthManager


# ─── Helpers ──────────────────────────────────────────────────────────────────

def clear():
    os.system('cls' if os.name == 'nt' else 'clear')

def separator():
    print("─" * 62)

def header():
    clear()
    print("=" * 62)
    print("   ETF Trading Bot  ·  Authentication Setup")
    print("=" * 62)
    print()


# ─── Option 1 : Automatic Login ───────────────────────────────────────────────

def option_auto_login():
    """Set up automatic login via credentials.json"""
    header()
    print("  OPTION 1 — Automatic Login")
    separator()
    print()
    print("  The bot will log into Zerodha automatically using your")
    print("  User ID, Password, and TOTP secret key.")
    print()
    print("  HOW TO FIND YOUR TOTP SECRET KEY")
    print("  (This is the long code, NOT the 6-digit OTP)")
    print()
    print("  1. Open https://console.zerodha.com")
    print("  2. Go to  Account  →  Security")
    print("  3. Under 'Two-factor authentication' click  Reset")
    print("  4. Select  'Use an authenticator app'")
    print("  5. The SECRET KEY is shown below the QR code")
    print("     (looks like:  JBSWY3DPEHPK3PXP)")
    print("  6. Copy that key and paste it below.")
    print("     Also scan the QR / enter the key in your auth app.")
    print()
    separator()
    print()

    user_id  = input("  Zerodha User ID   : ").strip().upper()
    password = input("  Zerodha Password  : ").strip()
    totp_key = input("  TOTP Secret Key   : ").strip().upper().replace(" ", "")

    if not all([user_id, password, totp_key]):
        print("\n  All three fields are required. No changes made.")
        return False

    credentials = {
        "user_id":   user_id,
        "password":  password,
        "totp_key":  totp_key
    }

    Config.ensure_directories()
    with open(Config.CREDENTIALS_FILE, 'w') as f:
        json.dump(credentials, f, indent=4)

    print()
    print(f"  Saved  →  {Config.CREDENTIALS_FILE}")
    print()
    print("  Testing login with Zerodha...")
    print()

    auth = AuthManager()
    if auth.authenticate():
        print(f"  ✓  Login successful!  (User: {auth.user_id})")
        print(f"  ✓  Session saved  →  {Config.ENCTOKEN_FILE}")
        return True
    else:
        print("  ✗  Login failed.")
        print()
        print("  Common reasons:")
        print("    • Wrong password")
        print("    • Wrong TOTP key (make sure it is the SECRET, not the 6-digit OTP)")
        print("    • Account is locked — try logging in on kite.zerodha.com first")
        return False


# ─── Option 2 : Manual Enctoken ───────────────────────────────────────────────

def option_manual_enctoken():
    """Set up manual enctoken from browser"""
    header()
    print("  OPTION 2 — Manual Enctoken")
    separator()
    print()
    print("  Log in to Zerodha in your browser, then copy the")
    print("  enctoken cookie and paste it here.")
    print()
    print("  STEP-BY-STEP (Chrome / Edge)")
    print()
    print("  1. Open  https://kite.zerodha.com  and log in normally")
    print("  2. Press  F12  to open Developer Tools")
    print("  3. Click the  Application  tab  (Chrome)")
    print("     or  Storage  tab  (Firefox / Edge)")
    print("  4. On the left expand  Cookies")
    print("     → click  https://kite.zerodha.com")
    print("  5. Find the row named  enctoken")
    print("  6. Click on it and copy the full value from the")
    print("     'Value' column (it is a long string)")
    print()
    print("  ⚠  The enctoken expires every day at midnight.")
    print("     You will need to repeat this each trading day.")
    print("     Use Option 1 to avoid this.")
    print()
    separator()
    print()

    user_id  = input("  Zerodha User ID  (e.g. AB1234) : ").strip().upper()
    enctoken = input("  Paste enctoken value            : ").strip()

    if not all([user_id, enctoken]):
        print("\n  Both fields are required. No changes made.")
        return False

    auth_data = {
        "user_id":   user_id,
        "enctoken":  enctoken,
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }

    Config.ensure_directories()
    with open(Config.ENCTOKEN_FILE, 'w') as f:
        json.dump(auth_data, f, indent=4)

    print()
    print(f"  Saved  →  {Config.ENCTOKEN_FILE}")
    print()
    print("  Verifying enctoken with Zerodha...")
    print()

    auth = AuthManager()
    if auth.authenticate():
        print(f"  ✓  Enctoken verified!  (User: {auth.user_id})")
        return True
    else:
        print("  ✗  Enctoken verification failed.")
        print()
        print("  Common reasons:")
        print("    • Token has already expired — copy a fresh one from the browser")
        print("    • You copied only part of the value — it must be the full string")
        print("    • Wrong User ID entered")
        return False


# ─── Main menu ────────────────────────────────────────────────────────────────

def check_existing_auth():
    """Return True if a valid session already exists."""
    if not (Config.CREDENTIALS_FILE.exists() or Config.ENCTOKEN_FILE.exists()):
        return False

    auth = AuthManager()
    return auth.authenticate()


def main():
    header()

    # Check if already authenticated
    if check_existing_auth():
        from backend.auth.manager import AuthManager as _A
        a = _A()
        a.authenticate()
        print(f"  ✓  Active session found  (User: {a.user_id})")
        print()
        print("  Do you want to reconfigure anyway?")
        print()
        print("  [y]  Yes, set up fresh credentials")
        print("  [n]  No, keep current setup and exit")
        print()
        choice = input("  Your choice: ").strip().lower()
        if choice != 'y':
            print()
            print("  No changes made. Run  python run.py  to start the bot.")
            print()
            return
        print()

    print("  Choose how you want to log in to Zerodha:")
    print()
    print("  [1]  Automatic Login  (Recommended)")
    print("       Provide your User ID, Password, and TOTP secret key.")
    print("       The bot logs in automatically — no daily manual steps.")
    print()
    print("  [2]  Manual Enctoken")
    print("       Log in via browser, copy the enctoken cookie, paste here.")
    print("       You must repeat this every trading day.")
    print()
    separator()
    print()

    choice = input("  Enter 1 or 2: ").strip()
    print()

    if choice == '1':
        success = option_auto_login()
    elif choice == '2':
        success = option_manual_enctoken()
    else:
        print("  Invalid choice. Please run the script again and enter 1 or 2.")
        return

    print()
    separator()
    if success:
        print()
        print("  Setup complete!  You are ready to trade.")
        print()
        print("  Next step:  python run.py")
        print()
    else:
        print()
        print("  Setup did not complete successfully.")
        print("  Fix the issue above and run this script again.")
        print()
        print("  Script:  python scripts/setup_auth.py")
        print()


if __name__ == "__main__":
    main()
