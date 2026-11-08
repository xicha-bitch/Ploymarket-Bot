# Telegram notify / confirm side-channel

Optional. Live trading works without it. Full Chinese setup guide lives in the
Project docs (`polymarket-telegram-bot.md`).

## Env (names only — paste values locally)

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CONFIRM=0
```

**If a token was ever pasted into chat:** treat it as leaked. In
[@BotFather](https://t.me/BotFather) → `/mybots` → your bot → API Token →
**Revoke**, then put the **new** token only in local `.env` as
`TELEGRAM_BOT_TOKEN=` (and `TELEGRAM_CHAT_ID=`). Never re-paste tokens into
chat. Agents/docs will not save, echo, commit, or call Telegram with a
chat-pasted token.

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
| `/start` `/help` | Chinese command help |
| `/status` `/pnl` `/stop` | Commands in the configured chat |
| `hi` / `在吗` / `跟我聊天` | Short friendly ACK (trading notify bot) |
| `天气` | Explains weather is unsupported; use `/status` |
| other text | Short Chinese help pointer |
| `TELEGRAM_CONFIRM=1` | Inline Approve/Deny before first place of day or size bump |

Unconfigured → no-op (trading continues). Never log token or chat id values.
