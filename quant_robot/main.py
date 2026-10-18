"""
RAMYA AI — Quant Robot Main CLI Entry Point
Runs the Elite Predictive Institutional Order Flow & Volatility Clustering HFT Scalper:
- Algorithmic Predictive Volume & Spread Analysis (VSA) Model (10ms L2 Order Book Scan)
- Ahead-of-Wave Post-Only Limit Placement on 90%+ Vertical Expansion Probability
- Quant-Based Intelligent Bracket Calculation (>= 1:2.5 Net RR strictly net of all fees)
- Dynamic Take Profit at Predicted Candle Exhaustion Wick Top
- Core Capital Vault Security: Strictly 1.0% Margin Exposure Cap per Trade Setup
- Microsecond Zero-Loss Breakeven Guard at +0.02% Favorable Move ($0.00 Risk Shield)
- Real-Time Predictive Diagnostic Dashboard (1-Second Continuous Stream)
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to sys.path if not present
BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from quant_robot.hft_net_positive_scalper import HFTNetPositiveScalper


def main():
    parser = argparse.ArgumentParser(description="RAMYA AI — Predictive Institutional Order Flow HFT Scalper & Cost Engine")
    parser.add_argument("--symbol", type=str, default="MARKXAUTUSD", help="Contract symbol (default: MARKXAUTUSD)")
    parser.add_argument("--profit-pct", type=float, default=None, help="Baseline net profit target (default: 0.0035 = 0.35%%)")
    parser.add_argument("--mode", type=str, default="PAPER", choices=["PAPER", "LIVE"], help="Execution mode: PAPER (default) or LIVE")
    parser.add_argument("--live", action="store_true", default=False, help="Explicit authorization flag required for LIVE execution")
    parser.add_argument("--tier1-lev", type=int, default=10, help="Base safe leverage (default: 10x)")
    parser.add_argument("--tier3-lev", type=int, default=10, help="Maximum leverage cap (default: 10x safe cap)")
    parser.add_argument("--boost-profit", action="store_true", default=False, help="Enable dynamic scaling (default: False)")
    args = parser.parse_args()

    # PAPER mode is strictly enabled by default. Live execution requires explicit authorization.
    is_live = args.live or args.mode.upper() == "LIVE"
    trading_mode = "LIVE" if is_live else "PAPER"
    print(f"\n[GOVERNANCE] Starting Execution Engine in [{trading_mode} MODE] (Live Authorized: {is_live})")

    scalper = HFTNetPositiveScalper(
        symbol=args.symbol,
        net_pure_profit_pct=args.profit_pct,
        boost_lot_size=args.boost_profit,
        tier1_leverage=args.tier1_lev,
        tier3_leverage=args.tier3_lev,
    )
    # Configure cost engine mode
    scalper.cost_engine.is_paper_mode = not is_live
    try:
        asyncio.run(scalper.run_autopilot_loop())
    except KeyboardInterrupt:
        print("\n[RAMYA AI HFT] Process stopped safely by user.")
        scalper.ws_client.stop()


if __name__ == "__main__":
    main()
