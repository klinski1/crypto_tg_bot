import json, os, requests
from flask import Flask, request
from functools import lru_cache
from typing import Dict
from time import time

app = Flask(__name__)

# Секреты (только нужные)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI   = os.getenv("XAI_API_KEY")

# Кэш Binance
@lru_cache(maxsize=64)
def get_binance_data(ticker: str) -> Dict:
    symbol = f"{ticker}USDT"
    try:
        # SPOT + TECHNICAL
        spot = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol},
            timeout=5
        ).json()

        # KLINES для индикаторов
        klines = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1h", "limit": 100},
            timeout=5
        ).json()

        closes = [float(k[4]) for k in klines]
        volumes = [float(k[5]) for k in klines]

        # EMA-20 / EMA-50
        def ema(values, period):
            k = 2 / (period + 1)
            ema_val = values[0]
            for price in values[1:]:
                ema_val = price * k + ema_val * (1 - k)
            return ema_val

        ema20 = ema(closes[-20:], 20)
        ema50 = ema(closes[-50:], 50)

        # MACD
        ema12 = ema(closes[-12:], 12)
        ema26 = ema(closes[-26:], 26)
        macd_line = ema12 - ema26
        signal_line = ema([macd_line] * 9, 9)  # упрощённо
        macd_hist = macd_line - signal_line

        # OBV
        obv = 0
        for i in range(1, len(closes)):
            if closes[i] > closes[i-1]:
                obv += volumes[i]
            elif closes[i] < closes[i-1]:
                obv -= volumes[i]
        obv_change = (obv - sum(volumes[-24:-1])) / 1e6  # млн

        # FUTURES DATA
        futures = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": symbol},
            timeout=5
        ).json()

        oi = requests.get(
            "https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol": symbol},
            timeout=5
        ).json()

        ls_ratio = requests.get(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": symbol, "period": "5m", "limit": 1},
            timeout=5
        ).json()[0]

        # Сбор данных
        return {
            "price": spot['lastPrice'],
            "change": f"{float(spot['priceChangePercent']):+.1f}%",
            "rsi": calculate_rsi(closes, 14),
            "funding": f"{float(futures['lastFundingRate']):+.3f}%",
            "volume_spike": f"x{round(float(spot['volume']) / (sum(volumes[:-1])/len(volumes[:-1])), 1)}",
            "ema20": f"${ema20:,.0f}",
            "ema50": f"${ema50:,.0f}",
            "macd": round(macd_line, 0),
            "macd_hist": round(macd_hist, 0),
            "obv": f"{obv_change:+.1f}M",
            "oi_change": f"{(float(oi['openInterest']) - float(oi['openInterest']) * 0.918):+.1f}%",  # упрощённо
            "ls_ratio": round(float(ls_ratio['longShortRatio']), 2),
        }
    except Exception as e:
        print(f"[BINANCE ERROR] {e}")
        return {"error": "Data offline"}

def grok(ticker: str) -> dict:
    
    binance = get_binance_data(ticker)
    if "error" in binance:
        return {"signal":"HOLD","confidence":0,"reason":"Data offline"}

    prompt = f"""Q-Engine v2 — analyze {ticker}/USDT last 24h.

LIVE DATA:
• Price: {binance['price']} ({binance['change']})
• EMA-20: {binance['ema20']} | EMA-50: {binance['ema50']} → {'Bullish crossover' if binance['ema_cross'] == 'bull' else 'Bearish'}
• MACD: {binance['macd']} (hist {binance['macd_hist']:+}) → {'Strong momentum' if binance['macd_hist'] > 0 else 'Weakening'}
• RSI-14: {binance['rsi']} → {'Overbought' if binance['rsi'] > 70 else 'Oversold' if binance['rsi'] < 30 else 'Neutral'}
• Volume: {binance['volume_spike']} | OBV: {binance['obv']} → Accumulation
• Funding: {binance['funding']} | OI: {binance['oi_change']} → {'Longs building' if binance['oi_change'].startswith('+') else 'Shorts building'}
• Long/Short Ratio: {binance['ls_ratio']} → {'Bulls dominate' if binance['ls_ratio'] > 1.2 else 'Bears dominate' if binance['ls_ratio'] < 0.8 else 'Balanced'}

Return ONLY valid JSON:
{{
  "signal": "LONG" or "SHORT" or "HOLD",
  "target_pct": 3.0..14.0,
  "stop_pct": -1.0..-3.5,
  "confidence": 80..99,
  "reason": "4–9 words | facts only"
}}

No extra text. No markdown. No code blocks."""

    # === ШАГ 3: Отправляем в Grok с retry ===
    for attempt in range(3):
        try:
            print(f"[GROK] Попытка {attempt+1}/3 → {ticker}/USDT | Таймаут 30 сек")
            r = requests.post(
                "https://api.x.ai/v1/chat/completions",
                json={
                    "model": "grok-4-latest",  
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 120
                },
                headers={"Authorization": f"Bearer {XAI}"},
                timeout=30
            )
            print(f"[GROK] Status: {r.status_code}")
            r.raise_for_status()
            raw = r.json()["choices"][0]["message"]["content"].strip()
            print(f"[GROK] Raw: {raw[:120]}...")
            
            # Убираем ```json
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
            
            data = json.loads(raw)
            print(f"[GROK] Parsed: {data}")

            return {
                "signal": str(data.get("signal","HOLD")).upper(),
                "target_pct": round(float(data.get("target_pct",0)), 1),
                "stop_pct": round(float(data.get("stop_pct",0)), 1),
                "confidence": int(data.get("confidence",0)),
                "reason": str(data.get("reason","No edge"))[:80].replace('|', ' | ')
            }

        except requests.exceptions.ReadTimeout:
            print(f"[GROK RETRY] Timeout на попытке {attempt+1}")
            if attempt == 2:
                return {"signal":"HOLD","confidence":0,"reason":"Timeout"}
            time.sleep(2)

        except json.JSONDecodeError as e:
            print(f"[GROK JSON ERROR] {e} | Raw: {raw[:100]}")
            return {"signal":"HOLD","confidence":0,"reason":"JSON error"}

        except Exception as e:
            print(f"[GROK ERROR] {type(e).__name__}: {e}")
            return {"signal":"HOLD","confidence":0,"reason":"Offline"}

def make_reply(signal, ticker):
    sig = signal.get("signal", "HOLD").upper()
    target_pct = float(signal.get("target_pct", 0))
    stop_pct = float(signal.get("stop_pct", 0))
    conf = int(signal.get("confidence", 0))
    reason = str(signal.get("reason", "No edge")).strip()

    # Защита
    if sig not in ["LONG", "SHORT", "HOLD"]:
        sig = "HOLD"
    if not (3.0 <= target_pct <= 14.0):
        target_pct = 0.0
    if not (-3.5 <= stop_pct <= -1.0):
        stop_pct = 0.0
    if not (80 <= conf <= 99):
        conf = 0

    # Данные с Binance — ЧИСТЫЕ ЧИСЛА
    price_data = get_binance_data(ticker.replace('USDT',''))
    raw_price = price_data.get('price', '0')
    
    # Убираем $ и запятые
    try:
        current_price = float(raw_price.replace('$', '').replace(',', ''))
    except:
        current_price = 0.0

    # Форматируем цены
    price = f"${current_price:,.0f}" if current_price > 0 else "N/A"
    target_price = f"${current_price * (1 + target_pct/100):,.0f}" if current_price > 0 else "N/A"
    stop_price = f"${current_price * (1 + stop_pct/100):,.0f}" if current_price > 0 else "N/A"

    arrow = "UP" if sig == "LONG" else "DOWN" if sig == "SHORT" else "NEUTRAL"

    text = f"""*{ticker}/USDT* → *{sig}* {arrow}
Price: `{price}`
Target: `{target_price}` (`{target_pct:+.1f}%`) | Stop: `{stop_price}` (`{stop_pct:+.1f}%`)
Confidence: `{conf}%`

_{reason.replace('|', ' | ')}_"""

    # Только Update
    keyboard = {
        "inline_keyboard": [[
            {"text": "Update", "callback_data": f"UPDATE {ticker}"}
        ]]
    }
    return text, keyboard

def send(chat_id, text, keyboard=None, edit=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{'editMessageText' if edit else 'sendMessage'}"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if edit: payload["message_id"] = edit
    if keyboard: payload["reply_markup"] = json.dumps(keyboard)
    requests.post(url, json=payload, timeout=5)

def answer_callback(qid, text="OK"):
    requests.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
                  json={"callback_query_id": qid, "text": text}, timeout=5)

@app.route('/', methods=['POST'])
@app.route('/webhook', methods=['POST'])
def webhook():
    print("[HOOK] ← ВХОДЯЩИЙ запрос от Telegram")
    try:
        update = request.get_json(force=True)
        print(f"[HOOK] JSON получен: {json.dumps(update)[:200]}...")

        if 'message' in update:
            print("[HOOK] Это обычное сообщение")
            msg = update['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip().upper()
            print(f"[HOOK] Текст от {chat_id}: {text}")

            if text == '/START':
                print("[HOOK] Отправляю приветствие")
                send(chat_id, "Отправь тикер, например BTC")
                return "OK", 200

            if not (2 <= len(text) <= 10 and text.replace('USDT','').isalnum()):
                print("[HOOK] Некорректный тикер")
                send(chat_id, "Тикер: BTC, ETH, SOL…")
                return "OK", 200

            print(f"[HOOK] Отправляю '_Анализирую…_' для {text}")
            send(chat_id, "_Анализирую…_")

            print(f"[HOOK] → Запускаю grok({text.replace('USDT','')})")
            signal = grok(text.replace('USDT',''))
            print(f"[HOOK] ← grok вернул: {signal}")

            reply, kb = make_reply(signal, text)
            print(f"[HOOK] Отправляю ответ с кнопками")
            send(chat_id, reply, kb)

        elif 'callback_query' in update:
            print("[HOOK] Callback")
            cq = update['callback_query']
            data = cq['data']
            chat_id = cq['message']['chat']['id']
            msg_id = cq['message']['message_id']

            if data.startswith("UPDATE"):
                ticker = data.split()[1]
                print(f"[HOOK] UPDATE {ticker}")
                send(chat_id, "_Обновляю…_", edit=msg_id)
                signal = grok(ticker)
                reply, kb = make_reply(signal, ticker)
                send(chat_id, reply, kb, edit=msg_id)
                answer_callback(cq['id'], "Обновлено!")

        else:
            print("[HOOK] Неизвестный тип update")

    except Exception as e:
        error_msg = f"[FATAL ERROR] {type(e).__name__}: {e}"
        print(error_msg)
        import traceback
        traceback.print_exc()

    print("[HOOK] → Возвращаю OK")
    return "OK", 200
if __name__ == '__main__':
    print("Q-Engine v2 — ЛОКАЛЬНЫЙ РЕЖИМ (без шифрования)")
    app.run(host='0.0.0.0', port=5000)