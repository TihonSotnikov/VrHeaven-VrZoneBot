"""Экспорт CSV: общие построители файлов для VR Heaven и владельца.

Оба экспорта собираются одними и теми же функциями; разница — в объёме:
VR Heaven выгружает всё и с полным распределением, владелец — только своих
администраторов, свои заказы, свои выплаты и журнал действий по своим
данным, без чужих долей и без Telegram-идентификаторов (владельцу полное
распределение заказа не показывается никогда).

Разделитель `;`, кодировка UTF-8 с BOM — файлы открываются в Excel
без настройки.
"""

import csv
import io
import json
from zoneinfo import ZoneInfo

from pricing import normalize_reset, order_label
from utils import fmt_dt, fmt_reset_times

ROLE_LABELS = {"owner": "владелец", "admin": "администратор"}

# Человеческие названия действий журнала — файл читают не только инженеры
ACTION_LABELS = {
    "order.create": "заказ оформлен",
    "order.cancel": "заказ отменён",
    "payout.create": "проведена выплата",
    "bonus.create": "начислен бонус",
    "bonus.cancel": "бонус отменён",
    "user.create": "создана учётная запись",
    "user.suspend": "учётная запись приостановлена",
    "user.activate": "учётная запись активирована",
    "user.delete": "учётная запись удалена",
    "user.password": "выдан новый пароль",
    "user.percent": "изменена доля владельца",
    "user.owner": "сменён владелец",
    "user.series_reset": "изменено время сбросов серии",
    "user.login": "вход в кабинет",
    "chat.unbind": "устройство отключено",
    "promo.create": "создана акция",
    "promo.price": "изменена цена акции",
    "promo.archive": "акция удалена",
    "setting.change": "изменена настройка",
    "superadmin.add": "добавлен супер-админ",
    "superadmin.remove": "убран супер-админ",
    "export.download": "выгрузка данных",
    "backup.create": "резервная копия",
}


def _csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def _status(row) -> str:
    if row["deleted_at"]:
        return "удалён"
    return "действующий" if row["is_active"] else "приостановлен"


def _order_cells(o, tz: ZoneInfo) -> dict:
    """Общие поля строки заказа для обоих вариантов файла."""
    return {
        "label": order_label(o["kind"], o["headsets"], o["minutes"], o["promo_name"]),
        "promo": o["promo_name"] or "",
        "cancelled": fmt_dt(o["cancelled_at"], tz) if o["cancelled_at"] else "",
        "created": fmt_dt(o["created_at"], tz),
    }


def users_csv(rows, tz: ZoneInfo) -> bytes:
    """Полный экспорт учётных записей (VR Heaven).

    Доля в процентах есть только у владельца: вознаграждение
    администратора считается лесенкой серии, и колонка со сплошными
    нулями у администраторов только сбивала бы с толку.
    """
    table = []
    for u in rows:
        is_admin = u["role"] == "admin"
        resets = fmt_reset_times(normalize_reset(u["series_reset_min"])) if is_admin else ""
        table.append(
            [u["id"], ROLE_LABELS[u["role"]], u["handle"], u["name"],
             u["contact"], "" if is_admin else u["percent"], resets,
             u["owner_handle"] or "", _status(u), u["chats_count"],
             fmt_dt(u["created_at"], tz)]
        )
    return _csv_bytes(
        ["id", "роль", "логин", "имя", "контакт", "доля_владельца_%", "сброс_серии",
         "владелец", "статус", "устройств_кабинета", "создан"],
        table,
    )


def owner_users_csv(rows, tz: ZoneInfo) -> bytes:
    """Экспорт владельца: его администраторы без служебных полей."""
    return _csv_bytes(
        ["id", "логин", "имя", "контакт", "статус", "создан"],
        [[u["id"], u["handle"], u["name"], u["contact"], _status(u),
          fmt_dt(u["created_at"], tz)]
         for u in rows],
    )


def orders_csv(rows, tz: ZoneInfo) -> bytes:
    """Полный экспорт заказов (VR Heaven) — со всем распределением."""
    table = []
    for o in rows:
        c = _order_cells(o, tz)
        table.append(
            [o["id"], o["admin_handle"], o["owner_handle"] or "",
             c["label"], c["promo"],
             o["base_price"], o["discount_percent"], o["price"],
             o["admin_share"], o["series_pos"] or "",
             o["admin_payout_id"] or "",
             o["owner_percent"], o["owner_share"], o["owner_payout_id"] or "",
             "да" if o["owner_suspended"] else "",
             round(o["price"] - o["admin_share"] - o["owner_share"], 2),
             c["cancelled"], c["created"]]
        )
    return _csv_bytes(
        ["id", "админ", "владелец", "заказ", "акция", "базовая_цена",
         "скидка_%", "цена", "вознаграждение_админа", "серия_№",
         "id_выплаты_админа", "доля_владельца_%", "доля_владельца",
         "id_выплаты_владельца", "владелец_был_приостановлен",
         "остаток_vr_heaven", "отменён", "дата"],
        table,
    )


def owner_orders_csv(rows, tz: ZoneInfo) -> bytes:
    """Экспорт владельца: его заказы и только его доля."""
    table = []
    for o in rows:
        c = _order_cells(o, tz)
        table.append(
            [o["id"], o["admin_handle"], c["label"], c["promo"], o["price"],
             o["owner_percent"], o["owner_share"], o["owner_payout_id"] or "",
             c["cancelled"], c["created"]]
        )
    return _csv_bytes(
        ["id", "админ", "заказ", "акция", "цена", "доля_владельца_%",
         "доля_владельца", "id_выплаты", "отменён", "дата"],
        table,
    )


def payouts_csv(rows, tz: ZoneInfo) -> bytes:
    """Полный экспорт выплат (VR Heaven)."""
    return _csv_bytes(
        ["id", "получатель", "роль", "сумма", "заказов", "дата"],
        [[p["id"], p["handle"], ROLE_LABELS[p["role"]], p["amount"],
          p["orders_count"], fmt_dt(p["created_at"], tz)]
         for p in rows],
    )


def owner_payouts_csv(rows, tz: ZoneInfo) -> bytes:
    """Экспорт владельца: его собственные выплаты."""
    return _csv_bytes(
        ["id", "сумма", "заказов", "дата"],
        [[p["id"], p["amount"], p["orders_count"], fmt_dt(p["created_at"], tz)]
         for p in rows],
    )


def bonuses_csv(rows, tz: ZoneInfo) -> bytes:
    """Экспорт бонусов администраторам (VR Heaven)."""
    def bonus_status(b) -> str:
        if b["cancelled_at"]:
            return "отменён"
        return "выплачен" if b["payout_id"] else "к выплате"

    return _csv_bytes(
        ["id", "админ", "сумма", "комментарий", "статус", "id_выплаты", "дата"],
        [[b["id"], b["admin_handle"], b["amount"], b["comment"],
          bonus_status(b), b["payout_id"] or "", fmt_dt(b["created_at"], tz)]
         for b in rows],
    )


def _actor(row, *, with_ids: bool) -> str:
    if row["actor_kind"] == "superadmin":
        return f"VR Heaven {row['actor_tg_id']}" if with_ids else "VR Heaven"
    if row["actor_kind"] == "staff":
        # Роль актора в журнале не хранится, а из кабинета действует и
        # владелец (вход, экспорт), поэтому подпись нейтральная — то же
        # слово, что на экране «История действий»
        handle = row["actor_handle"] or f"id {row['actor_user_id']}"
        return f"кабинет {handle}"
    return "система"


def _changes(row) -> str:
    """Что именно изменилось — одной читаемой строкой."""
    parts = []
    for label, key in (("было", "before_json"), ("стало", "after_json")):
        if not row[key]:
            continue
        try:
            value = json.loads(row[key])
        except ValueError:
            value = row[key]
        if isinstance(value, dict):
            parts.append(f"{label}: " + ", ".join(f"{k}={v}" for k, v in value.items()))
        else:
            parts.append(f"{label}: {value}")
    return " · ".join(parts)


def audit_csv(rows, tz: ZoneInfo) -> bytes:
    """Журнал действий (VR Heaven): кто, что, когда и что изменилось."""
    return _csv_bytes(
        ["id", "дата", "кто", "telegram_id", "действие", "объект", "id_объекта",
         "изменения", "запрос"],
        [[r["id"], fmt_dt(r["at"], tz), _actor(r, with_ids=True), r["actor_tg_id"] or "",
          ACTION_LABELS.get(r["action"], r["action"]), r["entity"],
          r["entity_id"] or "", _changes(r), r["request_id"] or ""]
         for r in rows],
    )


def owner_audit_csv(rows, tz: ZoneInfo) -> bytes:
    """Журнал действий владельца: только его данные, без Telegram-идентификаторов."""
    return _csv_bytes(
        ["дата", "кто", "действие", "объект", "id_объекта", "изменения"],
        [[fmt_dt(r["at"], tz), _actor(r, with_ids=False),
          ACTION_LABELS.get(r["action"], r["action"]), r["entity"],
          r["entity_id"] or "", _changes(r)]
         for r in rows],
    )
