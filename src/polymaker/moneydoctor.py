"""`polymaker moneydoctor` — a LIVE trading self-test that actually moves money.

Unlike `doctor` (read-only preflight) and `livetest` (a deep post-only order
that can't fill), this exercises the full order machinery for real:

  1. LIMIT  — place a post-only limit that rests on the book, confirm it appears,
              then cancel it. (Free.)
  2. BUY    — a market (taker) order that fills immediately; confirm shares land
              on-chain.
  3. SELL   — market-sell those shares back; confirm the position goes flat.

It crosses the spread twice and pays taker fees, so it costs a small amount —
reported at the end as the round-trip cost. Sized near the minimum order size.
Taker orders are used ONLY here; the maker strategy never crosses the spread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

import websockets
from rich.console import Console

from polymaker.config import Config
from polymaker.domain import MarketMeta, Quote, Side
from polymaker.strategy.quoting import round_to_tick

if TYPE_CHECKING:
    from polymaker.execution.gateway import ExecutionGateway

USER_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


async def run_moneydoctor(cfg: Config, console: Console, notional_usdc: float | None = None) -> bool:
    from polymaker.execution.gateway import ExecutionGateway

    ok = True

    def check(label: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "[green]✓[/green]" if passed else "[red]✗[/red]"
        console.print(f"  {mark} {label}" + (f"  [dim]{detail}[/dim]" if detail else ""))
        ok = ok and passed

    if not cfg.secrets.has_wallet:
        console.print("[red]No wallet in .env.[/red]")
        return False

    console.print("[bold]polymaker moneydoctor[/bold]  [dim](spends a little real money)[/dim]")
    gw = ExecutionGateway(cfg)
    await gw.connect()
    if cfg.proxy:
        console.print(f"  [dim]· routing via proxy {cfg.proxy.split('@')[-1]}[/dim]")

    meta, book = await _pick_market(cfg, gw)
    if meta is None:
        console.print("[yellow]No suitable liquid market found — run `polymaker scan` first.[/yellow]")
        return False
    token = meta.yes.token_id
    tick, dec = meta.tick_size, meta.price_decimals
    best_bid, best_ask = book["best_bid"], book["best_ask"]
    console.print(f"  market: [bold]{meta.question[:56]}[/bold]")
    console.print(f"  [dim]YES token · bid {best_bid} / ask {best_ask} · "
                  f"spread {round(best_ask - best_bid, 4)} · tick {tick:g}[/dim]")

    bal0 = await gw.collateral_balance()
    console.print(f"  [dim]starting balance: {bal0:.4f} pUSD[/dim]\n")

    # ── 1. LIMIT: rest + cancel ─────────────────────────────────────────
    limit_price = round_to_tick(best_bid - 2 * tick, tick, dec, up=False)
    limit_size = max(meta.min_order_size, 5.0)
    placed = await gw.place([Quote(token, Side.BUY, limit_price, limit_size)], meta)
    if placed:
        await asyncio.sleep(1.5)
        live = await gw.open_orders()
        found = any(o.order_id == placed[0].order_id for o in live)
        check("limit order rests on book", found, f"{limit_size:g} @ {limit_price}, {len(live)} live")
        await gw.cancel([placed[0].order_id])
        await asyncio.sleep(1.0)
        gone = not any(o.order_id == placed[0].order_id for o in await gw.open_orders())
        check("limit order cancels", gone)
    else:
        check("limit order placed", False, "post failed — see logs")

    # ── 2. MARKET BUY ───────────────────────────────────────────────────
    shares_target = meta.min_order_size + 3.0
    buy_usd = round(shares_target * best_ask * 1.06, 2)
    before = await gw._token_balance_opt(token) or 0.0  # baseline shares
    console.print(f"\n  [dim]market BUY ~${buy_usd} of YES (targeting ~{shares_target:g} shares)…[/dim]")
    resp_buy = await gw.market_order(token, Side.BUY, buy_usd, meta, fak=True)
    bought, spent, status = _fill(resp_buy, Side.BUY)
    check("market BUY matched", status == "matched" and bought > 0,
          f"got {bought:.2f} shares for ${spent:.2f} [{status}]")

    # ── settle: user WS (fast) with on-chain as source of truth ─────────
    if bought > 0:
        console.print("  [dim]waiting for settlement (user WS + chain)…[/dim]")
        settled = await _wait_settled(cfg, gw, token, before, timeout=60)
        got = settled - before
        check("buy settled on-chain", got > 0.5, f"{got:.2f} shares now available")

        # ── 3. MARKET SELL — retry until the exchange accepts it ────────
        sell_amt = math.floor(max(got, bought) * 100) / 100
        sold = await _sell_with_retry(gw, token, sell_amt, meta, before, console, check)

        await asyncio.sleep(3.0)
        remaining = await gw._token_balance_opt(token)
        if remaining is not None:
            check("position flat after round-trip", remaining <= before + 0.5,
                  f"{remaining - before:.2f} net shares vs. start ({sold:.2f} sold)")
    else:
        console.print("  [yellow]! buy did not fill — nothing to sell.[/yellow]")

    # ── cost ────────────────────────────────────────────────────────────
    await asyncio.sleep(2.0)
    bal1 = await gw.collateral_balance()
    cost = bal0 - bal1
    console.print(f"\n  [bold]round-trip cost: {cost:.4f} pUSD[/bold] "
                  f"[dim](spread + taker fees; balance {bal0:.2f} → {bal1:.2f})[/dim]")
    console.print(f"\n[bold]{'ALL GOOD' if ok else 'CHECK LOGS'}[/bold]")
    return ok


async def _pick_market(cfg: Config, gw: ExecutionGateway) -> tuple[MarketMeta | None, dict[str, float]]:
    """Pick a liquid, mid-priced market with enough touch depth for a tiny order."""
    from polymaker.catalog.store import CatalogStore

    store = CatalogStore(cfg.paths.db)
    rows = store.top(40)
    store.close()
    for meta, _sc in rows:
        mid = (meta.best_bid + meta.best_ask) / 2 if (meta.best_bid and meta.best_ask) else 0.0
        if not (0.2 < mid < 0.6):
            continue
        book = await gw.get_book(meta.yes.token_id)
        if not book or book["best_bid"] <= 0 or book["best_ask"] >= 1:
            continue
        need_shares = meta.min_order_size + 4
        if book["ask_depth"] >= need_shares and book["bid_depth"] >= need_shares:
            spread = book["best_ask"] - book["best_bid"]
            if spread <= 0.02:  # keep the round-trip cost small
                return meta, book
    return None, {}


async def _wait_settled(cfg: Config, gw: ExecutionGateway, token: str, baseline: float,
                        *, timeout: float = 60.0) -> float:
    """Return the settled on-chain share balance once the buy lands.

    Races two signals: the user WS `trade` status ladder (fast, push-based) and
    an on-chain balance poll (slower, but the source of truth the exchange checks
    when validating a sell). Returns the latest on-chain balance.
    """
    done = asyncio.Event()

    async def chain_poll() -> None:
        while not done.is_set():
            await asyncio.sleep(3.0)
            bal = await gw._token_balance_opt(token)
            if bal is not None and bal > baseline + 0.01:
                done.set()
                return

    async def ws_watch() -> None:
        kw: dict[str, object] = {"ping_interval": 5, "ping_timeout": None, "open_timeout": 10}
        if cfg.proxy:
            kw["proxy"] = cfg.proxy
        creds = gw.creds
        with contextlib.suppress(Exception):
            async with websockets.connect(USER_WS, **kw) as ws:  # type: ignore[arg-type]
                await ws.send(json.dumps({
                    "type": "user",
                    "auth": {"apiKey": creds.api_key, "secret": creds.api_secret,
                             "passphrase": creds.api_passphrase},
                    "markets": [],
                }))
                while not done.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    data = json.loads(raw)
                    for m in data if isinstance(data, list) else [data]:
                        if (isinstance(m, dict) and m.get("event_type") == "trade"
                                and str(m.get("asset_id")) == token
                                and str(m.get("status", "")).upper() in ("MINED", "CONFIRMED")):
                            done.set()
                            return

    tasks = [asyncio.create_task(chain_poll()), asyncio.create_task(ws_watch())]
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(done.wait(), timeout=timeout)
    done.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return await gw._token_balance_opt(token) or baseline


async def _sell_with_retry(
    gw: ExecutionGateway, token: str, amount: float, meta: MarketMeta, baseline: float,
    console: Console, check: Callable[..., None], attempts: int = 5,
) -> float:
    """Market-sell `amount`, retrying until the exchange accepts (balance settles)."""
    for i in range(attempts):
        console.print(f"  [dim]market SELL {amount:g} shares (attempt {i + 1})…[/dim]")
        resp = await gw.market_order(token, Side.SELL, amount, meta, fak=True)
        sold, recv, status = _fill(resp, Side.SELL)
        if status == "matched" and sold > 0:
            check("market SELL filled", True, f"sold {sold:.2f} shares for ${recv:.2f}")
            return sold
        err = resp.get("error", "") if isinstance(resp, dict) else ""
        console.print(f"  [dim]  not filled yet ({status} {str(err)[:48]}); waiting to retry…[/dim]")
        await asyncio.sleep(6.0)
        bal = await gw._token_balance_opt(token)
        if bal is not None:
            amount = math.floor(max(0.0, bal - 0.0) * 100) / 100  # sell what's actually available
            if amount < meta.min_order_size:
                break
    check("market SELL filled", False, "could not fill — flatten manually with cancel-all/limit")
    return 0.0


def _fill(resp: object, side: Side) -> tuple[float, float, str]:
    """Parse a market-order response -> (shares_filled, usd, status).

    makingAmount = what we give, takingAmount = what we get. So for a BUY,
    shares = takingAmount and usd = makingAmount; for a SELL it's the reverse.
    """
    if not isinstance(resp, dict):
        return 0.0, 0.0, "?"
    status = str(resp.get("status", ""))
    making = _f(resp.get("makingAmount"))
    taking = _f(resp.get("takingAmount"))
    return (taking, making, status) if side is Side.BUY else (making, taking, status)


def _f(x: object) -> float:
    try:
        return float(x)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 0.0
