"""Сборка текста сообщений: экранирование и граница длины.

Здесь проверяется свойство, которого не хватало больше всего: сообщение
не может сломаться из-за собственного содержимого — ни из-за символа во
вводе пользователя, ни из-за длины.
"""

import pytest
from helpers import ContractError, validate_html

from markup import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    Report,
    Table,
    clamp,
    esc,
    h,
    join,
    lines,
    pre_table,
    table_html,
)
from utils import MINUS, fmt_money, fmt_num, fmt_signed_money


def test_interpolated_value_is_escaped_by_construction():
    text = h("<b>Клуб: {}</b>", "Игры & <Развлечения>")
    assert text == "<b>Клуб: Игры &amp; &lt;Развлечения&gt;</b>"
    validate_html(text)


def test_template_tags_pass_through_but_values_never_do():
    text = h("<b>{}</b>\n{}", "<b>жирный?</b>", "a < b & c > d")
    assert "<b>&lt;b&gt;" in text
    validate_html(text)


def test_negative_money_uses_typographic_minus():
    """Дефис — спецсимвол Markdown; типографский минус безопасен везде."""
    assert fmt_num(-110) == f"{MINUS}110"
    assert fmt_money(-1234.5) == f"{MINUS}1\u00a0234,50 ₽"   # разряды — неразрывный пробел
    assert "-" not in fmt_money(-110)
    assert fmt_signed_money(500) == "+500 ₽"
    assert fmt_signed_money(-500) == f"{MINUS}500 ₽"


def test_negative_remainder_message_is_valid():
    """Отрицательный остаток VR Heaven — обычный случай, а не край."""
    text = h("Остаток VR Heaven: {}", fmt_money(-110))
    validate_html(text)


def test_clamp_keeps_message_within_limit_and_closes_tags():
    long_text = "<b>" + "я" * 6000 + "</b>"
    result = clamp(long_text)
    assert len(result) <= TEXT_LIMIT
    assert result.endswith("…</b>")
    validate_html(result)


def test_clamp_does_not_cut_inside_a_tag():
    body = "".join(f"<b>{i}</b>" for i in range(2000))
    result = clamp(body, 100)
    assert len(result) <= 100
    validate_html(result)


def test_clamp_leaves_short_text_untouched():
    assert clamp("<b>коротко</b>") == "<b>коротко</b>"


def test_long_free_text_from_user_still_produces_valid_message():
    """Длинный комментарий больше не ломает экран: он обрезается."""
    text = clamp(h("<b>Бонус</b>\nЗа что: {}", "очень длинно " * 500))
    assert len(text) <= TEXT_LIMIT
    validate_html(text)


def test_join_and_lines_skip_empty_parts():
    assert join("a", "", "b") == "a\n\nb"
    assert lines("a", None, "b") == "a\nb"


def test_table_html_escapes_cells():
    table = Table(["Имя"], [["Club <X> & Co"]])
    assert "<td>Club &lt;X&gt; &amp; Co</td>" in table_html(table)
    validate_html(table_html(table))


def test_pre_table_is_valid_fallback():
    table = Table(["Логин", "Сумма"], [["adm1", "1 500"], ["b", "2"]])
    result = pre_table(table)
    assert result.startswith("<pre>") and result.endswith("</pre>")
    validate_html(result)


def test_report_renders_both_ways():
    report = Report("Сводка").add(h("Строка {}", "&")).add(
        Table(["A"], [["<b>"]]))
    rich = report.to_html(rich=True)
    plain = report.to_html(rich=False)
    assert "<table>" in rich and "<pre>" in plain
    validate_html(rich)
    validate_html(plain)


def test_contract_validator_catches_real_failures():
    with pytest.raises(ContractError):
        validate_html("<b>не закрыт")
    with pytest.raises(ContractError):
        validate_html("<script>alert(1)</script>")
    with pytest.raises(ContractError):
        validate_html("цена < 100")
    with pytest.raises(ContractError):
        validate_html("Игры & развлечения")
    with pytest.raises(ContractError):
        validate_html("x" * (TEXT_LIMIT + 1))
    with pytest.raises(ContractError):
        validate_html("x" * (CAPTION_LIMIT + 1), limit=CAPTION_LIMIT)


def test_escaped_ampersand_passes_validator():
    validate_html(esc("Игры & развлечения"))
