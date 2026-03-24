#!/usr/bin/env python3
"""Uniswap V3 LP range calculator centered on SMA."""

import argparse
import sys
from math import sqrt

import requests
from web3 import Web3

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEFAULT_RPC = "https://arb1.arbitrum.io/rpc"

# CoinGecko mapping: pair name -> (coin_id, vs_currency)
PAIRS = {
    "WBTC/ETH": {"coin_id": "bitcoin", "vs_currency": "eth"},
    "ETH/USDC": {"coin_id": "ethereum", "vs_currency": "usd"},
    "WBTC/USDC": {"coin_id": "bitcoin", "vs_currency": "usd"},
}

# Arbitrum One token config
TOKEN_CONFIG = {
    "WBTC": {
        "address": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        "decimals": 8,
    },
    "WETH": {
        "address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "decimals": 18,
    },
    "ETH": {
        "address": None,  # native ETH — use w3.eth.get_balance()
        "decimals": 18,
    },
    "USDC": {
        "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "decimals": 6,
    },
}

ERC20_BALANCE_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

# ---------------------------------------------------------------------------
# Price data
# ---------------------------------------------------------------------------


def fetch_current_price(coin_id: str, vs_currency: str) -> float:
    url = f"{COINGECKO_BASE}/simple/price"
    params = {"ids": coin_id, "vs_currencies": vs_currency}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return float(data[coin_id][vs_currency])


def fetch_historical_prices(coin_id: str, vs_currency: str, days: int) -> list[float]:
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart"
    params = {"vs_currency": vs_currency, "days": days, "interval": "daily"}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return [p[1] for p in data["prices"]]


def calculate_sma(prices: list[float], period: int) -> float:
    if len(prices) < period:
        print(
            f"Warning: only {len(prices)} data points available, "
            f"using all for SMA (requested {period})",
            file=sys.stderr,
        )
        period = len(prices)
    return sum(prices[-period:]) / period


# ---------------------------------------------------------------------------
# Uniswap V3 concentrated liquidity math
# ---------------------------------------------------------------------------


def compute_token_amounts(P: float, Pa: float, Pb: float):
    """Return (amount0, amount1) for L=1. Only the ratio matters."""
    if P <= Pa:
        return (1 / sqrt(Pa) - 1 / sqrt(Pb), 0.0)
    if P >= Pb:
        return (0.0, sqrt(Pb) - sqrt(Pa))
    return (1 / sqrt(P) - 1 / sqrt(Pb), sqrt(P) - sqrt(Pa))


def compute_value_split(P: float, Pa: float, Pb: float):
    """Return (pct_token0, pct_token1) as value percentages."""
    a0, a1 = compute_token_amounts(P, Pa, Pb)
    val0 = a0 * P  # token0 value in token1 terms
    val1 = a1
    total = val0 + val1
    if total == 0:
        return (0.0, 0.0)
    return (val0 / total * 100, val1 / total * 100)


def compute_position_sizing(total_value_token1: float, P: float, Pa: float, Pb: float):
    """Given total portfolio value in token1, return (needed_token0, needed_token1)."""
    a0, a1 = compute_token_amounts(P, Pa, Pb)
    val0 = a0 * P
    val1 = a1
    total = val0 + val1
    if total == 0:
        return (0.0, 0.0)
    scale = total_value_token1 / total
    return (a0 * scale, a1 * scale)


# ---------------------------------------------------------------------------
# Wallet balance queries
# ---------------------------------------------------------------------------


def get_web3(rpc_url: str) -> Web3:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        print(f"Error: cannot connect to RPC at {rpc_url}", file=sys.stderr)
        sys.exit(1)
    return w3


def get_token_balance(w3: Web3, token_symbol: str, wallet: str) -> float:
    cfg = TOKEN_CONFIG[token_symbol]
    addr = w3.to_checksum_address(wallet)
    if cfg["address"] is None:
        raw = w3.eth.get_balance(addr)
    else:
        contract = w3.eth.contract(
            address=w3.to_checksum_address(cfg["address"]),
            abi=ERC20_BALANCE_ABI,
        )
        raw = contract.functions.balanceOf(addr).call()
    return raw / (10 ** cfg["decimals"])


def get_wallet_balances(w3: Web3, token0_sym: str, token1_sym: str, wallet: str):
    """Return (balance_token0, balance_token1).

    For pairs where token1 is ETH, combine native ETH + WETH.
    """
    bal0 = get_token_balance(w3, token0_sym, wallet)

    if token1_sym == "ETH":
        native = get_token_balance(w3, "ETH", wallet)
        wrapped = get_token_balance(w3, "WETH", wallet)
        bal1 = native + wrapped
    else:
        bal1 = get_token_balance(w3, token1_sym, wallet)

    return bal0, bal1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description="Uniswap V3 LP range calculator centered on SMA."
    )
    p.add_argument("--pair", default="WBTC/ETH", help="Token pair (default: WBTC/ETH)")
    p.add_argument("--sma-period", type=int, default=20, help="SMA period in days (default: 20)")
    p.add_argument("--width", type=float, default=3.0, help="Symmetric %% above/below SMA (default: 3.0)")
    p.add_argument("--wallet", default=None, help="Wallet address for balance check")
    p.add_argument("--rpc-url", default=DEFAULT_RPC, help="Arbitrum RPC URL")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    args = parse_args()
    pair = args.pair.upper()

    if pair not in PAIRS:
        print(f"Error: unsupported pair '{pair}'. Supported: {', '.join(PAIRS)}", file=sys.stderr)
        sys.exit(1)

    token0_sym, token1_sym = pair.split("/")
    coin_id = PAIRS[pair]["coin_id"]
    vs_currency = PAIRS[pair]["vs_currency"]

    # --- Fetch prices ---
    try:
        current_price = fetch_current_price(coin_id, vs_currency)
        historical = fetch_historical_prices(coin_id, vs_currency, args.sma_period)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching price data: {e}", file=sys.stderr)
        sys.exit(1)

    sma = calculate_sma(historical, args.sma_period)
    price_vs_sma = (current_price - sma) / sma * 100

    # --- Compute range ---
    Pa = sma * (1 - args.width / 100)
    Pb = sma * (1 + args.width / 100)

    lower_vs_current = (Pa - current_price) / current_price * 100
    upper_vs_current = (Pb - current_price) / current_price * 100

    pct0, pct1 = compute_value_split(current_price, Pa, Pb)

    # --- Determine token1 display label ---
    t1_label = "ETH" if token1_sym == "ETH" else token1_sym

    # --- Output ---
    print()
    print("=== SMA LP Range Calculator ===")
    print()
    print(f"  Pair:           {pair}")
    print(f"  SMA Period:     {args.sma_period} days")
    print(f"  SMA Price:      {sma:.6f} {t1_label}")
    print(f"  Current Price:  {current_price:.6f} {t1_label}")
    print(f"  Price vs SMA:   {price_vs_sma:+.2f}%")
    print()
    print(f"=== LP Range (centered on SMA +/- {args.width:.2f}%) ===")
    print()
    print(f"  Lower Bound:    {Pa:.6f} {t1_label}  (SMA - {args.width:.2f}%)")
    print(f"  Upper Bound:    {Pb:.6f} {t1_label}  (SMA + {args.width:.2f}%)")
    print()
    print("  vs Current Price:")
    print(f"    Lower Bound:  {lower_vs_current:+.2f}% from current")
    print(f"    Upper Bound:  {upper_vs_current:+.2f}% from current")
    print()

    if current_price <= Pa:
        print(f"  Current price is BELOW the range (100% {token0_sym}).")
    elif current_price >= Pb:
        print(f"  Current price is ABOVE the range (100% {token1_sym}).")
    else:
        print("  Current price is INSIDE the range.")

    print()
    print("=== Required Token Split ===")
    print()
    print(f"  {token0_sym}: {pct0:.1f}% of position value")
    print(f"  {token1_sym}: {pct1:.1f}% of position value")

    # --- Wallet section ---
    if args.wallet:
        if not Web3.is_address(args.wallet):
            print(f"\nError: invalid wallet address '{args.wallet}'", file=sys.stderr)
            sys.exit(1)

        try:
            w3 = get_web3(args.rpc_url)
            bal0, bal1 = get_wallet_balances(w3, token0_sym, token1_sym, args.wallet)
        except Exception as e:
            print(f"\nError querying wallet: {e}", file=sys.stderr)
            sys.exit(1)

        total_value = bal0 * current_price + bal1
        needed0, needed1 = compute_position_sizing(total_value, current_price, Pa, Pb)
        diff0 = needed0 - bal0
        diff1 = needed1 - bal1

        print()
        print("=== Wallet Balances ===")
        print()
        print(f"  {token0_sym} held:     {bal0:.8f}")
        print(f"  {token1_sym} held:     {bal1:.8f}")
        print(f"  Total value:   {total_value:.6f} {t1_label}")
        print()
        print("=== Position Sizing ===")
        print()
        print(f"  {token0_sym} needed:   {needed0:.8f}  ({needed0 * current_price:.6f} {t1_label} worth)")
        print(f"  {token1_sym} needed:   {needed1:.8f}")
        print()

        if abs(diff0) < 1e-12 and abs(diff1) < 1e-12:
            print("  Wallet is already balanced for this position.")
        elif diff0 > 0:
            # Need more token0, sell token1
            eth_to_sell = diff0 * current_price
            print(f"  Swap: Buy {diff0:.8f} {token0_sym} with ~{eth_to_sell:.6f} {t1_label}")
        else:
            # Need more token1, sell token0
            token0_to_sell = -diff0
            eth_to_receive = token0_to_sell * current_price
            print(f"  Swap: Sell {token0_to_sell:.8f} {token0_sym} for ~{eth_to_receive:.6f} {t1_label}")

    print()


if __name__ == "__main__":
    main()
