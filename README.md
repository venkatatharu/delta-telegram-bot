# Delta Exchange — Telegram Trading Bot 🤖

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python)]
[![Exchange](https://img.shields.io/badge/Exchange-Delta%20India-orange)]
[![Default](https://img.shields.io/badge/Default-Testnet-brightgreen)]

A **manual, human-in-the-loop** trading bot for **Delta Exchange India**, driven
entirely from Telegram. It is **not** an algorithmic/auto-trading bot: there is no
signal generation and nothing executes on its own.

> 🔒 **The one rule.** Every action that places, modifies, or closes a real order
> is gated behind an inline **✅ Confirm / ❌ Cancel** button handled by
> `on_confirm()`. The guided `/trade` flow, the one-line quick-trade command, the
> `/close` and `/panic` flows, and the TradingView webhook **all** route through
> that same step — none of them can fire an order directly.

## Commands

| Command | What it does |
|---|---|
| `/start` | Shows the active **network** (TESTNET/LIVE) + trading state |
| `/help` | Command reference |
| `/trade` | Guided flow: coin → side → **Fixed qty or Risk-based** sizing → **Fixed or Trailing SL** → **single TP or scale-out** → confirm |
| `/trade SYMBOL side qty entry sl=X tp=Y` | One-line quick trade (also `trail=`/`lev=`, `entry=market`) → the **same** confirm step |
| `/positions` | Open positions from the exchange |
| `/balance` | Wallet balances |
| `/pnl` | Realised + unrealised P&L and daily-loss usage |
| `/close SYMBOL` | Close a tracked position (asks to confirm) |
| `/panic` | Close **every** tracked position and halt new orders (asks to confirm) |
| `/resume` | Clear the `/panic` halt |
| `/journal` | Sends `trade_journal.csv` back to you |

Only the Telegram user whose id matches `TELEGRAM_OWNER_ID` can drive the bot.

## Features

1. **Testnet / live toggle** — `USE_TESTNET` switches between
   `https://cdn-ind.testnet.deltaex.org` and `https://api.india.delta.exchange`;
   the active network is shown in `/start` and on every confirmation card.
2. **Risk-based sizing** — contracts computed from `risk_amount ÷ (price distance ×
   contracting_price)`, floored to the product's size increment, shown before confirm.
3. **Kill-switch** — `/panic` closes everything tracked and halts; `/resume` re-enables.
4. **Trailing stop-loss** — `stop_order_type: trailing_stop_loss_order` + `trail_amount`.
5. **Partial TP / scale-out** — two TP legs tracked per symbol in
   `state["positions"][symbol]["tp_legs"]`, with fills detected by polling `get_positions()`.
6. **Quick one-line trade** — routes to the same confirm step (no separate path).
7. **Trade journal** — every open/close event appended to `trade_journal.csv`, `/journal` to fetch it.
8. **TradingView webhook** — optional Flask `POST /webhook` that turns an alert into a
   Telegram confirm card; **never auto-executes**.
9. **Retry / backoff** — the Delta client retries network errors and HTTP 429/5xx with
   exponential backoff (honouring `Retry-After`); order POSTs use a `client_order_id`
   and reconcile ambiguous failures so a retry can't double-submit.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate     # or: py -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the credentials
python -X utf8 delta_telegram_bot.py
```

> **Windows:** create `.env` from `.env.example`, fill in your credentials, then
> just **double-click `start_bot.bat`** — it creates the venv, installs
> dependencies, and launches the bot for you (Ctrl+C to stop).

`.env` keys (see `.env.example` for the full list):

```env
DELTA_API_KEY=...
DELTA_API_SECRET=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_OWNER_ID=<your numeric telegram id>
USE_TESTNET=true
```

- **Get Delta keys:** Delta India → API Management → enable Read + Trading. **Never Withdrawal.**
- **Get Telegram creds:** `@BotFather` → `/newbot` for the token; `@userinfobot` for your numeric id (`TELEGRAM_OWNER_ID`). Send your bot `/start` once.
- **Flask is optional** — only needed for the TradingView webhook (Feature 8). The core bot imports it lazily and runs without it.

> ⚠️ **Defaults to testnet.** Set `USE_TESTNET=false` (and optionally `ACK_LIVE=1`)
> only when you're ready for real money. Cryptocurrency trading is risky —
> trade at your own risk.

## Testing

```bash
python -X utf8 test_delta_bot.py      # exit 0 = all green; writes testnet_test_log.txt
```

The harness drives every command and the confirmation gate against the **testnet**
base URL using injected fake Delta + Telegram transports (no live keys needed),
and records a readable transcript to `testnet_test_log.txt` proving no order
reaches the exchange without an explicit confirm press.

## Layout

```
delta-telegram-bot/
├── delta_telegram_bot.py   # the bot
├── test_delta_bot.py       # offline testnet test harness
├── testnet_test_log.txt    # latest test transcript
├── FEATURE_GUIDE.md        # design notes + deviations from the spec
├── .env.example            # environment template
├── requirements.txt
└── README.md
```

## License

MIT. See the repo root LICENSE if present.
