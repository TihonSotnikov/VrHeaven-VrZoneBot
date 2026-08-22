# Эксплуатация бота VR Heaven

Всё, что нужно, чтобы поднять, обновить, откатить и восстановить бота.
Читается под давлением, поэтому команд ровно столько, сколько нужно.

## Раскладка на сервере

```
/opt/vrheaven-bot/
  repo.git/            репозиторий, куда пушат релизы
  releases/v1.0.0/     неизменяемая выкладка тега
  current -> releases/v1.0.0
  data/                adminbot.db, backups/, .bot.lock  (владелец vrheaven)
/etc/vrheaven-bot/
  env                  несекретная конфигурация  (root:vrheaven 0640)
  bot_token            только токен             (root:root 0600)
```

Сервис — `vrheaven-bot.service`, пользователь `vrheaven`, юнит лежит в
репозитории (`deploy/vrheaven-bot.service`) и ставится развёртыванием.

Интерпретатор — `/opt/vrheaven-python`, а не `/root/.local`: служебный
пользователь не читает `/root`, и `ProtectHome=true` закрывает его для
сервиса. Окружение, собранное против интерпретатора из `/root`, не
запустится вовсе (`status=203/EXEC`); `deploy.sh` это проверяет и
отказывается выкладывать такой релиз.

## Первый запуск

```bash
bash deploy/bootstrap.sh
# положить секреты:
printf '123456:AA...' > /etc/vrheaven-bot/bot_token
chmod 600 /etc/vrheaven-bot/bot_token
cat > /etc/vrheaven-bot/env <<'ENV'
ADMIN_IDS=111,222
TIMEZONE=Asia/Novosibirsk
DB_PATH=/opt/vrheaven-bot/data/adminbot.db
DATA_DIR=/opt/vrheaven-bot/data
BACKUP_DIR=/opt/vrheaven-bot/data/backups
ENV
chown root:vrheaven /etc/vrheaven-bot/env && chmod 640 /etc/vrheaven-bot/env
```

С рабочей машины:

```bash
git remote add production ssh://root@СЕРВЕР/opt/vrheaven-bot/repo.git
git push production main --tags
ssh root@СЕРВЕР 'bash /opt/vrheaven-bot/releases/v1.0.0/deploy/deploy.sh v1.0.0'
```

## Обновление

```bash
git tag -a v1.1.0 -m "..." && git push production main --tags
ssh root@СЕРВЕР 'cd /opt/vrheaven-bot && git --git-dir=repo.git --work-tree=/tmp/x checkout -f v1.1.0 -- deploy/deploy.sh 2>/dev/null; bash releases/*/deploy/deploy.sh v1.1.0'
```

Проще: развёртывание можно запускать скриптом из **текущего** релиза —
он одинаков во всех версиях:

```bash
ssh root@СЕРВЕР 'bash /opt/vrheaven-bot/current/deploy/deploy.sh v1.1.0'
```

`deploy.sh` перед переключением прогоняет миграции **на копии** боевой
базы и проверяет её инвариантами. Не прошло — релиз не выкладывается.

## Откат

Есть два случая, и путать их нельзя.

**Релиз без миграции схемы** — достаточно вернуть симлинк:

```bash
bash /opt/vrheaven-bot/current/deploy/rollback.sh v1.0.0
```

**Релиз с миграцией схемы** — схема останется новее старой программы, и
бот **намеренно** откажется стартовать. Нужна копия, снятая перед
миграцией (`*-pre-migration.db.gz`, не участвует в ротации 90 дней):

```bash
ls -t /opt/vrheaven-bot/data/backups/*pre-migration*
bash /opt/vrheaven-bot/current/deploy/rollback.sh v1.0.0 \
     /opt/vrheaven-bot/data/backups/adminbot-2026...-pre-migration.db.gz
```

Прежняя рабочая база при этом не удаляется, а переименовывается в
`adminbot-before-rollback-*.db` — вместе со своими `-wal` и `-shm`:
чужой журнал рядом с восстановленной базой — это два разных файла в одном.

Откат либо доходит до конца, либо оставляет работать то, что работало.
Копия распаковывается и проверяется инвариантами **до** остановки
сервиса: не прошла — бот продолжает работать, а не лежит. Если после
переключения бот не поднялся, скрипт возвращает и симлинк, и базу и
запускает прежний релиз обратно.

## Восстановление из копии

```bash
systemctl stop vrheaven-bot
cd /opt/vrheaven-bot/data
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mv adminbot.db "adminbot-broken-$STAMP.db"
# журнал прежней базы уезжает вместе с ней: рядом с восстановленной
# базой это два разных файла в одном
for x in wal shm; do
    if [ -f "adminbot.db-$x" ]; then mv "adminbot.db-$x" "adminbot-broken-$STAMP.db-$x"; fi
done
gunzip -c backups/adminbot-....db.gz > adminbot.db
chown vrheaven:vrheaven adminbot.db
/opt/vrheaven-bot/current/.venv/bin/python \
    /opt/vrheaven-bot/current/invariants.py adminbot.db
systemctl start vrheaven-bot
```

Копии проверяются автоматически раз в неделю, а результат приходит
супер-админам сообщением. Ручная проверка — та же команда `invariants.py`
над распакованной копией; часовой пояс ей больше не нужен: всё, что
нужно для суждения о заказе, лежит в самом заказе.

## Проверка состояния

```bash
systemctl status vrheaven-bot --no-pager
journalctl -u vrheaven-bot --since "-15 min" --no-pager | grep -iE "error|traceback"
ls -lt /opt/vrheaven-bot/data/backups | head
sqlite3 /opt/vrheaven-bot/data/adminbot.db \
  "select (select count(*) from orders), (select count(*) from users), \
          (select count(*) from outbox where status='pending');"
```

Недоставленные сообщения:

```bash
sqlite3 /opt/vrheaven-bot/data/adminbot.db \
  "select id, chat_id, kind, attempts, last_error from outbox \
   where status in ('pending','failed') order by id desc limit 20;"
```

Утилиты `sqlite3` на сервере может не быть — она не нужна ни боту, ни
развёртыванию. Поставить один раз: `apt install -y sqlite3`. Без неё те же
запросы выполняет интерпретатор релиза, он есть всегда:

```bash
/opt/vrheaven-bot/current/.venv/bin/python - <<'PY'
import sqlite3
db = sqlite3.connect("file:/opt/vrheaven-bot/data/adminbot.db?mode=ro", uri=True)
for query in ("select count(*) from orders", "select count(*) from users",
              "select count(*) from outbox where status='pending'"):
    print(query, "->", db.execute(query).fetchone()[0])
for row in db.execute("select id, chat_id, kind, attempts, last_error from outbox"
                      " where status in ('pending','failed')"
                      " order by id desc limit 20"):
    print(row)
PY
```

## Копии за пределы сервера

По умолчанию копия уходит документом супер-админам в Telegram — она
остаётся в чате навсегда. Чтобы добавить внешнее хранилище, задайте
команду в `/etc/vrheaven-bot/env`; она получит `{path}` и `{name}`:

```
BACKUP_OFFHOST_CMD=rclone copy {path} remote:vrheaven-backups/
```

Сбой выгрузки приходит супер-админам сообщением, а не остаётся в журнале.

## Что делать нельзя

- Запускать бота на боевом токене с ноутбука: Telegram отдаёт апдейты
  одному потребителю, второй экземпляр уводит их у сервера. Блокировка
  каталога данных защищает только от второго процесса на этом сервере.
- Править базу руками в обход бота: изменение не попадёт в журнал действий.
  Если пришлось — снимите копию до и после и запишите, что делали.
- Удалять `*-pre-migration*` копии.

## Проверка самих скриптов развёртывания

`deploy.sh` и `rollback.sh` — такой же код, как остальной, и ошибка в них
обнаруживается в тот момент, когда бот уже стоит. На рабочей машине:

```bash
bash deploy/selftest.sh .
```

Стенд поднимает настоящую раскладку каталогов во временной папке и
подменяет `systemctl`, `git`, `uv` и `chown` заглушками. Проверяются
повторная выкладка действующего тега, возврат прежнего релиза при
неудачном старте, отказ отката на непрошедшей проверку копии и возврат
базы, если бот не поднялся.
