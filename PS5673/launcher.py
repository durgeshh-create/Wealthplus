"""
ETF Trading Bot — Single entry point for compiled distribution.

On first run  : detects missing credentials and runs the auth setup wizard.
On normal run : starts the web dashboard directly.
"""
import sys
import os
from pathlib import Path

# Ensure the project root is importable when running as a script (not frozen)
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))


def _ask(prompt: str) -> str:
    """input() wrapper that flushes stdout first (needed in some console modes)."""
    print(prompt, end='', flush=True)
    return sys.stdin.readline().strip()


def run_auth_setup():
    """Run the interactive authentication setup wizard."""
    from scripts.setup_auth import main as setup_main
    setup_main()


def run_dashboard():
    """Start the trading dashboard (blocking call)."""
    import dashboard
    dashboard.main()


def auth_is_configured() -> bool:
    """Return True if at least one auth method is already saved and valid."""
    from backend.core.config import Config
    return Config.CREDENTIALS_FILE.exists() or Config.ENCTOKEN_FILE.exists()


def print_banner():
    print()
    print("=" * 62)
    print("   ETF Trading Bot")
    print("=" * 62)
    print()


def show_menu():
    print("=" * 62)
    print("   ETF Trading Bot  —  Menu")
    print("=" * 62)
    print()
    print("  [0]  Setup / Change Login Credentials")
    print("       Configure your Zerodha login (run this first!)")
    print()
    print("  [1]  Launch Dashboard")
    print("       Start the bot + open http://localhost:5000")
    print()
    print("  [2]  Exit")
    print()
    print("─" * 62)
    print()


def main():
    print_banner()

    # First-run detection: no credentials at all
    if not auth_is_configured():
        print("  No login credentials found.")
        print("  Please complete the one-time setup before starting the bot.")
        print()
        print("─" * 62)
        print()
        run_auth_setup()
        print()
        # After setup, offer to continue straight to dashboard
        print()
        choice = _ask("  Launch dashboard now? [y/n]: ").lower()
        if choice == 'y':
            run_dashboard()
        return

    # Normal run — show menu
    while True:
        show_menu()
        choice = _ask("  Enter choice (0 / 1 / 2): ")

        if choice == '0':
            run_auth_setup()
            input("\n  Press Enter to return to menu...")
        elif choice == '1':
            run_dashboard()
            # Dashboard only returns if user pressed Ctrl+C or an error occurred
            input("\n  Press Enter to return to menu...")
        elif choice == '2':
            print("\n  Goodbye!\n")
            sys.exit(0)
        else:
            print("\n  Please enter 0, 1, or 2.\n")


if __name__ == '__main__':
    main()
