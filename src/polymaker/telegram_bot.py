"""Telegram notify/control side-channel for polymaker.

Uses the Bot HTTP API via httpx (no extra dependency). Optional:
  - push fills / place failures / periodic status
  - /status /pnl /stop commands (long-poll)
  - approve/deny confirm gate before first place of day or size bumps

Secrets come only from env (.env locally). Never log token or chat values.
No-ops cleanly when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset so live
trading is never blocked by a missing notifier.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from polymaker.logging import get_logger

log = get_logger("telegram")

_API = "https://api.telegram.org"
_POLL_TIMEOUT_S = 25
_DEFAULT_CONFIRM_TIMEOUT_S = 90.0
_STATUS_INTERVAL_S = 900.0  # 15 min


def _env_present(name: str) -> bool:
    """True if the env key exists and is non-empty. Never returns the value."""
    v = os.environ.get(name)
    return bool(v and v.strip())


def telegram_configured() -> bool:
    """Key-name presence check only — never reads token/chat values into logs."""
    return _env_present("TELEGRAM_BOT_TOKEN") and _env_present("TELEGRAM_CHAT_ID")


@dataclass(frozen=True, slots=True)
class TelegramSecrets:
    """In-memory secrets. Do not print or structlog these fields."""

    token: str
    chat_id: str
    confirm: bool = False

    @classmethod
    def from_env(cls) -> TelegramSecrets | None:
        token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat_id:
            return None
        confirm_raw = (os.environ.get("TELEGRAM_CONFIRM") or "").strip().lower()
        confirm = confirm_raw in ("1", "true", "yes", "on")
        return cls(token=token, chat_id=chat_id, confirm=confirm)


StatusProvider = Callable[[], Awaitable[str] | str]
PnlProvider = Callable[[], Awaitable[str] | str]
StopHandler = Callable[[], Awaitable[None] | None]


class TelegramBot:
    """Async Telegram notifier + optional confirm/control loop."""

    def __init__(
        self,
        secrets: TelegramSecrets | None = None,
        *,
        proxy: str | None = None,
        confirm_timeout_s: float = _DEFAULT_CONFIRM_TIMEOUT_S,
        status_interval_s: float = _STATUS_INTERVAL_S,
    ) -> None:
        self._secrets = secrets if secrets is not None else TelegramSecrets.from_env()
        self._proxy = proxy
        self._confirm_timeout_s = confirm_timeout_s
        self._status_interval_s = status_interval_s
        self._offset = 0
        self._pending: dict[str, asyncio.Future[bool]] = {}
        self._stop = asyncio.Event()
        self.stop_requested = False
        self._status_provider: StatusProvider | None = None
        self._pnl_provider: PnlProvider | None = None
        self._on_stop: StopHandler | None = None
        # confirm bookkeeping (engine-facing)
        self._confirm_day: str | None = None
        self._max_day_notional: float = 0.0

    @property
    def enabled(self) -> bool:
        return self._secrets is not None

    @property
    def confirm_enabled(self) -> bool:
        return bool(self._secrets and self._secrets.confirm)

    def bind(
        self,
        *,
        status: StatusProvider | None = None,
        pnl: PnlProvider | None = None,
        on_stop: StopHandler | None = None,
    ) -> None:
        self._status_provider = status
        self._pnl_provider = pnl
        self._on_stop = on_stop

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": httpx.Timeout(40.0, connect=10.0)}
        if self._proxy:
            kwargs["proxy"] = self._proxy
        return kwargs

    def _url(self, method: str) -> str:
        assert self._secrets is not None
        # token only used in URL construction; never logged
        return f"{_API}/bot{self._secrets.token}/{method}"

    async def _api(self, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self._secrets:
            return {}
        # httpx INFO logs full request URLs; Telegram puts the token in the path.
        httpx_log = logging.getLogger("httpx")
        prev_level = httpx_log.level
        httpx_log.setLevel(logging.WARNING)
        try:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                r = await client.post(self._url(method), json=payload or {})
                r.raise_for_status()
                data = r.json()
                if not data.get("ok", False):
                    log.warning("telegram_api_not_ok", method=method)
                    return {}
                return data
        except httpx.HTTPError as exc:
            log.warning("telegram_api_failed", method=method, err=type(exc).__name__)
            return {}
        finally:
            httpx_log.setLevel(prev_level)

    async def send(self, text: str, *, reply_markup: dict[str, Any] | None = None) -> int | None:
        """Send a plain text message to the configured chat. Returns message_id."""
        if not self._secrets:
            return None
        payload: dict[str, Any] = {
            "chat_id": self._secrets.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        data = await self._api("sendMessage", payload)
        result = data.get("result") or {}
        mid = result.get("message_id")
        return int(mid) if mid is not None else None

    async def smoke_send(self) -> bool:
        """Send a one-line link check. Never logs secrets."""
        if not self.enabled:
            log.info("telegram_smoke_skipped", reason="not_configured")
            return False
        mid = await self.send("bot linked")
        ok = mid is not None
        log.info("telegram_smoke", ok=ok)
        return ok

    # ── notify helpers ──────────────────────────────────────────────────
    async def notify_fill(
        self,
        *,
        side: str,
        price: float,
        size: float,
        token_hint: str,
        trade_id: str | None = None,
    ) -> None:
        notional = price * size
        tid = f" id={trade_id[:12]}" if trade_id else ""
        await self.send(
            f"✅ FILL {side} {size:.2f} @ {price:.3f} (~${notional:.2f}) "
            f"tok={token_hint}{tid}"
        )

    async def notify_place_failed(self, *, n: int, detail: str = "") -> None:
        extra = f" — {detail[:200]}" if detail else ""
        await self.send(f"❌ PLACE FAILED ({n} quotes){extra}")

    async def notify_status(self, text: str) -> None:
        await self.send(text)

    # ── confirm gate ────────────────────────────────────────────────────
    def needs_confirm(self, place_notional: float) -> tuple[bool, str]:
        """Return (need, reason) for first-place-of-day or size increase.

        When confirm is disabled or bot unconfigured, always (False, "").
        """
        if not self.enabled or not self.confirm_enabled:
            return False, ""
        today = date.today().isoformat()
        if self._confirm_day != today:
            return True, f"first place of day ({today}), ~${place_notional:.2f}"
        # size bump: >25% above today's max and at least +$1 notional
        if (
            place_notional > self._max_day_notional * 1.25
            and place_notional > self._max_day_notional + 1.0
        ):
            return True, (
                f"size increase ${self._max_day_notional:.2f} → ${place_notional:.2f}"
            )
        return False, ""

    def note_placed(self, place_notional: float) -> None:
        today = date.today().isoformat()
        if self._confirm_day != today:
            self._confirm_day = today
            self._max_day_notional = place_notional
        else:
            self._max_day_notional = max(self._max_day_notional, place_notional)

    async def request_confirm(self, reason: str, *, detail: str = "") -> bool:
        """Ask operator via inline Approve/Deny. True = proceed.

        If Telegram is not configured, or confirm is off, returns True (pass-through).
        On timeout / deny, returns False.
        """
        if not self.enabled:
            return True
        if not self.confirm_enabled:
            # notify-only path still ready when confirm flag is off
            await self.send(f"ℹ️ would ask confirm: {reason}" + (f"\n{detail}" if detail else ""))
            return True

        req_id = uuid.uuid4().hex[:10]
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._pending[req_id] = fut
        markup = {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"ok:{req_id}"},
                {"text": "🚫 Deny", "callback_data": f"no:{req_id}"},
            ]],
        }
        body = f"⚠️ CONFIRM required\n{reason}"
        if detail:
            body += f"\n{detail}"
        body += f"\n(timeout {int(self._confirm_timeout_s)}s)"
        mid = await self.send(body, reply_markup=markup)
        if mid is None:
            self._pending.pop(req_id, None)
            log.warning("telegram_confirm_send_failed")
            return False
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=self._confirm_timeout_s)
        except TimeoutError:
            log.warning("telegram_confirm_timeout", req=req_id)
            with contextlib.suppress(asyncio.InvalidStateError):
                if not fut.done():
                    fut.set_result(False)
            await self.send(f"⏳ confirm timed out — trade skipped ({req_id})")
            return False
        finally:
            self._pending.pop(req_id, None)

    def _resolve_confirm(self, req_id: str, approved: bool) -> None:
        fut = self._pending.get(req_id)
        if fut is not None and not fut.done():
            fut.set_result(approved)

    # ── long-poll command loop ──────────────────────────────────────────
    async def run_forever(self) -> None:
        """Long-poll getUpdates until stop(). No-op when unconfigured."""
        if not self.enabled:
            log.info("telegram_disabled")
            return
        log.info("telegram_poll_started", confirm=self.confirm_enabled)
        await self.send(
            "polymaker telegram 已上线。可发 hi / 在吗 / /status；发 /help 看命令。"
        )
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram_poll_error", err=type(exc).__name__)
                await asyncio.sleep(2.0)

    async def run_status_loop(self) -> None:
        """Periodic status push. No-op when unconfigured."""
        if not self.enabled:
            return
        while not self._stop.is_set():
            try:
                await asyncio.sleep(self._status_interval_s)
                if self._stop.is_set():
                    break
                text = await self._call_provider(self._status_provider)
                if text:
                    await self.notify_status(f"⏱ periodic status\n{text}")
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram_status_loop_error", err=type(exc).__name__)

    def stop(self) -> None:
        self._stop.set()

    async def _poll_once(self) -> None:
        assert self._secrets is not None
        data = await self._api(
            "getUpdates",
            {
                "offset": self._offset,
                "timeout": _POLL_TIMEOUT_S,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        for upd in data.get("result") or []:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            if "callback_query" in upd:
                await self._handle_callback(upd["callback_query"])
            elif "message" in upd:
                await self._handle_message(upd["message"])

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        data = str(cq.get("data") or "")
        cq_id = cq.get("id")
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if not self._secrets or str(chat) != str(self._secrets.chat_id):
            if cq_id:
                await self._api("answerCallbackQuery", {"callback_query_id": cq_id, "text": "ignored"})
            return
        approved = data.startswith("ok:")
        denied = data.startswith("no:")
        if approved or denied:
            req_id = data.split(":", 1)[1]
            self._resolve_confirm(req_id, approved)
            if cq_id:
                await self._api(
                    "answerCallbackQuery",
                    {"callback_query_id": cq_id, "text": "approved" if approved else "denied"},
                )
            await self.send("✅ approved" if approved else "🚫 denied")
            return
        if cq_id:
            await self._api("answerCallbackQuery", {"callback_query_id": cq_id})

    @staticmethod
    def _help_text() -> str:
        return (
            "你好，我是 Polymarket 交易通知 bot。\n\n"
            "命令：\n"
            "/status — 仓位与挂单\n"
            "/pnl — 权益 / 当日 PnL\n"
            "/stop — 撤单并暂停下单\n"
            "/help — 本说明\n\n"
            "也可发「hi」「在吗」「跟我聊天」测连通；"
            "不查天气，交易状态请用 /status。"
        )

    @staticmethod
    def _normalize_chat_text(text: str) -> str:
        """Lowercase + strip common punctuation for casual matching."""
        t = text.strip().lower()
        for ch in "！!？?。.~～…、，,":
            t = t.replace(ch, "")
        return t.strip()

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        chat = (msg.get("chat") or {}).get("id")
        if not self._secrets or str(chat) != str(self._secrets.chat_id):
            return
        text = str(msg.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/"):
            cmd = text.split()[0].split("@", 1)[0].lower()
            if cmd in ("/start", "/help"):
                await self.send(self._help_text())
            elif cmd == "/status":
                body = await self._call_provider(self._status_provider) or "(暂无状态数据)"
                await self.send(f"📊 状态\n{body}")
            elif cmd == "/pnl":
                body = await self._call_provider(self._pnl_provider) or "(暂无 PnL 数据)"
                await self.send(f"📈 PnL\n{body}")
            elif cmd == "/stop":
                self.stop_requested = True
                await self.send("🛑 已请求停止 — 正在撤单并暂停下单")
                if self._on_stop is not None:
                    result = self._on_stop()
                    if asyncio.iscoroutine(result):
                        await result
            else:
                await self.send(f"未知命令 {cmd}。发 /help 看可用命令。")
            return

        norm = self._normalize_chat_text(text)
        if norm in ("hi", "hello", "hey", "在吗", "在么", "在不在", "跟我聊天", "聊天"):
            await self.send(
                "在的 🙂 我是交易通知 bot，不是闲聊助手。"
                "要看交易状态发 /status，更多命令 /help。"
            )
        elif "天气" in norm:
            await self.send("我不会查天气～这是交易通知 bot。看交易状态请发 /status。")
        else:
            await self.send("没听懂。试试 /help，或发 /status 看交易状态。")

    async def _call_provider(self, provider: StatusProvider | PnlProvider | None) -> str:
        if provider is None:
            return ""
        result = provider()
        if asyncio.iscoroutine(result):
            result = await result
        return str(result)


def schedule_notify(coro: Awaitable[Any]) -> None:
    """Fire-and-forget a notify coroutine from sync callbacks."""
    with contextlib.suppress(RuntimeError):
        asyncio.get_running_loop().create_task(coro)
