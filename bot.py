"""
JARVIS TRADE BOT — Autonomous crypto trading agent for OKX
Dollar-based Take Profit + Flask API for dashboard
"""

import os, time, hmac, hashlib, base64, json, logging, requests, threading
from datetime import datetime, timezone
from anthropic import Anthropic
from flask import Flask, jsonify
from flask_cors import CORS

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
OKX_API_KEY     = os.environ["OKX_API_KEY"]
OKX_SECRET_KEY  = os.environ["OKX_SECRET_KEY"]
OKX_PASSPHRASE  = os.environ["OKX_PASSPHRASE"]
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]

IS_DEMO         = os.environ.get("OKX_DEMO", "true").lower() == "true"
RISK_PCT        = float(os.environ.get("RISK_PCT", "1.0"))
INTERVAL_MIN    = int(os.environ.get("INTERVAL_MIN", "60"))
TAKE_USD        = float(os.environ.get("TAKE_USD", "5.0"))
STOP_USD        = float(os.environ.get("STOP_USD", "1.0"))
MONITOR_SEC     = int(os.environ.get("MONITOR_SEC", "10"))
SYMBOLS         = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]

BASE_URL    = "https://www.okx.com"
DEMO_HDR    = {"x-simulated-trading": "1"} if IS_DEMO else {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler("jarvis.log")]
)
log = logging.getLogger("JARVIS")

# Shared state for dashboard
state = {
    "balance": 0.0,
    "total_pnl": 0.0,
    "positions": [],
    "total_taken": 0.0,
    "take_count": 0,
    "last_signal": None,
    "last_update": None,
}

# ─────────────────────────────────────────
# SYSTEM PROMPT
# ─────────────────────────────────────────
SYSTEM_PROMPT = """Ты — институциональный крипто-трейдинг аналитик для OKX.
Ищи только высоковероятностные intraday сделки BTC/USDT и ETH/USDT.
Принцип: лучше 10 раз NO TRADE, чем один слабый сигнал.
Анализируй: market structure (BOS/CHoCH/HH/HL), liquidity sweep, order blocks, FVG,
volume delta, CVD, OI, funding rate, long/short ratio, ATR, volatility regime.
Сигнал ТОЛЬКО если все факторы совпали: тренд + объём + ликвидность + price action + RR >= 1:2.
Отвечай ТОЛЬКО валидным JSON без markdown:
{"decision":"LONG"|"SHORT"|"NO TRADE","symbol":"BTC-USDT-SWAP"|"ETH-USDT-SWAP"|null,"entry_zone":число|null,"stop_loss":число|null,"take_profit_1":число|null,"leverage":3,"confidence":"LOW"|"MEDIUM"|"HIGH","reason":"текст","final_verdict":"ENTER"|"WAIT"|"NO TRADE"}"""

# ─────────────────────────────────────────
# OKX AUTH
# ─────────────────────────────────────────
def sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(OKX_SECRET_KEY.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def headers(method, path, body=""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    h = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }
    h.update(DEMO_HDR)
    return h

def okx_get(path):
    r = requests.get(BASE_URL + path, headers=headers("GET", path), timeout=10)
    return r.json()

def okx_post(path, body):
    b = json.dumps(body)
    r = requests.post(BASE_URL + path, headers=headers("POST", path, b), data=b, timeout=10)
    return r.json()

# ─────────────────────────────────────────
# MARKET DATA
# ─────────────────────────────────────────
def candles(symbol, bar="1H", limit=24):
    r = requests.get(f"{BASE_URL}/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}", timeout=10)
    data = r.json().get("data", [])
    return [{"t": c[0], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
            for c in reversed(data)]

def ticker(symbol):
    r = requests.get(f"{BASE_URL}/api/v5/market/ticker?instId={symbol}", timeout=10)
    t = r.json().get("data", [{}])[0]
    return {"last": float(t.get("last", 0)), "vol24h": float(t.get("vol24h", 0)),
            "high": float(t.get("high24h", 0)), "low": float(t.get("low24h", 0))}

def funding(symbol):
    r = requests.get(f"{BASE_URL}/api/v5/public/funding-rate?instId={symbol}", timeout=10)
    d = r.json().get("data", [{}])[0]
    return float(d.get("fundingRate", 0))

def build_market_data(symbol):
    lines = [f"=== {symbol} | {datetime.utcnow().strftime('%H:%M UTC')} ==="]
    try:
        t = ticker(symbol)
        lines += [f"Price: ${t['last']:,.2f}", f"Vol24h: {t['vol24h']:,.0f}", f"H/L: {t['high']:,.2f} / {t['low']:,.2f}"]
    except: pass
    try:
        fr = funding(symbol)
        lines.append(f"Funding: {fr*100:.4f}%")
    except: pass
    try:
        c1h = candles(symbol, "1H", 24)
        lines.append("1H (last 8):")
        for c in c1h[-8:]:
            dt = datetime.fromtimestamp(int(c["t"])/1000, tz=timezone.utc).strftime("%H:%M")
            lines.append(f"  {dt} O:{c['o']:.1f} H:{c['h']:.1f} L:{c['l']:.1f} C:{c['c']:.1f} V:{c['v']:.0f}")
        closes = [c["c"] for c in c1h]
        lines.append(f"  Trend1H: {'BULL' if closes[-1] > closes[0] else 'BEAR'}")
        atr = sum(c["h"] - c["l"] for c in c1h[-14:]) / 14
        lines.append(f"  ATR14: {atr:.2f}")
    except: pass
    try:
        c15 = candles(symbol, "15m", 16)
        lines.append("15M (last 6):")
        for c in c15[-6:]:
            dt = datetime.fromtimestamp(int(c["t"])/1000, tz=timezone.utc).strftime("%H:%M")
            lines.append(f"  {dt} O:{c['o']:.1f} H:{c['h']:.1f} L:{c['l']:.1f} C:{c['c']:.1f}")
    except: pass
    return "\n".join(lines)

# ─────────────────────────────────────────
# ACCOUNT
# ─────────────────────────────────────────
def get_balance():
    data = okx_get("/api/v5/account/balance?ccy=USDT")
    for d in data.get("data", [{}])[0].get("details", []):
        if d.get("ccy") == "USDT":
            return float(d.get("availEq", 0))
    return 0.0

def get_position(symbol):
    data = okx_get(f"/api/v5/account/positions?instType=SWAP&instId={symbol}")
    for p in data.get("data", []):
        if float(p.get("pos", 0)) != 0:
            return p
    return None

def close_position(symbol, pos):
    pos_size = abs(float(pos.get("pos", 0)))
    pos_side = pos.get("posSide", "long")
    close_side = "sell" if pos_side == "long" else "buy"
    result = okx_post("/api/v5/trade/order", {
        "instId": symbol, "tdMode": "cross", "side": close_side,
        "posSide": pos_side, "ordType": "market", "sz": str(pos_size), "reduceOnly": True,
    })
    log.info(f"Close result: {result}")
    return result.get("code") == "0"

# ─────────────────────────────────────────
# PLACE ORDER
# ─────────────────────────────────────────
def place_order(signal, balance):
    symbol   = signal.get("symbol")
    decision = signal.get("decision")
    leverage = signal.get("leverage", 3)
    sl       = signal.get("stop_loss")
    entry    = signal.get("entry_zone")
    if not all([symbol, decision, sl, entry]): return False
    entry, sl = float(entry), float(sl)
    sl_dist = abs(entry - sl)
    if sl_dist == 0: return False
    risk_usdt = balance * (RISK_PCT / 100)
    contract_sz = 0.01 if "BTC" in symbol else 0.1
    contracts = max(1, round(risk_usdt / (sl_dist * contract_sz * leverage)))
    side = "buy" if decision == "LONG" else "sell"
    pos_side = "long" if decision == "LONG" else "short"
    okx_post("/api/v5/account/set-leverage", {"instId": symbol, "lever": str(leverage), "mgnMode": "cross"})
    result = okx_post("/api/v5/trade/order", {
        "instId": symbol, "tdMode": "cross", "side": side,
        "posSide": pos_side, "ordType": "market", "sz": str(contracts),
    })
    log.info(f"Order result: {result}")
    return result.get("code") == "0"

# ─────────────────────────────────────────
# PNL MONITOR
# ─────────────────────────────────────────
def pnl_monitor():
    log.info(f"💹 Monitor: TP=+${TAKE_USD} SL=-${STOP_USD} every {MONITOR_SEC}s")
    while True:
        try:
            balance = get_balance()
            state["balance"] = balance
            positions_list = []
            total_pnl = 0.0

            for symbol in SYMBOLS:
                pos = get_position(symbol)
                if not pos:
                    continue
                pnl = float(pos.get("upl", 0))
                total_pnl += pnl
                positions_list.append({
                    "symbol": symbol,
                    "side": pos.get("posSide"),
                    "size": pos.get("pos"),
                    "pnl": round(pnl, 2),
                    "entry": pos.get("avgPx"),
                })
                log.info(f"📊 {symbol} PnL: ${pnl:+.2f}")

                if pnl >= TAKE_USD:
                    log.info(f"💰 TAKE PROFIT ${pnl:.2f} — closing")
                    if close_position(symbol, pos):
                        state["total_taken"] = round(state["total_taken"] + pnl, 2)
                        state["take_count"] += 1
                elif pnl <= -STOP_USD:
                    log.info(f"🛑 STOP LOSS ${pnl:.2f} — closing")
                    close_position(symbol, pos)

            state["positions"] = positions_list
            state["total_pnl"] = round(total_pnl, 2)
            state["last_update"] = datetime.utcnow().strftime("%H:%M:%S UTC")

        except Exception as e:
            log.error(f"Monitor error: {e}")
        time.sleep(MONITOR_SEC)

# ─────────────────────────────────────────
# CLAUDE ANALYSIS
# ─────────────────────────────────────────
def analyze(market_data):
    client = Anthropic(api_key=ANTHROPIC_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": market_data}],
    )
    text = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)

# ─────────────────────────────────────────
# TRADE LOOP
# ─────────────────────────────────────────
def trade_loop():
    log.info(f"🤖 Trade loop | Demo={IS_DEMO} | interval={INTERVAL_MIN}min")
    time.sleep(15)
    while True:
        try:
            log.info("=" * 60)
            log.info(f"⏱  Cycle: {datetime.utcnow().strftime('%H:%M UTC')}")
            balance = get_balance()
            log.info(f"💰 Balance: ${balance:,.2f} USDT")
            if balance < 10:
                log.warning("Balance < $10, skipping")
            else:
                market_block = ""
                for symbol in SYMBOLS:
                    if get_position(symbol):
                        log.info(f"📊 Position open for {symbol}, monitor handles it")
                        continue
                    market_block += build_market_data(symbol) + "\n\n"
                if market_block.strip():
                    log.info("🧠 Analyzing with Claude...")
                    signal = analyze(market_block)
                    state["last_signal"] = signal
                    log.info(f"📡 {signal.get('decision')} | {signal.get('final_verdict')} | {signal.get('reason','')}")
                    if signal.get("final_verdict") == "ENTER" and signal.get("decision") in ("LONG", "SHORT"):
                        log.info(f"✅ Placing {signal['decision']} on {signal.get('symbol')}")
                        place_order(signal, balance)
                    else:
                        log.info(f"⏸  {signal.get('final_verdict')} — skipping")
        except Exception as e:
            log.error(f"❌ {e}", exc_info=True)
        log.info(f"💤 Next analysis in {INTERVAL_MIN} min")
        time.sleep(INTERVAL_MIN * 60)

# ─────────────────────────────────────────
# FLASK API FOR DASHBOARD
# ─────────────────────────────────────────
app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return "JARVIS TRADE BOT is running 🤖"

@app.route("/pnl")
def pnl_endpoint():
    return jsonify({
        "balance": state["balance"],
        "total_pnl": state["total_pnl"],
        "positions": state["positions"],
        "total_taken": state["total_taken"],
        "take_count": state["take_count"],
        "last_signal": state["last_signal"],
        "last_update": state["last_update"],
        "mode": "DEMO" if IS_DEMO else "REAL",
        "take_usd": TAKE_USD,
        "stop_usd": STOP_USD,
    })

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=pnl_monitor, daemon=True).start()
    threading.Thread(target=trade_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log.info(f"🌐 API running on port {port}")
    app.run(host="0.0.0.0", port=port)
