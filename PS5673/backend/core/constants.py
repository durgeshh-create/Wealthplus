"""
Constants for ETF Trading System
Defines constants for trading operations

NOTE: Active ETFs are configured in settings.json, not hardcoded here!
This ensures the bot only trades what you configure.
"""

# Cash parking ETF (required for strategy)
LIQUIDCASE_SYMBOL = 'LIQUIDCASE'

# Market Indices (for dashboard display)
MARKET_INDICES = {
    'NIFTY 50':         {'exchange': 'NSE', 'segment': 'NSE-INDICES'},
    'NIFTY MIDCAP 150': {'exchange': 'NSE', 'segment': 'NSE-INDICES'},
    'INDIA VIX':        {'exchange': 'NSE', 'segment': 'NSE-INDICES'},
}

# Order transaction types
TRANSACTION_BUY = "BUY"
TRANSACTION_SELL = "SELL"

# Order status
ORDER_STATUS_COMPLETE = "COMPLETE"
ORDER_STATUS_CANCELLED = "CANCELLED"
ORDER_STATUS_REJECTED = "REJECTED"
ORDER_STATUS_OPEN = "OPEN"
ORDER_STATUS_TRIGGER_PENDING = "TRIGGER PENDING"

# Signal types
SIGNAL_BUY = "BUY"
SIGNAL_SELL = "SELL"
SIGNAL_NONE = "NONE"
