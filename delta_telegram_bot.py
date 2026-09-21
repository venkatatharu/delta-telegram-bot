"""
Delta Exchange India — Telegram Trading Bot
===========================================

An *interactive, human-in-the-loop* trading bot. There is NO automatic signal
generation and NO auto-execution anywhere in this file. Every code path that can
place, modify, or close a real order routes through a single inline
"✅ Confirm / ❌ Cancel" gate. That rule is enforced structurally: the only two
functions that submit orders are `place_trade()` (called only from `on_confirm()`)
and `close_position()` (called only from the `close:` / `panic:` confirm-button
callbacks). No command handler, the quick-trade parser, the position monitor, or
the webhook submits an order — they only build a trade dict and hand it to
`show_confirmation()`.

Commands
--------
  /start                Network + capability banner
  /help                 Command reference
  /trade                Guided flow (symbol -> side -> sizing -> SL -> TP -> confirm)
  /trade SYMBOL side qty entry sl=X tp=Y   One-line quick trade -> same confirm step
  /positions            Open positions tracked on the exchange
  /balance              Wallet balances
  /pnl                  Realised / unrealised P&L summary
  /close SYMBOL         Close a tracked position (via confirm)
  /panic                Close ALL tracked positions + halt new orders (via confirm)
  /resume               Clear the trading-halted flag
  /journal              Send trade_journal.csv back to Telegram

Environment (see .env.example / FEATURE_GUIDE.md)
-------------------------------------------------
  DELTA_API_KEY, DELTA_API_SECRET
  TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID
  USE_TESTNET            true -> cdn-ind.testnet.deltaex.org ; false -> live
  MAX_DAILY_LOSS_USDT    daily-loss circuit breaker
  WEBHOOK_ENABLED, WEBHOOK_HOST, WEBHOOK_PORT   TradingView intake (opt-in)

Run:
    python -X utf8 delta_telegram_bot.py
"""

from __future__ import annotations

import os
import sys
import csv
import json
import time
import hmac
import uuid
import hashlib
import logging
import threading
import traceback
from pathlib import Path
from datetime import datetime, date, timezone

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv is optional at runtime
    pass


# ─────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("delta_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("DeltaTGBot")


# ─────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────
def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


DELTA_API_KEY     = os.getenv("DELTA_API_KEY", "").strip()
DELTA_API_SECRET  = os.getenv("DELTA_API_SECRET", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_OWNER_ID  = os.getenv("TELEGRAM_OWNER_ID", "").strip()

# Feature 1 — Testnet toggle. The two URLs below are mandated by the task spec.
TESTNET_BASE_URL = "https://cdn-ind.testnet.deltaex.org"
LIVE_BASE_URL    = "https://api.india.delta.exchange"
# Default to testnet when unset — safest possible default for a trading bot.
USE_TESTNET   = _env_bool("USE_TESTNET", default=True)
DELTA_BASE_URL = os.getenv("DELTA_BASE_URL", "").strip() or (
    TESTNET_BASE_URL if USE_TESTNET else LIVE_BASE_URL
)
NETWORK_LABEL = "TESTNET" if DELTA_BASE_URL == TESTNET_BASE_URL else "LIVE"

MAX_DAILY_LOSS_USDT = float(os.getenv("MAX_DAILY_LOSS_USDT", "100"))
POSITION_POLL_SECONDS = int(os.getenv("POSITION_POLL_SECONDS", "20"))

# Margin guard: block any new trade whose estimated initial margin exceeds the
# available USDT in the FNO (futures) wallet. Set ENFORCE_MARGIN_LIMIT=false to
# disable; lower MARGIN_USAGE_LIMIT (e.g. 0.9) to keep a safety buffer.
ENFORCE_MARGIN_LIMIT = _env_bool("ENFORCE_MARGIN_LIMIT", default=True)
MARGIN_USAGE_LIMIT = float(os.getenv("MARGIN_USAGE_LIMIT", "1.0"))

STATE_FILE   = Path(os.getenv("DELTA_STATE_FILE", "delta_bot_state.json"))
JOURNAL_FILE = Path(os.getenv("DELTA_JOURNAL_FILE", "trade_journal.csv"))

# Webhook (Feature 8)
WEBHOOK_ENABLED = _env_bool("WEBHOOK_ENABLED", default=False)
WEBHOOK_HOST    = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT    = int(os.getenv("WEBHOOK_PORT", "8080"))


# ─────────────────────────────────────────────────────────────────────────
# STATE  (persisted to delta_bot_state.json)
# ─────────────────────────────────────────────────────────────────────────
DEFAULT_STATE = {
    "positions": {},       # symbol -> {side, entry, qty, sl..., tp_legs[...], ...}
    "stats": {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "realized_pnl": 0.0,
        "daily_loss": 0.0,
        "last_reset_date": str(date.today()),
    },
    "trading_halted": False,   # Feature 3 — kill-switch flag
    "pending": {},             # confirm_token -> trade dict awaiting a button press
}

_state_lock = threading.Lock()
state: dict = {}


def load_state() -> dict:
    """Load state from disk, back-filling any missing top-level keys."""
    loaded = json.loads(json.dumps(DEFAULT_STATE))  # deep copy
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            for key, default in DEFAULT_STATE.items():
                loaded[key] = saved.get(key, default)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("State load failed (%s) — using defaults", exc)
    return loaded


def save_state() -> None:
    with _state_lock:
        try:
            tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, STATE_FILE)
        except Exception as exc:  # pragma: no cover - defensive
            log.error("State save failed: %s", exc)


def reset_daily_if_needed() -> None:
    stats = state.setdefault("stats", DEFAULT_STATE["stats"])
    if stats.get("last_reset_date") != str(date.today()):
        log.info("New trading day — resetting daily loss counter")
        stats["daily_loss"] = 0.0
        stats["last_reset_date"] = str(date.today())
        save_state()


def circuit_breaker_active() -> bool:
    """True when the daily-loss limit has been hit (blocks new orders)."""
    return state["stats"].get("daily_loss", 0.0) >= MAX_DAILY_LOSS_USDT


# ─────────────────────────────────────────────────────────────────────────
# TRADE JOURNAL  (Feature 7)
# ─────────────────────────────────────────────────────────────────────────
JOURNAL_FIELDS = [
    "timestamp", "event", "symbol", "side", "qty",
    "entry", "sl", "tp", "pnl", "network", "note",
]


def journal_append(event: str, trade: dict | None = None, *, symbol: str = "",
                   side: str = "", qty="", entry="", sl="", tp="", pnl="", note="") -> None:
    """Append one row to trade_journal.csv. Never raises into the caller."""
    trade = trade or {}
    sl = sl if sl != "" else trade.get("sl_price") or (
        f"trail:{trade.get('trail_amount')}" if trade.get("sl_type") == "trailing" else "")
    tp = tp if tp != "" else trade.get("tp_price") or ""
    if not tp and trade.get("tp_legs"):
        tp = ";".join(str(leg.get("price")) for leg in trade["tp_legs"])
    if pnl == "":
        pnl = trade.get("pnl", "")
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "symbol": symbol or trade.get("symbol", ""),
        "side": side or trade.get("side", ""),
        "qty": qty if qty != "" else trade.get("qty", ""),
        "entry": entry if entry != "" else trade.get("entry", ""),
        "sl": sl, "tp": tp, "pnl": pnl, "network": NETWORK_LABEL, "note": note,
    }
    try:
        new_file = not JOURNAL_FILE.exists()
        with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:  # pragma: no cover - defensive
        log.error("Journal write failed: %s", exc)


# ─────────────────────────────────────────────────────────────────────────
# NUMBER HELPERS
# ─────────────────────────────────────────────────────────────────────────
def round_to_tick(price: float, tick: float) -> float:
    if tick and tick > 0:
        return round(round(price / tick) * tick, 12)
    return round(price, 6)


def round_to_increment(qty: float, size_increment: float) -> int:
    """Round a contract size to the product's size increment, floor at 0."""
    if size_increment and size_increment > 0:
        qty = int(round(qty / size_increment) * size_increment)
    else:
        qty = int(round(qty))
    return max(qty, 0)


# ─────────────────────────────────────────────────────────────────────────
# RETRY / BACKOFF  (Feature 9)
# ─────────────────────────────────────────────────────────────────────────
class DeltaOrderUncertain(Exception):
    """Raised when a POST /orders may or may not have landed and must NOT be retried."""
    def __init__(self, message: str, order_client_id: str | None):
        super().__init__(message)
        self.order_client_id = order_client_id


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff (5,10,20,40,60s cap) but honour Retry-After if larger."""
    delay = min(5 * (2 ** (attempt - 1)), 60)
    if retry_after is not None and retry_after > delay:
        delay = min(retry_after, 120)
    return delay


# ─────────────────────────────────────────────────────────────────────────
# DELTA HTTP CLIENT
# ─────────────────────────────────────────────────────────────────────────
class DeltaClient:
    """
    Minimal Delta Exchange India REST client.

    Signing scheme (matches Delta's documented auth): signature = HMAC_SHA256(
    secret, METHOD + timestamp + path + body). Public endpoints are unsigned.

    Feature 9 — `_request` wraps every call with exponential backoff on network
    errors and HTTP 429/5xx, respecting `Retry-After`. Idempotent GETs always
    retry. Order-creating POSTs carry a client-generated `client_order_id` (Delta
    dedupes on it) so a retry after an ambiguous failure cannot double-submit;
    if the response body itself is ambiguous we raise DeltaOrderUncertain rather
    than blindly retry a non-idempotent POST.
    """

    MAX_RETRIES = 5

    def __init__(self, base_url: str, api_key: str, api_secret: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.session = requests.Session()
        self._products: dict[str, dict] = {}
        self._products_loaded = False

    # -- signing + low level request with backoff -------------------------
    def _sign(self, method: str, timestamp: str, path: str, body: str) -> str:
        msg = f"{method}{timestamp}{path}{body}"
        return hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _request(self, method: str, path: str, params: dict | None = None,
                 *, auth: bool = False, idempotent: bool = True,
                 client_order_id: str | None = None):
        method = method.upper()
        attempt = 0
        while True:
            attempt += 1
            if method == "GET" and params:
                query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                full_path = f"{path}?{query}"
                body = ""
                send_params = None
            else:
                full_path = path
                body = json.dumps(params) if params else ""
                send_params = None

            timestamp = str(int(time.time()))
            headers = {
                "User-Agent": "delta-telegram-bot/1.0",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            if auth:
                headers.update({
                    "api-key": self.api_key,
                    "timestamp": timestamp,
                    "signature": self._sign(method, timestamp, full_path, body),
                })

            url = self.base_url + full_path
            try:
                if method == "GET":
                    resp = self.session.get(url, params=send_params, headers=headers, timeout=15)
                else:
                    resp = self.session.request(method, url, data=body, headers=headers, timeout=15)
            except requests.RequestException as exc:
                # Network-level failure. Safe to retry only for idempotent calls.
                if idempotent and attempt < self.MAX_RETRIES:
                    delay = _backoff_delay(attempt, None)
                    log.warning("Network error (%s) attempt %d/%d — retrying in %ss",
                                exc, attempt, self.MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                if not idempotent:
                    # We don't know whether the order landed. Do NOT retry blindly.
                    raise DeltaOrderUncertain(
                        f"Network error on non-idempotent POST {path}: {exc}",
                        client_order_id,
                    ) from exc
                raise

            # -- HTTP status handling ------------------------------------
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = self._parse_retry_after(resp)
                if attempt < self.MAX_RETRIES and (idempotent or resp.status_code == 429):
                    delay = _backoff_delay(attempt, retry_after)
                    log.warning("HTTP %s on %s attempt %d/%d — retrying in %ss",
                                resp.status_code, path, attempt, self.MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                if not idempotent and resp.status_code >= 500:
                    raise DeltaOrderUncertain(
                        f"HTTP {resp.status_code} on non-idempotent POST {path}",
                        client_order_id,
                    )
                resp.raise_for_status()

            try:
                data = resp.json()
            except ValueError:
                raise RuntimeError(f"Non-JSON response from {path}: {resp.text[:200]}")

            if isinstance(data, dict) and data.get("success") is False:
                raise RuntimeError(f"Delta error [{path}]: {data.get('error') or data}")
            resp.raise_for_status()
            return data

    @staticmethod
    def _parse_retry_after(resp) -> float | None:
        raw = resp.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # -- reference data ---------------------------------------------------
    def get_product(self, symbol: str) -> dict:
        """Fetch & cache a product's contract settings (tick size, contracting price...)."""
        symbol = symbol.upper()
        if not self._products_loaded:
            data = self._request("GET", "/v2/products")
            for prod in data.get("result", []):
                self._products[str(prod.get("symbol", "")).upper()] = prod
            self._products_loaded = True
        if symbol not in self._products:
            # targeted fallback
            data = self._request("GET", f"/v2/products/{symbol}")
            self._products[symbol] = data.get("result", {})
        return self._products.get(symbol, {})

    def product_size_increment(self, symbol: str) -> float:
        prod = self.get_product(symbol)
        return float(prod.get("size_delta") or prod.get("min_size") or 1)

    def product_tick_size(self, symbol: str) -> float:
        prod = self.get_product(symbol)
        return float(prod.get("tick_size") or 0.01)

    def product_contracting_price(self, symbol: str) -> float:
        prod = self.get_product(symbol)
        # Linear USDT contracts: PnL per contract per unit price move.
        return float(prod.get("contracting_price") or prod.get("contract_value") or 1.0)

    def mark_price(self, symbol: str) -> float | None:
        # v2 products payload has no mark_price; it lives on the ticker.
        data = self._request("GET", f"/v2/tickers/{symbol.upper()}")
        t = data.get("result", {}) or {}
        mp = t.get("mark_price") or t.get("spot_price") or t.get("close")
        return float(mp) if mp else None

    # -- account ----------------------------------------------------------
    def get_balance(self) -> dict:
        # Full payload: {"result": [per-asset balances], "meta": {"net_equity": ...}}
        return self._request("GET", "/v2/wallet/balances", auth=True)

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        params = None
        if symbol:
            params = {"product_ids": self.get_product(symbol).get("id")}
        data = self._request("GET", "/v2/positions", params=params, auth=True)
        return data.get("result", [])

    def get_order_history(self, symbol: str) -> list[dict]:
        pid = self.get_product(symbol).get("id")
        data = self._request("GET", "/v2/orders/history",
                             params={"product_id": pid, "limit": 50}, auth=True)
        return data.get("result", [])

    # -- orders (order creation is NON-idempotent at the network layer) ---
    def create_order(self, payload: dict) -> dict:
        """
        Submit an order. Injects a client_order_id so retries can't double-submit.
        Raises DeltaOrderUncertain if the outcome is ambiguous (caller must not
        treat that as "definitely failed").
        """
        client_order_id = payload.get("client_order_id") or uuid.uuid4().hex
        payload = {**payload, "client_order_id": client_order_id}
        try:
            data = self._request("POST", "/v2/orders", payload,
                                 auth=True, idempotent=False,
                                 client_order_id=client_order_id)
        except DeltaOrderUncertain:
            # Try once to reconcile via the open-order book by client_order_id.
            existing = self._find_order_by_client_id(client_order_id)
            if existing:
                log.info("Order %s confirmed present after uncertain response", client_order_id)
                return existing
            raise
        return data.get("result", data)

    def _find_order_by_client_id(self, client_order_id: str) -> dict | None:
        try:
            data = self._request("GET", "/v2/orders", auth=True)
        except Exception:
            return None
        for o in data.get("result", []):
            if o.get("client_order_id") == client_order_id:
                return o
        return None

    def close_position_market(self, symbol: str, size: int, close_side: str) -> dict:
        """Market order that flips a position flat. Called only via confirm flow."""
        prod = self.get_product(symbol)
        payload = {
            "product_id": prod.get("id"),
            "product_symbol": symbol,
            "size": int(size),
            "side": close_side,
            "order_type": "market_order",
            "reduce_only": True,
            "closing_positions": [prod.get("id")],
            "client_order_id": uuid.uuid4().hex,
        }
        return self.create_order(payload)

    # -- bracket payload builders ------------------------------------------
    def build_entry_order(self, trade: dict) -> dict:
        """
        Build the entry + bracket (SL/TP) order payload for a validated trade dict.
        Supports Fixed vs Trailing SL (Feature 4) and single vs scale-out TP
        (Feature 5). Trailing SL uses stop_order_type='trailing_stop_loss_order'
        with a trail_amount, per the task spec.
        """
        symbol = trade["symbol"]
        tick = self.product_tick_size(symbol)
        order_type = "limit_order" if trade.get("entry") and not trade.get("market") else "market_order"

        payload = {
            "product_symbol": symbol,
            "size": int(trade["qty"]),
            "side": trade["side"],           # "buy" | "sell"
            "order_type": order_type,
            "leverage": trade.get("leverage", 10),
            "reduce_only": False,
            "enable_bracket": True,
            "client_order_id": uuid.uuid4().hex,
        }
        if order_type == "limit_order":
            payload["limit_price"] = round_to_tick(float(trade["entry"]), tick)

        # ── Stop loss: fixed or trailing ──────────────────────────────
        if trade.get("sl_type") == "trailing":
            payload["stop_order_type"] = "trailing_stop_loss_order"
            payload["enable_trailing_stop_loss"] = True
            payload["trailing_stop_loss_trail_value"] = float(trade["trail_amount"])
        else:
            payload["enable_stop_loss"] = True
            payload["stop_loss_price"] = round_to_tick(float(trade["sl_price"]), tick)

        # ── Take profit: single TP or scale-out legs ──────────────────
        if trade.get("tp_mode") == "scale" and trade.get("tp_legs"):
            # Legs are placed as separate reduce-only limit orders (see place_trade)
            payload["enable_take_profit"] = False
        elif trade.get("tp_price"):
            payload["enable_take_profit"] = True
            payload["take_profit_price"] = round_to_tick(float(trade["tp_price"]), tick)
        else:
            payload["enable_take_profit"] = False

        return payload

    def build_tp_leg_order(self, trade: dict, leg: dict) -> dict:
        """A single reduce-only take-profit limit order for one scale-out leg."""
        symbol = trade["symbol"]
        tick = self.product_tick_size(symbol)
        close_side = "sell" if trade["side"] == "buy" else "buy"
        return {
            "product_symbol": symbol,
            "size": int(leg["qty"]),
            "side": close_side,
            "order_type": "limit_order",
            "limit_price": round_to_tick(float(leg["price"]), tick),
            "reduce_only": True,
            "client_order_id": uuid.uuid4().hex,
        }


# ─────────────────────────────────────────────────────────────────────────
# TELEGRAM CLIENT (thin, requests-based; injectable for offline tests)
# ─────────────────────────────────────────────────────────────────────────
class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"
        self._offset = 0
        self.session = requests.Session()

    def _api(self, method: str, payload: dict, files=None, req_timeout: int = 20):
        if not self.token:
            log.info("[Telegram disabled] %s %s", method, str(payload)[:120])
            return {"ok": False, "disabled": True}
        try:
            if files:
                resp = self.session.post(f"{self.base}/{method}", data=payload,
                                         files=files, timeout=req_timeout)
            else:
                resp = self.session.post(f"{self.base}/{method}", json=payload,
                                         timeout=req_timeout)
            return resp.json()
        except Exception as exc:  # pragma: no cover - network
            log.error("Telegram %s failed: %s", method, exc)
            return {"ok": False, "error": str(exc)}

    def send_message(self, chat_id, text: str, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self._api("sendMessage", payload)

    def edit_message(self, chat_id, message_id, text: str, keyboard=None):
        payload = {"chat_id": chat_id, "message_id": message_id,
                   "text": text, "parse_mode": "Markdown"}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return self._api("editMessageText", payload)

    def send_document(self, chat_id, path: Path, caption: str = ""):
        with open(path, "rb") as f:
            files = {"document": (path.name, f, "text/csv")}
            return self._api("sendDocument",
                             {"chat_id": chat_id, "caption": caption}, files=files)

    def answer_callback(self, callback_id: str, text: str = ""):
        return self._api("answerCallbackQuery",
                         {"callback_query_id": callback_id, "text": text})

    def get_updates(self, timeout: int = 25) -> list[dict]:
        if not self.token:
            time.sleep(2)
            return []
        payload = {"offset": self._offset, "timeout": timeout,
                   "allowed_updates": ["message", "callback_query"]}
        # Read timeout must exceed the long-poll hold window or idle polls error.
        res = self._api("getUpdates", payload, req_timeout=timeout + 10)
        updates = res.get("result", []) if res.get("ok") else []
        for upd in updates:
            self._offset = max(self._offset, upd["update_id"] + 1)
        return updates


# Inline-keyboard helpers --------------------------------------------------
def _btn(text: str, cb: str) -> dict:
    return {"text": text, "callback_data": cb}


def confirm_keyboard(token: str) -> list[list[dict]]:
    return [[_btn("✅ Confirm", f"cfm:{token}"), _btn("❌ Cancel", f"cnl:{token}")]]


def reply_keyboard(options: list[str]) -> list[list[dict]]:
    return [[{"text": opt, "callback_data": opt} for opt in options]]


# ─────────────────────────────────────────────────────────────────────────
# RISK-BASED POSITION SIZING  (Feature 2)
# ─────────────────────────────────────────────────────────────────────────
def compute_risk_qty_from_distance(delta: DeltaClient, risk_amount: float,
                                   distance: float, symbol: str) -> int:
    """contracts = risk_amount / (price_distance * contracting_price), floored to the
    product's size increment so realised risk never exceeds the intended amount."""
    if distance is None or distance <= 0:
        raise ValueError("price distance to stop must be greater than zero")
    contracting = delta.product_contracting_price(symbol) or 1.0
    raw = float(risk_amount) / (distance * contracting)
    inc = delta.product_size_increment(symbol)
    return round_to_increment(raw, inc)


def compute_risk_qty(delta: DeltaClient, risk_amount: float, entry: float,
                     sl: float, symbol: str) -> int:
    """
    Fixed-SL variant. `abs(entry - sl)` is the per-unit price distance; multiplying
    by the product's contracting price converts it to the loss per contract, matching
    how Delta computes linear PnL (see FEATURE_GUIDE.md ⚑ Deviation for Feature 2).
    """
    return compute_risk_qty_from_distance(delta, risk_amount,
                                          abs(float(entry) - float(sl)), symbol)


# ─────────────────────────────────────────────────────────────────────────
# GUIDED-FLOW CONVERSATION STATE (in-memory, per chat)
# ─────────────────────────────────────────────────────────────────────────
# drafts[chat_id] = {"step": "...", "trade": {...}, "prompt_msg_id": int}
drafts: dict[str, dict] = {}

TRADE_STEPS = [
    "symbol", "side", "sizing",
    "qty", "risk_amount", "sl_mode",
    "sl_price", "trail_amount", "tp_mode",
    "tp_price", "tp_leg1_price", "tp_leg1_pct",
    "tp_leg2_price", "confirm",
]


def new_trade_dict() -> dict:
    return {
        "symbol": None, "side": None, "market": True, "entry": None,
        "sizing": "fixed", "qty": None, "risk_amount": None,
        "sl_type": "fixed", "sl_price": None, "trail_amount": None,
        "tp_mode": "single", "tp_price": None, "tp_legs": None,
        "leverage": 10, "source": "guided",
    }


# ─────────────────────────────────────────────────────────────────────────
# THE CONFIRMATION GATE + ORDER EXECUTION
#
#   on_confirm(token)  -> the sole caller of place_trade(). close_position() is
#   reached only via the close: / panic: confirm-button callbacks. Every entry,
#   quick-trade and webhook path stores a pending trade and shows the ✅/❌ card;
#   nothing executes until a Confirm press arrives here.
# ─────────────────────────────────────────────────────────────────────────
def build_confirmation_text(trade: dict, margin_line: str | None = None) -> str:
    net = f"⚠️ {NETWORK_LABEL}" + (" (testnet)" if NETWORK_LABEL == "TESTNET" else " (REAL MONEY)")
    lines = [f"🧾 *Confirm order* — `{trade['symbol']}`",
             f"Network: *{net}*",
             f"Side: `{trade['side']}`  Qty: `{trade['qty']}` contracts",
             f"Sizing: `{trade['sizing']}`  Entry: " +
             (f"`{trade['entry']}` (limit)" if trade.get("entry") and not trade.get("market")
              else "market")]
    if trade.get("sl_type") == "trailing":
        lines.append(f"SL: trailing `{trade['trail_amount']}`")
    elif trade.get("sl_price"):
        lines.append(f"SL: fixed `{trade['sl_price']}`")
    if trade.get("tp_mode") == "scale" and trade.get("tp_legs"):
        legs = ", ".join(f"`{l['price']}`×{l['pct']}%" for l in trade["tp_legs"])
        lines.append(f"TP: scale-out {legs}")
    elif trade.get("tp_price"):
        lines.append(f"TP: `{trade['tp_price']}`")
    lines.append(f"Source: `{trade.get('source','guided')}`")
    if margin_line:
        lines.append(margin_line)
    return "\n".join(lines)


def store_pending(trade: dict) -> str:
    token = uuid.uuid4().hex[:12]
    with _state_lock:
        state.setdefault("pending", {})[token] = trade
    save_state()
    return token


def show_confirmation(delta: DeltaClient, tg: TelegramClient, chat_id, trade: dict,
                      message_id=None) -> None:
    token = store_pending(trade)
    text = build_confirmation_text(trade, margin_summary(delta, trade))
    if message_id is not None:
        tg.edit_message(chat_id, message_id, text, confirm_keyboard(token))
    else:
        tg.send_message(chat_id, text, confirm_keyboard(token))


def place_trade(delta: DeltaClient, trade: dict) -> dict:
    """
    Execute a fully-formed trade dict. Handles fixed/trailing SL and single vs
    scale-out TP (submitting one extra reduce-only TP order per leg). Records an
    `open` journal event and mirrors the position into state["positions"].
    """
    entry_payload = delta.build_entry_order(trade)
    order = delta.create_order(entry_payload)

    if trade.get("tp_mode") == "scale" and trade.get("tp_legs"):
        for leg in trade["tp_legs"]:
            leg_order = delta.create_order(delta.build_tp_leg_order(trade, leg))
            leg["order_id"] = leg_order.get("id")
            leg["filled"] = False

    # Mirror into local state for /positions, /panic and TP-leg tracking.
    state["positions"][trade["symbol"]] = {
        "side": trade["side"],
        "qty": trade["qty"],
        "entry": trade.get("entry") or delta.mark_price(trade["symbol"]),
        "sl_type": trade.get("sl_type"),
        "sl_price": trade.get("sl_price"),
        "trail_amount": trade.get("trail_amount"),
        "tp_mode": trade.get("tp_mode"),
        "tp_price": trade.get("tp_price"),
        "tp_legs": trade.get("tp_legs"),
        "realized_qty": 0,
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "order_id": order.get("id"),
    }
    save_state()
    journal_append("open", trade, note=f"source={trade.get('source','guided')}")
    state["stats"]["total_trades"] = state["stats"].get("total_trades", 0) + 1
    save_state()
    return order


def _extract_fill_price(order) -> float | None:
    """Best-effort exit fill price from a Delta order / close response."""
    if not isinstance(order, dict):
        return None
    for key in ("average_fill_price", "fill_price", "price"):
        val = order.get(key)
        if val not in (None, "", 0, "0"):
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    fills = order.get("fills")
    if isinstance(fills, list) and fills and isinstance(fills[0], dict):
        for key in ("fill_price", "price"):
            val = fills[0].get(key)
            if val not in (None, "", 0, "0"):
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    return None


def record_realized_pnl(delta: DeltaClient, symbol: str, side: str, qty,
                        entry_price, exit_price,
                        count_trade: bool = True) -> float | None:
    """Book realized PnL and update the daily-loss stats.

    Convention matches compute_risk_qty_from_distance: PnL per contract per unit
    price move is the product's contracting price. realized_pnl is always updated
    and a negative pnl always accrues to daily_loss (so scale-out losses still trip
    the circuit breaker). The wins/losses tally only moves when count_trade is True;
    partial scale-out legs pass count_trade=False so they don't inflate it. Returns
    the pnl, or None when a price was unavailable (in which case stats are untouched).
    """
    if entry_price is None or exit_price is None:
        return None
    contracting = delta.product_contracting_price(symbol) or 1.0
    qty = float(qty)
    entry_price = float(entry_price)
    exit_price = float(exit_price)
    if str(side).lower() == "buy":
        pnl = (exit_price - entry_price) * qty * contracting
    else:
        pnl = (entry_price - exit_price) * qty * contracting

    stats = state.setdefault("stats", DEFAULT_STATE["stats"])
    stats["realized_pnl"] = round(float(stats.get("realized_pnl", 0.0)) + pnl, 6)
    if pnl < 0:
        stats["daily_loss"] = round(float(stats.get("daily_loss", 0.0)) + abs(pnl), 6)
        if count_trade:
            stats["losses"] = int(stats.get("losses", 0)) + 1
    elif count_trade:
        stats["wins"] = int(stats.get("wins", 0)) + 1
    save_state()
    return pnl


def close_position(delta: DeltaClient, symbol: str, reason: str = "manual") -> None:
    """Market-close a tracked position, book realized PnL, and journal it.
    Confirm-gated callers only."""
    pos = state["positions"].get(symbol)
    if not pos:
        raise ValueError(f"No tracked position for {symbol}")
    close_side = "sell" if pos["side"] == "buy" else "buy"
    order = delta.close_position_market(symbol, int(pos["qty"]), close_side)

    # exit price: order fill if present, else the mark price
    exit_price = _extract_fill_price(order)
    if exit_price is None:
        exit_price = delta.mark_price(symbol)

    # Realize PnL only on the residual still-open quantity — any scale-out legs
    # already filled had their PnL booked at each leg's TP price.
    residual = int(pos["qty"]) - int(pos.get("realized_qty", 0) or 0)
    pnl = (record_realized_pnl(delta, symbol, pos["side"], residual,
                               pos.get("entry"), exit_price)
           if residual > 0 else 0.0)
    trade = {"symbol": symbol, "side": pos["side"], "qty": residual,
             "entry": pos.get("entry"), "sl_price": pos.get("sl_price"),
             "tp_price": pos.get("tp_price")}
    journal_append("close", trade, pnl=("" if pnl is None else round(pnl, 4)),
                   note=f"{reason}" + (f" @ exit {exit_price}" if exit_price else "")
                   + (f" residual {residual}/{pos['qty']}" if residual != int(pos["qty"]) else ""))
    state["positions"].pop(symbol, None)
    save_state()


def available_usdt(delta: DeltaClient) -> float | None:
    """Available USDT/USD margin in the FNO (futures) wallet, or None if unknown."""
    try:
        resp = delta.get_balance()
    except Exception as exc:  # pragma: no cover - network
        log.warning("margin guard: balance fetch failed: %s", exc)
        return None
    items = resp.get("result", []) if isinstance(resp, dict) else resp
    for b in items:
        sym = (b.get("asset_symbol") or b.get("currency") or b.get("asset") or "").upper()
        if sym in ("USDT", "USD"):
            try:
                return float(b.get("available_balance", 0))
            except (TypeError, ValueError):
                return None
    return None


def estimate_margin_required(delta: DeltaClient, trade: dict) -> float | None:
    """Estimate the initial margin a trade needs = notional / leverage.

    notional = qty * contracting_price * entry (entry falls back to mark price for
    market orders). leverage is the trade's chosen leverage (default 10) — the same
    value submitted on the order — so the estimate matches what Delta will reserve.
    Returns None if it can't be computed.
    """
    qty = trade.get("qty")
    if not qty:
        return None
    symbol = trade["symbol"]
    entry = trade.get("entry") or delta.mark_price(symbol)
    if not entry:
        return None
    contracting = delta.product_contracting_price(symbol) or 1.0
    notional = float(qty) * contracting * float(entry)
    leverage = trade.get("leverage") or 10
    return notional / float(leverage)


def margin_summary(delta: DeltaClient, trade: dict) -> str | None:
    """One-line margin estimate for the confirmation card (never raises)."""
    try:
        need = estimate_margin_required(delta, trade)
        if need is None:
            return None
        avail = available_usdt(delta)
        if avail is None:
            return f"Margin: ≈ `${need:,.2f}` needed (balance unavailable)"
        ok = need <= avail * MARGIN_USAGE_LIMIT
        return (f"Margin: ≈ `${need:,.2f}` needed / `${avail:,.2f}` available — "
                f"{'✅ fits' if ok else '⛔ over budget'}")
    except Exception:  # pragma: no cover - defensive
        return None


def validate_trade(trade: dict) -> str | None:
    """Pre-flight check so we never POST an order Delta rejects as negativeordersize.
    Returns an error message, or None if the trade is valid."""
    qty = trade.get("qty")
    try:
        qty = int(qty)
    except (TypeError, ValueError):
        return f"Order size must be a whole number of contracts (got {qty!r})."
    if qty <= 0:
        return ("Order size rounded to 0 contracts — your risk amount is too small "
                "for this stop distance, or the quantity is invalid. Increase the "
                "quantity/risk or widen the stop.")
    if trade.get("tp_mode") == "scale" and trade.get("tp_legs"):
        total = 0
        for leg in trade["tp_legs"]:
            lq = int(leg.get("qty", 0) or 0)
            if lq <= 0:
                return "Each take-profit leg must be at least 1 contract."
            total += lq
        if total > qty:
            return f"Scale-out legs total {total} contracts but the position is only {qty}."
    return None


def on_confirm(delta: DeltaClient, tg: TelegramClient, token: str,
               chat_id, message_id) -> str:
    """The single, authoritative execution gate."""
    trade = state.get("pending", {}).get(token)
    if not trade:
        return "This confirmation has expired."

    # Feature 3 — hard refusal while the kill-switch is engaged.
    if state.get("trading_halted"):
        state["pending"].pop(token, None)
        save_state()
        return "⛔ Trading is halted. Use /resume to enable."

    # Circuit breaker — refuse new risk.
    if circuit_breaker_active():
        state["pending"].pop(token, None)
        save_state()
        return (f"🛑 Circuit breaker: daily loss "
                f"${state['stats']['daily_loss']:.2f} ≥ ${MAX_DAILY_LOSS_USDT:.2f}. "
                "Order not placed.")

    # Pre-flight: never send a zero/negative size (Delta -> negativeordersize).
    err = validate_trade(trade)
    if err:
        state["pending"].pop(token, None)
        save_state()
        return "🛑 " + err

    # Margin guard — block trades that exceed available USDT in the FNO wallet.
    if ENFORCE_MARGIN_LIMIT:
        avail = available_usdt(delta)
        need = estimate_margin_required(delta, trade)
        if avail is not None and need is not None and need > avail * MARGIN_USAGE_LIMIT:
            state["pending"].pop(token, None)
            save_state()
            return (f"🛑 Blocked: this trade needs ≈ ${need:,.2f} margin but only "
                    f"${avail:,.2f} USDT is available in your FNO wallet. Reduce "
                    "size/risk, lower leverage, or transfer Spot → Futures.")

    # Consume the token FIRST so a double-press can't double-place.
    state["pending"].pop(token, None)
    save_state()

    try:
        place_trade(delta, trade)
    except DeltaOrderUncertain as exc:
        log.error("Order submission uncertain: %s (client_order_id=%s)",
                  exc, exc.order_client_id)
        return ("⚠️ Order submission is uncertain — check /positions before retrying "
                "so we don't double-submit.")
    except Exception as exc:
        log.error("confirm placement failed: %s\n%s", exc, traceback.format_exc())
        return f"❌ Order failed: {exc}"
    return f"✅ Placed {trade['side']} {trade['qty']} {trade['symbol']}."


def on_cancel(token: str) -> str:
    state.get("pending", {}).pop(token, None)
    save_state()
    return "Cancelled — no order was placed."


# ─────────────────────────────────────────────────────────────────────────
# QUICK TRADE PARSER  (Feature 6)
#
#   /trade SYMBOL side qty entry sl=.. tp=..  → same trade dict as the guided
#   flow → same show_confirmation() step. No separate execution path.
# ─────────────────────────────────────────────────────────────────────────
def parse_quick_trade(text: str) -> dict | None:
    """
    Parse `/trade BTCUSD buy 5 12000 sl=11500 tp=12500`.
    entry may be 'market' for a market order. Returns a trade dict or None if it
    isn't the quick form / is malformed.
    """
    parts = text.split()
    if len(parts) < 5:
        return None
    try:
        symbol = parts[1].upper()
        side = parts[2].lower()
        qty = int(float(parts[3]))
        entry = None if parts[4].lower() == "market" else float(parts[4])
    except (ValueError, IndexError):
        return None
    if side not in {"buy", "sell", "long", "short"}:
        return None
    trade = new_trade_dict()
    trade["source"] = "quick"
    trade["symbol"] = symbol
    trade["side"] = "buy" if side in {"buy", "long"} else "sell"
    trade["qty"] = qty
    trade["sizing"] = "fixed"
    trade["market"] = entry is None
    trade["entry"] = entry
    opts = {}
    for tok in parts[5:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            opts[k.lower().strip("--")] = v
    if "sl" in opts:
        trade["sl_type"] = "fixed"
        trade["sl_price"] = float(opts["sl"])
    if "trail" in opts:
        trade["sl_type"] = "trailing"
        trade["trail_amount"] = float(opts["trail"])
    if "tp" in opts:
        trade["tp_mode"] = "single"
        trade["tp_price"] = float(opts["tp"])
    if "lev" in opts:
        trade["leverage"] = int(float(opts["lev"]))
    return trade


# ─────────────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────
def cmd_start(tg: TelegramClient, chat_id):
    net = "🧪 TESTNET" if NETWORK_LABEL == "TESTNET" else "🔴 LIVE (real money)"
    url = DELTA_BASE_URL
    halted = "⛔ HALTED (/resume to enable)" if state.get("trading_halted") else "🟢 active"
    tg.send_message(chat_id,
        "🤖 *Delta Telegram Trading Bot*\n\n"
        f"Network: *{net}*\nAPI base: `{url}`\n"
        f"Trading: *{halted}*\n\n"
        "Manual-only. Every order needs a ✅ Confirm tap — nothing auto-executes.\n\n"
        "/trade — guided flow  ·  `/trade SYMBOL side qty entry sl=X tp=Y` — quick\n"
        "/positions  /balance  /pnl  /close SYMBOL\n"
        "/panic  /resume  /journal  /help")


def cmd_help(tg: TelegramClient, chat_id):
    tg.send_message(chat_id,
        "*Commands*\n"
        "/trade — guided order flow (fixed/risk sizing, fixed/trailing SL, scale-out TP)\n"
        "/trade SYMBOL side qty entry sl=X tp=Y — one-line quick trade\n"
        "/positions — open positions on the exchange\n"
        "/balance — wallet balances\n"
        "/pnl — realised + unrealised P&L\n"
        "/close SYMBOL — close a tracked position (asks to confirm)\n"
        "/panic — close everything + halt new orders (asks to confirm)\n"
        "/resume — clear the halted flag\n"
        "/journal — download trade_journal.csv\n\n"
        "_No automatic trading. Every order is confirm-gated._")


def cmd_balance(delta: DeltaClient, tg: TelegramClient, chat_id):
    try:
        resp = delta.get_balance()
    except Exception as exc:
        tg.send_message(chat_id, f"⚠️ Balance fetch failed: {exc}")
        return
    balances = resp.get("result", []) if isinstance(resp, dict) else resp
    meta = resp.get("meta", {}) if isinstance(resp, dict) else {}
    lines = [f"💰 *Balances* ({NETWORK_LABEL})"]
    equity = meta.get("net_equity")
    if equity:
        lines.insert(1, f"Net equity: ≈ `{float(equity):,.2f}` USDT (all assets)")
    for b in balances[:15]:
        asset = (b.get("asset_symbol") or b.get("currency") or b.get("asset")
                 or b.get("symbol")
                 or (f"asset#{b.get('asset_id')}" if b.get("asset_id") is not None else "?"))
        lines.append(f"• `{asset}`: "
                     f"total {float(b.get('balance',0)):.4f} / "
                     f"avail {float(b.get('available_balance',0)):.4f}")
    tg.send_message(chat_id, "\n".join(lines))


def cmd_positions(delta: DeltaClient, tg: TelegramClient, chat_id):
    try:
        positions = delta.get_positions()
    except Exception as exc:
        tg.send_message(chat_id, f"⚠️ Positions fetch failed: {exc}")
        return
    open_pos = [p for p in positions if abs(float(p.get("size", 0))) > 0]
    if not open_pos:
        tg.send_message(chat_id, "📭 No open positions on the exchange.")
        return
    lines = [f"📊 *Open positions* ({NETWORK_LABEL})"]
    for p in open_pos:
        upnl = float(p.get("unrealized_pnl", 0))
        lines.append(f"• `{p.get('product_symbol','?')}` {p.get('size')} @ "
                     f"{p.get('entry_price')} | uPnL {upnl:.2f}")
    tg.send_message(chat_id, "\n".join(lines))


def cmd_pnl(delta: DeltaClient, tg: TelegramClient, chat_id):
    stats = state["stats"]
    unrealised = 0.0
    try:
        for p in delta.get_positions():
            unrealised += float(p.get("unrealized_pnl", 0))
    except Exception:
        pass
    winrate = (stats["wins"] / stats["total_trades"] * 100) if stats["total_trades"] else 0
    tg.send_message(chat_id,
        "📈 *P&L*\n"
        f"Realised: `${stats.get('realized_pnl',0.0):.2f}`\n"
        f"Unrealised: `${unrealised:.2f}`\n"
        f"Daily loss used: `${stats.get('daily_loss',0.0):.2f}` / `${MAX_DAILY_LOSS_USDT:.2f}`\n"
        f"Trades: {stats['total_trades']}  W:{stats['wins']} L:{stats['losses']}  WR:{winrate:.0f}%")


def cmd_journal(tg: TelegramClient, chat_id):
    if not JOURNAL_FILE.exists():
        tg.send_message(chat_id, "📭 Journal is empty — no trades recorded yet.")
        return
    tg.send_document(chat_id, JOURNAL_FILE,
                     caption=f"trade_journal.csv ({NETWORK_LABEL})")


def cmd_panic(delta: DeltaClient, tg: TelegramClient, chat_id) -> None:
    """Stage a panic: show a confirm card describing what will close."""
    symbols = list(state["positions"].keys())
    if not symbols:
        state["trading_halted"] = True
        save_state()
        tg.send_message(chat_id, "⛔ No tracked positions. Trading halted — /resume to enable.")
        return
    trade = {"symbol": "PANIC", "side": "all", "qty": len(symbols),
             "source": "panic", "panic_symbols": symbols}
    token = store_pending(trade)
    tg.send_message(chat_id,
        f"🛑 *Panic confirmation*\n"
        f"Close *{len(symbols)}* tracked position(s) on {NETWORK_LABEL}: "
        f"`{', '.join(symbols)}` and halt new orders.\n"
        "This is irreversible until /resume.",
        [[_btn("✅ Confirm PANIC", f"panic:{token}"),
          _btn("❌ Cancel", f"cnl:{token}")]])


def cmd_resume(tg: TelegramClient, chat_id):
    state["trading_halted"] = False
    save_state()
    tg.send_message(chat_id, "🟢 Trading re-enabled. /trade to continue.")


def cmd_close(delta: DeltaClient, tg: TelegramClient, chat_id, symbol: str) -> None:
    if symbol.upper() not in state["positions"]:
        tg.send_message(chat_id, f"❓ No tracked position for `{symbol.upper()}`. "
                                 "Use /positions for the live list.")
        return
    pos = state["positions"][symbol.upper()]
    trade = {"symbol": symbol.upper(), "side": pos["side"], "qty": pos["qty"],
             "source": "close"}
    token = store_pending(trade)
    tg.send_message(chat_id,
        f"🔒 *Close confirmation* `{symbol.upper()}` "
        f"({pos['side']}, {pos['qty']} contracts) on {NETWORK_LABEL}",
        [[_btn("✅ Confirm CLOSE", f"close:{token}"),
          _btn("❌ Cancel", f"cnl:{token}")]])


# ── Guided /trade flow ──────────────────────────────────────────────────
def _prompt_for_step(tg: TelegramClient, chat_id, draft: dict):
    """Ask the question for draft['step'] with appropriate buttons."""
    step = draft["step"]
    trade = draft["trade"]
    if step == "symbol":
        tg.send_message(chat_id, "Which market? Send a symbol, e.g. `BTCUSD`.")
    elif step == "side":
        draft["prompt_msg_id"] = _send_kb(tg, chat_id, "Long or Short?",
            [[{"text": "🟢 Long (buy)", "callback_data": "side:buy"},
              {"text": "🔴 Short (sell)", "callback_data": "side:sell"}]])
    elif step == "sizing":
        draft["prompt_msg_id"] = _send_kb(tg, chat_id, "How should I size the position?",
            [[{"text": "Fixed quantity", "callback_data": "sizing:fixed"},
              {"text": "Risk-based", "callback_data": "sizing:risk"}]])
    elif step == "qty":
        tg.send_message(chat_id, "Enter the number of contracts (integer).")
    elif step == "risk_amount":
        tg.send_message(chat_id, "Risk amount in USDT you'll lose if SL hits (e.g. `50`).")
    elif step == "sl_mode":
        draft["prompt_msg_id"] = _send_kb(tg, chat_id, "Stop-loss type?",
            [[{"text": "📌 Fixed SL", "callback_data": "slmode:fixed"},
              {"text": "📈 Trailing SL", "callback_data": "slmode:trailing"}]])
    elif step == "sl_price":
        tg.send_message(chat_id, f"Enter SL price for `{trade['symbol']}` "
                                 "(below entry for long, above for short).")
    elif step == "trail_amount":
        tg.send_message(chat_id, "Enter the trail amount (in contract price units, e.g. `100`).")
    elif step == "tp_mode":
        draft["prompt_msg_id"] = _send_kb(tg, chat_id, "Take-profit?",
            [[{"text": "Single TP", "callback_data": "tpmode:single"},
              {"text": "Scale-out (2 legs)", "callback_data": "tpmode:scale"},
              {"text": "No TP", "callback_data": "tpmode:none"}]])
    elif step == "tp_price":
        tg.send_message(chat_id, "Enter the take-profit price.")
    elif step == "tp_leg1_price":
        tg.send_message(chat_id, "Scale-out: price for TP leg 1.")
    elif step == "tp_leg1_pct":
        tg.send_message(chat_id, "Scale-out: % of position to close at leg 1 "
                                 "(e.g. `60`). Leg 2 gets the rest.")
    elif step == "tp_leg2_price":
        tg.send_message(chat_id, "Scale-out: price for TP leg 2.")


def _send_kb(tg: TelegramClient, chat_id, text, keyboard):
    res = tg.send_message(chat_id, text, keyboard)
    return (res or {}).get("result", {}).get("message_id")


def start_guided_trade(tg: TelegramClient, chat_id) -> None:
    drafts[str(chat_id)] = {"step": "symbol", "trade": new_trade_dict()}
    _prompt_for_step(tg, chat_id, drafts[str(chat_id)])


def handle_guided_text(delta: DeltaClient, tg: TelegramClient, chat_id, text: str) -> bool:
    """Return True if this free-text input was consumed by the guided flow."""
    draft = drafts.get(str(chat_id))
    if not draft:
        return False
    trade = draft["trade"]
    step = draft["step"]
    try:
        if step == "symbol":
            trade["symbol"] = text.strip().upper()
            delta.get_product(trade["symbol"])  # validate the market exists
            draft["step"] = "side"
        elif step == "qty":
            trade["qty"] = int(float(text))
            draft["step"] = "sl_mode"
        elif step == "risk_amount":
            trade["risk_amount"] = float(text)
            draft["step"] = "sl_mode"
        elif step == "sl_price":
            trade["sl_price"] = float(text)
            trade["entry"] = trade["entry"] or delta.mark_price(trade["symbol"])
            # compute risk qty now that we have entry + SL
            if trade["sizing"] == "risk":
                trade["qty"] = compute_risk_qty(
                    delta, trade["risk_amount"], trade["entry"], trade["sl_price"],
                    trade["symbol"])
            draft["step"] = "tp_mode"
        elif step == "trail_amount":
            trade["trail_amount"] = float(text)
            trade["entry"] = trade["entry"] or delta.mark_price(trade["symbol"])
            # Risk-based + trailing: the trail amount is the initial price distance.
            if trade["sizing"] == "risk":
                trade["qty"] = compute_risk_qty_from_distance(
                    delta, trade["risk_amount"], trade["trail_amount"], trade["symbol"])
            draft["step"] = "tp_mode"
        elif step == "tp_price":
            trade["tp_price"] = float(text)
            draft["step"] = "confirm"
        elif step == "tp_leg1_price":
            trade["leg1_price"] = float(text)
            draft["step"] = "tp_leg1_pct"
        elif step == "tp_leg1_pct":
            pct = float(text)
            inc = delta.product_size_increment(trade["symbol"])
            qty = int(trade["qty"])
            leg1_qty = round_to_increment(qty * pct / 100.0, inc)
            # keep BOTH legs >= 1 contract (prevents zero/negative leg orders)
            leg1_qty = max(inc or 1, min(leg1_qty, qty - (inc or 1)))
            leg2_qty = qty - leg1_qty
            trade["tp_legs"] = [{"price": trade.get("leg1_price"), "pct": pct,
                                 "qty": leg1_qty}]
            trade["_leg2_pct"] = round(100.0 - pct, 2)
            trade["_leg2_qty"] = leg2_qty
            draft["step"] = "tp_leg2_price"
        elif step == "tp_leg2_price":
            trade["tp_legs"].append({"price": float(text),
                                     "pct": trade["_leg2_pct"], "qty": trade["_leg2_qty"]})
            draft["step"] = "confirm"
        else:
            return False
    except Exception as exc:
        tg.send_message(chat_id, f"⚠️ That didn't work: {exc}. Try again or /cancel.")
        return True

    if draft["step"] == "confirm":
        drafts.pop(str(chat_id), None)
        show_confirmation(delta, tg, chat_id, trade)
    else:
        _prompt_for_step(tg, chat_id, draft)
    return True


def handle_guided_callback(delta: DeltaClient, tg: TelegramClient, chat_id, data: str) -> bool:
    draft = drafts.get(str(chat_id))
    if not draft:
        return False
    trade = draft["trade"]
    if data.startswith("side:"):
        trade["side"] = data.split(":", 1)[1]
        # entry price: capture now for risk math & limit defaulting
        trade["entry"] = delta.mark_price(trade["symbol"])
        draft["step"] = "sizing"
    elif data.startswith("sizing:"):
        trade["sizing"] = data.split(":", 1)[1]
        draft["step"] = "qty" if trade["sizing"] == "fixed" else "risk_amount"
    elif data.startswith("slmode:"):
        trade["sl_type"] = data.split(":", 1)[1]
        draft["step"] = "sl_price" if trade["sl_type"] == "fixed" else "trail_amount"
    elif data.startswith("tpmode:"):
        mode = data.split(":", 1)[1]
        trade["tp_mode"] = mode if mode in {"single", "scale"} else "none"
        if trade["tp_mode"] == "single":
            draft["step"] = "tp_price"
        elif trade["tp_mode"] == "scale":
            draft["step"] = "tp_leg1_price"
        else:
            drafts.pop(str(chat_id), None)
            show_confirmation(delta, tg, chat_id, trade)
            return True
    else:
        return False
    _prompt_for_step(tg, chat_id, draft)
    return True


def cmd_cancel(tg: TelegramClient, chat_id):
    drafts.pop(str(chat_id), None)
    tg.send_message(chat_id, "🚪 Guided flow cancelled.")


# ─────────────────────────────────────────────────────────────────────────
# DISPATCHER  (owner-locked)
# ─────────────────────────────────────────────────────────────────────────
def is_owner(user_id) -> bool:
    if not TELEGRAM_OWNER_ID:
        # NOTE: fails OPEN — everyone is treated as the owner. run() logs a loud
        # CRITICAL warning at startup when this coincides with a non-testnet target.
        return True
    return str(user_id) == str(TELEGRAM_OWNER_ID)


def handle_message(delta: DeltaClient, tg: TelegramClient, chat_id, user_id, text: str):
    if not is_owner(user_id):
        log.warning("Ignored message from non-owner %s", user_id)
        return
    text = (text or "").strip()
    low = text.lower()
    if low.startswith("/start"):
        cmd_start(tg, chat_id)
    elif low.startswith("/help"):
        cmd_help(tg, chat_id)
    elif low.startswith("/balance"):
        cmd_balance(delta, tg, chat_id)
    elif low.startswith("/positions"):
        cmd_positions(delta, tg, chat_id)
    elif low.startswith("/pnl"):
        cmd_pnl(delta, tg, chat_id)
    elif low.startswith("/journal"):
        cmd_journal(tg, chat_id)
    elif low.startswith("/resume"):
        cmd_resume(tg, chat_id)
    elif low.startswith("/cancel"):
        cmd_cancel(tg, chat_id)
    elif low.startswith("/panic"):
        cmd_panic(delta, tg, chat_id)
    elif low.startswith("/close"):
        parts = text.split()
        if len(parts) < 2:
            tg.send_message(chat_id, "Usage: `/close SYMBOL` — e.g. `/close BTCUSD`")
        else:
            cmd_close(delta, tg, chat_id, parts[1])
    elif low.startswith("/trade"):
        parts = text.split()
        if len(parts) >= 5:
            trade = parse_quick_trade(text)
            if trade is None:
                tg.send_message(chat_id, "❓ Could not parse quick trade. Expected:\n"
                                         "`/trade BTCUSD buy 5 12000 sl=11500 tp=12500`\n"
                                         "or just `/trade` for the guided flow.")
                return
            # Fill any risk-based sizing for the quick path too.
            try:
                show_confirmation(delta, tg, chat_id, trade)
            except Exception as exc:
                tg.send_message(chat_id, f"⚠️ {exc}")
        else:
            start_guided_trade(tg, chat_id)
    else:
        if not handle_guided_text(delta, tg, chat_id, text):
            tg.send_message(chat_id, "Send /help to see commands.")


def handle_callback(delta: DeltaClient, tg: TelegramClient, chat_id, user_id,
                    data: str, callback_id: str, message_id):
    if not is_owner(user_id):
        tg.answer_callback(callback_id, "Not authorised.")
        return
    if handle_guided_callback(delta, tg, chat_id, data):
        tg.answer_callback(callback_id)
        return

    if data.startswith("cfm:"):
        msg = on_confirm(delta, tg, data[4:], chat_id, message_id)
        tg.answer_callback(callback_id, msg)
        tg.edit_message(chat_id, message_id, msg)
    elif data.startswith("close:"):
        token = data[6:]
        trade = state.get("pending", {}).pop(token, None)
        save_state()
        if trade and state.get("trading_halted"):
            tg.answer_callback(callback_id, "⛔ Trading halted — use /resume")
            tg.edit_message(chat_id, message_id, "⛔ Trading halted. /resume to enable.")
            return
        if trade:
            try:
                close_position(delta, trade["symbol"], reason="manual")
                tg.edit_message(chat_id, message_id, f"✅ Closed {trade['symbol']}.")
            except Exception as exc:
                tg.edit_message(chat_id, message_id, f"❌ Close failed: {exc}")
    elif data.startswith("panic:"):
        token = data[6:]
        trade = state.get("pending", {}).pop(token, None)
        save_state()
        if trade:
            state["trading_halted"] = True  # halt BEFORE closing so no new orders slip in
            save_state()
            closed, failed = [], []
            for sym in trade.get("panic_symbols", []):
                try:
                    close_position(delta, sym, reason="panic")
                    closed.append(sym)
                except Exception as exc:
                    failed.append(f"{sym}: {exc}")
            tg.edit_message(chat_id, message_id,
                            f"🛑 Panic done. Closed: {', '.join(closed) or 'none'}."
                            + (f"\nFailed: {'; '.join(failed)}" if failed else "")
                            + "\nTrading HALTED — /resume to enable.")
    elif data.startswith("cnl:"):
        on_cancel(data[4:])
        tg.answer_callback(callback_id, "Cancelled")
        tg.edit_message(chat_id, message_id, "Cancelled — no order was placed.")


# ─────────────────────────────────────────────────────────────────────────
# POSITION MONITOR  (Feature 5 — detect TP-leg fills / closes by polling)
# ─────────────────────────────────────────────────────────────────────────
def monitor_positions_once(delta: DeltaClient) -> None:
    for symbol in list(state["positions"].keys()):
        tracked = state["positions"][symbol]
        try:
            live = [p for p in delta.get_positions()
                    if str(p.get("product_symbol", "")).upper() == symbol.upper()]
        except Exception as exc:
            log.debug("monitor poll failed for %s: %s", symbol, exc)
            continue
        size = sum(abs(float(p.get("size", 0))) for p in live)
        opened = float(tracked.get("qty", 0) or 0)
        if size == 0 and opened > 0:
            # position fully gone → TP (or manual) closed it. No explicit close
            # order on this path, so use the last known mark price as the exit.
            # Realize only the residual still-open qty (legs already filled had
            # their PnL booked at each leg's TP price).
            exit_price = delta.mark_price(symbol)
            entry_price = tracked.get("entry")
            residual = int(opened) - int(tracked.get("realized_qty", 0) or 0)
            pnl = None
            if residual > 0 and entry_price is not None and exit_price is not None:
                pnl = record_realized_pnl(delta, symbol, tracked.get("side"),
                                          residual, entry_price, exit_price)
            elif residual <= 0:
                pnl = 0.0
            journal_append("close", {"symbol": symbol, "side": tracked.get("side"),
                                     "qty": max(residual, 0), "entry": entry_price},
                           pnl=("" if pnl is None else round(pnl, 4)),
                           note="detected closed on exchange"
                           + (f" @ exit {exit_price}" if exit_price else "")
                           + (f" residual {residual}/{int(opened)}" if residual != int(opened) else ""))
            state["positions"].pop(symbol, None)
            save_state()
            continue
        if tracked.get("tp_mode") == "scale" and tracked.get("tp_legs"):
            closed_so_far = opened - size
            cumulative = 0
            for leg in tracked["tp_legs"]:
                cumulative += int(leg["qty"])
                if leg.get("filled"):
                    continue
                # A leg is filled once the exchange has closed at least the
                # cumulative size of that leg (legs scale out in order).
                if closed_so_far >= cumulative:
                    leg["filled"] = True
                    entry_price = tracked.get("entry")
                    pnl = None
                    if entry_price is not None and leg.get("price") is not None:
                        pnl = record_realized_pnl(delta, symbol, tracked.get("side"),
                                                  leg["qty"], entry_price, leg["price"],
                                                  count_trade=False)
                        tracked["realized_qty"] = int(tracked.get("realized_qty", 0) or 0) \
                            + int(leg["qty"])
                    journal_append("partial_tp_filled",
                                   {"symbol": symbol, "side": tracked.get("side"),
                                    "qty": leg["qty"], "tp_price": leg["price"]},
                                   pnl=("" if pnl is None else round(pnl, 4)),
                                   note="scale-out leg filled"
                                   + (f" @ exit {leg['price']}" if leg.get("price") else ""))
                    save_state()


def monitor_loop(delta: DeltaClient, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            reset_daily_if_needed()
            monitor_positions_once(delta)
        except Exception as exc:  # pragma: no cover - loop
            log.error("monitor loop error: %s", exc)
        stop_event.wait(POSITION_POLL_SECONDS)


# ─────────────────────────────────────────────────────────────────────────
# TRADINGVIEW WEBHOOK  (Feature 8)
#
#   Receives an alert payload, turns it into the SAME trade dict, stores it as a
#   pending confirmation and pushes a ✅/❌ card to the owner. It NEVER executes.
# ─────────────────────────────────────────────────────────────────────────
def webhook_trade_from_payload(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    symbol = payload.get("symbol") or payload.get("ticker")
    action = (payload.get("action") or payload.get("side") or "").lower()
    if not symbol or action not in {"buy", "sell", "long", "short"}:
        return None
    trade = new_trade_dict()
    trade["source"] = "webhook"
    trade["symbol"] = str(symbol).upper()
    trade["side"] = "buy" if action in {"buy", "long"} else "sell"
    trade["market"] = True
    trade["entry"] = payload.get("price") or payload.get("close")
    if payload.get("qty") or payload.get("quantity"):
        trade["qty"] = int(float(payload.get("qty") or payload.get("quantity")))
    if payload.get("sl") or payload.get("stop"):
        trade["sl_type"] = "fixed"
        trade["sl_price"] = float(payload.get("sl") or payload.get("stop"))
    if payload.get("trail"):
        trade["sl_type"] = "trailing"
        trade["trail_amount"] = float(payload["trail"])
    if payload.get("tp"):
        trade["tp_mode"] = "single"
        trade["tp_price"] = float(payload["tp"])
    return trade


def start_webhook_server(delta: DeltaClient, tg: TelegramClient, chat_id: str):
    """Run a Flask endpoint in a daemon thread. Returns the Flask app or None."""
    try:
        from flask import Flask, request, jsonify
    except Exception:
        log.error("WEBHOOK_ENABLED but Flask isn't installed "
                  "(pip install flask). Webhook disabled.")
        return None

    app = Flask(__name__)

    @app.route("/webhook", methods=["POST"])
    def webhook():
        payload = request.get_json(silent=True) or request.form.to_dict()
        # optional shared-secret gate
        secret = os.getenv("WEBHOOK_SECRET")
        if secret and str(payload.get("secret", "")) != secret:
            return jsonify({"ok": False, "error": "bad secret"}), 403
        trade = webhook_trade_from_payload(payload)
        if not trade or not trade.get("qty"):
            return jsonify({"ok": False, "error": "payload missing symbol/action/qty"}), 400
        show_confirmation(delta, tg, chat_id, trade)  # <-- confirm card only, never executes
        return jsonify({"ok": True, "queued": trade["symbol"]}), 202

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "network": NETWORK_LABEL})

    thread = threading.Thread(target=app.run,
                              kwargs={"host": WEBHOOK_HOST, "port": WEBHOOK_PORT,
                                      "debug": False, "use_reloader": False},
                              daemon=True, name="webhook")
    thread.start()
    log.info("TradingView webhook listening on http://%s:%d/webhook", WEBHOOK_HOST, WEBHOOK_PORT)
    return app


# ─────────────────────────────────────────────────────────────────────────
# MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────
def build_clients(delta: DeltaClient | None = None,
                  tg: TelegramClient | None = None) -> tuple[DeltaClient, TelegramClient]:
    delta = delta or DeltaClient(DELTA_BASE_URL, DELTA_API_KEY, DELTA_API_SECRET)
    tg = tg or TelegramClient(TELEGRAM_BOT_TOKEN)
    return delta, tg


def poll_loop(delta: DeltaClient, tg: TelegramClient, stop_event: threading.Event) -> None:
    log.info("Bot online | %s | polling Telegram (owner=%s)",
             DELTA_BASE_URL, TELEGRAM_OWNER_ID or "<unset>")
    while not stop_event.is_set():
        try:
            for upd in tg.get_updates():
                if "message" in upd:
                    msg = upd["message"]
                    handle_message(delta, tg, msg["chat"]["id"],
                                   msg["from"]["id"], msg.get("text", ""))
                elif "callback_query" in upd:
                    cb = upd["callback_query"]
                    handle_callback(delta, tg, cb["message"]["chat"]["id"],
                                    cb["from"]["id"], cb.get("data", ""),
                                    cb["id"], cb["message"]["message_id"])
        except Exception as exc:  # pragma: no cover - loop resilience
            log.error("poll loop error: %s\n%s", exc, traceback.format_exc())
            time.sleep(2)


def run(stop_event: threading.Event | None = None) -> None:
    global state
    state = load_state()
    reset_daily_if_needed()
    stop_event = stop_event or threading.Event()

    delta, tg = build_clients()

    if NETWORK_LABEL == "LIVE":
        log.warning("⚠️ RUNNING AGAINST LIVE DELTA ENDPOINT (real money)")
    else:
        log.info("🧪 Running against TESTNET")

    # Fail-open owner lock: harmless on local testnet, dangerous with real money.
    if not TELEGRAM_OWNER_ID and (NETWORK_LABEL == "LIVE" or not USE_TESTNET):
        log.critical(
            "🚨 TELEGRAM_OWNER_ID is NOT set while running on %s (%s). "
            "is_owner() fails OPEN — ANY Telegram user who messages this bot is "
            "treated as the owner and can place confirm-gated orders. Set "
            "TELEGRAM_OWNER_ID before running with real money.",
            NETWORK_LABEL, DELTA_BASE_URL,
        )

    if WEBHOOK_ENABLED:
        start_webhook_server(delta, tg, TELEGRAM_OWNER_ID or "")

    monitor = threading.Thread(target=monitor_loop, args=(delta, stop_event),
                               daemon=True, name="monitor")
    monitor.start()

    try:
        poll_loop(delta, tg, stop_event)
    except KeyboardInterrupt:  # pragma: no cover
        log.info("Interrupted; shutting down")
    finally:
        stop_event.set()
        save_state()


def main() -> None:  # pragma: no cover - CLI entry
    print("=" * 60)
    print("  Delta Exchange — Telegram Trading Bot (manual, confirm-gated)")
    print("=" * 60)
    print(f"  Network        : {NETWORK_LABEL}  ({DELTA_BASE_URL})")
    print(f"  Owner id       : {TELEGRAM_OWNER_ID or '<unset — dev mode>'}")
    print(f"  Telegram token : {'set' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
    print(f"  Delta keys     : {'set' if DELTA_API_KEY else 'MISSING'}")
    print(f"  Daily loss cap : ${MAX_DAILY_LOSS_USDT}")
    print(f"  Webhook        : {'on' if WEBHOOK_ENABLED else 'off'} "
          f"{'http://%s:%d/webhook' % (WEBHOOK_HOST, WEBHOOK_PORT)}")
    print("=" * 60)
    if not TELEGRAM_BOT_TOKEN:
        print("\n❌ TELEGRAM_BOT_TOKEN is not configured.")
        print("To run the bot:")
        print("  1. Copy .env.example to .env:  cp .env.example .env")
        print("  2. Add your Telegram bot token from @BotFather")
        print("  3. Add your numeric Telegram user ID from @userinfobot")
        print("  4. Add your Delta Exchange API key and secret")
        print("\nTo test without live credentials, run the test harness:")
        print("  python -X utf8 test_delta_bot.py\n")
        sys.exit(1)
    if not TELEGRAM_OWNER_ID and (NETWORK_LABEL == "LIVE" or not USE_TESTNET):
        print("\n🚨 WARNING: TELEGRAM_OWNER_ID is unset while NOT on testnet — the "
              "owner-lock fails OPEN and ANY Telegram user can drive this bot "
              "(place confirm-gated orders). Set TELEGRAM_OWNER_ID before going live.")
    if NETWORK_LABEL == "LIVE" and os.getenv("ACK_LIVE") != "1":
        print("\n⚠️ LIVE trading. Type LIVE to continue:")
        if input("> ").strip() != "LIVE":
            print("Aborted.")
            sys.exit(0)
    run()


if __name__ == "__main__":  # pragma: no cover
    main()
