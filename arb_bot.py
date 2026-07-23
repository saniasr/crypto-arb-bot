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

# FAKE ALERT PROTECTION:
# Same ticker (e.g. "AI/USDT") kabhi-kabhi 2 alag exchanges pe bilkul
# alag token hota hai - isse fake bade gaps dikhte hain. Isse rokne ke liye:

# Symbol tabhi compare hoga jab kam se kam itne exchanges pe mile
# (2 se badha kar 3 kiya - "coincidence" wale fake matches kam honge)
MIN_EXCHANGES_FOR_SYMBOL = 2

# Agar kisi exchange ka price baaki sabke median se itna % zyada door hai,
# to use "wrong/different token" maan ke us exchange ko is symbol ke liye
# is cycle me ignore kar do (genuine cross-exchange gap itna bada nahi hota)
OUTLIER_DEVIATION_PERCENT = 25

# Isse zyada gap sanity-fail maana jayega (bahut zyada chance hai ki ye
# symbol-mismatch ya stale data hai, genuine arbitrage nahi)
MAX_SANE_GAP_PERCENT = 15

# Purane 11 (binance/kucoin/okx/bybit hata diye the) + naye 7 jo ccxt me
# support karte hain (baaki 9 naye wale ccxt library me available nahi the)
CEX_LIST = [
    "gate", "mexc", "htx", "bitget", "kraken",
    "coinbase", "bingx", "poloniex", "lbank", "bitmart",
    "coinex",
    "blofin", "deepcoin", "digifinex", "phemex", "toobit",
    "weex", "xt",
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

def is_valid_quote(bid, ask):
    """
    Bid kabhi ask se zyada nahi ho sakta (crossed book) - agar aisा data
    aaye to exchange API ne galat/reversed fields diye hain, use reject karo.
    Chhoti si tolerance rakhi hai float rounding ke liye.
    """
    if bid <= 0 or ask <= 0:
        return False
    if bid > ask * 1.002:  # 0.2% tolerance
        return False
    return True


def init_exchanges():
    exchanges = {}
    for ex_id in CEX_LIST:
        try:
            klass = getattr(ccxt, ex_id)
            exchanges[ex_id] = klass({
                "enableRateLimit": True,
                "timeout": 20000,
                "options": {"defaultType": "spot"},  # futures/perp price mat lo
            })
        except Exception as e:
            print(f"[Init skip] {ex_id}: {e}")
    return exchanges


def fetch_all_tickers(ex_id, exchange):
    """
    Ek hi bulk call me exchange ke saare tickers le leta hai (fast, rate-limit
    friendly), sirf SPOT market ke USDT pairs filter karta hai (futures/perp/
    margin skip - unke prices spot se kaafi alag hote hain aur galat match
    ho sakte hain), aur illiquid wale hata deta hai.
    Returns: {symbol: {"bid": x, "ask": y}}
    """
    result = {}
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"[Market load failed] {ex_id}: {e}")
        return result

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"[Ticker fetch failed] {ex_id}: {e}")
        return result

    for symbol, t in tickers.items():
        if not symbol.endswith(f"/{QUOTE_CURRENCY}"):
            continue

        # Sirf spot aur ACTIVE market - futures/perpetual/margin aur
        # delisted/inactive tokens dono skip karo
        market_info = markets.get(symbol)
        if not market_info or not market_info.get("spot", False):
            continue
        if market_info.get("active") is False:
            continue

        # Agar ticker ka timestamp bahut purana hai (delisted/frozen market
        # ka sign), to skip kar do
        ts = t.get("timestamp")
        if ts:
            age_seconds = time.time() - (ts / 1000)
            if age_seconds > 600:  # 10 min se purana price
                continue

        bid = t.get("bid")
        ask = t.get("ask")
        last = t.get("last") or t.get("close")
        # bid/ask na mile to last price hi dono ke liye use karo (fallback)
        bid = float(bid) if bid else (float(last) if last else 0)
        ask = float(ask) if ask else (float(last) if last else 0)
        vol = t.get("quoteVolume") or 0
        if is_valid_quote(bid, ask) and vol and vol >= MIN_24H_VOLUME_USD:
            result[symbol] = {"bid": bid, "ask": ask}

    return result


# ============================================================
# MANUAL EXCHANGE INTEGRATIONS
# (ccxt inhe support nahi karta, isliye REST API directly use kar rahe hain)
# ============================================================

def fetch_biconomy_tickers():
    """Biconomy.com - public endpoint, no auth needed."""
    result = {}
    url = "https://api.biconomy.com/api/v1/tickers"
    headers = {"X-SITE-ID": "127"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"[Biconomy HTTP {resp.status_code}] {resp.text[:150]}")
            return result
        data = resp.json()
        for t in data.get("ticker", []):
            raw_symbol = t.get("symbol", "")  # e.g. "BTC_USDT"
            if not raw_symbol.endswith(f"_{QUOTE_CURRENCY}"):
                continue
            symbol = raw_symbol.replace("_", "/")  # -> "BTC/USDT"
            bid = float(t.get("buy", 0) or 0)
            ask = float(t.get("sell", 0) or 0)
            last = float(t.get("last", 0) or 0)
            vol_base = float(t.get("vol", 0) or 0)
            vol_usd = vol_base * (last or bid or ask)  # rough USD estimate
            if is_valid_quote(bid, ask) and vol_usd >= MIN_24H_VOLUME_USD:
                result[symbol] = {"bid": bid, "ask": ask}
    except Exception as e:
        print(f"[Biconomy fetch failed] {e}")
    return result


def fetch_coinstore_tickers():
    """Coinstore - public endpoint, no auth needed."""
    result = {}
    url = "https://api.coinstore.com/api/v1/market/tickers"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"[Coinstore HTTP {resp.status_code}] {resp.text[:150]}")
            return result
        data = resp.json()
        for t in data.get("data", []):
            raw_symbol = t.get("symbol", "")  # e.g. "TRXUSDT"
            if not raw_symbol.endswith(QUOTE_CURRENCY):
                continue
            base = raw_symbol[: -len(QUOTE_CURRENCY)]
            symbol = f"{base}/{QUOTE_CURRENCY}"
            bid = float(t.get("bid", 0) or 0)
            ask = float(t.get("ask", 0) or 0)
            vol_usd = float(t.get("volume", 0) or 0)  # already in quote currency
            if is_valid_quote(bid, ask) and vol_usd >= MIN_24H_VOLUME_USD:
                result[symbol] = {"bid": bid, "ask": ask}
    except Exception as e:
        print(f"[Coinstore fetch failed] {e}")
    return result


def fetch_btse_tickers():
    """BTSE - public endpoint, no auth needed. Fields confirmed from official docs."""
    result = {}
    url = "https://api.btse.com/spot/api/v3.2/market_summary"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"[BTSE HTTP {resp.status_code}] {resp.text[:150]}")
            return result
        data = resp.json()
        for t in data if isinstance(data, list) else []:
            raw_symbol = t.get("symbol", "")  # e.g. "BTC-USD" or "BTC-USDT"
            if not raw_symbol.endswith(f"-{QUOTE_CURRENCY}"):
                continue
            # Futures/perpetual markets same list me aate hain - skip karo
            if t.get("futures") is True:
                continue
            if t.get("isMarketOpenToSpot") is False:
                continue
            if t.get("active") is False:
                continue
            base = raw_symbol[: -(len(QUOTE_CURRENCY) + 1)]
            symbol = f"{base}/{QUOTE_CURRENCY}"
            last = float(t.get("last", 0) or 0)
            bid = float(t.get("highestBid", 0) or 0) or last
            ask = float(t.get("lowestAsk", 0) or 0) or last
            vol_usd = float(t.get("volume", 0) or 0)
            if is_valid_quote(bid, ask) and vol_usd >= MIN_24H_VOLUME_USD:
                result[symbol] = {"bid": bid, "ask": ask}
    except Exception as e:
        print(f"[BTSE fetch failed] {e}")
    return result


MANUAL_EXCHANGES = {
    "biconomy": fetch_biconomy_tickers,
    "coinstore": fetch_coinstore_tickers,
    "btse": fetch_btse_tickers,
}


# ============================================================
# ARBITRAGE LOGIC
# ============================================================

def remove_outlier_prices(quotes_by_exchange):
    """
    quotes_by_exchange = {exchange_id: {"bid": x, "ask": y}}
    Same symbol alag exchanges pe alag actual token ho sakta hai (ticker clash)
    ya kisi exchange ka price stale ho sakta hai. Har exchange ka mid-price
    (bid+ask)/2 nikaal ke, median se bahut door wale exchanges ko is symbol
    ke liye is cycle me ignore kar dete hain.
    """
    if len(quotes_by_exchange) < 3:
        return quotes_by_exchange  # outlier detection ke liye kaafi data nahi

    mids = {ex_id: (q["bid"] + q["ask"]) / 2 for ex_id, q in quotes_by_exchange.items()}
    sorted_mids = sorted(mids.values())
    n = len(sorted_mids)
    median = sorted_mids[n // 2] if n % 2 == 1 else (sorted_mids[n // 2 - 1] + sorted_mids[n // 2]) / 2

    cleaned = {}
    for ex_id, mid in mids.items():
        if median <= 0:
            continue
        deviation = abs(mid - median) / median * 100
        if deviation <= OUTLIER_DEVIATION_PERCENT:
            cleaned[ex_id] = quotes_by_exchange[ex_id]
    return cleaned


def find_gaps_for_symbol(quotes_by_exchange, threshold_percent):
    """
    quotes_by_exchange = {exchange_id: {"bid": x, "ask": y}} for ONE symbol.
    Real arbitrage profit ke hisaab se calculate karta hai: ek exchange pe
    ASK price par khareedo, dusre pe BID price par becho - last/market price
    se nahi, balki actually tradeable prices se.
    """
    gaps = []
    for ex_a, ex_b in combinations(quotes_by_exchange.keys(), 2):
        q_a, q_b = quotes_by_exchange[ex_a], quotes_by_exchange[ex_b]

        # Direction 1: A pe khareedo (ask), B pe becho (bid)
        if q_a["ask"] > 0 and q_b["bid"] > 0:
            gap_percent = (q_b["bid"] - q_a["ask"]) / q_a["ask"] * 100
            if 0 < gap_percent <= MAX_SANE_GAP_PERCENT and gap_percent >= threshold_percent:
                gaps.append({
                    "buy_from": ex_a, "buy_price": q_a["ask"],
                    "sell_at": ex_b, "sell_price": q_b["bid"],
                    "gap_percent": gap_percent,
                })

        # Direction 2: B pe khareedo (ask), A pe becho (bid)
        if q_b["ask"] > 0 and q_a["bid"] > 0:
            gap_percent = (q_a["bid"] - q_b["ask"]) / q_b["ask"] * 100
            if 0 < gap_percent <= MAX_SANE_GAP_PERCENT and gap_percent >= threshold_percent:
                gaps.append({
                    "buy_from": ex_b, "buy_price": q_b["ask"],
                    "sell_at": ex_a, "sell_price": q_a["bid"],
                    "gap_percent": gap_percent,
                })

    return gaps


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Arbitrage bot starting (ALL TOKENS mode)...")
    exchanges = init_exchanges()
    print(f"{len(exchanges)} CEX exchanges loaded (via ccxt): {list(exchanges.keys())}")
    print(f"{len(MANUAL_EXCHANGES)} manual exchanges loaded: {list(MANUAL_EXCHANGES.keys())}")

    last_alert_time = {}  # (symbol, buy_ex, sell_ex) -> timestamp

    while True:
        cycle_start = time.time()

        # Step 1: har exchange se saare tickers bulk me le lo
        price_map = {}  # {symbol: {exchange_id: price}}
        for ex_id, ex in exchanges.items():
            tickers = fetch_all_tickers(ex_id, ex)
            for symbol, price in tickers.items():
                price_map.setdefault(symbol, {})[ex_id] = price

        # Step 1b: manual exchanges (ccxt me support nahi karte)
        for ex_id, fetch_fn in MANUAL_EXCHANGES.items():
            tickers = fetch_fn()
            for symbol, price in tickers.items():
                price_map.setdefault(symbol, {})[ex_id] = price

        total_symbols = len(price_map)
        comparable_symbols = sum(1 for v in price_map.values() if len(v) >= MIN_EXCHANGES_FOR_SYMBOL)
        print(f"[Cycle] {total_symbols} unique symbols found, {comparable_symbols} present on {MIN_EXCHANGES_FOR_SYMBOL}+ exchanges")

        # Step 2: har symbol ke liye gaps nikalo (outlier/mismatched prices hata kar)
        all_alerts = []
        for symbol, prices_by_exchange in price_map.items():
            if len(prices_by_exchange) < MIN_EXCHANGES_FOR_SYMBOL:
                continue
            cleaned_prices = remove_outlier_prices(prices_by_exchange)
            if len(cleaned_prices) < 2:
                continue
            gaps = find_gaps_for_symbol(cleaned_prices, GAP_THRESHOLD_PERCENT)
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
