"""RiskManager: pre-trade gates and circuit breakers (see the README).

Consulted by the engine before every quote set. Returns a per-market decision
(size scale / reduce-only / halt) and owns the global kill switches. Position
and order data come from the StateStore; fair-value marks are pushed in by the
engine so PnL is always current.

Daily-loss accounting rules (fill/cash halt fix):
  * Trading cash moves ONLY via real fills (note_fill) — never invented.
  * Unexplained inventory MTM changes (REST flatten without fills) conserve
    equity via ``_conserved_usdc`` so they do not invent a trading loss.
  * Deposits/withdrawals are outside this ledger (not trading PnL).
  * Daily-loss latch is sticky across restart within the Shanghai day; restart
    must not wipe a real halt. Clearing a false-positive latch requires an
    authoritative (successful) trades+positions reconcile — never internal PnL
    rewriting alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from polymaker.config import RiskConfig
from polymaker.domain import Fill, MarketMeta, Side
from polymaker.logging import get_logger
from polymaker.state.store import StateStore

log = get_logger("risk.manager")

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def shanghai_day_key(ts: float | None = None) -> str:
    """Calendar day key in Asia/Shanghai for daily-loss baseline rolls."""
    if ts is None:
        dt = datetime.now(tz=_SHANGHAI)
    else:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_SHANGHAI)
    return dt.strftime("%Y-%m-%d")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    halt: bool  # HALTED regime for this market
    reduce_only: bool  # REDUCE_ONLY regime for this market
    size_scale: float  # multiply quote sizes by this [0,1]
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, store: StateStore) -> None:
        self._cfg = cfg
        self._store = store
        self._marks: dict[str, float] = {}  # token_id -> fair value
        self._net_cash = 0.0  # cumulative signed cash from fills (+sell, -buy)
        self._day_start_equity = 0.0
        self._day_key = ""
        self._killed = False
        self._daily_loss_latched = False
        # Inventory MTM that changed without a fill — held so equity/daily_pnl
        # are not invented. Not a fill; not deposit/withdrawal.
        self._conserved_usdc = 0.0
        self._order_attempts = 0
        self._order_errors = 0

    # ── PnL bookkeeping ─────────────────────────────────────────────────
    def note_fill(self, fill: Fill, *, count_toward_daily: bool = True) -> None:
        """Apply authoritative fill cash. Never call with synthetic/invented fills.

        When ``count_toward_daily`` is False, skip the daily-loss trip; the
        engine should ``absorb_equity_delta_into_baseline`` after apply_fill so
        historical backfills do not move today's daily_pnl.
        """
        notional = fill.price * fill.size
        signed = notional if fill.side is Side.SELL else -notional
        self._net_cash += signed
        # A real SELL explains prior unexplained inventory conservation.
        if fill.side is Side.SELL and self._conserved_usdc > 0:
            release = min(self._conserved_usdc, notional)
            self._conserved_usdc -= release
            if self._conserved_usdc < 1e-12:
                self._conserved_usdc = 0.0
        self._persist_day()
        if count_toward_daily:
            self._trip_daily_loss_if_needed()

    def absorb_equity_delta_into_baseline(self, equity_before: float) -> None:
        """Shift day_start by equity change so daily_pnl is unchanged."""
        if not self._day_key:
            return
        delta = self.equity - equity_before
        if abs(delta) < 1e-12:
            return
        self._day_start_equity += delta
        self._persist_day()

    def note_unexplained_inventory_delta(self, delta_inv_value: float) -> None:
        """Inventory MTM changed without a fill (e.g. REST flatten).

        Conserves equity so unexplained disposition does not invent trading PnL.
        Does NOT create fills or credit ``net_cash``. Does NOT clear a daily-loss
        latch (that requires authoritative reconcile).
        """
        if abs(delta_inv_value) < 1e-12:
            return
        # inv dropped by X (delta negative) -> conserved += X so equity stable
        self._conserved_usdc -= delta_inv_value
        if abs(self._conserved_usdc) < 1e-12:
            self._conserved_usdc = 0.0
        log.warning(
            "inventory_gap_conserved",
            delta_inv=round(delta_inv_value, 6),
            conserved=round(self._conserved_usdc, 6),
            net_cash=round(self._net_cash, 6),
        )
        self._persist_day()

    def update_mark(self, token_id: str, fv: float) -> None:
        self._marks[token_id] = fv

    def unit_value(self, token_id: str, fallback: float = 0.0) -> float:
        """Mark if known, else fallback (typically avg_price)."""
        if token_id in self._marks:
            return float(self._marks[token_id])
        return float(fallback)

    def _inventory_value(self) -> float:
        total = 0.0
        for tok, pos in self._store.positions.items():
            if pos.size > 0:
                total += pos.size * self._marks.get(tok, pos.avg_price)
        return total

    @property
    def net_cash(self) -> float:
        return self._net_cash

    @property
    def conserved_usdc(self) -> float:
        return self._conserved_usdc

    @property
    def inventory_value(self) -> float:
        return self._inventory_value()

    @property
    def equity(self) -> float:
        return self._net_cash + self._inventory_value() + self._conserved_usdc

    @property
    def daily_pnl(self) -> float:
        return self.equity - self._day_start_equity

    @property
    def day_key(self) -> str:
        return self._day_key

    @property
    def day_start_equity(self) -> float:
        return self._day_start_equity

    @property
    def daily_loss_latched(self) -> bool:
        return self._daily_loss_latched

    def reset_day(self) -> None:
        """Set baseline to current equity for the current Shanghai day.

        Prefer ``bootstrap_session`` / ``roll_day_if_needed`` at startup so a
        restart cannot wipe an in-day loss latch.
        """
        self._day_key = shanghai_day_key()
        self._day_start_equity = self.equity
        self._persist_day()

    def roll_day_if_needed(self, ts: float | None = None) -> bool:
        """Roll baseline at Asia/Shanghai midnight. Returns True if rolled.

        A new calendar day clears the daily-loss latch (fresh day). Same-day
        calls are no-ops.
        """
        key = shanghai_day_key(ts)
        if self._day_key == key:
            return False
        prev = self._day_key
        self._day_key = key
        self._day_start_equity = self.equity
        self._daily_loss_latched = False
        log.info("daily_pnl_day_rolled", prev=prev or None, day=key,
                 day_start=round(self._day_start_equity, 6))
        self._persist_day()
        return True

    def bootstrap_session(self) -> None:
        """Restore day baseline + latch; rebuild cash from fills only.

        Never invents fills. Never clears a same-day latch. Never rewrites cash
        from a historical daily_pnl figure.
        """
        self._net_cash = self._store.sum_fill_cash()
        row = self._store.load_risk_day()
        today = shanghai_day_key()
        if row is not None:
            self._day_key = str(row["day_key"])
            self._day_start_equity = float(row["day_start_equity"])
            self._daily_loss_latched = bool(row["daily_loss_latched"])
            self._conserved_usdc = float(row["conserved_usdc"])
            # Prefer persisted net_cash only when fills table is empty AND the
            # persisted value came from prior fill accounting in this code path;
            # fills remain the authority when present.
            if self._store.fill_count() == 0:
                # Keep rebuilt 0 from fills; do not import invented cash.
                self._net_cash = 0.0
            if self.roll_day_if_needed():
                return
            # Same day: if a pnl snapshot already breached the kill, keep latch
            # even when an older risk_day row lacked the flag.
            snap = self._store.latest_pnl_snapshot()
            if (
                snap is not None
                and shanghai_day_key(float(snap["ts"])) == today
                and float(snap["daily_pnl"]) <= -self._cfg.daily_loss_kill_usdc
            ):
                self._daily_loss_latched = True
            self._persist_day()
            return

        # First boot of persistence: same Shanghai day as latest snapshot →
        # preserve baseline + latch so restart cannot bypass risk. Do NOT copy
        # snapshot daily_pnl into cash/conserved.
        snap = self._store.latest_pnl_snapshot()
        if snap is not None and shanghai_day_key(float(snap["ts"])) == today:
            self._day_key = today
            self._day_start_equity = float(snap["equity"]) - float(snap["daily_pnl"])
            if float(snap["daily_pnl"]) <= -self._cfg.daily_loss_kill_usdc:
                self._daily_loss_latched = True
            self._conserved_usdc = 0.0
            log.warning(
                "daily_loss_restored_from_snapshot",
                day=today,
                day_start=round(self._day_start_equity, 6),
                latched=self._daily_loss_latched,
            )
            self._persist_day()
            return

        self._day_key = today
        self._day_start_equity = self.equity
        self._daily_loss_latched = False
        self._conserved_usdc = 0.0
        self._persist_day()

    def try_clear_false_daily_loss(self, *, authoritative_ok: bool) -> tuple[bool, str]:
        """Clear sticky daily-loss latch only after authoritative data succeeded.

        ``authoritative_ok`` means trades + positions reads completed successfully
        (empty results are OK; transport/TLS failure is NOT). Never clear from
        internal PnL rewriting alone.
        """
        if not self._daily_loss_latched:
            return False, "not_latched"
        if not authoritative_ok:
            return False, "authoritative_data_unknown"
        self._trip_daily_loss_if_needed()
        if self.daily_pnl <= -self._cfg.daily_loss_kill_usdc:
            return False, "real_loss_cap_still_hit"
        self._daily_loss_latched = False
        self._persist_day()
        log.warning(
            "daily_loss_false_positive_cleared",
            daily_pnl=round(self.daily_pnl, 6),
            conserved=round(self._conserved_usdc, 6),
        )
        return True, "cleared_false_positive"

    def _trip_daily_loss_if_needed(self) -> None:
        if self._daily_loss_latched:
            return
        if self.daily_pnl <= -self._cfg.daily_loss_kill_usdc:
            self._daily_loss_latched = True
            log.critical(
                "daily_loss_latched",
                daily_pnl=round(self.daily_pnl, 6),
                kill=self._cfg.daily_loss_kill_usdc,
            )
            self._persist_day()

    def _persist_day(self) -> None:
        if not self._day_key:
            return
        self._store.save_risk_day(
            day_key=self._day_key,
            day_start_equity=self._day_start_equity,
            net_cash=self._net_cash,
            conserved_usdc=self._conserved_usdc,
            daily_loss_latched=self._daily_loss_latched,
        )

    # ── error-rate breaker ──────────────────────────────────────────────
    def note_order_result(self, ok: bool) -> None:
        self._order_attempts += 1
        if not ok:
            self._order_errors += 1

    @property
    def error_rate(self) -> float:
        return self._order_errors / self._order_attempts if self._order_attempts >= 20 else 0.0

    # ── global kill switch ──────────────────────────────────────────────
    def global_halt(self) -> tuple[bool, str]:
        if self._killed:
            return True, "manual_kill"
        self._trip_daily_loss_if_needed()
        if self._daily_loss_latched:
            return True, f"daily_loss {self.daily_pnl:.0f}"
        if self.error_rate >= self._cfg.max_order_error_rate:
            return True, f"error_rate {self.error_rate:.2f}"
        return False, ""

    def kill(self) -> None:
        self._killed = True
        log.critical("kill_switch_engaged")

    # ── per-market evaluation ───────────────────────────────────────────
    def evaluate(
        self, meta: MarketMeta, *, ws_stale: bool, event_group_cost: float
    ) -> RiskDecision:
        halted, why = self.global_halt()
        if halted:
            return RiskDecision(True, False, 0.0, why)
        if ws_stale:
            return RiskDecision(True, False, 0.0, "ws_stale")

        market_notional = self._market_notional(meta)
        total_exposure = self._total_exposure()

        # hard caps -> reduce only
        if market_notional >= self._cfg.max_market_notional_usdc:
            return RiskDecision(False, True, 1.0, "market_cap")
        if event_group_cost >= self._cfg.max_event_group_loss_usdc:
            return RiskDecision(False, True, 1.0, "event_group_cap")
        if total_exposure >= self._cfg.max_total_exposure_usdc:
            return RiskDecision(False, True, 1.0, "total_exposure_cap")

        # soft scaling: taper size as any cap is approached (worst-binding wins)
        scale = min(
            _headroom(market_notional, self._cfg.max_market_notional_usdc),
            _headroom(total_exposure, self._cfg.max_total_exposure_usdc),
            _headroom(event_group_cost, self._cfg.max_event_group_loss_usdc),
        )
        return RiskDecision(False, False, scale, "")

    def filled_market_notional(self, meta: MarketMeta) -> float:
        """Filled-inventory notional for this market (no resting orders)."""
        return self._market_notional(meta)

    def filled_total_exposure(self) -> float:
        """Filled-inventory notional across all tokens (no resting orders)."""
        return self._total_exposure()

    def resting_buy_notional(self, meta: MarketMeta) -> float:
        """Resting BUY notional on this market's tokens."""
        resting = 0.0
        for tok in (meta.yes.token_id, meta.no.token_id):
            for o in self._store.orders_for(tok):
                if o.side is Side.BUY and o.size > 0:
                    resting += o.notional
        return resting

    def _market_notional(self, meta: MarketMeta) -> float:
        """Filled-inventory notional for this market. Deliberately does NOT count
        our own resting BUY orders: those are the quotes we're about to replace,
        and counting them makes the size taper collapse the moment we place a full
        quote (self-reinforcing cancel/replace churn). Worst-case fill is bounded
        instead by small per-quote sizes + the position cap that this drives.

        See ``worst_case_market_notional`` for an advisory filled+resting figure
        (does not drive the hard evaluate() taper — avoids churn). Place-time
        budget gates in the engine enforce resting+filled vs collateral/caps.
        """
        total = 0.0
        for tok in (meta.yes.token_id, meta.no.token_id):
            pos = self._store.position(tok)
            total += pos.size * self._marks.get(tok, pos.avg_price or 0.5)
        return total

    def worst_case_market_notional(self, meta: MarketMeta) -> float:
        """Advisory: filled inventory + resting BUY notional if all fills at once.

        Not used for hard REDUCE_ONLY (that would churn); useful for logging /
        size_scale soft advice and unit tests of micro-cap overshoot risk.
        """
        return self._market_notional(meta) + self.resting_buy_notional(meta)

    def advisory_size_scale(self, meta: MarketMeta) -> float:
        """Soft scale from worst-case filled+resting vs market cap (advisory only)."""
        return _headroom(
            self.worst_case_market_notional(meta), self._cfg.max_market_notional_usdc
        )

    def _total_exposure(self) -> float:
        total = 0.0
        for tok, pos in self._store.positions.items():
            if pos.size > 0:
                total += pos.size * self._marks.get(tok, pos.avg_price or 0.5)
        return total


def _headroom(current: float, cap: float) -> float:
    """1.0 well below the cap, tapering to 0 as we approach it (from 70%)."""
    if cap <= 0:
        return 1.0
    frac = current / cap
    if frac <= 0.7:
        return 1.0
    return max(0.0, (1.0 - frac) / 0.3)
