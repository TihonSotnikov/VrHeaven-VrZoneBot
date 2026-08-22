#!/usr/bin/env bash
# Откат на прежний релиз.
#
#   bash deploy/rollback.sh <тег>            — релиз без миграции
#   bash deploy/rollback.sh <тег> <копия>    — релиз с миграцией
#
# Если релиз нёс миграцию, откатить один симлинк недостаточно: схема
# окажется новее программы, и бот откажется стартовать — намеренно.
# Тогда нужна копия, снятая перед миграцией.
#
# Правило, из которого выведено всё остальное: **откат либо доходит до
# конца, либо оставляет работать то, что работало**. Проверки идут до
# остановки сервиса, а не после: копия, не прошедшая инварианты, не
# должна стоить простоя. Если после переключения бот не поднялся, скрипт
# возвращает и симлинк, и базу и запускает прежний релиз обратно.
set -euo pipefail

TAG="${1:?укажите тег, на который откатываемся}"
SNAPSHOT="${2:-}"
APP_DIR=/opt/vrheaven-bot
DATA_DIR="$APP_DIR/data"
DB="$DATA_DIR/adminbot.db"
SERVICE=vrheaven-bot
SERVICE_USER=vrheaven
RELEASE="$APP_DIR/releases/$TAG"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGED="$DATA_DIR/adminbot-restore-$STAMP.db"
PARKED="$DATA_DIR/adminbot-before-rollback-$STAMP.db"

resolve() { readlink -f "$1" 2>/dev/null || true; }
die() { echo "!! $*" >&2; exit 1; }

CURRENT=$(resolve "$APP_DIR/current")
DB_SWAPPED=0
SERVICE_STOPPED=0

# ------------------------------------------------ проверки до остановки

[ -d "$RELEASE" ] || die "нет каталога релиза $RELEASE"
[ -x "$RELEASE/.venv/bin/python" ] || \
    die "в релизе $TAG нет рабочего окружения ($RELEASE/.venv/bin/python)"

if [ -n "$SNAPSHOT" ]; then
    [ -f "$SNAPSHOT" ] || die "нет копии $SNAPSHOT"
    echo "== распаковываем и проверяем копию (бот пока работает) =="
    gunzip -c "$SNAPSHOT" > "$STAGED"
    if ! "$RELEASE/.venv/bin/python" "$RELEASE/invariants.py" "$STAGED"; then
        rm -f "$STAGED"
        die "копия не прошла проверку инвариантов — откат не выполнен, бот работает"
    fi
    chown "$SERVICE_USER:$SERVICE_USER" "$STAGED"
fi

# ------------------------------------------------------------- откат

restore_previous() {
    echo "!! откат на $TAG не удался — возвращаем прежнее состояние" >&2
    systemctl stop "$SERVICE" || true
    if [ "$DB_SWAPPED" = 1 ]; then
        rm -f "$DB" "$DB-wal" "$DB-shm"
        mv "$PARKED" "$DB"
        for extra in wal shm; do
            if [ -f "$PARKED-$extra" ]; then mv "$PARKED-$extra" "$DB-$extra"; fi
        done
        chown "$SERVICE_USER:$SERVICE_USER" "$DB"
    fi
    if [ -n "$CURRENT" ] && [ -d "$CURRENT" ]; then
        ln -sfn "$CURRENT" "$APP_DIR/current"
    fi
    systemctl start "$SERVICE" || true
    sleep 3
    systemctl is-active "$SERVICE" >&2 || \
        echo "!! бот не поднялся: journalctl -u $SERVICE" >&2
}

on_error() {
    local status=$?
    if [ "$SERVICE_STOPPED" = 1 ]; then restore_previous; fi
    exit "$status"
}
trap on_error EXIT

echo "== переключаемся на $TAG =="
systemctl stop "$SERVICE"
SERVICE_STOPPED=1

if [ -n "$SNAPSHOT" ]; then
    # WAL и SHM принадлежат прежней базе: оставить их рядом с
    # восстановленной — это смешать два разных файла
    mv "$DB" "$PARKED"
    for extra in wal shm; do
        if [ -f "$DB-$extra" ]; then mv "$DB-$extra" "$PARKED-$extra"; fi
    done
    mv "$STAGED" "$DB"
    DB_SWAPPED=1
    echo "   прежняя база сохранена: $PARKED"
fi

ln -sfn "$RELEASE" "$APP_DIR/current"
systemctl start "$SERVICE"
sleep 5
systemctl is-active "$SERVICE"

trap - EXIT
echo "Откат на $TAG выполнен"
