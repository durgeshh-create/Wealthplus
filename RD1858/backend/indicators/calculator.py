"""
Technical Indicators Calculator
Implements Williams %R (Daily) using proven methodology
"""
import pandas as pd
from typing import Optional

from backend.core.config import Config
from backend.utils.logger import get_logger

logger = get_logger(__name__)


def calculate_williams_r(
    data: pd.DataFrame,
    period: int = None,
    current_price: Optional[float] = None
) -> Optional[float]:
    """
    Calculate Williams %R indicator
    Formula: ((Highest High - Close) / (Highest High - Lowest Low)) * -100
    
    This is the PROVEN implementation from indicator_monitor.py
    
    Args:
        data: DataFrame with 'high', 'low', 'close' columns
        period: Lookback period (default from Config)
        current_price: Optional current price to use instead of last close
        
    Returns:
        Williams %R value (range: -100 to 0) or None if insufficient data
    """
    if period is None:
        period = Config.WILLIAMS_R_PERIOD
    
    try:
        # Check if we have enough data
        if len(data) < period:
            logger.debug(f"Insufficient data: {len(data)} < {period}")
            return None
        
        # Get last N periods
        last_n = data.iloc[-period:]
        
        # Calculate highest high and lowest low
        highest_high = last_n['high'].max()
        lowest_low = last_n['low'].min()
        
        # Use current price if provided, otherwise use last close
        if current_price is not None:
            close_price = current_price
        else:
            close_price = data['close'].iloc[-1]
        
        # Avoid division by zero
        if highest_high == lowest_low:
            logger.debug("Highest high equals lowest low, returning -50")
            return -50.0
        
        # Calculate Williams %R
        williams_r = ((highest_high - close_price) / (highest_high - lowest_low)) * -100
        
        # Clamp to valid range
        williams_r = max(-100.0, min(0.0, williams_r))
        
        return round(williams_r, 2)
        
    except Exception as e:
        logger.error(f"Error calculating Williams %R: {e}")
        return None


def calculate_daily_williams_r(
    historical_data: pd.DataFrame,
    live_price: Optional[float] = None,
    live_high: Optional[float] = None,
    live_low: Optional[float] = None,
    period: int = None
) -> Optional[float]:
    """
    Calculate Daily Williams %R with live data integration
    
    Args:
        historical_data: Historical daily OHLC data
        live_price: Current live price
        live_high: Today's high (if available)
        live_low: Today's low (if available)
        period: Lookback period
        
    Returns:
        Williams %R value or None
    """
    if period is None:
        period = Config.WILLIAMS_R_PERIOD
    
    try:
        if historical_data is None or len(historical_data) == 0:
            return None
        
        # If we have live data, integrate it
        if live_price is not None:
            # Create a working copy
            df = historical_data.copy()
            
            # Create today's row based on live data
            today_row = {
                'high': live_high if live_high is not None else live_price,
                'low': live_low if live_low is not None else live_price,
                'close': live_price
            }
            
            # Append today's data
            today_df = pd.DataFrame([today_row])
            combined_df = pd.concat([df, today_df], ignore_index=True)
            
            # Calculate using combined data
            return calculate_williams_r(combined_df, period)
        else:
            # Calculate using historical data only
            return calculate_williams_r(historical_data, period)
            
    except Exception as e:
        logger.error(f"Error calculating daily Williams %R: {e}")
        return None


def get_signal_status(williams_r: Optional[float], threshold: float = None) -> str:
    """
    Get signal status based on Williams %R value
    
    Args:
        williams_r: Williams %R value
        threshold: Buy threshold (default from Config)
        
    Returns:
        "OVERSOLD" if <= threshold, "OVERBOUGHT" if >= -20, "NEUTRAL" otherwise
    """
    if threshold is None:
        threshold = Config.WILLIAMS_R_THRESHOLD
    
    if williams_r is None:
        return "UNKNOWN"
    
    if williams_r <= threshold:
        return "OVERSOLD"  # Buy signal
    elif williams_r >= -20:
        return "OVERBOUGHT"  # Sell signal territory
    else:
        return "NEUTRAL"
