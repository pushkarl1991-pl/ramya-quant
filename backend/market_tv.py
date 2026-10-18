"""
RAMYA GLOBAL FINTECH - LIVE FINANCIAL TV STREAM RESOLVER
Resolves and caches real-time active YouTube live stream embed IDs for:
Zee Business, CNBC Awaaz, NDTV Profit, ET Now Swadesh, Aaj Tak, and CoinDesk Crypto.
"""

import time
import urllib.request
import re
import logging
from typing import Dict, Any, List

logger = logging.getLogger("ramya.tv_resolver")

# Verified stable fallback IDs (checked 2026-10-18)
CHANNELS_CONFIG = {
    "ZEE_BUSINESS": {
        "id": "ZEE_BUSINESS",
        "name": "Zee Business (Hindi Market Live)",
        "category": "Indian NSE / F&O / MCX",
        "handle": "@ZeeBusiness",
        "default_video_id": "kh2-jlF5ij4",
        "badge": "🔴 LIVE HINDI"
    },
    "CNBC_AWAAZ": {
        "id": "CNBC_AWAAZ",
        "name": "CNBC Awaaz (Hindi Live)",
        "category": "Equities, Nifty & Gold",
        "handle": "@CNBCAwaaz",
        "default_video_id": "q0JuuvfPhrA",
        "badge": "🔴 LIVE HINDI"
    },
    "NDTV_PROFIT": {
        "id": "NDTV_PROFIT",
        "name": "NDTV Profit Live",
        "category": "Institutional Stock Analysis",
        "handle": "@NDTVProfitIndia",
        "default_video_id": "xc-wG6C8Lr4",
        "badge": "🔴 LIVE"
    },
    "ET_NOW_SWADESH": {
        "id": "ET_NOW_SWADESH",
        "name": "ET Now Swadesh (Hindi Live)",
        "category": "Market Opening & Stock Picks",
        "handle": "@ETNowSwadesh",
        "default_video_id": "53WMt0pDzVQ",
        "badge": "🔴 LIVE HINDI"
    },
    "AAJ_TAK": {
        "id": "AAJ_TAK",
        "name": "Aaj Tak Live (Business & Markets)",
        "category": "Macro Economy & Commodity",
        "handle": "@aajtak",
        "default_video_id": "A1-UUD2fFP4",
        "badge": "🔴 LIVE 24/7"
    },
    "COINDESK_TV": {
        "id": "COINDESK_TV",
        "name": "CoinDesk Crypto 24/7 TV",
        "category": "Crypto Derivatives & Bitcoin",
        "handle": "@CoinDesk",
        "default_video_id": "SUzzPdBet10",
        "badge": "🪙 CRYPTO 24/7"
    }
}

_tv_cache: Dict[str, Any] = {}
_last_fetch_ts: float = 0.0
CACHE_TTL = 900.0  # 15 minutes

def resolve_live_video_id(handle: str, fallback: str) -> str:
    """Attempts to find the latest active video ID for the given channel handle."""
    url = f"https://www.youtube.com/{handle}/live"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            }
        )
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            final_url = resp.geturl()
            # If redirected to watch?v=XYZ
            m_url = re.search(r"[?&]v=([a-zA-Z0-9_-]{11})", final_url)
            if m_url:
                return m_url.group(1)
            
            html = resp.read().decode("utf-8", errors="ignore")
            m_vid = re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', html)
            if m_vid:
                return m_vid.group(1)
    except Exception as e:
        logger.debug(f"TV stream scrape failed for {handle}: {e}")
    return fallback

def get_live_tv_channels() -> List[Dict[str, Any]]:
    """Returns all channels with fresh, active live video IDs and embed URLs."""
    global _tv_cache, _last_fetch_ts
    now = time.time()
    
    if _tv_cache and (now - _last_fetch_ts < CACHE_TTL):
        return list(_tv_cache.values())

    out: Dict[str, Any] = {}
    for cid, conf in CHANNELS_CONFIG.items():
        vid = resolve_live_video_id(conf["handle"], conf["default_video_id"])
        out[cid] = {
            "id": conf["id"],
            "name": conf["name"],
            "category": conf["category"],
            "video_id": vid,
            "embed_url": f"https://www.youtube.com/embed/{vid}?autoplay=1&mute=1&playsinline=1",
            "direct_url": f"https://www.youtube.com/{conf['handle']}/live",
            "badge": conf["badge"]
        }

    _tv_cache = out
    _last_fetch_ts = now
    return list(_tv_cache.values())
