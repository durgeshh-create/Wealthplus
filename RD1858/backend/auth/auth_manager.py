"""
Authentication Manager for Zerodha
Handles login, token management, and session verification
"""
import json
import os
import requests
import pyotp
from datetime import datetime
from typing import Optional, Dict
from pathlib import Path

from backend.core.config import Config
from backend.utils.logger import get_logger
from backend.auth.token_store import save_token, load_token, delete_token

logger = get_logger(__name__)


class AuthManager:
    """Manages Zerodha authentication"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        })
        
        self.user_id: Optional[str] = None
        self.enctoken: Optional[str] = None
        self.is_authenticated = False
        
        Config.ensure_directories()
    
    def authenticate(self) -> bool:
        """
        Authenticate with Zerodha
        Tries multiple methods in order:
        1. Saved enctoken
        2. Fresh login with credentials
        
        Returns:
            True if authentication successful, False otherwise
        """
        logger.info("Starting authentication...")
        
        # Try saved enctoken first
        if self._load_saved_enctoken():
            logger.info("✓ Authenticated using saved enctoken")
            return True
        
        # Try fresh login
        if self._login_with_credentials():
            logger.info("✓ Authenticated using fresh login")
            return True
        
        # No valid auth found — login page will handle this
        logger.warning("No valid session — please log in via the browser at http://localhost:5000")
        return False
    
    def _load_saved_enctoken(self) -> bool:
        """
        Load and verify saved enctoken.
        Uses token_store which checks local file first, then Upstash Redis.
        """
        try:
            auth_data = load_token()
            if not auth_data:
                logger.debug("No saved enctoken found in any store")
                return False

            self.user_id  = auth_data.get('user_id')
            self.enctoken = auth_data.get('enctoken')

            if not self.user_id or not self.enctoken:
                logger.warning("Invalid enctoken data in store")
                return False

            self._update_session_headers()

            if self._verify_session():
                self.is_authenticated = True
                logger.info(f"Loaded saved enctoken for user {self.user_id}")
                return True
            else:
                logger.warning("Saved enctoken is expired")
                return False

        except Exception as e:
            logger.error(f"Error loading saved enctoken: {e}")
            return False
    
    def _login_with_credentials(self) -> bool:
        """Perform fresh login using credentials"""
        try:
            if not Config.CREDENTIALS_FILE.exists():
                logger.debug("No credentials file found — skipping auto-login")
                return False

            with open(Config.CREDENTIALS_FILE, 'r') as f:
                creds = json.load(f)

            user_id  = creds.get('user_id',  '').strip()
            password = creds.get('password', '').strip()
            totp_key = creds.get('totp_key', '').strip()

            if not user_id or not password or not totp_key:
                logger.debug("Credentials file incomplete — skipping auto-login")
                return False

            # Basic sanity check: TOTP keys are base32 (uppercase A-Z + 2-7)
            import re
            if not re.match(r'^[A-Z2-7]+=*$', totp_key.upper()):
                logger.debug("TOTP key appears invalid — skipping auto-login")
                return False
            
            logger.info("Performing fresh login...")
            
            # Step 1: Initial login
            login_data = {
                'user_id': user_id,
                'password': password
            }
            
            response = self.session.post(
                f'{Config.ZERODHA_API_BASE}/api/login',
                data=login_data
            )
            
            if response.status_code != 200:
                logger.error(f"Login failed: {response.text}")
                return False
            
            # Step 2: Submit TOTP
            totp = pyotp.TOTP(totp_key)
            twofa_data = {
                'user_id': user_id,
                'request_id': response.json()['data']['request_id'],
                'twofa_value': totp.now()
            }
            
            response = self.session.post(
                f'{Config.ZERODHA_API_BASE}/api/twofa',
                data=twofa_data
            )
            
            if response.status_code != 200:
                logger.error(f"2FA failed: {response.text}")
                return False
            
            # Extract enctoken
            if 'enctoken' in self.session.cookies:
                self.enctoken = self.session.cookies['enctoken']
                self.user_id = user_id
                self._update_session_headers()
                
                # Save for future use
                self._save_enctoken()
                
                self.is_authenticated = True
                logger.info(f"Fresh login successful for user {user_id}")
                return True
            else:
                logger.error("Enctoken not found in response")
                return False
                
        except Exception as e:
            logger.error(f"Error during login: {e}")
            return False
    
    def _update_session_headers(self):
        """Update session headers with authentication token"""
        if self.enctoken:
            self.session.headers.update({
                'Authorization': f'enctoken {self.enctoken}',
                'Accept': 'application/json, text/plain, */*'
            })
    
    def _verify_session(self) -> bool:
        """Verify if the current session is valid"""
        try:
            response = self.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/user/profile",
                timeout=10
            )
            
            if response.status_code == 200:
                profile = response.json()
                if profile.get('data', {}).get('user_name'):
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Session verification failed: {e}")
            return False
    
    def _save_enctoken(self):
        """Persist enctoken via token_store (local file + Upstash Redis)."""
        try:
            save_token(self.user_id, self.enctoken)
        except Exception as e:
            logger.error(f"Error saving enctoken: {e}")
    
    def get_profile(self) -> Optional[Dict]:
        """Get user profile information"""
        try:
            response = self.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/user/profile",
                timeout=10
            )
            
            if response.status_code == 200:
                return response.json().get('data', {})
            
            return None
            
        except Exception as e:
            logger.error(f"Error fetching profile: {e}")
            return None
