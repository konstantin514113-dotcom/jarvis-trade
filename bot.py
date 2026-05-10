import os, time, hmac, hashlib, base64, json, logging, requests, threading
from datetime import datetime, timezone
from anthropic import Anthropic
from flask import Flask, jsonify
from flask_cors import CORS

OKX_API_KEY  = os.environ["OKX_API_KEY"]
OKX_SECRET   = os.environ["OKX_SECRET_KEY"]
OKX_PASS     = os.environ["OKX_PASSPHRASE"]
ANT_KEY      = os.environ["ANTHROPIC_API_KEY"]
IS_DEMO      = os.environ.get("OKX_DEMO", "true").lower() == "true"
RISK_PCT     = float(os.environ.get("RISK_PCT", "1.0"))
INTERVAL_MIN = int(os.environ.get("INTERVAL_MIN", "60"))
TAKE_USD     = float(os.environ.get("TAKE_USD", "5.0"))
STOP_USD     = float(os.environ.get("STOP_USD", "1.0"))
MONITOR_SEC  = int(os.environ.get("MONITOR_SEC", "10"))
SYMBOLS      = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
BASE_URL     = "https://www.okx.com"
DEMO_HDR     = {"x-simulated-trading": "1"} if IS_DEMO else {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("JARVIS")

state = {
    "balance": 0.0,
    "total_pnl": 0.0,
    "positions": [],
    "total_taken": 0.0,
    "take_count": 0,
    "last_signal": None,
    "last_update": None,
}

SYSTEM_PROMPT = """You are an institutional crypto trading analyst for OKX.
Find only high-probability intraday trades for BTC/USDT and ETH/USDT.
Principle: better 10 times NO TRADE than one weak signal.
Analyze: market structure (BOS/CHoCH/HH/HL), liquidity sweep, order blocks, FVG,
volume delta, CVD, OI, funding rate, long/short ratio, ATR, volatility regime.
Signal ONLY if all factors align: trend + volume + liquidity + price action + RR >= 1:2.
Reply ONLY with valid JSON, no markdown:
{"decision":"LONG or SHORT or NO TRADE","symbol":"BTC-USDT-SWAP or ETH-USDT-SWAP or null","entry_zone":number_or_null,"stop_loss":number_or_null,"take_profit_1":number_or_null,"leverage":3,"confidence":"LOW or MEDIUM or HIGH","reason":"brief reason in english","final_verdict":"ENTER or WAIT or NO TRADE"}"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JARVIS PnL</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@800&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background:#080b0f; color:#c9d1d9; font-family:'JetBrains Mono',monospace; min-height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; padding:32px 16px; gap:24px; }
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
@keyframes pop{0%{transform:scale(1)}40%{transform:scale(1.05)}100%{transform:scale(1)}}
.title{font-family:'Syne',sans-serif;font-size:22px;font-weight:800;color:#fff;letter-spacing:3px;text-align:center}
.subtitle{font-size:10px;color:#1e2e3a;letter-spacing:2px;text-align:center}
.balance{font-size:12px;color:#37474f}
.balance span{color:#58a6ff}
.pnl-label{font-size:10px;letter-spacing:3px;color:#1e3a2a;text-align:center;margin-bottom:10px}
.pnl-value{font-family:'Syne',sans-serif;font-size:clamp(36px,12vw,56px);font-weight:800;letter-spacing:-1px;line-height:1;text-align:center;transition:color 0.3s}
.pnl-sub{font-size:10px;letter-spacing:3px;text-align:center;margin-top:6px}
.position-card{width:100%;max-width:320px;background:#0d1117;border-radius:8px;padding:10px 14px;display:flex;justify-content:space-between;align-items:center}
.pos-symbol{font-size:12px;color:#eceff1;font-weight:700}
.pos-side{font-size:10px;letter-spacing:1px}
.pos-pnl{font-size:14px;font-weight:700}
.saved-box{border-radius:14px;padding:16px 40px;text-align:center;transition:all 0.3s;min-width:220px}
.saved-label{font-size:10px;letter-spacing:3px;margin-bottom:6px}
.saved-value{font-family:'Syne',sans-serif;font-size:36px;font-weight:800;letter-spacing:-1px}
.saved-count{font-size:10px;letter-spacing:2px;margin-top:4px}
.signal-box{width:100%;max-width:320px;background:#0d1117;border:1px solid #161b22;border-radius:10px;padding:12px 16px}
.signal-label{font-size:10px;color:#30363d;letter-spacing:2px;margin-bottom:6px}
.signal-decision{font-size:12px;font-weight:700;letter-spacing:2px}
.signal-reason{font-size:11px;color:#455a64;line-height:1.4;margin-top:4px}
.live-dot{width:6px;height:6px;border-radius:50%;display:inline-block;animation:pulse 1.5s infinite;margin-right:6px}
.live-row{display:flex;align-items:center;font-size:10px;letter-spacing:2px}
.error{font-size:11px;color:#ef9a9a;text-align:center}
.history-box{width:100%;max-width:300px;background:#0d1117;border:1px solid #161b22;border-radius:12px;overflow:hidden}
.history-header{padding:8px 16px;border-bottom:1px solid #161b22;font-size:10px;color:#30363d;letter-spacing:2px}
.history-list{max-height:150px;overflow-y:auto}
.history-item{display:flex;justify-content:space-between;align-items:center;padding:7px 16px;border-bottom:1px solid #0d1117;font-size:11px}
.history-ts{color:#30363d}
.history-amt{color:#00e676;font-weight:700}
</style>
</head>
<body>
<div>
  <div class="title">JARVIS · PnL</div>
  <div class="subtitle" id="mode-label">... · OKX · обновление каждые 5с</div>
</div>
<div class="balance">Баланс: <span id="balance-val">—</span></div>
<div>
  <div class="pnl-label" id="pos-label">НЕТ ПОЗИЦИЙ</div>
  <div class="pnl-value" id="pnl-value" style="color:#00e676">+$0.00</div>
  <div class="pnl-sub" id="pnl-sub" style="color:#00e67655">ОЖИДАНИЕ СИГНАЛА</div>
</div>
<div id="positions-container" style="width:100%;max-width:320px;display:flex;flex-direction:column;gap:8px"></div>
<div class="saved-box" id="saved-box" style="background:#0d1a10;border:1px solid #00e67622">
  <div class="saved-label" style="color:#1e4a2a">ОТЛОЖЕНО</div>
  <div class="saved-value" id="saved-value" style="color:#ffea00;text-shadow:0 0 20px #ffea0033">$0.00</div>
  <div class="saved-count" id="saved-count" style="color:#3a4a1a">0 фиксаций × $5</div>
</div>
<div id="history-box" class="history-box" style="display:none">
  <div class="history-header">ИСТОРИЯ ФИКСАЦИЙ</div>
  <div class="history-list" id="history-list"></div>
</div>
<div id="signal-box" class="signal-box" style="display:none">
  <div class="signal-label">ПОСЛЕДНИЙ СИГНАЛ</div>
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
    <span class="signal-decision" id="signal-decision"></span>
    <span style="font-size:10px;color:#37474f" id="signal-confidence"></span>
  </div>
  <div class="signal-reason" id="signal-reason"></div>
</div>
<div id="error-box" class="error" style="display:none"></div>
<div class="live-row">
  <span class="live-dot" id="live-dot" style="background:#00e676"></span>
  <span id="live-label" style="color:#1e3a2a">LIVE</span>
  <span id="update-time" style="color:#1e2e3a;margin-left:8px;font-size:10px"></span>
</div>
<script>
const API='/pnl';
const TAKE_AT=5;
let prevPnl=0;
let history=[];

async function fetchData(){
  try{
    const res=await fetch(API);
    const d=await res.json();
    document.getElementById('mode-label').textContent=(d.mode||'DEMO')+' · OKX · обновление каждые 5с';
    document.getElementById('balance-val').textContent='$'+(d.balance||0).toFixed(2)+' USDT';
    const pnl=d.total_pnl||0;
    const up=pnl>=0;
    const pnlEl=document.getElementById('pnl-value');
    pnlEl.textContent=(up?'+':'-')+'$'+Math.abs(pnl).toFixed(2);
    pnlEl.style.color=up?'#00e676':'#ff1744';
    pnlEl.style.textShadow=up?'0 0 40px #00e67633':'0 0 40px #ff174433';
    const positions=d.positions||[];
    document.getElementById('pos-label').textContent=positions.length>0?'ОТКРЫТО: '+positions.length:'НЕТ ПОЗИЦИЙ';
    document.getElementById('pnl-sub').textContent=positions.length>0?(up?'▲ В ПЛЮСЕ':'▼ В МИНУСЕ'):'ОЖИДАНИЕ СИГНАЛА';
    document.getElementById('pnl-sub').style.color=up?'#00e67655':'#ff174455';
    const posContainer=document.getElementById('positions-container');
    posContainer.innerHTML='';
    positions.forEach(p=>{
      const pup=p.pnl>=0;
      const card=document.createElement('div');
      card.className='position-card';
      card.style.border='1px solid '+(pup?'#00e67622':'#ff174422');
      card.innerHTML='<div><div class="pos-symbol">'+p.symbol+'</div><div class="pos-side" style="color:'+(p.side==='long'?'#00e676':'#ff1744')+'">'+((p.side||'').toUpperCase())+' · '+Math.abs(parseFloat(p.size||0))+' контр.</div></div><div class="pos-pnl" style="color:'+(pup?'#00e676':'#ff1744')+'">'+(pup?'+':'')+'$'+p.pnl.toFixed(2)+'</div>';
      posContainer.appendChild(card);
    });
    const totalTaken=d.total_taken||0;
    const takeCount=d.take_count||0;
    document.getElementById('saved-value').textContent='$'+totalTaken.toFixed(2);
    document.getElementById('saved-count').textContent=takeCount+' фиксаций × $'+(d.take_usd||5);
    if(pnl>=TAKE_AT&&prevPnl<TAKE_AT){
      const box=document.getElementById('saved-box');
      box.style.background='#ffea0011';
      box.style.border='1px solid #ffea0055';
      setTimeout(()=>{box.style.background='#0d1a10';box.style.border='1px solid #00e67622';},800);
    }
    prevPnl=pnl;
    if(d.last_signal){
      const s=d.last_signal;
      document.getElementById('signal-box').style.display='block';
      const decEl=document.getElementById('signal-decision');
      decEl.textContent=s.decision||'';
      decEl.style.color=s.decision==='LONG'?'#00e676':s.decision==='SHORT'?'#ff1744':'#37474f';
      document.getElementById('signal-confidence').textContent=s.confidence||'';
      document.getElementById('signal-reason').textContent=s.reason||'';
    }
    document.getElementById('live-dot').style.background='#00e676';
    document.getElementById('live-label').style.color='#1e3a2a';
    document.getElementById('live-label').textContent='LIVE';
    document.getElementById('update-time').textContent=d.last_update||'';
    document.getElementById('error-box').style.display='none';
  }catch(e){
    document.getElementById('live-dot').style.background='#ff1744';
    document.getElementById('live-label').style.color='#ff174488';
    document.getElementById('live-label').textContent='OFFLINE';
    document.getElementById('error-box').style.display='block';
    document.getElementById('error-box').textContent='Нет связи с ботом';
  }
}
fetchData();
setInterval(fetchData,5000);
</script>
</body>
</html>"""

def sign(ts, method, path, body=""):
    msg = ts + method.upper() + path + body
    mac = hmac.new(OKX_SECRET.encode(), msg.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def get_headers(method, path, body=""):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    h = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_PASS,
        "Content-Type": "application/json",
    }
    h.update(DEMO_HDR)
    return h

def okx_get(path):
    r = requests.get(BASE_URL + path, headers=get_headers("GET", path), timeout=10)
    return r.json()

def okx_post(path, body):
    b = json.dumps(body)
    r = requests.post(BASE_URL + path, headers=get_headers("POST", path, b), data=b, timeout=10)
    return r.json()

def get_candles(symbol, bar="1H", limit=24):
    r = requests.get(f"{BASE_URL}/api/v5/market/candles?instId={symbol}&bar={bar}&limit={limit}", timeout=10)
    data = r.json().get("data", [])
    return [{"t": c[0], "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "v": float(c[5])}
            for c in reversed(data)]

def get_ticker(symbol):
    r = requests.get(f"{BASE_URL}/api/v5/market/ticker?instId={symbol}", timeout=10)
    t = r.json().get("data", [{}])[0]
    return {"last": float(t.get("last", 0)), "vol24h": float(t.get("vol24h", 0)),
            "high": float(t.get("high24h", 0)), "low": float(t.get("low24h", 0))}

def get_funding(symbol):
    r = requests.get(f"{BASE_URL}/api/v5/public/funding-rate?instId={symbol}", timeout=10)
    d = r.json().get("data", [{}])[0]
    return float(d.get("fundingRate", 0))

def build_market(symbol):
    lines = [f"=== {symbol} | {datetime.utcnow().strftime('%H:%M UTC')} ==="]
    try:
        t = get_ticker(symbol)
        lines += [f"Price: ${t['last']:,.2f}", f"Vol24h: {t['vol24h']:,.0f}", f"H/L: {t['high']:,.2f}/{t['low']:,.2f}"]
    except: pass
    try:
        lines.append(f"Funding: {get_funding(symbol)*100:.4f}%")
    except: pass
    try:
        c1h = get_candles(symbol, "1H", 24)
        lines.append("1H candles (last 8):")
        for c in c1h[-8:]:
            dt = datetime.fromtimestamp(int(c["t"])/1000, tz=timezone.utc).strftime("%H:%M")
            lines.append(f"  {dt} O:{c['o']:.1f} H:{c['h']:.1f} L:{c['l']:.1f} C:{c['c']:.1f} V:{c['v']:.0f}")
        closes = [c["c"] for c in c1h]
        atr = sum(c["h"] - c["l"] for c in c1h[-14:]) / 14
        lines.append(f"  Trend: {'BULL' if closes[-1] > closes[0] else 'BEAR'} | ATR14: {atr:.2f}")
    except: pass
    try:
        c15 = get_candles(symbol, "15m", 16)
        lines.append("15M candles (last 6):")
        for c in c15[-6:]:
            dt = datetime.fromtimestamp(int(c["t"])/1000, tz=timezone.utc).strftime("%H:%M")
            lines.append(f"  {dt} O:{c['o']:.1f} H:{c['h']:.1f} L:{c['l']:.1f} C:{c['c']:.1f}")
    except: pass
    return "\n".join(lines)

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
    log.info(f"Close: {result}")
    return result.get("code") == "0"

def place_order(signal, balance):
    symbol = signal.get("symbol")
    decision = signal.get("decision")
    leverage = signal.get("leverage", 3)
    sl = signal.get("stop_loss")
    entry = signal.get("entry_zone")
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
    log.info(f"Order: {result}")
    return result.get("code") == "0"

def pnl_monitor():
    log.info(f"Monitor started | TP=+${TAKE_USD} | SL=-${STOP_USD} | every {MONITOR_SEC}s")
    while True:
        try:
            balance = get_balance()
            state["balance"] = balance
            positions_list = []
            total_pnl = 0.0
            for symbol in SYMBOLS:
                pos = get_position(symbol)
                if not pos: continue
                pnl = float(pos.get("upl", 0))
                total_pnl += pnl
                positions_list.append({
                    "symbol": symbol,
                    "side": pos.get("posSide"),
                    "size": pos.get("pos"),
                    "pnl": round(pnl, 2),
                    "entry": pos.get("avgPx"),
                })
                log.info(f"{symbol} PnL: ${pnl:+.2f}")
                if pnl >= TAKE_USD:
                    log.info(f"TAKE PROFIT ${pnl:.2f} - closing")
                    if close_position(symbol, pos):
                        state["total_taken"] = round(state["total_taken"] + pnl, 2)
                        state["take_count"] += 1
                elif pnl <= -STOP_USD:
                    log.info(f"STOP LOSS ${pnl:.2f} - closing")
                    close_position(symbol, pos)
            state["positions"] = positions_list
            state["total_pnl"] = round(total_pnl, 2)
            state["last_update"] = datetime.utcnow().strftime("%H:%M:%S UTC")
        except Exception as e:
            log.error(f"Monitor error: {e}")
        time.sleep(MONITOR_SEC)

def analyze(market_data):
    client = Anthropic(api_key=ANT_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": market_data}],
    )
    text = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)

def trade_loop():
    log.info(f"Trade loop | Demo={IS_DEMO} | interval={INTERVAL_MIN}min")
    time.sleep(15)
    while True:
        try:
            log.info("=" * 40)
            log.info(f"Cycle: {datetime.utcnow().strftime('%H:%M UTC')}")
            balance = get_balance()
            log.info(f"Balance: ${balance:,.2f} USDT")
            if balance < 10:
                log.warning("Balance < $10, skipping")
            else:
                market_block = ""
                for symbol in SYMBOLS:
                    if get_position(symbol):
                        log.info(f"Position open for {symbol}")
                        continue
                    market_block += build_market(symbol) + "\n\n"
                if market_block.strip():
                    log.info("Analyzing with Claude...")
                    signal = analyze(market_block)
                    state["last_signal"] = signal
                    log.info(f"{signal.get('decision')} | {signal.get('final_verdict')} | {signal.get('reason','')}")
                    if signal.get("final_verdict") == "ENTER" and signal.get("decision") in ("LONG", "SHORT"):
                        log.info(f"Placing {signal['decision']} on {signal.get('symbol')}")
                        place_order(signal, balance)
                    else:
                        log.info(f"{signal.get('final_verdict')} - skipping")
        except Exception as e:
            log.error(f"Error: {e}", exc_info=True)
        log.info(f"Next in {INTERVAL_MIN} min")
        time.sleep(INTERVAL_MIN * 60)

app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return DASHBOARD_HTML

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

if __name__ == "__main__":
    threading.Thread(target=pnl_monitor, daemon=True).start()
    threading.Thread(target=trade_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    log.info(f"API on port {port}")
    app.run(host="0.0.0.0", port=port)
