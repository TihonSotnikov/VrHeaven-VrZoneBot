"""Транспорт и обработчик ошибок: темп, повторы, ответ на нажатие."""

import asyncio
from types import SimpleNamespace

import pytest
from aiogram.exceptions import (
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

import errors
from delivery import InboundThrottle, ThrottleMiddleware
from errors import cb_int, cb_ints, cb_tail


async def _call(middleware, method, responses):
    calls = {"n": 0}

    async def make_request(bot, sent):
        calls["n"] += 1
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    value = await middleware(make_request, None, method)
    return value, calls["n"]


async def test_retry_after_is_waited_out_and_the_call_repeats():
    middleware = ThrottleMiddleware(global_rate=1000, chat_rate=1000)
    method = SendMessage(chat_id=1, text="x")
    value, calls = await _call(
        middleware, method,
        [TelegramRetryAfter(method=method, message="Too Many Requests",
                            retry_after=0), "ok"])
    assert value == "ok" and calls == 2


async def test_send_is_never_retried_after_a_network_error():
    """Запрос мог дойти до Telegram: повтор продублировал бы финансовое
    уведомление. Такие сбои уходят в очередь Записей, которая знает
    про ключ повтора."""
    middleware = ThrottleMiddleware(global_rate=1000, chat_rate=1000)
    method = SendMessage(chat_id=1, text="x")
    with pytest.raises(TelegramNetworkError):
        await _call(middleware, method,
                    [TelegramNetworkError(method=method, message="reset"), "ok"])


async def test_idempotent_calls_are_retried_after_a_network_error():
    middleware = ThrottleMiddleware(global_rate=1000, chat_rate=1000)
    method = EditMessageText(chat_id=1, message_id=2, text="x")
    value, calls = await _call(
        middleware, method,
        [TelegramServerError(method=method, message="502"), "ok"])
    assert value == "ok" and calls == 2


async def test_answering_a_callback_is_retried_so_no_button_hangs():
    middleware = ThrottleMiddleware(global_rate=1000, chat_rate=1000)
    method = AnswerCallbackQuery(callback_query_id="1")
    value, _ = await _call(
        middleware, method,
        [TelegramServerError(method=method, message="502"), True])
    assert value is True


async def test_global_rate_limit_paces_the_fan_out():
    middleware = ThrottleMiddleware(global_rate=50, chat_rate=1000)

    async def make_request(bot, method):
        return "ok"

    started = asyncio.get_running_loop().time()
    for index in range(60):
        await middleware(make_request, None, SendMessage(chat_id=index, text="x"))
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed > 0.1        # 60 сообщений быстрее лимита не уходят


async def test_inbound_throttle_drops_a_flood_and_answers_the_button():
    middleware = InboundThrottle(rate=1, burst=2)
    handled = []

    async def handler(event, data):
        handled.append(event)
        return "ok"

    answer = []
    callback = SimpleNamespace(answer=lambda text: answer.append(text) or _done())
    event = SimpleNamespace(callback_query=callback)
    data = {"event_chat": SimpleNamespace(id=1)}
    for _ in range(5):
        await middleware(handler, event, data)
    assert len(handled) == 2
    assert answer


def _done():
    async def noop():
        return None
    return noop()


# ------------------------------------------------------- Разбор нажатий

@pytest.mark.parametrize("data, expected", [
    ("ad:card:12", 12), ("po:ok:7", 7), ("st:day:0", 0),
    ("ad:card:abc", None), ("ad:card:", None), ("", None), (None, None),
    ("ac:o:99999999999999999999", None), ("ad:card:-5", -5),
])
def test_cb_int_never_raises(data, expected):
    assert cb_int(data) == expected


@pytest.mark.parametrize("data, expected", [
    ("no:d:1:30", (1, 30)), ("ad:setown:5:0", (5, 0)),
    ("no:d:1:x", None), ("no:d", None), (None, None),
])
def test_cb_ints_never_raises(data, expected):
    assert cb_ints(data, 2) == expected


def test_cb_tail_extracts_string_keys():
    assert cb_tail("st:set:price_1_30", "st:set:") == "price_1_30"
    assert cb_tail("st:tgl:x", "st:set:") is None
    assert cb_tail(None, "st:set:") is None


# ------------------------------------------------------- Обработчик ошибок

async def test_error_handler_always_answers_the_button_and_counts():
    class FakeDispatcher:
        def __init__(self):
            self.handler = None

        def errors(self):
            def decorator(func):
                self.handler = func
                return func
            return decorator

    dispatcher = FakeDispatcher()
    errors.setup_error_handler(dispatcher)
    errors.COUNTERS.clear()
    answered = []

    async def answer(text, show_alert=False):
        answered.append(text)

    update = SimpleNamespace(update_id=1,
                             callback_query=SimpleNamespace(answer=answer))
    event = SimpleNamespace(update=update, exception=RuntimeError("сбой"))
    assert await dispatcher.handler(event) is True
    assert errors.COUNTERS["errors"] == 1
    assert answered == [errors.GENERIC_ERROR]
