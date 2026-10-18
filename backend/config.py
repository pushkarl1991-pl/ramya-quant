"""
RAMYA GLOBAL FINTECH PLATFORM - Configuration & Environments
Isolated enterprise configuration for Universal Multi-Market Algorithmic Trading.
"""

import os
from pathlib import Path
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent.parent

class PlatformSettings(BaseModel):

    APP_NAME: str = "RAMYA QUANT GLOBAL (RQG)"
    APP_VERSION: str = "3.0.0-ENTERPRISE"
    DEBUG: bool = False
    
    # Server settings
    HOST: str = "0.0.0.0"
    PORT: int = 8080
    SECRET_KEY: str = "ramya-quant-global-super-secure-key-2026-v3"
    
    # Database
    DATABASE_URL: str = f"sqlite:///{BASE_DIR}/ramya_fintech.db"
    
    # Global Wallet & Commission Rules
    DEFAULT_COMMISSION_PCT: float = 10.0  # 10% performance fee on realized net profit
    MIN_SECURITY_FUEL_INR: float = 1000.0
    MIN_SECURITY_FUEL_USDT: float = 15.0
    
    # Master Treasury Wallets (Where user's 10% automatically lands)
    MASTER_UPI_ID: str = "pushkar.quant@upi"
    MASTER_USDT_TRC20: str = "TYz7Q8kRamyaQuantGlobalOfficialTRC20Vault99"
    MASTER_USDT_POLYGON: str = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
    
    # Telegram Bot Settings (For live broadcast)
    TELEGRAM_BOT_TOKEN: str = "7189234812:AAHramyaQuantLiveBroadcasterToken"
    TELEGRAM_PUBLIC_CHANNEL: str = "@RamyaQuantVerifiedResults"
    TELEGRAM_ADMIN_CHAT_ID: str = "1098273645"

    class Config:
        env_file = ".env"
        extra = "allow"

settings = PlatformSettings()
