"""Панель VR Heaven: учётные записи, доступ, выплаты, настройки, экспорт."""

from helpers import (
    bind,
    button_texts,
    create_admin,
    create_owner,
    drain,
    fake_cb,
    fake_msg,
    make_order,
    make_state,
    set_window,
    window_text,
)

from handlers.vrheaven import (
    AddAdminSG,
    AddOwnerSG,
    SuperAdminFilter,
    admin_add_contact,
    admin_add_handle,
    admin_add_name,
    admin_add_pick_owner,
    admin_owner_set,
    discount_day_toggle,
    discount_days_menu,
    export_csv,
    owner_add_contact,
    owner_add_handle,
    owner_add_name,
    payout_confirm,
    payout_list,
    payout_pick,
    setting_apply,
    setting_ask,
    setting_set,
    setting_toggle,
    settings_menu,
    stale_callback,
    summary,
    super_admin_add,
    super_admin_add_ask,
    super_admin_del,
    super_admins_menu,
    user_add_start,
    user_delete_ask,
    user_delete_confirm,
    user_log,
    user_pct_ask,
    user_pct_set,
    user_pwd_regen,
    user_toggle,
    users_list,
)

VR_CHAT = 999
OTHER_VR = 998
OWNER_CHAT = 201
WINDOW = 50


async def _panel(db):
    await set_window(db, VR_CHAT, WINDOW)


def _cb(data, **kwargs):
    return fake_cb(data, chat_id=VR_CHAT, message_id=WINDOW, **kwargs)


def _msg(text):
    return fake_msg(text, chat_id=VR_CHAT)


# ------------------------------------------------------------------- Доступ

async def test_super_admin_filter_covers_env_and_database(db, config):
    flt = SuperAdminFilter()
    assert await flt(fake_cb("am", chat_id=VR_CHAT), db, config) is True
    assert await flt(fake_cb("am", chat_id=12345), db, config) is False
    async with db.write() as tx:
        await db.add_super_admin(tx, 12345, "менеджер", VR_CHAT)
    assert await flt(fake_cb("am", chat_id=12345), db, config) is True


async def test_roster_can_grow_and_shrink_but_keeps_the_recovery_key(db, ui, config):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await super_admin_add_ask(_cb("sa:add"), state, ui)
    await super_admin_add(_msg("12345"), state, db, ui, config)
    assert [r["tg_id"] for r in await db.list_super_admins()] == [12345]

    await super_admins_menu(_cb("sa:menu"), state, db, ui, config)
    text = await window_text(db, VR_CHAT)
    assert "Супер-админов: 3" in text
    assert "запасной вход" in text

    await super_admin_del(_cb(f"sa:delok:{12345}"), state, db, ui, config)
    assert await db.list_super_admins() == []

    # запись из .env убрать из бота нельзя
    cb = _cb(f"sa:delok:{VR_CHAT}")
    await super_admin_del(cb, state, db, ui, config)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_single_super_admin_is_allowed(db, ui, config):
    """Ростер может состоять из одного человека — старт этим не блокируется."""
    from roster import super_admin_ids
    assert len(await super_admin_ids(db, config)) == 2
    single = config.__class__(**{**config.__dict__, "admin_ids": frozenset({VR_CHAT})})
    assert await super_admin_ids(db, single) == [VR_CHAT]


# -------------------------------------------------------- Учётные записи

async def test_owner_is_created_through_the_flow(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await user_add_start(_cb("ow:add"), state, ui)
    assert await state.get_state() == AddOwnerSG.handle.state
    await owner_add_handle(_msg("club"), state, db, ui)
    await owner_add_name(_msg("Клуб на Ленина"), state, ui)
    await owner_add_contact(_msg("—"), state, db, ui)
    owner = await db.get_user_by_handle("club")
    assert owner["percent"] == 30 and owner["contact"] == ""
    text = await window_text(db, VR_CHAT)
    assert "Пароль: <code>" in text and "только один раз" in text
    assert await button_texts(db, VR_CHAT) == ["Пароль передан"]


async def test_admin_is_created_and_attached_to_an_owner(db, ui):
    await _panel(db)
    owner = await create_owner(db, "club")
    state = make_state(db, VR_CHAT)
    await user_add_start(_cb("ad:add"), state, ui)
    await admin_add_handle(_msg("adm"), state, db, ui)
    await admin_add_name(_msg("Пётр"), state, ui)
    await admin_add_contact(_msg("@petr"), state, db, ui)
    assert await state.get_state() == AddAdminSG.owner.state
    await admin_add_pick_owner(_cb(f"ad:pickown:{owner['id']}"), state, db, ui)
    admin = await db.get_user_by_handle("adm")
    assert admin["owner_id"] == owner["id"] and admin["contact"] == "@petr"


async def test_double_pick_creates_a_single_admin(db, ui):
    await _panel(db)
    owner = await create_owner(db, "club")
    state = make_state(db, VR_CHAT)
    await user_add_start(_cb("ad:add"), state, ui)
    await admin_add_handle(_msg("adm"), state, db, ui)
    await admin_add_name(_msg("Пётр"), state, ui)
    await admin_add_contact(_msg("-"), state, db, ui)
    cb = _cb(f"ad:pickown:{owner['id']}")
    await admin_add_pick_owner(cb, state, db, ui)
    text_after_first = await window_text(db, VR_CHAT)
    await admin_add_pick_owner(_cb(f"ad:pickown:{owner['id']}"), state, db, ui)
    assert len(await db.list_users("admin")) == 1
    assert await window_text(db, VR_CHAT) == text_after_first   # пароль не затёрт


async def test_overlong_name_and_contact_are_refused(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await user_add_start(_cb("ow:add"), state, ui)
    await owner_add_handle(_msg("club"), state, db, ui)
    await owner_add_name(_msg("я" * 100), state, ui)
    assert "Введите имя ещё раз" in await window_text(db, VR_CHAT)
    await owner_add_name(_msg("Клуб"), state, ui)
    await owner_add_contact(_msg("к" * 100), state, db, ui)
    assert await db.list_users("owner") == []
    assert "Введите контакт ещё раз" in await window_text(db, VR_CHAT)


async def test_suspend_and_activate_are_audited(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    await user_toggle(_cb(f"ow:toggle:{owner['id']}"), db, ui)
    assert (await db.get_user(owner["id"]))["is_active"] == 0
    await user_toggle(_cb(f"ow:toggle:{owner['id']}"), db, ui)
    assert (await db.get_user(owner["id"]))["is_active"] == 1
    actions = [r["action"] for r in await db.audit_for_entity("user", owner["id"])]
    assert actions == ["user.activate", "user.suspend"]


async def test_password_reset_unbinds_devices_and_shows_password_once(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    await bind(db, owner, OWNER_CHAT)
    await user_pwd_regen(_cb(f"ow:pwdok:{owner['id']}"), db, ui)
    assert await db.chats_for_user(owner["id"]) == []
    text = await window_text(db, VR_CHAT)
    assert "Пароль: <code>" in text
    assert await button_texts(db, VR_CHAT) == ["Пароль передан"]


async def test_owner_percent_edit_applies_only_to_new_orders(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=1000)
    state = make_state(db, VR_CHAT)
    await user_pct_ask(_cb(f"ow:pct:{owner['id']}"), state, db, ui)
    await user_pct_set(_msg("50"), state, db, ui)
    assert (await db.get_user(owner["id"]))["percent"] == 50
    assert (await db.get_order(1))["owner_share"] == 300.0


async def test_owner_percent_bounds(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    state = make_state(db, VR_CHAT)
    await user_pct_ask(_cb(f"ow:pct:{owner['id']}"), state, db, ui)
    await user_pct_set(_msg("100"), state, db, ui)
    assert (await db.get_user(owner["id"]))["percent"] == 30
    await user_pct_set(_msg("0"), state, db, ui)
    assert (await db.get_user(owner["id"]))["percent"] == 0


async def test_owner_with_admins_cannot_be_deleted(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await user_delete_ask(_cb(f"ow:del:{owner['id']}"), db, ui)
    assert "Сначала перекрепите" in await window_text(db, VR_CHAT)
    cb = _cb(f"ow:delok:{owner['id']}")
    await user_delete_confirm(cb, db, ui)
    assert (await db.get_user(owner["id"]))["deleted_at"] is None

    await admin_owner_set(_cb(f"ad:setown:{admin['id']}:0"), db, ui)
    await user_delete_confirm(_cb(f"ow:delok:{owner['id']}"), db, ui)
    assert (await db.get_user(owner["id"]))["deleted_at"] is not None


async def test_user_log_screen_shows_recent_actions(db, ui, config):
    await _panel(db)
    owner = await create_owner(db)
    await user_toggle(_cb(f"ow:toggle:{owner['id']}"), db, ui)
    await user_log(_cb(f"ow:log:{owner['id']}"), db, ui, config)
    assert "учётная запись приостановлена" in await window_text(db, VR_CHAT)


# ------------------------------------------------------------------ Выплата

async def test_empty_period_is_not_offered_for_payout(db, ui):
    """Строк нет — платить нечего: ни кнопки, ни экрана подтверждения."""
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.create_payout(tx, owner)             # период владельца закрыт
    await payout_list(_cb("po:list"), make_state(db, VR_CHAT), db, ui)
    buttons = " ".join(await button_texts(db, VR_CHAT))
    assert "владелец" not in buttons and "администратор" in buttons
    cb = _cb(f"po:p:{owner['id']}")
    await payout_pick(cb, db, ui)
    text = await window_text(db, VR_CHAT)
    assert "уже нет долей к выплате" in text
    assert "Подтвердить" not in await button_texts(db, VR_CHAT)


async def test_a_period_of_zero_share_orders_can_be_closed(db, ui):
    """Доля нулевая, но строки настоящие: период обязан закрываться,
    иначе заказы висят открытыми вечно и вечно числятся в остатке
    VR Heaven (SPEC §4). Отчёт дня выплат такому получателю по-прежнему
    не уходит — там граница по ненулевой доле."""
    await _panel(db)
    owner = await create_owner(db, percent=0)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    total = await db.owner_unpaid_total(owner["id"])
    assert total["due_sum"] == 0 and db.is_payable(total)
    await payout_list(_cb("po:list"), make_state(db, VR_CHAT), db, ui)
    assert "владелец" in " ".join(await button_texts(db, VR_CHAT))

    await payout_confirm(_cb(f"po:ok:{owner['id']}"), db, ui)
    assert (await db.owner_unpaid_total(owner["id"]))["orders_count"] == 0


async def test_payout_notifies_the_recipient_and_closes_the_period(
        db, ui, bot, worker):
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await bind(db, owner, OWNER_CHAT)
    await make_order(db, admin, owner, price=300)
    await payout_confirm(_cb(f"po:ok:{owner['id']}"), db, ui)
    await drain(worker)
    assert (await db.owner_unpaid_total(owner["id"]))["due_sum"] == 0
    texts = bot.records(OWNER_CHAT)
    assert texts and "Выплата проведена" in texts[0] and "90 ₽" in texts[0]
    assert "Выплата проведена" in await window_text(db, VR_CHAT)


async def test_payout_double_tap_is_safe(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=300)
    await payout_confirm(_cb(f"po:ok:{owner['id']}"), db, ui)
    await payout_confirm(_cb(f"po:ok:{owner['id']}"), db, ui)
    assert len(await db.payouts_for_user(owner["id"])) == 1
    assert "уже нет долей" in await window_text(db, VR_CHAT)


# ---------------------------------------------------------------- Настройки

async def test_settings_are_grouped_into_four_decisions(db, ui):
    await _panel(db)
    await settings_menu(_cb("st:menu"), make_state(db, VR_CHAT), db, ui)
    buttons = await button_texts(db, VR_CHAT)
    assert len(buttons) == 5           # четыре раздела и «В меню»
    assert buttons[0] == "Цены сеансов"
    assert buttons[-1] == "В меню"
    assert any(b.startswith("Скидка") for b in buttons)
    assert any(b.startswith("Акции") for b in buttons)
    assert any(b.startswith("ПК-бонус") for b in buttons)


async def test_setting_edit_flow(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await setting_ask(_cb("st:set:price_1_30"), state, db, ui)
    await setting_set(_msg("350"), state, db, ui)
    assert (await db.get_settings())["price_1_30"] == 350
    log = await db.export_audit()
    assert log[-1]["action"] == "setting.change"


async def test_tripled_price_requires_confirmation(db, ui):
    """Лишний ноль в цене раньше молча переписывал прайс всем клубам."""
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await setting_ask(_cb("st:set:price_1_30"), state, db, ui)
    await setting_set(_msg("30000"), state, db, ui)
    assert (await db.get_settings())["price_1_30"] == 300
    text = await window_text(db, VR_CHAT)
    assert "Было: 300 ₽" in text and "Станет: 30\u00a0000 ₽" in text
    await setting_apply(_cb("st:apply"), state, db, ui)
    assert (await db.get_settings())["price_1_30"] == 30000


async def test_discount_percent_rejects_100(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await setting_ask(_cb("st:set:discount_percent"), state, db, ui)
    await setting_set(_msg("100"), state, db, ui)
    assert (await db.get_settings())["discount_percent"] == 20


async def test_discount_time_rejects_reversed_range(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await setting_ask(_cb("st:set:discount_time"), state, db, ui)
    await setting_set(_msg("22:00-02:00"), state, db, ui)
    settings = await db.get_settings()
    assert (settings["discount_start_min"], settings["discount_end_min"]) == (600, 960)
    assert "через полночь не поддерживается" in await window_text(db, VR_CHAT)
    await setting_set(_msg("11:00-15:30"), state, db, ui)
    settings = await db.get_settings()
    assert (settings["discount_start_min"], settings["discount_end_min"]) == (660, 930)


async def test_discount_days_toggle(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    await discount_days_menu(_cb("st:days"), state, db, ui)
    await discount_day_toggle(_cb("st:day:5"), state, db, ui)
    assert int((await db.get_settings())["discount_days"]) == 0b0111111
    cb = _cb("st:day:9")
    await discount_day_toggle(cb, state, db, ui)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


async def test_all_days_off_warns_that_discount_is_dead(db, ui):
    await _panel(db)
    state = make_state(db, VR_CHAT)
    async with db.write() as tx:
        await db.set_setting(tx, "discount_days", 1)
    await discount_day_toggle(_cb("st:day:0"), state, db, ui)
    assert "Ни один день не выбран" in await window_text(db, VR_CHAT)


async def test_toggle_flips_and_is_audited(db, ui):
    await _panel(db)
    await setting_toggle(_cb("st:tgl:pc_bonus_enabled"), db, ui)
    assert (await db.get_settings())["pc_bonus_enabled"] == 0
    cb = _cb("st:tgl:group_enabled")            # ключа прежней версии больше нет
    await setting_toggle(cb, db, ui)
    assert cb.answer.await_args.kwargs.get("show_alert") is True


# ------------------------------------------------------------------ Экспорт

async def test_export_produces_five_files_including_the_audit_log(
        db, ui, config, bot, worker):
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    order_id = await make_order(db, admin, owner, price=300)
    async with db.write() as tx:
        await db.cancel_order(tx, order_id, for_self=False)
        await db.delete_user(tx, admin["id"])
    await export_csv(_cb("ex:all"), make_state(db, VR_CHAT), db, ui, config)
    await drain(worker)
    names = {d[3].filename for d in bot.documents}
    assert names == {"users.csv", "orders.csv", "payouts.csv", "bonuses.csv",
                     "audit.csv"}
    orders = next(d[3].data for d in bot.documents if d[3].filename == "orders.csv")
    text = orders.decode("utf-8-sig")
    assert "остаток_vr_heaven" in text and "adm1" in text
    users = next(d[3].data for d in bot.documents if d[3].filename == "users.csv")
    assert "удалён" in users.decode("utf-8-sig")


async def test_summary_shows_the_vrheaven_remainder(db, ui):
    await _panel(db)
    owner = await create_owner(db)
    admin = await create_admin(db, owner)
    await make_order(db, admin, owner, price=790, admin_share=79,
                     owner_percent=30)
    await summary(_cb("sum"), make_state(db, VR_CHAT), db, ui)
    text = await window_text(db, VR_CHAT)
    assert "Остаток VR Heaven: 474 ₽" in text


async def test_stale_button_gets_an_explicit_answer(db, ui):
    await _panel(db)
    cb = _cb("что-то:устаревшее")
    await stale_callback(cb, make_state(db, VR_CHAT), ui)
    assert cb.answer.await_args.args[0] == "Экран устарел — открыт текущий"


async def test_users_list_paginates(db, ui):
    await _panel(db)
    for index in range(12):
        await create_owner(db, f"own{index:02d}")
    await users_list(_cb("ow:list"), make_state(db, VR_CHAT), db, ui)
    buttons = await button_texts(db, VR_CHAT)
    assert "Дальше ›" in buttons
    assert len([b for b in buttons if b.startswith("own")]) == 8
    await users_list(_cb("ow:list:1"), make_state(db, VR_CHAT), db, ui)
    buttons = await button_texts(db, VR_CHAT)
    assert "‹ Назад" in buttons
    assert len([b for b in buttons if b.startswith("own")]) == 4


async def test_super_admin_cannot_be_an_owner_or_an_administrator(db, ui, config):
    """SPEC §12.8. Чат супер-админа уводится в панель VR Heaven, поэтому
    кабинет за тем же чатом перестал бы открываться вовсе."""
    admin = await create_admin(db, handle="adm1")
    await bind(db, admin, 4242)
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await super_admin_add_ask(fake_cb("sa:add", chat_id=VR_CHAT, message_id=WINDOW),
                              state, ui)
    await super_admin_add(fake_msg("4242", chat_id=VR_CHAT), state, db, ui, config)
    assert "adm1" in await window_text(db, VR_CHAT)
    assert await db.list_super_admins() == []


async def test_super_admin_add_still_works_for_a_free_id(db, ui, config):
    await set_window(db, VR_CHAT, WINDOW)
    state = make_state(db, VR_CHAT)
    await super_admin_add_ask(fake_cb("sa:add", chat_id=VR_CHAT, message_id=WINDOW),
                              state, ui)
    await super_admin_add(fake_msg("4242", chat_id=VR_CHAT), state, db, ui, config)
    assert [r["tg_id"] for r in await db.list_super_admins()] == [4242]
