"""
Central Configuration for ETF Trading System
Single source of truth for all configuration parameters
"""
import sys
from pathlib import Path
from typing import Dict, List

class Config:
    """Central configuration class"""

    # Project paths — works for both normal Python and PyInstaller frozen exe
    if getattr(sys, 'frozen', False):
        # Running as a compiled .exe — exe sits at the project root
        PROJECT_ROOT = Path(sys.executable).parent
    else:
        PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    BACKEND_DIR = PROJECT_ROOT / "backend"
    CONFIG_DIR = PROJECT_ROOT / "config"
    DATA_DIR = PROJECT_ROOT / "data"
    LOGS_DIR = PROJECT_ROOT / "logs"
    
    # Data paths
    DAILY_DATA_DIR = DATA_DIR / "daily"
    WEEKLY_DATA_DIR = DATA_DIR / "weekly"
    
    # Config files
    ENCTOKEN_FILE = CONFIG_DIR / "enctoken.json"
    CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
    SETTINGS_FILE = CONFIG_DIR / "settings.json"
    
    # Log file
    LOG_FILE = LOGS_DIR / "trading.log"
    
    # Trading parameters
    SLOTS_COUNT = 2
    PROFIT_TARGET_PCT = 3.0
    WILLIAMS_R_THRESHOLD = -80
    WILLIAMS_R_PERIOD = 14

    # Cash deployment limits
    MAX_CASH_PER_STOCK = 50000        # Max cumulative cash deployed per ETF/stock
    MAX_CASH_PER_TRANSACTION = 10000  # Max cash per individual buy order
    CASH_RESERVE = 5000               # Minimum idle cash to keep — never deployed or parked in LIQUIDCASE

    # Intraday Strategy (BANKBEES — Williams %R Mean Reversion)
    INTRADAY_SYMBOL = 'BANKBEES'
    INTRADAY_MAX_ATTEMPTS = 5
    INTRADAY_BUY_FUND_PER_ATTEMPT = 10000   # ₹ per buy attempt (user configurable)
    INTRADAY_PROFIT_TARGET_PCT = 1.0
    INTRADAY_STEP_PCT = 0.25                # 0.25% further drop/rise for next attempt
    INTRADAY_WILLIAMS_PERIOD = 14
    INTRADAY_SQUAREOFF_TIME = "15:15"       # HH:MM IST
    
    # Zerodha endpoints
    ZERODHA_WS_URL = "wss://ws.zerodha.com"
    ZERODHA_API_BASE = "https://kite.zerodha.com"
    KITE_API_BASE = "https://api.kite.trade"
    
    # Hardcoded fallback — only used if settings.json is missing/corrupt
    # Safe default is DRY_RUN=True so a config failure never causes live trades
    DRY_RUN = True

    @classmethod
    def is_dry_run(cls) -> bool:
        """
        Read trading_mode from settings.json.
        Returns True (dry run) on any error — never fail open to live trading.
        """
        try:
            settings = cls.load_settings()
            mode = settings.get('trading_mode', '')
            if mode:
                return mode.upper() != 'LIVE'
            return cls.DRY_RUN
        except Exception:
            return True  # Always fail safe
    
    # Market hours (IST)
    MARKET_OPEN_HOUR = 9
    MARKET_OPEN_MINUTE = 15
    MARKET_CLOSE_HOUR = 15
    MARKET_CLOSE_MINUTE = 30
    
    # Order parameters
    ORDER_TYPE = "MARKET"
    PRODUCT_TYPE = "CNC"  # Cash and Carry
    EXCHANGE = "NSE"
    ORDER_VARIETY = "regular"
    
    # WebSocket settings
    WS_RECONNECT_DELAY = 5
    WS_PING_INTERVAL = 30
    
    # Data refresh intervals (seconds)
    PORTFOLIO_REFRESH_INTERVAL = 60
    MARKET_DATA_REFRESH_INTERVAL = 2
    
    @classmethod
    def ensure_directories(cls):
        """Create required directories if they don't exist"""
        cls.CONFIG_DIR.mkdir(exist_ok=True)
        cls.DATA_DIR.mkdir(exist_ok=True)
        cls.DAILY_DATA_DIR.mkdir(exist_ok=True)
        cls.WEEKLY_DATA_DIR.mkdir(exist_ok=True)
        cls.LOGS_DIR.mkdir(exist_ok=True)
    
    @classmethod
    def load_settings(cls) -> Dict:
        """Load settings from settings.json"""
        import json
        
        if not cls.SETTINGS_FILE.exists():
            # Return default settings
            return {
                'active_etfs': ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'],
                'slots_count': cls.SLOTS_COUNT,
                'profit_target_pct': cls.PROFIT_TARGET_PCT,
                'williams_r_threshold': cls.WILLIAMS_R_THRESHOLD,
                'williams_r_period': cls.WILLIAMS_R_PERIOD,
                'max_cash_per_stock': cls.MAX_CASH_PER_STOCK,
                'max_cash_per_transaction': cls.MAX_CASH_PER_TRANSACTION,
            }
        
        try:
            with open(cls.SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading settings: {e}")
            return {
                'active_etfs': ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'],
                'slots_count': cls.SLOTS_COUNT,
                'profit_target_pct': cls.PROFIT_TARGET_PCT,
                'williams_r_threshold': cls.WILLIAMS_R_THRESHOLD,
                'williams_r_period': cls.WILLIAMS_R_PERIOD,
                'max_cash_per_stock': cls.MAX_CASH_PER_STOCK,
                'max_cash_per_transaction': cls.MAX_CASH_PER_TRANSACTION,
            }
    
    @classmethod
    def get_cash_reserve(cls) -> float:
        """Minimum idle cash to keep — never deployed or auto-parked into LIQUIDCASE."""
        try:
            settings = cls.load_settings()
            return float(settings.get('cash_reserve', cls.CASH_RESERVE))
        except Exception:
            return cls.CASH_RESERVE

    @classmethod
    def get_active_etfs(cls) -> List[str]:
        """Get list of active ETFs from settings"""
        settings = cls.load_settings()
        return settings.get('active_etfs', ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'])

    @classmethod
    def get_bnh_symbols(cls) -> List[str]:
        """Get list of Dip Accumulator symbols from settings"""
        settings = cls.load_settings()
        return settings.get('bnh_symbols', ['MID150BEES'])

    @classmethod
    def get_all_monitored_symbols(cls) -> List[str]:
        """All symbols needing historical data: active_etfs + bnh_symbols, deduplicated."""
        settings = cls.load_settings()
        active = settings.get('active_etfs', ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'])
        bnh    = settings.get('bnh_symbols', ['MID150BEES'])
        seen, combined = set(), []
        for s in active + bnh:
            if s not in seen:
                seen.add(s)
                combined.append(s)
        return combined
