"""Engine: wires every component into a single async event loop.

Data flow per market:
  market WS -> OrderBook -> (wake) -> Quoter task -> strategy (pure) -> reconcile
  -> ExecutionGateway ; user WS -> StateStore ; periodic REST reconcile + heartbeat.

One lightweight quoter task per market, woken by book/fill events and debounced.
The strategy layer is pure; the engine owns all the state and I/O around it.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any

from polymaker.alerts import Alerter
from polymaker.catalog.gamma import GammaClient, fetch_reward_rates, parse_market
from polymaker.catalog.store import CatalogStore
from polymaker.config import Config, StrategyProfile
from polymaker.domain import Fill, MarketMeta, OpenOrder, Quote, Regime, Side, TargetQuotes
from polymaker.execution.batch import chunk_quotes
from polymaker.execution.gateway import (
    ExecutionGateway,
    OpenOrdersReadError,
    PositionsReadError,
    TradesReadError,
)
from polymaker.execution.trades import fills_from_rest_trade
from polymaker.execution.intent import (
    PlacementIntent,
    PlacementIntentStore,
    client_key_for,
)
from polymaker.execution.reconciler import reconcile
from polymaker.journal import Journal
from polymaker.logging import get_logger
from polymaker.marketdata.parse import TradePrint
from polymaker.marketdata.service import MarketDataService
from polymaker.merge import Merger
from polymaker.risk.manager import RiskManager
from polymaker.state.store import StateStore
from polymaker.state.tracker import UserEventProcessor
from polymaker.strategy.estimators import (
    FlowEstimator,
    MarketEstimators,
    MarkoutTracker,
    VolEstimator,
)
from polymaker.strategy.budget import filter_quotes_to_budget, place_headroom_usdc
from polymaker.strategy.maker_gates import compute_exit_urgency
from polymaker.strategy.quoting import QuoteInputs, compute_fair_value, construct_quotes
from polymaker.strategy.regime import RegimeInputs, RegimeMachine
from polymaker.telegram_bot import (
    TelegramBot,
    TelegramSecrets,
    parse_notify_mode,
    schedule_notify,
)
from polymaker.telegram_copy import format_pnl_report, format_status_report
from polymaker.userstream.client import UserStream

log = get_logger("engine")

# CLOB maker fee is 0; keep a tiny buffer for rounding / allowance dust.
_FEE_BUFFER_USDC = 0.01
_COLLATERAL_REFRESH_S = 20.0


class Engine:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._running = False

        self.journal = Journal(cfg.paths.journal_dir, enabled=cfg.engine.journal, day="live")
        self.state = StateStore(cfg.paths.db)
        self.catalog = CatalogStore(cfg.paths.db)
        self.gateway = ExecutionGateway(cfg, self.journal)
        self.risk = RiskManager(cfg.risk, self.state)
        self.merger = Merger(cfg)
        self.alerter = Alerter(cfg.secrets.alert_webhook_url, proxy=cfg.proxy)
        self.telegram = self._make_telegram(cfg)

        self.md = MarketDataService(on_dirty=self._on_dirty, on_trade=self._on_trade,
                                    journal=self.journal, proxy=cfg.proxy)
        self.user_proc = UserEventProcessor(self.state, on_change=self._wake_cid,
                                            on_fill=self._on_fill)
        self.user: UserStream | None = None

        # per-market state
        self.metas: dict[str, MarketMeta] = {}
        self.profiles: dict[str, StrategyProfile] = {}
        self.est: dict[str, MarketEstimators] = {}
        self.regime_m: dict[str, RegimeMachine] = {}
        self._dirty: dict[str, asyncio.Event] = {}
        self._sweep: dict[str, bool] = {}
        self._merging: set[str] = set()
        self._token_cid: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}  # per-market: serialize recompute vs reconcile
        self._halted: set[str] = set()  # markets closed/resolved/not-accepting
        self._last_quote_fv: dict[str, float] = {}  # requote suppression
        # inventory open timestamps for exit urgency (token_id -> first non-flat ts)
        self._inv_open_ts: dict[str, float] = {}
        self._intents = PlacementIntentStore()
        # supervised tasks: name -> (factory, task) so a dead task restarts
        self._task_specs: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._aux_tasks: list[asyncio.Task[Any]] = []  # fire-and-forget (merges)
        # health / recovery signals
        self._reconcile_now = asyncio.Event()
        self._user_started = False  # user WS task launched (live mode)
        self._hb_was_down = False
        self._chain_lock = asyncio.Lock()  # serialize on-chain txs (nonce safety)
        self._collateral_usdc: float | None = None
        self._collateral_ts: float = 0.0
        self._restart_backoff_s: dict[str, float] = {}
        self._last_budget_skip: dict[str, tuple[str, ...]] = {}
        self._entry_blocked_infeasible: dict[str, bool] = {}
        # positions Data-API recovery: honor Retry-After / exponential backoff
        self._positions_fail_streak: int = 0
        self._positions_backoff_until: float = 0.0
        self._positions_healthy: bool = True
        self._last_heartbeat_ok_ts: float = 0.0
        self._last_reconcile_ok_ts: float = 0.0
        self._last_reconcile_fail_ts: float = 0.0
        # Recovery / authority gates: unknown official data => stay protective.
        self._recovery_blocked: bool = False
        self._trades_backfill_ok: bool = False
        self._last_trades_ok_ts: float = 0.0
        self._session_start_ts: float = time.time()

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        await self.gateway.connect()
        await self._refresh_collateral(force=True)
        await self._resolve_markets()
        if not self.metas:
            log.warning("no_markets_selected", hint="add markets to config/markets.toml, run `polymaker scan`")
        # freshen reward/fee/end-date params from live Gamma BEFORE quoting so a
        # stale catalog (e.g. old reward min-size) can't mis-size our orders
        await self.refresh_market_metadata()
        # Restore day baseline + sticky latch before reconcile so conservation
        # can persist and restart cannot wipe an in-day daily_loss halt.
        self.risk.bootstrap_session()
        await self._startup_reconcile()

        # subscribe feeds
        self.md.set_markets([(cid, [m.yes.token_id, m.no.token_id]) for cid, m in self.metas.items()])
        self.user = UserStream(
            self.gateway.creds, self.gateway.address, self.user_proc,
            other_token=self._other_token, condition_of_token=self._cid_of_token,
            journal=self.journal, proxy=self.cfg.proxy,
            on_reconnect=self._on_user_reconnect,
        )
        self.user.set_markets(list(self.metas))

        # launch supervised tasks (a dead task is restarted, never silently gone)
        self._spawn("market_ws", self.md.run)
        assert self.user is not None
        self._spawn("user_ws", self.user.run)
        # register the dead-man switch BEFORE any quoter can place an order,
        # so a crash between placing and the first heartbeat still auto-cancels
        with contextlib.suppress(Exception):
            await self.gateway.heartbeat()
        self._spawn("heartbeat", self._heartbeat_loop)
        self._user_started = True
        self._spawn("reconcile", self._reconcile_loop)
        self._spawn("metadata", self._metadata_refresh_loop)
        self._spawn("maintenance", self._maintenance_loop)
        for cid in self.metas:
            self._spawn(f"quote:{cid[:8]}", lambda c=cid: self._quoter(c))
        self._spawn("supervisor", self._supervise)
        # Telegram side-channel: faults stay inside the poll task (never kills trade).
        self.telegram.bind(
            status=self._telegram_status_text,
            pnl=self._telegram_pnl_text,
            on_stop=self._telegram_stop,
        )
        if self.telegram.enabled:
            self._spawn("telegram", self.telegram.run_forever)
            if self.telegram.push_periodic:
                self._spawn("telegram_status", self.telegram.run_status_loop)
        self.risk.roll_day_if_needed()
        log.info(
            "engine_started",
            markets=len(self.metas),
            day=self.risk.day_key,
            daily_loss_latched=self.risk.daily_loss_latched,
            trades_backfill_ok=self._trades_backfill_ok,
        )

    @staticmethod
    def _make_telegram(cfg: Config) -> TelegramBot:
        """Build notifier from env-backed secrets. Never logs token/chat values."""
        secrets: TelegramSecrets | None = None
        if cfg.secrets.has_telegram:
            secrets = TelegramSecrets(
                token=cfg.secrets.telegram_bot_token or "",
                chat_id=cfg.secrets.telegram_chat_id or "",
                confirm=bool(cfg.secrets.telegram_confirm),
                notify_mode=parse_notify_mode(cfg.secrets.telegram_notify),
            )
        bot = TelegramBot(secrets, proxy=cfg.proxy)
        if bot.enabled:
            log.info(
                "telegram_enabled",
                confirm=bot.confirm_enabled,
                notify=bot.notify_mode,
            )
        else:
            log.info("telegram_disabled")
        return bot

    def _spawn(self, name: str, factory: Any) -> None:
        self._task_specs[name] = factory
        self._tasks[name] = asyncio.create_task(factory(), name=name)

    _supervise_interval_s: float = 5.0

    async def _supervise(self) -> None:
        """Restart any engine task that exits while we're running, with backoff."""
        while self._running:
            await asyncio.sleep(self._supervise_interval_s)
            self._aux_tasks = [task for task in self._aux_tasks if not task.done()]
            for name, task in list(self._tasks.items()):
                if name == "supervisor":
                    continue
                if not task.done():
                    self._restart_backoff_s.pop(name, None)
                    continue
                if not self._running:
                    return
                delay = self._restart_backoff_s.get(name, 1.0)
                exc = None
                with contextlib.suppress(asyncio.CancelledError, asyncio.InvalidStateError):
                    exc = task.exception()
                log.critical(
                    "task_died_restarting",
                    task=name,
                    err=str(exc) if exc else "exited",
                    backoff_s=delay,
                )
                self.alerter.alert(
                    "task_died",
                    f"{name} died: {exc}",
                    critical=delay <= 1.0,
                )
                await asyncio.sleep(delay)
                if not self._running:
                    return
                self._tasks[name] = asyncio.create_task(self._task_specs[name](), name=name)
                self._restart_backoff_s[name] = min(max(delay, 1.0) * 2.0, 60.0)

    async def run_forever(self) -> None:
        await self.start()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self._tasks.values(), *self._aux_tasks)

    async def shutdown(self) -> None:
        self._running = False
        log.info("engine_shutdown")
        self.telegram.stop()
        self.md.stop()
        if self.user:
            self.user.stop()
        for t in [*self._tasks.values(), *self._aux_tasks]:
            t.cancel()
        with contextlib.suppress(Exception):
            await self.gateway.cancel_all()
        self.gateway.close()
        self.journal.close()
        self.state.close()
        self.catalog.close()

    # ── market resolution ───────────────────────────────────────────────
    async def _resolve_markets(self) -> None:
        reward_rates: dict[str, float] | None = None
        async with GammaClient(self.cfg.wallet.gamma_host) as gamma:
            for entry in self.cfg.enabled_markets:
                meta = self.catalog.get_by_slug(entry.slug) if entry.slug else None
                if meta is None and entry.condition_id:
                    meta = self.catalog.get(entry.condition_id)
                if meta is None:  # fall back to a live Gamma fetch
                    if reward_rates is None:
                        reward_rates = await fetch_reward_rates(self.cfg.wallet.clob_host)
                    meta = await self._fetch_meta(gamma, entry.slug, entry.condition_id, reward_rates)
                if meta is None:
                    log.warning("market_unresolved", ref=entry.ref)
                    continue
                self.metas[meta.condition_id] = meta
                self.profiles[meta.condition_id] = self.cfg.profile_for(entry)
                self.est[meta.condition_id] = self._make_estimators(self.profiles[meta.condition_id])
                self.regime_m[meta.condition_id] = RegimeMachine()
                self._dirty[meta.condition_id] = asyncio.Event()
                self._locks[meta.condition_id] = asyncio.Lock()
                for tok in (meta.yes.token_id, meta.no.token_id):
                    self._token_cid[tok] = meta.condition_id

    async def _fetch_meta(
        self, gamma: GammaClient, slug: str | None, condition_id: str | None,
        reward_rates: dict[str, float],
    ) -> MarketMeta | None:
        tag_id = self.catalog.cached_tag("politics")
        if tag_id is None:  # cold start: resolve + cache so the sweep is scoped
            tag_id = await gamma.resolve_tag_id("politics")
            if tag_id:
                self.catalog.cache_tag("politics", tag_id)
        async for raw in gamma.iter_markets(tag_id=tag_id, max_pages=25):
            if (slug and raw.get("slug") == slug) or (condition_id and raw.get("conditionId") == condition_id):
                m = parse_market(raw, reward_rates)
                if m:
                    self.catalog.upsert_market(m)
                return m
        return None

    @staticmethod
    def _make_estimators(p: StrategyProfile) -> MarketEstimators:
        return MarketEstimators(
            vol=VolEstimator(p.vol_short_halflife_s, p.vol_long_halflife_s),
            flow=FlowEstimator(p.flow_ewma_halflife_s),
            markout=MarkoutTracker(),
        )

    async def _startup_reconcile(self) -> None:
        """Adopt a known order+position view or fail closed before quoting."""
        try:
            await self.gateway.cancel_all()
        except Exception as exc:  # noqa: BLE001
            log.error("startup_cancel_all_failed", err=str(exc)[:200])
            self.alerter.alert(
                "startup_cancel_all_failed",
                f"cancel-all failed; verifying open orders before quoting: {exc}",
                critical=True,
            )

        try:
            leftover = await self.gateway.open_orders()
        except Exception as exc:  # noqa: BLE001
            log.error("startup_open_orders_failed", err=str(exc)[:200])
            self.alerter.alert(
                "startup_open_orders_failed",
                f"cannot verify open orders at startup: {exc}",
                critical=True,
            )
            raise RuntimeError("startup_open_orders_failed") from exc

        if leftover:
            log.warning("startup_orders_remain", n=len(leftover))
            try:
                for tok in {o.token_id for o in leftover}:
                    await self.gateway.cancel_asset(tok)
                still = await self.gateway.open_orders()
            except Exception as exc:  # noqa: BLE001
                log.error("startup_orders_unverified", err=str(exc)[:200])
                self.alerter.alert(
                    "startup_orders_unverified",
                    f"could not finish cancelling leftover startup orders: {exc}",
                    critical=True,
                )
                raise RuntimeError("startup_orders_unverified") from exc
            for tok in self._token_cid:
                self.state.replace_open_orders(
                    tok, [o for o in still if o.token_id == tok], grace_s=0.0
                )
            if still:
                log.error("startup_orders_stuck", n=len(still))
                self.alerter.alert(
                    "startup_orders_stuck",
                    f"{len(still)} orders survived cancel-all",
                    critical=True,
                )
                raise RuntimeError("startup_orders_stuck")
        else:
            for tok in self._token_cid:
                self.state.replace_open_orders(tok, [], grace_s=0.0)

        # purge positions that leaked in for markets we don't trade (manual UI
        # bets etc.) so they can't distort exposure caps or PnL
        self.state.drop_untracked_positions(set(self._token_cid))
        try:
            positions = self._only_traded(await self.gateway.positions())
        except PositionsReadError as exc:
            # Fail closed: a 410/transport miss is NOT empty inventory.
            log.error("startup_positions_failed", err=str(exc))
            self.alerter.alert(
                "startup_positions_failed",
                f"positions read failed (not treating as zero): {exc}",
                critical=True,
            )
            raise
        # Empty dict is a valid complete snapshot and must zero stale locals.
        # Prefer fill backfill BEFORE position snapshot so cash is authoritative.
        trades_ok = await self._backfill_trades(reason="startup")
        deltas = self.state.reconcile_positions(positions, is_snapshot=True)
        self._apply_inventory_conservation(deltas)
        # Positions succeeded here; trades may still be unknown (TLS). Stay
        # protective until trades backfill also succeeds — do not auto-clear halt.
        self._recovery_blocked = not trades_ok
        if trades_ok:
            self.risk.try_clear_false_daily_loss(authoritative_ok=True)
        log.info(
            "startup_positions",
            n=len(positions),
            deltas=len(deltas),
            trades_ok=trades_ok,
            recovery_blocked=self._recovery_blocked,
            daily_loss_latched=self.risk.daily_loss_latched,
        )

    def _only_traded(self, positions: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
        """Scope account positions to tokens WE trade. Manual/UI positions in
        other markets are the operator's business — they must not enter our
        state, exposure caps, or PnL."""
        return {t: v for t, v in positions.items() if t in self._token_cid}

    def _apply_inventory_conservation(
        self, deltas: list[tuple[str, float, float, float]]
    ) -> None:
        """Conserve equity for inventory changes that did not come from fills."""
        for token_id, old_size, new_size, unit in deltas:
            mark = self.risk.unit_value(token_id, fallback=unit)
            delta_inv = (new_size - old_size) * mark
            if abs(delta_inv) < 1e-12:
                continue
            self.risk.note_unexplained_inventory_delta(delta_inv)

    def _trades_backfill_after_ts(self) -> int:
        """Bounded lookback that covers cross-day unreconciled matched orders.

        Do NOT clamp up to Shanghai midnight — that dropped the 20:38 incident
        fill on a next-day restart (after_ts≈23:59).
        """
        lookback_s = 36 * 3600.0
        after = int(max(0.0, self._session_start_ts - lookback_s))
        hint = self.state.earliest_matched_order_ts()
        if hint is not None and hint > 0:
            after = min(after, max(0, int(hint) - 120))
        return after

    def _shanghai_day_start_ts(self) -> float | None:
        if not self.risk.day_key:
            return None
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo

            return datetime.strptime(self.risk.day_key, "%Y-%m-%d").replace(
                tzinfo=ZoneInfo("Asia/Shanghai")
            ).timestamp()
        except (ValueError, TypeError, OSError):
            return None

    async def _backfill_trades(self, *, reason: str) -> bool:
        """Pull authoritative trades and apply with store dedupe.

        Transport/TLS failure → False (unknown ≠ empty). A successful fetch is
        not yet ``trades_ok``: coverage must also clear known matched orders
        missing fills. Never invents fills from local PnL/order_log.
        """
        after = self._trades_backfill_after_ts()
        day_start_ts = self._shanghai_day_start_ts()
        try:
            rows = await self.gateway.get_trades(after_ts=after)
        except TradesReadError as exc:
            self._trades_backfill_ok = False
            log.warning("trades_backfill_failed", reason=reason, err=str(exc)[:160])
            return False

        our_order_ids = self.state.known_order_ids()
        applied = 0
        for row in rows:
            for fill in fills_from_rest_trade(
                row,
                self.gateway.address,
                self._other_token_for_fill,
                funder_address=self.gateway.funder,
                our_order_ids=our_order_ids,
            ):
                if fill.token_id not in self._token_cid:
                    continue
                count_toward_daily = (
                    day_start_ts is None or fill.ts <= 0.0 or fill.ts >= day_start_ts
                )
                equity_before = self.risk.equity
                if not self.state.apply_fill(fill):
                    continue
                self.risk.note_fill(fill, count_toward_daily=count_toward_daily)
                if not count_toward_daily:
                    # apply_fill also moved inventory; keep daily_pnl neutral.
                    self.risk.absorb_equity_delta_into_baseline(equity_before)
                applied += 1
                cid = self._token_cid.get(fill.token_id)
                if cid:
                    self._wake_cid(cid)

        missing = self.state.matched_orders_missing_fills()
        # Only treat traded tokens we care about as coverage gaps.
        missing_ours = [m for m in missing if str(m["token_id"]) in self._token_cid]
        coverage_complete = len(missing_ours) == 0
        self._trades_backfill_ok = coverage_complete
        if coverage_complete:
            self._last_trades_ok_ts = time.time()
            log.info(
                "trades_backfill_ok",
                reason=reason,
                fetched=len(rows),
                applied=applied,
                after_ts=after,
            )
        else:
            log.warning(
                "trades_backfill_incomplete",
                reason=reason,
                fetched=len(rows),
                applied=applied,
                after_ts=after,
                missing_n=len(missing_ours),
                missing_oid_prefix=str(missing_ours[0]["order_id"])[:12],
            )
        return coverage_complete

    def _other_token_for_fill(self, token_id: str) -> str | None:
        cid = self._token_cid.get(token_id)
        if cid is None:
            return None
        meta = self.metas.get(cid)
        if meta is None:
            return None
        return meta.other_token(token_id)

    async def _authoritative_resync(self, *, reason: str) -> bool:
        """Trades + positions + open-orders must all succeed before trust returns.

        Empty successful reads are fine. Any failure => unknown, stay protective.
        May clear a false-positive daily_loss latch only when accounting after
        conservation no longer breaches the kill; never clears on transport miss.
        """
        trades_ok = await self._backfill_trades(reason=reason)
        positions_ok = False
        orders_ok = False
        positions: dict[str, tuple[float, float]] | None = None
        try:
            positions = self._only_traded(await self.gateway.positions())
            positions_ok = True
            self._positions_fail_streak = 0
            self._positions_backoff_until = 0.0
            self._positions_healthy = True
            self._last_reconcile_ok_ts = time.time()
        except PositionsReadError as exc:
            self._positions_fail_streak += 1
            self._positions_healthy = False
            delay = self._positions_backoff_delay_s(exc)
            self._positions_backoff_until = time.monotonic() + delay
            log.warning(
                "authoritative_positions_failed", reason=reason, err=str(exc)[:160]
            )

        live: list[Any] | None = None
        try:
            live = await self.gateway.open_orders()
            orders_ok = True
        except OpenOrdersReadError as exc:
            log.warning(
                "authoritative_orders_failed", reason=reason, err=str(exc)[:160]
            )

        if positions is not None:
            deltas = self.state.reconcile_positions(positions, is_snapshot=True)
            self._apply_inventory_conservation(deltas)
        if live is not None:
            by_token: dict[str, list[Any]] = {}
            for o in live:
                by_token.setdefault(o.token_id, []).append(o)
            for meta in self.metas.values():
                for tok in (meta.yes.token_id, meta.no.token_id):
                    if self.state.inflight(tok) == 0:
                        self.state.replace_open_orders(
                            tok, by_token.get(tok, []), grace_s=0.0
                        )

        authoritative_ok = trades_ok and positions_ok and orders_ok
        if authoritative_ok:
            self._recovery_blocked = False
            cleared, why = self.risk.try_clear_false_daily_loss(authoritative_ok=True)
            log.info(
                "authoritative_resync_ok",
                reason=reason,
                daily_loss_cleared=cleared,
                clear_why=why,
                daily_pnl=round(self.risk.daily_pnl, 6),
            )
        else:
            self._recovery_blocked = True
            self.risk.try_clear_false_daily_loss(authoritative_ok=False)
            log.warning(
                "authoritative_resync_incomplete",
                reason=reason,
                trades_ok=trades_ok,
                positions_ok=positions_ok,
                orders_ok=orders_ok,
            )
        return authoritative_ok

    # ── callbacks ───────────────────────────────────────────────────────
    def _on_dirty(self, condition_id: str, token_id: str) -> None:
        ev = self._dirty.get(condition_id)
        if ev is not None:
            ev.set()

    def _wake_cid(self, condition_id: str) -> None:
        ev = self._dirty.get(condition_id)
        if ev is not None:
            ev.set()

    def _wake_all(self) -> None:
        for ev in self._dirty.values():
            ev.set()

    def _on_user_reconnect(self) -> None:
        """User WS reconnected: events during the gap were lost — force an
        immediate authoritative resync before trusting our state again."""
        log.warning("user_ws_reconnected_forcing_reconcile")
        self._recovery_blocked = True
        self._reconcile_now.set()
        self._aux_tasks.append(
            asyncio.create_task(self._authoritative_resync(reason="user_reconnect"))
        )

    def _on_trade(self, tp: TradePrint) -> None:
        cid = self._token_cid.get(tp.asset_id)
        if cid is None:
            return
        p = self.profiles[cid]
        self.est[cid].flow.update(tp.aggressor, tp.size, tp.ts)
        # A trade only flags a SWEEP (-> pull quotes) if it's genuinely toxic:
        # large in absolute terms AND large relative to the resting depth it
        # consumed (i.e. it actually ate through the book). A big trade absorbed
        # by a deep book doesn't move the price and isn't toxic — for a liquid
        # market the FV-jump detector is the real event signal. event_sweep_mult
        # sets how many order-sizes big the print must be to even be considered.
        base = p.base_size_usdc / max(tp.price, 0.01)
        if tp.size < p.event_sweep_mult * base:
            return
        book = self.md.book(tp.asset_id)
        if book is None:
            return
        bb, ba = book.best_bid(), book.best_ask()
        if bb is None or ba is None:
            return
        # aggressor BUY lifts asks; SELL hits bids — measure the side it consumed
        if tp.aggressor is Side.BUY:
            consumed = book.depth_within(Side.SELL, ba.price, ba.price + 3 * book.tick_size)
        else:
            consumed = book.depth_within(Side.BUY, bb.price - 3 * book.tick_size, bb.price)
        if consumed > 0 and tp.size >= p.event_sweep_frac * consumed:
            self._sweep[cid] = True

    def _on_fill(self, fill: Fill) -> None:
        self.risk.note_fill(fill)
        schedule_notify(
            self.telegram.notify_fill(
                side=fill.side.value,
                price=fill.price,
                size=fill.size,
                token_hint=fill.token_id[:12],
                trade_id=fill.trade_id,
            )
        )
        cid = self._token_cid.get(fill.token_id)
        if cid is None:
            return
        est = self.est[cid]
        fv = est.last_fv if est.last_fv is not None else fill.price
        token_fv = fv if fill.token_id == self.metas[cid].yes.token_id else (1.0 - fv)
        est.markout.record_fill(fill.side, token_fv, fill.ts)
        # track inventory open time for exit urgency (BUY opens / adds; flat clears)
        pos = self.state.position(fill.token_id)
        if pos.size >= self.metas[cid].min_order_size:
            self._inv_open_ts.setdefault(fill.token_id, fill.ts)
        else:
            self._inv_open_ts.pop(fill.token_id, None)

    def _exit_urgency_for(
        self, token_id: str, pos_avg: float, token_fv: float, profile: StrategyProfile, now: float
    ) -> float:
        open_ts = self._inv_open_ts.get(token_id)
        if open_ts is None:
            return 0.0
        held_s = max(0.0, now - open_ts)
        # long inventory is adverse when mark < avg entry
        adverse = max(0.0, pos_avg - token_fv) if pos_avg > 0 else 0.0
        return compute_exit_urgency(
            held_s=held_s, adverse=adverse, exit_urgency_s=profile.exit_urgency_s
        )

    # ── quoter ──────────────────────────────────────────────────────────
    async def _quoter(self, cid: str) -> None:
        debounce = self.cfg.engine.debounce_ms / 1000.0
        base_tick = self.cfg.engine.quoter_tick_s
        ev = self._dirty[cid]
        while self._running:
            try:
                # Book/fill events wake us instantly. Otherwise we refresh on a
                # slow baseline tick, EXCEPT: if an EVENT cool-off is active,
                # wake precisely when it ends (re-enter promptly, not up to a
                # minute late); if we're holding inventory, tick faster to walk
                # exit urgency.
                timeout = self._next_wake_s(cid, base_tick)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ev.wait(), timeout=timeout)
                if ev.is_set():
                    await asyncio.sleep(debounce)  # coalesce a burst of updates
                ev.clear()
                await self._recompute(cid)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                log.error("quoter_error", cid=cid[:8], err=str(exc))
                await asyncio.sleep(0.5)

    def _next_wake_s(self, cid: str, base_tick: float) -> float:
        now = time.time()
        wake = base_tick
        rm = self.regime_m.get(cid)
        if rm is not None:
            cd = rm.cooloff_remaining(now)
            if cd > 0:
                wake = min(wake, cd + 0.5)  # re-enter right when cool-off ends
        meta = self.metas.get(cid)
        if meta is not None:  # holding inventory -> tick faster to manage exits
            held = self.state.position(meta.yes.token_id).size + self.state.position(meta.no.token_id).size
            if held >= meta.min_order_size:
                wake = min(wake, 10.0)
        return max(1.0, wake)

    async def _recompute(self, cid: str) -> None:
        lock = self._locks.get(cid)
        if lock is None:
            return
        async with lock:  # serialize vs the reconcile loop mutating this market
            await self._recompute_locked(cid)

    async def _recompute_locked(self, cid: str) -> None:
        meta = self.metas[cid]
        p = self.profiles[cid]
        yes_book = self.md.book(meta.yes.token_id)
        no_book = self.md.book(meta.no.token_id)
        books_usable = (
            yes_book is not None
            and not yes_book.is_empty
            and self.md.is_fresh(meta.yes.token_id)
            and self.md.is_fresh(meta.no.token_id)
        )
        if not books_usable:
            # Don't price new quotes off missing/cleared books. If the link is
            # down or we are waiting for a post-reconnect snapshot, pull any
            # existing quotes so we don't leave stale orders working.
            link_down = (not self.md.connected and self.md.disconnected_since > 0.0)
            waiting = (
                self.md.needs_snapshot(meta.yes.token_id)
                or self.md.needs_snapshot(meta.no.token_id)
            )
            if link_down or waiting:
                live = (
                    self.state.orders_for(meta.yes.token_id)
                    + self.state.orders_for(meta.no.token_id)
                )
                if live:
                    tq = TargetQuotes(cid, Regime.HALTED, ())
                    plan = reconcile(
                        tq, live, tick=meta.tick_size,
                        reprice_ticks=p.reprice_ticks, resize_frac=p.resize_frac,
                    )
                    if plan.to_cancel:
                        ok = await self.gateway.cancel(plan.to_cancel)
                        if ok:
                            for oid in plan.to_cancel:
                                self.state.remove_order(oid)
            return

        # crossed/locked or one-sided book -> FV is unreliable; skip this tick
        bb, ba = yes_book.best_bid(), yes_book.best_ask()
        if bb is None or ba is None or bb.price >= ba.price:
            return

        now = time.time()
        micro = yes_book.microprice(p.micro_levels)
        if micro is None:
            return
        est = self.est[cid]
        est.flow.decay_to(now)
        fv = compute_fair_value(micro, est.flow.z, meta.tick_size)
        prev_fv = est.last_fv
        est.on_fair_value(fv, now)

        self.risk.update_mark(meta.yes.token_id, fv)
        self.risk.update_mark(meta.no.token_id, 1.0 - fv)

        pos_yes = self.state.position(meta.yes.token_id)
        pos_no = self.state.position(meta.no.token_id)
        q_max = p.q_max_usdc
        inv_util = abs(pos_yes.size - pos_no.size) * fv / q_max if q_max > 0 else 0.0
        hours_to_end = _hours_to_end(meta.end_date_iso, now)

        # ── blind/stale conditions ──────────────────────────────────────────
        # A QUIET market with a live WS link is NOT stale — the CLOB WS pings
        # every 5s (pong-timeout 10s), so a dead link flips `connected` within
        # ~15s. Gating on the connection (not book-mutation recency) stops a
        # legitimately-quiet thin market from false-halting into zero rewards.
        market_stale = (
            not self.md.connected
            and self.md.disconnected_since > 0.0
            and (now - self.md.disconnected_since) > self.cfg.risk.ws_stale_halt_s
        )
        user_blind = (
            self._user_started
            and self.user is not None
            and not self.user.connected
            and (now - self.user.disconnected_since) > self.cfg.risk.user_ws_blind_halt_s
        )
        hb_blind = (
            self.cfg.engine.heartbeat
            and self.gateway.heartbeat_failures >= self.cfg.risk.heartbeat_halt_failures
        )
        # Do not place new quotes while positions/trades authority is unknown or
        # a disconnect recovery is still incomplete (prevents duplicate orders /
        # duplicate fill accounting).
        recovery_blind = (
            self._recovery_blocked
            or not self._positions_healthy
            or (self._user_started and not self._trades_backfill_ok and self.user is not None)
        )
        halted = cid in self._halted
        blind = market_stale or user_blind or hb_blind or halted or recovery_blind
        if blind:
            log.warning(
                "market_blind",
                cid=cid[:8],
                market_stale=market_stale,
                user_blind=user_blind,
                hb_blind=hb_blind,
                recovery_blind=recovery_blind,
                halted=halted,
            )
            self.alerter.alert(
                f"blind:{cid[:8]}",
                f"{meta.question[:40]} blind (stale={market_stale} user={user_blind} "
                f"hb={hb_blind} recovery={recovery_blind} halted={halted})",
                critical=hb_blind or recovery_blind,
            )

        rd = self.risk.evaluate(meta, ws_stale=blind,
                                event_group_cost=self._event_group_cost(meta))
        if rd.halt and rd.reason not in ("ws_stale",):
            self.alerter.alert(
                f"risk_halt:{rd.reason}", f"risk halt: {rd.reason}",
                critical=any(k in rd.reason for k in ("daily_loss", "kill", "error_rate")),
            )
        ws_stale = blind
        regime = self.regime_m[cid].decide(
            RegimeInputs(
                now=now, tick=meta.tick_size, fv=fv, prev_fv=prev_fv,
                vol_ratio=est.vol.ratio, flow_z=est.flow.z, inventory_util=inv_util,
                hours_to_end=hours_to_end, sweep_flagged=self._sweep.pop(cid, False),
                ws_stale=ws_stale, risk_halt=rd.halt, risk_reduce_only=rd.reduce_only,
            ),
            p,
        )

        # keep inv-open clocks in sync even when fills arrived via REST reconcile
        for tok, pos in ((meta.yes.token_id, pos_yes), (meta.no.token_id, pos_no)):
            if pos.size >= meta.min_order_size:
                self._inv_open_ts.setdefault(tok, now)
            else:
                self._inv_open_ts.pop(tok, None)

        yes_u = self._exit_urgency_for(meta.yes.token_id, pos_yes.avg_price, fv, p, now)
        no_u = self._exit_urgency_for(
            meta.no.token_id, pos_no.avg_price, 1.0 - fv, p, now
        )
        # soft advisory: worst-case filled+resting vs cap (does not replace hard rd)
        adv_scale = self.risk.advisory_size_scale(meta)
        size_scale = min(rd.size_scale, adv_scale) if adv_scale < 1.0 else rd.size_scale

        headroom = self._current_headroom(meta)
        expensive = max(fv, 1.0 - fv)
        self._entry_blocked_infeasible[cid] = bool(
            headroom is not None
            and meta.min_order_size > 0
            and expensive * meta.min_order_size > headroom + 1e-9
        )
        tq = construct_quotes(QuoteInputs(
            meta=meta, regime=regime, fv=fv, vol_short=est.vol.short,
            toxicity=est.markout.toxicity, yes_view=yes_book.view(),
            no_view=(no_book.view() if no_book else _empty_view()),
            pos_yes=pos_yes, pos_no=pos_no, profile=p, now=now,
            risk_size_scale=size_scale,
            yes_exit_urgency=yes_u, no_exit_urgency=no_u,
            headroom_usdc=headroom,
        ))

        live = self.state.orders_for(meta.yes.token_id) + self.state.orders_for(meta.no.token_id)
        plan = reconcile(tq, live, tick=meta.tick_size,
                         reprice_ticks=p.reprice_ticks, resize_frac=p.resize_frac)
        if plan.is_noop:
            self._maybe_merge(cid, meta, p, pos_yes.size, pos_no.size)
            return

        if plan.to_cancel:
            ok = await self.gateway.cancel(plan.to_cancel)
            if ok:
                for oid in plan.to_cancel:
                    self.state.remove_order(oid)
            else:
                # cancel MAY have partially applied server-side — keep our view,
                # resync from REST, and skip placing this cycle (avoid doubles)
                await self._refresh_token_orders(meta, grace_s=10.0)
                self._dirty[cid].set()
                return
        placed_n = 0
        if plan.to_place:
            if self.telegram.stop_requested:
                log.warning("telegram_stop_skip_place", cid=cid[:8])
                return
            # LOAD SHED: under rate-budget pressure, skip *new* quotes in calm
            # regimes (cancels/exits above already ran) so we don't inject latency
            # right when the book is busy. Risk regimes always place.
            shed = (
                self.gateway.order_pressure > 0.85
                and regime in (Regime.QUIET, Regime.TRENDING)
            )
            if shed:
                log.warning("shed_load", cid=cid[:8], pressure=round(self.gateway.order_pressure, 2))
                self._dirty[cid].set()  # retry soon
            else:
                to_place = await self._budget_filter_quotes(cid, meta, plan.to_place)
                if not to_place:
                    # Affordable set empty after budget gate — not an exchange
                    # reject loop; skip place without counting as order error.
                    log.info(
                        "budget_skip_empty",
                        cid=cid[:8],
                        wanted=len(plan.to_place),
                        collateral=None if self._collateral_usdc is None
                        else round(self._collateral_usdc, 6),
                    )
                else:
                    place_notional = sum(
                        q.price * q.size for q in to_place if q.side is Side.BUY
                    )
                    need, reason = self.telegram.needs_confirm(place_notional)
                    if need:
                        ok = await self.telegram.request_confirm(
                            reason,
                            detail=f"{meta.question[:60]} | {len(to_place)} quotes",
                        )
                        if not ok:
                            log.warning(
                                "telegram_confirm_denied", cid=cid[:8], reason=reason
                            )
                            return
                    try:
                        placed = await self._place_with_intent(cid, meta, to_place)
                    except Exception as exc:  # noqa: BLE001
                        log.error("place_intent_failed", cid=cid[:8], err=str(exc)[:200])
                        await self._quarantine(meta, reason="place_exception")
                        placed = []
                    placed_n = len(placed)
                    self.risk.note_order_result(len(placed) == len(to_place))
                    for o in placed:
                        self.state.upsert_order(o)
                    if len(placed) < len(to_place):
                        # QUARANTINE: a failed/partial batch may still have posted
                        # orders we don't have ids for. Cancel everything on these
                        # tokens (idempotent) and resync — never risk an untracked order.
                        await self._quarantine(meta, reason="place_incomplete")
                        schedule_notify(
                            self.telegram.notify_place_failed(
                                n=len(to_place) - placed_n,
                                detail="partial batch — quarantine",
                            )
                        )
                    elif placed_n > 0:
                        self.telegram.note_placed(place_notional)
                        # optimistic local reserve until next collateral refresh
                        spent = sum(
                            o.price * o.size for o in placed if o.side is Side.BUY
                        )
                        if self._collateral_usdc is not None and spent > 0:
                            self._collateral_usdc = max(
                                0.0, self._collateral_usdc - spent
                            )
                    else:
                        schedule_notify(
                            self.telegram.notify_place_failed(n=len(to_place))
                        )
        self._last_quote_fv[cid] = fv
        log.info("requote", cid=cid[:8], regime=regime.value, fv=round(fv, 4),
                 place=placed_n, cancel=len(plan.to_cancel),
                 pos_yes=round(pos_yes.size, 1), pos_no=round(pos_no.size, 1),
                 tox=round(est.markout.toxicity, 3), flowz=round(est.flow.z, 2),
                 yes_exit_u=round(yes_u, 2), no_exit_u=round(no_u, 2))
        self._maybe_merge(cid, meta, p, pos_yes.size, pos_no.size)

    async def _refresh_collateral(self, *, force: bool = False) -> float | None:
        """Refresh CLOB collateral snapshot (no secrets logged)."""
        now = time.time()
        if (
            not force
            and self._collateral_usdc is not None
            and (now - self._collateral_ts) < _COLLATERAL_REFRESH_S
        ):
            return self._collateral_usdc
        try:
            bal = await self.gateway.collateral_balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("collateral_refresh_failed", err=str(exc)[:120])
            return self._collateral_usdc
        self._collateral_usdc = bal
        self._collateral_ts = now
        log.info("collateral_refreshed", available=round(bal, 6))
        return bal

    async def _budget_filter_quotes(
        self, cid: str, meta: MarketMeta, quotes: list[Quote]
    ) -> list[Quote]:
        """Drop BUY quotes that exceed resting+filled+fee headroom (no undersize split)."""
        await self._refresh_collateral(force=False)
        available = self._collateral_usdc
        if available is None:
            log.warning("budget_unknown_collateral", cid=cid[:8], action="skip_place")
            return []

        resting = self.risk.resting_buy_notional(meta)
        filled_mkt = self.risk.filled_market_notional(meta)
        filled_total = self.risk.filled_total_exposure()

        headroom = place_headroom_usdc(
            available_usdc=available,
            resting_buy_usdc=resting,
            filled_market_usdc=filled_mkt,
            filled_total_usdc=filled_total,
            max_market_notional_usdc=self.cfg.risk.max_market_notional_usdc,
            max_total_exposure_usdc=self.cfg.risk.max_total_exposure_usdc,
            fee_buffer_usdc=_FEE_BUFFER_USDC,
        )
        decision = filter_quotes_to_budget(quotes, headroom_usdc=headroom)
        skipped = tuple(decision.skipped)
        if any("reason=infeasible" in msg for msg in skipped):
            self._entry_blocked_infeasible[cid] = True
        if skipped and skipped != self._last_budget_skip.get(cid):
            for msg in skipped:
                log.warning("budget_reject", cid=cid[:8], detail=msg)
            log.info(
                "budget_filter",
                cid=cid[:8],
                available=round(available, 6),
                resting=round(resting, 6),
                headroom=round(headroom, 6),
                kept=len(decision.kept),
                skipped=len(decision.skipped),
            )
            self._last_budget_skip[cid] = skipped
        elif not skipped:
            self._last_budget_skip.pop(cid, None)
        return list(decision.kept)

    def _current_headroom(self, meta: MarketMeta) -> float | None:
        """Best-effort place headroom from the last collateral snapshot (no I/O)."""
        available = self._collateral_usdc
        if available is None:
            return None
        return place_headroom_usdc(
            available_usdc=available,
            resting_buy_usdc=self.risk.resting_buy_notional(meta),
            filled_market_usdc=self.risk.filled_market_notional(meta),
            filled_total_usdc=self.risk.filled_total_exposure(),
            max_market_notional_usdc=self.cfg.risk.max_market_notional_usdc,
            max_total_exposure_usdc=self.cfg.risk.max_total_exposure_usdc,
            fee_buffer_usdc=_FEE_BUFFER_USDC,
        )

    async def _place_with_intent(
        self, cid: str, meta: MarketMeta, quotes: list[Quote]
    ) -> list[OpenOrder]:
        """Place in max_orders_per_batch chunks; ledger unknown/partial → quarantine."""
        key = client_key_for(
            cid, [(q.token_id, q.side.value, q.price, q.size) for q in quotes]
        )
        self._intents.record(
            PlacementIntent(client_key=key, condition_id=cid, n_quotes=len(quotes))
        )
        placed_all: list[OpenOrder] = []
        batches = chunk_quotes(list(quotes), self.cfg.execution.max_orders_per_batch)
        try:
            for batch in batches:
                batch_placed = await self.gateway.place(batch, meta)
                placed_all.extend(batch_placed)
                if len(batch_placed) < len(batch):
                    self._intents.ack(key, [o.order_id for o in placed_all])
                    self._intents.mark_unknown(key)
                    break
            else:
                self._intents.ack(key, [o.order_id for o in placed_all])
        except Exception:  # noqa: BLE001 — quarantine path; gateway usually swallows
            self._intents.mark_unknown(key)
            raise
        if self._intents.should_quarantine(key):
            self._intents.mark_quarantined(key)
        else:
            self._intents.clear(key)
        return placed_all

    async def _quarantine(self, meta: MarketMeta, reason: str) -> None:
        """Cancel all orders on a market's tokens and resync state from REST."""
        log.warning("quarantine", cid=meta.condition_id[:8], reason=reason)
        self._intents.clear_condition(meta.condition_id)
        for tok in (meta.yes.token_id, meta.no.token_id):
            await self.gateway.cancel_asset(tok)
            for o in self.state.orders_for(tok):
                self.state.remove_order(o.order_id)
        await self._refresh_token_orders(meta)

    async def _refresh_token_orders(self, meta: MarketMeta, grace_s: float = 0.0) -> None:
        """Open-orders resync for one market's tokens (grace_s=0 = authoritative)."""
        try:
            live = await self.gateway.open_orders()
        except OpenOrdersReadError as exc:
            log.warning(
                "refresh_open_orders_failed",
                cid=meta.condition_id[:8],
                err=str(exc)[:160],
            )
            return
        for tok in (meta.yes.token_id, meta.no.token_id):
            self.state.replace_open_orders(
                tok, [o for o in live if o.token_id == tok], grace_s=grace_s
            )

    def _maybe_merge(self, cid: str, meta: MarketMeta, p: StrategyProfile,
                     yes_size: float, no_size: float) -> None:
        amount = min(yes_size, no_size)
        if amount < p.merge_min_size or cid in self._merging:
            return
        self._merging.add(cid)
        self._aux_tasks.append(asyncio.create_task(self._merge_task(cid, meta, amount)))

    async def _merge_task(self, cid: str, meta: MarketMeta, amount: float) -> None:
        try:
            # serialize all on-chain txs so concurrent merges can't reuse a nonce;
            # read on-chain balances as source of truth for the mergeable amount
            async with self._chain_lock:
                bals = await self.gateway.token_balances([meta.yes.token_id, meta.no.token_id])
                if bals:
                    amount = min(amount, bals.get(meta.yes.token_id, 0.0),
                                 bals.get(meta.no.token_id, 0.0))
                raw = int(amount * 1e6)
                if raw <= 0:
                    return
                await asyncio.to_thread(self.merger.merge, meta.condition_id, raw, meta.neg_risk)
        finally:
            self._merging.discard(cid)

    # ── background loops ────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        if not self.cfg.engine.heartbeat:
            return
        halt_after = self.cfg.risk.heartbeat_halt_failures
        while self._running:
            ok = await self.gateway.heartbeat()
            if ok:
                self._last_heartbeat_ok_ts = time.time()
            if not ok and self.gateway.heartbeat_failures >= halt_after and not self._hb_was_down:
                # exchange is (or soon will be) auto-cancelling everything we
                # have live; recompute will see hb_blind and pull quotes
                self._hb_was_down = True
                self._recovery_blocked = True
                log.critical("heartbeat_down_halting", failures=self.gateway.heartbeat_failures)
                self._wake_all()
            elif ok and self._hb_was_down:
                # recovered: server-side orders were wiped — clear local orders
                # and require authoritative trades/positions/orders before quotes.
                self._hb_was_down = False
                log.warning("heartbeat_recovered_resyncing")
                self.state.clear_orders()
                self._recovery_blocked = True
                auth_ok = await self._authoritative_resync(reason="heartbeat_recover")
                if auth_ok:
                    self._wake_all()
                else:
                    log.warning("heartbeat_recover_still_blind")
                    self._wake_all()  # pull quotes via recovery_blind
            delay = self.cfg.engine.heartbeat_interval_s
            delay = max(delay, self.gateway.heartbeat_backoff_s)
            await asyncio.sleep(delay)

    def _positions_backoff_delay_s(self, exc: PositionsReadError) -> float:
        """Retry-After when present; else exponential cap. No busy-loop."""
        if exc.retry_after_s is not None and exc.retry_after_s > 0:
            return min(float(exc.retry_after_s), 120.0)
        streak = max(1, self._positions_fail_streak)
        return min(2.0 ** streak, 60.0)

    def _record_health_snapshot(self, *, reconcile: str) -> None:
        """Persist one redacted local health row (acceptance evidence)."""
        hb = "unknown"
        if self.cfg.engine.heartbeat and self._last_heartbeat_ok_ts <= 0:
            hb = "pending"
        elif self._last_heartbeat_ok_ts > 0:
            age = time.time() - self._last_heartbeat_ok_ts
            # Align column with real freshness (avoid "ok" with multi-minute age).
            stale_after = max(30.0, self.cfg.engine.heartbeat_interval_s * 6)
            if self.gateway.heartbeat_failures >= self.cfg.risk.heartbeat_halt_failures:
                hb = "down"
            elif age > stale_after:
                hb = "stale"
            else:
                hb = "ok"
        market = "unknown"
        with contextlib.suppress(Exception):
            market = self.md.health_market_state()
        user_state = "unknown"
        if self.user is None:
            user_state = "disabled"
        elif self.user.connected:
            user_state = "connected"
        elif self.user.disconnected_since > 0:
            user_state = "disconnected"
        tg = "disabled"
        if self.telegram.enabled:
            if self.telegram.last_poll_ok_ts > 0:
                tg = "ok"
            elif self.telegram.poll_error_count > 0:
                tg = "error"
            else:
                tg = "pending"
        detail = {
            "hb_age_s": (
                round(time.time() - self._last_heartbeat_ok_ts, 3)
                if self._last_heartbeat_ok_ts > 0
                else None
            ),
            "market_msgs": int(getattr(self.md, "message_count", 0)),
            "market_snaps": int(getattr(self.md, "snapshot_count", 0)),
            "user_msgs": int(getattr(self.user, "message_count", 0) or 0) if self.user else 0,
            "tg_polls": int(self.telegram.poll_ok_count),
            "pos_streak": int(self._positions_fail_streak),
            "pos_healthy": bool(self._positions_healthy),
            "orders_n": len(self.state.orders),
            "pos_n": sum(1 for p in self.state.positions.values() if p.size > 0),
            "trades_ok": bool(self._trades_backfill_ok),
            "recovery_blocked": bool(self._recovery_blocked),
            "daily_loss_latched": bool(self.risk.daily_loss_latched),
            "conserved_usdc": round(self.risk.conserved_usdc, 6),
        }
        self.state.record_health(
            heartbeat=hb,
            market=market,
            user_stream=user_state,
            telegram_poll=tg,
            reconcile=reconcile,
            detail=detail,
        )

    async def _reconcile_loop(self) -> None:
        rounds = 0
        while self._running:
            # periodic cadence, but wake immediately when a reconnect/recovery
            # demands an urgent resync
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._reconcile_now.wait(),
                    timeout=self.cfg.engine.reconcile_interval_s,
                )
            forced = self._reconcile_now.is_set()
            self._reconcile_now.clear()
            rounds += 1
            reconcile_status = "ok"
            try:
                # a MATCHED whose settlement event was lost would block a token's
                # reconciliation forever — expire stale in-flight guards first
                expired = self.state.expire_inflight(self.cfg.engine.reconcile_interval_s * 2)
                if expired:
                    self.alerter.alert("inflight_expired",
                                       f"{len(expired)} stuck in-flight guards cleared")

                positions: dict[str, tuple[float, float]] | None
                now_mono = time.monotonic()
                if now_mono < self._positions_backoff_until:
                    # Still cooling down after 429/fail — keep local inventory,
                    # do not hammer Data API.
                    remaining = self._positions_backoff_until - now_mono
                    log.info(
                        "positions_backoff_skip",
                        remaining_s=round(remaining, 2),
                        streak=self._positions_fail_streak,
                    )
                    positions = None
                    reconcile_status = "backoff"
                else:
                    try:
                        positions = self._only_traded(await self.gateway.positions())
                    except PositionsReadError as exc:
                        # Keep prior local inventory; never interpret failed read as flat.
                        self._positions_fail_streak += 1
                        self._positions_healthy = False
                        delay = self._positions_backoff_delay_s(exc)
                        self._positions_backoff_until = time.monotonic() + delay
                        self._last_reconcile_fail_ts = time.time()
                        log.warning(
                            "reconcile_positions_failed",
                            err=str(exc),
                            status=exc.status_code,
                            retry_after_s=exc.retry_after_s,
                            backoff_s=round(delay, 2),
                            streak=self._positions_fail_streak,
                        )
                        positions = None
                        reconcile_status = "fail"
                    else:
                        was_unhealthy = not self._positions_healthy
                        self._positions_fail_streak = 0
                        self._positions_backoff_until = 0.0
                        self._positions_healthy = True
                        self._last_reconcile_ok_ts = time.time()
                        if was_unhealthy:
                            log.info("positions_reconcile_recovered")
                            # Resume normal cadence; wake quoters so exits refresh.
                            self._wake_all()
                if positions is not None:
                    # Empty dict is a valid complete snapshot and must zero stale locals.
                    deltas = self.state.reconcile_positions(positions, is_snapshot=True)
                    self._apply_inventory_conservation(deltas)

                live: list[Any] | None
                try:
                    live = await self.gateway.open_orders()
                except OpenOrdersReadError as exc:
                    # Fail-open on order book: keep local resting view; exit status
                    # path treats this as unknown rather than "confirmed none".
                    log.warning("reconcile_open_orders_failed", err=str(exc)[:160])
                    live = None
                    if reconcile_status == "ok":
                        reconcile_status = "orders_fail"
                if live is not None:
                    by_token: dict[str, list[Any]] = {}
                    for o in live:
                        by_token.setdefault(o.token_id, []).append(o)
                    # iterate ALL our tokens, not just those in the REST response — a
                    # token whose orders vanished server-side must be cleaned up too.
                    # Hold the market lock so we don't race the quoter mid-flight.
                    for cid, meta in self.metas.items():
                        lock = self._locks.get(cid)
                        if lock is None:
                            continue
                        async with lock:
                            for tok in (meta.yes.token_id, meta.no.token_id):
                                if self.state.inflight(tok) == 0:
                                    self.state.replace_open_orders(tok, by_token.get(tok, []))
                if forced:
                    # Forced path (reconnect): also refresh trades when possible.
                    trades_ok = await self._backfill_trades(reason="forced_reconcile")
                    authoritative_ok = (
                        trades_ok and positions is not None and live is not None
                    )
                    if authoritative_ok:
                        self._recovery_blocked = False
                        self.risk.try_clear_false_daily_loss(authoritative_ok=True)
                    else:
                        self._recovery_blocked = True
                    log.info(
                        "forced_reconcile_done",
                        positions=("failed" if positions is None else len(positions)),
                        open_orders=("failed" if live is None else len(live)),
                        trades_ok=trades_ok,
                        recovery_blocked=self._recovery_blocked,
                    )
                    self._wake_all()
                self.risk.roll_day_if_needed()
            except Exception as exc:  # noqa: BLE001
                reconcile_status = "error"
                log.warning("reconcile_error", err=str(exc))
            # slower loops: on-chain position divergence + pnl snapshot + WAL
            if rounds % 4 == 0:
                with contextlib.suppress(Exception):
                    await self._check_position_divergence()
            self.state.record_pnl(self.risk.equity, self.risk.net_cash,
                                  self.risk.inventory_value, self.risk.daily_pnl)
            with contextlib.suppress(Exception):
                self._record_health_snapshot(reconcile=reconcile_status)
            if rounds % 20 == 0:
                self.state.checkpoint_wal()

    async def _check_position_divergence(self) -> None:
        """Compare internal positions to on-chain truth; alert + correct on drift.

        Catches subtle fill-attribution bugs before they compound. On-chain is
        authoritative (it's what the exchange settles), so we correct to it —
        but only for tokens with no in-flight trades (optimistic state is newer).
        """
        tokens = [t for t in self._token_cid if self.state.inflight(t) == 0]
        onchain = await self.gateway.token_balances(tokens)
        if not onchain:
            return
        for tok, chain_size in onchain.items():
            internal = self.state.position(tok).size
            if abs(internal - chain_size) > max(1.0, 0.02 * chain_size):
                log.error("position_divergence", token=tok[:12],
                          internal=round(internal, 2), onchain=round(chain_size, 2))
                self.alerter.alert(
                    f"divergence:{tok[:8]}",
                    f"position drift: internal {internal:.1f} vs on-chain {chain_size:.1f}",
                    critical=True,
                )
                prev = self.state.position(tok)
                old_size = prev.size
                old_avg = prev.avg_price
                self.state.force_set_position(tok, chain_size, old_avg, source="onchain")
                unit = self.risk.unit_value(tok, fallback=old_avg)
                self._apply_inventory_conservation(
                    [(tok, old_size, chain_size, unit)]
                )
                cid = self._token_cid.get(tok)
                if cid:
                    self._wake_cid(cid)

    async def refresh_market_metadata(self) -> None:
        """Pull fresh metadata from Gamma for all traded markets: halt on
        closed/not-accepting, and freshen reward/fee/end-date params so we quote
        at the CURRENT reward minimum, band, and fees (these change over time —
        e.g. the reward min-size jumping 50->100 shares). Called at startup and
        periodically. Safe to await."""
        if not self.metas:
            return
        try:
            async with GammaClient(self.cfg.wallet.gamma_host) as gamma:
                raws = await gamma.markets_by_condition(list(self.metas))
        except Exception as exc:  # noqa: BLE001
            log.warning("metadata_refresh_error", err=str(exc))
            return
        for cid, raw in raws.items():
            if cid not in self.metas:
                continue
            accepting = bool(raw.get("acceptingOrders", True))
            closed = bool(raw.get("closed", False))
            if closed or not accepting:
                if cid not in self._halted:
                    self._halted.add(cid)
                    log.critical("market_halted_by_meta", cid=cid[:8], closed=closed,
                                 accepting=accepting)
                    self.alerter.alert(f"halted:{cid[:8]}",
                                       f"{self.metas[cid].question[:40]} closed/not-accepting",
                                       critical=True)
                    meta = self.metas[cid]
                    for tok in (meta.yes.token_id, meta.no.token_id):
                        with contextlib.suppress(Exception):
                            await self.gateway.cancel_asset(tok)
                    self._wake_cid(cid)
                continue
            self._halted.discard(cid)
            self._apply_meta_refresh(cid, raw)

    def _apply_meta_refresh(self, cid: str, raw: dict[str, Any]) -> None:
        import dataclasses

        old = self.metas[cid]
        fee = raw.get("feeSchedule") or {}
        rate = _fnum(fee.get("rate"))
        candidates: dict[str, Any] = {
            "rewards_min_size": _fnum(raw.get("rewardsMinSize")),
            "rewards_max_spread": _fnum(raw.get("rewardsMaxSpread")),
            "taker_fee_bps": int(round(rate * 10000)) if rate is not None else None,
            "rebate_rate": _fnum(fee.get("rebateRate")),
            "end_date_iso": raw.get("endDate"),
            "min_order_size": _fnum(raw.get("orderMinSize")),
        }
        updates = {k: v for k, v in candidates.items()
                   if v is not None and getattr(old, k) != v}
        if updates:
            self.metas[cid] = dataclasses.replace(old, **updates)
            log.info("meta_refreshed", cid=cid[:8], **updates)
            self._wake_cid(cid)

    async def _metadata_refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.cfg.engine.catalog_refresh_s)
            await self.refresh_market_metadata()

    async def _maintenance_loop(self) -> None:
        """Periodic REST book refresh to catch any silently-missed WS deltas."""
        while self._running:
            await asyncio.sleep(120.0)
            for meta in list(self.metas.values()):
                for tok in (meta.yes.token_id, meta.no.token_id):
                    with contextlib.suppress(Exception):
                        await self._refresh_book(tok)

    # ── Telegram status / control (report side-channel) ─────────────────
    async def _telegram_status_text(self) -> str:
        """Chinese status: separate balance/resting/mark/fills/session ledger."""
        await self._refresh_collateral(force=False)
        bal = self._collateral_usdc

        resting = 0.0
        n_orders = 0
        for meta in self.metas.values():
            resting += self.risk.resting_buy_notional(meta)
            n_orders += len(self.state.orders_for(meta.yes.token_id))
            n_orders += len(self.state.orders_for(meta.no.token_id))

        mark: float | None
        try:
            mark = await self.gateway.portfolio_value()
        except Exception:  # noqa: BLE001
            mark = None

        fills: int | None
        try:
            fills = self.state.fill_count()
        except Exception:  # noqa: BLE001
            fills = None

        snap = self.state.snapshot()
        positions = snap.get("positions") or {}
        if not isinstance(positions, dict):
            positions = {}

        blocked = any(self._entry_blocked_infeasible.values()) if self._entry_blocked_infeasible else None
        one_sided = False
        held_sides = 0
        held_tokens: set[str] = set()
        for meta in self.metas.values():
            yes_held = self.state.position(meta.yes.token_id).size >= meta.min_order_size
            no_held = self.state.position(meta.no.token_id).size >= meta.min_order_size
            if yes_held:
                held_sides += 1
                held_tokens.add(meta.yes.token_id)
            if no_held:
                held_sides += 1
                held_tokens.add(meta.no.token_id)
        if held_sides == 1:
            one_sided = True
        elif held_sides == 0:
            one_sided = False
        # Code may allow exit quoting whenever we hold inventory; that is NOT
        # the same as exchange confirming a resting SELL.
        exit_quoting_allowed = held_sides > 0
        exit_order_status: str
        exit_sell_count: int | None
        try:
            live_orders = await self.gateway.open_orders()
        except OpenOrdersReadError:
            exit_order_status = "unknown"
            exit_sell_count = None
        else:
            sells = [o for o in live_orders if o.side is Side.SELL and o.size > 0]
            if held_tokens:
                sells = [o for o in sells if o.token_id in held_tokens]
            exit_sell_count = len(sells)
            exit_order_status = "resting" if exit_sell_count > 0 else "none"
        return format_status_report(
            markets=len(self.metas),
            open_orders=n_orders,
            balance_clob=bal,
            resting_buy=resting,
            position_mark=mark,
            fills=fills,
            session_equity=self.risk.equity,
            session_daily_delta=self.risk.daily_pnl,
            stop_requested=self.telegram.stop_requested,
            positions=positions,
            entry_blocked_infeasible=blocked,
            one_sided_inventory=one_sided,
            exit_quoting_allowed=exit_quoting_allowed,
            exit_order_status=exit_order_status,  # type: ignore[arg-type]
            exit_sell_count=exit_sell_count,
            daily_loss_latched=bool(self.risk.daily_loss_latched),
            recovery_blocked=bool(self._recovery_blocked),
        )

    def _telegram_pnl_text(self) -> str:
        fills: int | None
        try:
            fills = self.state.fill_count()
        except Exception:  # noqa: BLE001
            fills = None
        return format_pnl_report(
            session_equity=self.risk.equity,
            net_cash=self.risk.net_cash,
            inventory=self.risk.inventory_value,
            session_daily_delta=self.risk.daily_pnl,
            fills=fills,
        )

    async def _telegram_stop(self) -> None:
        """/stop: halt new placements only — no mass cancel (safety)."""
        self.telegram.stop_requested = True
        log.critical("telegram_stop_requested")
        self._wake_all()

    async def _refresh_book(self, token_id: str) -> None:
        levels = await self.gateway.get_full_book(token_id)
        if levels is None:
            return
        bids, asks, book_hash = levels
        book = self.md.book(token_id)
        if book is None:
            return
        # drift check: only overwrite if the REST top-of-book disagrees with ours
        cur_bb = book.best_bid()
        cur_ba = book.best_ask()
        rest_bb = max((p for p, _ in bids), default=None)
        rest_ba = min((p for p, _ in asks), default=None)
        drift = (
            (cur_bb is None) != (rest_bb is None)
            or (cur_ba is None) != (rest_ba is None)
            or (cur_bb and rest_bb and abs(cur_bb.price - rest_bb) > book.tick_size)
            or (cur_ba and rest_ba and abs(cur_ba.price - rest_ba) > book.tick_size)
        )
        if drift:
            log.warning("book_drift_corrected", token=token_id[:12])
            book.apply_snapshot(bids, asks, time.time(), book_hash)
            self.md.ack_snapshot(token_id)
            cid = self._token_cid.get(token_id)
            if cid:
                self._wake_cid(cid)

    # ── helpers ─────────────────────────────────────────────────────────
    def _other_token(self, token_id: str) -> str | None:
        cid = self._token_cid.get(token_id)
        return self.metas[cid].other_token(token_id) if cid else None

    def _cid_of_token(self, token_id: str) -> str | None:
        return self._token_cid.get(token_id)

    def _event_group_cost(self, meta: MarketMeta) -> float:
        if not meta.event_id:
            return 0.0
        cost = 0.0
        for m in self.metas.values():
            if m.event_id == meta.event_id:
                for tok in (m.yes.token_id, m.no.token_id):
                    pos = self.state.position(tok)
                    cost += pos.size * pos.avg_price
        return cost


def _fnum(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return None


def _hours_to_end(end_date_iso: str | None, now: float) -> float | None:
    if not end_date_iso:
        return None
    try:
        dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        hrs = (dt.timestamp() - now) / 3600.0
        # A past end date on a still-trading market is a stale/placeholder date
        # (common for "next X" appointment markets) — treat as unknown so we
        # don't wrongly HALT. The true end is signalled by acceptingOrders=False,
        # which the metadata refresh already halts on.
        return hrs if hrs > 0.0 else None
    except (ValueError, TypeError):
        return None


def _empty_view() -> Any:
    from polymaker.marketdata.orderbook import BookView

    return BookView(None, 0.0, None, 0.0, None, None, 0.0, 0.0)
