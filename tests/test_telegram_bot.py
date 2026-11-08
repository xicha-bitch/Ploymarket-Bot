"""Telegram notifier: no-op without secrets, confirm gate, command handlers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from polymaker.telegram_bot import TelegramBot, TelegramSecrets, telegram_configured


def test_telegram_configured_checks_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert telegram_configured() is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    assert telegram_configured() is False
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    assert telegram_configured() is True


def test_disabled_bot_passthrough() -> None:
    bot = TelegramBot(secrets=None)
    assert bot.enabled is False
    assert bot.needs_confirm(10.0) == (False, "")

    async def _go() -> None:
        assert await bot.request_confirm("x") is True
        assert await bot.send("hi") is None
        assert await bot.smoke_send() is False

    asyncio.run(_go())


def test_needs_confirm_first_day_and_size_bump() -> None:
    secrets = TelegramSecrets(token="t", chat_id="1", confirm=True)
    bot = TelegramBot(secrets)
    need, reason = bot.needs_confirm(5.0)
    assert need is True
    assert "first place" in reason
    bot.note_placed(5.0)
    assert bot.needs_confirm(5.5) == (False, "")
    need2, reason2 = bot.needs_confirm(8.0)  # >25% and +$1
    assert need2 is True
    assert "size increase" in reason2


def test_confirm_disabled_notifies_but_passes() -> None:
    secrets = TelegramSecrets(token="t", chat_id="1", confirm=False)
    bot = TelegramBot(secrets)
    assert bot.needs_confirm(9.0) == (False, "")

    @respx.mock
    async def _go() -> None:
        route = respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
            return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        )
        assert await bot.request_confirm("would ask") is True
        assert route.called

    asyncio.run(_go())


@respx.mock
def test_smoke_send_success() -> None:
    secrets = TelegramSecrets(token="t", chat_id="42", confirm=False)
    bot = TelegramBot(secrets)
    respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )

    async def _go() -> bool:
        return await bot.smoke_send()

    assert asyncio.run(_go()) is True


@respx.mock
def test_handle_commands_status_pnl_stop() -> None:
    secrets = TelegramSecrets(token="t", chat_id="99", confirm=False)
    bot = TelegramBot(secrets)
    status = AsyncMock(return_value="ok-status")
    pnl = AsyncMock(return_value="ok-pnl")
    on_stop = AsyncMock()
    bot.bind(status=status, pnl=pnl, on_stop=on_stop)
    send_route = respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 2}})
    )

    async def _go() -> None:
        await bot._handle_message({"chat": {"id": 99}, "text": "/status"})
        await bot._handle_message({"chat": {"id": 99}, "text": "/pnl"})
        await bot._handle_message({"chat": {"id": 99}, "text": "/stop"})

    asyncio.run(_go())
    assert status.await_count == 1
    assert pnl.await_count == 1
    assert on_stop.await_count == 1
    assert bot.stop_requested is True
    assert send_route.call_count >= 3


@respx.mock
def test_confirm_approve_via_callback() -> None:
    secrets = TelegramSecrets(token="t", chat_id="7", confirm=True)
    bot = TelegramBot(secrets, confirm_timeout_s=2.0)
    respx.post(url__regex=r"https://api\.telegram\.org/bot.*/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})
    )
    respx.post(url__regex=r"https://api\.telegram\.org/bot.*/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": True})
    )

    async def _go() -> bool:
        task = asyncio.create_task(bot.request_confirm("bump"))
        # wait until pending appears
        for _ in range(50):
            if bot._pending:
                break
            await asyncio.sleep(0.02)
        req_id = next(iter(bot._pending))
        await bot._handle_callback(
            {
                "id": "cq1",
                "data": f"ok:{req_id}",
                "message": {"chat": {"id": 7}},
            }
        )
        return await task

    assert asyncio.run(_go()) is True


def test_secrets_from_env_never_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramSecrets.from_env() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("TELEGRAM_CONFIRM", "1")
    s = TelegramSecrets.from_env()
    assert s is not None
    assert s.confirm is True
    # sanity: values exist in object but test must not print them
    assert len(s.token) > 0
    assert len(s.chat_id) > 0
