# Micro-capital live guide (~$20–30)

Fork of [warproxxx/poly-maker](https://github.com/warproxxx/poly-maker) with **small live risk caps**.  
**Live-only** — there is no paper/sim/dry-run mode.

A GitHub repo is just the published source tree (code + history) you clone; this folder is that code.

## Live defaults (safety)

| Knob | Value | Role |
|------|------:|------|
| `max_total_exposure_usdc` | 25 | hard total notional + open buys |
| `max_market_notional_usdc` | 12 | per-market cap |
| `daily_loss_kill_usdc` | 4 | stop quoting after daily realized loss |
| `micro-capital` `base_size_usdc` | 4 | ~per-side quote size |
| `micro-tight` `base_size_usdc` | 2.5 | even smaller first live profile |
| `post_only` | true | maker-only (no taker fee path) |

Upstream large presets: `config-original/`.

## Env vars (set locally — never paste into chat)

Copy `.env.example` → `.env` on **your** machine:

| Var | Required for live | Meaning |
|-----|-------------------|---------|
| `PK` | yes | Signer private key (hot wallet only) |
| `BROWSER_ADDRESS` | yes | Polymarket deposit/funder address from the UI |
| `POLYGON_RPC` | optional | Custom Polygon RPC |
| `POLY_BUILDER_KEY` / `SECRET` / `PASSPHRASE` | only if merge via relayer | Builder API creds |
| `POLY_RELAYER_URL` | optional | Default relayer URL is fine for most |

Also set `signature_type` in `config/config.toml` to match your account (often `3` for current deposit wallets).

## Live path

Full clone → install → secrets → start: **[DEPLOY.md](DEPLOY.md)**.

```bash
uv sync --extra dev
cp .env.example .env          # fill PK + BROWSER_ADDRESS locally (never paste into chat)
uv run polymaker capital-check --bankroll 25
uv run polymaker doctor
uv run polymaker scan
uv run polymaker markets-add <slug> --profile micro-capital   # or micro-tight
uv run polymaker run --bankroll 25                            # LIVE only
uv run polymaker status
uv run polymaker cancel-all                                   # panic
```

Optional tiny live plumbing checks (spend little): `livetest`, `moneydoctor`.

## Honest expectations

- Maker rebates often need ≥$1 accrued — $20–30 may earn $0 rebates.
- One-sided fills create directional inventory; caps limit blast radius, not losses.
- Not financial advice; you can lose the bankroll.
