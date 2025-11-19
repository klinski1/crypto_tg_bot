import os
import json
import time
import requests
import re
from flask import Flask, request, jsonify
from functools import lru_cache
from typing import Dict, Any

app = Flask(__name__)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI_API_KEY    = os.getenv("XAI_API_KEY")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# === UTILS ===
def send(chat_id: int, text: str, kb=None, edit=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{'editMessageText' if edit else 'sendMessage'}"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if edit: payload["message_id"] = edit
    if kb: payload["reply_markup"] = kb
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

# === RSI + EMA ===
def calculate_rsi(p, period=14):
    if len(p) <= period: return 50.0
    g = [max(p[i]-p[i-1],0) for i in range(1,len(p))]
    l = [max(p[i-1]-p[i],0) for i in range(1,len(p))]
    ag = sum(g[-period:])/period
    al = sum(l[-period:])/period or 1e-10
    return round(100 - 100/(1 + ag/al), 1)

def ema(v, period):
    if not v: return 0.0
    k = 2/(period+1)
    e = v[0]
    for x in v[1:]: e = x*k + e*(1-k)
    return e

# === BINANCE DATA (кэш + защита) ===
@lru_cache(maxsize=16)
def get_binance_data(ticker: str) -> Dict[str, Any]:
    ticker = ticker.upper()
    symbol = f"{ticker}USDT"
    try:
        klines = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 100},
            headers=HEADERS, timeout=10
        ).json()

        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        ema20 = ema(closes[-20:], 20)
        ema50 = ema(closes[-50:], 50)
        ema_cross = "bull" if ema20 > ema50 else "bear"

        macd_line = ema(closes[-12:], 12) - ema(closes[-26:], 26)
        macd_hist = macd_line - ema([macd_line]*9, 9)

        rsi = calculate_rsi(closes)
        vol_spike = round(volumes[-1] / (sum(volumes[:-1])/99), 1)

        # POC
        mn, mx = min(closes), max(closes)
        bin_size = max((mx-mn)/20, 1)
        profile = {}
        for p, v in zip(closes, volumes):
            key = round(p/bin_size)*bin_size
            profile[key] = profile.get(key, 0) + v
        poc = max(profile, key=profile.get, default=closes[-1])

        # Futures
        funding = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": symbol}, headers=HEADERS, timeout=5).json()
        ls = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio", params={"symbol": symbol, "period": "5m", "limit": 1}, headers=HEADERS, timeout=5).json()[0]

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
            "poc_price": f"${poc:,.0f}",
            "ls_ratio": round(float(ls['longShortRatio']), 2),
        }
    except Exception as e:
        print(f"[BINANCE] {e}")
        return {"error": "offline"}

# === GROK ===
def grok(ticker: str) -> dict:
    data = get_binance_data(ticker)
    if "error" in data:
        return {"signal": "HOLD", "confidence": 0, "reason": "Binance offline"}

    prompt = f"""Ты Q-Engine v3. Анализ {ticker}/USDT сейчас.

Цена: {data['price']:.0f} ({data['change']})
EMA: {data['ema20']} → {data['ema50']} ({data['ema_cross'].upper()})
MACD: {data['macd']} (hist {data['macd_hist']:+})
RSI: {data['rsi']:.1f} | Объём: {data['volume_spike']}
Funding: {data['funding']} | L/S: {data['ls_ratio']}
POC: {data['poc_price']}

Ответь ТОЛЬКО одним JSON без текста:
{{"signal":"LONG","target_pct":6.5,"stop_pct":-2.0,"confidence":94,"reason":"EMA bull + RSI oversold"}}
или SHORT, или HOLD."""

    try:
        r = requests.post(
            "https://api.x.ai/v1/chat/completions",
            json={"model": "grok-4-latest", "messages": [{"role": "user", "content": prompt}], "temperature": 0.0, "max_tokens": 140},
            headers={"Authorization": f"Bearer {XAI_API_KEY}"},
            timeout=45
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()

        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0: raise ValueError("no json")
        raw = raw[start:end]
        raw = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', raw)
        raw = raw.replace('“','"').replace('”','"').replace("'",'"')

        result = json.loads(raw)
        sig = str(result.get("signal","HOLD")).upper()
        if sig not in ["LONG","SHORT","HOLD"]: sig = "HOLD"

        return {
            "signal": sig,
            "target_pct": float(result.get("target_pct",0)),
            "stop_pct": float(result.get("stop_pct",0)),
            "confidence": int(result.get("confidence",0)),
            "reason": str(result.get("reason","No edge"))[:100]
        }
    except Exception as e:
        print(f"[GROK] {e} → HOLD")
        return {"signal": "HOLD", "confidence": 0, "reason": "Grok timeout"}

# === MAKE_REPLY 100% КОРРЕКТНЫЙ ===
def make_reply(signal: dict, ticker: str):
    if signal["signal"] == "HOLD" or signal["confidence"] == 0:
        price = get_binance_data(ticker).get("price", 0)
        text = f"""*{ticker}/USDT* → *HOLD*
Price: `${price:,.0f}`
Confidence: 0%

_{signal['reason']}_

[ Update ]"""
        kb = {"inline_keyboard": [[{"text": "Update", "callback_data": f"UPDATE {ticker}"}]]}
        return text, kb

    data = get_binance_data(ticker)
    price = data["price"]
    tp = abs(float(signal["target_pct"]))
    sl = abs(float(signal["stop_pct"]))

    if signal["signal"] == "LONG":
        target_price = price * (1 + tp/100)
        stop_price = price * (1 - sl/100)
        tp_sign = f"+{tp:.1f}"
        sl_sign = f"-{sl:.1f}"
    else:  # SHORT
        target_price = price * (1 - tp/100)
        stop_price = price * (1 + sl/100)
        tp_sign = f"-{tp:.1f}"
        sl_sign = f"+{sl:.1f}"

    arrow = "UP" if signal["signal"] == "LONG" else "DOWN"
    text = f"""*{ticker}/USDT* → *{signal['signal']}* {arrow}
Price: `${price:,.0f}`
Target: `${target_price:,.0f}` (`{tp_sign}%`) | Stop: `${stop_price:,.0f}` (`{sl_sign}%`)
Confidence: `{signal['confidence']}%`

_{signal['reason']}_

**POC: {data['poc_price']}**

[ Update ]"""

    kb = {"inline_keyboard": [[{"text": "Update", "callback_data": f"UPDATE {ticker}"}]]}
    return text, kb

# === WEBHOOK ===
@app.route("/", methods=["POST"])
def webhook():
    try:
        u = request.get_json()
        if "message" in u:
            text = u["message"]["text"].strip().upper()
            chat_id = u["message"]["chat"]["id"]
            if text in ["BTC","ETH","SOL","BNB","XRP"]:
                send(chat_id, "_Анализирую…_")
                s = grok(text)
                reply, kb = make_reply(s, text)
                send(chat_id, reply, kb)
        elif "callback_query" in u:
            cq = u["callback_query"]
            data = cq["data"]
            chat_id = cq["message"]["chat"]["id"]
            msg_id = cq["message"]["message_id"]
            if data.startswith("UPDATE"):
                ticker = data.split()[1]
                send(chat_id, "_Обновляю…_", edit=msg_id)
                s = grok(ticker)
                reply, kb = make_reply(s, ticker)
                send(chat_id, reply, kb, edit=msg_id)
    except Exception as e:
        print(f"[FATAL] {e}")
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)