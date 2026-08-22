#!/usr/bin/env bash
# Стенд для deploy.sh и rollback.sh.
#
#     bash deploy/selftest.sh .
#
# Скрипты развёртывания — такой же код, как остальной, и ошибка в них
# стоит дороже: она обнаруживается в тот момент, когда бот уже стоит.
# Здесь поднимается настоящая раскладка каталогов во временной папке,
# а systemctl, git, uv и chown подменяются заглушками. Проверяется ровно
# то, ради чего скрипты переписаны:
#
#   * повторная выкладка действующего тега его не уничтожает;
#   * неудача после остановки возвращает прежний релиз и поднимает бота;
#   * копия, не прошедшая инварианты, не стоит простоя;
#   * при откате WAL прежней базы не остаётся рядом с восстановленной;
#   * откат, при котором бот не поднялся, возвращает всё как было.
#
# Нужен собранный .venv репозитория (uv sync) — репетиция миграций идёт
# настоящим интерпретатором.
set -uo pipefail
REPO=$(cd "${1:-.}" && pwd)
ROOT=$(mktemp -d)
APP="$ROOT/opt/vrheaven-bot"
BIN="$ROOT/bin"
mkdir -p "$APP/releases" "$APP/data/backups" "$BIN"

PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "   ok   — $1"; }
bad()  { FAIL=$((FAIL+1)); echo "   FAIL — $1"; }
check(){ if eval "$2"; then ok "$1"; else bad "$1"; fi; }

# ---- заглушки
cat > "$BIN/systemctl" <<EOS
#!/usr/bin/env bash
action="\$1"; shift || true
case "\$action" in
  stop)   echo stopped  > "$ROOT/service.state" ;;
  start)  if [ -f "$ROOT/start.fails" ]; then echo failed > "$ROOT/service.state"; exit 1; fi
          echo running > "$ROOT/service.state" ;;
  is-active) grep -q running "$ROOT/service.state" 2>/dev/null || exit 3
             echo active ;;
  *) : ;;
esac
EOS
cat > "$BIN/journalctl" <<'EOS'
#!/usr/bin/env bash
exit 0
EOS
cat > "$BIN/chown" <<'EOS'
#!/usr/bin/env bash
exit 0
EOS
cat > "$BIN/git" <<EOS
#!/usr/bin/env bash
# git --git-dir=X --work-tree=Y checkout -f TAG
tree=""; tag=""
for a in "\$@"; do
  case "\$a" in --work-tree=*) tree="\${a#--work-tree=}";; v*) tag="\$a";; esac
done
mkdir -p "\$tree"
rsync -a --exclude .venv --exclude .git --exclude __pycache__ --exclude .pytest_cache \
      --exclude .ruff_cache --exclude '*.db' --exclude .claude "$REPO/" "\$tree/"
echo "\$tag" > "\$tree/TAG"
EOS
cat > "$BIN/uv" <<EOS
#!/usr/bin/env bash
# uv sync --frozen --no-dev -> лёгкая обёртка вокруг настоящего окружения
# репозитория: репетиция миграций идёт по-настоящему, а не понарошку
mkdir -p .venv/bin
printf 'home = %s\n' "\${UV_FAKE_HOME:-/opt/vrheaven-python/cpython-3.12/bin}" \
    > .venv/pyvenv.cfg
cat > .venv/bin/python <<'W'
#!/bin/sh
exec "$REPO/.venv/bin/python" "\$@"
W
chmod +x .venv/bin/python
EOS
cat > "$BIN/install" <<'EOS'
#!/usr/bin/env bash
exit 0
EOS
chmod +x "$BIN"/*
export PATH="$BIN:$PATH"

# ---- скрипты с подменённым APP_DIR
prep() {
  sed -e "s#^APP_DIR=/opt/vrheaven-bot#APP_DIR=$APP#" \
      -e "s#/etc/systemd/system/#$ROOT/#" "$REPO/deploy/$1" > "$ROOT/$1"
  chmod +x "$ROOT/$1"
}
prep deploy.sh
prep rollback.sh

seed_release() {   # seed_release <tag>
  mkdir -p "$APP/releases/$1/.venv/bin"
  rsync -a --exclude .venv --exclude .git --exclude __pycache__ --exclude .pytest_cache \
        --exclude .ruff_cache --exclude '*.db' --exclude .claude "$REPO/" "$APP/releases/$1/"
  { echo '#!/bin/sh'; echo "exec \"$REPO/.venv/bin/python\" \"\$@\""; } \
      > "$APP/releases/$1/.venv/bin/python"
  chmod +x "$APP/releases/$1/.venv/bin/python"
  echo "$1" > "$APP/releases/$1/TAG"
}
echo running > "$ROOT/service.state"

echo "== 1. Повторная выкладка действующего тега =="
seed_release v1.0.0
ln -sfn "$APP/releases/v1.0.0" "$APP/current"
python3 -c "
import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute('create table t(x)'); c.commit(); c.close()
" "$APP/data/adminbot.db"
bash "$ROOT/deploy.sh" v1.0.0 >"$ROOT/out1" 2>&1; rc=$?
check "выкладка того же тега завершилась успешно (rc=$rc)" "[ $rc -eq 0 ]"
check "каталог релиза на месте"            "[ -d '$APP/releases/v1.0.0' ]"
check "current указывает на него"          "[ \"\$(readlink -f '$APP/current')\" = \"\$(readlink -f '$APP/releases/v1.0.0')\" ]"
check "внутри лежит выложенный код"        "[ -f '$APP/releases/v1.0.0/deploy/deploy.sh' ]"
check "прежний каталог сохранён рядом"     "ls -d '$APP/releases/'v1.0.0-replaced-* >/dev/null 2>&1"
check "сервис запущен"                     "grep -q running '$ROOT/service.state'"
check "мусора .staging не осталось"        "! ls -d '$APP/releases/'.staging-* >/dev/null 2>&1"

echo "== 2. Неудачный старт после переключения =="
touch "$ROOT/start.fails"
seed_release v1.0.1
bash "$ROOT/deploy.sh" v1.0.1 >"$ROOT/out2" 2>&1; rc=$?
check "скрипт сообщил о неудаче (rc=$rc)"  "[ $rc -ne 0 ]"
check "current вернулся на прежний релиз"  "[ \"\$(readlink -f '$APP/current')\" = \"\$(readlink -f '$APP/releases/v1.0.0')\" ]"
check "в выводе есть слово о возврате"     "grep -q 'возвращаем прежний релиз' '$ROOT/out2'"
rm -f "$ROOT/start.fails"
echo running > "$ROOT/service.state"

echo "== 3. Откат с испорченной копией не роняет бота =="
python3 - "$APP/data/bad.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""
create table users(id integer primary key, role text, handle text, name text,
  contact text, password_hash text, percent real, owner_id integer,
  series_reset_min integer, is_active integer, deleted_at text, created_at text);
create table orders(id integer primary key, admin_id integer, owner_id integer,
  kind text, headsets integer, minutes integer, promo_id integer, promo_name text,
  base_price real, discount_percent real, price real, admin_percent real,
  admin_share real, series_pos integer, series_since text, owner_percent real,
  owner_share real, admin_payout_id integer, owner_payout_id integer,
  cancelled_at text, created_at text);
create table payouts(id integer primary key, user_id integer, amount real,
  orders_count integer, created_at text);
create table promos(id integer primary key, name text, name_folded text,
  price real, archived_at text, created_at text);
create table bonuses(id integer primary key, admin_id integer, amount real,
  comment text, payout_id integer, cancelled_at text, created_at text);
insert into users values(1,'admin','a','A','','x',0,null,null,1,null,'2026-01-01T00:00:00+00:00');
-- доля владельца без владельца: настоящая порча
insert into orders values(1,1,null,'standard',1,30,null,null,300,0,300,0,50,1,
  '2026-01-01T00:00:00+00:00',0,99,null,null,null,'2026-01-01T00:00:00+00:00');
""")
c.commit(); c.close()
PY
gzip -c "$APP/data/bad.db" > "$APP/data/backups/bad.db.gz"
cp "$APP/data/adminbot.db" "$ROOT/db.before"
bash "$ROOT/rollback.sh" v1.0.0 "$APP/data/backups/bad.db.gz" >"$ROOT/out3" 2>&1; rc=$?
check "откат отказался (rc=$rc)"           "[ $rc -ne 0 ]"
check "сервис не остановлен"               "grep -q running '$ROOT/service.state'"
check "боевая база не тронута"             "cmp -s '$ROOT/db.before' '$APP/data/adminbot.db'"
check "сказано, что бот работает"          "grep -q 'бот работает' '$ROOT/out3'"

echo "== 4. Откат с исправной копией =="
python3 - "$APP/data/good.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.executescript("""
create table users(id integer primary key, role text, handle text, name text,
  contact text, password_hash text, percent real, owner_id integer,
  series_reset_min integer, is_active integer, deleted_at text, created_at text);
create table orders(id integer primary key, admin_id integer, owner_id integer,
  kind text, headsets integer, minutes integer, promo_id integer, promo_name text,
  base_price real, discount_percent real, price real, admin_percent real,
  admin_share real, series_pos integer, series_since text, owner_percent real,
  owner_share real, admin_payout_id integer, owner_payout_id integer,
  cancelled_at text, created_at text);
create table payouts(id integer primary key, user_id integer, amount real,
  orders_count integer, created_at text);
create table promos(id integer primary key, name text, name_folded text,
  price real, archived_at text, created_at text);
create table bonuses(id integer primary key, admin_id integer, amount real,
  comment text, payout_id integer, cancelled_at text, created_at text);
insert into users values(1,'admin','a','A','','x',0,null,null,1,null,'2026-01-01T00:00:00+00:00');
insert into orders values(1,1,null,'standard',1,30,null,null,300,0,300,0,50,1,
  '2026-01-01T00:00:00+00:00',0,0,null,null,null,'2026-01-01T00:00:00+00:00');
""")
c.commit(); c.close()
PY
gzip -c "$APP/data/good.db" > "$APP/data/backups/good.db.gz"
printf 'stale-wal' > "$APP/data/adminbot.db-wal"
bash "$ROOT/rollback.sh" v1.0.0 "$APP/data/backups/good.db.gz" >"$ROOT/out4" 2>&1; rc=$?
check "откат прошёл (rc=$rc)"              "[ $rc -eq 0 ]"
check "база заменена копией"               "cmp -s '$APP/data/good.db' '$APP/data/adminbot.db'"
check "прежняя база сохранена"             "ls '$APP/data/'adminbot-before-rollback-*.db >/dev/null 2>&1"
check "чужой WAL убран от новой базы"      "[ ! -f '$APP/data/adminbot.db-wal' ]"
check "чужой WAL сохранён рядом с прежней" "ls '$APP/data/'adminbot-before-rollback-*.db-wal >/dev/null 2>&1"
check "сервис работает"                    "grep -q running '$ROOT/service.state'"

echo "== 5. Откат, при котором бот не поднялся =="
cp "$APP/data/adminbot.db" "$ROOT/db.before5"
touch "$ROOT/start.fails"
bash "$ROOT/rollback.sh" v1.0.0 "$APP/data/backups/good.db.gz" >"$ROOT/out5" 2>&1; rc=$?
check "скрипт сообщил о неудаче (rc=$rc)"  "[ $rc -ne 0 ]"
check "база возвращена как была"           "cmp -s '$ROOT/db.before5' '$APP/data/adminbot.db'"
check "предпринята попытка поднять прежнее" "grep -q 'возвращаем прежнее состояние' '$ROOT/out5'"
rm -f "$ROOT/start.fails"

echo "== 6. Откат на несуществующий релиз =="
echo running > "$ROOT/service.state"
bash "$ROOT/rollback.sh" v9.9.9 >"$ROOT/out6" 2>&1; rc=$?
check "отказ до остановки (rc=$rc)"        "[ $rc -ne 0 ]"
check "сервис не остановлен"               "grep -q running '$ROOT/service.state'"

echo "== 7. venv, смотрящий на интерпретатор вне /opt, не выкладывается =="
echo running > "$ROOT/service.state"
seed_release v1.0.2
UV_FAKE_HOME=/root/.local/share/uv/python/bin \
  bash "$ROOT/deploy.sh" v1.0.2 >"$ROOT/out7" 2>&1; rc=$?
check "выкладка отклонена (rc=$rc)"        "[ $rc -ne 0 ]"
check "сервис не остановлен"               "grep -q running '$ROOT/service.state'"
check "названа настоящая причина"          "grep -q 'вне /opt' '$ROOT/out7'"

echo
echo "итого: успешно $PASS, провалено $FAIL"
[ "$FAIL" -eq 0 ] || { echo "--- вывод последнего провала ---"; tail -30 "$ROOT"/out*; }
rm -rf "$ROOT"
exit $((FAIL > 0))
