import json, os, requests
from flask import Flask, request
from functools import lru_cache

app = Flask(__name__)

# Секреты (только нужные)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
XAI   = os.getenv("XAI_API_KEY")

# Кэш Binance
@lru_cache(maxsize=64)
def get_binance_data(ticker: str) -> dict:
    ticker = ticker.upper() + "USDT"
    try:
        klines = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": ticker, "interval": "1h", "limit": 25},
            timeout=5
        ).json()
        closes = [float(k[4]) for k in klines[:-1]]
        volumes = [float(k[5]) for k in klines[:-1]]
        price = closes[-1]
        change = round((price / closes[0] - 1) * 100, 2)

        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        up = sum(d for d in deltas[-14:] if d > 0) / 14
        down = abs(sum(d for d in deltas[-14:] if d < 0)) / 14
        rsi = 100 if down == 0 else round(100 - (100 / (1 + up/down)), 1)

        funding = requests.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": ticker, "limit": 1}, timeout=5
        ).json()[0]
        funding_rate = round(float(funding["fundingRate"]) * 100, 4)

        vol_24h = sum(volumes)
        spike = round(vol_24h / (vol_24h / 24), 1) if vol_24h else 1.0

        return {
            "price": f"${price:,.0f}",
            "change": f"{change:+.1f}%",
            "rsi": rsi,
            "funding": f"{funding_rate:+.3f}%",
            "volume_spike": f"x{spike}"
        }
    except:
        return {"error": "Binance down"}

def grok(ticker: str) -> dict:
    binance = get_binance_data(ticker)
    if "error" in binance:
        return {"signal":"HOLD","confidence":0,"reason":"Data offline"}

    prompt = f"""Q-Engine v2 — analyze {ticker}/USDT last 24h.

LIVE DATA:
• Price: {binance['price']} ({binance['change']})
• RSI-14: {binance['rsi']}
• Funding Rate: {binance['funding']}
• Volume spike: {binance['volume_spike']}

Return ONLY valid JSON with these keys:
"signal": "LONG" or "SHORT" or "HOLD"
"target_pct": number between 3.0 and 14.0
"stop_pct": number between -1.0 and -3.5
"confidence": integer between 80 and 99
"reason": string with 4-9 words, pipe-separated facts

No extra text. No markdown. No code blocks."""

    # Retry с увеличенным таймаутом (до 30 сек)
    for attempt in range(3):
        try:
            print(f"[GROK] Попытка {attempt+1}/3, таймаут 30 сек")
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
            print(f"[GROK] Raw: {raw[:100]}...")
            if raw.startswith("```"):
                raw = raw.split("\n",1)[1].rsplit("\n",1)[0].strip()
            data = json.loads(raw)
            print(f"[GROK] Parsed: {data}")
            return {
                "signal": str(data.get("signal","HOLD")).upper(),
                "target_pct": round(float(data.get("target_pct",0)),1),
                "stop_pct": round(float(data.get("stop_pct",0)),1),
                "confidence": int(data.get("confidence",0)),
                "reason": str(data.get("reason","No edge"))[:80]
            }
        except requests.exceptions.ReadTimeout as e:
            print(f"[GROK RETRY] Timeout на попытке {attempt+1}: {e}")
            if attempt == 2:
                return {"signal":"HOLD","confidence":0,"reason":"Timeout"}
            time.sleep(2)  # Пауза перед retry
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
                send(chat_id, "*Q-Engine v2 готов!*\\nОтправь тикер → BTC")
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