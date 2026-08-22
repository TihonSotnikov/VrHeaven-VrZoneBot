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
    """Файл инструкции для роли: (заголовок, имя файла, содержимое).

    Содержимое отдаётся с подписью UTF-8 (BOM). Причина не косметическая:
    Bot API не передаёт кодировку вовсе — aiogram кладёт документ в форму
    как `application/octet-stream`, без charset, и просмотрщик определяет
    кодировку сам. Android по умолчанию читает UTF-8 и всё показывает
    верно; iOS при отсутствии подписи падает на однобайтовую кодировку,
    и «Как пользоваться» превращается в «ÐšÐ°Ðº Ð¿Ð¾Ð»ÑŒÐ·Ð¾Ð²Ð°Ñ‚ÑŒÑÑ».
    Три байта подписи — единственный сигнал, который доезжает до
    просмотрщика; ту же роль они играют в выгрузках CSV для Excel.
    """
    title, filename = GUIDE_TITLES[role]
    path = os.path.join(config.guides_dir, f"{role}.md")
    # utf-8-sig на чтении снимает подпись, если она уже есть в файле:
    # двойного BOM не будет ни при каком состоянии guides/
    with open(path, encoding="utf-8-sig") as handle:
        return title, filename, handle.read().encode("utf-8-sig")


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
