# VR Heaven revenue bot — production-readiness audit

**Date:** 21 August 2026
**Scope:** full repository at `/Users/tihon/VrHeaven/tgbot_for_admins`
**Method:** read-only. No repository file was created, modified, or deleted during the review; reproductions ran from a scratch directory against the project's own test fixtures.
**Companion artifact:** <https://claude.ai/code/artifact/1391affe-b628-4b9b-8c77-8b07162815bf>

A Telegram ledger for VR-session revenue in a PC club. The accounting core is careful and
well-tested. Everything around it — message formatting, the window model, backups, migration,
and delivery — is where it will fail in production.

| | |
|---|---|
| Codebase | ~3 400 LOC · 12 modules |
| Tests | 244 passing · 18 files |
| Stack | Python 3.12 · aiogram 3.29.1 (Bot API 10.1) · aiosqlite · APScheduler |

---

## Contents

1. [Scope and method](#1-scope-and-method)
2. [Verdict](#2-verdict)
3. [Defects](#3-defects) — category 1: concrete faults, with evidence
4. [Design weaknesses](#4-design-weaknesses) — category 2: structurally likely to cause problems
5. [End-user experience](#5-end-user-experience)
6. [Operations and delivery](#6-operations-and-delivery)
7. [Optional improvements](#7-optional-improvements) — category 3
8. [Unknowns](#8-unknowns) — category 4: what the repository cannot answer
9. [Priority register](#9-priority-register)
10. [Target design](#10-target-design)
11. [Roadmap](#11-roadmap)

---

## 1. Scope and method

Every source file, test, and configuration artefact in the working tree was read: `main.py`,
`config.py`, `db.py`, `pricing.py`, `window.py`, `keyboards.py`, `reports.py`, `export.py`,
`scheduler.py`, `utils.py`, `handlers/staff.py`, `handlers/vrheaven.py`, all 18 test modules,
`README.md`, `SPEC.md`, `pyproject.toml`, `.env.example`, `.gitignore`, and
`.claude/settings.local.json`.

Claims are established one of three ways, and each finding says which:

- **Reproduced** — driven through the project's own test harness in a scratch directory and
  observed to fail.
- **Read from source** — a direct consequence of code that was read, with file and line cited.
- **Verified against the library** — checked against the installed `aiogram 3.29.1` source in
  `.venv` rather than assumed.

The test suite passes cleanly: `244 passed in 7.73s`. That number is not evidence of correctness
in the areas below — [W‑3](#w3--hand-escaped-markdownv2-plus-a-fake-bot-that-never-validates-it)
explains why the harness is structurally blind to the most severe defect found.

**What could not be checked.** No live Telegram calls were made, so the behaviour of the new
Bot API 10.1 Rich Messages against real clients is unverified (see [§8](#8-unknowns)). The
production server was not contacted; deployment facts are read from
`.claude/settings.local.json`, which records the exact commands previously run against
`root@150.241.105.197`.

---

## 2. Verdict

**Not ready for confident production operation.**

The domain model is the strong part. The three-way split is exact to the kopeck under an
adversarial grid of prices and percentages; shares are frozen at order time; soft-delete and
soft-cancel are consistently applied; the payout close is written as a conditional `UPDATE` and
genuinely survives a double press. Someone thought hard about the money.

The failures sit in the layers around it, and four of them are live in production today:

- Super-admins **stop receiving order notifications** from roughly the third cheap session of
  every shift, silently, because a negative remainder produces an unescaped `-` in a MarkdownV2
  message ([D‑1](#d1--a-negative-vr-heaven-remainder-silently-kills-every-super-admin-order-notification)).
- The off-site backup delivered to Telegram is **deleted by the bot itself** at the next screen
  render ([D‑2](#d2--the-window-model-destroys-the-telegram-backup-document)).
- The start-up backup runs *after* schema migration and **overwrites the same day's file**,
  destroying the last known-good pre-migration copy
  ([D‑3](#d3--the-start-up-backup-runs-after-migration-and-overwrites-the-days-only-copy)).
- An unrelated push notification **deletes the administrator's open «Оплата получена» screen**
  mid-order, after the price has been quoted to the customer
  ([D‑5](#d5--an-incoming-notification-deletes-the-administrators-open-payment-screen)).

Underneath those, three structural conditions make the system hard to operate safely: there is
**no version control** in the working tree and deployment is `rsync --delete` from a laptop;
there is **no audit trail** of who cancelled, paid, or repriced anything; and the database layer
has **no real transaction boundaries**, so every multi-statement money operation is exposed to
interleaving.

---

## 3. Defects

Category 1 — concrete faults in the repository, with evidence.

---

### D‑1 · A negative VR Heaven remainder silently kills every super-admin order notification

**CRITICAL** · Reproduced

```
handlers/staff.py:480 — f"Остаток VR Heaven: {fmt_money(vr_share)}"
utils.py:47           — fmt_num() returns "-110" for negative amounts
main.py:36            — DefaultBotProperties(parse_mode=ParseMode.MARKDOWN_V2)
```

**What.** The new-order push to super-admins is MarkdownV2. `fmt_money()` renders negative
amounts with a bare hyphen, and `-` is a reserved MarkdownV2 character that must be escaped.
Telegram rejects the whole message with `400 Bad Request: can't parse entities`.
`_notify_vrheaven()` catches `TelegramAPIError` and logs a warning, so the failure is invisible.

**Where.** Only this one call site interpolates a value that can go negative into MarkdownV2.
Everywhere else the same formatter is used inside Rich-Message HTML, where `-` is harmless —
which is why the summary screen shows the negative remainder correctly and the push does not.

**Why it matters.** This is not an edge case; it is the common case. `SPEC.md §3` states the
remainder may be negative because the salary ladder is price-independent. With default prices, a
single-headset 15-minute session at 200 ₽ and a 30 % owner goes negative on the **third order of
the shift** — earlier still during discount hours.

**How it fails.** From the third cheap order onward, VR Heaven receives nothing. The orders are
recorded correctly; only the oversight channel dies. Precisely the loss-making orders — the ones
a super-admin most needs to see — are the ones that never arrive. Nothing surfaces except a
`WARNING` line in `journalctl`.

**Fix.** Make `fmt_money`/`fmt_num` MarkdownV2-safe at the source — emit the typographic minus
`−` (U+2212), which is not reserved, or escape the hyphen. Better: stop hand-escaping altogether
(see [W‑3](#w3--hand-escaped-markdownv2-plus-a-fake-bot-that-never-validates-it)). Add a
regression test asserting the ladder-driven negative remainder renders safely.

```
$ python repro_neg.py          # five 15-min orders, one admin, 30% owner
order 1: 'Остаток VR Heaven: 90 ₽'    -> markdownv2 errors: []
order 2: 'Остаток VR Heaven: 40 ₽'    -> markdownv2 errors: []
order 3: 'Остаток VR Heaven: -10 ₽'   -> ERROR: unescaped '-'
order 4: 'Остаток VR Heaven: -60 ₽'   -> ERROR: unescaped '-'
order 5: 'Остаток VR Heaven: -110 ₽'  -> ERROR: unescaped '-'
```

---

### D‑2 · The window model destroys the Telegram backup document

**CRITICAL** · Read from source

```
scheduler.py:155-161 — backup_job() sets the document as the chat window
window.py:92-98      — render(new_message=True) deletes the current window first
window.py:120-130    — render_cb() deletes a non-editable message and replaces it
```

**What.** The daily backup is sent as a document and then registered as the chat's single window
(`SPEC.md §10` makes this explicit: «документ становится окном чата»). The single-window
invariant then guarantees the document is deleted at the next interaction — a push replaces the
window, and pressing «В меню» on the document itself fails to edit, so `render_cb` deletes it and
sends text.

**Where.** `scheduler.py:161` stores `document.message_id` as the window; `window.py:92-94` and
`window.py:126` delete it. Confirmed as intended behaviour by
`tests/test_scheduler.py::test_backup_reaches_every_superadmin_and_becomes_window`.

**Why it matters.** Telegram is the only off-host copy of the database. Local copies live in
`BACKUP_DIR` on the same disk as `adminbot.db`. Making the off-host copy self-deleting removes
the only defence against loss of the server.

**How it fails.** Backup arrives at 03:00. At 09:05 the first order of the day pushes a
notification; the document is deleted. The super-admin believes off-site backups exist. Disk
loss or a bad `rsync` then destroys everything, and the only surviving copies are whichever
documents happened to arrive after the last interaction.

**Fix.** Exclude documents from the window model entirely: send the backup as a plain document
with no keyboard, leave the window untouched, and re-render the menu as a separate message if
needed. Independently, push backups to an off-host target (object storage or a second machine)
rather than relying on a chat.

---

### D‑3 · The start-up backup runs after migration and overwrites the day's only copy

**CRITICAL** · Read from source

```
main.py:25 then main.py:30 — db.init() (schema + _migrate) runs BEFORE make_backup()
scheduler.py:128-131       — if os.path.exists(path): os.remove(path)  # then VACUUM INTO
```

**What.** Two compounding problems. First, ordering: `db.init()` runs `_migrate()`, which
rebuilds the orders table, and only then is the “safety” backup taken — so the backup captures
the post-migration state, not the state that could be rolled back to. Second, `make_backup`
*deletes* the existing file for today before writing the new one, and the file name has day
granularity.

**Where.** `main.py:25` then `main.py:30`; `scheduler.py:128`.

**Why it matters.** The one moment the project most needs an immutable pre-change snapshot — a
code deploy that carries a schema migration — is exactly the moment it overwrites the day's
snapshot with the migrated result. Deleting before writing also means a failed `VACUUM INTO`
(disk full, permissions) leaves *no* file for today.

**How it fails.** Deploy at 14:00 introduces a migration that mangles orders. The bot restarts,
backs up the mangled database over `adminbot-2026-08-21.db`, and the last good copy is now 24
hours old — assuming the 03:00 job ran and its Telegram document has not already been deleted by
[D‑2](#d2--the-window-model-destroys-the-telegram-backup-document).

**Fix.** Take the start-up backup *before* `db.init()`, to a distinct immutable name
(`adminbot-{stamp}-pre-migration-{schema_hash}.db`). Write every backup to a temporary path and
`os.replace()` into place so a failure never destroys the previous file. Keep hourly or per-boot
granularity, not per-day.

---

### D‑4 · A crash during migration silently orphans all order history

**CRITICAL** · Read from source

```
db.py:204-245 — _migrate(): RENAME → CREATE → INSERT…SELECT → DROP, no explicit
                transaction, no schema version, detection by column presence (db.py:215)
```

**What.** The orders rebuild is a four-step sequence. Under Python's legacy `sqlite3` isolation,
DDL statements (`ALTER`, `CREATE`) execute in autocommit; only the `INSERT…SELECT` opens a
transaction. So `ALTER TABLE orders RENAME TO orders_legacy` and `CREATE TABLE orders` commit
immediately, before any data is copied.

**Where.** `db.py:216-227`. Migration is detected by
`if "promo_id" not in await self._table_columns("orders")` — the new empty table already has
`promo_id`.

**Why it matters.** The interrupted state is indistinguishable from the completed state. There
is no `user_version`, no migrations table, and no post-condition check.

**How it fails.** Process is killed (OOM, `systemctl restart` during deploy, host reboot)
between the `CREATE` and the copy's commit. On restart `_migrate` sees `promo_id` present,
skips, and the bot comes up with an empty orders table while all history sits in `orders_legacy`.
Nothing reports it. Summaries, payouts, and exports all silently show zero.

**Fix.** Wrap each migration in `BEGIN IMMEDIATE … COMMIT` with an explicit
`PRAGMA user_version` bump inside the same transaction; refuse to start if the version is
unrecognised. Verify row counts before dropping the legacy table. Take an immutable
pre-migration backup first
([D‑3](#d3--the-start-up-backup-runs-after-migration-and-overwrites-the-days-only-copy)).

---

### D‑5 · An incoming notification deletes the administrator's open payment screen

**HIGH** · Reproduced

```
window.py:92-98 — render(..., new_message=True) deletes the current window
handlers/vrheaven.py:111 · handlers/staff.py:102 — every push uses new_message=True
```

**What.** Pushes must be new messages to produce a notification, and the single-window rule
requires deleting the previous window. If the previous window is the confirmation screen —
**«К оплате: 300 ₽» with the «Оплата получена» button** — it is deleted. FSM state stays in
`NewOrderSG.confirm`, but the button the admin needs is gone.

**Where.** Any push into an administrator's chat: bonus award, order cancellation by VR Heaven,
payout confirmation, payout-day report, new-device warning.

**Why it matters.** This lands squarely in the money-handling moment. The price has been quoted
and the customer is paying; the screen vanishes with no explanation.

**How it fails.** Admin quotes 300 ₽ and takes cash. VR Heaven awards someone a bonus. The
admin's screen becomes «Вам начислен бонус». The order is unrecorded, and the admin has no idea
whether it went through. Most likely reaction: redo the order, or don't — either way the ledger
and the till disagree.

**Fix.** Suppress or defer pushes while a chat holds an active money-critical FSM state; or drop
the single-window invariant for the order flow so the confirmation screen is never replaced. At
minimum, on re-entry detect the orphaned `confirm` state and re-render the pending order rather
than silently dropping it.

```
$ python repro_push.py
admin window before push: 50
confirm screen text: '*Новый заказ · 1 шлем · 30 мин*\n\n*К оплате: 300 ₽*…'
fsm state: NewOrderSG:confirm
deleted messages in admin chat: [(1, 50)]
fsm state after push: NewOrderSG:confirm
=> confirm screen with 'Оплата получена' was deleted: True
```

---

### D‑6 · Concurrent orders from one account collide on the salary ladder step

**HIGH** · Reproduced

```
handlers/staff.py:437 — count_series_orders(...) + 1, then add_order() at :441
db.py:444-451         — count_series_orders is a plain SELECT COUNT(*)
```

**What.** The ladder position is a check-then-insert: count non-cancelled orders since the
series reset, add one, insert. Two orders processed concurrently both read the same count and
both write the same `series_pos`.

**Where.** Reachable whenever one account is bound to two chats — a first-class feature
(`SPEC.md §7`, «мульти-аккаунт»), and the normal arrangement for a shift handover or co-owners.

**Why it matters.** The administrator is **underpaid**, permanently. Shares are frozen at
creation and never recomputed, so nothing corrects it. The «№N в серии» shown on the result
screen and in `orders.csv` is also wrong.

**How it fails.** Two devices confirm orders in the same second. Both are recorded at step 1 —
50 ₽ each instead of 50 ₽ + 100 ₽. The admin is out 50 ₽ with no trace of why, and the
discrepancy only surfaces if someone reconciles the CSV by hand.

**Fix.** Derive the ladder position inside the same transaction as the insert, under
`BEGIN IMMEDIATE`, or compute it in SQL as part of the `INSERT … SELECT`. A per-admin async lock
is a cheap interim mitigation but does not survive multiple processes.

```
$ python repro_race.py
CONCURRENT ORDERS -> (id, price, admin_share, series_pos):
  [(1, 300.0, 50.0, 1), (2, 500.0, 50.0, 1)]   # expected 50.0/1 and 100.0/2
```

---

### D‑7 · Double-tapping «Оплата получена» raises an unhandled KeyError and hangs the button

**HIGH** · Reproduced

```
handlers/staff.py:413-414 — data = await state.get_data(); await state.clear()
handlers/staff.py:439     — data["price"]  → KeyError when the state was already cleared
main.py:38                — Dispatcher() with no events_isolation; no dp.errors handler
```

**What.** aiogram dispatches updates as concurrent tasks (`handle_as_tasks=True` by default,
verified in `.venv`) and `main.py` registers no event isolation. Both taps pass the state filter;
the second finds an empty FSM payload and dereferences `data["price"]`.

**Where.** `handlers/staff.py:439`. The existing test
`test_order_confirm_double_tap_records_single_order` models the dispatcher rather than exercising
it, and its docstring's claim that «второй апдейт уходит в stale_callback» does not hold for the
real dispatcher.

**Why it matters.** The user-visible result is a spinner that never resolves and no message at
all. Worse, the reason no *second order* is created is accidental: `MemoryStorage.get_data` and
`set_data` contain no `await` points, so the read-then-clear pair happens not to yield. Swapping
in a persistent storage — the natural fix for
[W‑2](#w2--fsm-state-lives-in-memory-and-dies-with-the-process) — turns this into a genuine
duplicate-order bug.

**How it fails.** Admin taps twice on a slow connection. One order is recorded, one callback
hangs. If the admin reads the hang as failure, they redo the order and the customer is
double-charged in the ledger.

**Fix.** Pass `events_isolation=SimpleEventIsolation()` to `Dispatcher` so per-chat updates
serialise, and make the handler defensive: if the payload is missing, answer the callback with an
explicit alert instead of raising. Register a global `dp.errors` handler that always answers the
callback query.

---

### D‑8 · Unbounded free text overflows the 4096-character message limit and breaks the window

**HIGH** · Reproduced

```
handlers/vrheaven.py:605-620 — bonus comment via _normalize_contact(), no length cap
handlers/vrheaven.py:887-909 — _add_name_step(), no length cap
handlers/vrheaven.py:911-913 — _normalize_contact(), no length cap
```

**What.** Bonus comments, account names, and contacts accept any text up to Telegram's own
4096-character input limit. `esc()` then roughly doubles the length for MarkdownV2, so the
rendered card exceeds 4096 and Telegram rejects it.

**Where.** Reproduced with a 4500-character comment: the rendered card came to **5726
characters**.

**Why it matters.** The failure is not contained. `render()` deletes the current window, then
the replacement send also fails, and the `TelegramBadRequest` escapes the handler unhandled. The
chat is left with no window and a stale `windows.message_id`. Meanwhile the bonus *was* created
and the notification to the administrator *also* failed — swallowed by `_push_user_chats`.

**How it fails.** A super-admin pastes a long explanation into a bonus comment. The bonus lands
in the ledger; neither party sees any confirmation; the admin is never told. The money appears in
the next payout with no record anyone can read.

**Fix.** Cap every free-text field at input (name ≤ 64, contact ≤ 64, comment ≤ 200) with an
explicit validation message, and add a length guard in `window.py` that truncates with an
ellipsis rather than letting the send fail. Never delete a window before the replacement is
confirmed sent.

```
$ python repro_misc.py
1) bonus comment: rendered length = 5726 -> Telegram limit 4096: EXCEEDS
   stored comment length: 4500
```

---

### D‑9 · Malformed callback data raises unhandled exceptions

**MEDIUM** · Reproduced

```
handlers/vrheaven.py — 11 sites of int(cb.data.rsplit/split(...)) with no guard
handlers/staff.py:323, 359, 561, 594 — same pattern
```

**What.** Callback payloads are parsed with bare `int()`. Telegram relays whatever the client
sends; a modified client can send arbitrary `callback_data`.

**Where.** Reproduced: `ad:card:abc` → `ValueError`; `ad:card:` → `ValueError`;
`ac:o:99999999999999999999` → `OverflowError: Python int too large to convert to SQLite INTEGER`.

**Why it matters.** No `dp.errors` handler exists, so the exception is logged and dropped; the
callback is never answered. It is not a privilege-escalation path — every handler re-validates
the target from the database and the role filters hold — but it is an unguarded input surface and
a free way to spam `ERROR` lines into the journal.

**How it fails.** Log noise that masks real errors, and a hung button for anyone who triggers it.

**Fix.** One parsing helper that returns `None` on any malformed payload and answers the callback
with «Кнопка устарела». Register a catch-all `dp.errors` handler that logs with context and
always answers the callback.

---

### D‑10 · VR Heaven cannot cancel an order older than the last ten, contrary to the spec

**MEDIUM** · Read from source

```
db.py:454-463 — last_orders(limit=10), global, ordered by id DESC
handlers/vrheaven.py:181-196 — the only entry point to cancellation
SPEC.md §5 — «VR Heaven может отменить любой заказ любой давности»
```

**What.** The cancellation screen offers exactly the last ten non-cancelled orders across all
clubs. There is no lookup by order number and no pagination.

**Why it matters.** With several active clubs, ten orders is under an hour of traffic. The spec's
escalation path — «позже — только через поддержку @VrHeaven» — leads to a screen that cannot
perform the action.

**How it fails.** An admin reports a mistaken order from yesterday. Support has no way to cancel
it through the bot and must edit SQLite by hand on the server, bypassing every notification and
consistency check the code provides.

**Fix.** Add order lookup by number (`№123`) plus pagination, and scope the list per
administrator. The confirmation screen already warns about paid shares, so the safety rail exists.

---

### D‑11 · Case-insensitive promo-name uniqueness does not work for Cyrillic

**MEDIUM** · Reproduced

```
db.py:650-657 — WHERE lower(name) = lower(?)
SPEC.md §2 — «уникально среди действующих без учёта регистра»
```

**What.** SQLite's built-in `lower()` folds ASCII only. `lower('День Рождения')` returns the
string unchanged, so «День рождения» and «день рождения» are accepted as two distinct active
promos.

**Why it matters.** The interface is entirely Russian, so in practice the uniqueness rule never
fires for a real promo name. There is also no `UNIQUE` constraint anywhere in the schema — every
uniqueness rule in the system is application-level check-then-insert.

**How it fails.** Two visually identical promo buttons appear in the admin's order screen,
distinguishable only by price. Orders split arbitrarily between them and reporting fragments.

**Fix.** Normalise in Python with `str.casefold()` before comparing, store a `name_folded`
column, and add a partial `UNIQUE INDEX … WHERE archived_at IS NULL` so the database enforces it
rather than the handler.

```
sqlite> select lower('День Рождения');                          -- 'День Рождения'
sqlite> select lower('День рождения')=lower('день рождения');   -- 0
```

---

### D‑12 · Rate-limit and network errors are swallowed as ordinary delivery failures

**MEDIUM** · Verified against library

```
aiogram/exceptions.py:82 — class TelegramRetryAfter(TelegramAPIError)
aiogram/exceptions.py:74 — class TelegramNetworkError(TelegramAPIError)
handlers/*.py, scheduler.py — every push wrapped in `except TelegramAPIError`
```

**What.** `TelegramRetryAfter` (HTTP 429) and `TelegramNetworkError` both subclass
`TelegramAPIError`, so the “best-effort delivery” handlers treat a throttle exactly like a
blocked chat: log and move on. There is no retry, no throttling middleware, and no queue.

**Where.** Most damaging in `scheduler.py:40-61`, which fans out payout-day reports to every
recipient and every bound chat in a tight loop with no pacing.

**Why it matters.** Telegram's global limit is about 30 messages per second. Payout day is
exactly when the fan-out is largest and the reports matter most. Conversely, `render_cb` for the
initiator's own screen is *not* wrapped, so a 429 there escapes unhandled after the order has
already been written.

**How it fails.** On the 1st at 10:00, an arbitrary subset of owners and admins never receive
their period report, and the bot reports «доставлено N из M» to nobody. Or: an order is recorded,
the result screen fails to render, the admin re-enters the order.

**Fix.** Handle `TelegramRetryAfter` explicitly with sleep-and-retry; add an outgoing throttle (a
token bucket, or aiogram's flood-control middleware); pace the payout-day fan-out. Treat only
`TelegramForbiddenError`/`TelegramNotFound` as permanent, and use them to unbind dead chats.

---

### D‑13 · `create_payout` has no transaction boundary

**MEDIUM** · Read from source

```
db.py:711-771 — INSERT payout(amount=0) → UPDATE orders → UPDATE bonuses →
                SELECT sums → UPDATE payout SET amount; commit only at the end
db.py         — every other method commits independently on the same connection
```

**What.** A payout is written across five statements with the amount filled in last. There is no
`BEGIN IMMEDIATE`, and every other database method on the shared connection issues its own
`commit()` — which will commit whatever partial work is pending, including another coroutine's.

**Why it matters.** The conditional-`UPDATE` design does correctly prevent a double payout
(verified: two concurrent calls returned one payout and `None`). What it does not provide is
atomicity. A crash, or an interleaved commit from `add_order`/`cancel_order`, can leave orders
marked as paid against a payout row that still says `amount = 0`.

**How it fails.** Process dies between the orders `UPDATE` and the amount `UPDATE`. Those orders
are permanently out of the recipient's current period, and the payout record says the recipient
was paid nothing. The money owed is unrecoverable from the data.

**Fix.** Give `Database` an explicit `transaction()` async context manager issuing
`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`, and route every multi-statement operation through it —
`create_payout`, order creation with its ladder read, and cancellation with its payout check.
Serialise with an `asyncio.Lock` around the connection.

---

### D‑14 · The payout-day report announces a period close that never happens

**MEDIUM** · Read from source

```
scheduler.py:55       — report += "Выплата будет проведена в ближайшее время"
scheduler.py:84, :89  — title="Учётный период закрыт"
SPEC.md §4 — «календарных границ нет: выплата закрывает период»
```

**What.** On the 1st and 15th the bot sends every recipient a report titled «Учётный период
закрыт». Nothing is closed — the period closes only when a super-admin manually runs a payout,
which may be days later or not at all. Orders placed at 10:05 join the same still-open period.

**Why it matters.** Recipients are told a number is final when it is not, and are promised a
payout the bot has no power to schedule. The figures in the report will not match what they are
eventually paid.

**How it fails.** An owner reads «закрыт · К выплате: 12 400 ₽», then receives 14 950 ₽ three
days later. They cannot reconcile either number and have no statement to check against.

**Fix.** Either genuinely close the period (snapshot it and treat later orders as the next
period) or retitle honestly: «Накоплено к выплате на 1 сентября» with «Выплата проводится
вручную; итоговая сумма может измениться». Given `SPEC.md §4` defines the period by payout, the
honest wording is the correct fix.

---

### D‑15 · A suspended owner permanently loses their claim on orders placed during suspension

**MEDIUM** · Read from source

```
handlers/staff.py:426-430 — owner resolved only if is_active and not deleted;
                            otherwise owner_id is written as NULL (handlers/staff.py:442)
```

**What.** If the owner is suspended at the moment of the order, the order is written with
`owner_id = NULL`. That matches the letter of `SPEC.md §7`, but the association is destroyed, not
merely the share.

**Why it matters.** Reactivating the owner does not restore anything. The orders never appear in
that owner's period, «Мои администраторы», or CSV export — even historically. From the data there
is no way to tell the club ever generated that revenue. The whole amount flows to the VR Heaven
remainder with no marker.

**How it fails.** An owner is suspended for two days over a payment dispute. Fifty orders are
recorded as ownerless. After reinstatement neither side can reconstruct what the club produced
during those days, and the dispute is unresolvable from the ledger.

**Fix.** Always record `owner_id`, and record `owner_percent = 0` plus an explicit
`owner_suspended` flag when suspended. Association and entitlement are separate facts and should
be stored separately.

---

### D‑16 · Self-cancellation can race a payout it was meant to be blocked by

**LOW** · Read from source

```
handlers/staff.py:503-517 — _cancel_blocked_text(order) reads a stale row
handlers/staff.py:610     — then db.cancel_order(order_id)
db.py:431-442             — cancel_order() checks only `cancelled_at IS NULL`
```

**What.** The block on cancelling a paid order is enforced by reading the order row and
inspecting the payout columns, then issuing a conditional `UPDATE` that does not re-check them. A
payout landing in the gap slips through.

**Why it matters.** `SPEC.md §5` states a paid share blocks self-cancellation absolutely. The
window is small — it needs a payout in the same instant as a self-cancel inside the 15-minute
window — but the guard is a check-then-act where a conditional write was already available.

**How it fails.** A paid order is marked cancelled. Its share stays inside a completed payout
while the order is excluded from all future calculations, so payout totals no longer reconcile
against order rows.

**Fix.** Add `AND admin_payout_id IS NULL AND owner_payout_id IS NULL` to the `UPDATE` for the
self-cancel path (VR Heaven's path deliberately allows it), and branch on `rowcount`.

---

### D‑17 · Price is frozen at quote time rather than at payment, contrary to the spec

**LOW** · Read from source

```
handlers/staff.py:365-372 — quote() is computed and stored in FSM at duration choice
handlers/staff.py:439     — order_payment() uses data["price"] verbatim
SPEC.md §2 — «Цена всегда выводится из текущих настроек в момент оформления»
```

**What.** The discount schedule and price table are evaluated when the duration button is
pressed, not when payment is confirmed. A screen left open across the 16:00 discount boundary
records the discounted price.

**Why it matters.** Quoting the customer a price and honouring it is defensible business
behaviour — but it is not what the spec says, and the divergence is undocumented. The same gap
applies to a price change made by VR Heaven between quote and confirmation.

**How it fails.** Small and bounded: the FSM holds one pending order, so the exposure is one
stale-priced order per admin. It becomes a reporting puzzle when `discount_percent` on the order
does not match the schedule at `created_at`.

**Fix.** Decide the rule and write it down. If quote-time pricing is intended, amend `SPEC.md §2`
and store the quote timestamp on the order. If payment-time pricing is intended, re-quote in
`order_payment` and show the change before recording.

---

### D‑18 · Stale copy and dead fields

**LOW** · Read from source

- **Stale UI** — `handlers/vrheaven.py:1355`: the discount-days screen still prints «Скидка не
  применяется к акции 3=4», naming a promo that was generalised away and may not exist. It should
  say that discounts never apply to any promo.
- **Dead column** — `orders.admin_percent` and `users.percent` for administrators are always `0`
  under the ladder model (`db.py:52`), yet `users.csv` exports the column as «доля_%» and
  `orders.csv` carries it. Every administrator row reads 0 %, which looks like a bug to whoever
  opens the file.
- **Stale docstring** — `utils.py:80-95`: `parse_percent` documents a paired-percentage
  constraint that no longer exists, and accepts `100`, which `SPEC.md §3` forbids. The handler
  rejects it at `handlers/vrheaven.py:514` via `_owner_percent_error` (`:345`), so the defect is
  confined to the utility, but the next caller will inherit it.
- **Undocumented limit** — `utils.py:125-136`: `parse_time_range` requires start < end, so a
  discount window cannot cross midnight. A night-shift promotion (22:00–02:00) is
  unrepresentable, and no screen says so.

**Fix.** Correct the copy; drop `admin_percent` from admin-facing exports or label it
«историческая доля»; tighten `parse_percent` to `[0, 100)`; document or support the
midnight-crossing window.

---

## 4. Design weaknesses

Category 2 — not yet broken, but structurally likely to cause problems.

---

### W‑1 · The data layer has no transaction boundaries

**HIGH**

**Shape.** One shared `aiosqlite` connection; every method ends in its own `commit()`. Because
aiosqlite serialises statements on a single thread but each `await` yields, a coroutine's
`commit()` commits whatever another coroutine has left pending. There is no `BEGIN IMMEDIATE`
anywhere in `db.py`.

**Consequence.** Every multi-statement business operation — payout close
([D‑13](#d13--create_payout-has-no-transaction-boundary)), order creation with its ladder read
([D‑6](#d6--concurrent-orders-from-one-account-collide-on-the-salary-ladder-step)), cancellation
with its payout check ([D‑16](#d16--self-cancellation-can-race-a-payout-it-was-meant-to-be-blocked-by)) —
is an unprotected read-modify-write. `make_backup` even calls `db.conn.commit()` directly at
`scheduler.py:130`, committing arbitrary in-flight work before `VACUUM INTO`.

**Also.** `journal_mode` is `delete`, not WAL. With one connection there is no contention, but
`VACUUM INTO` and any external `sqlite3` session on the server can block each other. FK
enforcement *is* correctly active (verified: orphan insert rejected).

**Direction.** An explicit transaction context manager plus an `asyncio.Lock` on the connection;
WAL mode; and a rule that no handler composes two `db` calls where one reads what the other
writes.

---

### W‑2 · FSM state lives in memory and dies with the process

**HIGH**

**Shape.** `main.py:38` constructs `Dispatcher()` with defaults: `MemoryStorage` and
`DisabledEventIsolation`. aiogram's own docstring reads: *“This storage is not recommended for
production use, as all data is lost when the bot restarts.”*

**Consequence.** Every deploy — `rsync` then restart, which is the documented workflow —
discards every in-flight flow. An administrator mid-order loses the pending price silently: the
next tap on «Оплата получена» no longer matches the state filter and falls through to
`stale_callback`, which in `handlers/staff.py:765` calls `cb.answer()` with **no text at all**.
The screen jumps to the menu, the order is not recorded, and nothing explains why.

**Tension.** Fixing this by moving to a persistent store simultaneously removes the accidental
atomicity that currently prevents duplicate orders
([D‑7](#d7--double-tapping-оплата-получена-raises-an-unhandled-keyerror-and-hangs-the-button)).
Event isolation must land in the same change, not after it.

**Direction.** Persist FSM state in the existing SQLite file (a small custom `BaseStorage` avoids
adding Redis), enable `SimpleEventIsolation`, and give `stale_callback` an explicit message.

---

### W‑3 · Hand-escaped MarkdownV2 plus a fake bot that never validates it

**HIGH**

**Shape.** Roughly a hundred message literals carry manual backslashes (`«Учёт VR\-сеансов»`,
`«\(применена автоматически\)»`). Correctness depends on a human escaping every reserved
character in every string, forever. `tests/helpers.py`'s `FakeBot` records `text` verbatim and
validates nothing — not entity syntax, not the 4096-character limit.

**Consequence.** The single most severe defect in this report
([D‑1](#d1--a-negative-vr-heaven-remainder-silently-kills-every-super-admin-order-notification))
is invisible to 244 passing tests, as is
[D‑8](#d8--unbounded-free-text-overflows-the-4096-character-message-limit-and-breaks-the-window).
A conservative MarkdownV2 validator wired into `FakeBot` for this audit found zero violations on
covered paths and immediately caught D‑1 on the uncovered one — the technique costs about twenty
lines.

**Direction.** Switch to HTML parse mode with a single escaping helper applied at interpolation,
or adopt `aiogram.utils.formatting` so entities are constructed rather than spelled. Then assert
in `FakeBot` that every outgoing text parses and fits.

---

### W‑4 · No audit trail for any money-affecting action

**HIGH**

**Shape.** The schema records *what* happened and never *who* did it. There is no `cancelled_by`
on orders, no `created_by` on payouts or bonuses, and no history for `settings`. Accountability
rests entirely on push notifications, which are transient by construction — and which the window
model deletes ([D‑2](#d2--the-window-model-destroys-the-telegram-backup-document)).

**Consequence.** With more than one super-admin there is no way to answer “who cancelled order
412?”, “who awarded this 5 000 ₽ bonus?”, or “when did the 60-minute price change and who changed
it?”. For a system whose stated purpose is «кто кому сколько должен», this is the gap most likely
to end in a dispute nobody can settle.

**Direction.** An append-only `audit_log` table: actor Telegram id, action, entity type and id,
before/after JSON, timestamp. Write to it inside the same transaction as the change. Surface the
last N entries per entity in the VR Heaven card, and add it to the CSV export.

---

### W‑5 · Super-admin access is a single Telegram ID in a file on the server

**HIGH**

**Shape.** `ADMIN_IDS` is read once at start-up (`config.py:27-31`) and cannot be changed from
inside the bot. The working copy's `.env` contains exactly one id. Adding or replacing a
super-admin means editing a file over SSH and restarting the service.

**Consequence.** A lost phone, a deleted Telegram account, or a stolen session and the business
loses all administrative access to its own ledger until someone with server credentials
intervenes. There is no second super-admin as a check on the first, and no rotation path.

**Direction.** Keep `ADMIN_IDS` as the bootstrap/recovery mechanism, but store the operative
super-admin roster in the database so it can be managed in-bot with an audit entry. Require at
least two ids at start-up and refuse to start with one.

---

### W‑6 · Backup strategy has one disk and one ephemeral chat

**MEDIUM**

**Shape.** Copies land in `BACKUP_DIR` beside the live database on the same volume, plus a
Telegram document that the bot deletes
([D‑2](#d2--the-window-model-destroys-the-telegram-backup-document)). Retention is 14 daily files
with no weekly or monthly tier. Restore is a manual «подложить файл» with no verification step
and no documented drill.

**Consequence.** Volume loss, an accidental `rm`, or ransomware takes the database and every
backup together. Silent corruption discovered after 14 days is unrecoverable. As the database
grows the Telegram path also hits the 50 MB bot upload ceiling, and `backup_job` will start
logging warnings that nobody reads.

**Direction.** Push each backup off-host (S3-compatible object storage or a second server) with
grandfather-father-son retention. Run `PRAGMA integrity_check` on the copy after writing and
alert on failure. Rehearse a restore and write the drill into the README.

---

### W‑7 · No abuse resistance on an endpoint open to every Telegram user

**MEDIUM**

**Shape.** Any user can `/start`, which writes a row into `windows` and sends a message.
`login_handle` answers «Логин не найден» for unknown handles — a clean account-enumeration
oracle. Password attempts reset by simply restarting the flow, so the 5-attempt cap is not a
lockout. `verify_password` runs PBKDF2 with 200 000 iterations **on the event loop**, measured at
**21 ms per attempt**.

**Consequence.** Passwords themselves are strong (10 characters from a 54-symbol alphabet,
≈2⁵⁷), so guessing is not the threat. Availability is: a modest flood of login attempts blocks
the event loop for the whole bot while every real administrator's order screen stalls, and the
`windows` table grows without bound.

**Direction.** A throttling middleware keyed by chat id; move PBKDF2 to a thread executor; return
an identical message for unknown and wrong credentials; prune `windows` rows for chats with no
bound user; add a per-chat cool-down after repeated failures.

---

### W‑8 · The single-window invariant fights the product

**MEDIUM**

**Shape.** “Exactly one bot message per chat” is enforced by deleting whatever was there. It is
elegant on paper and it is the root cause of
[D‑2](#d2--the-window-model-destroys-the-telegram-backup-document) and
[D‑5](#d5--an-incoming-notification-deletes-the-administrators-open-payment-screen). It also
breaks under concurrency: two simultaneous pushes to one super-admin both read the same
`window_id`, both delete it, and both send — leaving two live messages and one orphan.

**Consequence.** The invariant costs the user their chat history. There is no scrollback of
orders, no record of what a notification said, and no way to keep a backup file. For a ledger, an
append-only conversation is arguably the more useful artefact.

**Direction.** Keep one *interactive* window but stop deleting non-interactive content:
notifications, documents, and reports persist; only the menu/flow message is edited in place.
Guard window updates with a per-chat lock.

---

### W‑9 · Nothing scales past a few dozen accounts

**MEDIUM**

**Shape.** No pagination anywhere: `users_list_kb` and `payout_pick_kb` emit one button per
account, `last_orders` is capped at 10
([D‑10](#d10--vr-heaven-cannot-cancel-an-order-older-than-the-last-ten-contrary-to-the-spec)),
report tables truncate to the last 25 rows while totals cover everything. `admin_recent_orders`
fetches 10 then filters by the cancel window, so an admin with 11 orders in 15 minutes cannot
reach the eleventh.

**Consequence.** At current scale this is invisible. At 40 clubs the payout screen becomes an
unusable wall of buttons and eventually exceeds Telegram's keyboard limits. Queries are unindexed
for the summary aggregates but the row counts are tiny — this is a UI scaling problem, not a
database one.

**Direction.** Paginate every list; add search by handle; make the order list per-administrator
and time-bounded.

---

### W‑10 · Smaller structural risks

**LOW**

- **Migrations** — detected by sniffing for a column (`db.py:211` and `db.py:215`). No
  `user_version`, no ordered migration list, no down path. The third migration will not fit this
  pattern.
- **Dead chats** — `my_chat_member` is not handled, so a user who blocks the bot stays in
  `user_chats` forever and every push to them fails and is logged.
- **Chat type** — no private-chat filter. If a super-admin adds the bot to a group, or staff log
  in from one, credentials and full revenue splits render in a shared chat.
- **DST** — `pricing.series_start` does naive wall-clock arithmetic on an aware datetime. Correct
  for zones without DST (`Europe/Moscow`, `Asia/Novosibirsk`); wrong by an hour twice a year for
  any zone that has it — and `TIMEZONE` is configurable.
- **Typos in prices** — `parse_amount` accepts anything up to 100 000 000 with no confirmation
  step. Entering `30000` instead of `300` silently reprices every subsequent order, and only
  [W‑4](#w4--no-audit-trail-for-any-money-affecting-action)'s missing audit log would have shown
  when.
- **Remainder understated** — `db.py:587-606`: for an order whose administrator has been deleted,
  the unpayable admin share is still subtracted from the VR Heaven remainder, understating it.
- **Callback ordering** — `order_payment` answers the callback only after every push has been
  attempted. Under slow delivery this can exceed Telegram's callback deadline, leaving the admin
  with no confirmation for an order that *was* recorded.

---

## 5. End-user experience

The bar set by `SPEC.md §9` is “simple, direct, predictable, needs no explanation”. Much of the
copy meets it — «К оплате: X ₽» in bold above a single «Оплата получена» button is exactly right,
empty states are written rather than blank, and the automatic discount correctly asks the
administrator to decide nothing. The problems are structural rather than verbal.

### The order flow

| Moment | What the admin sees | Problem | Fix |
|---|---|---|---|
| Тип заказа | «Стандартный сеанс» + «Отмена» when no promos are active | A screen with one real choice. Pure friction on every single order. | Skip straight to headsets when `list_promos()` is empty. |
| Сколько шлемов | «1 шлем» · «2 шлема» · «Отмена» | No «Назад». The duration screen has one; this one does not. Getting back to promos means abandoning to the main menu. | «Назад» on every step of the flow. |
| К оплате | Price, PC-bonus reminder, «Оплата получена» · «Отмена» | “Отмена” is ambiguous — abandon the order, or go back a step? It silently returns to the main menu with no confirmation, after the price was quoted. | Rename to «Отменить заказ» and confirm, or split into «Назад» and «Отменить». |
| К оплате | The screen disappears | [D‑5](#d5--an-incoming-notification-deletes-the-administrators-open-payment-screen) — a push deletes it mid-payment. The single worst UX failure in the bot. | Protect money-critical screens from replacement. |
| Заказ оформлен | «Отменить заказ можно в течение 15 минут» — as text, not a button | Cancelling means menu → «Отменить заказ» → find it in a list, under time pressure with a customer waiting. | Put «Отменить этот заказ» directly on the result screen. |
| Заказ оформлен | «Ваше вознаграждение: 150 ₽ (№3 в серии)» | The admin's own pay is displayed on a screen held at the counter, facing the customer. | Move earnings to «Моя статистика»; keep the receipt screen about the order. |
| After a restart | Screen jumps to the menu, nothing said | [W‑2](#w2--fsm-state-lives-in-memory-and-dies-with-the-process) — `stale_callback` answers with empty text. The order is not recorded and the admin is not told. | Persist FSM state; give the fallback an explicit message. |

### Everywhere else

- **A suspended administrator is told their login does not exist.** `get_user_by_handle` filters
  on `is_active = 1`, so suspension is indistinguishable from a typo (`db.py:274-281`). The admin
  retypes, then calls support. Suspension deserves its own message: «Учётная запись
  приостановлена. Обратитесь в VR Heaven: @VrHeaven».
- **Suspension is invisible until it blocks something.** A suspended admin sees a normal menu and
  only learns the truth when «Новый заказ» refuses. Show the state in the menu header.
- **“Период” is never defined for staff.** «Моя статистика» stacks three different time windows —
  “Сегодня” (local midnight), the series (12-hour reset), and the current period (open until
  someone pays out) — with nothing explaining any of them. Payout dates appear only inside a
  new-order push. Add one line: «Период закрывается выплатой; плановые выплаты — 1-го и 15-го».
- **No support entry point in any menu.** `@VrHeaven` appears only inside error messages. A
  confused user in a working menu has nowhere to go. Add «Поддержка» to both staff menus.
- **The one-time password is one wrong tap from gone.** It renders into the window alongside the
  full account card keyboard; pressing «К списку» destroys it, and regenerating unbinds every
  device again. Show the password on a dedicated screen whose only button is «Пароль передан».
- **Promo prices cannot be edited** — only deleted and recreated (`keyboards.py:202-210`), which
  quietly changes the promo id that new orders reference.
- **Bonuses cannot be negative** (`parse_amount` requires > 0), so there is no way to record a
  deduction. In practice deductions will be handled off-book, defeating the ledger.
- **Tapping a promo in settings goes straight to deletion.** `promos_kb` (`keyboards.py:207`) maps
  the promo button to `pr:del:`. A confirmation screen follows, but the button reads like “open”,
  not “delete”.
- **The VR Heaven menu leads with «Отменить заказ»**, the rarest and most destructive action,
  above «Сводка», the most frequent one.

---

## 6. Operations and delivery

---

### O‑1 · There is no version control

**CRITICAL**

**What.** The working tree contains no `.git` directory. `.claude/settings.local.json` records
the deployment as `rsync -az --delete` from this laptop to
`root@150.241.105.197:/root/vrheaven-admin-bot/`, and a previously-run command on the server
confirms it is not a repository there either.

**Why it matters.** No history, no diffs, no blame, no branches, no rollback, no review, no CI. A
bad deploy can only be undone by remembering what changed and typing it back. The laptop's
working copy is the single authoritative artefact of the entire system.

**How it fails.** Laptop lost or disk failure and the source is gone — the server has a copy, but
with no history and no way to tell a deliberate change from a mistake. A regression shipped on
Friday cannot be reverted on Saturday.

**Fix.** `git init`, commit, push to a private remote. Tag releases. Deploy by checking out a tag
on the server rather than mirroring a directory. This is the highest-leverage single action
available and costs an afternoon.

---

### O‑2 · The production bot token lives in the developer working copy

**HIGH**

**What.** `.env` in the project root holds a live 46-character bot token and a single
`ADMIN_IDS` entry. There is no separate staging bot, and no `ENV`/`DEBUG` switch distinguishing
local from production.

**Why it matters.** Telegram permits one long-poll consumer per token. Running `uv run main.py`
locally — the exact command the README gives — makes the laptop and the server fight over
`getUpdates`. Neither `main.py` nor the systemd unit handles `TelegramConflictError`, so the
outcome is a flapping bot and lost updates.

**How it fails.** A developer runs the bot to test a change. Orders placed in that window are
handled by the laptop against a stale local database, or dropped entirely.

**Fix.** Register a second bot for development and keep its token in `.env`; move the production
token to the server only, ideally into a systemd credential rather than a world-readable file.
Rotate the current token, since it has been sitting in a synced working directory.

---

### O‑3 · The documented deployment is not the actual deployment

**MEDIUM**

**What.** The README's systemd unit specifies service `tgbot-admins`, `User=tgbot`, and
`WorkingDirectory=/opt/tgbot_for_admins`. The recorded deployment commands target service
`vrheaven-admin-bot` in `/root/vrheaven-admin-bot`, deployed over SSH as `root`, with a recorded
`chown -R root:root` on the directory. The unit file itself was not read in this session, so the
effective `User=` is inferred rather than confirmed — but the deployment path is unambiguously
inside `/root`.

**Why it matters.** Two problems in one. A process that needs nothing but one SQLite file and
outbound HTTPS is deployed into `/root` with root-owned files — an unnecessary blast radius
whatever the unit's `User=` turns out to be. And the only written operational documentation
describes a different service name, user, and path, so anyone recovering the service under
pressure follows instructions that do not match reality.

**Fix.** Read the live unit first, then reconcile: dedicated service user, deployment out of
`/root`, and systemd hardening (`NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`,
`ReadWritePaths` limited to the data directory). Then rewrite the README section to the unit that
actually runs, and keep it in the repository so it cannot drift again.

---

### O‑4 · Diagnostics stop at journalctl

**MEDIUM**

**What.** `logging.basicConfig(level=INFO)` and nothing else. No structured fields, no
correlation between a log line and a chat or order, no metrics, no health check, no alerting.

**Why it matters.** Every finding in this report that fails *silently* —
[D‑1](#d1--a-negative-vr-heaven-remainder-silently-kills-every-super-admin-order-notification),
[D‑12](#d12--rate-limit-and-network-errors-are-swallowed-as-ordinary-delivery-failures), the
swallowed delivery errors — surfaces only as a `WARNING` in a journal nobody watches. There is no
signal that would have told anyone D‑1 was happening.

**Fix.** Log with structured context (chat id, user handle, order id). Count delivery failures
and parse errors, and push a daily digest to super-admins — the bot already has a notification
channel. Alert on repeated `ERROR`s and on backup failure.

---

### O‑5 · Housekeeping

**LOW**

- **Stale artefact** — `adminbot.db` sits in the project root: an old-schema database from July
  with 2 users, 9 orders, and their password hashes. It is not production data, but it is
  credential-adjacent material in a directory that gets synced.
- **No CI** — tests run only when someone remembers. No linter (`ruff` is not even installed in
  `.venv` despite `.ruff_cache` being gitignored), no type checking, no pre-deploy gate.
- **Dependencies** — `aiogram>=3.29`, `aiosqlite>=0.20`, `python-dotenv>=1.0` have no upper
  bounds. `uv.lock` pins them today, so this is latent rather than live. aiogram 3.29.1 targets
  Bot API 10.1 — a very recent surface (see [§8](#8-unknowns)).
- **Shutdown** — `main.py:51-53` closes the database in a `finally` block while handler tasks may
  still be running, since `handle_as_tasks=True` leaves them unawaited. Low probability, but it is
  a partial-write window on every restart.

---

## 7. Optional improvements

Category 3 — worthwhile, not required for release.

- **Order lookup by number** for VR Heaven, which also resolves
  [D‑10](#d10--vr-heaven-cannot-cancel-an-order-older-than-the-last-ten-contrary-to-the-spec)
  properly rather than by widening a list.
- **Editable promo prices** and an explicit archive action, separating “change the price” from
  “retire the promo”.
- **Negative bonuses (deductions)** with a mandatory comment, so corrections stay inside the
  ledger.
- **A confirmation step on price edits** showing old → new, which turns a mistyped zero from a
  silent repricing into a caught mistake.
- **Per-device management**: show bound chats in the account card with last-seen, and allow
  revoking one device without a full password reset.
- **Shift summary for administrators** at series reset — orders, turnover, earnings for the
  closing 12 hours.
- **Owner-side transparency**: show which orders are already paid out, not only the open period.
- **Idempotency keys on order creation**, so a retried callback provably cannot double-write
  regardless of dispatcher behaviour.
- **Decimal money** instead of `REAL`: store kopecks as `INTEGER`. The current rounding discipline
  is genuinely careful and well-tested, but integers remove the class of problem entirely.
- **Property-based tests** over the split and ladder invariants, and a golden-file test for CSV
  exports.
- **Type checking and linting** in CI: `ruff` plus `mypy --strict` on `pricing.py`, `db.py`, and
  `utils.py` at minimum.

---

## 8. Unknowns

Category 4 — questions the repository cannot answer, and how to close them.

**U‑1 · Rich Messages against real clients.** `SendRichMessage`, `InputRichMessage`, and
`editMessageText(rich_message=…)` all exist in the installed aiogram 3.29.1 (Bot API 10.1) —
verified in `.venv`, not assumed. What is unverified: whether `<p>`/`<br>`/`<b>`/`<table>` render
as the code expects, how older Telegram clients degrade, and whether Telegram objects to the
`parse_mode=MarkdownV2` default that aiogram attaches to a `rich_message` edit carrying no `text`.
Every table screen in the product depends on this. *Close it with* a live smoke test against a
staging bot on old and new clients.

**U‑2 · Converting between message kinds.** Whether Telegram permits editing a text message into
a rich one and back. `render` falls back to delete-and-resend, so failure is survivable but
visibly janky. *Close it with* the same smoke test.

**U‑3 · Production scale.** Order rate, club count, and database size were not measured. This
determines whether the pagination limits in
[W‑9](#w9--nothing-scales-past-a-few-dozen-accounts) and the 50 MB backup ceiling in
[W‑6](#w6--backup-strategy-has-one-disk-and-one-ephemeral-chat) are theoretical or imminent.
*Close it with* row counts and file size from the live database.

**U‑4 · Production `ADMIN_IDS`.** The working-copy `.env` has one id. The server's was not read in
this session. If it is also one,
[W‑5](#w5--super-admin-access-is-a-single-telegram-id-in-a-file-on-the-server) is an active single
point of failure rather than a latent one.

**U‑5 · Existing off-host backups.** Whether anything outside this repository's mechanism copies
the database elsewhere — a hosting-provider snapshot, for instance — materially changes the
severity of [D‑2](#d2--the-window-model-destroys-the-telegram-backup-document) and
[W‑6](#w6--backup-strategy-has-one-disk-and-one-ephemeral-chat).

**U‑6 · Chat context in the field.** Whether any staff member has logged in from a group chat.
Nothing in the code prevents it, and the consequence is credentials and revenue splits rendered
where colleagues can read them.

**U‑7 · Whether anyone has noticed D‑1.** Super-admins may already have stopped seeing order
notifications. One command answers it and calibrates how long the oversight channel has been dark:

```bash
ssh root@150.241.105.197 'journalctl -u vrheaven-admin-bot | grep -c "Не удалось уведомить VR Heaven"'
```

---

## 9. Priority register

### Critical blockers

| ID | Issue | Effect if shipped | Effort |
|---|---|---|---|
| O‑1 | No version control | No rollback, no history, single-copy source | 0.5 d |
| D‑1 | Negative remainder breaks super-admin notifications | Oversight channel silently dark on loss-making orders | 0.5 d |
| D‑3 | Start-up backup overwrites the pre-migration copy | No rollback point on the riskiest operation | 0.5 d |
| D‑2 | Telegram backup document self-deletes | Only off-host copy is ephemeral | 0.5 d |
| D‑4 | Interrupted migration orphans all orders | Silent total loss of order history | 1 d |
| O‑2 | Production token in the working copy | Local run hijacks the production bot | 0.5 d |

### High priority

| ID | Issue | Effect | Effort |
|---|---|---|---|
| D‑5 | Push deletes the open payment screen | Unrecorded orders after cash is taken | 1 d |
| W‑1 · D‑13 | No transaction boundaries | Non-atomic payouts; interleaved commits | 2 d |
| D‑6 | Ladder step collides under concurrency | Administrators silently underpaid | 0.5 d |
| W‑2 · D‑7 | Volatile FSM, no event isolation | Lost orders on restart; hung buttons | 1.5 d |
| W‑3 | Hand-escaped MarkdownV2, unvalidated in tests | Whole class of formatting bugs untestable | 2 d |
| D‑8 | Unbounded text overflows the message limit | Broken window, bonus with no confirmation | 0.5 d |
| W‑4 | No audit trail | Money disputes unresolvable | 2 d |
| W‑5 | Single super-admin, config-only | Total lockout risk | 1 d |
| D‑12 | Rate limits swallowed as delivery failures | Payout-day reports silently dropped | 1 d |
| O‑3 | Deployed into /root; README does not match reality | Unnecessary blast radius; wrong runbook | 0.5 d |

### Medium priority

- D‑9 callback parsing and a global error handler · D‑10 order lookup beyond the last ten ·
  D‑11 Cyrillic-safe promo uniqueness with a database constraint · D‑14 honest payout-day
  wording · D‑15 preserve owner association during suspension
- W‑6 off-host backups with tiered retention and integrity checks · W‑7 throttling, off-loop
  PBKDF2, no account enumeration · W‑8 stop deleting non-interactive messages · W‑9 pagination
- O‑4 structured logging and an alert path · O‑5 CI with tests, `ruff`, and dependency bounds
- UX: back buttons throughout, cancel-from-receipt, suspension messaging, a support entry in
  every menu, a defined “period”

### Low priority

- D‑16 conditional self-cancel · D‑17 settle and document the quote-time pricing rule ·
  D‑18 stale copy, dead columns, stale docstrings
- W‑10 migration versioning, `my_chat_member`, private-chat filter, DST-safe series arithmetic,
  price-change confirmation, remainder accounting for deleted administrators
- Everything in [§7](#7-optional-improvements), and removing `adminbot.db` from the working tree

---

## 10. Target design

Not a rewrite. The domain model is sound; these are the layers around it.

### Keep as-is

The pricing and split logic, the frozen-share model, soft delete and soft cancel, the independent
two-sided payout, the conditional-`UPDATE` payout close, and the CSV export shape. All of it is
coherent and well-tested. `SPEC.md` is unusually good and should stay the source of truth — most
gaps in this report are places where the code drifted from it, not places where it is wrong.

### Data layer

- A `Database.transaction()` async context manager (`BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`)
  plus an `asyncio.Lock`. Every read-modify-write goes through it; no method commits on its own.
- WAL mode and an explicit `busy_timeout`.
- Ordered, versioned migrations keyed on `PRAGMA user_version`, each atomic, with a refusal to
  start on an unknown version.
- An append-only `audit_log` written inside the same transaction as every money-affecting change.
- Database-level constraints where the invariant is a database invariant: partial `UNIQUE` on
  active handles and active folded promo names.
- Money as integer kopecks (worthwhile, not urgent — the current discipline holds).

### Presentation layer

- One rendering module owning escaping. HTML parse mode with a single escape helper, or
  `aiogram.utils.formatting`. No hand-typed backslashes anywhere.
- A length guard at the boundary: no outgoing message can exceed the limit, and truncation is
  explicit rather than a failed request.
- The window model narrowed: one interactive window, edited in place; notifications, documents,
  and reports append and persist. Money-critical screens are never replaced by a push.
- Per-chat locking on window updates.

### Dispatch and reliability

- `SimpleEventIsolation` plus SQLite-backed FSM storage, landed together.
- A global `dp.errors` handler that logs with context and always answers the callback with a
  human message.
- An outgoing delivery service: token-bucket throttle, explicit `TelegramRetryAfter` retry,
  permanent failures (`Forbidden`, `NotFound`) unbinding the chat.
- Idempotency keys on order creation.

### Operations

- Git, tags, and a private remote. Deploy = checkout a tag + restart, with tests as a gate.
- Separate staging and production bots and databases.
- Dedicated service user, hardened systemd unit, no root.
- Backups: pre-migration immutable snapshot, atomic write-then-rename, off-host push, tiered
  retention, integrity check, and a rehearsed restore drill in the README.
- Structured logs, a daily health digest to super-admins, alerts on backup failure and repeated
  errors.

### Testing

- `FakeBot` validates entity syntax and message length on every send — this alone would have
  caught the top finding.
- Concurrency tests that drive real concurrent handler invocation rather than modelling the
  dispatcher.
- A live smoke test against the staging bot covering Rich Messages, document delivery, and the
  window transitions.
- Property-based tests over split and ladder invariants; golden files for CSV.

---

## 11. Roadmap

Five phases. Each ends in a state that is strictly safer than the one before it.

### Phase 0 — Stop the bleeding · 1–2 days

Nothing here changes behaviour beyond making failures visible and recoverable. Ship it before
touching anything else.

- `git init`, commit the current tree, push to a private remote, tag `v0-audit-baseline`.
- Fix **D‑1** — one-line change in `fmt_num`, plus a regression test.
- Fix **D‑3** — back up before `db.init()`, to a distinct immutable name, write-then-rename.
- Fix **D‑2** — take the backup document out of the window model.
- Rotate the bot token; register a staging bot; remove `adminbot.db` from the tree.
- Run the `journalctl` grep to close **U‑7**.

**Exit:** rollback exists, super-admins see orders again, and a recoverable snapshot precedes
every deploy.

### Phase 1 — Make the money layer atomic · 1 week

- Transaction context manager and connection lock; WAL. Route `create_payout`, order creation,
  and cancellation through it (**W‑1**, **D‑13**, **D‑6**, **D‑16**).
- Versioned migrations on `user_version`, atomic, with row-count verification (**D‑4**).
- `SimpleEventIsolation` and SQLite-backed FSM storage in one change (**W‑2**, **D‑7**).
- Global error handler; safe callback parsing (**D‑9**).
- Concurrency tests that invoke handlers concurrently for real.

**Exit:** no money operation can be observed half-done; a restart mid-flow loses nothing.

### Phase 2 — Make the interface trustworthy · 1 week

- Centralised escaping and a length guard; `FakeBot` validates every send (**W‑3**, **D‑8**).
- Narrow the window model; protect money-critical screens from pushes (**D‑5**, **W‑8**).
- Delivery service with throttling and retry (**D‑12**).
- UX pass: back buttons, cancel-from-receipt, suspension messaging, support entry, defined
  “period”, honest payout-day wording (**D‑14**), password hand-off screen.
- Live smoke test on the staging bot to close **U‑1** and **U‑2**.

**Exit:** no screen can be lost mid-order; no message can fail to render because of its content.

### Phase 3 — Make it operable · 1 week

- `audit_log` across cancellations, payouts, bonuses, settings, and account changes; surfaced in
  the card and the export (**W‑4**).
- Super-admin roster in the database with `ADMIN_IDS` as bootstrap; refuse to start with fewer
  than two (**W‑5**).
- Off-host backups, tiered retention, integrity check, rehearsed restore documented (**W‑6**).
- Dedicated user and hardened systemd unit; README corrected to match (**O‑3**).
- Structured logging, daily health digest, alerts (**O‑4**).
- CI: tests, `ruff`, dependency bounds, deploy gated on green.

**Exit:** every money action is attributable, backups survive the machine, and failures reach a
human.

### Phase 4 — Harden and grow · ongoing

- Throttling, off-loop PBKDF2, non-enumerable login, `windows` pruning (**W‑7**).
- Pagination and search across every list; order lookup by number (**W‑9**, **D‑10**).
- Cyrillic-safe uniqueness with database constraints (**D‑11**); preserve owner association
  during suspension (**D‑15**).
- Integer kopecks; property-based invariant tests; golden-file CSV tests.
- Feature work from [§7](#7-optional-improvements) — editable promo prices, deductions,
  per-device revocation, shift summaries.

**Exit:** the system scales past the current club count without operational surprises.

---

**Realistic sequencing.** Phase 0 is roughly two days and removes the four live production
risks. Phases 1–3 are about three weeks of focused work and take the system from “works when
nothing goes wrong” to “fails safely and says so”. Phase 4 is continuous. The one thing that
should not wait is **O‑1**: every later phase is a change to code that currently has no way to be
reverted.
