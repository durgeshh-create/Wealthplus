"""
ETF Trading Bot - Standalone CLI Mode
Monitors market and executes trades based on strategy.

⚠️ NOTE: This is the standalone CLI trading bot.
For normal use, launch the dashboard instead: python dashboard.py
The dashboard includes both visualization AND automated trading.

This file is provided for:
- CLI-only trading (no web interface)
- Development and testing
- Running on headless servers
"""
import sys
import time
import signal
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.config import Config
from backend.utils.logger import get_logger, log_separator
from backend.auth.manager import AuthManager
from backend.data.historical import HistoricalDataManager
from backend.data.realtime import RealtimeDataManager
from backend.portfolio.tracker import PortfolioTracker
from backend.orders.manager import OrderManager
from backend.strategy.signal_generator import SignalGenerator
from backend.strategy.executor import StrategyExecutor

logger = get_logger(__name__)


class TradingBot:
    """Main trading bot"""
    
    def __init__(self):
        self.running = False
        self.auth = None
        self.historical = None
        self.realtime = None
        self.portfolio = None
        self.orders = None
        self.signal_gen = None
        self.executor = None
    
    def initialize(self) -> bool:
        """Initialize all components"""
        log_separator(logger, "INITIALIZING TRADING BOT")
        
        try:
            # Authentication
            logger.info("Authenticating...")
            self.auth = AuthManager()
            if not self.auth.authenticate():
                logger.error("Authentication failed")
                return False
            logger.info("✓ Authenticated")
            
            # Historical data (auto-refreshes stale CSVs via Zerodha API)
            logger.info("Loading & refreshing historical data...")
            self.historical = HistoricalDataManager(self.auth)
            logger.info("✓ Historical data loaded")
            
            # Real-time data
            logger.info("Initializing real-time data...")
            self.realtime = RealtimeDataManager(self.auth)
            if not self.realtime.initialize():
                logger.error("Failed to initialize real-time data")
                return False
            if not self.realtime.start():
                logger.error("Failed to start real-time data")
                return False
            logger.info("✓ Real-time data connected")
            
            # Wait for initial data
            logger.info("Waiting for market data...")
            time.sleep(5)
            
            # Portfolio — retry up to 5 times with backoff (handles transient
            # Zerodha rejections immediately after a mid-session restart)
            logger.info("Syncing portfolio...")
            self.portfolio = PortfolioTracker(self.auth)
            _synced = False
            for _attempt in range(1, 6):
                if self.portfolio.sync():
                    _synced = True
                    break
                logger.warning(f"Portfolio sync attempt {_attempt}/5 failed — retrying in 10s...")
                time.sleep(10)
            if not _synced:
                logger.error("Failed to sync portfolio after 5 attempts — proceeding with empty state")
            logger.info("✓ Portfolio synced" if _synced else "⚠️ Starting with empty portfolio state")
            
            # Orders
            self.orders = OrderManager(self.auth)
            logger.info("✓ Order manager ready")
            
            # Strategy components
            self.signal_gen = SignalGenerator(self.historical, self.realtime, self.portfolio)
            # ✅ FIX: rebuild buy counts from today's Zerodha order history
            # so slot guards work correctly after session restart (e.g. 12:35 PM session
            # knows what the 9 AM session already bought and doesn't re-buy).
            self.signal_gen.rebuild_from_order_history()
            self.executor = StrategyExecutor(self.orders, self.portfolio, self.realtime, self.signal_gen)
            logger.info("✓ Strategy components ready (with execution locking)")
            
            log_separator(logger, "INITIALIZATION COMPLETE")
            return True
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            return False
    
    def run(self):
        """Main trading loop"""
        log_separator(logger, "STARTING TRADING BOT")
        
        logger.info(f"Mode: {'DRY RUN' if Config.DRY_RUN else 'LIVE TRADING'}")
        logger.info(f"Press Ctrl+C to stop")
        logger.info("")
        
        self.running = True
        last_portfolio_sync = time.time()
        last_signal_check = time.time()
        
        try:
            while self.running:
                current_time = time.time()
                
                # Periodic portfolio sync
                if current_time - last_portfolio_sync >= Config.PORTFOLIO_REFRESH_INTERVAL:
                    logger.debug("Syncing portfolio...")
                    self.portfolio.sync()
                    last_portfolio_sync = current_time
                
                # Check for signals
                if current_time - last_signal_check >= Config.MARKET_DATA_REFRESH_INTERVAL:
                    self._check_and_execute_signals()
                    last_signal_check = current_time
                
                time.sleep(1)
                
        except KeyboardInterrupt:
            logger.info("Received stop signal")
        except Exception as e:
            logger.error(f"Error in trading loop: {e}", exc_info=True)
        finally:
            self.stop()
    
    def _check_and_execute_signals(self):
        """Check for signals and execute if found"""
        try:
            # Generate signals
            signals = self.signal_gen.get_active_signals()
            
            buy_signals = signals.get('buy', [])
            sell_signals = signals.get('sell', [])
            
            # Log active signals
            if buy_signals or sell_signals:
                logger.info("-" * 60)
                logger.info(f"Active signals - Buy: {len(buy_signals)}, Sell: {len(sell_signals)}")
                
                for signal in buy_signals:
                    logger.info(f"  BUY: {signal['symbol']} | W%R={signal['williams_r']:.2f} | ₹{signal['price']:.2f}")
                
                for signal in sell_signals:
                    logger.info(f"  SELL: {signal['symbol']} | ₹{signal['price']:.2f}")
                
                logger.info("-" * 60)
                
                # Execute signals
                if Config.DRY_RUN:
                    logger.warning("DRY RUN MODE: Signals detected but not executing")
                else:
                    logger.info("Executing signals...")
                    results = self.executor.execute_signals(signals)
                    
                    for action, success in results.items():
                        status = "✓" if success else "✗"
                        logger.info(f"{status} {action}")
            
        except Exception as e:
            logger.error(f"Error checking signals: {e}")
    
    def stop(self):
        """Stop the trading bot"""
        logger.info("Stopping trading bot...")
        self.running = False
        
        if self.realtime:
            self.realtime.stop()
        
        logger.info("Trading bot stopped")


def signal_handler(sig, frame):
    """Handle Ctrl+C"""
    logger.info("\nShutdown signal received")
    sys.exit(0)


def main():
    """Main entry point"""
    # Setup signal handler
    signal.signal(signal.SIGINT, signal_handler)
    
    # Create and initialize bot
    bot = TradingBot()
    
    if not bot.initialize():
        logger.error("Failed to initialize bot")
        return 1
    
    # Run bot
    bot.run()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
