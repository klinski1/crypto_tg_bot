#!/bin/bash
echo "Запуск CryptoBot без Binance API-ключа..."

# 1. Секреты (вводишь 1 раз)
read -p "Telegram Bot Token: " TOKEN
read -p "xAI API Key (xai_...): " XAI
ENCRYPT_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
echo "Ключ шифрования: $ENCRYPT_KEY"

aws secretsmanager create-secret --name TELEGRAM_BOT_TOKEN --secret-string "$TOKEN" --region eu-central-1
aws secretsmanager create-secret --name XAI_API_KEY --secret-string "$XAI" --region eu-central-1
aws secretsmanager create-secret --name ENCRYPTION_KEY --secret-string "$ENCRYPT_KEY" --region eu-central-1

# 2. Деплой
cdk bootstrap
cdk deploy --require-approval never

# 3. Webhook
URL=$(aws cloudformation describe-stacks --stack-name CryptoBotStack --query "Stacks[0].Outputs[?OutputKey=='WebhookUrl'].OutputValue" --output text)
curl -F "url=$URL" "https://api.telegram.org/bot$TOKEN/setWebhook"

echo "ГОТОВО! Бот: https://t.me/$(curl -s https://api.telegram.org/bot$TOKEN/getMe | jq -r .result.username)"