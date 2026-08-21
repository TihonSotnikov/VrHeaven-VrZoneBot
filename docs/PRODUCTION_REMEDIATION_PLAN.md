# VR Heaven revenue bot — production remediation and design plan

**Status:** design only. No code is written or changed by this document.
**Input:** [AUDIT.md](AUDIT.md) — findings D‑1…D‑18, W‑1…W‑10, O‑1…O‑5, U‑1…U‑7.
**Output contract:** every audit finding is answered here with a chosen solution, affected
components, implementation detail, dependencies, risk, and post-change behaviour.

This is an engineering design, not a summary. Where several designs are defensible, the
alternatives are stated, compared, and one is chosen with justification. Where the right answer
depends on a business decision rather than an engineering one, it is deferred to
[§11](#11-product-decisions-that-must-be-settled-first) and *not* silently decided here.

---

## Contents

1. [Executive architecture direction](#1-executive-architecture-direction)
2. [Critical and high-priority solutions](#2-critical-and-high-priority-solutions)
3. [Lower-priority solutions](#3-lower-priority-solutions)
4. [Message and UI architecture](#4-message-and-ui-architecture)
5. [Database and concurrency architecture](#5-database-and-concurrency-architecture)
6. [Backup and recovery architecture](#6-backup-and-recovery-architecture)
7. [Deployment and operational architecture](#7-deployment-and-operational-architecture)
8. [Testing strategy](#8-testing-strategy)
9. [Dependencies and implementation order](#9-dependencies-and-implementation-order)
10. [Phased roadmap](#10-phased-roadmap)
11. [Product decisions that must be settled first](#11-product-decisions-that-must-be-settled-first)
12. [Target state](#12-target-state)

---

## 1. Executive architecture direction

The audit's findings are not eighteen unrelated bugs. They cluster into **five wrong
foundations**, and almost every individual defect is a symptom of one of them. Fixing the
foundations retires the symptoms in groups; fixing the symptoms individually would leave the
system able to regenerate them.

| # | Wrong foundation | Symptoms it produced |
|---|---|---|
| F1 | **No transaction boundary.** Every repository method commits on a shared connection, so multi-statement operations are not atomic and unrelated coroutines commit each other's partial work. | D‑4, D‑6, D‑13, D‑16, W‑1 |
| F2 | **The wrong UI invariant.** “Exactly one message per chat” was enforced by deleting whatever was there. | D‑2, D‑5, W‑8, and the loss of all chat history |
| F3 | **Escaping as a human discipline.** ~100 hand-escaped MarkdownV2 literals, validated by nothing. | D‑1, D‑8, W‑3 |
| F4 | **Delivery as fire-and-forget.** `except TelegramAPIError: log` in every fan-out, no retry, no throttle, no durability. | D‑12, and the silence that hid D‑1 |
| F5 | **No operational substrate.** No VCS, no staging, no audit trail, no alerting, no restore guarantee. | O‑1…O‑5, W‑4, W‑5, W‑6 |

### The five directional decisions

**D‑I — The correct invariant is “exactly one *interactive* message”, not “exactly one message”.**
This single reframing is the core of the UI redesign ([§4](#4-message-and-ui-architecture)). The
property users actually needed — *there is never any ambiguity about where to tap* — is delivered
by “only one message in the chat carries buttons”. The property that was actually implemented —
*the bot deletes everything else* — delivered that same guarantee **and** destroyed backups
(D‑2), destroyed in-progress sales (D‑5), and erased all history. The new invariant keeps the
benefit and drops the cost.

**D‑II — Money correctness is a database property, not a timing accident.** Today no duplicate
order is created only because `MemoryStorage` happens not to yield between read and clear. That
is not a guarantee. Correctness moves into `BEGIN IMMEDIATE` transactions plus a `UNIQUE`
idempotency token on orders, so the guarantee holds regardless of dispatcher behaviour, storage
backend, process count, or retry.

**D‑III — Formatting correctness becomes structural, not vigilant.** Migrate to HTML parse mode
with a formatting helper that escapes interpolated values *by construction*, so “forgot to
escape” is not an expressible mistake. D‑1 stops being a bug class rather than a bug.

**D‑IV — Notifications become durable records, screens stay ephemeral.** The message taxonomy and
the delivery architecture are the same split: **Records** go through a persistent outbox with
retry; the **Window** is rendered inline and is always reconstructible. This is why the two
redesigns are one design.

**D‑V — Nothing ships until it can be un-shipped.** Version control, tagged releases, staging,
and a proven restore precede every other change, because every other change is a change to code
that currently cannot be reverted.

### Explicitly *not* changing

The domain core is correct and stays: the three-way split and its kopeck-exactness, the frozen
share model, soft delete and soft cancel, independent two-sided payout, the conditional-`UPDATE`
payout close, the CSV export shape, and `SPEC.md` as the normative document. This plan changes
the layers around a working ledger. It is not a rewrite, and any proposal to make it one should
be rejected.

### Scope boundary

Not recommended, and deliberately so:

- **Postgres.** SQLite is the right store for this workload (single writer, <10⁵ rows, one host).
  Migrating would add backup, availability, and connection-management burden to a project that
  cannot yet do basic ops. Revisit only if multi-host becomes a requirement.
- **Redis for FSM.** A second stateful service for a few dozen users, when the existing SQLite
  file can hold the same data with the same durability. See [§2.4](#24-fsm-persistence-and-event-isolation-w2-d7).
- **Webhooks.** Long polling is correct for a single-instance bot with no public ingress. Adds
  TLS, a reverse proxy, and a new failure mode for no benefit at this scale.
- **A microservice/queue split.** The outbox is a table, not RabbitMQ.

---

## 2. Critical and high-priority solutions

### 2.1 Database transactions and concurrency (W‑1, D‑6, D‑13, D‑16)

#### Approaches compared

| | A. Single connection, explicit transactions, async lock | B. Connection pool + WAL | C. Postgres |
|---|---|---|---|
| Write atomicity | Yes — one writer, `BEGIN IMMEDIATE` | Yes, but writers contend → `SQLITE_BUSY` handling needed | Yes |
| Read concurrency | Serialised with writes | Real | Real |
| New failure modes | None | Busy-timeouts, lock escalation | Network, availability, ops |
| Ops cost | Zero | Low | High |
| Fit for ~50 users | Correct | Unnecessary | Wrong |

**Chosen: A.** At this scale every request is sub-millisecond; serialising them costs nothing
measurable and removes an entire class of bug. WAL is still enabled — not for bot throughput, but
so an operator or a verification script can read the live database without blocking the bot.

#### Design

`db.py` gains explicit transaction control and loses per-method commits.

```python
# Connection is opened with isolation_level=None → no implicit transactions,
# no implicit commits, full manual control. This is the change that makes
# DDL transactional and therefore makes D-4 solvable at all.
conn = await aiosqlite.connect(path, isolation_level=None)
await conn.execute("PRAGMA journal_mode = WAL")
await conn.execute("PRAGMA foreign_keys = ON")
await conn.execute("PRAGMA busy_timeout = 5000")
await conn.execute("PRAGMA synchronous = FULL")   # keep: this is a ledger
```

Two access paths, and only two:

- `db.read()` — no transaction, autocommit reads. Used by reports, screens, exports.
- `async with db.write() as tx:` — acquires `self._write_lock` (an `asyncio.Lock`), issues
  `BEGIN IMMEDIATE`, yields a transaction handle, then `COMMIT` or `ROLLBACK` on exception.

**Every mutating repository method takes `tx` as its first argument and never commits.** This is
the enforcement mechanism: a write cannot be issued without a caller having opened a transaction,
because there is no other way to obtain a `tx`. Reviewer discipline is not required.

Nesting is forbidden and asserted (`RuntimeError` if the lock is already held by the same task) —
`BEGIN IMMEDIATE` inside a transaction is a silent no-op in SQLite and would produce exactly the
false sense of atomicity being removed.

**Important detail — the lock must wrap the whole transaction, not each statement.** Wrapping
statements is what the current code effectively does and is precisely why interleaving happens.

#### Operations converted to single transactions

| Operation | Statements now atomic | Retires |
|---|---|---|
| `create_order` | series count → ladder → INSERT order → INSERT audit | D‑6 |
| `create_payout` | INSERT payout → UPDATE orders → UPDATE bonuses → SUM → UPDATE amount → audit | D‑13 |
| `cancel_order` | conditional UPDATE (with payout guard) → audit | D‑16 |
| `cancel_bonus`, `create_bonus`, all user/promo/settings mutations | mutation → audit | W‑4 |

`create_order` keeps the readable two-step form (`COUNT`, then `INSERT`) rather than a clever
single-statement `INSERT…SELECT` with a `CASE` ladder — inside `BEGIN IMMEDIATE` the write lock
is already held, so the two steps are atomic, and readability wins.

`cancel_order` gains a `for_self: bool` parameter. The self-cancel path adds
`AND admin_payout_id IS NULL AND owner_payout_id IS NULL` to the `WHERE`; VR Heaven's path
deliberately does not (cancelling a paid order is an allowed supervisory action). Branch on
`rowcount`. This closes D‑16's check-then-act at the write itself rather than at a prior read.

#### Idempotency (D‑7, and durable protection for D‑6)

`orders` gains `client_token TEXT UNIQUE`. The confirmation screen generates a UUID into FSM data
when it is created; `create_order` writes it. A duplicate submission — double tap, retried
callback, restored FSM after a restart — hits the unique constraint, and the handler treats
`IntegrityError` on that constraint as *“already recorded”*, looks up the existing order, and
renders the normal receipt.

This matters because it is a guarantee rather than a timing property. The current absence of
duplicate orders depends on `MemoryStorage` not yielding; the moment FSM storage becomes durable
([§2.4](#24-fsm-persistence-and-event-isolation-w2-d7)) that accident disappears. The token
makes the behaviour correct *before* that change lands, and correct forever after.

#### Risks

- **Mechanical breadth.** ~40 repository methods change signature. Mitigated by doing it as one
  atomic commit with no behaviour change, and by the fact that omitting `tx` becomes a
  `TypeError` at import/call time rather than a silent bug.
- **A long transaction blocks the bot.** Only `VACUUM INTO` is long. It moves out of the write
  path — see [§6](#6-backup-and-recovery-architecture).
- **`SimpleEventIsolation` is in-process only.** It does not protect against a second process.
  That is acceptable *because* the DB transaction is the real guarantee, and because
  [§7](#7-deployment-and-operational-architecture) adds a single-instance lock.

#### After the change

Two devices confirming orders in the same second produce ladder steps 1 and 2, not 1 and 1. A
process killed mid-payout leaves either a complete payout or none. A paid order cannot be
self-cancelled under any interleaving. A retried confirmation produces one order and one receipt.

---

### 2.2 Migration safety (D‑4)

#### Why the current design cannot be patched

`_migrate()` detects work by sniffing for a column. Under Python's legacy `sqlite3` isolation,
`ALTER` and `CREATE` commit immediately, so the "migration needed" signal (`promo_id` missing) is
destroyed before the data is copied. The interrupted state is indistinguishable from success.

The fix is not a bigger `try/except`. It is `isolation_level=None`
([§2.1](#21-database-transactions-and-concurrency-w1-d6-d13-d16)) — **SQLite DDL is fully
transactional**, so once Python stops auto-committing it, a table rebuild becomes atomic.

#### Design

A migration is `(version: int, name: str, fn: Callable[[Connection], Awaitable[None]])` in an
ordered list. `PRAGMA user_version` is the counter.

```
for each migration with version > user_version:
    snapshot = backup(reason="pre-migration", immutable=True)     # §6
    BEGIN IMMEDIATE
      PRAGMA foreign_keys = OFF        # required for table-rebuild migrations
      fn(conn)
      PRAGMA user_version = <version>  # inside the same transaction
      row-count post-conditions
    COMMIT
    PRAGMA foreign_keys = ON
    PRAGMA foreign_key_check           # after commit; abort startup on violation
```

Four rules that carry the guarantee:

1. **The version bump is inside the transaction.** Version and data move together or not at all.
2. **Post-conditions inside the transaction.** A table rebuild asserts
   `COUNT(new) == COUNT(old)` and aborts (rolling back) if not. The audit's silent-zero-orders
   scenario becomes an abort.
3. **Downgrade protection.** If `user_version > max(MIGRATIONS)`, the bot **refuses to start**
   with an explicit Russian error. An older binary rolled back onto a newer schema must fail
   loudly, not corrupt.
4. **Immutable pre-migration snapshot**, taken before the transaction and exempt from rotation.

`foreign_keys = OFF` is set *inside* the transaction only for rebuild migrations; note that this
pragma is a no-op inside a transaction in SQLite, so it must in practice be issued immediately
before `BEGIN`. The implementer must follow the order: `PRAGMA foreign_keys=OFF` → `BEGIN
IMMEDIATE` → work → `COMMIT` → `PRAGMA foreign_keys=ON` → `PRAGMA foreign_key_check`. This is a
known SQLite sharp edge and is the reason the current code disables FKs at init time.

#### Baseline

The existing ad-hoc steps become `v1` (baseline schema, applied only to a database with
`user_version = 0` that already has the current tables — detected once, then stamped) and the
historical rebuild becomes `v2`. Production is already past both; the framework stamps it to the
current version on first run after verifying the schema matches, and all future work is
`v3`, `v4`, ….

**Dependency:** requires the connection change from §2.1 and the backup work from §6. It must
land before any schema change in this plan — and this plan adds several
(`client_token`, `audit_log`, `fsm_state`, `outbox`, `promos.name_folded`, `orders.owner_id`
semantics, `super_admins`).

#### Risk

Stamping an existing production database to a baseline version is the one irreversible-feeling
step. Mitigation: the stamp runs only after a schema comparison against the expected v2 shape
(`sqlite_master` DDL normalised and compared), and only after a pre-migration snapshot. Rehearse
on a copy of the production database in staging first — this is a mandatory Phase 1 gate.

---

### 2.3 Message formatting (D‑1, D‑8, W‑3)

#### Approaches compared

| | A. HTML + auto-escaping helper | B. `aiogram.utils.formatting` | C. Keep MarkdownV2, centralise escaping |
|---|---|---|---|
| Reserved characters | 3 (`& < >`) | none — entities are built | 18 |
| Consistency with Rich tables | Same format | Two formats in one codebase | Two formats |
| Change size | ~100 literals, mechanical | ~100 literals, structural rewrite | Small |
| Can “forgot to escape” still happen? | No, if interpolation is forced through the helper | No | Yes |

**Chosen: A.** Rich Messages already require HTML, so the bot converges on one format instead of
maintaining two. HTML's three reserved characters make accidental breakage far less likely than
MarkdownV2's eighteen even before the helper. Option B is the theoretically strongest but forces
a structural rewrite of every message *and* still leaves the Rich HTML builders in a second
paradigm — worse coherence for a marginal gain.

#### Design

A single formatting entry point where **template text is trusted and every interpolated value is
escaped automatically**:

```python
# utils/markup.py
def h(template: str, *args, **kwargs) -> str:
    """HTML message body. The template is authored by us and passed through;
    every argument is escaped. Escaping is not something a caller can forget."""
    return template.format(*(esc_html(a) for a in args),
                           **{k: esc_html(v) for k, v in kwargs.items()})
```

Call sites become `h("<b>Заказ №{}</b>\nАдминистратор: {}", order_id, admin_handle)`. There is no
supported way to interpolate an unescaped value, so D‑1's failure mode is not expressible.

Money formatting is fixed at the source regardless: `fmt_num` emits U+2212 (`−`) for negatives.
Under HTML this is cosmetic rather than load-bearing, but it is the correct typography and it
keeps the formatter safe if any text ever returns to a Markdown context.

#### Length guard (D‑8)

Two layers, because input validation alone is not enough:

1. **Input caps at entry**, with explicit Russian validation messages: account name ≤ 64, contact
   ≤ 64, bonus comment ≤ 200, promo name ≤ 40 (already enforced).
2. **A hard guard in the send layer.** Any outgoing body over 4096 (or caption over 1024) is
   truncated at a safe HTML boundary with `…`, and the truncation is logged at `ERROR` with the
   screen name. A message must never fail to send because of its own length.

Layer 2 is what actually retires D‑8, because it also covers content the caps did not anticipate
(a long account name plus a long promo name plus a long comment on one card).

**Never delete a window before its replacement is confirmed sent** — this is the other half of
D‑8 and is enforced structurally by the new render path
([§4.6](#46-window-render-algorithm)).

#### Dependency

Should land *after* the contract-checking `FakeBot`
([§8.1](#81-a-fakebot-that-enforces-the-telegram-contract)) so the migration is guarded by tests
that fail on malformed output. A one-line `fmt_num` hotfix ships in Phase 0 without waiting for
any of this.

---

### 2.4 FSM persistence and event isolation (W‑2, D‑7)

#### Approaches compared

| | A. SQLite-backed `BaseStorage` in the existing DB | B. Redis | C. Hybrid — persist only money-critical state |
|---|---|---|---|
| New infrastructure | None | A second stateful service | None |
| Backed up by existing mechanism | Yes, automatically | No — separate concern | Partially |
| Contention with money writes | Shares the write lock (sub-ms) | None | Some |
| Complexity | One small class, 4 methods | Deployment, monitoring, backup | Two state models |

**Chosen: A.** The `BaseStorage` contract is four abstract methods (`set_state`, `get_state`,
`set_data`, `get_data`) plus `close`; `update_data` and `get_value` have working defaults. A
SQLite implementation is ~60 lines. Redis for a few dozen users would add a second thing that can
be down, and a second thing that needs backing up, to a project whose ops story is the weakest
part of the system. Option C keeps two state models forever for no benefit.

#### Design

```sql
CREATE TABLE fsm_state (
    bot_id     INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    thread_id  INTEGER,
    business_connection_id TEXT,
    destiny    TEXT NOT NULL DEFAULT 'default',
    state      TEXT,
    data       TEXT NOT NULL DEFAULT '{}',   -- JSON
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, chat_id, user_id, thread_id, business_connection_id, destiny)
);
```

Writes go through `db.write()` like everything else. Data is `json.dumps`'d; a non-serialisable
value raises immediately rather than being silently dropped — tests assert this
([§8](#8-testing-strategy)).

**`events_isolation=SimpleEventIsolation()` must land in the same commit.** This is not optional
sequencing, it is a correctness dependency the audit identified precisely: `BaseStorage.update_data`
is a non-atomic get-then-set, and today it is safe only because `MemoryStorage` never yields.
A SQLite storage *does* yield, so without per-key isolation, two updates to one chat interleave
and one is lost. `SimpleEventIsolation` serialises updates per `(bot_id, chat_id, user_id)`, which
is exactly the granularity `update_data` needs.

Its in-process limitation is acceptable: money correctness rests on DB transactions and the
idempotency token, not on this lock, and the single-instance guard in
[§7](#7-deployment-and-operational-architecture) prevents a second process.

#### Housekeeping

A startup task deletes `fsm_state` rows with `updated_at` older than 7 days, so abandoned wizards
do not accumulate. FSM rows are excluded from the CSV export and are the one table it is always
safe to truncate during recovery.

#### After the change

A deploy no longer destroys an in-progress order: FSM data survives, the window payload survives
([§4](#4-message-and-ui-architecture)), and the administrator's next tap works exactly as it did
before the restart. Restarts become invisible to users rather than a silent data-loss event.

`stale_callback` still exists for genuinely stale taps, but it now answers with real text
(«Экран устарел — открыт текущий экран») instead of the current empty `cb.answer()`.

---

### 2.5 Delivery, rate limits, retries, error handling (D‑12, D‑9, W‑10 partial)

Three layers, each solving a different problem. They compose; none replaces another.

#### Layer 1 — Session request middleware (throttle + retry)

Registered on `bot.session.middleware`, so it wraps **every** outgoing API call — sends, edits,
deletes, documents, `answerCallbackQuery` — with no call-site changes and no way to bypass it.

- **Global token bucket** at ~25 msg/s (headroom under Telegram's ~30).
- **Per-chat bucket** at ~1 msg/s for private chats.
- **`TelegramRetryAfter`**: sleep `retry_after + jitter`, retry, bounded at 3 attempts.
- **`TelegramServerError` / `TelegramNetworkError`**: retry with backoff **only for idempotent
  methods** — `editMessageText`, `deleteMessage`, `answerCallbackQuery`.

**A `sendMessage` that fails with a network error is never auto-retried.** The request may have
succeeded before the connection dropped; retrying would duplicate a financial notification. Such
failures propagate to Layer 2, which owns the durable retry decision with idempotency context.
This distinction is the single most important detail in the delivery design and the easiest to
get wrong.

#### Layer 2 — Outbox for Records

Notifications and reports are queued, not sent inline:

```sql
CREATE TABLE outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    kind         TEXT NOT NULL,          -- order_created | order_cancelled | bonus | payout | report | backup | export
    payload      TEXT NOT NULL,          -- JSON: body, document ref, dedup key
    dedup_key    TEXT UNIQUE,            -- e.g. "order_created:412:chat:777"
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed | dropped
    attempts     INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error   TEXT,
    created_at   TEXT NOT NULL,
    sent_message_id INTEGER
);
```

The enqueue happens **inside the business transaction**. An order and its notifications commit
together: if the order rolls back, so do the notifications; if it commits, the notifications are
guaranteed to be attempted. A background worker drains with exponential backoff, and `dedup_key`
makes redelivery after a crash safe.

**Why an outbox rather than a `try/except` with retries.** The audit's D‑12 failure is that
payout-day reports are dropped and *nobody learns*. A retry loop still loses everything on
restart, and payout day is exactly when the fan-out is largest. With an outbox, a failed report is
a visible row, retried across restarts, and reportable: “7 сообщений не доставлены” becomes a
number an operator can see. The cost is one table and one worker.

**Scope discipline:** only Records go through the outbox. The Window is rendered inline and
synchronously, because an interactive screen that arrives “eventually” is worse than one that
fails visibly. This split is the same split as the message taxonomy in
[§4](#4-message-and-ui-architecture) — that is not a coincidence, it is the design.

#### Layer 3 — Permanent failures and dead chats

`TelegramForbiddenError` and `TelegramNotFound` are permanent, not transient: mark the outbox row
`dropped`, unbind the chat from `user_chats`, write an audit entry, and notify VR Heaven that a
device was disconnected. A `my_chat_member` handler does the same proactively when a user blocks
the bot. This retires the “every push to a blocked chat fails forever and is logged forever”
half of W‑10.

#### Global error handler (D‑9)

`dp.errors` handler that:

1. logs with structured context (`update_id`, `chat_id`, `user_handle`, `handler`, traceback);
2. **always answers the callback query** if the update was one — the audit's hung spinner is
   caused by nothing else;
3. renders a neutral window («Что-то пошло не так. Попробуйте ещё раз или напишите @VrHeaven»);
4. increments an error counter feeding the daily digest ([§7](#7-deployment-and-operational-architecture)).

Callback parsing is centralised in one helper returning `None` for anything malformed, so
`ValueError`/`OverflowError` become a toast («Кнопка устарела») rather than an exception.

#### After the change

Payout-day reports are delivered or visibly pending — never silently gone. A 429 delays a message
instead of dropping it. A blocked user is unbound once instead of failing forever. No button ever
spins without resolution.

---

### 2.6 Auditability of money-affecting actions (W‑4)

#### Design

One append-only table, written **inside the same transaction as the change it records** — so an
un-audited mutation is impossible by construction, not by convention.

```sql
CREATE TABLE audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    actor_kind   TEXT NOT NULL,          -- superadmin | staff | system
    actor_tg_id  INTEGER,                -- Telegram id, for superadmin/staff
    actor_user_id INTEGER,               -- users.id, when the actor is staff
    action       TEXT NOT NULL,          -- order.create, order.cancel, payout.create, …
    entity       TEXT NOT NULL,          -- order | payout | bonus | user | promo | setting
    entity_id    INTEGER,
    before_json  TEXT,
    after_json   TEXT,
    request_id   TEXT                    -- correlates with logs and the outbox
);
CREATE INDEX idx_audit_entity ON audit_log(entity, entity_id, id);
CREATE INDEX idx_audit_actor  ON audit_log(actor_tg_id, id);
```

**Why a log table rather than `cancelled_by` columns on each entity.** Denormalised columns answer
only the questions someone anticipated, cannot record settings changes (which have no natural
entity row to hang off), and multiply with every new action. One log answers “who touched order
412”, “what did this super-admin do last month”, and “when did the 60-minute price change and to
what” with the same query shape. Rows are tiny; retention is forever.

Covered actions: order create/cancel; payout create; bonus create/cancel; user
create/suspend/activate/delete/password-reset/percent-change/owner-change/series-reset; promo
create/archive; every settings change; super-admin roster changes; chat bind/unbind.

**Surfacing.** Without a reader this is a table nobody looks at:

- The VR Heaven account card and order-cancellation screen show the last 5 relevant entries.
- A new `audit.csv` joins the export set.
- The daily digest ([§7](#7-deployment-and-operational-architecture)) summarises the day's
  money-affecting actions per actor.

**Dependency:** requires §2.1 (there is no “same transaction” to write into otherwise).

---

### 2.7 Super-admin roster (W‑5)

`ADMIN_IDS` stops being the operative roster and becomes the **bootstrap and recovery**
mechanism only.

```sql
CREATE TABLE super_admins (
    tg_id      INTEGER PRIMARY KEY,
    label      TEXT NOT NULL DEFAULT '',
    added_by   INTEGER,
    added_at   TEXT NOT NULL,
    removed_at TEXT
);
```

- Effective roster = `ADMIN_IDS` ∪ active `super_admins`. `ADMIN_IDS` members can never be removed
  in-bot — that is the lockout escape hatch.
- Add/remove is an in-bot flow with confirmation and an audit entry.
- **Start-up refuses to run with fewer than two effective super-admins**, with an explicit Russian
  message. A single-key system is not an acceptable state for a ledger, and a hard failure at
  start is the only reliable way to force the second key to exist.

Removing yourself when you are the last is blocked at the handler with an explanatory alert.

**Product decision required** — who the second super-admin is. See
[§11](#11-product-decisions-that-must-be-settled-first).

---

## 3. Lower-priority solutions

Concise by design — these have one obvious correct answer.

| ID | Solution | Component |
|---|---|---|
| **D‑10** | Order lookup by number (`№123`) plus keyset pagination on the cancel list, scoped per administrator. Replaces the global last-10 window; the existing paid-share warning on the confirm screen already covers the safety case. | `handlers/vrheaven.py`, `db.last_orders` |
| **D‑11** | Store `promos.name_folded` (Python `str.casefold()`, which folds Cyrillic; SQLite's `lower()` does not). Add `CREATE UNIQUE INDEX … ON promos(name_folded) WHERE archived_at IS NULL`. Enforcement moves from handler to database. | `db.py` migration, `create_promo` |
| **D‑14** | Retitle the payout-day report to «Накоплено к выплате на {date}» with «Выплата проводится вручную; итоговая сумма может измениться». Keeps `SPEC.md §4`'s definition (payout closes the period) intact instead of inventing a second period concept. Alternative — actually closing the period — is rejected: it would create calendar boundaries the spec explicitly denies, and the manual-payout workflow would immediately reopen them. | `scheduler.py` |
| **D‑15** | Always write `owner_id`; record `owner_percent = 0` and a new `owner_suspended INTEGER` flag when the owner is inactive. Association and entitlement become separate facts. Historical orders remain visible to the owner with a zero share. Requires a **product decision** on retroactive treatment — see §11. | `handlers/staff.py`, `orders` schema |
| **D‑17** | Keep quote-time pricing (it is defensible: the customer was quoted that price), store `quoted_at` on the order, and **amend `SPEC.md §2`** to match. Requires the product decision in §11 before implementing. | `orders` schema, `SPEC.md` |
| **D‑18** | Fix the «акция 3=4» string to name no specific promo; drop `admin_percent` from admin-facing CSV columns or relabel «историческая доля»; tighten `parse_percent` to `[0, 100)`; document the no-midnight-crossing discount limit on the time-entry screen. | `handlers/vrheaven.py`, `export.py`, `utils.py` |
| **W‑7** | Throttling middleware keyed by chat id (inbound); `verify_password`/`hash_password` moved to `asyncio.to_thread` so PBKDF2's 21 ms stops blocking the loop; identical message for unknown handle and wrong password; `windows` rows pruned for chats with no bound user; per-chat cool-down after repeated failures. | new middleware, `utils.py`, `handlers/staff.py` |
| **W‑9** | Keyset pagination (`WHERE id < ?`) on every list — accounts, payouts, orders, bonuses — plus search by handle. Page size 8. | `keyboards.py`, `db.py` |
| **W‑10** | Private-chat filter on both routers (`F.chat.type == "private"`) with an explanatory message in groups; DST-safe series arithmetic via `datetime.combine` + `fold` handling; confirmation step on price edits showing old → new; VR remainder query counts the unpayable share of a deleted administrator as retained. | `handlers/*`, `pricing.py`, `db.py` |
| **O‑5** | Remove `adminbot.db` from the tree; add upper bounds (`aiogram>=3.29,<4`); install and configure `ruff`; graceful shutdown that awaits in-flight handler tasks before `db.close()`. | repo hygiene, `main.py` |

---

## 4. Message and UI architecture

This is the largest single design change and the one with the most alternatives worth weighing.

### 4.1 The invariant is wrong, not the goal

The single-window model was chasing a real property: **the user should never be unsure which
message is live**. It achieved that by deleting everything else, which cost the daily backup
(D‑2), an in-progress sale (D‑5), and all chat history.

The same property is delivered by a weaker, cheaper invariant:

> **Exactly one message in the chat carries buttons: the Window.**

Everything else in the chat is text or a document with no keyboard, so there is nothing else to
tap, nothing to mistake for the live screen, and no reason to delete it. A stale-button class of
bug becomes unreachable rather than defended against.

### 4.2 Message classes

| Class | Interactive | Persistent | Count per chat | Examples |
|---|---|---|---|---|
| **Window** | Yes — the only message with a keyboard | No — replaced/edited freely | Exactly 1 | Menus, account lists and cards, settings, wizard steps, confirmations, report views, the payment screen |
| **Record** | No keyboard | Yes — **never auto-deleted** | Unbounded | Order receipt, order notification to owner/VR Heaven, cancellation notice, bonus award, payout confirmation, period report, new-device warning, backup document, export documents |
| **Transient** | No | No | 0–1, briefly | The user's own input (deleted on receipt), and nothing else |
| **Toast** | — | — | — | `answerCallbackQuery` alerts: validation errors, «Кнопка устарела», «Заказ оформлен» |

The rule that makes this auditable: **the bot issues `deleteMessage` for exactly two things — the
Window, and the user's own input. Nothing else, ever.** A `deleteMessage` call anywhere outside
the window renderer or the input cleaner is a bug, and a test asserts it
([§8.4](#84-message-lifecycle-tests)).

### 4.3 Classification of every current message

Derived from the full call-site inventory:

**Become Records** (currently pushes that destroy the window, or documents wrongly made the window):

- New-order notification → owner, and → each super-admin (`_push_user_chats`, `_notify_vrheaven`)
- Order cancellation notices → owner, administrator, super-admins
- Bonus awarded / bonus cancelled → administrator
- Payout completed → recipient
- Payout-day period report → every recipient (`scheduler.py:58`)
- Payout-day summary → each super-admin (`scheduler.py:76`)
- New-device login warning (`staff.py:215`) — a security event, must persist
- **Backup document** (`scheduler.py:150`) — retires D‑2
- **Export CSVs** (`staff.py:729`, `vrheaven.py:1504`)
- **The administrator's own order receipt** — new; today the result is a window that the next tap
  overwrites

**Stay Window**: everything else — every menu, list, card, settings screen, wizard step,
confirmation, and report view.

**Notable reclassification — the order receipt.** Today the post-order screen is a Window
carrying both the receipt *and* the navigation. Splitting them gives the administrator a durable
record of every sale (which is the point of the product) and lets the Window carry the action the
audit asked for:

- **Record:** «Заказ №412 · 1 шлем · 30 мин · 300 ₽ · 14:32» — compact, one line of substance,
  no buttons, permanent.
- **Window** immediately below: «Заказ записан» + buttons «Отменить заказ №412» · «Новый заказ» ·
  «В меню».

This retires the audit's “cancel is text, not a button” finding without violating the
one-keyboard rule.

### 4.4 How a Record interacts with the Window — the core decision

Sending a Record pushes the Window out of last position. Telegram anchors the view to the bottom,
so the user now sees a notification where the live screen used to be.

| | A. Leave the Window in place | B. Re-anchor: delete and re-send the Window below the Record | C. Give Records a «В меню» button | D. Re-anchor only for “important” Windows |
|---|---|---|---|---|
| Window stays reachable | No — user must scroll up | Yes | Via an extra tap | Sometimes |
| One-keyboard invariant | Held | Held | **Broken** — two tappable messages | Held |
| In-progress order survives | Yes but hidden | Yes and visible | Yes but hidden | Unpredictable |
| API cost per Record | 0 | +2 (delete + send) | 0 | ~1 |
| Predictability | Poor | High | Medium | Poor — behaviour varies by screen |

**Chosen: B — always re-anchor.** It is the only option that keeps the live screen live, keeps the
one-keyboard invariant, and behaves identically every time. Two extra API calls per notification
is nothing at this volume, and the throttle layer already governs them. Option D's conditional
behaviour is exactly the kind of “sometimes it moves” unpredictability the product must not have.

Re-anchoring is **coalesced**: several Records delivered in one burst produce one re-anchor at the
end, inside the per-chat delivery lock.

**This is the fix for D‑5.** A bonus notification arriving mid-sale now yields: notification
appears, payment screen reappears below it with «Оплата получена» intact, FSM data untouched. The
administrator loses nothing.

**Alternative considered and rejected — suppress or defer notifications during a money-critical
flow.** It sounds protective but is worse: a cancellation notice delayed behind an open wizard is
a correctness problem, and “active flow” needs a timeout or a chat can be muted indefinitely by an
abandoned screen. Re-anchoring solves the same problem without introducing delivery semantics
that depend on UI state.

### 4.5 Window state and reconstructibility

Re-anchoring requires the Window's content to be reproducible by a component that did not create
it (the delivery worker). Two ways:

- **(i) Store the rendered payload** — `text`, `markup_json`, `is_rich` — alongside `message_id`.
- **(ii) A screen registry** — store a screen id + params and re-invoke the render function.

**Chosen: (i).** Re-anchoring becomes content-agnostic and needs no handler changes; it also gives
free crash recovery, since after a restart the Window on screen is still backed by a stored
payload. Option (ii) is architecturally cleaner but requires converting ~60 screens into
registered, replayable functions — a large refactor for a benefit ((ii)'s freshness) that is
mostly not wanted.

```sql
CREATE TABLE chat_windows (
    chat_id      INTEGER PRIMARY KEY,
    message_id   INTEGER NOT NULL,
    text         TEXT NOT NULL,
    markup_json  TEXT,
    is_rich      INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
```

**Accepted limitation:** a re-anchored report view shows the numbers it showed when rendered, not
live ones. This is acceptable and should be stated in the spec — it is a snapshot the user was
already looking at, and any button press re-queries. Money-critical screens are unaffected: the
payment screen's price is FSM-frozen by design, so re-anchoring it is exactly correct.

### 4.6 Window render algorithm

One function owns every Window transition. It replaces both `render` and `render_cb`.

```
show_window(chat_id, text, markup, *, source_message_id=None):
    async with chat_lock(chat_id):
        cur = load(chat_windows, chat_id)

        # 1. A tap on the current window edits in place — the common path.
        if source_message_id == cur.message_id:
            try: edit(cur.message_id, text, markup); save(...); return
            except NotModified: return
            except BadRequest: pass          # fall through to replace

        # 2. A tap on anything else is a stale or foreign tap.
        #    NEVER delete it — it may be a Record.
        if source_message_id and source_message_id != cur.message_id:
            toast("Экран устарел — открыт текущий экран")
            # fall through to refresh the real window

        # 3. Edit the existing window if it is still last; otherwise re-anchor.
        if cur and not must_reanchor:
            try: edit(cur.message_id, ...); save(...); return
            except BadRequest: pass

        # 4. Re-anchor: send FIRST, then delete the old window.
        new = send(text, markup)             # if this fails, the old window survives
        save(chat_id, new.message_id, text, markup)
        if cur: try_delete(cur.message_id)
```

Three properties are load-bearing:

- **Step 2 never deletes the tapped message.** Today `render_cb` deletes any non-editable pressed
  message — which is exactly how a backup document gets destroyed. Under the new model a Record
  has no buttons, so a tap on one should be impossible; the branch is defensive, and it must
  refuse to delete.
- **Step 4 sends before deleting.** Today `render` deletes first, so a failed send (D‑8's
  over-length case) leaves the chat with no window at all. Reversing the order makes the failure
  mode “stale window remains” instead of “no window exists”.
- **`chat_lock(chat_id)`** serialises all message operations for a chat. Two concurrent Records to
  one super-admin no longer both delete the same window and both send — the W‑8 concurrency half.

### 4.7 Keeping history clean

Records are permanent, so their volume is a design constraint, not an afterthought:

- Records are **compact** — a receipt is 2–3 lines, not a screen.
- **No Record for anything the actor already sees on their own Window.** The existing
  `exclude_id` logic generalises into a rule: the initiator gets a Window, everyone else gets a
  Record.
- **No redundant confirmations.** «Действие отменено», «Настройка обновлена», «Дни скидки
  обновлены» stay Toasts — they have no lasting value.
- **Wizard steps never become Records.** Prompts, validation errors, and intermediate screens are
  Window edits, exactly as today.
- **Volume, per role, per day at current scale:** an administrator gets ~1 Record per order
  (their receipt); an owner ~1 per order in their club; a super-admin ~1 per order across all
  clubs plus one backup. The super-admin firehose is the one that will not scale past a few clubs
  — see the digest decision in [§11](#11-product-decisions-that-must-be-settled-first).

### 4.8 Documents

Backups and exports are Records with a caption, no keyboard, and **no interaction with
`chat_windows` at all**. The Window is re-anchored below them like any other Record. Backup
documents are additionally marked `never_delete` in the outbox payload as documentation of intent.

This is D‑2's fix stated precisely: the backup was never a Window, and the bug was classifying it
as one.

### 4.9 Order-flow UX changes

Delivered alongside the message model, since they touch the same screens:

| Change | Rationale (audit) |
|---|---|
| Skip the order-kind screen when no promos are active | Removes a screen with one real choice from every order |
| «Назад» on every wizard step, not only duration | The headsets step is currently a dead end |
| «Отмена» on the payment screen renamed «Отменить заказ» + confirmation | “Отмена” currently reads as “go back” and silently discards a quoted sale |
| Receipt as Record, actions on the Window | Cancel becomes one tap, and the sale is permanently recorded |
| Administrator's earnings move from the receipt to «Моя статистика» | The receipt is held facing the customer |
| Suspended account gets its own login message | «Логин не найден» sends a suspended admin to support with the wrong problem |
| Suspension shown in the menu header | Currently invisible until an action is refused |
| One line defining «период» in staff reports | Three time windows on one screen, none explained |
| «Поддержка» entry in both staff menus | `@VrHeaven` currently appears only inside errors |
| Password shown on a dedicated screen whose only button is «Пароль передан» | One wrong tap currently destroys a one-time password |
| Promo tap opens a promo screen (edit price / archive), not deletion | The button currently reads “open” and means “delete” |
| VR Heaven menu reordered: «Сводка» first, «Отменить заказ» last | Most frequent action first, most destructive last |

---

## 5. Database and concurrency architecture

Consolidating §2.1, §2.2, §2.4, §2.6 into the target shape.

### Connection and pragmas

One writer connection, `isolation_level=None`, WAL, `foreign_keys=ON`, `busy_timeout=5000`,
`synchronous=FULL`. WAL is for external readers (ops queries, the restore verifier), not for bot
throughput.

### Access API

```python
rows = await db.read("SELECT …", params)          # autocommit, no lock

async with db.write() as tx:                       # asyncio.Lock + BEGIN IMMEDIATE
    order_id = await orders.create(tx, …)
    await audit.record(tx, …)
    await outbox.enqueue(tx, …)
# COMMIT here; any exception → ROLLBACK, nothing partial
```

Mutating repository functions require `tx`. There is no other way to write.

### Schema additions

| Table / column | Purpose | Finding |
|---|---|---|
| `orders.client_token TEXT UNIQUE` | Idempotent order creation | D‑7, D‑6 |
| `orders.quoted_at TEXT` | Records when the price was quoted | D‑17 |
| `orders.owner_suspended INTEGER` | Association preserved, entitlement zeroed | D‑15 |
| `promos.name_folded TEXT` + partial unique index | Cyrillic-safe uniqueness in the DB | D‑11 |
| `audit_log` | Attribution for every money action | W‑4 |
| `fsm_state` | Durable FSM | W‑2 |
| `outbox` | Durable Record delivery | D‑12 |
| `chat_windows` (replaces `windows`) | Window payload for re-anchoring | §4.5 |
| `super_admins` | In-bot roster | W‑5 |

Each is a numbered migration under the framework in §2.2.

### Concurrency model

| Layer | Mechanism | Protects |
|---|---|---|
| Update dispatch | `SimpleEventIsolation` per (bot, chat, user) | FSM `update_data` read-modify-write |
| Chat messaging | `chat_lock(chat_id)` in the render/delivery layer | Window pointer, re-anchor ordering |
| Database writes | `asyncio.Lock` + `BEGIN IMMEDIATE` | All money operations |
| Business identity | `orders.client_token UNIQUE` | Duplicate orders, independent of all of the above |

Layered deliberately: the first three are in-process and would not survive a second process; the
fourth is a database guarantee and would.

### Money representation

`REAL` is retained for now. The current rounding discipline is careful, exhaustively tested, and
correct. Converting to integer kopecks is the right long-term move (§10 Phase 5) but it touches
every query, every report, and every export, and it is not on the critical path to a safe
release. Sequencing it after stability is the correct trade.

---

## 6. Backup and recovery architecture

### Requirements

1. Survive loss of the host.
2. An immutable snapshot immediately before every schema migration.
3. Every copy verified, not merely written.
4. Restore rehearsed, not theoretical.
5. No backup operation may destroy an existing good backup.

### Design

**Creation.** `backup(reason)` where `reason ∈ {daily, boot, pre-migration, manual}`:

```
tmp = f"{dir}/.tmp-{uuid}.db"
VACUUM INTO tmp                       # consistent snapshot, no bot downtime
verify(tmp): PRAGMA integrity_check; PRAGMA foreign_key_check; invariant queries
gzip + encrypt(tmp)                   # age/gpg, public key in repo, private key offline
os.replace(tmp, f"{dir}/adminbot-{utc_iso}-{reason}.db.gz.age")
upload_offhost(...)
```

Three changes from today, each closing a specific finding:

- **Timestamped, not day-granular.** `adminbot-2026-08-21T03-00-00Z-daily.db` — a second backup
  the same day no longer overwrites the first.
- **Write-then-`os.replace`.** No existing file is ever deleted before its replacement is
  complete and verified. Retires D‑3's “failed VACUUM leaves no file for today”.
- **Boot backup runs *before* `db.init()`**, so it captures the pre-migration state. This is the
  ordering half of D‑3, and it is a two-line change with outsized value.

**Verification is part of creation, not a separate hope.** A copy that fails `integrity_check` is
not published and raises an alert.

**Encryption.** The database contains password hashes and full financial history. Once off-host
storage is introduced, at-rest encryption is required, not optional. Public key in the repo,
private key held offline by the business owner — this also correctly makes restore a deliberate
act.

**Retention (GFS).** All backups from the last 7 days; one weekly for 8 weeks; one monthly for 12
months; **pre-migration snapshots exempt from rotation for 90 days**. Replaces the flat 14-day
window, under which corruption discovered on day 15 is unrecoverable.

**Off-host.** S3-compatible object storage (or `rclone` to any supported target). Upload failure
is an **alert**, not a logged warning — the audit's core lesson is that warnings nobody reads are
equivalent to silence.

**Telegram.** The document remains, now as a Record that is never deleted (D‑2), but it is
explicitly demoted from “the off-site backup” to “a convenience copy”. Skipped with a warning
above ~45 MB rather than failing at the 50 MB bot limit.

### Restore guarantee

Backups are only as good as the last time someone restored one. A **monthly automated restore
verification job**:

1. downloads the newest off-host backup;
2. decrypts, decompresses, opens it;
3. runs `integrity_check`, `foreign_key_check`, and the business invariant suite
   ([§8.5](#85-invariant-checker-runnable-against-production-data)) — share sums equal price,
   payout totals reconcile against their orders, no order is both cancelled and paid;
4. reports «Проверка резервной копии: ОК, N заказов, дата X» to super-admins as a Record.

This is what converts “we have backups” into “we have proven restores”, and it makes silent
corruption detectable within a month instead of never.

A written, rehearsed restore runbook lives in the repository (not only the README) and is
executed once in staging as a Phase 4 gate.

### Interaction with rollback

Once a migration has run, rolling code back is **not** sufficient — the schema is ahead of the
binary, and §2.2's downgrade protection will refuse to start. The documented rollback procedure
for a migration-bearing release is therefore: stop → restore the pre-migration snapshot →
switch the release symlink back → start. For a release with no migration, the symlink switch
alone is enough. This distinction must be in the runbook, because getting it wrong under pressure
is how data is lost.

---

## 7. Deployment and operational architecture

### Version control (O‑1) — the prerequisite for everything

`git init`, full history from today, private remote (GitHub/GitLab private or self-hosted), tagged
releases `vX.Y.Z`. Branch `main` protected; work on branches; CI required to merge.

This is first in the roadmap not because it is urgent in itself but because **every other change
in this plan is a change to code that currently cannot be reverted**.

### Environments

| | Production | Staging |
|---|---|---|
| Bot | `@vrheaven_admin_bot` | `@vrheaven_admin_staging_bot` |
| Token | systemd credential, root-owned 0600 | developer `.env` |
| Database | `/opt/vrheaven-bot/data/adminbot.db` | separate file, periodically seeded from a sanitised prod copy |
| `ADMIN_IDS` | real | developer only |

Retires O‑2. The current production token has lived in a synced working directory and **must be
rotated** as part of Phase 0 regardless of anything else here.

**Single-instance guard.** An `flock` on the data directory at start-up; a second process exits
immediately with a clear message. `TelegramConflictError` during polling is fatal — exit non-zero
so systemd surfaces it — rather than looping. This is a stronger fix than “use a staging bot”,
because it prevents the failure rather than relying on discipline.

### Server layout

```
/opt/vrheaven-bot/
  releases/v1.4.0/        ← immutable checkout of a tag
  releases/v1.4.1/
  current -> releases/v1.4.1
  data/                   ← DB, WAL, backups; never inside a release
```

Deploy: fetch tag → `uv sync --frozen` → migration dry-run against a copy → stop → switch symlink
→ start → health check → on failure, switch back (plus snapshot restore if the release migrated).

Replaces `rsync --delete` from a laptop, under which the laptop is the only authoritative copy.

### Service hardening (O‑3)

Dedicated `vrheaven` user; deployment out of `/root`. Read the live unit first — the effective
`User=` was never confirmed — then converge on:

```ini
User=vrheaven
WorkingDirectory=/opt/vrheaven-bot/current
ExecStart=/opt/vrheaven-bot/current/.venv/bin/python main.py
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/vrheaven-bot/data
LoadCredential=bot_token:/etc/vrheaven-bot/token
Restart=always
RestartSec=5
WatchdogSec=120
```

The unit file lives **in the repository** and is installed by the deploy script, so it cannot
drift from its documentation again.

### Observability (O‑4)

- **Structured logging** — JSON lines with `request_id`, `chat_id`, `user_handle`, `action`.
  `request_id` correlates a log line, an `audit_log` row, and an `outbox` row.
- **Counters** — orders, payouts, delivery failures, retries, parse errors, outbox depth.
- **Daily digest to super-admins, 09:00, as a Record** — orders and turnover, errors by type,
  undelivered outbox rows, backup status (local + off-host + last verification).
- **Alerts** — immediate Record to super-admins on: backup failure, off-host upload failure,
  outbox row exceeding max attempts, error rate above threshold, start-up refusal.

The digest is the specific countermeasure to how D‑1 stayed invisible: a delivery failure now
appears in a message a human receives, not only in a journal nobody reads.

### CI

On every push: `ruff`, `mypy --strict` on `pricing.py`/`db.py`/`utils.py`, full `pytest`, and a
migration test that applies every migration to a fixture database from each historical version.
Green CI is required to tag, and only tags deploy.

---

## 8. Testing strategy

The audit's sharpest finding is that **244 passing tests could not have caught the most severe
defect**. The strategy below is organised by the failure *classes* that escaped, not by module.

### 8.1 A `FakeBot` that enforces the Telegram contract

The highest-value change in this section, and it is roughly forty lines.

`FakeBot` currently records `text` verbatim and validates nothing. It gains assertions on every
send/edit:

- body ≤ 4096, caption ≤ 1024 → catches **D‑8**
- parse-mode syntax validates (HTML well-formed, or MarkdownV2 entities balanced) → catches **D‑1**
- keyboard within Telegram's button and payload limits → catches a **W‑9** failure mode
- `chat_id` is a chat the test actually created → catches misrouted notifications

Because every existing test funnels through `FakeBot`, all 244 immediately begin guarding
formatting for free. This must land **before** the HTML migration (§2.3) so the migration is
performed under test.

### 8.2 Real-dispatcher concurrency tests

`test_order_confirm_double_tap_records_single_order` models the dispatcher and asserts a behaviour
the real dispatcher does not exhibit — a false negative that certified the bug it was written to
prevent.

Replacement: build an actual `Dispatcher` with the real routers, real storage, and real isolation,
then feed two `Update` objects through `dp.feed_update` concurrently via `asyncio.gather`. Assert
one order, one receipt, one notification set, and no unhandled exception.

Covers: double-submit (D‑7), ladder collision across two chats (D‑6), concurrent payout (D‑13),
concurrent cancel-vs-payout (D‑16), and two simultaneous Records to one chat (§4.6).

**Rule going forward: a concurrency test that simulates the dispatcher is not a concurrency test.**

### 8.3 Migration tests

For each version N: construct a database at N, migrate, assert final schema and row counts.
Plus a **crash-injection test** — raise inside a migration and assert `user_version` is unchanged,
data is intact, and the next start-up retries cleanly. This is D‑4's regression test and there is
no other way to write it.

Plus a rehearsal against a sanitised copy of the real production database, run in staging before
Phase 1 ships.

### 8.4 Message lifecycle tests

Encode §4's invariants directly:

- exactly one message per chat carries `reply_markup`;
- `deleteMessage` is only ever called for the current window id or a user message id — **any other
  delete fails the test**;
- a Record delivered during an active order flow leaves FSM data intact and the payment screen
  re-anchored below it (D‑5);
- a backup document is never deleted by any subsequent interaction (D‑2);
- a failed window send leaves the previous window intact (D‑8).

### 8.5 Invariant checker runnable against production data

A standalone script asserting business truths, not code paths:

- `admin_share + owner_share + vr_share == price` for every order, to the kopeck;
- every payout's `amount` equals the sum of its orders' shares plus its bonuses;
- no order is both `cancelled_at IS NOT NULL` and payout-linked via the self-cancel path;
- no active handle collision; no active promo `name_folded` collision;
- every order has a `series_pos` unique within its administrator's series window.

Run in CI against fixtures, in the monthly restore verification against a real backup (§6), and
on demand during incidents. This is what closes the loop between backup, correctness, and
operations.

### 8.6 Property-based and golden tests

`hypothesis` over `split()` and the ladder for kopeck-exactness across generated prices and
percentages (extends the existing adversarial grid to unbounded input). Golden-file tests for all
five CSV exports so a column change is a visible diff.

### 8.7 Live smoke test

Automated against the **staging** bot, run before each production tag: send and edit a Rich
Message table, send a document, exercise the window re-anchor path, verify a 429 retry. This is
the only way to close **U‑1** and **U‑2** — no offline test can tell whether Telegram renders
`<table>` as intended or accepts `parse_mode` alongside `rich_message`.

### 8.8 Rich-message fallback

Because U‑1 is unverified, the design should not assume it. `TableRenderer` gets two backends —
native Rich HTML and a `<pre>` monospace fallback — with a runtime capability flag that
**auto-demotes on repeated Rich failures** and alerts super-admins. This converts an unknown from
a release blocker into a graceful degradation.

---

## 9. Dependencies and implementation order

```
O-1 Version control ──────────────────────────► gates everything
                          │
      ┌───────────────────┼───────────────────────────┐
      ▼                   ▼                           ▼
 §6 Backup            §8.1 FakeBot              Phase-0 hotfixes
 (ordering,           contract                  D-1 · D-3 · D-2 · token
  atomic write)           │                           │
      │                   │                           │
      ▼                   │                           │
 §2.2 Migration           │                           │
 framework ◄──────────────┼───────────────────────────┘
      │  (needs isolation_level=None from §2.1)
      ├──────────────┬──────────────┬───────────────┬────────────┐
      ▼              ▼              ▼               ▼            ▼
 §2.1 Trans-    §2.4 FSM      §2.5 Outbox    §2.6 Audit    §2.7 Super-
 actions        + isolation   table          log           admin roster
      │              │              │               │
      ├─ D-6 ladder  └─ D-7 ────┐   │               │
      ├─ D-13 payout            │   │               │
      ├─ D-16 cancel            │   │               │
      └─ client_token ──────────┴───┤               │
                                    ▼               │
                          §2.5 Delivery layers ─────┘
                          (middleware + worker)
                                    │
                                    ▼
                          §4 Message architecture
                                    │
                          §2.3 HTML migration (guarded by §8.1)
                                    │
                                    ▼
                          §4.9 UX pass · §7 Ops · §3 Low priority
```

**Hard orderings, and why:**

1. **VCS before all.** Nothing else is revertible without it.
2. **Backup ordering fix before the migration framework.** The framework's safety promise is the
   pre-migration snapshot; that snapshot must already be correct and immutable.
3. **`isolation_level=None` before the migration framework.** Transactional DDL is the mechanism
   that makes D‑4 solvable; without it the framework is decorative.
4. **Migration framework before every schema change.** Six tables and columns depend on it.
5. **FSM persistence and `SimpleEventIsolation` in one commit.** Persistence without isolation
   converts a latent duplicate-order bug into a live one. Landing `client_token` first means even
   a botched sequencing cannot duplicate an order.
6. **`FakeBot` contract before the HTML migration.** Otherwise ~100 literals are rewritten
   unguarded.
7. **Outbox before the message architecture.** Records are defined by durable delivery; the
   taxonomy without the outbox is only a naming convention.
8. **Audit log after transactions.** “Same transaction as the change” requires transactions.

**Safely parallel:** backup work ∥ `FakeBot` contract ∥ ops setup (staging, CI, systemd) — no
shared code.

---

## 10. Phased roadmap

Each phase ends in a state strictly safer than the one before, and each is independently
shippable.

### Phase 0 — Stop the bleeding · 1–2 days

No architecture. Remove the four live production risks and make work revertible.

- `git init`, commit, private remote, tag `v0-audit-baseline`.
- **D‑1** — `fmt_num` emits U+2212; regression test.
- **D‑3** — boot backup moved before `db.init()`; timestamped names; write-then-`os.replace`.
- **D‑2** — backup document no longer becomes the window (minimal change; full model in Phase 4).
- Rotate the production token; register the staging bot; remove `adminbot.db` from the tree.
- Run the `journalctl` grep to close **U‑7** and quantify how long notifications have been dark.

**Milestone:** rollback exists; super-admins receive order notifications again; a recoverable
snapshot precedes every deploy.

### Phase 1 — Data foundation · ~1 week

- `isolation_level=None`, WAL, pragma set (§5).
- `db.read()` / `db.write()`; all mutating methods take `tx`; per-method commits removed.
- Migration framework, `user_version`, downgrade protection, post-conditions (§2.2).
- Baseline stamp rehearsed against a copy of the production database in staging. **Gate.**
- `orders.client_token` + idempotent creation.
- **D‑6**, **D‑13**, **D‑16** resolved by transaction placement.
- §8.2 real-dispatcher concurrency tests; §8.3 migration tests including crash injection.

**Milestone:** no money operation can be observed half-done; no interrupted migration can silently
lose history; a duplicate submission is impossible by constraint.

### Phase 2 — Runtime robustness · ~1 week

- SQLite FSM storage + `SimpleEventIsolation` (one commit) + TTL cleanup.
- `dp.errors` handler; centralised callback parsing (**D‑9**); `stale_callback` gains real text.
- Session request middleware: throttle + `TelegramRetryAfter` retry, idempotent-only network
  retry (§2.5 Layer 1).
- `outbox` table + worker; permanent-failure handling; `my_chat_member` (§2.5 Layers 2–3).
- **D‑12** resolved.

**Milestone:** restarts are invisible to users; no notification is silently dropped; no button
spins unresolved.

### Phase 3 — Attribution and formatting · ~1 week

- `audit_log` written inside every money transaction; surfaced in cards, `audit.csv`, digest (**W‑4**).
- `super_admins` roster; two-key start-up requirement (**W‑5**).
- §8.1 `FakeBot` contract lands **first**, then the HTML migration (**D‑1** structurally, **W‑3**).
- Length guard in the send layer + input caps (**D‑8**).

**Milestone:** every money action is attributable; no message can fail to render because of its
content; the system cannot run on a single key.

### Phase 4 — Message and UI architecture · ~1.5 weeks

- `chat_windows` with stored payload; unified `show_window`; per-chat lock (§4.5–4.6).
- Record/Window/Transient/Toast taxonomy applied to all ~200 call sites (§4.3).
- Re-anchoring, coalesced (§4.4) — **D‑5** and **D‑2** fully resolved.
- Order-flow UX pass (§4.9).
- §8.4 message lifecycle tests; §8.7 live staging smoke test — closes **U‑1**, **U‑2**.
- `TableRenderer` fallback (§8.8).

**Milestone:** no screen is lost mid-order; chat history is useful and permanent; exactly one
message is ever tappable.

### Phase 5 — Operations and hardening · ~1.5 weeks

- Release-directory layout, tagged deploys, rollback runbook incl. the migration case (§6, §7).
- Dedicated user, hardened unit from the repository, `flock` single-instance guard (**O‑3**, **O‑2**).
- Off-host encrypted backups, GFS retention, monthly restore verification (**W‑6**).
- Structured logging, counters, daily digest, alerts (**O‑4**).
- CI: ruff, mypy, tests, migration matrix (**O‑5**).
- Inbound throttling, off-loop PBKDF2, non-enumerable login, `windows` pruning (**W‑7**).
- Pagination and order lookup (**W‑9**, **D‑10**).
- **D‑11**, **D‑14**, **D‑15**, **D‑17**, **D‑18**, remaining **W‑10**.
- `SPEC.md` and `README.md` reconciled with actual behaviour (§11).

**Milestone:** the system fails safely, says so, restores provably, and scales past the current
club count.

### Phase 6 — Deferred quality · ongoing

Integer kopecks; property-based invariant tests; golden CSV tests; editable promo prices;
deductions; per-device revocation; shift summaries; owner-side paid-order visibility.

**Total to a confident release: Phases 0–5, roughly six weeks of focused work.**

---

## 11. Product decisions that must be settled first

These are business decisions, not engineering ones. Each blocks the phase noted. Making them
silently in code is how the current spec/behaviour drift happened, so each should be answered and
then written into `SPEC.md`.

| # | Decision | Options | Recommendation | Blocks |
|---|---|---|---|---|
| **P1** | **Pricing moment** — is the price fixed when quoted, or when payment is confirmed? | (a) quote-time, honour what the customer was told; (b) payment-time, per `SPEC.md §2` as written | **(a)** — it matches how a counter sale actually works; amend §2 and store `quoted_at` | D‑17, Phase 5 |
| **P2** | **Suspended owner** — what happens to orders placed while an owner is suspended? | (a) association kept, share 0; (b) current behaviour, orders become ownerless; (c) share accrues and is released on reinstatement | **(a)** — losing the association makes disputes unresolvable and is almost certainly not intended | D‑15, Phase 5 |
| **P3** | **Retroactivity of P2** — should existing ownerless orders be repaired? | (a) leave history as-is; (b) backfill from the administrator's owner at the time | **(a)** unless the business needs those figures — backfill is a guess about a past state | D‑15, Phase 5 |
| **P4** | **Notification volume** — do owners and super-admins want one Record per order forever? | (a) per-order for both; (b) per-order for owners, hourly digest for super-admins; (c) configurable per recipient | **(c)** defaulting to (a) — but this must be decided before §4.3 fixes the taxonomy | Phase 4 |
| **P5** | **Second super-admin** — who holds the second key? | a co-owner, a trusted manager, or a break-glass account | Must be a real second person; a shared account defeats the purpose | W‑5, Phase 3 |
| **P6** | **Payout-day wording** — is the period genuinely closed on the 1st/15th, or is that a reminder? | (a) reminder with honest wording; (b) real calendar period close | **(a)** — (b) contradicts `SPEC.md §4`'s payout-closes-period definition | D‑14, Phase 5 |
| **P7** | **Bonus deductions** — should negative bonuses be allowed? | (a) yes, with mandatory comment; (b) no, handle off-book | **(a)** — off-book corrections defeat the ledger's purpose | Phase 6 |
| **P8** | **Backup encryption key custody** — who holds the private key? | business owner offline; escrow; both | Must be decided before off-host backups exist, or the copies are unencrypted or unrecoverable | W‑6, Phase 5 |
| **P9** | **Chat history expectations** — is a permanent per-order record in the administrator's chat desirable or unwanted clutter? | (a) yes, it is the receipt; (b) only for cancellable orders; (c) no | **(a)** — this is the premise of the Record class; confirm before §4 | Phase 4 |

`SPEC.md` sections requiring amendment once settled: **§2** (P1), **§4** (P6), **§5** (D‑10),
**§7** (P2), **§9** (the message model — the largest rewrite), **§10** (backup architecture),
**§12** (invariants 7 and 9 restated).

### Closing the audit's open unknowns

These are not decisions but unresolved facts. Each is closed by a specific action in this plan
rather than left open; the two marked **gate** must be answered before the phase they block, since
a wrong assumption there changes the design rather than just the schedule.

| # | Unknown | How this plan closes it | When |
|---|---|---|---|
| **U‑1** | Do Rich Message tables render as intended, and does Telegram accept `parse_mode` alongside `rich_message`? | Live staging smoke test ([§8.7](#87-live-smoke-test)) plus a `TableRenderer` fallback that auto-demotes to `<pre>` on repeated failure ([§8.8](#88-rich-message-fallback)) — the unknown becomes graceful degradation instead of a release blocker | Phase 4 |
| **U‑2** | Can a text message be edited into a rich one and back? | Same smoke test; the `show_window` algorithm ([§4.6](#46-window-render-algorithm)) already falls back to re-anchor on `BadRequest`, so failure is survivable either way | Phase 4 |
| **U‑3** | Actual order rate, club count, and database size | Measured directly from the live database as the first task of Phase 1. Determines whether pagination (**W‑9**) and the ~45 MB Telegram backup ceiling (**W‑6**) are theoretical or imminent, and whether P4's notification volume needs a digest now or later | **Gate** — Phase 1 |
| **U‑4** | Does production `ADMIN_IDS` really contain one entry? | Read the server `.env` in Phase 0. If one, **W‑5** is an active single point of failure and P5 becomes urgent rather than scheduled; the two-key start-up requirement ([§2.7](#27-super-admin-roster-w5)) would then fail closed on first deploy of Phase 3, so the second key must exist before that phase ships | **Gate** — Phase 0 |
| **U‑5** | Does any backup exist outside this repository's mechanism (e.g. a provider snapshot)? | Confirmed with the hosting provider in Phase 0. A provider snapshot materially reduces the severity of **D‑2**/**W‑6** in the interim but does not replace [§6](#6-backup-and-recovery-architecture) — provider snapshots are host-scoped and unverified | Phase 0 |
| **U‑6** | Has any staff member logged in from a group chat? | Query `user_chats` for positive `chat_id` values that are not private chats, in Phase 1. The private-chat filter (**W‑10**) prevents recurrence; any existing group binding must be unbound and its password rotated, since credentials and revenue splits were rendered where colleagues could read them | Phase 1 |
| **U‑7** | How long have super-admin order notifications been failing? | The `journalctl` grep in Phase 0. Quantifies the oversight gap and tells the business how many orders went unreviewed — a reconciliation question, not just a technical one | Phase 0 |

---

## 12. Target state

### Money

Every order, payout, cancellation, and bonus is written in a single `BEGIN IMMEDIATE` transaction
alongside its audit entry and its outbound notifications. A crash leaves either everything or
nothing. A duplicate submission is refused by a unique constraint rather than prevented by timing.
The salary ladder is computed under the write lock, so two devices produce steps 1 and 2. Shares
still sum to the price to the kopeck — that never changed and never will.

### Attribution

Every money-affecting action names its actor, timestamp, and before/after state, queryable by
entity or by actor, exported as CSV, and summarised daily. “Who cancelled order 412?” has an
answer.

### Messages

Each chat has exactly one message with buttons — the Window — which is edited in place and
re-anchored below any new Record. Records (receipts, notifications, reports, backups, exports)
are permanent, have no buttons, and are never deleted by the bot. The user's own input is deleted
on receipt. Toasts carry trivia. The bot calls `deleteMessage` for exactly two things: its own
Window and the user's input.

An administrator mid-sale who receives a bonus notification sees the notification, then their
payment screen again, unchanged. A super-admin's backup from three weeks ago is still in the chat.
Nobody has to guess where to tap.

### Delivery

Every outgoing call passes a global and per-chat throttle and retries on 429. Notifications are
enqueued in the same transaction as the change that caused them and drained by a worker that
survives restarts. A blocked user is unbound once. Undelivered messages are a visible count, not
silence.

### State

FSM state lives in SQLite and survives restarts; per-chat event isolation serialises updates. A
deploy no longer costs anyone an order. Every unhandled error answers the callback, logs with
correlation, and tells the user something true.

### Data safety

Timestamped, verified, encrypted backups written atomically, retained on a GFS schedule, pushed
off-host, and **proven restorable monthly** by a job that opens the newest copy and runs the
business invariant suite. Migrations are versioned, atomic, post-condition-checked, preceded by an
immutable snapshot, and refuse to run backwards.

### Operations

Tagged releases from a private git remote, deployed by symlink switch to an immutable release
directory, gated on green CI, rollback documented for both the migration and non-migration cases.
A dedicated unprivileged user, a hardened unit stored in the repository, a single-instance lock,
separate staging and production bots, and secrets in systemd credentials rather than a synced
working copy. A daily digest and immediate alerts mean a silent failure is a contradiction in
terms.

### The property that matters most

The audit's defining finding was not any single bug — it was that **the system's most severe
defect was invisible to 244 passing tests, to its own logs, and to its operators, for an unknown
length of time.** The target state is not merely a system with those bugs fixed. It is a system in
which that specific outcome cannot recur: the tests enforce the Telegram contract, the delivery
layer counts what it fails to send, the audit log names who did what, the digest puts all of it in
front of a human every morning, and the backups prove monthly that the data behind it all is still
there.
