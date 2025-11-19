import os
import json
import time
import requests
from flask import Flask, request, jsonify
from functools import lru_cache
from typing import Dict, Any

# === CONFIG ===
app = Flask(__name__)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY    = os.getenv("XAI_API_KEY")

# === HEADERS ДЛЯ BINANCE (ОБЯЗАТЕЛЬНО!) ===
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json"
}

# === UTILS ===
def send_telegram(chat_id: int, text: str, keyboard: dict = None, edit: int = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{'editMessageText' if edit else 'sendMessage'}"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if edit: payload["message_id"] = edit
    if keyboard: payload["reply_markup"] = keyboard
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

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

# === BINANCE DATA (КЭШ + HEADERS) ===
@lru_cache(maxsize=16)
def get_binance_data(ticker: str) -> Dict[str, Any]:
    ticker = ticker.upper()
    symbol = f"{ticker}USDT"
    try:
        # === SPOT + KLINES ===
        klines = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 100},
            headers=HEADERS, timeout=10
        ).json()

        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        # === Индикаторы ===
        ema20 = ema(closes[-20:], 20)
        ema50 = ema(closes[-50:], 50)
        ema_cross = "bull" if ema20 > ema50 else "bear"

        ema12 = ema(closes[-12:], 12)
        ema26 = ema(closes[-26:], 26)
        macd_line = ema12 - ema26
        macd_hist = macd_line - ema([macd_line] * 9, 9)

        rsi = calculate_rsi(closes, 14)
        vol_spike = round(volumes[-1] / (sum(volumes[:-1]) / len(volumes[:-1])), 1) if len(volumes) > 1 else 1

        # === POC ===
        price_min, price_max = min(closes), max(closes)
        bin_size = max((price_max - price_min) / 20, 1)
        profile = {}
        for price, vol in zip(closes, volumes):
            bin_key = round(price / bin_size) * bin_size
            profile[bin_key] = profile.get(bin_key, 0) + vol
        poc_price = max(profile, key=profile.get, default=closes[-1])

        # === Futures (только 1 запрос) ===
        funding = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol}, headers=HEADERS, timeout=5
        ).json()
        ls = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "5m", "limit": 1}, headers=HEADERS, timeout=5
        ).json()[0]

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
            "ls_ratio": round(float(ls['longShortRatio']), 2),
        }

    except Exception as e:
        print(f"[BINANCE ERROR] {e}")
        return {"error": "Offline"}

# === GROK ===
def grok(ticker: str) -> dict:
    data = get_binance_data(ticker)
    if "error" in data:
        return {"signal": "HOLD", "confidence": 0, "reason": "Data offline"}

    prompt = f"""Analyze {ticker}/USDT 24h.

LIVE:
• Price: {data['price']} ({data['change']})
• EMA: {data['ema20']} | {data['ema50']} → {data['ema_cross'].upper()}
• MACD: {data['macd']} (hist {data['macd_hist']:+})
• RSI: {data['rsi']}
• Volume: {data['volume_spike']}
• Funding: {data['funding']}
• L/S: {data['ls_ratio']}
• POC: {data['poc_price']}

Return JSON:
{{"signal": "LONG|SHORT|HOLD", "target_pct": 3.0..14.0, "stop_pct": -1.0..-3.5, "confidence": 80..99, "reason": "facts"}}"""

    for _ in range(3):
        try:
            r = requests.post(
                "https://api.x.ai/v1/chat/completions",
                json={
                    "model": "grok-4-latest",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 120
                },
                headers={"Authorization": f"Bearer {XAI_API_KEY}"},
                timeout=30
            )
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            if "```" in raw:
                raw = raw.split("```", 2)[1] if "json" in raw.lower() else raw
            result = json.loads(raw)
            return {
                "signal": result.get("signal", "HOLD").upper(),
                "target_pct": round(float(result.get("target_pct", 0)), 1),
                "stop_pct": round(float(result.get("stop_pct", 0)), 1),
                "confidence": int(result.get("confidence", 0)),
                "reason": result.get("reason", "No edge")[:80]
            }
        except Exception as e:
            print(f"[GROK] {e}")
            time.sleep(2)
    return {"signal": "HOLD", "confidence": 0, "reason": "Timeout"}

# === REPLY ===
def make_reply(signal: dict, ticker: str):
    data = get_binance_data(ticker)
    if "error" in data:
        return "_Данные недоступны…_", None

    price = data["price"]
    target_price = price * (1 + signal["target_pct"]/100)
    stop_price = price * (1 + signal["stop_pct"]/100)

    arrow = "UP" if signal["signal"] == "LONG" else "DOWN" if signal["signal"] == "SHORT" else "NEUTRAL"
    poc = f"POC: {data['poc_price']}"

    text = f"""*{ticker}/USDT* → *{signal['signal']}* {arrow}
Price: `${price:,.0f}`
Target: `${target_price:,.0f}` (`{signal['target_pct']:+.1f}%`) | Stop: `${stop_price:,.0f}` (`{signal['stop_pct']:+.1f}%`)
Confidence: `{signal['confidence']}%`

_{signal['reason']}_

**{poc}**

[ Update ]"""

    kb = {"inline_keyboard": [[{"text": "Update", "callback_data": f"UPDATE {ticker}"}]]}
    return text, kb

# === WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    try:
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
    except Exception as e:
        print(f"[ERROR] {e}")
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)