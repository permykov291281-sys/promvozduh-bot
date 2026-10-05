#!/bin/bash
# Перезапуск бота в фоне: ./run.sh
cd "$(dirname "$0")"
mkdir -p data
pkill -f "^python3 -u bot.py$" && sleep 1
setsid nohup python3 -u bot.py >> data/bot.log 2>&1 < /dev/null &
