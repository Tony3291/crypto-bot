import logging
import asyncio
import aiohttp
import json
import math
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TELEGRAM_TOKEN = "8650706334:AAHJQrBxkw-zOw286H1v-PvtDtUWsM9KFfY"
GROQ_API_KEY   = "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"
ALLOWED_CHAT   = 5214099942

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SPOT = "https://api.binance.com"
FUT  = "https://fapi.binance.com"
GROQ = "https://api.groq.com/openai/v1/chat/completions"
FNG  = "https://api.alternative.me/fng/?limit=1"

# ---- HTTP ----
async def get(session, url, params=None, timeout=10):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug("GET %s: %s", url[:45], e)
    return None

async def post(session, url, headers, body, timeout=45):
    try:
        async with session.post(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug("POST %s: %s", url[:45], e)
    return None

# ---- INDICATORS ----
def ema(closes, p):
    if not closes or len(closes) < p:
        return None
    k = 2.0 / (p + 1)
    v = sum(closes[:p]) / p
    for x in closes[p:]:
        v = x * k + v * (1 - k)
    return v

def rsi(closes, p=14):
    if not closes or len(closes) < p + 1:
        return None
    d = [closes[i+1] - closes[i] for i in range(len(closes) - 1)]
    ag = sum(x if x > 0 else 0 for x in d[-p:]) / p
    al = sum(-x if x < 0 else 0 for x in d[-p:]) / p
    if al == 0:
        return 100.0
    return round(100 - (100 / (1 + ag / al)), 2)

def macd_ind(closes):
    if not closes or len(closes) < 35:
        return None, None, None
    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    if not e12 or not e26:
        return None, None, None
    ml = e12 - e26
    snaps = []
    c = closes[:]
    for _ in range(9):
        if len(c) >= 26:
            a = ema(c, 12)
            b = ema(c, 26)
            if a and b:
                snaps.insert(0, a - b)
        c = c[:-1]
    sig = sum(snaps) / len(snaps) if snaps else ml
    return ml, sig, ml - sig

def bollinger(closes, p=20):
    if not closes or len(closes) < p:
        return None, None, None
    w = closes[-p:]
    m = sum(w) / p
    s = (sum((x - m) ** 2 for x in w) / p) ** 0.5
    return m - 2*s, m, m + 2*s

def atr_calc(klines, p=14):
    if not klines or len(klines) < p + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2])
        l = float(klines[i][3])
        pc = float(klines[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-p:]) / min(p, len(trs)) if trs else None

def vwap_calc(klines):
    if not klines:
        return None
    tv = tpv = 0
    for k in klines:
        tp = (float(k[2]) + float(k[3]) + float(k[4])) / 3
        v = float(k[5])
        tpv += tp * v
        tv += v
    return tpv / tv if tv else None

def compute(klines):
    if not klines or len(klines) < 20:
        return {}
    closes  = [float(k[4]) for k in klines]
    highs   = [float(k[2]) for k in klines]
    lows    = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    cur = closes[-1]

    e9   = ema(closes, 9)
    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    e200 = ema(closes, 200)
    rv   = rsi(closes, 14)
    ml, sv, hv = macd_ind(closes)
    bl, bm, bu = bollinger(closes, 20)
    at   = atr_calc(klines, 14)
    vw   = vwap_calc(klines[-24:] if len(klines) >= 24 else klines)

    av = sum(volumes[-20:]) / 20 if volumes else 0
    vr = volumes[-1] / av if av > 0 else 1.0

    hh = highs[-1] > max(highs[-6:-1]) if len(highs) > 5 else False
    ll = lows[-1]  < min(lows[-6:-1])  if len(lows)  > 5 else False
    hl = lows[-1]  > min(lows[-6:-1])  if len(lows)  > 5 else False
    lh = highs[-1] < max(highs[-6:-1]) if len(highs) > 5 else False

    ts = "N/A"
    if len(klines) >= 28:
        up = [max(highs[i] - highs[i-1], 0) for i in range(1, len(highs))]
        dn = [max(lows[i-1] - lows[i], 0)   for i in range(1, len(lows))]
        a14 = at or 1
        pdi = 100 * (sum(up[-14:]) / 14) / a14
        mdi = 100 * (sum(dn[-14:]) / 14) / a14
        dx  = abs(pdi - mdi) / (pdi + mdi + 1e-9) * 100
        ts  = "Strong" if dx > 25 else ("Moderate" if dx > 15 else "Weak")

    return {
        "cur": cur, "e9": e9, "e21": e21, "e50": e50, "e200": e200,
        "rsi": rv, "macd": ml, "sig": sv, "hist": hv,
        "bl": bl, "bm": bm, "bu": bu, "at": at, "vw": vw,
        "vr": vr, "ts": ts, "hh": hh, "ll": ll, "hl": hl, "lh": lh,
    }

# ---- SCORING ----
def score(ind):
    if not ind:
        return 0
    s = 0
    cur = ind.get("cur", 0)

    rv = ind.get("rsi")
    if rv is not None:
        if rv < 25:     s += 3
        elif rv < 35:   s += 2
        elif rv < 45:   s += 1
        elif rv > 75:   s -= 3
        elif rv > 65:   s -= 2
        elif rv > 55:   s -= 1

    e9 = ind.get("e9"); e21 = ind.get("e21")
    e50 = ind.get("e50"); e200 = ind.get("e200")
    if e9 and e21 and e50:
        if cur > e9 > e21 > e50:    s += 3
        elif cur > e9 > e21:         s += 2
        elif cur > e9:               s += 1
        elif cur < e9 < e21 < e50:  s -= 3
        elif cur < e9 < e21:         s -= 2
        elif cur < e9:               s -= 1
    if e200:
        s += 1 if cur > e200 else -1

    hv = ind.get("hist"); ml = ind.get("macd")
    if hv is not None and ml is not None:
        if hv > 0 and ml > 0:   s += 2
        elif hv > 0:             s += 1
        elif hv < 0 and ml < 0: s -= 2
        else:                    s -= 1

    bl = ind.get("bl"); bu = ind.get("bu"); bm = ind.get("bm")
    if bl and bu and bm:
        rng = bu - bl
        if rng > 0:
            pos = (cur - bl) / rng
            if pos <= 0.10:    s += 2
            elif pos <= 0.25:  s += 1
            elif pos >= 0.90:  s -= 2
            elif pos >= 0.75:  s -= 1

    vw = ind.get("vw")
    if vw:
        s += 1 if cur > vw else -1

    if ind.get("hh") and ind.get("hl"):  s += 2
    elif ind.get("hh"):                   s += 1
    if ind.get("ll") and ind.get("lh"):  s -= 2
    elif ind.get("ll"):                   s -= 1

    return max(-10, min(10, s))

def detect_trend(ind):
    if not ind:
        return "SIDEWAYS"
    cur = ind.get("cur", 0)
    e9  = ind.get("e9"); e21 = ind.get("e21"); e50 = ind.get("e50")
    rv  = ind.get("rsi", 50) or 50
    hv  = ind.get("hist") or 0
    ts  = ind.get("ts", "Weak")
    if e9 and e21 and e50:
        if cur > e9 > e21 > e50 and rv > 55 and hv > 0 and ts in ("Strong", "Moderate"):
            return "STRONG UPTREND"
        elif cur > e9 > e21 and rv > 50:
            return "UPTREND"
        elif cur < e9 < e21 < e50 and rv < 45 and hv < 0 and ts in ("Strong", "Moderate"):
            return "STRONG DOWNTREND"
        elif cur < e9 < e21 and rv < 50:
            return "DOWNTREND"
    return "SIDEWAYS"

def sig_label(s):
    if s >= 7:    return "STRONG BUY"
    elif s >= 4:  return "BUY"
    elif s >= 2:  return "WEAK BUY"
    elif s <= -7: return "STRONG SELL"
    elif s <= -4: return "SELL"
    elif s <= -2: return "WEAK SELL"
    return "HOLD"

def fmt(n, d=4):
    if n is None:
        return "N/A"
    try:
        n = float(n)
        if abs(n) >= 1e9: return f"{n/1e9:.2f}B"
        if abs(n) >= 1e6: return f"{n/1e6:.2f}M"
        if abs(n) >= 1e3: return f"{n/1e3:.2f}K"
        return f"{n:.{d}f}"
    except Exception:
        return str(n)

# ---- CONFLUENCE ----
def confluence(s1h, s4h, s1d, i4h, i1d, fr=0.0):
    weighted = (s1h * 1 + s4h * 3 + s1d * 2) / 6.0
    bull_tfs = sum(1 for x in [s1h, s4h, s1d] if x >= 2)
    bear_tfs = sum(1 for x in [s1h, s4h, s1d] if x <= -2)
    reasons  = []

    tr4h = detect_trend(i4h)
    tr1d = detect_trend(i1d)
    if "UP" in tr4h and "UP" in tr1d:
        reasons.append("4H + 1D both uptrend")
    elif "DOWN" in tr4h and "DOWN" in tr1d:
        reasons.append("4H + 1D both downtrend")

    rv4h = i4h.get("rsi"); rv1d = i1d.get("rsi")
    if rv4h and rv4h < 40:
        reasons.append(f"RSI oversold on 4H ({rv4h:.0f})")
    if rv4h and rv4h > 65:
        reasons.append(f"RSI overbought on 4H ({rv4h:.0f})")

    h4h = i4h.get("hist") or 0
    h1d = i1d.get("hist") or 0
    if h4h > 0 and h1d > 0:
        reasons.append("MACD bullish on 4H + 1D")
    elif h4h < 0 and h1d < 0:
        reasons.append("MACD bearish on 4H + 1D")

    if fr > 0.05:
        reasons.append(f"High funding {fr:.3f}% bearish bias")
    elif fr < -0.02:
        reasons.append(f"Negative funding {fr:.3f}% bullish bias")

    if bull_tfs >= 2 and weighted >= 2:
        action = "STRONG BUY" if weighted >= 4 else "BUY"
        conf   = min(70 + bull_tfs * 5, 90)
    elif bear_tfs >= 2 and weighted <= -2:
        action = "STRONG SELL" if weighted <= -4 else "SELL"
        conf   = min(70 + bear_tfs * 5, 90)
    else:
        action = "HOLD"
        conf   = 40
        reasons.append("Mixed TF signals — no clear edge")

    return action, conf, reasons, weighted

# ---- PREDICTED RANGES ----
def predict_ranges(price, i1h, i4h, i1d, i1w, weighted):
    if not price or price <= 0:
        return {}
    at1h = (i1h.get("at") if i1h else None) or price * 0.008
    at4h = (i4h.get("at") if i4h else None) or price * 0.015
    at1d = (i1d.get("at") if i1d else None) or price * 0.030
    at1w = (i1w.get("at") if i1w else None) or price * 0.060

    bias = max(-0.5, min(0.5, weighted / 10.0))
    if bias > 0.15:    bl = "Bullish"
    elif bias < -0.15: bl = "Bearish"
    else:              bl = "Neutral"

    def make_r(atr, candles):
        vol    = atr * math.sqrt(max(candles, 1))
        center = price * (1 + bias * 0.008 * math.sqrt(max(candles, 1)))
        h = center + vol
        l = center - vol
        l = max(l, price * 0.50)   # never below 50% of price
        h = max(h, price * 1.001)
        return {"h": round(h, 8), "l": round(l, 8),
                "mp": round(min((h - l) / price * 100, 100), 2), "b": bl}

    return {
        "1H":  make_r(at1h, 1),
        "2H":  make_r(at1h, 2),
        "4H":  make_r(at4h, 1),
        "12H": make_r(at4h, 3),
        "1D":  make_r(at1d, 1),
        "3D":  make_r(at1d, 3),
        "1W":  make_r(at1w, 1),
        "1M":  make_r(at1w, 4),
    }

# ---- TRADE SETUPS ----
def spot_setup(price, action, i4h, i1d):
    at  = i4h.get("at") or (price * 0.015)
    bl  = i1d.get("bl"); bu = i1d.get("bu")

    if "BUY" in action:
        sl  = price - at * 1.5
        if bl and bl > sl and bl < price:
            sl = bl * 0.995
        sl  = max(sl, price * 0.85)
        risk = price - sl
        tp1 = price + risk * 1.5
        tp2 = price + risk * 2.5
        tp3 = price + risk * 4.0
    elif "SELL" in action:
        sl  = price + at * 1.5
        if bu and bu < sl and bu > price:
            sl = bu * 1.005
        sl  = min(sl, price * 1.15)
        risk = sl - price
        tp1 = max(price - risk * 1.5, price * 0.01)
        tp2 = max(price - risk * 2.5, price * 0.01)
        tp3 = max(price - risk * 4.0, price * 0.01)
    else:
        risk = at
        sl  = price - at
        tp1 = price + at
        tp2 = price + at * 2
        tp3 = price + at * 3

    sl_p  = abs((sl - price) / price * 100)
    tp1_p = abs((tp1 - price) / price * 100)
    rr    = round(tp1_p / sl_p, 2) if sl_p else 0
    return {"entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "sl_pct": round(sl_p, 2), "tp1_pct": round(tp1_p, 2), "rr": rr}

def fut_setup(price, direction, i4h, fr=0.0):
    if direction == "NO TRADE":
        return None
    lev = 25
    at  = i4h.get("at") or (price * 0.012)

    if direction == "LONG":
        sl  = max(price - at, price * 0.94)
        liq = price * (1 - 0.95 / lev)
        risk = price - sl
        tp1 = price + risk
        tp2 = price + risk * 2
        tp3 = price + at * 3
    else:
        sl  = min(price + at, price * 1.06)
        liq = price * (1 + 0.95 / lev)
        risk = sl - price
        tp1 = max(price - risk, price * 0.01)
        tp2 = max(price - risk * 2, price * 0.01)
        tp3 = max(price - at * 3, price * 0.01)

    sl_p    = abs((sl - price) / price * 100)
    tp1_p   = abs((tp1 - price) / price * 100)
    rr      = round(tp1_p / sl_p, 2) if sl_p else 0
    liq_buf = abs((liq - sl) / price * 100)

    return {
        "direction": direction, "entry": price, "lev": lev,
        "sl": sl, "sl_pct": round(sl_p, 2), "pnl_sl": round(-sl_p * lev, 1),
        "liq": liq, "liq_buf": round(liq_buf, 2),
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "tp1_pct": round(tp1_p, 2), "pnl_tp1": round(tp1_p * lev, 1),
        "pnl_tp2": round(abs((tp2-price)/price*100)*lev, 1), "rr": rr,
    }

# ---- GROQ AI ----
async def groq_ai(session, coin, ctx):
    prompt = (
        f"You are a professional crypto analyst. Analyze {coin}/USDT with this LIVE data:\n"
        f"{json.dumps(ctx, default=str)}\n\n"
        f"Reply ONLY with valid JSON (no markdown, no extra text):\n"
        f'{{"news":[{{"h":"real headline","d":"Jun 2026","s":"bullish/bearish/neutral","i":"impact"}}],'
        f'"sentiment":"BULLISH/BEARISH/NEUTRAL","score":5,'
        f'"context":"macro context with real numbers",'
        f'"short":{{"dir":"BULLISH/BEARISH/NEUTRAL","conf":65,"t_low":0.0,"t_high":0.0,'
        f'"sup":0.0,"res":0.0,"why":["reason1","reason2","reason3"]}},'
        f'"long":{{"dir":"BULLISH/BEARISH/NEUTRAL","conf":55,"t_low":0.0,"t_high":0.0,'
        f'"why":["reason1","reason2"]}},'
        f'"pumps":["catalyst1","catalyst2","catalyst3"],'
        f'"dumps":["risk1","risk2","risk3"],'
        f'"spot_action":"BUY/SELL/HOLD","spot_sl":0.0,"spot_tp1":0.0,"spot_why":"reason",'
        f'"fut_dir":"LONG/SHORT/NO TRADE","fut_why":"reason","fut_invalid":"condition",'
        f'"risk":"LOW/MEDIUM/HIGH/VERY HIGH","risk_why":"reason"}}'
        f"\n\nUse 4 news items. Fill all price fields with real numbers from the data."
    )

    h    = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {"model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1200, "temperature": 0.25}
    resp = await post(session, GROQ, h, body, timeout=45)
    if not resp or "choices" not in resp:
        return None
    raw = resp["choices"][0]["message"]["content"].strip()
    # Strip markdown fences
    for tok in ["```json", "```", "json"]:
        if raw.startswith(tok):
            raw = raw[len(tok):]
    raw = raw.strip().rstrip("`").strip()
    if "{" in raw:
        raw = raw[raw.index("{"):]
    if "}" in raw:
        raw = raw[:raw.rindex("}") + 1]
    try:
        return json.loads(raw)
    except Exception:
        return None

# ---- MAIN ANALYSIS ----
async def analyze(coin_raw):
    coin = coin_raw.upper().strip().lstrip("/")
    if coin.endswith("USDT"):
        coin = coin[:-4]
    sym = coin + "USDT"
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    logger.info("Analyzing %s", sym)

    conn = aiohttp.TCPConnector(ssl=False, limit=20)
    hdrs = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as s:

        sc = await get(s, f"{SPOT}/api/v3/ticker/price", {"symbol": sym}, timeout=6)
        fc = await get(s, f"{FUT}/fapi/v1/ticker/price",  {"symbol": sym}, timeout=6)
        on_spot = isinstance(sc, dict) and "price" in sc
        on_fut  = isinstance(fc, dict) and "price" in fc

        if not on_spot and not on_fut:
            return [f"No data for {sym}. Try /BTC /ETH /SOL"]

        kb = FUT  if on_fut  else SPOT
        kp = "/fapi/v1/klines" if on_fut else "/api/v3/klines"

        results = await asyncio.gather(
            get(s, f"{SPOT}/api/v3/ticker/24hr", {"symbol": sym}),
            get(s, f"{FUT}/fapi/v1/ticker/24hr",  {"symbol": sym}) if on_fut else asyncio.sleep(0),
            get(s, f"{FUT}/fapi/v1/premiumIndex",  {"symbol": sym}) if on_fut else asyncio.sleep(0),
            get(s, f"{FUT}/fapi/v1/openInterest",  {"symbol": sym}) if on_fut else asyncio.sleep(0),
            get(s, f"{FUT}/fapi/v1/fundingRate",   {"symbol": sym, "limit": 5}) if on_fut else asyncio.sleep(0),
            get(s, f"{FUT}/futures/data/globalLongShortAccountRatio", {"symbol": sym, "period": "5m", "limit": 1}) if on_fut else asyncio.sleep(0),
            get(s, f"{FUT}/fapi/v1/openInterestHist", {"symbol": sym, "period": "1h", "limit": 12}) if on_fut else asyncio.sleep(0),
            get(s, f"{FUT}/futures/data/takerlongshortRatio", {"symbol": sym, "period": "5m", "limit": 1}) if on_fut else asyncio.sleep(0),
            get(s, f"{kb}{kp}", {"symbol": sym, "interval": "1h", "limit": 120}),
            get(s, f"{kb}{kp}", {"symbol": sym, "interval": "4h", "limit": 120}),
            get(s, f"{kb}{kp}", {"symbol": sym, "interval": "1d", "limit": 200}),
            get(s, f"{kb}{kp}", {"symbol": sym, "interval": "1w", "limit": 52}),
            get(s, f"{SPOT}/api/v3/ticker/24hr", {"symbol": "BTCUSDT"}),
            get(s, f"{SPOT}/api/v3/ticker/24hr", {"symbol": "ETHUSDT"}),
            get(s, FNG, timeout=6),
            return_exceptions=True,
        )

        def safe(x, t):
            return x if isinstance(x, t) else None

        spot_t = safe(results[0],  dict)
        fut_t  = safe(results[1],  dict)
        prem   = safe(results[2],  dict)
        oi     = safe(results[3],  dict)
        fund   = safe(results[4],  list)
        ls     = safe(results[5],  list)
        oih    = safe(results[6],  list)
        taker  = safe(results[7],  list)
        ki1h   = safe(results[8],  list)
        ki4h   = safe(results[9],  list)
        ki1d   = safe(results[10], list)
        ki1w   = safe(results[11], list)
        btc_t  = safe(results[12], dict)
        eth_t  = safe(results[13], dict)
        fg     = safe(results[14], dict)

        price = 0.0
        if fut_t:    price = float(fut_t.get("lastPrice", 0))
        elif spot_t: price = float(spot_t.get("lastPrice", 0))

        i1h = compute(ki1h) if ki1h else {}
        i4h = compute(ki4h) if ki4h else {}
        i1d = compute(ki1d) if ki1d else {}
        i1w = compute(ki1w) if ki1w else {}

        s1h = score(i1h); s4h = score(i4h); s1d = score(i1d)
        fr_val = float(prem.get("lastFundingRate", 0)) * 100 if prem else 0.0

        action, conf, reasons, weighted = confluence(s1h, s4h, s1d, i4h, i1d, fr_val)
        fut_dir = "LONG" if "BUY" in action else ("SHORT" if "SELL" in action else "NO TRADE")

        ss   = spot_setup(price, action, i4h, i1d)
        fs   = fut_setup(price, fut_dir, i4h, fr_val)
        rngs = predict_ranges(price, i1h, i4h, i1d, i1w, weighted)

        tr4h  = detect_trend(i4h)
        tr24h = detect_trend(i1d)
        at4h  = i4h.get("at") or 0
        vol_lbl = "HIGH" if (at4h / price * 100 > 3 if price else False) else \
                  "MEDIUM" if (at4h / price * 100 > 1.5 if price else False) else "LOW"

        ctx = {
            "coin": coin, "price": price,
            "change_24h": float(spot_t.get("priceChangePercent", 0)) if spot_t else 0,
            "volume_24h": float(spot_t.get("quoteVolume", 0)) if spot_t else 0,
            "trend_4h": tr4h, "trend_24h": tr24h,
            "funding_pct": fr_val,
            "oi": float(oi.get("openInterest", 0)) if oi else 0,
            "long_pct": float(ls[0].get("longAccount", 0)) * 100 if ls else 50,
            "rsi_1h": i1h.get("rsi"), "rsi_4h": i4h.get("rsi"), "rsi_1d": i1d.get("rsi"),
            "ema9_4h": i4h.get("e9"), "ema21_4h": i4h.get("e21"),
            "bb_low_4h": i4h.get("bl"), "bb_high_4h": i4h.get("bu"),
            "atr_4h": at4h, "volatility": vol_lbl,
            "score_1h": s1h, "score_4h": s4h, "score_1d": s1d,
            "weighted": round(weighted, 2),
            "signal": action, "conf": conf,
            "btc_price": float(btc_t.get("lastPrice", 0)) if btc_t else 0,
            "btc_change": float(btc_t.get("priceChangePercent", 0)) if btc_t else 0,
            "fear_greed": fg["data"][0]["value"] if fg and "data" in fg else "N/A",
            "spot_sl": ss["sl"], "spot_tp1": ss["tp1"],
            "pred_1d_h": rngs.get("1D", {}).get("h"), "pred_1d_l": rngs.get("1D", {}).get("l"),
        }
        ai = await groq_ai(s, coin, ctx)

    # ---- FORMAT ----
    pages = []

    # PAGE 1
    p1 = []
    p1.append("=" * 28)
    p1.append(f"*{coin}/USDT — Intelligence v10*")
    p1.append(f"_{now}_")
    p1.append("=" * 28)

    p1.append("\n*BROADER MARKET*")
    if btc_t:
        bc = float(btc_t.get("priceChangePercent", 0))
        bp = float(btc_t.get("lastPrice", 0))
        p1.append(f"  BTC: `${fmt(bp, 0)}` ({bc:+.2f}%)")
    if eth_t:
        ec = float(eth_t.get("priceChangePercent", 0))
        ep = float(eth_t.get("lastPrice", 0))
        p1.append(f"  ETH: `${fmt(ep, 0)}` ({ec:+.2f}%)")
    if fg and "data" in fg:
        fv  = fg["data"][0]["value"]
        fvc = fg["data"][0]["value_classification"]
        p1.append(f"  Fear & Greed: `{fv}/100` — _{fvc}_")

    p1.append(f"\n*{coin} PRICE*")
    p1.append(f"  Current: `${fmt(price, 6)}`")
    if spot_t:
        ch  = float(spot_t.get("priceChangePercent", 0))
        h24 = float(spot_t.get("highPrice", price))
        l24 = float(spot_t.get("lowPrice",  price))
        vol = float(spot_t.get("quoteVolume", 0))
        p1.append(f"  24h: `{ch:+.2f}%`  High: `${fmt(h24, 6)}`  Low: `${fmt(l24, 6)}`")
        p1.append(f"  Volume: `{fmt(vol, 0)} USDT`")

    p1.append(f"\n*MARKET STATE*")
    p1.append(f"  Trend 4H:   `{tr4h}`")
    p1.append(f"  Trend 24H:  `{tr24h}`")
    p1.append(f"  Volatility: `{vol_lbl}`  Strength: `{i4h.get('ts', 'N/A')}`")

    p1.append(f"\n*CONFLUENCE SIGNAL*")
    p1.append(f"  *{action}* — Confidence: `{conf}%`")
    p1.append(f"  Scores: 1H:`{s1h:+d}` 4H:`{s4h:+d}` 1D:`{s1d:+d}` W:`{weighted:+.1f}`")
    for r in reasons[:3]:
        p1.append(f"  - {r}")

    p1.append(f"\n*PREDICTED RANGES (from `${fmt(price, 4)}`)*")
    for tf, r in rngs.items():
        p1.append(
            f"  `{tf:3}` H:`${fmt(r['h'], 4)}` L:`${fmt(r['l'], 4)}` "
            f"+-`{r['mp']:.1f}%` ({r['b']})"
        )

    pages.append("\n".join(p1))

    # PAGE 2
    p2 = []
    if on_fut:
        p2.append("*FUTURES MARKET (LIVE)*")
        if fut_t:
            fp  = float(fut_t.get("lastPrice", 0))
            fch = float(fut_t.get("priceChangePercent", 0))
            fv  = float(fut_t.get("quoteVolume", 0))
            p2.append(f"  Price: `${fmt(fp, 6)}` ({fch:+.2f}%)  Vol: `{fmt(fv, 0)}`")
        if prem:
            mark  = float(prem.get("markPrice", 0))
            idx   = float(prem.get("indexPrice", 0))
            basis = (mark - idx) / idx * 100 if idx else 0
            p2.append(f"  Mark: `${fmt(mark, 6)}`  Index: `${fmt(idx, 6)}`")
            p2.append(f"  Funding: `{fr_val:.4f}%`  Basis: `{basis:+.4f}%`")
            if fr_val > 0.05:    p2.append("   -> High funding: dump risk")
            elif fr_val < -0.02: p2.append("   -> Negative funding: squeeze possible")
        if oi:
            p2.append(f"  OI: `{fmt(float(oi.get('openInterest', 0)), 2)} {coin}`")
        if oih and len(oih) >= 2:
            ov  = [float(x.get("sumOpenInterest", 0)) for x in oih]
            oc  = (ov[-1] - ov[0]) / ov[0] * 100 if ov[0] else 0
            p2.append(f"  OI Trend 12h: `{oc:+.2f}%`")
        if ls:
            lp = float(ls[0].get("longAccount", 0)) * 100
            sp = 100 - lp
            p2.append(f"  Long/Short: `{lp:.1f}% / {sp:.1f}%`")
            if lp > 68:   p2.append("   -> Extreme longs: liquidation risk")
            elif sp > 65: p2.append("   -> Heavy shorts: squeeze possible")
        if taker:
            tb = float(taker[0].get("buySell", 1))
            p2.append(f"  Taker B/S: `{tb:.3f}` ({'Buy dom' if tb > 1 else 'Sell dom'})")
        if fund and len(fund) >= 3:
            rates = [float(f.get("fundingRate", 0)) * 100 for f in fund]
            avg   = sum(rates) / len(rates)
            p2.append(f"  Avg Funding(5): `{avg:.4f}%`")

    def iblock(lbl, ind, sc_val):
        if not ind:
            return f"\n*{lbl}* — No data"
        cur  = ind.get("cur", 0)
        sl   = sig_label(sc_val)
        tr   = detect_trend(ind)
        ts   = ind.get("ts", "N/A")
        lines = [f"\n*{lbl}* — {sl} ({sc_val:+d}/10) | {tr} [{ts}]"]

        rv = ind.get("rsi")
        if rv is not None:
            zone = "Overbought" if rv > 70 else ("Oversold" if rv < 30 else "Normal")
            lines.append(f"  RSI: `{rv}` — {zone}")

        for nm, ky in [("EMA9","e9"),("EMA21","e21"),("EMA50","e50"),("EMA200","e200")]:
            v = ind.get(ky)
            if v:
                d2 = (cur - v) / v * 100
                lines.append(f"  {nm}: `{fmt(v, 6)}` ({d2:+.2f}%) {'above' if cur > v else 'below'}")

        ml = ind.get("macd"); hv = ind.get("hist")
        if ml is not None:
            cross = "Bullish" if (hv or 0) > 0 else "Bearish"
            lines.append(f"  MACD: {cross} `{fmt(ml, 8)}` Hist:`{fmt(hv, 8)}`")

        bl2 = ind.get("bl"); bu2 = ind.get("bu"); bm2 = ind.get("bm")
        if bl2 and bu2:
            rng2 = bu2 - bl2
            pos  = (cur - bl2) / rng2 * 100 if rng2 else 50
            tag  = " [LOWER BAND]" if cur <= bl2 else (" [UPPER BAND]" if cur >= bu2 else f" [{pos:.0f}%]")
            lines.append(f"  BB: `{fmt(bl2, 6)}`/`{fmt(bm2, 6)}`/`{fmt(bu2, 6)}`{tag}")

        at2 = ind.get("at"); vw2 = ind.get("vw"); vr2 = ind.get("vr")
        if at2: lines.append(f"  ATR: `{fmt(at2, 6)}` ({at2/cur*100:.2f}%)")
        if vw2: lines.append(f"  VWAP: `{fmt(vw2, 6)}` ({'above' if cur > vw2 else 'below'})")
        if vr2: lines.append(f"  Volume: `{vr2:.2f}x` avg")
        return "\n".join(lines)

    p2.append(iblock("1H", i1h, s1h))
    p2.append(iblock("4H", i4h, s4h))
    p2.append(iblock("1D", i1d, s1d))
    if i1w:
        p2.append(iblock("1W", i1w, score(i1w)))

    if i1d.get("bl"):
        p2.append("\n*KEY LEVELS*")
        p2.append(f"  1D Sup: `${fmt(i1d.get('bl'), 6)}`  1D Res: `${fmt(i1d.get('bu'), 6)}`")
        if i4h.get("bl"):
            p2.append(f"  4H Sup: `${fmt(i4h.get('bl'), 6)}`  4H Res: `${fmt(i4h.get('bu'), 6)}`")
        if price:
            sup = i1d.get("bl", price); res = i1d.get("bu", price)
            if res > price: p2.append(f"  To Res: `+{(res-price)/price*100:.2f}%`")
            if sup < price: p2.append(f"  To Sup: `-{(price-sup)/price*100:.2f}%`")

    p2.append("\n" + "=" * 28)
    p2.append("*TRADE SETUPS*")

    p2.append(f"\n*SPOT: {action}* (conf: {conf}%)")
    p2.append(f"  Entry:    `${fmt(ss['entry'], 6)}`")
    p2.append(f"  Stop Loss:`${fmt(ss['sl'], 6)}` (-{ss['sl_pct']:.2f}%)")
    p2.append(f"  TP1:      `${fmt(ss['tp1'], 6)}` (+{ss['tp1_pct']:.2f}%)  R/R: `{ss['rr']}:1`")
    p2.append(f"  TP2:      `${fmt(ss['tp2'], 6)}`")
    p2.append(f"  TP3:      `${fmt(ss['tp3'], 6)}`")
    p2.append(f"  Size:     Max 5% of portfolio")

    p2.append(f"\n*FUTURES 25x: {fut_dir}* — WARNING: EXTREME RISK")
    if fs:
        p2.append(f"  Entry:     `${fmt(fs['entry'], 6)}`")
        p2.append(f"  Stop Loss: `${fmt(fs['sl'], 6)}` (-{fs['sl_pct']:.2f}% | PnL:{fs['pnl_sl']:+.0f}%)")
        p2.append(f"  Liq Price: `${fmt(fs['liq'], 6)}` (buffer: {fs['liq_buf']:.2f}%)")
        p2.append(f"  TP1:       `${fmt(fs['tp1'], 6)}` (PnL:+{fs['pnl_tp1']:.0f}%)")
        p2.append(f"  TP2:       `${fmt(fs['tp2'], 6)}` (PnL:+{fs['pnl_tp2']:.0f}%)")
        p2.append(f"  TP3:       `${fmt(fs['tp3'], 6)}`  R/R: `{fs['rr']}:1`")
        p2.append(f"  Margin:    MAX 2% of account")
    else:
        p2.append(f"  No clear signal — mixed confluence")
        p2.append(f"  Wait for TF alignment or key level break")

    pages.append("\n".join(p2))

    # PAGE 3 — AI
    p3 = []
    p3.append("=" * 28)
    p3.append("*GROQ AI ANALYSIS*")
    p3.append("=" * 28)

    if ai:
        p3.append("\n*LATEST NEWS*")
        news_list = ai.get("news", [])
        if isinstance(news_list, list):
            for n in news_list[:4]:
                if isinstance(n, dict):
                    sent_sym = "+" if n.get("s") == "bullish" else ("-" if n.get("s") == "bearish" else "~")
                    p3.append(f"  [{sent_sym}] *{n.get('h', 'N/A')}*")
                    if n.get("i"):
                        p3.append(f"       {n.get('d', '')} - {n['i']}")

        sent  = ai.get("sentiment", "N/A")
        ss2   = ai.get("score", 5)
        p3.append(f"\n*SENTIMENT: {sent}* ({ss2}/10)")
        if ai.get("context"):
            p3.append(f"  {ai['context']}")

        sh = ai.get("short", {})
        if isinstance(sh, dict):
            p3.append(f"\n*SHORT TERM (24-72h)*")
            p3.append(f"  {sh.get('dir','N/A')} — Confidence: {sh.get('conf',0)}%")
            tl = sh.get("t_low"); th = sh.get("t_high")
            if tl and float(tl) > 0:
                p3.append(f"  Target: `${fmt(tl, 4)}` — `${fmt(th, 4)}`")
            sup = sh.get("sup"); res = sh.get("res")
            if sup and float(sup) > 0:
                p3.append(f"  Sup: `${fmt(sup, 6)}`  Res: `${fmt(res, 6)}`")
            for r in sh.get("why", []):
                p3.append(f"  - {r}")

        lg = ai.get("long", {})
        if isinstance(lg, dict):
            p3.append(f"\n*LONG TERM (1-4 weeks)*")
            p3.append(f"  {lg.get('dir','N/A')} — Confidence: {lg.get('conf',0)}%")
            tl = lg.get("t_low"); th = lg.get("t_high")
            if tl and float(tl) > 0:
                p3.append(f"  Target: `${fmt(tl, 4)}` — `${fmt(th, 4)}`")
            for r in lg.get("why", []):
                p3.append(f"  - {r}")

        pumps = ai.get("pumps", [])
        if isinstance(pumps, list):
            p3.append(f"\n*PUMP CATALYSTS*")
            for i, c in enumerate(pumps[:4], 1):
                p3.append(f"  {i}. {c}")

        dumps = ai.get("dumps", [])
        if isinstance(dumps, list):
            p3.append(f"\n*DUMP RISKS*")
            for i, r in enumerate(dumps[:4], 1):
                p3.append(f"  {i}. {r}")

        sa = ai.get("spot_action", "N/A")
        p3.append(f"\n*AI SPOT: {sa}*")
        s_sl  = ai.get("spot_sl")
        s_tp1 = ai.get("spot_tp1")
        if s_sl and float(s_sl) > 0:
            p3.append(f"  SL: `${fmt(s_sl, 6)}`  TP1: `${fmt(s_tp1, 6)}`")
        if ai.get("spot_why"):
            p3.append(f"  {ai['spot_why']}")

        fd2 = ai.get("fut_dir", "N/A")
        p3.append(f"\n*AI FUTURES: {fd2}* (25x — EXTREME RISK)")
        if ai.get("fut_why"):
            p3.append(f"  {ai['fut_why']}")
        if ai.get("fut_invalid"):
            p3.append(f"  Invalidation: {ai['fut_invalid']}")

        rl = ai.get("risk", "N/A")
        p3.append(f"\n*RISK: {rl}*")
        if ai.get("risk_why"):
            p3.append(f"  {ai['risk_why']}")
    else:
        p3.append("\nGroq AI unavailable — technical analysis above is valid")

    p3.append("\n" + "=" * 28)
    p3.append("Not financial advice. 25x = extreme risk. DYOR.")
    pages.append("\n".join(p3))
    return pages


# ---- TELEGRAM ----
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT:
        return
    text = update.message.text or ""
    cmd  = text.split()[0].lstrip("/").split("@")[0].strip()

    if cmd.lower() in ("start", "help"):
        await update.message.reply_text(
            "*Crypto Intelligence Bot v10*\n\n"
            "Type /COIN for analysis:\n"
            "/BTC /ETH /SOL /PEPE /DOGE /XRP\n\n"
            "Gets: Price, Ranges, Indicators,\n"
            "Spot + Futures signals, AI analysis",
            parse_mode="Markdown")
        return

    if not cmd:
        return

    msg = await update.message.reply_text(
        f"Analyzing {cmd.upper()}/USDT... (~20 sec)",
        parse_mode="Markdown")
    try:
        pages = await analyze(cmd)
        await msg.delete()
        for i, page in enumerate(pages):
            if page.strip():
                await update.message.reply_text(page.strip(), parse_mode="Markdown")
                if i < len(pages) - 1:
                    await asyncio.sleep(0.5)
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        try:
            await msg.edit_text(f"Error: {str(e)[:200]}", parse_mode="Markdown")
        except Exception:
            pass


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handler))
    app.add_handler(CommandHandler("help",  handler))
    app.add_handler(MessageHandler(filters.COMMAND, handler))
    logger.info("Crypto Intelligence Bot v10 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
