"""polymaker command-line interface (live-only micro-capital fork).

  polymaker scan                 sweep Gamma for political markets -> SQLite
  polymaker markets              rank/browse the catalog
  polymaker markets-add <slug>   append a market to config/markets.toml
  polymaker status               positions / open orders / PnL (reads SQLite)
  polymaker doctor               preflight: wallet auth, balances, WS reachability
  polymaker run                  start LIVE MM (micro risk caps)
  polymaker capital-check        verify risk caps vs bankroll
  polymaker fee-estimate         estimate taker fees for a hypothetical fill
  polymaker cancel-all           panic button
  polymaker telegram-smoke       send 'bot linked' if TELEGRAM_* set
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from polymaker import __version__
from polymaker.config import Config

app = typer.Typer(
    name="polymaker",
    help="Live-only maker for Polymarket CLOB V2 (micro-capital defaults).",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def version() -> None:
    """Print the polymaker version."""
    console.print(f"polymaker {__version__}")


@app.command()
def scan(
    config_dir: str = typer.Option("config", help="config directory"),
    min_liquidity: float = typer.Option(1000.0, help="minimum market liquidity (USDC)"),
    all_markets: bool = typer.Option(False, "--all", help="include non-rewards markets"),
) -> None:
    """Sweep Gamma for political markets, score, and persist to SQLite."""
    from polymaker.catalog.scanner import ScanConfig, run_scan
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)

    async def _go() -> int:
        metas = await run_scan(store, ScanConfig(min_liquidity=min_liquidity, rewards_only=not all_markets))
        return len(metas)

    n = asyncio.run(_go())
    csv_path = Path(config_dir).parent / "markets.csv"
    written = store.export_csv(csv_path)
    console.print(f"[green]Scanned and stored {n} markets.[/green] "
                  f"Wrote [bold]{csv_path}[/bold] ({written} rows) — open it, pick markets, "
                  f"then `polymaker markets-add <slug>`.")
    store.close()


@app.command()
def markets(
    config_dir: str = typer.Option("config", help="config directory"),
    limit: int = typer.Option(25, help="rows to show"),
) -> None:
    """Show the top scored markets from the catalog."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    rows = store.top(limit)
    if not rows:
        console.print("[yellow]Catalog empty. Run `polymaker scan` first.[/yellow]")
        raise typer.Exit()

    table = Table(title="Political markets by score")
    for col in ("score", "reward/day", "rebate/day", "spread", "tick", "neg", "question"):
        table.add_column(col, justify="right" if col != "question" else "left")
    for meta, sc in rows:
        table.add_row(
            f"{sc.score:.2f}", f"{meta.rewards_daily_rate:.0f}", f"{sc.rebate_potential:.0f}",
            f"{sc.spread:.3f}", f"{meta.tick_size:g}", "Y" if meta.neg_risk else "-",
            meta.question[:60],
        )
    console.print(table)
    console.print("\nAdd one with: [bold]polymaker markets-add <slug>[/bold]  (slugs are in the catalog)")


@app.command(name="markets-add")
def markets_add(
    slug: str,
    profile: str = typer.Option("micro-capital", help="strategy profile"),
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """Append a market (by slug) to config/markets.toml."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    meta = store.get_by_slug(slug)
    store.close()
    if meta is None:
        console.print(f"[red]No market with slug {slug!r} in the catalog. Run `polymaker scan`.[/red]")
        raise typer.Exit(1)

    path = Path(config_dir) / "markets.toml"
    block = f'\n[[markets]]\nslug    = "{slug}"\nprofile = "{profile}"\nenabled = true\n'
    with path.open("a") as fh:
        fh.write(block)
    console.print(f"[green]Added[/green] {meta.question[:60]!r} to {path}")


@app.command()
def status(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show positions, open orders, and marks from the local state DB."""
    from polymaker.state.store import StateStore

    cfg = Config.load(config_dir)
    store = StateStore(cfg.paths.db)
    snap = store.snapshot()
    console.print(f"[bold]Open orders:[/bold] {snap['open_orders']}")
    positions = snap["positions"]  # type: ignore[assignment]
    if not positions:
        console.print("[dim]No open positions.[/dim]")
    else:
        table = Table(title="Positions")
        table.add_column("token")
        table.add_column("size", justify="right")
        table.add_column("avg", justify="right")
        for tok, p in positions.items():  # type: ignore[union-attr]
            table.add_row(tok[:16] + "…", f"{p['size']:.2f}", f"{p['avg_price']:.3f}")
        console.print(table)
    store.close()


@app.command()
def pnl(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Show PnL from the recorded snapshots (equity, daily PnL, fills)."""
    import sqlite3

    cfg = Config.load(config_dir)
    conn = sqlite3.connect(cfg.paths.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT ts, equity, net_cash, inventory_value, daily_pnl FROM pnl_snapshots "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchall()
    if not rows:
        console.print("[yellow]No PnL snapshots yet (run the engine first).[/yellow]")
    else:
        r = rows[0]
        color = "green" if r["daily_pnl"] >= 0 else "red"
        console.print(
            f"[bold]equity:[/bold] {r['equity']:.4f}  "
            f"[bold]inventory:[/bold] {r['inventory_value']:.4f}  "
            f"[bold]net cash:[/bold] {r['net_cash']:.4f}"
        )
        console.print(f"[bold]daily PnL:[/bold] [{color}]{r['daily_pnl']:+.4f}[/{color}] pUSD")
    nfills = conn.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"]
    console.print(f"[dim]total fills recorded: {nfills}[/dim]")
    conn.close()


@app.command(name="export-csv")
def export_csv(
    config_dir: str = typer.Option("config", help="config directory"),
    out: str = typer.Option("markets.csv", help="output CSV path"),
    limit: int = typer.Option(500, help="max rows"),
) -> None:
    """Export the scored market catalog to a CSV for easy picking."""
    from polymaker.catalog.store import CatalogStore

    cfg = Config.load(config_dir)
    store = CatalogStore(cfg.paths.db)
    n = store.export_csv(Path(out), limit=limit)
    console.print(f"Wrote {n} rows to {out}")
    store.close()


@app.command()
def doctor(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Preflight: wallet auth, balances, WS reachability."""
    from polymaker.doctor import run_doctor

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_doctor(cfg, console))
    raise typer.Exit(0 if ok else 1)


@app.command()
def run(
    config_dir: str = typer.Option("config", help="config directory"),
    bankroll: float = typer.Option(
        25.0,
        help="bankroll USDC/pUSD for preflight capital-check before live start",
    ),
) -> None:
    """Start the live market maker with micro-capital risk caps."""
    from polymaker.capital import assess_capital
    from polymaker.engine import Engine
    from polymaker.logging import configure

    cfg = Config.load(config_dir)
    report = assess_capital(cfg, bankroll)
    console.print(
        "[bold yellow]LIVE[/bold yellow] — real orders; micro caps "
        f"(exposure≤${cfg.risk.max_total_exposure_usdc:.0f}, "
        f"daily kill≤${cfg.risk.daily_loss_kill_usdc:.0f})."
    )
    if not report.ok:
        for w in report.warnings:
            console.print(f"[yellow]capital warning:[/yellow] {w}")
        console.print(
            "[red]Refusing live start: capital-check failed. "
            "Tune config/ risk caps or fund to match --bankroll.[/red]"
        )
        raise typer.Exit(2)
    if not cfg.secrets.has_wallet:
        console.print(
            "[red]Set PK and BROWSER_ADDRESS in local .env "
            "(on your machine; never paste keys into chat).[/red]"
        )
        raise typer.Exit(2)

    configure(json_file=Path(cfg.paths.log_dir) / "live.jsonl")
    if cfg.engine.loop == "uvloop":
        try:
            import uvloop

            uvloop.install()
        except Exception:  # noqa: BLE001
            pass

    engine = Engine(cfg)

    async def _go() -> None:
        try:
            await engine.run_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await engine.shutdown()

    console.print("[bold green]Starting polymaker LIVE…[/bold green]")
    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped.[/yellow]")


@app.command(name="capital-check")
def capital_check(
    bankroll: float = typer.Option(25.0, help="your intended bankroll in USDC/pUSD"),
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """Check whether risk/strategy caps fit a small bankroll (~$20–30)."""
    from polymaker.capital import assess_capital

    cfg = Config.load(config_dir)
    report = assess_capital(cfg, bankroll)
    console.print(
        f"bankroll=${report.bankroll_usdc:.2f}  "
        f"exposure_cap=${report.max_total_exposure_usdc:.2f}  "
        f"market_cap=${report.max_market_notional_usdc:.2f}  "
        f"daily_kill=${report.daily_loss_kill_usdc:.2f}"
    )
    if report.ok:
        console.print("[green]Capital check passed for current config.[/green]")
    else:
        console.print("[red]Capital check failed:[/red]")
        for w in report.warnings:
            console.print(f"  - {w}")
    for tip in report.tips:
        console.print(f"[dim]tip:[/dim] {tip}")
    raise typer.Exit(0 if report.ok else 1)


@app.command(name="fee-estimate")
def fee_estimate(
    shares: float = typer.Option(5.0, help="share quantity"),
    price: float = typer.Option(0.5, help="fill price in [0,1]"),
    category: str = typer.Option("crypto", help="market category for taker fee rate"),
) -> None:
    """Estimate taker fee (maker is 0) using the public fee curve."""
    from polymaker.fees import estimate_taker_fee

    est = estimate_taker_fee(shares, price, category=category)
    console.print(
        f"category={est.category} shares={est.shares} price={est.price} "
        f"notional=${est.notional_usdc:.4f}"
    )
    console.print(
        f"taker_fee=${est.taker_fee_usdc:.5f} ({est.taker_fee_bps_of_notional:.1f} bps)  "
        f"maker_fee=$0"
    )


@app.command()
def livetest(
    config_dir: str = typer.Option("config", help="config directory"),
    notional: float = typer.Option(5.0, help="order notional in USDC"),
) -> None:
    """Live wallet round-trip: place a deep post-only order and cancel it (~$5)."""
    from polymaker.livetest import run_livetest

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_livetest(cfg, console, notional))
    raise typer.Exit(0 if ok else 1)


@app.command()
def moneydoctor(
    config_dir: str = typer.Option("config", help="config directory"),
) -> None:
    """LIVE trading self-test: rest a limit, then market buy + sell (spends a little)."""
    from polymaker.moneydoctor import run_moneydoctor

    cfg = Config.load(config_dir)
    ok = asyncio.run(run_moneydoctor(cfg, console))
    raise typer.Exit(0 if ok else 1)


@app.command(name="cancel-all")
def cancel_all(config_dir: str = typer.Option("config", help="config directory")) -> None:
    """Cancel all open orders for the wallet (panic button)."""
    from polymaker.execution.gateway import ExecutionGateway

    cfg = Config.load(config_dir)
    gw = ExecutionGateway(cfg)

    async def _go() -> None:
        await gw.connect()
        await gw.cancel_all()

    asyncio.run(_go())
    console.print("[green]Sent cancel-all.[/green]")


@app.command(name="telegram-smoke")
def telegram_smoke() -> None:
    """Send a one-line 'bot linked' message if TELEGRAM_* env keys are set.

    Never prints token or chat id — only whether the send succeeded.
    """
    from polymaker.telegram_bot import TelegramBot, telegram_configured

    # Presence check only (key names); values never printed.
    if not telegram_configured():
        console.print(
            "[yellow]Telegram not configured.[/yellow] "
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to local .env "
            "(never paste them into chat)."
        )
        raise typer.Exit(1)

    ok = asyncio.run(TelegramBot().smoke_send())
    if ok:
        console.print("[green]Smoke OK — check Telegram for 'bot linked'.[/green]")
    else:
        console.print("[red]Smoke send failed (token/chat wrong or network).[/red]")
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
