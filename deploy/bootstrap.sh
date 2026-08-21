#!/usr/bin/env bash
# Первичная подготовка сервера. Идемпотентно: повторный запуск безопасен.
#
#   bash bootstrap.sh
#
# Создаёт служебного пользователя, каталоги релизов и данных, пустой
# репозиторий для push и каталог секретов. Секреты не заполняет — их
# кладут отдельно (см. RUNBOOK.md).
set -euo pipefail

APP_DIR=/opt/vrheaven-bot
ETC_DIR=/etc/vrheaven-bot
PYTHON_DIR=/opt/vrheaven-python
SERVICE_USER=vrheaven

id -u "$SERVICE_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"

mkdir -p "$APP_DIR/releases" "$APP_DIR/data/backups" "$ETC_DIR"
chown -R root:root "$APP_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data"
chmod 750 "$APP_DIR/data"
chmod 750 "$ETC_DIR"

if [ ! -d "$APP_DIR/repo.git" ]; then
    git init --bare --initial-branch=main "$APP_DIR/repo.git"
fi

UV=$(command -v uv || echo /root/.local/bin/uv)
[ -x "$UV" ] || {
    echo "uv не установлен: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
}

# Интерпретатор ставится в общий каталог, а не в /root: служебный
# пользователь туда не ходит, да и ProtectHome=true закрывает /root
# для самого сервиса.
export UV_PYTHON_INSTALL_DIR="$PYTHON_DIR"
mkdir -p "$PYTHON_DIR"
"$UV" python install 3.12
chmod -R a+rX "$PYTHON_DIR"

echo "Готово. Дальше: положите секреты в $ETC_DIR и запустите deploy.sh <тег>"
