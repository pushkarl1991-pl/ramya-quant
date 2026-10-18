"""
RAMYA GLOBAL FINTECH - Universal Payment Gateways (UPI, Bank, & Web3 USDT)
Handles client deposits via Indian UPI QR, Direct Bank IMPS/NEFT, and Global USDT TRC20/Polygon.
"""

from typing import Dict, Any
from backend.config import settings

class UniversalPaymentGatewayManager:
    """Provides deposit addresses, UPI deep links, and payment verification."""

    @staticmethod
    def get_indian_deposit_options(amount_inr: float, client_id: int) -> Dict[str, Any]:
        """Generates dynamic UPI QR link and direct bank details for Indian clients."""
        import os, json
        cfg_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "payment_gateway_config.json")
        upi_id = settings.MASTER_UPI_ID
        payee_name = "Ramya Quant Global"
        bank_name = "HDFC Bank Ltd"
        bank_acc = "50200084729104"
        bank_ifsc = "HDFC0001245"
        qr_image = ""
        if os.path.exists(cfg_file):
            try:
                with open(cfg_file, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                    upi_id = cfg.get("upi_id") or upi_id
                    payee_name = cfg.get("payee_name") or payee_name
                    bank_name = cfg.get("bank_name") or bank_name
                    bank_acc = cfg.get("bank_account") or bank_acc
                    bank_ifsc = cfg.get("ifsc_code") or bank_ifsc
                    qr_image = cfg.get("qr_image_base64") or ""
            except Exception:
                pass

        import urllib.parse
        encoded_pn = urllib.parse.quote(payee_name)
        upi_string = (
            f"upi://pay?pa={upi_id}&pn={encoded_pn}&am={amount_inr:.2f}"
            f"&cu=INR&tn=RQG_Fuel_Client_{client_id}"
        )
        qr_url = qr_image if qr_image else f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={urllib.parse.quote(upi_string)}"
        return {
            "method": "UPI_INSTANT",
            "amount_inr": amount_inr,
            "master_upi_id": upi_id,
            "payee_name": payee_name,
            "upi_deep_link": upi_string,
            "qr_code_url": qr_url,
            "custom_qr_uploaded": bool(qr_image),
            "bank_transfer": {
                "account_name": payee_name,
                "bank_name": bank_name,
                "account_number": bank_acc,
                "ifsc_code": bank_ifsc,
                "account_type": "Current / Business Account"
            },
            "instructions": "Scan QR using PhonePe, Google Pay, or Paytm. Enter transaction UTR to auto-credit fuel instantly."
        }

    @staticmethod
    def get_global_crypto_deposit_options(amount_usdt: float, client_id: int) -> Dict[str, Any]:
        """Provides TRC20 and Polygon USDT deposit addresses for international clients."""
        return {
            "method": "CRYPTO_WEB3_USDT",
            "amount_usdt": amount_usdt,
            "supported_networks": [
                {
                    "network": "Tron (TRC20)",
                    "vault_address": settings.MASTER_USDT_TRC20,
                    "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={settings.MASTER_USDT_TRC20}",
                    "min_deposit": "10 USDT",
                    "speed": "2 minutes confirmation"
                },
                {
                    "network": "Polygon (USDT)",
                    "vault_address": settings.MASTER_USDT_POLYGON,
                    "qr_url": f"https://api.qrserver.com/v1/create-qr-code/?size=250x250&data={settings.MASTER_USDT_POLYGON}",
                    "min_deposit": "5 USDT",
                    "speed": "Fast (Low Gas Fee)"
                }
            ],
            "instructions": "Send exact USDT to the network address above. Funds are credited automatically upon 12 blockchain block confirmations."
        }
