"""
Smart Logging System for ETF Trading
- Overwrites log file on each start
- Clear, structured logging with timestamps
- Different log levels for different components
"""
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

from backend.core.config import Config

_IST = timezone(timedelta(hours=5, minutes=30))


class ISTFormatter(logging.Formatter):
    """Logging formatter that stamps all records in IST instead of local/UTC."""
    def converter(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=_IST).timetuple()

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_IST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%H:%M:%S')


class TradingLogger:
    """Centralized logging system for the trading application"""
    
    _instance: Optional['TradingLogger'] = None
    _initialized = False
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        if TradingLogger._initialized:
            return
        
        # Ensure logs directory exists
        Config.ensure_directories()
        
        # Configure root logger
        self.setup_logging()
        TradingLogger._initialized = True
    
    def setup_logging(self):
        """Set up logging configuration"""
        
        # Remove existing handlers
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # Set root logger level
        root_logger.setLevel(logging.DEBUG)
        
        # Create formatters — all timestamps in IST
        detailed_formatter = ISTFormatter(
            '%(asctime)s IST | %(levelname)-8s | %(name)-20s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        simple_formatter = ISTFormatter(
            '%(asctime)s IST | %(levelname)-8s | %(message)s',
            datefmt='%H:%M:%S'
        )
        
        # File handler (overwrites on start)
        file_handler = logging.FileHandler(
            Config.LOG_FILE,
            mode='w',  # Overwrite mode
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(detailed_formatter)
        
        # Console handler (with UTF-8 encoding for Windows)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.WARNING)  # Only show warnings/errors in terminal
        console_handler.setFormatter(simple_formatter)
        # Force UTF-8 encoding for console output
        if hasattr(sys.stdout, 'reconfigure'):
            try:
                sys.stdout.reconfigure(encoding='utf-8')
            except:
                pass  # Ignore if reconfigure fails
        
        # Add handlers to root logger
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)
        
        # Log startup
        startup_logger = logging.getLogger('SYSTEM')
        startup_logger.info("=" * 80)
        startup_logger.info(f"ETF Trading System Started - {datetime.now(_IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
        startup_logger.info(f"Log file: {Config.LOG_FILE}")
        startup_logger.info(f"Mode: {'DRY RUN' if Config.is_dry_run() else 'LIVE TRADING'}")
        startup_logger.info("=" * 80)
    
    @staticmethod
    def get_logger(name: str) -> logging.Logger:
        """Get a logger instance with the given name"""
        return logging.getLogger(name)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance
    
    Args:
        name: Logger name (usually __name__)
        
    Returns:
        Configured logger instance
    """
    TradingLogger()  # Ensure logging is initialized
    return logging.getLogger(name)


# Convenience function for logging session separators
def log_separator(logger: logging.Logger, title: str = ""):
    """Log a visual separator"""
    logger.info("-" * 80)
    if title:
        logger.info(f"{title:^80}")
        logger.info("-" * 80)
