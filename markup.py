"""Единая точка сборки текста сообщений — HTML-разметка Telegram.

Ручного экранирования в проекте нет: шаблон сообщения пишем мы, а любое
подставляемое значение экранируется функцией h() по построению. Забыть
экранировать нечего — другого способа подставить значение нет.

Здесь же граница длины: ни одно исходящее сообщение не может превысить
лимит Telegram, потому что обрезка выполняется до отправки, по границе
тега, с закрытием всех открытых тегов.
"""

import html
import re
from dataclasses import dataclass, field

# Лимиты Bot API. Считаем по «сырой» строке вместе с тегами — это строже
# фактического лимита Telegram (он считает видимый текст) и потому надёжно.
TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024

# Длины свободного ввода (проверяются на вводе, до записи в БД)
NAME_MAX = 64
CONTACT_MAX = 64
COMMENT_MAX = 200
PROMO_NAME_MAX = 40
HANDLE_MAX = 32

TRUNCATION_MARKER = "…"


def esc(value) -> str:
    """Экранирует значение для HTML-разметки Telegram (& < >)."""
    return html.escape(str(value), quote=False)


def h(template: str, *args, **kwargs) -> str:
    """Тело сообщения: шаблон — наш, все значения экранируются.

        h("<b>Заказ №{}</b>\\nАдминистратор: {}", order_id, handle)

    Шаблон проходит как есть (в нём наши теги), аргументы — через esc().
    """
    if not args and not kwargs:
        return template
    return template.format(*(esc(a) for a in args),
                           **{k: esc(v) for k, v in kwargs.items()})


def join(*parts: str) -> str:
    """Склейка готовых блоков сообщения через пустую строку."""
    return "\n\n".join(p for p in parts if p)


def lines(*parts: str) -> str:
    return "\n".join(p for p in parts if p)


# ------------------------------------------------------------- Ограничение длины

def _tokens(text: str):
    """Разбивает HTML на неделимые кусочки: тег, HTML-сущность, символ."""
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "<":
            end = text.find(">", i)
            if end == -1:
                yield "text", text[i:]
                return
            yield "tag", text[i:end + 1]
            i = end + 1
        elif char == "&":
            end = text.find(";", i)
            if 0 < end - i <= 9:
                yield "text", text[i:end + 1]
                i = end + 1
            else:
                yield "text", "&"
                i += 1
        else:
            yield "text", char
            i += 1


_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)")
_VOID_TAGS = {"br", "hr", "img"}


def clamp(text: str, limit: int = TEXT_LIMIT) -> str:
    """Обрезает сообщение до лимита Telegram, не ломая разметку.

    Сообщение никогда не должно упасть при отправке из-за собственной
    длины: обрезаем по границе тега, дописываем многоточие и закрываем
    все открытые теги.
    """
    if len(text) <= limit:
        return text
    out: list[str] = []
    stack: list[str] = []
    length = 0
    best: tuple[int, tuple[str, ...]] | None = None
    for kind, token in _tokens(text):
        if kind == "tag":
            match = _TAG_NAME_RE.match(token)
            if match:
                closing, name = match.group(1), match.group(2).lower()
                if name in _VOID_TAGS or token.endswith("/>"):
                    pass
                elif closing:
                    if stack and stack[-1] == name:
                        stack.pop()
                else:
                    stack.append(name)
        out.append(token)
        length += len(token)
        tail = TRUNCATION_MARKER + "".join(f"</{t}>" for t in reversed(stack))
        if length + len(tail) <= limit:
            best = (len(out), tuple(stack))
        elif best is not None:
            break
    if best is None:      # даже первый токен не помещается — режем грубо
        return text[:limit]
    count, open_tags = best
    tail = TRUNCATION_MARKER + "".join(f"</{t}>" for t in reversed(open_tags))
    return "".join(out[:count]) + tail


# ------------------------------------------------------------------- Таблицы

@dataclass(frozen=True)
class Table:
    headers: list[str]
    rows: list[list]


@dataclass
class Report:
    """Отчёт, независимый от способа показа.

    Один и тот же отчёт рендерится нативной таблицей Telegram (Rich
    Messages) или моноширинным <pre> — на случай, если Rich Messages
    недоступны клиенту (см. messaging.rich_supported).
    """
    title: str
    blocks: list = field(default_factory=list)

    def add(self, block) -> "Report":
        self.blocks.append(block)
        return self

    def to_html(self, *, rich: bool, limit: int = TEXT_LIMIT) -> str:
        """Отчёт одним сообщением, заведомо укладывающимся в лимит.

        Длинный отчёт режется не с конца: в конце стоят итоги — «К выплате»
        и «Остаток VR Heaven», ради которых отчёт и открывают. Обрезается
        середина (строки таблиц), последний блок остаётся целым.
        """
        separator = "" if rich else "\n\n"
        parts = [f"<p><b>{esc(self.title)}</b></p>" if rich
                 else f"<b>{esc(self.title)}</b>"]
        for block in self.blocks:
            if isinstance(block, Table):
                parts.append(table_html(block) if rich else pre_table(block))
            elif rich:
                parts.append(f"<p>{block}</p>")
            else:
                parts.append(block)
        text = separator.join(parts)
        if len(text) <= limit:
            return text
        tail = parts[-1]
        head = separator.join(parts[:-1])
        room = limit - len(tail) - len(separator)
        if room <= len(TRUNCATION_MARKER):    # итоги сами длиннее лимита
            return clamp(text, limit)
        return clamp(head, room) + separator + tail


def table_html(table: Table) -> str:
    """Классический <table> — Telegram строит из него нативную таблицу."""
    head = "".join(f"<th>{esc(c)}</th>" for c in table.headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in row) + "</tr>"
        for row in table.rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def pre_table(table: Table) -> str:
    """Моноширинная таблица — запасной вариант без Rich Messages."""
    cells = [[str(c) for c in table.headers]]
    cells += [[str(c) for c in row] for row in table.rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(cells[0]))]
    rendered = []
    for index, row in enumerate(cells):
        rendered.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)).rstrip())
        if index == 0:
            rendered.append("  ".join("-" * w for w in widths))
    return "<pre>" + esc("\n".join(rendered)) + "</pre>"
