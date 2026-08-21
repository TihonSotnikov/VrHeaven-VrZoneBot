#!/usr/bin/env bash
# Развёртывание релиза на сервере.
#
#   bash /opt/vrheaven-bot/repo.git/hooks/../../releases/<tag>/deploy/deploy.sh <tag>
#   или проще:  bash deploy/deploy.sh <tag>   (из любого каталога на сервере)
#
# Шаги: выложить тег в отдельный каталог → поставить зависимости → прогнать
# миграции на копии базы → остановить сервис → переключить симлинк →
# запустить → проверить. Прежний релиз остаётся на месте для отката.
set -euo pipefail

TAG="${1:?укажите тег релиза, например v1.0.0}"
APP_DIR=/opt/vrheaven-bot
ETC_DIR=/etc/vrheaven-bot
SERVICE=vrheaven-bot
SERVICE_USER=vrheaven
RELEASE="$APP_DIR/releases/$TAG"
UV=$(command -v uv || echo /root/.local/bin/uv)

echo "== выкладываем $TAG =="
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
git --git-dir="$APP_DIR/repo.git" --work-tree="$RELEASE" checkout -f "$TAG"

echo "== зависимости =="
cd "$RELEASE"
"$UV" sync --frozen --no-dev

echo "== репетиция миграций на копии боевой базы =="
if [ -f "$APP_DIR/data/adminbot.db" ]; then
    REHEARSAL=$(mktemp -d)
    "$RELEASE/.venv/bin/python" - "$APP_DIR/data/adminbot.db" "$REHEARSAL/rehearsal.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
src.execute("VACUUM INTO ?", (sys.argv[2],))
src.close()
PY
    DB_PATH="$REHEARSAL/rehearsal.db" DATA_DIR="$REHEARSAL" \
        BACKUP_DIR="$REHEARSAL/backups" \
        "$RELEASE/.venv/bin/python" -c "
import asyncio, sys
sys.path.insert(0, '$RELEASE')
from db import Database
async def main():
    db = Database('$REHEARSAL/rehearsal.db')
    await db.init()
    print('репетиция: версия схемы', await db.schema_version())
    await db.close()
asyncio.run(main())
"
    "$RELEASE/.venv/bin/python" "$RELEASE/invariants.py" "$REHEARSAL/rehearsal.db" \
        "$(grep -E '^TIMEZONE=' "$ETC_DIR/env" | cut -d= -f2- || echo Europe/Moscow)"
    rm -rf "$REHEARSAL"
fi

echo "== юнит =="
install -m 0644 "$RELEASE/deploy/vrheaven-bot.service" \
    /etc/systemd/system/$SERVICE.service
systemctl daemon-reload

echo "== переключение =="
systemctl stop "$SERVICE" || true
ln -sfn "$RELEASE" "$APP_DIR/current"
chown -R root:root "$RELEASE"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data"
systemctl enable "$SERVICE"
systemctl start "$SERVICE"

echo "== проверка =="
sleep 5
systemctl is-active "$SERVICE"
journalctl -u "$SERVICE" --since "-1 min" --no-pager | tail -20

# Прежние релизы храним для отката, но не бесконечно
ls -1dt "$APP_DIR/releases"/* | tail -n +6 | xargs -r rm -rf
echo "Готово: $TAG"
