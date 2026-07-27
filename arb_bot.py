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
GAP_THRESHOLD_PERCENT = 0.3

# Kitne second baad recheck kare (all-tokens scan thoda time leta hai,
# isliye 5 min rakha hai - kam mat karo warna exchanges rate-limit kar denge)
CHECK_INTERVAL_SECONDS = 240

# Same alert baar baar na aaye isliye cooldown (seconds)
ALERT_COOLDOWN_SECONDS = 300

# Sirf isi quote currency ke pairs compare karo (apples-to-apples rahe)
QUOTE_CURRENCY = "USDT"

# Illiquid/dead tokens ka fake gap na aaye isliye minimum 24h volume (USD)
# har exchange par har side. Isse Curve jaisa "stale price = fake 1% gap"
# wala issue nahi aayega.
MIN_24H_VOLUME_USD = 50_000

# Manual exchanges (Biconomy/Coinstore/BTSE) ka volume calculation kam accurate
# hota hai (estimate/approximation), isliye inke liye zyada strict threshold -
# taaki thin/stale listings pass na ho paayein
MANUAL_EXCHANGE_MIN_VOLUME_USD = 100_000

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

# REAL PROFIT CHECK:
# Raw price gap "profit" nahi hota - har trade pe fees lagte hain (buy pe
# taker fee + sell pe taker fee, dono exchanges pe). Zyadatar CEX ~0.1%
# taker fee lete hain, matlab round-trip ~0.4% (2 exchanges x 0.1% x 2 sides).
# Iska matlab agar gap isse kam hai, to wo "genuine" nahi hai - fees hi
# saara profit kha jayengi. Conservative estimate rakha hai.
ESTIMATED_ROUND_TRIP_FEE_PERCENT = 0.4

# Alert tabhi bhejo jab fees minus karne ke baad bhi itna % net profit bache
MIN_NET_PROFIT_PERCENT = 2.5

# Purane 11 (binance/kucoin/okx/bybit hata diye the) + naye 7 jo ccxt me
# support karte hain (baaki 9 naye wale ccxt library me available nahi the)
CEX_LIST = [
    "gate", "htx", "bitget", "kraken",
    "bingx", "poloniex", "lbank",
    "coinex",
    "blofin", "deepcoin", "digifinex", "phemex", "toobit",
    "weex", "xt",
    "whitebit", "bitrue", "exmo", "hitbtc",
    "p2b", "cryptocom", "bigone",
    "cex", "bydfi", "bitkan", "hashkey",
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

def safe_float(value, default=0.0):
    """Empty strings, None, ya garbage values crash na karein - 0 return karo."""
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


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


def load_currency_status(exchanges):
    """
    Har exchange se currencies ka deposit/withdraw enabled status ek baar
    fetch karke cache kar leta hai (baar-baar cycle me nahi - ye data
    rarely change hota hai). Network-level info bhi store karta hai (jaise
    ERC20, BSC, TRC20) - kyunki sirf "deposit: true" hone se kaam nahi
    chalta agar dono exchanges ka COMMON network match na kare.

    Returns: {exchange_id: {currency_code: {
        "deposit": bool, "withdraw": bool,
        "withdraw_networks": set(), "deposit_networks": set()
    }}}
    """
    status_cache = {}
    for ex_id, ex in exchanges.items():
        try:
            currencies = ex.fetch_currencies()
            if not currencies:
                continue
            status_cache[ex_id] = {}
            for code, info in currencies.items():
                deposit_ok = info.get("deposit")
                withdraw_ok = info.get("withdraw")

                networks = info.get("networks") or {}
                withdraw_networks = set()
                deposit_networks = set()
                for net_id, net_info in networks.items():
                    net_name = (net_info.get("network") or net_id or "").upper()
                    if not net_name:
                        continue
                    if net_info.get("withdraw") is not False:
                        withdraw_networks.add(net_name)
                    if net_info.get("deposit") is not False:
                        deposit_networks.add(net_name)

                status_cache[ex_id][code] = {
                    "deposit": deposit_ok if deposit_ok is not None else True,
                    "withdraw": withdraw_ok if withdraw_ok is not None else True,
                    "withdraw_networks": withdraw_networks,
                    "deposit_networks": deposit_networks,
                }
        except Exception as e:
            print(f"[Currency status unavailable] {ex_id}: {e}")
    return status_cache


# Ye exchanges ka deposit/withdraw data ccxt se milta hai, lekin proven
# unreliable hai (ESPORTS/USDT case me Toobit ne "sab theek hai" dikhaya
# jabki deposit actually band tha) - inke involved har alert me hamesha
# warning dikhega, chahe status data "clean" bhi kyun na lage
UNRELIABLE_STATUS_EXCHANGES = {"toobit"}


def check_transferable(status_cache, buy_ex, sell_ex, base_currency):
    """
    Check karta hai ki coin buy_ex se withdraw ho sakta hai aur sell_ex me
    deposit ho sakta hai - warna arbitrage execute nahi ho sakta, chahe
    price gap kitna bhi genuine kyun na ho.

    Network-level bhi check karta hai: agar dono exchanges ka network data
    available hai, to kam se kam EK common network hona chahiye jisse
    buy_ex se withdraw ho sake aur sell_ex me deposit ho sake. Alag-alag
    chain (jaise buy_ex sirf BSC support kare, sell_ex sirf ERC20 accept
    kare) ka matlab hai coin transfer hi nahi ho sakta - fake-looking
    "arbitrage" isi wajah se bhi bahut baar hota hai.

    Agar sell_ex (jahan DEPOSIT hoga) UNRELIABLE_STATUS_EXCHANGES list me hai
    (jaise Toobit - jiska deposit status baar baar galat sabit hua hai), to
    alert BLOCK kar dete hain. Withdraw side (buy_ex) allow hai, kyunki
    sirf deposit info unreliable paayi gayi thi, withdraw nahi.

    Returns: (can_send: bool, block_reason: str or None, unverified: bool)
    """
    if sell_ex in UNRELIABLE_STATUS_EXCHANGES:
        return False, f"{sell_ex} deposit status unreliable - blocked", False

    buy_info = status_cache.get(buy_ex, {}).get(base_currency)
    sell_info = status_cache.get(sell_ex, {}).get(base_currency)

    if buy_info and buy_info["withdraw"] is False:
        return False, f"withdraw disabled on {buy_ex}", False
    if sell_info and sell_info["deposit"] is False:
        return False, f"deposit disabled on {sell_ex}", False

    # Network-level match - dono taraf network data ho tabhi check karo
    if buy_info and sell_info:
        buy_networks = buy_info.get("withdraw_networks")
        sell_networks = sell_info.get("deposit_networks")
        if buy_networks and sell_networks:
            common = buy_networks & sell_networks
            if not common:
                return False, f"no common network between {buy_ex} and {sell_ex}", False

    unverified = buy_info is None or sell_info is None
    return True, None, unverified


def fetch_all_tickers(ex_id, exchange):
    """
    Ek hi bulk call me exchange ke saare tickers le leta hai (fast, rate-limit
    friendly), sirf SPOT market filter karta hai (futures/perp/margin skip),
    delisted/stale tokens hata deta hai.

    Returns: (usdt_result, full_result)
      usdt_result = {symbol: {"bid","ask"}} - sirf /USDT pairs, volume-filtered
                    (cross-exchange arbitrage ke liye)
      full_result = {symbol: {"bid","ask"}} - SAARE spot pairs (USDT+cross,
                    jaise BTC/ETH bhi) - triangular arbitrage ke liye
    """
    usdt_result = {}
    full_result = {}
    try:
        markets = exchange.load_markets()
    except Exception as e:
        print(f"[Market load failed] {ex_id}: {e}")
        return usdt_result, full_result

    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"[Ticker fetch failed] {ex_id}: {e}")
        return usdt_result, full_result

    for symbol, t in tickers.items():
        if "/" not in symbol:
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
        if not is_valid_quote(bid, ask):
            continue

        # Triangular ke liye - saare valid spot pairs (koi volume filter
        # nahi yahan, kyunki base/quote already USDT side se filter honge)
        full_result[symbol] = {"bid": bid, "ask": ask}

        # Cross-exchange arbitrage ke liye - sirf /USDT pairs, volume filtered
        if symbol.endswith(f"/{QUOTE_CURRENCY}"):
            vol = t.get("quoteVolume") or 0
            if vol and vol >= MIN_24H_VOLUME_USD:
                usdt_result[symbol] = {"bid": bid, "ask": ask}

    return usdt_result, full_result


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
        skipped = 0
        for t in data.get("ticker", []):
            try:
                raw_symbol = t.get("symbol", "")  # e.g. "BTC_USDT"
                if not raw_symbol.endswith(f"_{QUOTE_CURRENCY}"):
                    continue
                symbol = raw_symbol.replace("_", "/")  # -> "BTC/USDT"
                bid = safe_float(t.get("buy"))
                ask = safe_float(t.get("sell"))
                last = safe_float(t.get("last"))
                vol_base = safe_float(t.get("vol"))
                vol_usd = vol_base * (last or bid or ask)  # rough USD estimate
                if is_valid_quote(bid, ask) and vol_usd >= MANUAL_EXCHANGE_MIN_VOLUME_USD:
                    result[symbol] = {"bid": bid, "ask": ask}
            except Exception:
                skipped += 1
                continue
        if skipped:
            print(f"[Biconomy] {skipped} malformed ticker(s) skipped")
        print(f"[Biconomy] {len(result)} valid spot tickers with bid/ask found")
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
            if is_valid_quote(bid, ask) and vol_usd >= MANUAL_EXCHANGE_MIN_VOLUME_USD:
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
            if is_valid_quote(bid, ask) and vol_usd >= MANUAL_EXCHANGE_MIN_VOLUME_USD:
                result[symbol] = {"bid": bid, "ask": ask}
    except Exception as e:
        print(f"[BTSE fetch failed] {e}")
    return result


def fetch_coinw_tickers():
    """CoinW - public endpoint, no auth needed."""
    result = {}
    url = "https://api.coinw.com/api/v1/public?command=returnTicker"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"[CoinW HTTP {resp.status_code}] {resp.text[:150]}")
            return result
        data = resp.json()
        skipped = 0
        for raw_symbol, t in data.items():
            try:
                if not raw_symbol.upper().endswith(f"_{QUOTE_CURRENCY}"):
                    continue
                base = raw_symbol.upper()[: -(len(QUOTE_CURRENCY) + 1)]
                symbol = f"{base}/{QUOTE_CURRENCY}"
                bid = safe_float(t.get("highestBid"))
                ask = safe_float(t.get("lowestAsk"))
                vol_usd = safe_float(t.get("quoteVolume")) or safe_float(t.get("baseVolume")) * safe_float(t.get("last"))
                if is_valid_quote(bid, ask) and vol_usd >= MANUAL_EXCHANGE_MIN_VOLUME_USD:
                    result[symbol] = {"bid": bid, "ask": ask}
            except Exception:
                skipped += 1
                continue
        if skipped:
            print(f"[CoinW] {skipped} malformed ticker(s) skipped")
    except Exception as e:
        print(f"[CoinW fetch failed] {e}")
    return result


def fetch_hotcoin_tickers():
    """Hotcoin - public endpoint, no auth needed."""
    result = {}
    url = "https://api.hotcoinfin.com/v1/market/ticker"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            print(f"[Hotcoin HTTP {resp.status_code}] {resp.text[:150]}")
            return result
        data = resp.json()
        skipped = 0
        for t in data.get("ticker", []):
            try:
                raw_symbol = t.get("symbol", "")  # e.g. "btc_usdt"
                if not raw_symbol.lower().endswith(f"_{QUOTE_CURRENCY.lower()}"):
                    continue
                base = raw_symbol.upper()[: -(len(QUOTE_CURRENCY) + 1)]
                symbol = f"{base}/{QUOTE_CURRENCY}"
                bid = safe_float(t.get("buy"))
                ask = safe_float(t.get("sell"))
                last = safe_float(t.get("last"))
                vol_base = safe_float(t.get("vol"))
                vol_usd = vol_base * (last or bid or ask)
                if is_valid_quote(bid, ask) and vol_usd >= MANUAL_EXCHANGE_MIN_VOLUME_USD:
                    result[symbol] = {"bid": bid, "ask": ask}
            except Exception:
                skipped += 1
                continue
        if skipped:
            print(f"[Hotcoin] {skipped} malformed ticker(s) skipped")
    except Exception as e:
        print(f"[Hotcoin fetch failed] {e}")
    return result


MANUAL_EXCHANGES = {
    # "biconomy" hata diya - baar baar stale/frozen price de raha tha (RAY,
    # XPL, ASMLON sab me galat data), reliable nahi nikla
    "coinstore": fetch_coinstore_tickers,
    "btse": fetch_btse_tickers,
    "coinw": fetch_coinw_tickers,
    "hotcoin": fetch_hotcoin_tickers,
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
    se nahi, balki actually tradeable prices se. Fees minus karke NET profit
    check karta hai - raw gap "genuine" profit nahi hota.
    """
    gaps = []
    for ex_a, ex_b in combinations(quotes_by_exchange.keys(), 2):
        q_a, q_b = quotes_by_exchange[ex_a], quotes_by_exchange[ex_b]

        # Direction 1: A pe khareedo (ask), B pe becho (bid)
        if q_a["ask"] > 0 and q_b["bid"] > 0:
            gap_percent = (q_b["bid"] - q_a["ask"]) / q_a["ask"] * 100
            net_profit_percent = gap_percent - ESTIMATED_ROUND_TRIP_FEE_PERCENT
            if (0 < gap_percent <= MAX_SANE_GAP_PERCENT
                    and gap_percent >= threshold_percent
                    and net_profit_percent >= MIN_NET_PROFIT_PERCENT):
                gaps.append({
                    "buy_from": ex_a, "buy_price": q_a["ask"],
                    "sell_at": ex_b, "sell_price": q_b["bid"],
                    "gap_percent": gap_percent,
                    "net_profit_percent": net_profit_percent,
                })

        # Direction 2: B pe khareedo (ask), A pe becho (bid)
        if q_b["ask"] > 0 and q_a["bid"] > 0:
            gap_percent = (q_a["bid"] - q_b["ask"]) / q_b["ask"] * 100
            net_profit_percent = gap_percent - ESTIMATED_ROUND_TRIP_FEE_PERCENT
            if (0 < gap_percent <= MAX_SANE_GAP_PERCENT
                    and gap_percent >= threshold_percent
                    and net_profit_percent >= MIN_NET_PROFIT_PERCENT):
                gaps.append({
                    "buy_from": ex_b, "buy_price": q_b["ask"],
                    "sell_at": ex_a, "sell_price": q_a["bid"],
                    "gap_percent": gap_percent,
                    "net_profit_percent": net_profit_percent,
                })

    return gaps


# ============================================================
# MAIN LOOP
# ============================================================

def remove_frozen_prices(price_history, price_map, freeze_cycles=2):
    """
    Kisi exchange ka price agar lagataar 'freeze_cycles' baar bilkul same
    raha (chahe timestamp na mile, jaisa manual exchanges - Biconomy,
    Coinstore, BTSE - me hota hai), to wo stale/dead data hai. Aise
    exchange-symbol combos ko is cycle ke price_map se hata dete hain.

    price_history = {(ex_id, symbol): [pichle mid-prices, most recent last]}
    (ye dict function ke bahar persist hota hai, cycles ke beech)
    """
    frozen_count = 0
    for symbol, quotes in list(price_map.items()):
        for ex_id in list(quotes.keys()):
            mid = (quotes[ex_id]["bid"] + quotes[ex_id]["ask"]) / 2
            key = (ex_id, symbol)
            history = price_history.setdefault(key, [])
            history.append(mid)
            if len(history) > freeze_cycles:
                history.pop(0)

            if len(history) == freeze_cycles and len(set(history)) == 1:
                # Itne cycles se price bilkul nahi badla - frozen/stale
                del price_map[symbol][ex_id]
                frozen_count += 1
                if not price_map[symbol]:
                    del price_map[symbol]

    return frozen_count


def main():
    print("Arbitrage bot starting (ALL TOKENS mode)...")
    exchanges = init_exchanges()
    print(f"{len(exchanges)} CEX exchanges loaded (via ccxt): {list(exchanges.keys())}")
    print(f"{len(MANUAL_EXCHANGES)} manual exchanges loaded: {list(MANUAL_EXCHANGES.keys())}")

    print("Loading deposit/withdraw status (one-time, may take a minute)...")
    currency_status = load_currency_status(exchanges)
    print(f"Currency status loaded for {len(currency_status)} exchange(s)")

    last_alert_time = {}  # (symbol, buy_ex, sell_ex) -> timestamp
    price_history = {}  # (ex_id, symbol) -> [recent mid-prices] (frozen-price detection ke liye)

    while True:
        cycle_start = time.time()

        # Step 1: har exchange se saare tickers bulk me le lo
        price_map = {}  # {symbol: {exchange_id: price}}
        for ex_id, ex in exchanges.items():
            usdt_tickers, _ = fetch_all_tickers(ex_id, ex)
            for symbol, price in usdt_tickers.items():
                price_map.setdefault(symbol, {})[ex_id] = price

        # Step 1b: manual exchanges (ccxt me support nahi karte)
        for ex_id, fetch_fn in MANUAL_EXCHANGES.items():
            tickers = fetch_fn()
            for symbol, price in tickers.items():
                price_map.setdefault(symbol, {})[ex_id] = price

        # Step 1c: frozen/stale prices hatao (jo cycles se badle hi nahi)
        frozen_count = remove_frozen_prices(price_history, price_map)
        if frozen_count:
            print(f"[Cycle] {frozen_count} frozen/stale exchange-price(s) excluded")

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
        all_alerts.sort(key=lambda g: -g["net_profit_percent"])
        print(f"[Cycle] {len(all_alerts)} gap(s) found above {GAP_THRESHOLD_PERCENT}% threshold")

        sent_count = 0
        skipped_no_transfer = 0
        eligible_alerts = []  # cooldown + transferability dono pass kiye hue

        for gap in all_alerts:
            key = (gap["symbol"], gap["buy_from"], gap["sell_at"])
            now = time.time()
            if key in last_alert_time and (now - last_alert_time[key]) < ALERT_COOLDOWN_SECONDS:
                continue  # cooldown active

            base_currency = gap["symbol"].split("/")[0]
            can_transfer, reason, unverified = check_transferable(
                currency_status, gap["buy_from"], gap["sell_at"], base_currency
            )
            if not can_transfer:
                skipped_no_transfer += 1
                if skipped_no_transfer <= 5:  # spam se bachne ke liye sirf pehle 5
                    print(f"[Transfer blocked] {gap['symbol']}: {reason}")
                continue  # arbitrage execute nahi ho sakta - coin move hi nahi ho sakta

            gap["unverified"] = unverified
            eligible_alerts.append(gap)

        # Top N ka detailed alert bhejo
        detailed = eligible_alerts[:MAX_ALERTS_PER_CYCLE]
        remaining = eligible_alerts[MAX_ALERTS_PER_CYCLE:]

        for gap in detailed:
            key = (gap["symbol"], gap["buy_from"], gap["sell_at"])
            last_alert_time[key] = time.time()
            warning_line = (
                "\n⚠️ Deposit/withdraw status unverified for one exchange - "
                "manually confirm before trading!"
                if gap["unverified"] else ""
            )
            msg = (
                f"🚨 <b>Arbitrage Alert: {gap['symbol']}</b>\n\n"
                f"Buy on: <b>{gap['buy_from']}</b> @ {gap['buy_price']:.6f}\n"
                f"Sell on: <b>{gap['sell_at']}</b> @ {gap['sell_price']:.6f}\n"
                f"Raw Gap: <b>{gap['gap_percent']:.2f}%</b>\n"
                f"Est. Net Profit (after ~{ESTIMATED_ROUND_TRIP_FEE_PERCENT}% fees): "
                f"<b>{gap['net_profit_percent']:.2f}%</b>"
                f"{warning_line}"
            )
            print(msg.replace("\n", " | "))
            send_telegram_alert(msg)
            sent_count += 1

        # Baaki jo qualify karte hain lekin detailed nahi bheje, unka compact
        # summary bhejo - taaki tumhe pata chale kitne aur opportunities hain
        if remaining:
            lines = [f"📋 <b>{len(remaining)} more opportunities this cycle:</b>\n"]
            for gap in remaining[:40]:  # ek message me zyada se zyada 40 lines
                lines.append(
                    f"• {gap['symbol']}: {gap['buy_from']}→{gap['sell_at']} "
                    f"| Net: {gap['net_profit_percent']:.2f}%"
                )
                # cooldown yahan bhi set karo taaki ye bhi repeat na ho
                key = (gap["symbol"], gap["buy_from"], gap["sell_at"])
                last_alert_time[key] = time.time()
            if len(remaining) > 40:
                lines.append(f"...and {len(remaining) - 40} more (not shown)")
            summary_msg = "\n".join(lines)
            send_telegram_alert(summary_msg)
            sent_count += 1

        elapsed = time.time() - cycle_start
        print(f"[Cycle] done in {elapsed:.1f}s, {len(detailed)} detailed + "
              f"{len(remaining)} summarized, {skipped_no_transfer} skipped "
              f"(withdraw/deposit disabled)")

        sleep_time = max(5, CHECK_INTERVAL_SECONDS - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
