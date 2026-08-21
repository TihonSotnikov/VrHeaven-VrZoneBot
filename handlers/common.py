"""Общее для обоих кабинетов: поддержка, инструкции, защита от групп."""

import os

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from config import Config
from markup import h, join

SUPPORT = "@VrHeaven"

SUPPORT_TEXT = join(
    "<b>Поддержка</b>",
    f"Вопросы по заказам, доступу и выплатам — {SUPPORT}",
    "Пишите сразу: восстановление доступа, отмена старого заказа и "
    "спорные начисления решаются только там.",
)

GROUP_TEXT = (
    "Бот работает только в личном чате: в общем чате видны логины, "
    f"пароли и суммы. Откройте бота лично и нажмите /start · {SUPPORT}"
)

GUIDE_TITLES = {
    "superadmin": ("Как работает бот", "vrheaven-instrukciya.md"),
    "owner": ("Как пользоваться ботом", "vladelec-instrukciya.md"),
    "admin": ("Как пользоваться ботом", "administrator-instrukciya.md"),
}


def guide_bytes(config: Config, role: str) -> tuple[str, str, bytes]:
    """Файл инструкции для роли: (заголовок, имя файла, содержимое)."""
    title, filename = GUIDE_TITLES[role]
    path = os.path.join(config.guides_dir, f"{role}.md")
    with open(path, "rb") as handle:
        return title, filename, handle.read()


def guide_caption(role: str) -> str:
    title = GUIDE_TITLES[role][0]
    return h("<b>{}</b>\n\nКороткая инструкция — сохраните её в чате", title)


# Личный чат: в группе бот показывал бы пароли и распределение денег
# всем участникам, поэтому там он не работает вовсе.
guard_router = Router(name="non-private")
guard_router.message.filter(F.chat.type != "private")
guard_router.callback_query.filter(F.message.chat.type != "private")


@guard_router.message()
async def group_message(message: Message) -> None:
    await message.answer(GROUP_TEXT)


@guard_router.callback_query()
async def group_callback(cb: CallbackQuery) -> None:
    await cb.answer(GROUP_TEXT, show_alert=True)
