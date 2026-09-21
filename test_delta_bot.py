"""
Offline test harness for delta_telegram_bot.py
==============================================

Exercises every command and the confirmation gate with **injected fake
transports** for both the Delta REST layer and the Telegram API, configured
against the TESTNET base URL, and writes a readable log to testnet_test_log.txt.

Why fakes: a genuine end-to-end run needs Delta testnet API keys plus interactive
✅-button taps on a real Telegram chat, which aren't available in CI. These tests
prove the *control flow and safety invariants* deterministically — above all the
non-negotiable rule that **no order reaches the exchange without an explicit
confirm press** — and assert the testnet host is the one actually used.

Run:  python -X utf8 test_delta_bot.py     (exit 0 = all pass)
"""

from __future__ import annotations

import os
import io
import sys
import json
import time
import tempfile
import contextlib
from pathlib import Path

try:  # the console on Windows defaults to cp1252 and can't encode emoji
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ── Force testnet + DUMMY credentials BEFORE importing the module ──────────
# load_dotenv() (override=False) never clobbers vars already in os.environ, so
# setting these here makes the harness hermetic — an operator's real .env in the
# project folder cannot leak in and change behaviour (notably TELEGRAM_OWNER_ID,
# which otherwise flips is_owner()'s fail-open dev mode).
TEST_OWNER = "111"
os.environ["USE_TESTNET"] = "true"
os.environ.pop("DELTA_BASE_URL", None)
os.environ["TELEGRAM_OWNER_ID"] = TEST_OWNER
os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
os.environ["DELTA_API_KEY"] = "test-key"
os.environ["DELTA_API_SECRET"] = "test-secret"
os.environ["MAX_DAILY_LOSS_USDT"] = "100"
os.environ["WEBHOOK_ENABLED"] = "false"
_TMP = tempfile.mkdtemp(prefix="deltatest_")  # outside OneDrive to avoid file locks
os.environ["DELTA_STATE_FILE"] = os.path.join(_TMP, "delta_bot_state.json")
os.environ["DELTA_JOURNAL_FILE"] = os.path.join(_TMP, "trade_journal.csv")

Path("var").mkdir(exist_ok=True)

import delta_telegram_bot as bot  # noqa: E402

# ── reference data returned by the fake Delta server ─────────────────────
PRODUCTS = [
    {"id": 1, "symbol": "BTCUSD", "tick_size": 0.5, "contracting_price": 0.001,
     "size_delta": 1, "min_size": 1, "mark_price": 12345.6, "market_price": 12345.0},
    {"id": 2, "symbol": "ETHUSD", "tick_size": 0.05, "contracting_price": 0.01,
     "size_delta": 1, "min_size": 1, "mark_price": 3000.0, "market_price": 3000.0},
]
OWNER = TEST_OWNER
CHAT = 555

# Never actually sleep during retry tests.
bot.time.sleep = lambda *a, **k: None  # type: ignore


# ── fake Telegram transport ───────────────────────────────────────────────
class FakeTG:
    def __init__(self):
        self.sent = []          # dicts describing each outbound API call
        self._mid = 1
        self.documents = []

    def _rec(self, **kw):
        self.sent.append(kw)
        return kw

    def send_message(self, chat_id, text, keyboard=None):
        mid = self._mid; self._mid += 1
        self._rec(kind="message", chat_id=chat_id, text=text,
                  keyboard=keyboard, message_id=mid)
        return {"ok": True, "result": {"message_id": mid}}

    def edit_message(self, chat_id, message_id, text, keyboard=None):
        self._rec(kind="edit", chat_id=chat_id, text=text,
                  keyboard=keyboard, message_id=message_id)
        return {"ok": True}

    def send_document(self, chat_id, path, caption=""):
        self.documents.append(str(path))
        self._rec(kind="doc", chat_id=chat_id, path=str(path), caption=caption)
        return {"ok": True}

    def answer_callback(self, callback_id, text=""):
        self._rec(kind="cb", text=text)
        return {"ok": True}

    def get_updates(self, timeout=25):
        return []

    # helpers used by the tests ------------------------------------------------
    def texts(self):
        return [m.get("text", "") for m in self.sent]

    def find_keyboard(self, prefix):
        """Return (token, message_id) of the most recent button matching prefix."""
        for m in reversed(self.sent):
            for row in (m.get("keyboard") or []):
                for b in row:
                    cd = b.get("callback_data", "")
                    if cd.startswith(prefix):
                        return cd[len(prefix):], m.get("message_id")
        return None, None


# ── fake Delta HTTP server ────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise bot.requests.exceptions.HTTPError(f"HTTP {self.status_code}")


class FakeServer:
    def __init__(self):
        self.orders = []            # recorded order payloads (POST /v2/orders)
        self.positions = {}         # symbol -> remaining size (for monitor tests)
        self.counts = {}
        self.next_id = 1000
        # fault-injection knobs
        self.status_seq = {}        # (method, path) -> list of statuses to return first
        self.raise_network_once = set()   # (method, path) that records-then-raises once
        self.session = _Session(self)

    def bump(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def handle(self, method, url, body):
        method = method.upper()
        path = url.split(bot.DELTA_BASE_URL, 1)[-1].split("?", 1)[0]
        key = (method, path)
        self.bump(key)

        # consume an injected status sequence (e.g. 429, 429 then success).
        # Only short-circuit for injected *error* statuses; a 200 falls through
        # to the real handler so the client sees a proper success payload.
        seq = self.status_seq.get(key)
        if seq:
            status = seq.pop(0)
            if not seq:
                del self.status_seq[key]
            if status != 200:
                headers = {"Retry-After": "1"} if status == 429 else {}
                return FakeResp(status, {"success": False, "error": "forced"}, headers)

        # ---- POST /v2/orders (order creation) ----
        if method == "POST" and path == "/v2/orders":
            payload = json.loads(body) if body else {}
            self.next_id += 1
            order = {"id": self.next_id, **payload}
            self.orders.append(order)  # recorded on the "server"
            if key in self.raise_network_once:
                self.raise_network_once.discard(key)  # remove before raising
                raise bot.requests.exceptions.ConnectionError("simulated drop")
            return FakeResp(200, {"success": True, "result": order})

        # ---- GET endpoints ----
        if path == "/v2/products":
            return FakeResp(200, {"success": True, "result": PRODUCTS})
        if path.startswith("/v2/products/"):
            sym = path.rsplit("/", 1)[-1]
            prod = next((p for p in PRODUCTS if p["symbol"] == sym), {})
            return FakeResp(200, {"success": True, "result": prod})
        if path.startswith("/v2/tickers/"):
            sym = path.rsplit("/", 1)[-1]
            prod = next((p for p in PRODUCTS if p["symbol"] == sym), {})
            mp = prod.get("mark_price", 0)
            return FakeResp(200, {"success": True, "result": {
                "symbol": sym, "mark_price": mp, "spot_price": mp, "close": mp}})
        if path == "/v2/wallet/balances":
            return FakeResp(200, {"success": True,
                "meta": {"net_equity": "56099.4"},
                "result": [
                    {"asset_symbol": "USDT", "asset_id": 3, "balance": "1000", "available_balance": "900"},
                    {"asset_symbol": "BTC", "asset_id": 2, "balance": "0.25", "available_balance": "0.25"},
                    {"asset_symbol": "ETH", "asset_id": 1, "balance": "10", "available_balance": "10"}]})
        if path == "/v2/positions":
            result = [{"product_symbol": s, "size": sz, "entry_price": 12345.6,
                       "unrealized_pnl": 1.23} for s, sz in self.positions.items()]
            return FakeResp(200, {"success": True, "result": result})
        if path == "/v2/orders":
            return FakeResp(200, {"success": True, "result": list(self.orders)})
        if path == "/v2/orders/history":
            return FakeResp(200, {"success": True, "result": []})
        return FakeResp(404, {"success": False, "error": f"unmapped {method} {path}"})


class _Session:
    def __init__(self, server):
        self.server = server

    def get(self, url, params=None, headers=None, timeout=None):
        # the client already appended query params to url for GET-with-params
        return self.server.handle("GET", url, None)

    def request(self, method, url, data=None, headers=None, timeout=None):
        return self.server.handle(method, url, data)


# ── per-scenario context reset ─────────────────────────────────────────────
def fresh():
    server = FakeServer()
    tg = FakeTG()
    delta = bot.DeltaClient(bot.DELTA_BASE_URL, "test-key", "test-secret")
    delta.session = server.session
    bot.state = json.loads(json.dumps(bot.DEFAULT_STATE))
    bot.drafts.clear()
    return delta, tg, server


def cb(delta, tg, data, message_id=0):
    bot.handle_callback(delta, tg, CHAT, OWNER, data, "callback-id", message_id)


def msg(delta, tg, text):
    bot.handle_message(delta, tg, CHAT, OWNER, text)


# ── tiny test framework ─────────────────────────────────────────────────────
LOG = io.StringIO()
RESULTS = []


def say(label, value):
    """Echo a transcript line (captured into testnet_test_log.txt by the harness)."""
    flat = " / ".join(str(value).splitlines())
    print(f"    · {label}: {flat}")


def test(name):
    def deco(fn):
        try:
            with contextlib.redirect_stdout(LOG):
                print(f"\n[CASE] {name}")
                fn()
            RESULTS.append((name, True, ""))
            print(f"PASS  {name}")
        except AssertionError as e:
            RESULTS.append((name, False, str(e)))
            print(f"FAIL  {name}: {e}")
        except Exception as e:  # noqa: BLE001
            import traceback
            RESULTS.append((name, False, repr(e)))
            print(f"ERROR {name}: {e!r}")
            traceback.print_exc(file=LOG)
        return fn
    return deco


# ─────────────────────────────────────────────────────────────────────────
# TESTS
# ─────────────────────────────────────────────────────────────────────────
@test("F1: testnet toggle selects testnet base URL & /start shows it")
def t_f1():
    assert bot.NETWORK_LABEL == "TESTNET", bot.NETWORK_LABEL
    assert bot.DELTA_BASE_URL == bot.TESTNET_BASE_URL
    assert bot.DELTA_BASE_URL != bot.LIVE_BASE_URL
    delta, tg, _ = fresh()
    bot.cmd_start(tg, CHAT)
    assert any("TESTNET" in t for t in tg.texts())


@test("F1: live URL is used when USE_TESTNET=false")
def t_f1_live():
    import importlib
    os.environ["USE_TESTNET"] = "false"
    try:
        importlib.reload(bot)
        assert bot.NETWORK_LABEL == "LIVE"
        assert bot.DELTA_BASE_URL == bot.LIVE_BASE_URL
    finally:
        os.environ["USE_TESTNET"] = "true"
        importlib.reload(bot)
        bot.time.sleep = lambda *a, **k: None  # reload reset the patch


@test("base: read-only commands work & never place orders")
def t_readonly():
    delta, tg, server = fresh()
    bot.cmd_balance(delta, tg, CHAT)
    bot.cmd_positions(delta, tg, CHAT)
    bot.cmd_pnl(delta, tg, CHAT)
    assert any("Balances" in t for t in tg.texts())
    assert any("P&L" in t for t in tg.texts())
    bal = next(t for t in tg.texts() if "Balances" in t)
    assert "Net equity" in bal and "56,099.40" in bal, bal   # meta.net_equity surfaced
    assert "USDT" in bal and "?" not in bal, bal              # currency resolved, no '?'
    assert server.orders == [], "read-only commands must not submit orders"


@test("NON-NEGOTIABLE: guided risk+trailing+scale-out shows confirm, no order until ✅")
def t_guided_gate_and_features():
    delta, tg, server = fresh()
    msg(delta, tg, "/trade")
    bot.handle_guided_text(delta, tg, CHAT, "BTCUSD")
    cb(delta, tg, "side:buy")
    cb(delta, tg, "sizing:risk")
    bot.handle_guided_text(delta, tg, CHAT, "50")      # risk amount
    cb(delta, tg, "slmode:trailing")
    bot.handle_guided_text(delta, tg, CHAT, "100")     # trail amount -> qty computed
    cb(delta, tg, "tpmode:scale")
    bot.handle_guided_text(delta, tg, CHAT, "13000")   # leg1 price
    bot.handle_guided_text(delta, tg, CHAT, "60")      # leg1 pct
    bot.handle_guided_text(delta, tg, CHAT, "14000")   # leg2 price -> confirm

    # a confirmation card with a ✅/❌ exists...
    token, mid = tg.find_keyboard("cfm:")
    assert token, "expected a confirm card"
    card = next(m for m in reversed(tg.sent)
                if any(b.get("callback_data", "").endswith(token)
                       for row in (m.get("keyboard") or []) for b in row))
    say("confirmation card shown", card["text"])
    # ...and it shows the computed risk qty + trailing + scale-out BEFORE confirming
    assert "Sizing: `risk`" in card["text"], card["text"]
    assert "trailing" in card["text"]
    assert "scale-out" in card["text"]
    # risk qty: distance=trail 100 * contracting 0.001 = 0.1 /contract; 50/0.1=500
    assert "Qty: `500`" in card["text"], card["text"]
    assert "Margin:" in card["text"] and "✅ fits" in card["text"], card["text"]

    # THE RULE: nothing submitted to the exchange yet
    assert server.orders == [], "order placed before confirm!"
    say("orders sent to exchange BEFORE confirm", len(server.orders))

    # confirm -> now exactly entry + 2 TP legs land
    cb(delta, tg, f"cfm:{token}", mid)
    assert len(server.orders) == 3, f"expected 3 orders, got {len(server.orders)}"
    entry = server.orders[0]
    assert entry["side"] == "buy" and entry["size"] == 500
    assert entry.get("stop_order_type") == "trailing_stop_loss_order"          # F4
    assert entry.get("enable_bracket") is True
    legs = server.orders[1:3]
    assert all(o.get("reduce_only") is True for o in legs)                    # F5 scale-out
    assert {o["size"] for o in legs} == {300, 200}
    say("orders after confirm",
        f"entry buy 500 (trailing_stop_loss_order) + TP legs "
        f"{[o['size'] for o in legs]} reduce_only")
    # state mirrors tp_legs (F5)
    pos = bot.state["positions"]["BTCUSD"]
    assert len(pos["tp_legs"]) == 2 and pos["tp_legs"][0]["qty"] == 300
    # journal recorded the open (F7)
    jtxt = bot.JOURNAL_FILE.read_text(encoding="utf-8")
    assert "open,BTCUSD,buy,500" in jtxt.replace('"', "") or ",open," in jtxt


@test("F3: /panic stages a confirm, then closes all + halts; on_confirm refuses when halted")
def t_panic():
    delta, tg, server = fresh()
    # place a normal fixed-SL order first (reuse quick path + confirm)
    msg(delta, tg, "/trade BTCUSD buy 5 12000 sl=11500 tp=12500")
    token, mid = tg.find_keyboard("cfm:"); cb(delta, tg, f"cfm:{token}", mid)
    assert "BTCUSD" in bot.state["positions"]
    orders_after_open = len(server.orders)

    bot.cmd_panic(delta, tg, CHAT)
    ptoken, pmid = tg.find_keyboard("panic:")
    assert ptoken, "panic should show a confirm card"
    assert len(server.orders) == orders_after_open, "panic must not close before confirm"
    say("/panic staged", "confirm card shown, 0 closes executed yet")

    cb(delta, tg, f"panic:{ptoken}", pmid)
    assert bot.state["trading_halted"] is True
    assert "BTCUSD" not in bot.state["positions"]
    assert len(server.orders) > orders_after_open, "panic confirm must place close order(s)"
    say("/panic confirmed", "all tracked positions closed, trading_halted=True")

    # while halted, any fresh order-confirm is refused with no submission
    tr = bot.new_trade_dict()
    tr.update(symbol="ETHUSD", side="sell", qty=3, market=True,
              sl_type="fixed", sl_price=3100, tp_mode="none", source="quick")
    tok2 = bot.store_pending(tr)
    before = len(server.orders)
    reply = bot.on_confirm(delta, tg, tok2, CHAT, 0)
    say("on_confirm while halted", f"refused with: {reply!r} (no order placed)")
    assert "halt" in reply.lower(), reply
    assert len(server.orders) == before, "on_confirm must not place while halted"
    assert tok2 not in bot.state["pending"], "token should be consumed on refusal"

    bot.cmd_resume(tg, CHAT)
    assert bot.state["trading_halted"] is False


@test("F3: circuit breaker refuses new orders on confirm even when not halted")
def t_circuit_breaker():
    delta, tg, server = fresh()
    bot.state["stats"]["daily_loss"] = bot.MAX_DAILY_LOSS_USDT + 1
    tr = bot.new_trade_dict()
    tr.update(symbol="BTCUSD", side="buy", qty=2, market=True,
              sl_type="fixed", sl_price=11500, tp_mode="none")
    tok = bot.store_pending(tr)
    reply = bot.on_confirm(delta, tg, tok, CHAT, 0)
    say("confirm with circuit breaker tripped", f"refused: {reply!r} (0 orders placed)")
    assert "Circuit breaker" in reply
    assert server.orders == []
    assert tok not in bot.state["pending"]


@test("Margin guard: blocks a trade whose required margin exceeds available USDT")
def t_margin_guard_blocks():
    delta, tg, server = fresh()
    # fake USDT available = 900; qty 1000 * 0.001 * 12000 = 12000 notional / 10 = 1200 margin
    assert bot.estimate_margin_required(
        delta, {"symbol": "BTCUSD", "qty": 1000, "entry": 12000, "leverage": 10}) == 1200.0
    assert bot.available_usdt(delta) == 900.0
    tr = bot.new_trade_dict()
    tr.update(symbol="BTCUSD", side="buy", qty=1000, market=True,
              entry=12000, sl_type="fixed", sl_price=11500, tp_mode="none")
    tok = bot.store_pending(tr)
    before = len(server.orders)
    reply = bot.on_confirm(delta, tg, tok, CHAT, 0)
    say("oversized confirm", f"blocked: {reply!r}")
    assert "Blocked" in reply, reply
    assert len(server.orders) == before, "must not place an oversized order"
    assert tok not in bot.state["pending"], "token consumed on block"


@test("Margin guard: allows a trade that fits within available USDT")
def t_margin_guard_allows():
    delta, tg, server = fresh()
    # qty 100 * 0.001 * 12000 = 1200 notional / 10 = 120 margin <= 900
    tr = bot.new_trade_dict()
    tr.update(symbol="BTCUSD", side="buy", qty=100, market=True,
              entry=12000, sl_type="fixed", sl_price=11500, tp_mode="none")
    tok = bot.store_pending(tr)
    reply = bot.on_confirm(delta, tg, tok, CHAT, 0)
    assert "Placed" in reply, reply
    assert len(server.orders) == 1, server.orders


@test("F6: quick trade builds the same dict and routes to the SAME confirm step")
def t_quick():
    delta, tg, server = fresh()
    msg(delta, tg, "/trade BTCUSD buy 3 12000 sl=11500 tp=12500")
    token, mid = tg.find_keyboard("cfm:")
    assert token
    assert server.orders == [], "quick trade must be confirm-gated like the guided flow"
    say("/trade quick form", "routed to same confirm card, 0 orders before confirm")
    tr = bot.state["pending"][token]
    assert tr["source"] == "quick" and tr["qty"] == 3 and tr["side"] == "buy"
    assert tr["entry"] == 12000.0 and tr["sl_price"] == 11500.0 and tr["tp_price"] == 12500.0
    cb(delta, tg, f"cfm:{token}", mid)
    assert len(server.orders) == 1
    o = server.orders[0]
    assert o["order_type"] == "limit_order" and o["limit_price"] == 12000.0
    assert o["enable_stop_loss"] is True and o["enable_take_profit"] is True

    # market quick trade parses correctly
    mkt = bot.parse_quick_trade("/trade ETHUSD sell 5 market sl=3100 tp=2800")
    assert mkt and mkt["market"] is True and mkt["entry"] is None and mkt["qty"] == 5
    # malformed quick trade -> parse returns None (falls back to guided / error msg)
    assert bot.parse_quick_trade("/trade BTCUSD buy x y") is None


@test("F5: monitor marks scale-out legs filled cumulatively and detects full close")
def t_monitor_scaleout():
    delta, tg, server = fresh()
    # open a scale-out position via the guided flow (reuse the working path)
    msg(delta, tg, "/trade")
    bot.handle_guided_text(delta, tg, CHAT, "BTCUSD")
    cb(delta, tg, "side:buy"); cb(delta, tg, "sizing:fixed")
    bot.handle_guided_text(delta, tg, CHAT, "500")     # fixed qty 500
    cb(delta, tg, "slmode:fixed")
    bot.handle_guided_text(delta, tg, CHAT, "11500")   # SL -> tp_mode
    cb(delta, tg, "tpmode:scale")
    bot.handle_guided_text(delta, tg, CHAT, "13000")
    bot.handle_guided_text(delta, tg, CHAT, "60")
    bot.handle_guided_text(delta, tg, CHAT, "14000")
    token, mid = tg.find_keyboard("cfm:"); cb(delta, tg, f"cfm:{token}", mid)

    pos = bot.state["positions"]["BTCUSD"]
    assert pos["tp_legs"][0]["qty"] == 300 and pos["tp_legs"][1]["qty"] == 200

    # exchange shows only 200 left => 300 closed => exactly leg1 filled (cumulative)
    server.positions["BTCUSD"] = 200
    bot.monitor_positions_once(delta)
    assert pos["tp_legs"][0]["filled"] is True
    assert pos["tp_legs"][1].get("filled") is not True, "only one leg should have filled"

    # exchange flat => position dropped + close journalled
    server.positions["BTCUSD"] = 0
    bot.monitor_positions_once(delta)
    assert "BTCUSD" not in bot.state["positions"]


@test("F7: /journal sends the CSV document")
def t_journal_send():
    delta, tg, server = fresh()
    # create an open event
    msg(delta, tg, "/trade BTCUSD buy 2 12000 sl=11500 tp=12500")
    token, mid = tg.find_keyboard("cfm:"); cb(delta, tg, f"cfm:{token}", mid)
    assert bot.JOURNAL_FILE.exists()
    bot.cmd_journal(tg, CHAT)
    assert tg.documents, "expected a sendDocument call"


@test("F8: TradingView webhook payload -> confirm card only, never executes")
def t_webhook_confirm_only():
    delta, tg, server = fresh()
    payload = {"symbol": "BTCUSD", "action": "long", "qty": 4,
               "price": 12100, "sl": 11800, "tp": 13000}
    tr = bot.webhook_trade_from_payload(payload)
    assert tr and tr["source"] == "webhook" and tr["side"] == "buy" and tr["qty"] == 4
    bot.show_confirmation(delta, tg, CHAT, tr)
    token, mid = tg.find_keyboard("cfm:")
    assert token, "webhook must present a confirmation card"
    assert server.orders == [], "webhook must never auto-execute"
    say("TradingView alert -> Telegram", "confirm card shown, 0 orders auto-executed")
    # and bad payloads are rejected, not coerced into trades
    assert bot.webhook_trade_from_payload({"symbol": "BTCUSD"}) is None
    assert bot.webhook_trade_from_payload({"action": "hold"}) is None


@test("F8 (flask optional): /webhook endpoint queues a confirm when flask is present")
def t_webhook_flask():
    try:
        import flask  # noqa: F401
    except Exception:
        print("  (flask not installed — skipping live route test; core path covered above)")
        return
    delta, tg, server = fresh()
    app = bot.start_webhook_server(delta, tg, str(CHAT))
    client = app.test_client()
    r = client.post("/webhook", json={"symbol": "BTCUSD", "action": "buy",
                                      "qty": 2, "price": 12000, "sl": 11500})
    assert r.status_code == 202, r.status_code
    assert server.orders == []
    assert tg.find_keyboard("cfm:")[0]


@test("F9: backoff honours Retry-After and recovers on GET 429s")
def t_backoff_429():
    server = FakeServer()
    delta = bot.DeltaClient(bot.DELTA_BASE_URL, "k", "s")
    delta.session = server.session
    server.status_seq[("GET", "/v2/wallet/balances")] = [429, 429, 200]
    res = delta.get_balance()          # should retry through the two 429s
    assert res and res["result"][0]["asset_symbol"] == "USDT"
    assert server.counts[("GET", "/v2/wallet/balances")] == 3


@test("F9: pure backoff/retry-after helpers")
def t_backoff_helpers():
    assert bot._backoff_delay(1, None) == 5
    assert bot._backoff_delay(2, None) == 10
    assert bot._backoff_delay(3, None) == 20
    assert bot._backoff_delay(10, None) == 60                 # capped at 60
    assert bot._backoff_delay(1, 30) == 30                    # retry-after wins if larger
    assert bot.DeltaClient._parse_retry_after(FakeResp(429, {}, {"Retry-After": "7"})) == 7.0
    assert bot.DeltaClient._parse_retry_after(FakeResp(429, {}, {})) is None


@test("F9: ambiguous order POST reconciles by client_order_id, never double-submits")
def t_duplicate_guard():
    server = FakeServer()
    delta = bot.DeltaClient(bot.DELTA_BASE_URL, "k", "s")
    delta.session = server.session
    payload = {"product_symbol": "BTCUSD", "side": "buy", "size": 1,
               "order_type": "market_order", "client_order_id": "coid-xyz"}
    # POST will be recorded by the server, then a network error is raised on the way back
    server.raise_network_once.add(("POST", "/v2/orders"))
    result = delta.create_order(payload)     # must reconcile, not re-POST
    assert result["client_order_id"] == "coid-xyz"
    assert server.counts[("POST", "/v2/orders")] == 1, "duplicate POST would mean a dup order"
    assert len(server.orders) == 1


@test("risk sizing: fixed-SL math & increment rounding")
def t_risk_math():
    delta, tg, server = fresh()
    # distance 12000-11500=500 * contracting 0.001 = 0.5/contract; 100/0.5=200
    assert bot.compute_risk_qty(delta, 100, 12000, 11500, "BTCUSD") == 200
    assert bot.round_to_increment(199.6, 1) == 200
    assert bot.round_to_tick(12000.3, 0.5) == 12000.5
    assert bot.round_to_tick(11500.2, 0.5) == 11500.0


@test("PnL: losing close updates daily_loss, realized_pnl & losses + journals pnl")
def t_close_loss_books_pnl():
    delta, tg, server = fresh()
    # BTCUSD mark 12345.6, contracting 0.001, size_increment 1
    # entry 13000 buy, qty 1000 -> (12345.6-13000)*1000*0.001 = -654.4
    bot.state["positions"]["BTCUSD"] = {"side": "buy", "qty": 1000, "entry": 13000}
    bot.close_position(delta, "BTCUSD", reason="test-loss")
    s = bot.state["stats"]
    assert s["losses"] == 1, s
    assert s["wins"] == 0, s
    assert abs(s["realized_pnl"] - (-654.4)) < 1e-6, s
    assert abs(s["daily_loss"] - 654.4) < 1e-6, s
    assert "BTCUSD" not in bot.state["positions"]
    # the close row must carry the realized pnl
    close_rows = [ln for ln in bot.JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
                  if ",close," in ln]
    assert close_rows, "no close row written"
    assert "654.4" in close_rows[-1], close_rows[-1]


@test("PnL: winning close updates wins & realized_pnl, leaves daily_loss at 0")
def t_close_win_books_pnl():
    delta, tg, server = fresh()
    # entry 12000 buy, qty 1000 -> (12345.6-12000)*1000*0.001 = +345.6
    bot.state["positions"]["BTCUSD"] = {"side": "buy", "qty": 1000, "entry": 12000}
    bot.close_position(delta, "BTCUSD", reason="test-win")
    s = bot.state["stats"]
    assert s["wins"] == 1 and s["losses"] == 0, s
    assert abs(s["realized_pnl"] - 345.6) < 1e-6, s
    assert s["daily_loss"] == 0.0, s


@test("PnL: monitor's exchange-detected close books PnL at the mark price")
def t_monitor_close_books_pnl():
    delta, tg, server = fresh()
    # entry 13000 buy, qty 1000; get_positions() returns [] -> size 0 -> close path
    bot.state["positions"]["BTCUSD"] = {"side": "buy", "qty": 1000,
                                        "entry": 13000, "tp_mode": "single"}
    bot.monitor_positions_once(delta)
    s = bot.state["stats"]
    assert s["losses"] == 1 and abs(s["daily_loss"] - 654.4) < 1e-6, s
    assert "BTCUSD" not in bot.state["positions"]


@test("Circuit breaker actually trips once realized losses cross the cap")
def t_circuit_breaker_trips_on_realized_loss():
    orig = bot.MAX_DAILY_LOSS_USDT
    try:
        bot.MAX_DAILY_LOSS_USDT = 100.0
        delta, tg, server = fresh()
        # one losing close of -654.4 blows past the 100 cap
        bot.state["positions"]["BTCUSD"] = {"side": "buy", "qty": 1000, "entry": 13000}
        bot.close_position(delta, "BTCUSD", reason="trip")
        assert bot.circuit_breaker_active() is True
        # and a fresh confirm is refused with no order reaching the exchange
        tr = bot.new_trade_dict()
        tr.update(symbol="BTCUSD", side="buy", qty=1, market=True,
                  sl_type="fixed", sl_price=11500, tp_mode="none")
        tok = bot.store_pending(tr)
        before = len(server.orders)
        reply = bot.on_confirm(delta, tg, tok, CHAT, 0)
        assert "Circuit breaker" in reply, reply
        assert len(server.orders) == before, "must not place once breaker tripped"
        assert tok not in bot.state["pending"]
    finally:
        bot.MAX_DAILY_LOSS_USDT = orig


@test("PnL: partial TP-leg fill realizes PnL at leg price (no trade counted) & no double count on close")
def t_partial_tp_fill_win():
    delta, tg, server = fresh()
    # BTCUSD contracting 0.001, mark 12345.6
    bot.state["positions"]["BTCUSD"] = {
        "side": "buy", "qty": 500, "entry": 12000, "tp_mode": "scale",
        "tp_legs": [{"price": 13000, "qty": 300, "pct": 60, "filled": False},
                    {"price": 14000, "qty": 200, "pct": 40, "filled": False}],
        "realized_qty": 0,
    }
    server.positions["BTCUSD"] = 200          # 300 closed → leg1 filled at 13000
    bot.monitor_positions_once(delta)
    s = bot.state["stats"]; pos = bot.state["positions"]["BTCUSD"]
    # leg1: (13000-12000)*300*0.001 = +300 ; partial → counts a win = False
    assert abs(s["realized_pnl"] - 300.0) < 1e-6, s
    assert s["wins"] == 0 and s["losses"] == 0, s
    assert pos["realized_qty"] == 300 and pos["tp_legs"][0]["filled"]
    assert not pos["tp_legs"][1].get("filled")
    plines = [ln for ln in bot.JOURNAL_FILE.read_text(encoding="utf-8").splitlines()
              if "partial_tp_filled" in ln]
    assert plines and "300" in plines[-1], plines

    # now flat on the exchange → only the residual 200 is realized at mark price
    server.positions["BTCUSD"] = 0
    bot.monitor_positions_once(delta)
    # residual: (12345.6-12000)*200*0.001 = +69.12 ; total 369.12, one win
    assert abs(s["realized_pnl"] - 369.12) < 1e-4, s
    assert s["wins"] == 1 and s["losses"] == 0, s
    assert "BTCUSD" not in bot.state["positions"]


@test("PnL: partial TP-leg loss accrues daily_loss (circuit breaker) without counting a loss")
def t_partial_tp_fill_loss():
    orig = bot.MAX_DAILY_LOSS_USDT
    try:
        bot.MAX_DAILY_LOSS_USDT = 1000.0
        delta, tg, server = fresh()
        bot.state["positions"]["BTCUSD"] = {
            "side": "buy", "qty": 500, "entry": 13000, "tp_mode": "scale",
            "tp_legs": [{"price": 12000, "qty": 400, "pct": 80, "filled": False},
                        {"price": 11000, "qty": 100, "pct": 20, "filled": False}],
            "realized_qty": 0,
        }
        server.positions["BTCUSD"] = 100      # 400 closed → leg1 filled at 12000
        bot.monitor_positions_once(delta)
        s = bot.state["stats"]
        # (12000-13000)*400*0.001 = -400 → daily_loss +400, losses counter untouched
        assert abs(s["daily_loss"] - 400.0) < 1e-6, s
        assert s["losses"] == 0 and s["wins"] == 0, s
        assert abs(s["realized_pnl"] - (-400.0)) < 1e-6, s
        assert bot.circuit_breaker_active() is False          # 400 < 1000
        bot.MAX_DAILY_LOSS_USDT = 100.0
        assert bot.circuit_breaker_active() is True           # realized partial loss trips it
    finally:
        bot.MAX_DAILY_LOSS_USDT = orig


@test("PnL: manual close after a partial leg realizes only the residual qty")
def t_close_after_partial():
    delta, tg, server = fresh()
    bot.state["positions"]["BTCUSD"] = {
        "side": "buy", "qty": 500, "entry": 12000, "tp_mode": "scale",
        "tp_legs": [{"price": 13000, "qty": 300, "pct": 60, "filled": True},
                    {"price": 14000, "qty": 200, "pct": 40, "filled": False}],
        "realized_qty": 300,
    }
    bot.state["stats"]["realized_pnl"] = 300.0     # the already-booked leg1
    bot.close_position(delta, "BTCUSD", reason="manual")
    s = bot.state["stats"]
    # residual 200 @ mark 12345.6 → +69.12 ; total 369.12, one win, no daily loss
    assert abs(s["realized_pnl"] - 369.12) < 1e-4, s
    assert s["wins"] == 1 and s["daily_loss"] == 0.0, s
    assert "BTCUSD" not in bot.state["positions"]


@test("validate_trade blocks zero/negative size and bad scale-out legs")
def t_validate_trade():
    assert "0 contracts" in bot.validate_trade({"qty": 0})
    assert bot.validate_trade({"qty": -5})
    assert "whole number" in bot.validate_trade({"qty": None})
    assert bot.validate_trade({"qty": 5}) is None
    # legs sum above position size
    assert bot.validate_trade({"qty": 5, "tp_mode": "scale",
                               "tp_legs": [{"qty": 4}, {"qty": 4}]})
    # a zero-size leg
    assert bot.validate_trade({"qty": 5, "tp_mode": "scale",
                               "tp_legs": [{"qty": 5}, {"qty": 0}]})
    # valid 3+2 split
    assert bot.validate_trade({"qty": 5, "tp_mode": "scale",
                               "tp_legs": [{"qty": 3}, {"qty": 2}]}) is None


@test("on_confirm blocks a zero-size order without sending it to Delta")
def t_confirm_blocks_zero_size():
    delta, tg, server = fresh()
    tr = bot.new_trade_dict()
    tr.update(symbol="BTCUSD", side="buy", qty=0, market=True,
              sl_type="fixed", sl_price=11500, tp_mode="none")
    tok = bot.store_pending(tr)
    reply = bot.on_confirm(delta, tg, tok, CHAT, 0)
    assert "0 contracts" in reply, reply
    assert server.orders == [], "must not POST an invalid size"
    assert tok not in bot.state["pending"]


@test("App-like guided flow: market info + LIMIT order + custom leverage")
def t_guided_limit_and_leverage():
    delta, tg, server = fresh()
    msg(delta, tg, "/trade")
    bot.handle_guided_text(delta, tg, CHAT, "BTCUSD")
    # market-info card shown after the symbol
    mi = next((t for t in tg.texts() if "— Futures" in t), None)
    assert mi and "Last/Mark" in mi, tg.texts()
    cb(delta, tg, "side:buy")            # -> order_type
    assert any("Order type?" in t for t in tg.texts())
    cb(delta, tg, "ot:limit")            # -> limit_price
    bot.handle_guided_text(delta, tg, CHAT, "80000")   # -> leverage
    assert any("Set leverage" in t for t in tg.texts())
    cb(delta, tg, "lev:custom")          # -> leverage_input
    bot.handle_guided_text(delta, tg, CHAT, "25")      # -> sizing
    cb(delta, tg, "sizing:fixed")
    bot.handle_guided_text(delta, tg, CHAT, "10")      # qty
    cb(delta, tg, "slmode:fixed")
    bot.handle_guided_text(delta, tg, CHAT, "78000")   # sl
    cb(delta, tg, "tpmode:none")                        # -> confirm

    token, mid = tg.find_keyboard("cfm:")
    assert token, "expected confirm card"
    tr = bot.state["pending"][token]
    assert tr["market"] is False and tr["entry"] == 80000.0
    assert tr["leverage"] == 25 and tr["qty"] == 10
    card = next(m for m in reversed(tg.sent)
                if any(b.get("callback_data", "").endswith(token)
                       for row in (m.get("keyboard") or []) for b in row))
    assert "Order: Limit @ `80000.0`" in card["text"], card["text"]
    assert "Leverage: `25x`" in card["text"], card["text"]


# ─────────────────────────────────────────────────────────────────────────
def main():
    print("=" * 68)
    print("  delta_telegram_bot.py — offline testnet test harness")
    print("=" * 68)
    # point state + journal into the temp dir for the run
    bot.STATE_FILE = Path(os.environ["DELTA_STATE_FILE"])
    bot.JOURNAL_FILE = Path(os.environ["DELTA_JOURNAL_FILE"])
    if bot.STATE_FILE.exists():
        bot.STATE_FILE.unlink()
    if bot.JOURNAL_FILE.exists():
        bot.JOURNAL_FILE.unlink()

    # run all @test-registered cases in definition order
    for name, ok, err in RESULTS:
        pass

    # RESULTS is filled by decorators as functions were defined; ensure execution
    # happened at import. (Tests run at definition time via the decorator.)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, e) for n, ok, e in RESULTS if not ok]

    report = io.StringIO()
    report.write("Delta Telegram Bot — testnet test log\n")
    report.write(f"generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    report.write(f"network under test: {bot.DELTA_BASE_URL} ({bot.NETWORK_LABEL})\n")
    report.write("=" * 60 + "\n\n")
    report.write(LOG.getvalue())
    report.write("\n" + "-" * 60 + "\n")
    report.write(f"TOTAL: {len(RESULTS)}  PASS: {passed}  FAIL: {len(failed)}\n")
    for n, e in failed:
        report.write(f"  FAIL {n}: {e}\n")

    Path("testnet_test_log.txt").write_text(report.getvalue(), encoding="utf-8")
    print(LOG.getvalue())
    print(f"\nTOTAL {len(RESULTS)}  PASS {passed}  FAIL {len(failed)}")
    for n, e in failed:
        print(f"  FAIL {n}: {e}")
    print("log -> testnet_test_log.txt")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
