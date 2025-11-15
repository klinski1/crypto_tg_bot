import os
import json
import time
import requests
from typing import Dict, Any
from flask import Flask, request, jsonify
from functools import lru_cache

# === CONFIG ===
app = Flask(__name__)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY    = os.getenv("XAI_API_KEY")
BYBIT_API_URL  = "https://api.bybit.com"
BINANCE_API_URL = "https://api.binance.com"

# === UTILS ===
def send_telegram(chat_id: int, text: str, keyboard: dict = None, edit: int = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = keyboard
    if edit:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText"
        payload["message_id"] = edit
    requests.post(url, json=payload, timeout=5)

# === RSI ===
def calculate_rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) <= period:
        return 50.0
    gains = [max(prices[i] - prices[i-1], 0) for i in range(1, len(prices))]
    losses = [max(prices[i-1] - prices[i], 0) for i in range(1, len(prices))]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period or 1e-10
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)

# === EMA ===
def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    ema_val = values[0]
    for p in values[1:]:
        ema_val = p * k + ema_val * (1 - k)
    return ema_val

# === BINANCE DATA + POC + DIVERGENCE ===
@lru_cache(maxsize=32)
def get_binance_data(ticker: str) -> Dict[str, Any]:
    symbol = f"{ticker}USDT"
    try:
        # Klines: 1h, 100 candles
        klines = requests.get(
            f"{BINANCE_API_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 100},
            timeout=7
        ).json()

        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        # === Indicators ===
        ema20 = ema(closes[-20:], 20)
        ema50 = ema(closes[-50:], 50)
        ema_cross = "bull" if ema20 > ema50 else "bear"

        ema12 = ema(closes[-12:], 12)
        ema26 = ema(closes[-26:], 26)
        macd_line = ema12 - ema26
        macd_hist = macd_line - ema([macd_line] * 9, 9)

        rsi = calculate_rsi(closes, 14)

        avg_vol = sum(volumes[:-1]) / len(volumes[:-1]) if len(volumes) > 1 else 1
        vol_spike = round(volumes[-1] / avg_vol, 1)

        # === POC (Volume Profile) ===
        price_min, price_max = min(closes), max(closes)
        bin_size = (price_max - price_min) / 20 or 1
        profile = {}
        for i, price in enumerate(closes):
            bin_key = round(price / bin_size) * bin_size
            profile[bin_key] = profile.get(bin_key, 0) + volumes[i]
        poc_price = max(profile, key=profile.get, default=closes[-1])

        # === Divergence (last 40 candles) ===
        rsi_vals = [calculate_rsi(closes[:i+1], 14) for i in range(13, len(closes))]
        macd_vals = []
        for i in range(26, len(closes)):
            e12 = ema(closes[:i+1][-12:], 12)
            e26 = ema(closes[:i+1][-26:], 26)
            macd_vals.append(e12 - e26)

        # Lows
        lows_idx = [i for i in range(-40, 0) if closes[i] == min(closes[-40:])]
        rsi_div = macd_div = "none"
        if len(lows_idx) >= 2:
            i1, i2 = lows_idx[-2], lows_idx[-1]
            p1, p2 = closes[i1], closes[i2]
            r1, r2 = rsi_vals[i1-13], rsi_vals[i2-13]
            m1, m2 = macd_vals[i1-26], macd_vals[i2-26]
            if p2 < p1 and r2 > r1:
                rsi_div = "bullish_rsi"
            if p2 > p1 and r2 < r1:
                rsi_div = "bearish_rsi"
            if p2 < p1 and m2 > m1:
                macd_div = "bullish_macd"
            if p2 > p1 and m2 < m1:
                macd_div = "bearish_macd"

        # === Futures ===
        funding = requests.get(f"{BINANCE_API_URL}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=5).json()
        oi = requests.get(f"{BINANCE_API_URL}/fapi/v1/openInterest", params={"symbol": symbol}, timeout=5).json()
        ls = requests.get(f"{BINANCE_API_URL}/futures/data/globalLongShortAccountRatio", params={"symbol": symbol, "period": "5m", "limit": 1}, timeout=5).json()[0]

        return {
            "price": closes[-1],
            "change": f"{(closes[-1]/closes[0]-1)*100:+.1f}%",
            "rsi": rsi,
            "funding": f"{float(funding['lastFundingRate'])*100:+.3f}%",
            "volume_spike": f"x{vol_spike}",
            "ema20": f"${ema20:,.0f}",
            "ema50": f"${ema50:,.0f}",
            "ema_cross": ema_cross,
            "macd": round(macd_line),
            "macd_hist": round(macd_hist),
            "poc_price": f"${poc_price:,.0f}",
            "rsi_div": rsi_div,
            "macd_div": macd_div,
            "ls_ratio": round(float(ls['longShortRatio']), 2),
        }

    except Exception as e:
        print(f"[DATA ERROR] {e}")
        return {"error": "Offline"}

# === LIQUIDATION HEATMAP (Bybit) ===
def get_liquidation_cluster(ticker: str) -> str:
    try:
        resp = requests.get(
            f"{BYBIT_API_URL}/v5/market/recent-trade",
            params={"category": "linear", "symbol": f"{ticker}USDT", "limit": 100},
            timeout=5
        ).json()
        liq_prices = [float(t['price']) for t in resp.get('result', {}).get('list', []) if float(t['size']) > 500]
        if liq_prices:
            cluster = min(liq_prices) if len(set(liq_prices)) < 5 else "N/A"
            return f"${round(cluster):,.0f}"
    except:
        pass
    return "N/A"

# === GROK AI ===
def grok(ticker: str) -> dict:
    data = get_binance_data(ticker)
    if "error" in data:
        return {"signal": "HOLD", "confidence": 0, "reason": "Offline"}

    liq = get_liquidation_cluster(ticker)

    prompt = f"""Analyze {ticker}/USDT 24h.

LIVE:
• Price: {data['price']} ({data['change']})
• EMA: {data['ema20']} | {data['ema50']} → {data['ema_cross'].upper()}
• MACD: {data['macd']} (hist {data['macd_hist']:+})
• RSI: {data['rsi']} → {data['rsi_div'].upper()}
• Volume: {data['volume_spike']}
• POC: {data['poc_price']}
• Liquidation: {liq}

Return JSON:
{{"signal": "LONG|SHORT|HOLD", "target_pct": 3.0..14.0, "stop_pct": -1.0..-3.5, "confidence": 80..99, "reason": "facts | only"}}"""

    for _ in range(3):
        try:
            r = requests.post(
                "https://api.x.ai/v1/chat/completions",
                json={"model": "grok-4-latest", "messages": [{"role": "user", "content": prompt}], "temperature": 0, "max_tokens": 120},
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                timeout=30
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
            result = json.loads(raw)
            return {
                "signal": result.get("signal", "HOLD").upper(),
                "target_pct": round(float(result.get("target_pct", 0)), 1),
                "stop_pct": round(float(result.get("stop_pct", 0)), 1),
                "confidence": int(result.get("confidence", 0)),
                "reason": result.get("reason", "No edge")[:80].replace('|', ' | ')
            }
        except Exception as e:
            print(f"[GROK] {e}")
            time.sleep(2)
    return {"signal": "HOLD", "confidence": 0, "reason": "Timeout"}

def make_reply(signal: dict, ticker: str):
    # Получаем свежие данные
    data = get_binance_data(ticker)
    if "error" in data:
        return "_Данные недоступны…_", None

    sig = signal["signal"]
    price = data["price"]
    target_price = price * (1 + signal["target_pct"] / 100)
    stop_price = price * (1 + signal["stop_pct"] / 100)

    arrow = "UP" if sig == "LONG" else "DOWN" if sig == "SHORT" else "NEUTRAL"
    div = "DIVERGENCE!" if "div" in signal["reason"].lower() else ""
    poc = f"POC: {data.get('poc_price', 'N/A')}"
    liq = f"LIQ: {get_liquidation_cluster(ticker)}"

    text = f"""*{ticker}/USDT* → *{sig}* {arrow}
Price: `${price:,.0f}`
Target: `${target_price:,.0f}` (`{signal['target_pct']:+.1f}%`) | Stop: `${stop_price:,.0f}` (`{signal['stop_pct']:+.1f}%`)
Confidence: `{signal['confidence']}%`

_{signal['reason']}_

**{div} {poc} {liq}**

[ Update ]"""

    kb = {"inline_keyboard": [[{"text": "Update", "callback_data": f"UPDATE {ticker}"}]]}
    return text, kb

# === WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    update = request.get_json()
    if "message" in update:
        text = update["message"]["text"].strip().upper()
        chat_id = update["message"]["chat"]["id"]
        if text in ["BTC", "ETH", "SOL"]:
            send_telegram(chat_id, "_Анализирую…_")
            signal = grok(text)
            reply, kb = make_reply(signal, text)
            send_telegram(chat_id, reply, kb)
    elif "callback_query" in update:
        cq = update["callback_query"]
        data = cq["data"]
        chat_id = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]
        if data.startswith("UPDATE"):
            ticker = data.split()[1]
            send_telegram(chat_id, "_Обновляю…_", edit=msg_id)
            signal = grok(ticker)
            reply, kb = make_reply(signal, ticker)
            send_telegram(chat_id, reply, kb, edit=msg_id)
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)