"""
Crypto Arbitrage Alert Bot
===========================
20 CEX + DEX exchanges track karke, jab bhi kisi 2 exchanges ke beech
arbitrage gap (price difference %) threshold se zyada ho, Telegram par
alert bhejta hai.

SETUP:
1. pip install ccxt requests
2. Neeche CONFIG section me apna TELEGRAM_BOT_TOKEN aur TELEGRAM_CHAT_ID daalo
3. python arb_bot.py

Telegram bot kaise banaye:
- @BotFather ko /newbot bhejo, token milega
- Apna chat_id nikalne ke liye @userinfobot ko /start bhejo (ya group me bot add karke getUpdates check karo)
"""

import os
import ccxt
import requests
import time
import traceback
from itertools import combinations

# ============================================================
# CONFIG - token/chat_id ab environment variables se aayenge
# (Railway ki "Variables" settings me set karna hai)
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# Kitna % gap hone par alert bheje
GAP_THRESHOLD_PERCENT = 0.05

# Kaunse symbols track karne hain
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]

# Kitne second baad recheck kare
CHECK_INTERVAL_SECONDS = 60

# Same alert baar baar na aaye isliye cooldown (seconds)
ALERT_COOLDOWN_SECONDS = 600

# 15 CEX (ccxt IDs) - inme se jo bhi tumhare region me kaam kare
CEX_LIST = [
    "binance", "kucoin", "okx", "bybit", "gate",
    "mexc", "htx", "bitget", "kraken", "coinbase",
    "bingx", "poloniex", "lbank", "bitmart", "coinex",
]

# 5 DEX (DexScreener ke via cover honge - alag chains)
# format: symbol -> token contract address (chain ke hisaab se)
# DexScreener free API se best liquidity pair uthayenge
DEX_TOKEN_ADDRESSES = {
    "BTC/USDT": "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599",  # WBTC (ethereum)
    "ETH/USDT": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH (ethereum)
    "SOL/USDT": "So11111111111111111111111111111111111111112",  # SOL (solana)
    "BNB/USDT": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB (bsc)
    "XRP/USDT": None,  # DEX pair reliably available nahi - skip
}

# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_alert(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code != 200:
            print(f"[Telegram error] {resp.text}")
    except Exception as e:
        print(f"[Telegram send failed] {e}")


# ============================================================
# CEX PRICE FETCHING
# ============================================================

def init_exchanges():
    """Har CEX ka ccxt instance banata hai. Jo fail ho jaye usko skip karta hai."""
    exchanges = {}
    for ex_id in CEX_LIST:
        try:
            klass = getattr(ccxt, ex_id)
            exchanges[ex_id] = klass({"enableRateLimit": True, "timeout": 10000})
        except Exception as e:
            print(f"[Init skip] {ex_id}: {e}")
    return exchanges


def fetch_cex_prices(exchanges, symbol):
    """Ek symbol ke liye sab CEX se last price nikalta hai."""
    prices = {}
    for ex_id, ex in exchanges.items():
        try:
            if symbol not in ex.load_markets():
                continue
            ticker = ex.fetch_ticker(symbol)
            price = ticker.get("last") or ticker.get("close")
            if price:
                prices[ex_id] = float(price)
        except Exception:
            # exchange down / symbol not listed / rate limited -> skip silently
            continue
    return prices


# ============================================================
# DEX PRICE FETCHING (via DexScreener free API)
# ============================================================

def fetch_dex_price(symbol):
    """DexScreener se best-liquidity pair ka price nikalta hai."""
    token_address = DEX_TOKEN_ADDRESSES.get(symbol)
    if not token_address:
        return {}

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    headers = {"User-Agent": "Mozilla/5.0 (arb-bot)"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[DexScreener HTTP {resp.status_code}] {symbol}: {resp.text[:150]}")
            return {}
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return {}
        # sabse zyada liquidity wala pair lo (most reliable price)
        best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        dex_name = best_pair.get("dexId", "dex")
        price = float(best_pair.get("priceUsd", 0))
        if price > 0:
            return {f"dex:{dex_name}": price}
    except Exception as e:
        print(f"[DexScreener error] {symbol}: {e}")
    return {}


# ============================================================
# ARBITRAGE LOGIC
# ============================================================

def find_arbitrage_gaps(all_prices, threshold_percent):
    """all_prices = {exchange_name: price}. Har pair combination check karta hai."""
    gaps = []
    for (ex_a, price_a), (ex_b, price_b) in combinations(all_prices.items(), 2):
        if price_a <= 0 or price_b <= 0:
            continue
        gap_percent = abs(price_a - price_b) / min(price_a, price_b) * 100
        if gap_percent >= threshold_percent:
            low_ex, low_price = (ex_a, price_a) if price_a < price_b else (ex_b, price_b)
            high_ex, high_price = (ex_b, price_b) if price_a < price_b else (ex_a, price_a)
            gaps.append({
                "buy_from": low_ex, "buy_price": low_price,
                "sell_at": high_ex, "sell_price": high_price,
                "gap_percent": gap_percent,
            })
    return gaps


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Arbitrage bot starting...")
    exchanges = init_exchanges()
    print(f"{len(exchanges)} CEX exchanges loaded: {list(exchanges.keys())}")

    last_alert_time = {}  # (symbol, buy_ex, sell_ex) -> timestamp

    while True:
        for symbol in SYMBOLS:
            try:
                all_prices = {}
                all_prices.update(fetch_cex_prices(exchanges, symbol))
                all_prices.update(fetch_dex_price(symbol))

                if len(all_prices) < 2:
                    continue

                gaps = find_arbitrage_gaps(all_prices, GAP_THRESHOLD_PERCENT)

                for gap in sorted(gaps, key=lambda g: -g["gap_percent"]):
                    key = (symbol, gap["buy_from"], gap["sell_at"])
                    now = time.time()
                    if key in last_alert_time and (now - last_alert_time[key]) < ALERT_COOLDOWN_SECONDS:
                        continue  # cooldown active, skip repeat alert

                    last_alert_time[key] = now
                    msg = (
                        f"🚨 <b>Arbitrage Alert: {symbol}</b>\n\n"
                        f"Buy on: <b>{gap['buy_from']}</b> @ {gap['buy_price']:.4f}\n"
                        f"Sell on: <b>{gap['sell_at']}</b> @ {gap['sell_price']:.4f}\n"
                        f"Gap: <b>{gap['gap_percent']:.2f}%</b>"
                    )
                    print(msg.replace("\n", " | "))
                    send_telegram_alert(msg)

            except Exception:
                print(f"[Loop error - {symbol}]")
                traceback.print_exc()

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
