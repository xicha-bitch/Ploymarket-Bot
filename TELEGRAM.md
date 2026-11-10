# Telegram status / notify side-channel

Optional. Live trading works without it. Restored for **status reporting** on
the micro-budget / positions-v2 stack (not a rollback of trading logic).

## Env (names only — paste values locally)

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CONFIRM=0
TELEGRAM_NOTIFY=quiet
```

**If a token was ever pasted into chat:** treat it as leaked. In
[@BotFather](https://t.me/BotFather) → `/mybots` → your bot → API Token →
**Revoke**, then put the **new** token only in local `.env`. Never re-paste
tokens into chat.

1. Create a bot with [@BotFather](https://t.me/BotFather) → token → `TELEGRAM_BOT_TOKEN`
2. Message the bot, then read `chat.id` from `getUpdates` → `TELEGRAM_CHAT_ID`
3. Chat must be a **private** user chat (groups/channels are blocked outbound)
4. Smoke: `uv run polymaker telegram-smoke` (sends `bot linked`; never prints the token)
5. Run as usual: `uv run polymaker run` — notifier polls inside the engine

## Behaviour

Default is **quiet**: no periodic status spam, no place/cancel pushes.
`/status` `/pnl` and chat replies work when configured. `/stop` only pauses
**new** placements (no mass cancel).

| `TELEGRAM_NOTIFY` | Auto pushes |
|-------------------|-------------|
| `quiet` (default) | none |
| `fills` | fills only |
| `all` | fills + place failures + ~15 min status (legacy) |

Status lines separate: CLOB balance · resting BUY · position mark · fills · PnL.
API failures show `unknown`, never fake `$0`.

Unconfigured → no-op (trading continues). Telegram faults never kill the trade loop.
Never log token or chat id values.
