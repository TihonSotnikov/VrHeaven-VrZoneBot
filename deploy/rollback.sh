#!/usr/bin/env bash
# Откат на прежний релиз.
#
#   bash deploy/rollback.sh <тег>            — релиз без миграции
#   bash deploy/rollback.sh <тег> <копия>    — релиз с миграцией
#
# Если релиз нёс миграцию, откатить один симлинк недостаточно: схема
# окажется новее программы, и бот откажется стартовать — намеренно.
# Тогда нужно восстановить копию, снятую перед миграцией.
set -euo pipefail

TAG="${1:?укажите тег, на который откатываемся}"
SNAPSHOT="${2:-}"
APP_DIR=/opt/vrheaven-bot
SERVICE=vrheaven-bot
RELEASE="$APP_DIR/releases/$TAG"

[ -d "$RELEASE" ] || { echo "нет каталога релиза $RELEASE" >&2; exit 1; }

systemctl stop "$SERVICE"

if [ -n "$SNAPSHOT" ]; then
    [ -f "$SNAPSHOT" ] || { echo "нет копии $SNAPSHOT" >&2; exit 1; }
    STAMP=$(date -u +%Y%m%dT%H%M%SZ)
    mv "$APP_DIR/data/adminbot.db" "$APP_DIR/data/adminbot-before-rollback-$STAMP.db"
    gunzip -c "$SNAPSHOT" > "$APP_DIR/data/adminbot.db"
    chown vrheaven:vrheaven "$APP_DIR/data/adminbot.db"
    "$RELEASE/.venv/bin/python" "$RELEASE/invariants.py" "$APP_DIR/data/adminbot.db"
fi

ln -sfn "$RELEASE" "$APP_DIR/current"
systemctl start "$SERVICE"
sleep 5
systemctl is-active "$SERVICE"
