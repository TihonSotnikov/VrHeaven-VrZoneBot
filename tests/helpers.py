"""Тестовые двойники Telegram — с проверкой контракта Bot API.

Прежний FakeBot записывал текст и не проверял ничего, поэтому самый
тяжёлый дефект в истории проекта (неэкранированный минус, убивавший
уведомления супер-админам) был невидим для 244 зелёных тестов.

Здесь каждая отправка и правка проверяется:

* длина тела ≤ 4096, подписи документа ≤ 1024;
* разметка HTML разбирается: теги парные, из разрешённого списка,
  «голых» < > & в тексте нет;
* клавиатура укладывается в лимиты Telegram;
* сообщение уходит в чат, известный тесту.

Любое нарушение — падение теста в месте отправки.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from fsm_storage import SQLiteStorage
from markup import CAPTION_LIMIT, TEXT_LIMIT

# Теги, которые Telegram понимает в HTML-разметке, плюс табличные теги
# нативных таблиц (Rich Messages)
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre",
    "a", "blockquote", "tg-spoiler", "span",
    "p", "br", "table", "thead", "tbody", "tr", "th", "td",
}
VOID_TAGS = {"br"}

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s[^<>]*)?)>")
_ENTITY_RE = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,9}|#\d{1,6}|#x[0-9a-fA-F]{1,6});")

MAX_BUTTONS = 100
MAX_CALLBACK_BYTES = 64


class ContractError(AssertionError):
    """Сообщение, которое Telegram отверг бы."""


def validate_html(text: str, *, limit: int = TEXT_LIMIT, where: str = "") -> None:
    if len(text) > limit:
        raise ContractError(f"{where}: длина {len(text)} > {limit}")
    stack: list[str] = []
    for match in _TAG_RE.finditer(text):
        closing, name = match.group(1), match.group(2).lower()
        if name not in ALLOWED_TAGS:
            raise ContractError(f"{where}: Telegram не знает тег <{name}>")
        if name in VOID_TAGS:
            continue
        if closing:
            if not stack or stack[-1] != name:
                raise ContractError(f"{where}: закрыт тег </{name}> без открытого")
            stack.pop()
        else:
            stack.append(name)
    if stack:
        raise ContractError(f"{where}: не закрыты теги {stack}")
    bare = _TAG_RE.sub("", text)
    if "<" in bare or ">" in bare:
        raise ContractError(f"{where}: неэкранированный символ < или > в тексте")
    for index, char in enumerate(bare):
        if char == "&" and not _ENTITY_RE.match(bare, index):
            raise ContractError(f"{where}: неэкранированный символ & в тексте")


def validate_markup(markup, where: str = "") -> None:
    if markup is None:
        return
    buttons = [b for row in markup.inline_keyboard for b in row]
    if len(buttons) > MAX_BUTTONS:
        raise ContractError(f"{where}: {len(buttons)} кнопок — больше лимита")
    for button in buttons:
        if button.callback_data is None:
            continue
        size = len(button.callback_data.encode())
        if size > MAX_CALLBACK_BYTES:
            raise ContractError(
                f"{where}: callback_data {size} байт > {MAX_CALLBACK_BYTES}")
        if not button.text:
            raise ContractError(f"{where}: кнопка без подписи")


class FakeBot:
    """Записывает вызовы Bot API и проверяет их допустимость."""

    def __init__(self):
        self.sent = []       # (chat_id, message_id, text)
        self.edited = []     # (chat_id, message_id, text); text=None для rich
        self.deleted = []    # (chat_id, message_id)
        self.documents = []  # (chat_id, message_id, caption, document)
        self.rich_sent = []  # (chat_id, message_id, html)
        self.edit_error: str | None = None
        self.rich_error: str | None = None
        self.send_error_chats: set[int] = set()
        self.send_error: Exception | None = None
        self.last_markup = None
        self.last_rich = None
        self._next_id = 1000
        self.answers: list[str | None] = []
        self.other_calls: list[str] = []
        # Живые сообщения чата: (chat_id, message_id) -> {text, markup}
        self.live: dict[tuple[int, int], dict] = {}

    # ---- совместимость с aiogram

    id = 9
    session = SimpleNamespace(middleware=lambda *a, **k: None)

    async def __call__(self, method):
        """Прямой вызов метода API: bot(SendRichMessage(...)) и всё, что
        aiogram шлёт сам (ответ на нажатие, удаление сообщения)."""
        name = type(method).__name__
        if name == "SendRichMessage":
            validate_html(method.rich_message.html, where="sendRichMessage")
            validate_markup(method.reply_markup, where="sendRichMessage")
            if self.rich_error:
                raise TelegramBadRequest(method=None, message=self.rich_error)
            self._fail_for(method.chat_id)
            self._next_id += 1
            self.rich_sent.append((method.chat_id, self._next_id,
                                   method.rich_message.html))
            self.live[(method.chat_id, self._next_id)] = {
                "text": method.rich_message.html, "markup": method.reply_markup}
            self.last_markup = method.reply_markup
            self.last_rich = method.rich_message
            return SimpleNamespace(message_id=self._next_id)
        if name == "AnswerCallbackQuery":
            self.answers.append(method.text)
            return True
        if name == "DeleteMessage":
            await self.delete_message(method.chat_id, method.message_id)
            return True
        if name == "SendMessage":
            return await self.send_message(method.chat_id, method.text,
                                           reply_markup=method.reply_markup)
        if name == "EditMessageText":
            return await self.edit_message_text(
                method.text, chat_id=method.chat_id, message_id=method.message_id,
                reply_markup=method.reply_markup,
                rich_message=getattr(method, "rich_message", None))
        self.other_calls.append(name)
        return True

    def _fail_for(self, chat_id) -> None:
        if chat_id in self.send_error_chats:
            raise (self.send_error or TelegramBadRequest(
                method=None, message="Bad Request: chat not found"))

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        validate_html(text, where=f"sendMessage({chat_id})")
        validate_markup(reply_markup, where=f"sendMessage({chat_id})")
        self._fail_for(chat_id)
        self._next_id += 1
        self.sent.append((chat_id, self._next_id, text))
        self.live[(chat_id, self._next_id)] = {"text": text, "markup": reply_markup}
        self.last_markup = reply_markup
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None,
                                reply_markup=None, rich_message=None, **kwargs):
        if text is not None:
            validate_html(text, where=f"editMessageText({chat_id})")
        if rich_message is not None:
            validate_html(rich_message.html, where=f"editMessageText({chat_id}) rich")
        validate_markup(reply_markup, where=f"editMessageText({chat_id})")
        if self.edit_error:
            raise TelegramBadRequest(method=None, message=self.edit_error)
        self.edited.append((chat_id, message_id, text))
        self.live[(chat_id, message_id)] = {
            "text": text if text is not None else rich_message.html,
            "markup": reply_markup}
        self.last_markup = reply_markup
        if rich_message is not None:
            self.last_rich = rich_message

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        self.live.pop((chat_id, message_id), None)

    async def send_document(self, chat_id, document, caption=None,
                            reply_markup=None, **kwargs):
        if caption:
            validate_html(caption, limit=CAPTION_LIMIT,
                          where=f"sendDocument({chat_id})")
        self._fail_for(chat_id)
        self._next_id += 1
        self.documents.append((chat_id, self._next_id, caption, document))
        self.live[(chat_id, self._next_id)] = {"text": caption or "",
                                               "markup": reply_markup}
        return SimpleNamespace(message_id=self._next_id)

    # ---- удобства для тестов

    def texts(self, chat_id: int | None = None) -> list[str]:
        """Все тексты Записей и Окон, в порядке отправки."""
        rows = self.sent + [(c, m, t) for c, m, t in self.rich_sent]
        if chat_id is not None:
            rows = [r for r in rows if r[0] == chat_id]
        return [t for _, _, t in rows]

    def interactive(self, chat_id: int) -> list[int]:
        """Живые сообщения чата, у которых есть кнопки."""
        return [mid for (chat, mid), data in self.live.items()
                if chat == chat_id and data["markup"] is not None]

    def records(self, chat_id: int | None = None) -> list[str]:
        """Тексты Записей: сообщения без кнопок. Переставленное Окно
        (у него кнопки есть) сюда не попадает."""
        result = []
        for chat, message_id, text in self.sent:
            if chat_id is not None and chat != chat_id:
                continue
            live = self.live.get((chat, message_id))
            if live is not None and live["markup"] is not None:
                continue
            result.append(text)
        return result


def make_state(db, chat_id: int = 1) -> FSMContext:
    """Состояние сценария в настоящем хранилище — тесты проверяют то же,
    что работает в бою, включая переживание перезапуска."""
    return FSMContext(
        storage=SQLiteStorage(db),
        key=StorageKey(bot_id=9, chat_id=chat_id, user_id=chat_id),
    )


def fake_cb(data: str, chat_id: int = 1, message_id: int = 50,
            from_id: int | None = None):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id, type="private"),
            message_id=message_id,
        ),
        from_user=SimpleNamespace(id=from_id if from_id is not None else chat_id),
        answer=AsyncMock(),
    )


def fake_msg(text: str, chat_id: int = 1, from_id: int | None = None):
    return SimpleNamespace(
        text=text,
        chat=SimpleNamespace(id=chat_id, type="private"),
        from_user=SimpleNamespace(id=from_id if from_id is not None else chat_id),
        delete=AsyncMock(),
        answer=AsyncMock(),
    )


# ------------------------------------------------------------------ Данные

async def create_owner(db, handle="own1", *, percent=None, password="secret"):
    """Владелец; без явного percent действует доля по умолчанию (30%)."""
    from utils import hash_password
    async with db.write() as tx:
        uid = await db.create_user(tx, "owner", handle, f"Владелец {handle}", "",
                                   hash_password(password), percent)
    return await db.get_user(uid)


async def create_admin(db, owner=None, handle="adm1", *, password="secret",
                       contact=""):
    from utils import hash_password
    async with db.write() as tx:
        uid = await db.create_user(tx, "admin", handle, f"Админ {handle}", contact,
                                   hash_password(password), 0,
                                   owner_id=owner["id"] if owner else None)
    return await db.get_user(uid)


async def make_order(db, admin, owner=None, *, price=300.0, kind="standard",
                     headsets=1, minutes=30, discount=0.0, base=None,
                     admin_share=None, series_pos=1, owner_percent=None,
                     promo_id=None, promo_name=None, created_at=None):
    """Заказ с явно заданными начислениями — для проверок расчётов.

    Боевое оформление идёт через db.create_order (лесенка считается там);
    здесь доли задаются прямо, чтобы проверять выплаты и отчёты на любых
    числах, включая исторические заказы прежней схемы.
    """
    from pricing import ladder_amount
    from utils import utcnow_iso
    percent = owner_percent if owner_percent is not None else (
        owner["percent"] if owner else 0)
    if admin_share is None:
        admin_share = ladder_amount(series_pos) if series_pos else 0.0
    owner_share = round(price * percent / 100, 2) if owner else 0.0
    async with db.write() as tx:
        cur = await tx.execute(
            "INSERT INTO orders (admin_id, owner_id, kind, headsets, minutes,"
            " promo_id, promo_name, base_price, discount_percent, price,"
            " admin_percent, admin_share, series_pos, owner_percent, owner_share,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (admin["id"], owner["id"] if owner else None, kind, headsets, minutes,
             promo_id, promo_name, base if base is not None else price, discount,
             price, 0, admin_share, series_pos, percent if owner else 0,
             owner_share, created_at or utcnow_iso()),
        )
        return cur.lastrowid


async def bind(db, user, chat_id: int) -> None:
    async with db.write() as tx:
        await db.bind_chat(tx, user["id"], chat_id)


async def set_window(db, chat_id: int, message_id: int, text: str = "экран") -> None:
    """Окно чата с настоящей клавиатурой: в бою у Окна кнопки есть всегда,
    и именно по ним тесты отличают Окно от Записи."""
    import keyboards as kb
    async with db.write() as tx:
        await db.save_window(tx, chat_id, message_id, text=text,
                             markup_json=kb.to_staff_menu_kb().model_dump_json(),
                             rich=False)


async def window_text(db, chat_id: int) -> str:
    """Текст текущего Окна чата — источник правды об экране."""
    row = await db.get_window(chat_id)
    return row["text"] if row else ""


async def window_markup(db, chat_id: int):
    row = await db.get_window(chat_id)
    if not row or not row["markup_json"]:
        return None
    from aiogram.types import InlineKeyboardMarkup
    return InlineKeyboardMarkup.model_validate_json(row["markup_json"])


async def button_texts(db, chat_id: int) -> list[str]:
    markup = await window_markup(db, chat_id)
    if markup is None:
        return []
    return [b.text for row in markup.inline_keyboard for b in row]


async def drain(worker, limit: int = 200) -> int:
    """Доставляет всё, что стоит в очереди Записей."""
    total = 0
    for _ in range(limit):
        sent = await worker.drain()
        if not sent:
            break
        total += sent
    return total
