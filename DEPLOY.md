# Deploy — live-only micro-capital Polymarket bot

**Live only.** `polymaker run` places real orders under micro risk caps.
There is no paper / sim / dry-run mode.

Secrets stay on your machine or in Cursor Environment Secrets — never in git,
chat, or this repo.

## 1. Get the code

```bash
git clone https://github.com/xicha-bitch/ploymarket-bot.git
cd ploymarket-bot
```

Or use this Cloud Agent VM checkout if you already have it mounted.

## 2. Install (uv)

Requires Python 3.12+.

```bash
# if needed: curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --extra dev
uv run polymaker --help
uv run polymaker capital-check --bankroll 25   # no keys required
```

`uv run polymaker doctor` without secrets exits `1` with `NOT READY` /
`PK + BROWSER_ADDRESS set` — that is expected and proves the install.

## 3. Secrets (you must provide — never paste into chat)

| Name | Required | Where |
|------|----------|--------|
| `PK` | yes | local `.env` **or** Cursor Environment Secret |
| `BROWSER_ADDRESS` | yes | local `.env` **or** Cursor Environment Secret |

Local file (recommended for a dedicated VM):

```bash
cp .env.example .env
# edit .env yourself: set PK + BROWSER_ADDRESS
# confirm signature_type in config/config.toml (often 3 for deposit wallets)
```

Cursor Cloud Agents: Dashboard → Cloud Agents → this Environment → Secrets.
Add names exactly `PK` and `BROWSER_ADDRESS` (no values in docs/PRs).

Fund the hot wallet / Polymarket deposit with **only** the intended bankroll
(~$20–30 USDC/pUSD on Polygon).

## 4. Start commands (exact)

```bash
uv sync --extra dev
uv run polymaker capital-check --bankroll 25
uv run polymaker doctor
uv run polymaker scan
uv run polymaker markets-add <slug> --profile micro-capital   # or micro-tight
uv run polymaker run --bankroll 25                            # LIVE
uv run polymaker status
uv run polymaker cancel-all                                   # panic
```

Optional tiny live plumbing: `livetest`, `moneydoctor` (spend a little).

Default micro caps (`config/`): exposure ≤ $25, market ≤ $12, daily kill ≤ $4,
`post_only = true`. See [MICRO.md](MICRO.md).

## 5. Sanity without keys (already verified in deploy)

| Command | Expected without `.env` |
|---------|-------------------------|
| `uv run polymaker --help` | exit 0, command list |
| `uv run polymaker capital-check --bankroll 25` | exit 0, caps print |
| `uv run polymaker doctor` | exit 1, `NOT READY`, missing PK/BROWSER |
| `uv run polymaker run --bankroll 25` | exit 2, refuse to set local `.env` |

## Panic / stop

```bash
uv run polymaker cancel-all
# Ctrl-C the run process
```
