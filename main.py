"""
Royal Signal Bot -- FINAL STRATEGY VERSION
--------------------------------------------
24/7 background service: har X minute market hours mein candle data check karta hai,
FinalStrategy (backtested) chalata hai, aur BUY signal aane par WhatsApp pe alert bhejta hai.

Strategy (validated via backtest -- see backtest-report.md):
  Layer 1 (mandatory):  EMA9 > EMA50  AND  RSI 40-65   -> healthy uptrend
                         Backtest: 72 signals, 62.5% win rate (weekly NIFTY)
  Layer 2 (bonus only): support + bullish candlestick (hammer/engulfing)
                         -> agar align ho jaye, position size 1.5x, warna 1x
                         Layer 2 kabhi bhi Layer 1 ko block nahi karta (mandatory gate nahi hai),
                         warna signals bahut kam ho jate hain (backtest mein 72 -> 2).

Kite Connect access aane tak ye MOCK data pe chalta hai (USE_MOCK_DATA=True).
Jab Kite API Key mil jaye, sirf get_market_data() function ko real Kite call
se replace karna hoga -- baaki kuch badalne ki zaroorat nahi.
"""

import os
import time
import random
import datetime
import requests

# ============================================================
# CONFIG -- environment variables se aayenge (Railway/Render
# "Environment Variables" section mein set karo, code mein kabhi
# seedha mat likhna)
# ============================================================
USE_MOCK_DATA = os.environ.get("USE_MOCK_DATA", "true").lower() == "true"

KITE_API_KEY = os.environ.get("KITE_API_KEY", "")
KITE_ACCESS_TOKEN = os.environ.get("KITE_ACCESS_TOKEN", "")

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
ALERT_TO_NUMBER = os.environ.get("ALERT_TO_NUMBER", "")  # jis number pe alert jayega

CHECK_INTERVAL_SECONDS = int(os.environ.get("CHECK_INTERVAL_SECONDS", "300"))  # default 5 min
SYMBOL = os.environ.get("SYMBOL", "NIFTY 50")

SUPPORT_LOOKBACK = int(os.environ.get("SUPPORT_LOOKBACK", "10"))
SUPPORT_TOLERANCE_PCT = float(os.environ.get("SUPPORT_TOLERANCE_PCT", "2.0"))

NORMAL_SIZE = float(os.environ.get("NORMAL_SIZE", "1.0"))
HIGH_CONVICTION_SIZE = float(os.environ.get("HIGH_CONVICTION_SIZE", "1.5"))


# ============================================================
# CANDLESTICK + PRICE ACTION HELPERS (Layer 2)
# ============================================================
def _body(c):
    return abs(c["close"] - c["open"])


def _range(c):
    return c["high"] - c["low"]


def is_bullish_engulfing(prev, curr):
    return (
        prev["close"] < prev["open"]
        and curr["close"] > curr["open"]
        and curr["close"] >= prev["open"]
        and curr["open"] <= prev["close"]
    )


def is_hammer(c):
    b, r = _body(c), _range(c)
    if r == 0:
        return False
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    return lower_wick > 2 * b and upper_wick < b and b / r < 0.35


def is_near_support(candles, lookback=SUPPORT_LOOKBACK, tolerance_pct=SUPPORT_TOLERANCE_PCT):
    """
    candles: list of recent OHLC dicts, oldest -> newest, LAST item is 'current' candle.
    Needs at least lookback+1 candles.
    """
    if len(candles) < lookback + 1:
        return False
    window = candles[-(lookback + 1):-1]
    swing_low = min(c["low"] for c in window)
    curr = candles[-1]
    return curr["low"] <= swing_low * (1 + tolerance_pct / 100)


# ============================================================
# FINAL STRATEGY -- Layer 1 mandatory, Layer 2 sizing only
# ============================================================
class FinalStrategy:
    def evaluate(self, data, recent_candles):
        """
        data: dict with keys price, rsi, ema_fast, ema_slow (current candle snapshot)
        recent_candles: list of OHLC dicts (oldest->newest, last = current), used for Layer 2
        """
        trend_ok = data["ema_fast"] > data["ema_slow"]
        rsi_ok = 40 <= data["rsi"] <= 65
        layer1_pass = trend_ok and rsi_ok

        if not layer1_pass:
            reasons = []
            reasons.append("Trend OK (EMA9>EMA50)" if trend_ok else "Trend NOT OK (EMA9<=EMA50)")
            reasons.append(f"RSI OK (40-65): {data['rsi']}" if rsi_ok else f"RSI NOT in 40-65 zone: {data['rsi']}")
            return {"verdict": "WAIT", "size": 0, "conviction": None, "reasons": reasons}

        # Layer 1 passed -- BUY confirmed. Layer 2 only affects sizing.
        layer2_pass = False
        if len(recent_candles) >= 2:
            prev, curr = recent_candles[-2], recent_candles[-1]
            bull_pattern = is_bullish_engulfing(prev, curr) or is_hammer(curr)
            at_support = is_near_support(recent_candles)
            layer2_pass = bull_pattern and at_support

        if layer2_pass:
            return {
                "verdict": "BUY",
                "size": HIGH_CONVICTION_SIZE,
                "conviction": "HIGH",
                "reasons": ["Layer1 pass (trend+RSI)", "Layer2 pass (support+bullish candle) -- higher conviction"],
            }
        return {
            "verdict": "BUY",
            "size": NORMAL_SIZE,
            "conviction": "NORMAL",
            "reasons": ["Layer1 pass (trend+RSI)", "Layer2 no confluence -- normal size"],
        }


strategy = FinalStrategy()


# ============================================================
# MARKET HOURS CHECK -- NSE 9:15 AM to 3:30 PM IST, Mon-Fri
# ============================================================
def is_market_open():
    now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)  # IST
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return False
    open_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
    close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_time <= now <= close_time


# ============================================================
# DATA FETCH -- abhi mock, baad mein Kite Connect se replace hoga
# ============================================================
_mock_history = []  # rolling candle history for mock mode


def get_market_data_mock():
    """
    Test ke liye random-ish OHLC candle banata hai aur rolling history maintain karta hai,
    taaki Layer 2 (support/candlestick) bhi test ho sake, sirf Layer 1 nahi.
    """
    global _mock_history
    last_close = _mock_history[-1]["close"] if _mock_history else 24150.0
    open_ = last_close + random.uniform(-15, 15)
    close = open_ + random.uniform(-40, 40)
    high = max(open_, close) + random.uniform(0, 20)
    low = min(open_, close) - random.uniform(0, 20)
    candle = {"open": round(open_, 2), "high": round(high, 2), "low": round(low, 2), "close": round(close, 2)}
    _mock_history.append(candle)
    _mock_history = _mock_history[-30:]  # keep last 30 for support lookback

    closes = [c["close"] for c in _mock_history]
    ema_fast = sum(closes[-9:]) / min(9, len(closes))
    ema_slow = sum(closes[-50:]) / min(50, len(closes))

    data = {
        "price": candle["close"],
        "rsi": round(random.uniform(15, 80), 1),
        "ema_fast": round(ema_fast, 2),
        "ema_slow": round(ema_slow, 2),
    }
    return data, list(_mock_history)


def get_market_data_kite():
    """
    Kite Connect se live data laane ke liye.
    Jab API key mil jaye, is function ko fill karna hoga:
      1. kiteconnect.KiteConnect(api_key=KITE_API_KEY) instance banao
      2. set_access_token(KITE_ACCESS_TOKEN)
      3. historical_data() se pichle 30+ candles lo (OHLC)
      4. EMA9/EMA50/RSI14 calculate karo (jaisa Signal Desk tool mein kiya tha)
      5. return (data_dict, recent_candles_list) -- same shape jo get_market_data_mock() deta hai
    """
    raise NotImplementedError("Kite Connect access milne ke baad ye function likhna hai.")


def get_market_data():
    if USE_MOCK_DATA:
        return get_market_data_mock()
    return get_market_data_kite()


# ============================================================
# WHATSAPP ALERT
# ============================================================
def send_whatsapp_alert(message):
    if not WHATSAPP_TOKEN or not WHATSAPP_PHONE_NUMBER_ID or not ALERT_TO_NUMBER:
        print("[WARN] WhatsApp config missing -- alert console pe hi print ho raha hai:")
        print(message)
        return

    url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": ALERT_TO_NUMBER,
        "type": "text",
        "text": {"body": message},
    }
    try:
        res = requests.post(url, headers=headers, json=payload, timeout=10)
        if res.status_code >= 300:
            print(f"[ERROR] WhatsApp send failed: {res.status_code} {res.text}")
        else:
            print("[OK] WhatsApp alert sent.")
    except requests.RequestException as e:
        print(f"[ERROR] WhatsApp request exception: {e}")


def format_alert(symbol, data, result):
    ts = (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).strftime("%d-%b %H:%M")
    lines = [f"[{ts}] {symbol} -- {result['verdict']}"]
    lines.append(f"Price: {data.get('price', 'NA')}")
    if result["verdict"] == "BUY":
        lines.append(f"Conviction: {result['conviction']} | Size: {result['size']}x")
    for r in result["reasons"]:
        lines.append(f"- {r}")
    return "\n".join(lines)


# ============================================================
# MAIN LOOP
# ============================================================
def run_once():
    if not is_market_open():
        print(f"[{datetime.datetime.now()}] Market closed -- skipping.")
        return

    data, recent_candles = get_market_data()
    result = strategy.evaluate(data, recent_candles)
    message = format_alert(SYMBOL, data, result)
    print(message)

    if result["verdict"] == "BUY":
        send_whatsapp_alert(message)


def main():
    print("Royal Signal Bot started (Final Strategy: Layer1 mandatory + Layer2 sizing).")
    print(f"Mock data: {USE_MOCK_DATA} | Interval: {CHECK_INTERVAL_SECONDS}s")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"[ERROR] run_once failed: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
