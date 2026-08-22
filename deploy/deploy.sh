#!/usr/bin/env bash
# Развёртывание релиза на сервере.
#
#   bash /opt/vrheaven-bot/current/deploy/deploy.sh <тег>
#
# Шаги: собрать релиз в отдельном каталоге → поставить зависимости →
# прогнать миграции на копии базы → остановить сервис → поставить релиз
# на место → переключить симлинк → запустить → проверить.
#
# Два свойства, ради которых написано именно так:
#
# 1. Действующий релиз не удаляется никогда. Прежде скрипт начинал с
#    `rm -rf` каталога тега, и повторная выкладка того же тега сносила
#    работающий релиз вместе с собой — в том числе этот самый файл.
#    Сборка идёт в стороне, готовое встаёт на место переименованием.
# 2. Неудача не оставляет бота лежать. Если после остановки что-то пошло
#    не так, симлинк возвращается на прежний релиз, и сервис поднимается
#    обратно.
set -euo pipefail

TAG="${1:?укажите тег релиза, например v1.0.0}"
APP_DIR=/opt/vrheaven-bot
SERVICE=vrheaven-bot
SERVICE_USER=vrheaven
KEEP_RELEASES=5
RELEASE="$APP_DIR/releases/$TAG"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGE="$APP_DIR/releases/.staging-$TAG-$STAMP"
UV=$(command -v uv || echo /root/.local/bin/uv)
# Интерпретатор — из общего каталога: venv, указывающий на /root, сервис
# запустить не сможет (ни прав, ни доступа при ProtectHome=true)
export UV_PYTHON_INSTALL_DIR=/opt/vrheaven-python

resolve() { readlink -f "$1" 2>/dev/null || true; }

CURRENT=$(resolve "$APP_DIR/current")
RESTORE_TARGET="$CURRENT"
SERVICE_STOPPED=0

cleanup() {
    local status=$?
    if [ -n "$STAGE" ]; then rm -rf "$STAGE"; fi
    if [ "$status" -ne 0 ] && [ "$SERVICE_STOPPED" = 1 ]; then
        echo "!! развёртывание $TAG не удалось — возвращаем прежний релиз" >&2
        if [ -n "$RESTORE_TARGET" ] && [ -d "$RESTORE_TARGET" ]; then
            ln -sfn "$RESTORE_TARGET" "$APP_DIR/current"
        fi
        systemctl start "$SERVICE" || true
        sleep 3
        systemctl is-active "$SERVICE" >&2 || \
            echo "!! бот не поднялся, разбирайтесь вручную: journalctl -u $SERVICE" >&2
    fi
    exit "$status"
}
trap cleanup EXIT

echo "== убираем старые релизы =="
# Чистка идёт до выкладки: сюда не попадает ни действующий релиз, ни тот,
# что кладём сейчас, ни каталог с этим скриптом
kept=0
for dir in $(ls -1dt "$APP_DIR"/releases/*/ 2>/dev/null); do
    dir=$(resolve "${dir%/}")
    [ -z "$dir" ] && continue
    [ "$dir" = "$CURRENT" ] && continue
    [ "$dir" = "$(resolve "$RELEASE")" ] && continue
    kept=$((kept + 1))
    [ "$kept" -le "$KEEP_RELEASES" ] && continue
    echo "   $(basename "$dir")"
    rm -rf "$dir"
done

echo "== собираем $TAG =="
rm -rf "$STAGE"
mkdir -p "$STAGE"
git --git-dir="$APP_DIR/repo.git" --work-tree="$STAGE" checkout -f "$TAG"

echo "== зависимости =="
cd "$STAGE"
"$UV" sync --frozen --no-dev
PYTHON_HOME=$(sed -n "s/^home = //p" "$STAGE/.venv/pyvenv.cfg")
case "$PYTHON_HOME" in
    /opt/*) ;;
    *) echo "venv смотрит на интерпретатор вне /opt ($PYTHON_HOME):" \
            "сервис его не запустит" >&2; exit 1 ;;
esac

echo "== репетиция миграций на копии боевой базы =="
if [ -f "$APP_DIR/data/adminbot.db" ]; then
    REHEARSAL=$(mktemp -d)
    "$STAGE/.venv/bin/python" - "$APP_DIR/data/adminbot.db" "$REHEARSAL/rehearsal.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
src.execute("VACUUM INTO ?", (sys.argv[2],))
src.close()
PY
    DB_PATH="$REHEARSAL/rehearsal.db" DATA_DIR="$REHEARSAL" \
        BACKUP_DIR="$REHEARSAL/backups" \
        "$STAGE/.venv/bin/python" -c "
import asyncio, sys
sys.path.insert(0, '$STAGE')
from db import Database
async def main():
    db = Database('$REHEARSAL/rehearsal.db')
    await db.init()
    print('репетиция: версия схемы', await db.schema_version())
    await db.close()
asyncio.run(main())
"
    # Инварианты не зависят ни от одной настройки: всё, что нужно для
    # суждения о заказе, лежит в самом заказе
    "$STAGE/.venv/bin/python" "$STAGE/invariants.py" "$REHEARSAL/rehearsal.db"
    rm -rf "$REHEARSAL"
fi

echo "== юнит =="
install -m 0644 "$STAGE/deploy/vrheaven-bot.service" \
    /etc/systemd/system/$SERVICE.service
systemctl daemon-reload

echo "== переключение =="
systemctl stop "$SERVICE" || true
SERVICE_STOPPED=1

if [ -e "$RELEASE" ]; then
    # Переименование, а не удаление: под этим каталогом может лежать
    # действующий релиз и файл выполняющегося сейчас скрипта. Каталог
    # заберёт чистка следующей выкладки
    REPLACED="$APP_DIR/releases/$TAG-replaced-$STAMP"
    WAS_CURRENT=0
    if [ "$CURRENT" = "$(resolve "$RELEASE")" ]; then
        WAS_CURRENT=1                  # выкладываем поверх действующего тега
    fi
    mv "$RELEASE" "$REPLACED"
    if [ "$WAS_CURRENT" = 1 ]; then
        RESTORE_TARGET="$REPLACED"
    fi
fi
mv "$STAGE" "$RELEASE"
STAGE=""                       # каталог больше не наш, чистить его нечем
ln -sfn "$RELEASE" "$APP_DIR/current"
chown -R root:root "$RELEASE"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data"
systemctl enable "$SERVICE"
systemctl start "$SERVICE"

echo "== проверка =="
sleep 5
systemctl is-active "$SERVICE"
journalctl -u "$SERVICE" --since "-1 min" --no-pager | tail -20
echo "Готово: $TAG"
