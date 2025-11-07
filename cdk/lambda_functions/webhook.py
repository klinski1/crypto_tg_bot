# cdk/lambda_functions/webhook.py
import json, os, boto3, requests, time
from cryptography.fernet import Fernet
from functools import lru_cache

secrets = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['TABLE_NAME'])

TOKEN = secrets.get_secret_value(SecretId='TELEGRAM_BOT_TOKEN')['SecretString']
XAI   = secrets.get_secret_value(SecretId='XAI_API_KEY')['SecretString']
fernet = Fernet(secrets.get_secret_value(SecretId='ENCRYPTION_KEY')['SecretString'])

# ───── BINANCE ДАННЫЕ ─────
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
        price_now = closes[-1]
        change_24h = round((price_now / closes[0] - 1) * 100, 2)

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
        vol_avg = vol_24h / 24
        spike = round(vol_24h / vol_avg, 1) if vol_avg > 0 else 1.0

        return {
            "price": f"${price_now:,.0f}",
            "change": f"{change_24h:+.1f}%",
            "rsi": rsi,
            "funding": f"{funding_rate:+.3f}%",
            "volume_spike": f"x{spike}",
            "timestamp": int(time.time())
        }
    except:
        return {"error": "Binance down"}

def grok(ticker: str) -> dict:
    ticker = ticker.upper().replace("USDT", "")
    binance = get_binance_data(ticker)

    if "error" in binance:
        return {
            "signal": "HOLD",
            "target_pct": 0.0,
            "stop_pct": 0.0,
            "confidence": 0,
            "reason": "Data offline"
        }

    prompt = f"""Q-Engine v2 — zero-hallucination crypto oracle
Analyze {ticker}/USDT last 24h using real data below.

LIVE DATA:
• Price: {binance['price']} ({binance['change']})
• RSI-14: {binance['rsi']}
• Funding Rate: {binance['funding']}
• Volume spike: {binance['volume_spike']} vs 7d avg

TASK:
Return ONLY a valid JSON object with EXACTLY these keys:
- "signal": "LONG" | "SHORT" | "HOLD"
- "target_pct": number 3.0 to 14.0
- "stop_pct": number -1.0 to -3.5
- "confidence": integer 80–99
- "reason": 4–9 words, pipe-separated facts

NO examples. NO markdown. NO extra text. NO code blocks."""

    payload = {
       "model": "grok-4-fast-reasoning",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 120
    }

    try:
        response = requests.post(
            "https://api.x.ai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {XAI}"},
            timeout=7
        )
        response.raise_for_status()

        raw = response.json()["choices"][0]["message"]["content"].strip()

        # Убираем ```json ... ``` если вдруг
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("\n", 1)[0].strip()

        data = json.loads(raw)

        # Жёсткая валидация
        signal = str(data.get("signal", "HOLD")).upper()
        if signal not in ["LONG", "SHORT", "HOLD"]:
            signal = "HOLD"

        target = float(data.get("target_pct", 0))
        stop = float(data.get("stop_pct", 0))
        conf = int(data.get("confidence", 0))
        reason = str(data.get("reason", "No edge"))[:80]

        return {
            "signal": signal,
            "target_pct": round(max(3.0, min(14.0, target)), 1),
            "stop_pct": round(max(-3.5, min(-1.0, stop)), 1),
            "confidence": max(80, min(99, conf)),
            "reason": reason
        }

    except Exception as e:
        print(f"[GROK ERROR] {e}")
        return {
            "signal": "HOLD",
            "target_pct": 0.0,
            "stop_pct": 0.0,
            "confidence": 0,
            "reason": "Engine offline"
        }
# ───── КНОПКИ ─────
def make_reply(signal: dict, ticker: str):
    emoji = "UP" if signal["signal"] == "LONG" else "DOWN" if signal["signal"] == "SHORT" else "NEUTRAL"
    text = f"""*{ticker}/USDT* → *{signal['signal']}* {emoji}
Target: `{signal['target_pct']:+.1f}%` | Stop: `{signal['stop_pct']:+.1f}%`
Confidence: `{signal['confidence']}%`

_{signal['reason']}_"""

    keyboard = {
        "inline_keyboard": [[
            {"text": "LONG", "callback_data": f"LONG {ticker}"},
            {"text": "SHORT", "callback_data": f"SHORT {ticker}"},
            {"text": "Update", "callback_data": f"UPDATE {ticker}"}
        ]]
    }
    return text, keyboard

# ───── ЛЯМБДА ─────
def lambda_handler(event, context):
    try:
        body = json.loads(event['body'])
        
        if 'message' in body:
            msg = body['message']
            chat_id = msg['chat']['id']
            text = msg.get('text', '').strip().upper()

            if text == '/START':
                send(chat_id, "Send any ticker → get signal in 1 sec\nExample: BTC")
                return {"statusCode": 200}

            if len(text) >= 2 and text.replace('USDT','').isalnum():
                send(chat_id, "Analyzing…")
                signal = grok(text.replace('USDT',''))
                reply, keyboard = make_reply(signal, text)
                send(chat_id, reply, keyboard)
                return {"statusCode": 200}

            send(chat_id, "Send ticker: BTC, ETH, SOL…")
            return {"statusCode": 200}

        if 'callback_query' in body:
            cq = body['callback_query']
            data = cq['data']
            chat_id = cq['message']['chat']['id']
            msg_id = cq['message']['message_id']

            if data.startswith("UPDATE"):
                ticker = data.split()[1]
                send(chat_id, "Updating…", edit=msg_id)
                signal = grok(ticker)
                reply, keyboard = make_reply(signal, ticker)
                send(chat_id, reply, keyboard, edit=msg_id)
                answer_callback(cq['id'], "Updated!")
            else:
                answer_callback(cq['id'], f"You voted {data}")
            return {"statusCode": 200}

    except Exception as e:
        print(e)
    return {"statusCode": 200}

# ───── HELPERS ─────
def send(chat_id, text, keyboard=None, edit=None):
    url = f"https://api.telegram.org/bot{TOKEN}/{'editMessageText' if edit else 'sendMessage'}"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if edit: payload["message_id"] = edit
    if keyboard: payload["reply_markup"] = json.dumps(keyboard)
    requests.post(url, json=payload, timeout=5)

def answer_callback(query_id, text="OK"):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
        json={"callback_query_id": query_id, "text": text},
        timeout=5
    )