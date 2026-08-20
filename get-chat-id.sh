#!/usr/bin/env bash
# Find your Telegram chat ID and write it into .env.
#
# The bot answers only TELEGRAM_CHAT_ID (core/bot.py's @authorized decorator and
# a second check inside agent.run). Left at 0 it ignores everyone, which looks
# exactly like a dead bot.

set -e
cd "$(dirname "$0")"

TOKEN=$(grep '^TELEGRAM_TOKEN=' .env | cut -d= -f2-)
if [ -z "$TOKEN" ] || [ "$TOKEN" = "your-telegram-bot-token" ]; then
    echo "[!!] TELEGRAM_TOKEN is not set in .env"
    exit 1
fi

NAME=$(curl -s --max-time 10 "https://api.telegram.org/bot${TOKEN}/getMe" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('@'+d['result']['username'] if d.get('ok') else '')")
if [ -z "$NAME" ]; then
    echo "[!!] Token rejected by Telegram. Check TELEGRAM_TOKEN in .env."
    exit 1
fi
echo "[OK] Bot is $NAME"

echo "[..] Looking for messages sent to $NAME ..."
CHAT_ID=$(curl -s --max-time 10 "https://api.telegram.org/bot${TOKEN}/getUpdates" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ids=[]
for u in d.get('result',[]):
    m=u.get('message') or u.get('edited_message') or {}
    c=m.get('chat') or {}
    if c.get('id') and c['id'] not in ids: ids.append(c['id'])
print(ids[-1] if ids else '')
")

if [ -z "$CHAT_ID" ]; then
    echo ""
    echo "[!!] No messages found."
    echo "     Open Telegram, find $NAME, send it any message (e.g. 'hi'),"
    echo "     then run this script again."
    exit 1
fi

echo "[OK] Found chat ID: $CHAT_ID"
# BSD sed on macOS needs the empty -i argument.
sed -i '' "s/^TELEGRAM_CHAT_ID=.*/TELEGRAM_CHAT_ID=${CHAT_ID}/" .env
echo "[OK] Written to .env — the bot will now answer you and nobody else."
