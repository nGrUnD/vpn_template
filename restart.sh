#!/bin/bash
set -e
cd "$(dirname "$0")"
git pull
pkill -f "app.bot.main" || true
fuser -k 8081/tcp || true
exec python -m app.bot.main
