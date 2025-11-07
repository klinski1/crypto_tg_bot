Write-Host "Запуск CryptoBot..." -ForegroundColor Green

# 1. Секреты
$TOKEN = Read-Host "Telegram Bot Token"
$XAI = Read-Host "xAI API Key (xai_...)"
$ENCRYPT_KEY = python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
Write-Host "Ключ шифрования: $ENCRYPT_KEY"

aws secretsmanager create-secret --name TELEGRAM_BOT_TOKEN --secret-string $TOKEN
aws secretsmanager create-secret --name XAI_API_KEY --secret-string $XAI
aws secretsmanager create-secret --name ENCRYPTION_KEY --secret-string $ENCRYPT_KEY

# 2. Деплой
cdk bootstrap
cdk deploy --require-approval never

# 3. Webhook
$URL = (aws cloudformation describe-stacks --stack-name CryptoBotStack --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text)
Invoke-RestMethod -Uri "https://api.telegram.org/bot$TOKEN/setWebhook" -Method Post -Body @{url=$URL}

$botname = (Invoke-RestMethod "https://api.telegram.org/bot$TOKEN/getMe").result.username
Write-Host "ГОТОВО! Бот: https://t.me/$botname" -ForegroundColor Cyan