# FEATURE_GUIDE.md — Delta Exchange Telegram Trading Bot

This guide documents the intended implementation approach for the nine features
in `delta_telegram_bot.py`. (It is written alongside the code: originally this
file was referenced by the task but absent from the repo, so it was authored as
the spec the implementation follows. Deviations from the literal task text are
called out in **⚑ Deviation** notes.)

---

## The one rule that governs everything

> Every code path that can place, modify, or close a real order must go through
> the same inline **✅ Confirm / ❌ Cancel** gate — `on_confirm()` — and never
> fire an order directly.

How the code enforces this *structurally*, not by convention:

- `place_trade()` (entry order) and `close_position()` (exit order) are the only
  functions that touch `DeltaClient.create_order(...)` for live order submission.
- They are called **only** from:
  - `on_confirm()` — the guided / quick / webhook confirmation gate; and
  - the `close:` / `panic:` callback branches in `handle_callback()`, which are
    themselves reached only after a `✅ Confirm …` button press on a card the bot
    sent.
- No command handler, the quick-trade parser (`parse_quick_trade`), the webhook
  (`webhook_trade_from_payload`), or the position monitor ever submits an order.
  They only build a *trade dict* and hand it to `show_confirmation()`.

So "add a feature" always means "produce a trade dict and route it to the shared
confirmation step", never "call the exchange directly."

---

## Shared data shape — the *trade dict*

Built by `new_trade_dict()`, consumed by `build_entry_order()` / `place_trade()`:

```python
{
  "symbol": "BTCUSD", "side": "buy" | "sell",
  "market": bool, "entry": float | None,          # limit price when market=False
  "sizing": "fixed" | "risk", "qty": int, "risk_amount": float | None,
  "sl_type": "fixed" | "trailing", "sl_price": float | None, "trail_amount": float | None,
  "tp_mode": "single" | "scale" | "none", "tp_price": float | None,
  "tp_legs": [ {"price","pct","qty","filled",...} ] | None,
  "leverage": int, "source": "guided" | "quick" | "webhook",
}
```

Because every input path fills this same dict and then calls `show_confirmation`,
the guided flow, the one-line command, and the webhook are provably the *same*
code path after the point of parsing.

---

## Feature 1 — Testnet mode toggle

- `USE_TESTNET` env flag selects `DELTA_BASE_URL`:
  - `true` → `https://cdn-ind.testnet.deltaex.org`
  - `false` → `https://api.india.delta.exchange`
- `DELTA_BASE_URL` (if set) overrides both, for power users / custom endpoints.
- `NETWORK_LABEL` (`TESTNET`/`LIVE`) is derived from the chosen URL and surfaced
  in `/start` ("Network: 🧪 TESTNET … API base: …"), in every confirmation card
  header, and in the journal rows.
- **Default is testnet.** `_env_bool("USE_TESTNET", default=True)` — a trading
  bot should fail safe toward "no real money".
- `main()` refuses to run against LIVE unless `ACK_LIVE=1` or the operator types
  `LIVE` at the terminal.

## Feature 2 — Risk-based position sizing

- Guided flow offers `Fixed quantity` vs `Risk-based` at the `sizing` step.
- Risk branch asks for **risk amount first**, then SL, then computes quantity from
  entry/SL once both are known (`handle_guided_text`, `sl_price` step).
- Core helper `compute_risk_qty()`:

  ```python
  contracts = risk_amount / (abs(entry - sl) * contracting_price)
  ```
  rounded **down** to the product's size increment (`round_to_increment`).

- **⚑ Deviation:** the task text gives `quantity = risk_amount / abs(entry - sl)`.
  That is correct only if PnL per contract per unit-price is exactly 1. On Delta's
  linear USDT contracts the loss per contract is
  `abs(entry - sl) × contracting_price`, so we divide by `contracting_price`
  (fetched from the product, falling back to `1.0`). This makes the *risk in
  money* land on `risk_amount`, which is the intent behind the literal formula.
- The computed qty is shown in the confirmation card (`Qty: N contracts`,
  `Sizing: risk`) **before** the user confirms.

## Feature 3 — Kill-switch `/panic` and `/resume`

- `/panic` stages a panic close: it sends a **confirm card** listing every symbol
  in `state["positions"]`. On `✅ Confirm PANIC` the handler sets
  `trading_halted=True` **before** closing anything (so no new order can slip in
  mid-loop), then market-closes each tracked position via the same
  `close_position()`.
- While `trading_halted`, `on_confirm()` returns "⛔ Trading is halted" and pops
  the pending token — no order is placed. The `close:` callback is also gated.
- `/resume` clears the flag.
- `/start` shows the current trading state (active vs halted).

## Feature 4 — Trailing stop-loss

- Guided flow offers `Fixed SL` vs `Trailing SL`.
- Trailing asks for a **trail amount** (not a price).
- `build_entry_order()` emits `stop_order_type="trailing_stop_loss_order"` with
  the trail value (`trailing_stop_loss_trail_value`) instead of a fixed
  `stop_loss_price`, matching the task's requested field naming.

## Feature 5 — Partial TP / scale-out

- Guided flow: `Single TP` / `Scale-out (2 legs)` / `No TP`.
- Scale-out captures leg-1 price and the **% split**, derives leg-2 qty as the
  remainder (rounded to the size increment), and stores both legs under
  `tp_legs`.
- `build_entry_order()` disables the bracket TP when scaling out; `place_trade()`
  submits each leg as its own reduce-only limit TP order
  (`build_tp_leg_order()`), stamping `order_id` and `filled=False`.
- Fills are detected by **polling** `get_positions()`: `monitor_loop()` →
  `monitor_positions_once()` compares exchange position size against the tracked
  size, marks legs filled proportionally and journals `partial_tp_filled`, and
  journals a `close` + drops the position when the size reaches zero.

## Feature 6 — One-line quick trade

- `parse_quick_trade()` reads
  `/trade SYMBOL side qty entry sl=X tp=Y` (also `trail=`, `lev=`, `entry=market`),
  returns the **same trade dict** the guided flow produces, and calls
  `show_confirmation()` → the **same** confirm step → the **same** `on_confirm()`.
- No separate execution path exists, by construction.

## Feature 7 — Trade journal

- `journal_append()` writes `trade_journal.csv` with columns
  `timestamp,event,symbol,side,qty,entry,sl,tp,network,note`.
- `open` on entry, `close` on manual/panic/exchange-detected close,
  `partial_tp_filled` on scale-out leg fills.
- `/journal` sends the CSV back via Telegram (`sendDocument`).
- Writes are best-effort and never raise into the trading path.

## Feature 8 — TradingView webhook intake

- `start_webhook_server()` runs a tiny Flask app in a daemon thread with
  `POST /webhook` (and `/health`).
- `webhook_trade_from_payload()` maps an alert JSON
  (`{symbol, action, qty, price, sl|trail, tp}`) into a trade dict with
  `source="webhook"`.
- The endpoint calls **`show_confirmation()` only** — it forwards a ✅/❌ card to
  the owner and returns `202`. It never calls `create_order`. An optional
  `WEBHOOK_SECRET` gates the endpoint.
- **⚑ Note:** Flask is imported lazily so the core bot runs (and `py_compile`s)
  without Flask installed; enabling `WEBHOOK_ENABLED=true` without it just logs
  and skips the webhook. Add `flask` to requirements to use it.

## Feature 9 — Retry / backoff

- `DeltaClient._request()` wraps every call: exponential backoff `5→10→20→40→60s`
  (capped) on network errors and HTTP `429`/`5xx`, honouring `Retry-After` when
  larger (`_backoff_delay`, `_parse_retry_after`).
- **Duplicate-order safety:** order-creating POSTs are marked non-idempotent and
  carry a client-generated `client_order_id` (Delta dedupes on it). A network
  error or `5xx` on a non-idempotent POST raises `DeltaOrderUncertain` instead of
  blind retry. `create_order()` then reconciles by looking the order up by
  `client_order_id`; if still unknown, `on_confirm()` tells the operator to check
  `/positions` rather than re-placing — so a retry can never double-submit.

---

## Testing approach (see `test_delta_bot.py`)

The acceptance asks for each command exercised against **testnet, not live**. Real
end-to-end runs need Delta testnet API keys and interactive Telegram taps, which
aren't available here. The harness therefore injects a **fake transport** for both
the Delta HTTP layer and the Telegram API, configures the testnet base URL, and
drives every command + the confirm gate deterministically, asserting:

- testnet base URL is actually used (never the live host);
- no order reaches the exchange without a `cfm:`/`close:`/`panic:` confirm press;
- risk sizing, trailing SL, scale-out legs, journal rows, quick-trade parsing,
  webhook confirm-only behaviour, `/panic`→`on_confirm` refusal, and backoff all
  behave as specified.

Output is captured to `testnet_test_log.txt`.
