"""
Crypto Arbitrage Alert Bot (ALL TOKENS VERSION)
=================================================
15 CEX exchanges ke SAARE USDT-pair tokens automatically scan karta hai
(fixed symbol list nahi) aur jab bhi kisi token ka price 2+ exchanges ke
beech threshold se zyada differ kare, Telegram alert bhejta hai.

SETUP: same as before -> pip install ccxt requests
TELEGRAM_BOT_TOKEN aur TELEGRAM_CHAT_ID env variables se aate hain.
"""

import os
import ccxt
import requests
import time
import traceback
from itertools import combinations

# ============================================================
# CONFIG
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# Kitna % gap hone par alert bheje
GAP_THRESHOLD_PERCENT = 0.5

# Kitne second baad recheck kare (all-tokens scan thoda time leta hai,
# isliye 5 min rakha hai - kam mat karo warna exchanges rate-limit kar denge)
CHECK_INTERVAL_SECONDS = 300

# Same alert baar baar na aaye isliye cooldown (seconds)
ALERT_COOLDOWN_SECONDS = 900

# Sirf isi quote currency ke pairs compare karo (apples-to-apples rahe)
QUOTE_CURRENCY = "USDT"

# Illiquid/dead tokens ka fake gap na aaye isliye minimum 24h volume (USD)
# har exchange par har side. Isse Curve jaisa "stale price = fake 1% gap"
# wala issue nahi aayega.
MIN_24H_VOLUME_USD = 20_000

# Ek cycle me kitne top gaps Telegram pe bhejne hain (spam avoid karne ke liye)
MAX_ALERTS_PER_CYCLE = 15

# User-selected CEX list (ccxt IDs) - jo bhi exchange ccxt me available
# nahi hoga wo automatically skip ho jayega (logs me "[Init skip]" dikhega)
CEX_LIST = [
    "biconomy", "bifinance", "blofin", "blocking", "btse",
    "coinstore", "deepcoin", "digifinex", "hotcoin", "ourbit",
    "kcex", "phemex", "pionex", "toobit", "weex", "xt",
]

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
# EXCHANGE SETUP
# ============================================================

def init_exchanges():
    exchanges = {}
    for ex_id in CEX_LIST:
        try:
            klass = getattr(ccxt, ex_id)
            exchanges[ex_id] = klass({"enableRateLimit": True, "timeout": 20000})
        except Exception as e:
            print(f"[Init skip] {ex_id}: {e}")
    return exchanges


def fetch_all_tickers(ex_id, exchange):
    """
    Ek hi bulk call me exchange ke saare tickers le leta hai (fast, rate-limit
    friendly), USDT pairs filter karta hai, aur illiquid wale hata deta hai.
    Returns: {symbol: price}
    """
    result = {}
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"[Ticker fetch failed] {ex_id}: {e}")
        return result

    for symbol, t in tickers.items():
        if not symbol.endswith(f"/{QUOTE_CURRENCY}"):
            continue
        last = t.get("last") or t.get("close")
        vol = t.get("quoteVolume") or 0
        if last and last > 0 and vol and vol >= MIN_24H_VOLUME_USD:
            result[symbol] = float(last)

    return result


# ============================================================
# ARBITRAGE LOGIC
# ============================================================

def find_gaps_for_symbol(prices_by_exchange, threshold_percent):
    """prices_by_exchange = {exchange_id: price} for ONE symbol."""
    gaps = []
    for (ex_a, price_a), (ex_b, price_b) in combinations(prices_by_exchange.items(), 2):
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
    print("Arbitrage bot starting (ALL TOKENS mode)...")
    exchanges = init_exchanges()
    print(f"{len(exchanges)} CEX exchanges loaded: {list(exchanges.keys())}")

    last_alert_time = {}  # (symbol, buy_ex, sell_ex) -> timestamp

    while True:
        cycle_start = time.time()

        # Step 1: har exchange se saare tickers bulk me le lo
        price_map = {}  # {symbol: {exchange_id: price}}
        for ex_id, ex in exchanges.items():
            tickers = fetch_all_tickers(ex_id, ex)
            for symbol, price in tickers.items():
                price_map.setdefault(symbol, {})[ex_id] = price

        total_symbols = len(price_map)
        comparable_symbols = sum(1 for v in price_map.values() if len(v) >= 2)
        print(f"[Cycle] {total_symbols} unique symbols found, {comparable_symbols} present on 2+ exchanges")

        # Step 2: har symbol ke liye gaps nikalo
        all_alerts = []
        for symbol, prices_by_exchange in price_map.items():
            if len(prices_by_exchange) < 2:
                continue
            gaps = find_gaps_for_symbol(prices_by_exchange, GAP_THRESHOLD_PERCENT)
            for gap in gaps:
                gap["symbol"] = symbol
                all_alerts.append(gap)

        # Sabse bade gap wale pehle bhejo
        all_alerts.sort(key=lambda g: -g["gap_percent"])
        print(f"[Cycle] {len(all_alerts)} gap(s) found above {GAP_THRESHOLD_PERCENT}% threshold")

        sent_count = 0
        for gap in all_alerts:
            if sent_count >= MAX_ALERTS_PER_CYCLE:
                break

            key = (gap["symbol"], gap["buy_from"], gap["sell_at"])
            now = time.time()
            if key in last_alert_time and (now - last_alert_time[key]) < ALERT_COOLDOWN_SECONDS:
                continue  # cooldown active

            last_alert_time[key] = now
            msg = (
                f"🚨 <b>Arbitrage Alert: {gap['symbol']}</b>\n\n"
                f"Buy on: <b>{gap['buy_from']}</b> @ {gap['buy_price']:.6f}\n"
                f"Sell on: <b>{gap['sell_at']}</b> @ {gap['sell_price']:.6f}\n"
                f"Gap: <b>{gap['gap_percent']:.2f}%</b>"
            )
            print(msg.replace("\n", " | "))
            send_telegram_alert(msg)
            sent_count += 1

        elapsed = time.time() - cycle_start
        print(f"[Cycle] done in {elapsed:.1f}s, {sent_count} alert(s) sent")

        sleep_time = max(5, CHECK_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
