# Crypto Bot Grok
Безопасный Telegram-бот на Grok API

## 7-минутный запуск (Windows PowerShell)

```powershell
# 1. Создай папку и перейди
mkdir crypto-bot; cd crypto-bot

# 2. Скопируй ВЕСЬ base64 ниже → bot.txt → сохрани
# 3. Выполни:
Get-Content bot.txt | Set-Content -Encoding ASCII bot.b64
certutil -decode bot.b64 crypto-bot-grok.zip
Expand-Archive crypto-bot-grok.zip -DestinationPath .
del bot.b64 bot.txt crypto-bot-grok.zip
cd crypto-bot-grok
```

## Дальше — 3 команды
```powershell
# Создай секреты (вставь свои токены)
aws secretsmanager create-secret --name TELEGRAM_BOT_TOKEN --secret-string "123456:ABC..."
aws secretsmanager create-secret --name XAI_API_KEY --secret-string "xai_..."
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | % { $k = $_; aws secretsmanager create-secret --name ENCRYPTION_KEY --secret-string $k }

# Деплой
cdk bootstrap
cdk deploy --require-approval never

# Webhook
$url = (cdk output 2>&1 | Select-String 'WebhookUrl').ToString().Split('=')[1].Trim()
$token = (aws secretsmanager get-secret-value --secret-id TELEGRAM_BOT_TOKEN --query SecretString --output text) -replace '"',''
Invoke-RestMethod -Uri "https://api.telegram.org/bot$token/setWebhook" -Method Post -Body @{url=$url}
```
Готово! Пиши боту /start → BTC
