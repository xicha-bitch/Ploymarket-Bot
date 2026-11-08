# Telegram notify / confirm side-channel

Optional. Live trading works without it. Full Chinese setup guide lives in the
Project docs (`polymarket-telegram-bot.md`).

## Env (names only — paste values locally)

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CONFIRM=0
```

1. Create a bot with [@BotFather](https://t.me/BotFather) → token → `TELEGRAM_BOT_TOKEN`
2. Message the bot, then read `chat.id` from `getUpdates` → `TELEGRAM_CHAT_ID`
3. Smoke: `uv run polymaker telegram-smoke` (sends `bot linked`; never prints the token)
4. Run as usual: `uv run polymaker run` — notifier polls inside the engine

## Behaviour

| Hook | What happens |
|------|----------------|
| Fill | Telegram push |
| Place failure / partial | Telegram push |
| Every ~15 min | Status summary |
| `/status` `/pnl` `/stop` | Commands in the configured chat |
| `TELEGRAM_CONFIRM=1` | Inline Approve/Deny before first place of day or size bump |

Unconfigured → no-op (trading continues). Never log token or chat id values.
