"""Каждая кнопка ведёт к обработчику.

Кнопка, за которой никого нет, выглядит рабочей и молча возвращает
человека в меню. Здесь все клавиатуры прогоняются через фильтры настоящих
роутеров: непринятый callback валит тест.
"""

import re

import pytest
from helpers import create_admin, create_owner, fake_cb

import keyboards as kb
from handlers import staff, vrheaven

# Значения-заглушки для параметрических кнопок: важен маршрут, не сущность
SAMPLE = {"id": 1, "handle": "adm1", "name": "Админ", "price": 600.0,
          "amount": 500.0, "is_active": 1, "role": "admin"}


def _row(**overrides):
    return {**SAMPLE, **overrides}


def _all_callbacks() -> set[str]:
    promos = [_row(id=3, name="3=4")]
    users = [_row(id=4)]
    orders = [_row(id=5, admin_handle="adm1", created_at="2026-08-21T10:00:00+00:00",
                   admin_payout_id=None, owner_payout_id=None, kind="standard",
                   headsets=1, minutes=30, promo_name=None)]
    settings = {"price_1_15": 200, "price_1_30": 300, "price_1_60": 500,
                "price_2_15": 350, "price_2_30": 500, "price_2_60": 800,
                "discount_enabled": 1, "discount_percent": 20,
                "discount_days": 31, "discount_start_min": 600,
                "discount_end_min": 960, "pc_bonus_enabled": 1,
                "pc_bonus_points": 100}
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Moscow")
    markups = [
        kb.vrheaven_menu_kb(), kb.to_vrheaven_menu_kb(), kb.owners_menu_kb(),
        kb.admins_menu_kb(), kb.users_list_kb(users, "ad", page=1, has_next=True),
        kb.user_card_kb(_row(), "ad", bonus_count=2),
        kb.user_card_kb(_row(role="owner"), "ow"),
        kb.password_kb("ad", 4), kb.owner_pick_kb(users, "ad:pickown"),
        kb.payout_pick_kb([(4, "adm1 · 100 ₽")]),
        kb.settings_menu_kb(settings, 1), kb.prices_kb(settings),
        kb.discount_kb(settings), kb.pc_bonus_kb(settings),
        kb.discount_days_kb(settings), kb.promos_kb(promos),
        kb.promo_card_kb(promos[0]), kb.bonuses_kb([_row(id=9)], 4),
        kb.super_admins_kb([(111, "111", True), (222, "222 · запасной", False)]),
        kb.export_kb(),
        kb.staff_admin_menu_kb(), kb.staff_owner_menu_kb(), kb.to_staff_menu_kb(),
        kb.order_kind_kb(promos), kb.headsets_kb(with_back=True),
        kb.duration_kb(1), kb.payment_kb("no:std"),
        kb.order_done_kb(5, cancellable=True),
        kb.staff_orders_pick_kb(orders, tz),
        kb.orders_pick_kb(orders, tz, prev_cb="ac:list", next_cb="ac:page:5"),
        kb.welcome_kb(), kb.login_cancel_kb(), kb.cancel_kb(),
        kb.confirm_kb("po:ok:4", "po:list"), kb.back_kb("ad:card:4"),
    ]
    return {button.callback_data
            for markup in markups
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data}


# Часть кнопок живёт только на экране своего сценария: экран оплаты,
# выбор владельца, подтверждение цены. Проверяем маршрут в этих состояниях.
SCREEN_STATES = [None, "NewOrderSG:confirm", "AddAdminSG:owner",
                 "EditSettingSG:value"]


async def _routed(router, data: str, db, config, chat_id: int,
                  raw_state=None) -> bool:
    callback = fake_cb(data, chat_id=chat_id)
    kwargs = {"db": db, "config": config, "ui": None, "state": None,
              "event_update": None, "bot": None, "raw_state": raw_state}
    passed, extra = await router.callback_query.check_root_filters(callback, **kwargs)
    if not passed:
        return False                        # чужой роутер, спрашиваем другой
    kwargs.update(extra)
    for handler in router.callback_query.handlers:
        if handler.callback in (vrheaven.stale_callback, staff.stale_callback):
            continue
        passed, _ = await handler.check(callback, **kwargs)
        if passed:
            return True
    return False


@pytest.mark.parametrize("data", sorted(_all_callbacks()))
async def test_every_button_reaches_a_handler(data, db, config):
    """Ни одна кнопка не должна попадать в обработчик устаревшего экрана."""
    await create_owner(db, "own1")
    await create_admin(db, handle="adm1")
    routed = False
    for raw_state in SCREEN_STATES:
        routed = (await _routed(vrheaven.router, data, db, config, 999, raw_state)
                  or await _routed(staff.router, data, db, config, 1, raw_state))
        if routed:
            break
    assert routed, f"кнопка {data!r} никуда не ведёт"


def test_callback_payloads_fit_the_telegram_limit():
    for data in _all_callbacks():
        assert len(data.encode()) <= 64, data


def test_no_button_text_is_empty():
    for markup in (kb.vrheaven_menu_kb(), kb.staff_admin_menu_kb(),
                   kb.staff_owner_menu_kb()):
        for row in markup.inline_keyboard:
            for button in row:
                assert button.text.strip()
                assert not re.search(r"[<>&]", button.text)
