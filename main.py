"""
Crypto Intelligence Bot v11
- Directional price range prediction (single direction)
- Auto scanner: all Binance futures for OB/SMC/ICT setups
- CryptoPanic news (fixed)
- Groq AI (fixed with better prompt)
- Order Block / FVG / BOS / CHOCH detection
"""
import logging
import asyncio
import aiohttp
import json
import math
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, ContextTypes, filters,
)

TELEGRAM_TOKEN   = "8650706334:AAHJQrBxkw-zOw286H1v-PvtDtUWsM9KFfY"
GROQ_API_KEY     = "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"
CRYPTOPANIC_KEY  = "free"   # CryptoPanic free tier (no key needed for public)
ALLOWED_CHAT     = 5214099942

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SPOT  = "https://api.binance.com"
FUT   = "https://fapi.binance.com"
GROQ  = "https://api.groq.com/openai/v1/chat/completions"
FNG   = "https://api.alternative.me/fng/?limit=1"
CP    = "https://cryptopanic.com/api/free/v1/posts/"

# ---- HTTP ----
async def get(session, url, params=None, timeout=10):
    try:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug("GET %s: %s", url[:50], e)
    return None

async def post_req(session, url, headers, body, timeout=45):
    try:
        async with session.post(
            url, headers=headers, json=body,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug("POST %s: %s", url[:50], e)
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
    return round(100 - (100 / (1 + ag / al)), 2) if al else 100.0

def macd_ind(closes):
    if not closes or len(closes) < 35:
        return None, None, None
    e12 = ema(closes, 12); e26 = ema(closes, 26)
    if not e12 or not e26:
        return None, None, None
    ml = e12 - e26
    snaps = []; c = closes[:]
    for _ in range(9):
        if len(c) >= 26:
            a = ema(c, 12); b = ema(c, 26)
            if a and b: snaps.insert(0, a - b)
        c = c[:-1]
    sig = sum(snaps) / len(snaps) if snaps else ml
    return ml, sig, ml - sig

def bollinger(closes, p=20):
    if not closes or len(closes) < p:
        return None, None, None
    w = closes[-p:]; m = sum(w) / p
    s = (sum((x - m) ** 2 for x in w) / p) ** 0.5
    return m - 2*s, m, m + 2*s

def atr_calc(klines, p=14):
    if not klines or len(klines) < p + 1:
        return None
    trs = []
    for i in range(1, len(klines)):
        h = float(klines[i][2]); l = float(klines[i][3]); pc = float(klines[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-p:]) / min(p, len(trs)) if trs else None

def vwap_calc(klines):
    if not klines: return None
    tv = tpv = 0
    for k in klines:
        tp = (float(k[2]) + float(k[3]) + float(k[4])) / 3
        v = float(k[5]); tpv += tp * v; tv += v
    return tpv / tv if tv else None

def compute(klines):
    if not klines or len(klines) < 20:
        return {}
    closes  = [float(k[4]) for k in klines]
    highs   = [float(k[2]) for k in klines]
    lows    = [float(k[3]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    cur = closes[-1]

    e9=ema(closes,9); e21=ema(closes,21); e50=ema(closes,50); e200=ema(closes,200)
    rv=rsi(closes,14); ml,sv,hv=macd_ind(closes); bl,bm,bu=bollinger(closes,20)
    at=atr_calc(klines,14)
    vw=vwap_calc(klines[-24:] if len(klines)>=24 else klines)

    av = sum(volumes[-20:]) / 20 if volumes else 0
    vr = volumes[-1] / av if av > 0 else 1.0

    hh = highs[-1] > max(highs[-6:-1]) if len(highs)>5 else False
    ll = lows[-1]  < min(lows[-6:-1])  if len(lows)>5  else False
    hl = lows[-1]  > min(lows[-6:-1])  if len(lows)>5  else False
    lh = highs[-1] < max(highs[-6:-1]) if len(highs)>5 else False

    ts = "N/A"
    if len(klines) >= 28:
        up = [max(highs[i]-highs[i-1], 0) for i in range(1,len(highs))]
        dn = [max(lows[i-1]-lows[i], 0)   for i in range(1,len(lows))]
        a14 = at or 1
        pdi = 100*(sum(up[-14:])/14)/a14; mdi = 100*(sum(dn[-14:])/14)/a14
        dx  = abs(pdi-mdi)/(pdi+mdi+1e-9)*100
        ts  = "Strong" if dx>25 else ("Moderate" if dx>15 else "Weak")

    return {
        "cur":cur,"e9":e9,"e21":e21,"e50":e50,"e200":e200,
        "rsi":rv,"macd":ml,"sig":sv,"hist":hv,
        "bl":bl,"bm":bm,"bu":bu,"at":at,"vw":vw,
        "vr":vr,"ts":ts,"hh":hh,"ll":ll,"hl":hl,"lh":lh,
        "highs":highs,"lows":lows,"closes":closes,
    }

# ---- SCORING ----
def score(ind):
    if not ind: return 0
    s = 0; cur = ind.get("cur", 0)
    rv = ind.get("rsi")
    if rv is not None:
        if rv < 25: s+=3
        elif rv < 35: s+=2
        elif rv < 45: s+=1
        elif rv > 75: s-=3
        elif rv > 65: s-=2
        elif rv > 55: s-=1
    e9=ind.get("e9"); e21=ind.get("e21"); e50=ind.get("e50"); e200=ind.get("e200")
    if e9 and e21 and e50:
        if cur>e9>e21>e50: s+=3
        elif cur>e9>e21: s+=2
        elif cur>e9: s+=1
        elif cur<e9<e21<e50: s-=3
        elif cur<e9<e21: s-=2
        elif cur<e9: s-=1
    if e200: s += 1 if cur>e200 else -1
    hv=ind.get("hist"); ml=ind.get("macd")
    if hv is not None and ml is not None:
        if hv>0 and ml>0: s+=2
        elif hv>0: s+=1
        elif hv<0 and ml<0: s-=2
        else: s-=1
    bl=ind.get("bl"); bu=ind.get("bu"); bm=ind.get("bm")
    if bl and bu and bm:
        rng=bu-bl
        if rng>0:
            pos=(cur-bl)/rng
            if pos<=0.10: s+=2
            elif pos<=0.25: s+=1
            elif pos>=0.90: s-=2
            elif pos>=0.75: s-=1
    vw=ind.get("vw")
    if vw: s += 1 if cur>vw else -1
    if ind.get("hh") and ind.get("hl"): s+=2
    elif ind.get("hh"): s+=1
    if ind.get("ll") and ind.get("lh"): s-=2
    elif ind.get("ll"): s-=1
    return max(-10, min(10, s))

def detect_trend(ind):
    if not ind: return "SIDEWAYS"
    cur=ind.get("cur",0); e9=ind.get("e9"); e21=ind.get("e21"); e50=ind.get("e50")
    rv=ind.get("rsi",50) or 50; hv=ind.get("hist") or 0; ts=ind.get("ts","Weak")
    if e9 and e21 and e50:
        if cur>e9>e21>e50 and rv>55 and hv>0 and ts in ("Strong","Moderate"): return "STRONG UPTREND"
        elif cur>e9>e21 and rv>50: return "UPTREND"
        elif cur<e9<e21<e50 and rv<45 and hv<0 and ts in ("Strong","Moderate"): return "STRONG DOWNTREND"
        elif cur<e9<e21 and rv<50: return "DOWNTREND"
    return "SIDEWAYS"

def sig_label(s):
    if s>=7: return "STRONG BUY"
    elif s>=4: return "BUY"
    elif s>=2: return "WEAK BUY"
    elif s<=-7: return "STRONG SELL"
    elif s<=-4: return "SELL"
    elif s<=-2: return "WEAK SELL"
    return "HOLD"

def fmt(n, d=4):
    if n is None: return "N/A"
    try:
        n=float(n)
        if abs(n)>=1e9: return f"{n/1e9:.2f}B"
        if abs(n)>=1e6: return f"{n/1e6:.2f}M"
        if abs(n)>=1e3: return f"{n/1e3:.2f}K"
        return f"{n:.{d}f}"
    except Exception: return str(n)

# ---- SMC / ICT / ORDER BLOCK DETECTION ----
def detect_order_blocks(klines, n=50):
    """
    Detect Order Blocks (OB), Fair Value Gaps (FVG),
    Break of Structure (BOS), Change of Character (CHOCH)
    """
    if not klines or len(klines) < 10:
        return {}

    klines = klines[-n:]
    closes = [float(k[4]) for k in klines]
    highs  = [float(k[2]) for k in klines]
    lows   = [float(k[3]) for k in klines]
    opens  = [float(k[1]) for k in klines]
    cur    = closes[-1]

    results = {
        "bullish_ob": [],
        "bearish_ob": [],
        "fvg_bull":   [],
        "fvg_bear":   [],
        "bos":        None,
        "choch":      None,
        "swing_high": None,
        "swing_low":  None,
    }

    # Swing highs and lows
    swing_highs = []
    swing_lows  = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            swing_highs.append((i, highs[i]))
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            swing_lows.append((i, lows[i]))

    if swing_highs:
        results["swing_high"] = swing_highs[-1][1]
    if swing_lows:
        results["swing_low"] = swing_lows[-1][1]

    # Break of Structure (BOS) - price breaks last swing high/low
    if swing_highs and cur > swing_highs[-1][1]:
        results["bos"] = {"type": "BULLISH", "level": swing_highs[-1][1],
                          "msg": f"BOS Bullish: broke ${fmt(swing_highs[-1][1],6)}"}
    elif swing_lows and cur < swing_lows[-1][1]:
        results["bos"] = {"type": "BEARISH", "level": swing_lows[-1][1],
                          "msg": f"BOS Bearish: broke ${fmt(swing_lows[-1][1],6)}"}

    # Change of Character (CHOCH) - opposite structure break
    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        # Lower high after uptrend = CHOCH bearish
        if swing_highs[-1][1] < swing_highs[-2][1] and cur < swing_lows[-1][1]:
            results["choch"] = {"type": "BEARISH",
                                "msg": f"CHOCH Bearish: lower high + structure break"}
        # Higher low after downtrend = CHOCH bullish
        elif swing_lows[-1][1] > swing_lows[-2][1] and cur > swing_highs[-1][1]:
            results["choch"] = {"type": "BULLISH",
                                "msg": f"CHOCH Bullish: higher low + structure break"}

    # Bullish Order Blocks: last bearish candle before a strong bullish move
    for i in range(len(klines) - 5, max(0, len(klines) - 20), -1):
        if opens[i] > closes[i]:  # bearish candle
            # Check if followed by bullish momentum
            subsequent_close = closes[min(i+3, len(closes)-1)]
            if subsequent_close > highs[i]:  # price swept above
                ob_high = highs[i]
                ob_low  = lows[i]
                # Only valid if current price is near or above OB
                if cur > ob_low and cur < ob_high * 1.3:
                    results["bullish_ob"].append({
                        "high": ob_high, "low": ob_low,
                        "mid": (ob_high + ob_low) / 2,
                        "dist_pct": (ob_high - cur) / cur * 100,
                    })
                    if len(results["bullish_ob"]) >= 2:
                        break

    # Bearish Order Blocks: last bullish candle before a strong bearish move
    for i in range(len(klines) - 5, max(0, len(klines) - 20), -1):
        if closes[i] > opens[i]:  # bullish candle
            subsequent_close = closes[min(i+3, len(closes)-1)]
            if subsequent_close < lows[i]:  # price swept below
                ob_high = highs[i]
                ob_low  = lows[i]
                if cur < ob_high and cur > ob_low * 0.7:
                    results["bearish_ob"].append({
                        "high": ob_high, "low": ob_low,
                        "mid": (ob_high + ob_low) / 2,
                        "dist_pct": (cur - ob_low) / cur * 100,
                    })
                    if len(results["bearish_ob"]) >= 2:
                        break

    # Fair Value Gaps (FVG)
    for i in range(1, len(klines) - 1):
        # Bullish FVG: gap between candle[i-1] high and candle[i+1] low
        if i+1 < len(klines):
            if lows[i+1] > highs[i-1]:
                fvg_size = lows[i+1] - highs[i-1]
                fvg_mid  = (lows[i+1] + highs[i-1]) / 2
                if fvg_size / cur > 0.003:  # min 0.3% gap
                    results["fvg_bull"].append({
                        "top": lows[i+1], "bottom": highs[i-1],
                        "mid": fvg_mid,
                        "size_pct": fvg_size / cur * 100,
                    })
            # Bearish FVG: gap between candle[i-1] low and candle[i+1] high
            elif highs[i+1] < lows[i-1]:
                fvg_size = lows[i-1] - highs[i+1]
                fvg_mid  = (lows[i-1] + highs[i+1]) / 2
                if fvg_size / cur > 0.003:
                    results["fvg_bear"].append({
                        "top": lows[i-1], "bottom": highs[i+1],
                        "mid": fvg_mid,
                        "size_pct": fvg_size / cur * 100,
                    })

    # Keep only most recent FVGs
    results["fvg_bull"] = results["fvg_bull"][-2:]
    results["fvg_bear"] = results["fvg_bear"][-2:]

    return results

def smc_signal(smc, score_val):
    """Derive trade signal from SMC analysis"""
    bull_points = 0; bear_points = 0
    reasons = []

    if smc.get("bos"):
        if smc["bos"]["type"] == "BULLISH":
            bull_points += 3; reasons.append("BOS Bullish confirmed")
        else:
            bear_points += 3; reasons.append("BOS Bearish confirmed")

    if smc.get("choch"):
        if smc["choch"]["type"] == "BULLISH":
            bull_points += 2; reasons.append("CHOCH Bullish reversal")
        else:
            bear_points += 2; reasons.append("CHOCH Bearish reversal")

    if smc.get("bullish_ob"):
        bull_points += 2; reasons.append(f"Bullish OB @ ${fmt(smc['bullish_ob'][0]['high'],6)}")

    if smc.get("bearish_ob"):
        bear_points += 2; reasons.append(f"Bearish OB @ ${fmt(smc['bearish_ob'][0]['low'],6)}")

    if smc.get("fvg_bull"):
        bull_points += 1; reasons.append(f"Bullish FVG @ ${fmt(smc['fvg_bull'][-1]['mid'],6)}")

    if smc.get("fvg_bear"):
        bear_points += 1; reasons.append(f"Bearish FVG @ ${fmt(smc['fvg_bear'][-1]['mid'],6)}")

    # Combine with indicator score
    if score_val > 2: bull_points += 2
    elif score_val < -2: bear_points += 2

    if bull_points > bear_points and bull_points >= 3:
        return "LONG", bull_points, reasons
    elif bear_points > bull_points and bear_points >= 3:
        return "SHORT", bear_points, reasons
    return "NEUTRAL", 0, reasons

# ---- CONFLUENCE ----
def confluence(s1h, s4h, s1d, i4h, i1d, fr=0.0):
    weighted = (s1h*1 + s4h*3 + s1d*2) / 6.0
    bull_tfs = sum(1 for x in [s1h,s4h,s1d] if x>=2)
    bear_tfs = sum(1 for x in [s1h,s4h,s1d] if x<=-2)
    reasons  = []

    tr4h=detect_trend(i4h); tr1d=detect_trend(i1d)
    if "UP" in tr4h and "UP" in tr1d: reasons.append("4H + 1D both uptrend")
    elif "DOWN" in tr4h and "DOWN" in tr1d: reasons.append("4H + 1D both downtrend")

    rv4h=i4h.get("rsi")
    if rv4h and rv4h<40: reasons.append(f"RSI oversold 4H ({rv4h:.0f})")
    if rv4h and rv4h>65: reasons.append(f"RSI overbought 4H ({rv4h:.0f})")

    h4h=i4h.get("hist") or 0; h1d=i1d.get("hist") or 0
    if h4h>0 and h1d>0: reasons.append("MACD bullish 4H+1D")
    elif h4h<0 and h1d<0: reasons.append("MACD bearish 4H+1D")

    if fr>0.05: reasons.append(f"High funding {fr:.3f}% bearish")
    elif fr<-0.02: reasons.append(f"Negative funding {fr:.3f}% bullish")

    if bull_tfs>=2 and weighted>=2:
        action = "STRONG BUY" if weighted>=4 else "BUY"
        conf   = min(70+bull_tfs*5, 90)
    elif bear_tfs>=2 and weighted<=-2:
        action = "STRONG SELL" if weighted<=-4 else "SELL"
        conf   = min(70+bear_tfs*5, 90)
    else:
        action="HOLD"; conf=40
        reasons.append("Mixed TF signals")

    return action, conf, reasons, weighted

# ---- DIRECTIONAL PRICE PREDICTION ----
def predict_directional(price, i1h, i4h, i1d, i1w, weighted, smc_dir="NEUTRAL"):
    """
    Predict price range in ONE direction based on:
    - Weighted score direction
    - SMC signal direction
    - ATR volatility
    - Key levels as targets
    """
    if not price or price <= 0:
        return {}

    at1h = (i1h.get("at") if i1h else None) or price*0.008
    at4h = (i4h.get("at") if i4h else None) or price*0.015
    at1d = (i1d.get("at") if i1d else None) or price*0.030
    at1w = (i1w.get("at") if i1w else None) or price*0.060

    # Determine overall direction
    if weighted >= 1.5 or smc_dir == "LONG":
        direction = "LONG"
        bias_pct = abs(weighted) / 10.0
    elif weighted <= -1.5 or smc_dir == "SHORT":
        direction = "SHORT"
        bias_pct = abs(weighted) / 10.0
    else:
        direction = "NEUTRAL"
        bias_pct  = 0.05

    def make_r(atr, candles, is_long, weight_factor=1.0):
        vol = atr * math.sqrt(max(candles, 1)) * weight_factor
        if is_long:
            # For LONG: target high = price + vol * (1 + bias)
            target = price + vol * (1 + bias_pct * 0.5)
            stop   = price - vol * 0.6  # tighter stop
            stop   = max(stop, price * 0.85)
        else:
            # For SHORT: target low = price - vol * (1 + bias)
            target = price - vol * (1 + bias_pct * 0.5)
            target = max(target, price * 0.01)
            stop   = price + vol * 0.6
            stop   = min(stop, price * 1.15)

        move_pct = abs(target - price) / price * 100
        return {
            "target": round(target, 8),
            "stop":   round(stop, 8),
            "move_pct": round(min(move_pct, 100), 2),
            "direction": direction,
        }

    is_long = (direction == "LONG")
    return {
        "direction": direction,
        "1H":  make_r(at1h, 1,  is_long),
        "2H":  make_r(at1h, 2,  is_long),
        "4H":  make_r(at4h, 1,  is_long),
        "12H": make_r(at4h, 3,  is_long),
        "1D":  make_r(at1d, 1,  is_long),
        "3D":  make_r(at1d, 3,  is_long),
        "1W":  make_r(at1w, 1,  is_long),
        "1M":  make_r(at1w, 4,  is_long),
    }

# ---- TRADE SETUPS ----
def spot_setup(price, action, i4h, i1d):
    at=i4h.get("at") or (price*0.015)
    bl=i1d.get("bl"); bu=i1d.get("bu")
    if "BUY" in action:
        sl=price-at*1.5
        if bl and bl>sl and bl<price: sl=bl*0.995
        sl=max(sl, price*0.85)
        risk=price-sl
        tp1=price+risk*1.5; tp2=price+risk*2.5; tp3=price+risk*4.0
    elif "SELL" in action:
        sl=price+at*1.5
        if bu and bu<sl and bu>price: sl=bu*1.005
        sl=min(sl, price*1.15)
        risk=sl-price
        tp1=max(price-risk*1.5, price*0.01)
        tp2=max(price-risk*2.5, price*0.01)
        tp3=max(price-risk*4.0, price*0.01)
    else:
        risk=at; sl=price-at
        tp1=price+at; tp2=price+at*2; tp3=price+at*3
    sl_p=abs((sl-price)/price*100); tp1_p=abs((tp1-price)/price*100)
    rr=round(tp1_p/sl_p,2) if sl_p else 0
    return {"entry":price,"sl":sl,"tp1":tp1,"tp2":tp2,"tp3":tp3,
            "sl_pct":round(sl_p,2),"tp1_pct":round(tp1_p,2),"rr":rr}

def fut_setup(price, direction, i4h, fr=0.0):
    if direction == "NO TRADE": return None
    lev=25; at=i4h.get("at") or (price*0.012)
    if direction=="LONG":
        sl=max(price-at, price*0.94)
        liq=price*(1-0.95/lev)
        risk=price-sl
        tp1=price+risk; tp2=price+risk*2; tp3=price+at*3
    else:
        sl=min(price+at, price*1.06)
        liq=price*(1+0.95/lev)
        risk=sl-price
        tp1=max(price-risk, price*0.01)
        tp2=max(price-risk*2, price*0.01)
        tp3=max(price-at*3, price*0.01)
    sl_p=abs((sl-price)/price*100); tp1_p=abs((tp1-price)/price*100)
    rr=round(tp1_p/sl_p,2) if sl_p else 0
    liq_buf=abs((liq-sl)/price*100)
    return {"direction":direction,"entry":price,"lev":lev,
            "sl":sl,"sl_pct":round(sl_p,2),"pnl_sl":round(-sl_p*lev,1),
            "liq":liq,"liq_buf":round(liq_buf,2),
            "tp1":tp1,"tp2":tp2,"tp3":tp3,
            "tp1_pct":round(tp1_p,2),"pnl_tp1":round(tp1_p*lev,1),
            "pnl_tp2":round(abs((tp2-price)/price*100)*lev,1),"rr":rr}

# ---- NEWS (CryptoPanic fixed) ----
async def get_news(session, coin):
    """CryptoPanic free API - no key needed"""
    try:
        url = "https://cryptopanic.com/api/free/v1/posts/"
        params = {"currencies": coin, "kind": "news", "public": "true"}
        data = await get(session, url, params=params, timeout=8)
        if data and "results" in data and data["results"]:
            news = []
            for item in data["results"][:4]:
                title = item.get("title","")
                votes = item.get("votes",{})
                bull  = votes.get("positive",0)
                bear  = votes.get("negative",0)
                sent  = "bullish" if bull>bear else ("bearish" if bear>bull else "neutral")
                news.append({"title":title,"sentiment":sent,"source":item.get("source",{}).get("title","")})
            return news
    except Exception as e:
        logger.debug("News error: %s", e)

    # Fallback: try alternative
    try:
        url2 = f"https://min-api.cryptocompare.com/data/v2/news/?categories={coin}&lTs=0"
        data2 = await get(session, url2, timeout=8)
        if data2 and "Data" in data2 and data2["Data"]:
            news = []
            for item in data2["Data"][:4]:
                news.append({
                    "title": item.get("title",""),
                    "sentiment": "neutral",
                    "source": item.get("source",""),
                })
            return news
    except Exception as e:
        logger.debug("News fallback error: %s", e)
    return []

# ---- GROQ AI (fixed) ----
async def groq_ai(session, coin, ctx):
    ctx_str = json.dumps(ctx, default=str)
    prompt = (
        f"Analyze {coin}/USDT crypto. Data: {ctx_str}\n\n"
        f"Return ONLY this JSON object (no text before/after, no markdown):\n"
        f'{{"sentiment":"BULLISH","score":6,'
        f'"context":"2 sentence macro context",'
        f'"direction":"LONG",'
        f'"confidence":70,'
        f'"target_1d":{ctx.get("pred_1d_target",0)},'
        f'"target_1w":{ctx.get("pred_1w_target",0)},'
        f'"key_support":{ctx.get("spot_sl",0)},'
        f'"key_resistance":{ctx.get("spot_tp1",0)},'
        f'"why_bull":["reason1","reason2","reason3"],'
        f'"why_bear":["reason1","reason2","reason3"],'
        f'"pump_catalyst":"specific event or level",'
        f'"dump_trigger":"specific event or level",'
        f'"spot_action":"BUY",'
        f'"spot_sl":{ctx.get("spot_sl",0)},'
        f'"spot_tp1":{ctx.get("spot_tp1",0)},'
        f'"fut_dir":"LONG",'
        f'"fut_entry":{ctx.get("price",0)},'
        f'"fut_sl":{ctx.get("spot_sl",0)},'
        f'"fut_tp1":{ctx.get("spot_tp1",0)},'
        f'"risk":"MEDIUM",'
        f'"risk_note":"specific reason"}}\n\n'
        f"Replace all values with real analysis. Use actual price numbers from the data."
    )
    h    = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 600,
        "temperature": 0.2,
    }
    resp = await post_req(session, GROQ, h, body, timeout=40)
    if not resp or "choices" not in resp:
        return None
    raw = resp["choices"][0]["message"]["content"].strip()
    # Aggressive JSON extraction
    start = raw.find("{"); end = raw.rfind("}")
    if start == -1 or end == -1: return None
    raw = raw[start:end+1]
    try:
        return json.loads(raw)
    except Exception:
        # Try to fix common issues
        try:
            raw = raw.replace("'", '"').replace("True","true").replace("False","false")
            return json.loads(raw)
        except Exception:
            return None

# ---- BINANCE FUTURES SCANNER ----
async def scan_futures(session, top_n=150):
    """Scan top Binance futures for OB/SMC setups"""
    info = await get(session, f"{FUT}/fapi/v1/exchangeInfo", timeout=10)
    if not info or "symbols" not in info:
        return []

    # Get all USDT-M futures trading
    symbols = [
        s["symbol"] for s in info["symbols"]
        if s.get("status") == "TRADING" and s["symbol"].endswith("USDT")
        and s.get("contractType") == "PERPETUAL"
    ]

    # Get 24h stats to filter by volume (only liquid coins)
    stats = await get(session, f"{FUT}/fapi/v1/ticker/24hr", timeout=10)
    if stats and isinstance(stats, list):
        vol_map = {s["symbol"]: float(s.get("quoteVolume", 0)) for s in stats}
        # Sort by volume and take top N
        symbols = sorted(
            [s for s in symbols if s in vol_map],
            key=lambda x: vol_map.get(x, 0), reverse=True
        )[:top_n]

    setups = []
    batch_size = 20

    for i in range(0, min(len(symbols), top_n), batch_size):
        batch = symbols[i:i+batch_size]
        tasks = [
            get(session, f"{FUT}/fapi/v1/klines",
                {"symbol": sym, "interval": "4h", "limit": 60}, timeout=8)
            for sym in batch
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, klines in zip(batch, results):
            if not isinstance(klines, list) or len(klines) < 20:
                continue
            try:
                ind  = compute(klines)
                sc   = score(ind)
                smc  = detect_order_blocks(klines, 50)
                sdir, sp, sreasons = smc_signal(smc, sc)
                tr   = detect_trend(ind)

                # Only flag strong setups
                if sdir in ("LONG", "SHORT") and sp >= 4:
                    cur   = ind.get("cur", 0)
                    rv    = ind.get("rsi", 50)
                    at    = ind.get("at", cur*0.01)
                    entry = cur
                    if sdir == "LONG":
                        sl  = max(cur - at*1.2, cur*0.92)
                        tp1 = cur + at*1.5
                        tp2 = cur + at*3.0
                    else:
                        sl  = min(cur + at*1.2, cur*1.08)
                        tp1 = max(cur - at*1.5, cur*0.01)
                        tp2 = max(cur - at*3.0, cur*0.01)

                    setups.append({
                        "symbol":  sym,
                        "dir":     sdir,
                        "score":   sp,
                        "trend":   tr,
                        "price":   cur,
                        "rsi":     rv,
                        "entry":   entry,
                        "sl":      sl,
                        "tp1":     tp1,
                        "tp2":     tp2,
                        "reasons": sreasons[:3],
                    })
            except Exception as e:
                logger.debug("Scan %s: %s", sym, e)

        await asyncio.sleep(0.2)  # rate limit

    # Sort by score descending
    setups.sort(key=lambda x: x["score"], reverse=True)
    return setups[:8]  # top 8 setups

# ---- MAIN ANALYSIS ----
async def analyze(coin_raw):
    coin = coin_raw.upper().strip().lstrip("/")
    if coin.endswith("USDT"): coin = coin[:-4]
    sym  = coin + "USDT"
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    logger.info("Analyzing %s", sym)

    conn = aiohttp.TCPConnector(ssl=False, limit=30)
    hdrs = {"User-Agent": "Mozilla/5.0"}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as s:

        sc2 = await get(s, f"{SPOT}/api/v3/ticker/price", {"symbol": sym}, timeout=6)
        fc  = await get(s, f"{FUT}/fapi/v1/ticker/price",  {"symbol": sym}, timeout=6)
        on_spot = isinstance(sc2, dict) and "price" in sc2
        on_fut  = isinstance(fc,  dict) and "price" in fc

        if not on_spot and not on_fut:
            return [f"Symbol {sym} not found. Try /BTC /ETH /SOL"]

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
            get_news(s, coin),
            return_exceptions=True,
        )

        def safe(x, t):
            return x if isinstance(x, t) else None

        spot_t=safe(results[0], dict); fut_t=safe(results[1], dict)
        prem=safe(results[2], dict);   oi=safe(results[3], dict)
        fund=safe(results[4], list);   ls=safe(results[5], list)
        oih=safe(results[6], list);    taker=safe(results[7], list)
        ki1h=safe(results[8], list);   ki4h=safe(results[9], list)
        ki1d=safe(results[10], list);  ki1w=safe(results[11], list)
        btc_t=safe(results[12], dict); eth_t=safe(results[13], dict)
        fg=safe(results[14], dict);    news=results[15] if isinstance(results[15], list) else []

        price = 0.0
        if fut_t:    price = float(fut_t.get("lastPrice", 0))
        elif spot_t: price = float(spot_t.get("lastPrice", 0))

        i1h=compute(ki1h) if ki1h else {}
        i4h=compute(ki4h) if ki4h else {}
        i1d=compute(ki1d) if ki1d else {}
        i1w=compute(ki1w) if ki1w else {}

        s1h=score(i1h); s4h=score(i4h); s1d=score(i1d)
        fr_val = float(prem.get("lastFundingRate", 0))*100 if prem else 0.0

        action, conf, reasons, weighted = confluence(s1h, s4h, s1d, i4h, i1d, fr_val)
        fut_dir = "LONG" if "BUY" in action else ("SHORT" if "SELL" in action else "NO TRADE")

        # SMC detection on 4H
        smc4h  = detect_order_blocks(ki4h, 50) if ki4h else {}
        sdir, spoints, sreasons = smc_signal(smc4h, s4h)

        # Directional prediction
        rngs = predict_directional(price, i1h, i4h, i1d, i1w, weighted, sdir)

        ss = spot_setup(price, action, i4h, i1d)
        fs = fut_setup(price, fut_dir, i4h, fr_val)

        tr4h=detect_trend(i4h); tr24h=detect_trend(i1d)
        at4h=i4h.get("at") or 0
        vol_lbl = "HIGH" if at4h/price*100>3 else ("MEDIUM" if at4h/price*100>1.5 else "LOW") if price else "N/A"

        pred_dir = rngs.get("direction","NEUTRAL")
        pred_1d  = rngs.get("1D",{})
        pred_1w  = rngs.get("1W",{})

        ctx = {
            "coin":coin, "price":price,
            "change_24h":float(spot_t.get("priceChangePercent",0)) if spot_t else 0,
            "volume_24h":float(spot_t.get("quoteVolume",0)) if spot_t else 0,
            "trend_4h":tr4h, "trend_24h":tr24h,
            "funding_pct":fr_val,
            "oi":float(oi.get("openInterest",0)) if oi else 0,
            "long_pct":float(ls[0].get("longAccount",0))*100 if ls else 50,
            "rsi_1h":i1h.get("rsi"), "rsi_4h":i4h.get("rsi"), "rsi_1d":i1d.get("rsi"),
            "score_4h":s4h, "score_1d":s1d, "weighted":round(weighted,2),
            "signal":action, "conf":conf, "smc_dir":sdir,
            "btc_change":float(btc_t.get("priceChangePercent",0)) if btc_t else 0,
            "fear_greed":fg["data"][0]["value"] if fg and "data" in fg else "N/A",
            "spot_sl":ss["sl"], "spot_tp1":ss["tp1"],
            "pred_1d_target":pred_1d.get("target",price),
            "pred_1w_target":pred_1w.get("target",price),
            "atr_4h":at4h,
        }
        ai = await groq_ai(s, coin, ctx)

    # ---- FORMAT ----
    pages = []

    # PAGE 1
    p1=[]
    p1.append(f"*{coin}/USDT — Intelligence v11*")
    p1.append(f"_{now}_")
    p1.append("---")

    p1.append("\n*BROADER MARKET*")
    if btc_t:
        bc=float(btc_t.get("priceChangePercent",0)); bp=float(btc_t.get("lastPrice",0))
        p1.append(f"  BTC: `${fmt(bp,0)}` ({bc:+.2f}%)")
    if eth_t:
        ec=float(eth_t.get("priceChangePercent",0)); ep=float(eth_t.get("lastPrice",0))
        p1.append(f"  ETH: `${fmt(ep,0)}` ({ec:+.2f}%)")
    if fg and "data" in fg:
        fv=fg["data"][0]["value"]; fvc=fg["data"][0]["value_classification"]
        p1.append(f"  Fear & Greed: `{fv}/100` — _{fvc}_")

    p1.append(f"\n*{coin} PRICE*")
    p1.append(f"  Current: `${fmt(price,6)}`")
    if spot_t:
        ch=float(spot_t.get("priceChangePercent",0))
        h24=float(spot_t.get("highPrice",price)); l24=float(spot_t.get("lowPrice",price))
        vol=float(spot_t.get("quoteVolume",0))
        p1.append(f"  24h: `{ch:+.2f}%`  H:`${fmt(h24,6)}`  L:`${fmt(l24,6)}`")
        p1.append(f"  Vol: `{fmt(vol,0)} USDT`")

    p1.append(f"\n*MARKET STATE*")
    p1.append(f"  Trend 4H: `{tr4h}`  |  Trend 24H: `{tr24h}`")
    p1.append(f"  Volatility: `{vol_lbl}`  Strength: `{i4h.get('ts','N/A')}`")

    p1.append(f"\n*CONFLUENCE SIGNAL*")
    dir_e = "LONG" if "BUY" in action else ("SHORT" if "SELL" in action else "HOLD")
    p1.append(f"  *{dir_e}* — Confidence: `{conf}%`")
    p1.append(f"  Scores: 1H:`{s1h:+d}` 4H:`{s4h:+d}` 1D:`{s1d:+d}` W:`{weighted:+.1f}`")
    for r in reasons[:3]: p1.append(f"  - {r}")

    # SMC section
    p1.append(f"\n*SMC / ICT ANALYSIS (4H)*")
    p1.append(f"  Direction: *{sdir}* (strength: {spoints}/10)")
    for r in sreasons[:3]: p1.append(f"  - {r}")
    if smc4h.get("bos"):
        b=smc4h["bos"]; p1.append(f"  BOS {b['type']}: `${fmt(b['level'],6)}`")
    if smc4h.get("choch"):
        c=smc4h["choch"]; p1.append(f"  CHOCH {c['type']}")
    if smc4h.get("bullish_ob"):
        ob=smc4h["bullish_ob"][0]
        p1.append(f"  Bullish OB: `${fmt(ob['low'],6)}` - `${fmt(ob['high'],6)}`")
    if smc4h.get("bearish_ob"):
        ob=smc4h["bearish_ob"][0]
        p1.append(f"  Bearish OB: `${fmt(ob['low'],6)}` - `${fmt(ob['high'],6)}`")
    if smc4h.get("fvg_bull"):
        fvg=smc4h["fvg_bull"][-1]
        p1.append(f"  Bull FVG: `${fmt(fvg['bottom'],6)}` - `${fmt(fvg['top'],6)}`")
    if smc4h.get("fvg_bear"):
        fvg=smc4h["fvg_bear"][-1]
        p1.append(f"  Bear FVG: `${fmt(fvg['bottom'],6)}` - `${fmt(fvg['top'],6)}`")

    # DIRECTIONAL PRICE PREDICTION
    dir_arrow = "UP" if pred_dir=="LONG" else ("DOWN" if pred_dir=="SHORT" else "SIDEWAYS")
    p1.append(f"\n*PRICE PREDICTION — {pred_dir} ({dir_arrow})*")
    p1.append(f"  _Based on indicators + SMC + ATR_")
    for tf in ["1H","2H","4H","12H","1D","3D","1W","1M"]:
        r = rngs.get(tf,{})
        if r:
            if pred_dir == "LONG":
                p1.append(f"  `{tf:3}` Target: `${fmt(r['target'],4)}` (+{r['move_pct']:.1f}%)  Stop: `${fmt(r['stop'],4)}`")
            elif pred_dir == "SHORT":
                p1.append(f"  `{tf:3}` Target: `${fmt(r['target'],4)}` (-{r['move_pct']:.1f}%)  Stop: `${fmt(r['stop'],4)}`")
            else:
                p1.append(f"  `{tf:3}` Range: `${fmt(r.get('target',price),4)}` ({r['move_pct']:.1f}%)")

    pages.append("\n".join(p1))

    # PAGE 2 — Futures + Indicators + Trade Setup
    p2=[]
    if on_fut:
        p2.append("*FUTURES MARKET (LIVE)*")
        if fut_t:
            fp=float(fut_t.get("lastPrice",0)); fch=float(fut_t.get("priceChangePercent",0))
            p2.append(f"  Price: `${fmt(fp,6)}` ({fch:+.2f}%)  Vol: `{fmt(float(fut_t.get('quoteVolume',0)),0)}`")
        if prem:
            mark=float(prem.get("markPrice",0)); idx=float(prem.get("indexPrice",0))
            basis=(mark-idx)/idx*100 if idx else 0
            p2.append(f"  Mark: `${fmt(mark,6)}`  Index: `${fmt(idx,6)}`")
            p2.append(f"  Funding: `{fr_val:.4f}%`  Basis: `{basis:+.4f}%`")
            if fr_val>0.05: p2.append("   -> High funding: dump risk")
            elif fr_val<-0.02: p2.append("   -> Negative funding: squeeze possible")
        if oi: p2.append(f"  OI: `{fmt(float(oi.get('openInterest',0)),2)} {coin}`")
        if oih and len(oih)>=2:
            ov=[float(x.get("sumOpenInterest",0)) for x in oih]
            oc=(ov[-1]-ov[0])/ov[0]*100 if ov[0] else 0
            p2.append(f"  OI Trend 12h: `{oc:+.2f}%`")
        if ls:
            lp=float(ls[0].get("longAccount",0))*100; sp=100-lp
            p2.append(f"  Long/Short: `{lp:.1f}% / {sp:.1f}%`")
            if lp>68: p2.append("   -> Extreme longs: liq risk")
            elif sp>65: p2.append("   -> Heavy shorts: squeeze possible")
        if taker:
            tb=float(taker[0].get("buySell",1))
            p2.append(f"  Taker B/S: `{tb:.3f}` ({'Buy dom' if tb>1 else 'Sell dom'})")
        if fund and len(fund)>=3:
            rates=[float(f.get("fundingRate",0))*100 for f in fund]
            p2.append(f"  Avg Funding(5): `{sum(rates)/len(rates):.4f}%`")

    def iblock(lbl, ind, sc_val):
        if not ind: return f"\n*{lbl}* — No data"
        cur=ind.get("cur",0); sl=sig_label(sc_val); tr=detect_trend(ind); ts=ind.get("ts","N/A")
        lines=[f"\n*{lbl}* — {sl} ({sc_val:+d}/10) | {tr} [{ts}]"]
        rv=ind.get("rsi")
        if rv is not None:
            zone="OB" if rv>70 else ("OS" if rv<30 else "Normal")
            lines.append(f"  RSI: `{rv}` {zone}")
        for nm,ky in [("EMA9","e9"),("EMA21","e21"),("EMA50","e50"),("EMA200","e200")]:
            v=ind.get(ky)
            if v:
                d2=(cur-v)/v*100
                lines.append(f"  {nm}: `{fmt(v,6)}` ({d2:+.2f}%) {'>' if cur>v else '<'}")
        ml=ind.get("macd"); hv=ind.get("hist")
        if ml is not None:
            cross="Bull" if (hv or 0)>0 else "Bear"
            lines.append(f"  MACD: {cross} `{fmt(hv,8)}`")
        bl2=ind.get("bl"); bu2=ind.get("bu"); bm2=ind.get("bm")
        if bl2 and bu2:
            rng2=bu2-bl2; pos=(cur-bl2)/rng2*100 if rng2 else 50
            tag=" [LOW]" if cur<=bl2 else (" [HIGH]" if cur>=bu2 else f" [{pos:.0f}%]")
            lines.append(f"  BB: `{fmt(bl2,6)}`/`{fmt(bm2,6)}`/`{fmt(bu2,6)}`{tag}")
        at2=ind.get("at"); vw2=ind.get("vw"); vr2=ind.get("vr")
        if at2: lines.append(f"  ATR: `{fmt(at2,6)}` ({at2/cur*100:.2f}%)")
        if vw2: lines.append(f"  VWAP: `{fmt(vw2,6)}` ({'>' if cur>vw2 else '<'})")
        if vr2: lines.append(f"  Vol: `{vr2:.2f}x`")
        return "\n".join(lines)

    p2.append(iblock("1H", i1h, s1h))
    p2.append(iblock("4H", i4h, s4h))
    p2.append(iblock("1D", i1d, s1d))
    if i1w: p2.append(iblock("1W", i1w, score(i1w)))

    if i1d.get("bl"):
        p2.append("\n*KEY LEVELS*")
        p2.append(f"  1D Sup: `${fmt(i1d.get('bl'),6)}`  Res: `${fmt(i1d.get('bu'),6)}`")
        if i4h.get("bl"):
            p2.append(f"  4H Sup: `${fmt(i4h.get('bl'),6)}`  Res: `${fmt(i4h.get('bu'),6)}`")
        if price:
            sup=i1d.get("bl",price); res=i1d.get("bu",price)
            if res>price: p2.append(f"  To Res: `+{(res-price)/price*100:.2f}%`")
            if sup<price: p2.append(f"  To Sup: `-{(price-sup)/price*100:.2f}%`")

    p2.append("\n*TRADE SETUPS*")
    p2.append(f"\n*SPOT: {action}* (conf: {conf}%)")
    p2.append(f"  Entry: `${fmt(ss['entry'],6)}`  SL: `${fmt(ss['sl'],6)}` (-{ss['sl_pct']:.2f}%)")
    p2.append(f"  TP1:   `${fmt(ss['tp1'],6)}` (+{ss['tp1_pct']:.2f}%)  R/R: `{ss['rr']}:1`")
    p2.append(f"  TP2:   `${fmt(ss['tp2'],6)}`  TP3: `${fmt(ss['tp3'],6)}`")

    p2.append(f"\n*FUTURES 25x: {fut_dir}*  WARNING: EXTREME RISK")
    if fs:
        p2.append(f"  Entry: `${fmt(fs['entry'],6)}`")
        p2.append(f"  SL:    `${fmt(fs['sl'],6)}` (-{fs['sl_pct']:.2f}% | PnL:{fs['pnl_sl']:+.0f}%)")
        p2.append(f"  Liq:   `${fmt(fs['liq'],6)}` (buffer:{fs['liq_buf']:.2f}%)")
        p2.append(f"  TP1:   `${fmt(fs['tp1'],6)}` (+{fs['pnl_tp1']:.0f}%)")
        p2.append(f"  TP2:   `${fmt(fs['tp2'],6)}` (+{fs['pnl_tp2']:.0f}%)")
        p2.append(f"  TP3:   `${fmt(fs['tp3'],6)}`  R/R:`{fs['rr']}:1`  Margin:2%")
    else:
        p2.append(f"  No clear signal — wait for confluence")

    pages.append("\n".join(p2))

    # PAGE 3 — News + AI
    p3=[]
    p3.append("*LATEST NEWS*")
    p3.append("---")
    if news:
        for n in news[:4]:
            s_sym = "+" if n.get("sentiment")=="bullish" else ("-" if n.get("sentiment")=="bearish" else "~")
            p3.append(f"  [{s_sym}] {n.get('title','')[:80]}")
            if n.get("source"): p3.append(f"       Source: {n['source']}")
    else:
        p3.append("  No recent news found.")
    p3.append("---")

    p3.append("\n*AI DECISION LAYER (Groq)*")
    p3.append("---")
    if ai:
        sent=ai.get("sentiment","N/A"); asc=ai.get("score",5)
        p3.append(f"  Sentiment: *{sent}* ({asc}/10)")
        if ai.get("context"): p3.append(f"  {ai['context']}")

        adir=ai.get("direction","N/A"); aconf=ai.get("confidence",0)
        p3.append(f"\n  Direction: *{adir}* ({aconf}%)")
        t1d=ai.get("target_1d"); t1w=ai.get("target_1w")
        if t1d and float(t1d)>0: p3.append(f"  1D Target: `${fmt(t1d,4)}`")
        if t1w and float(t1w)>0: p3.append(f"  1W Target: `${fmt(t1w,4)}`")

        ks=ai.get("key_support"); kr=ai.get("key_resistance")
        if ks and float(ks)>0: p3.append(f"  Support: `${fmt(ks,6)}`  Resistance: `${fmt(kr,6)}`")

        bull=ai.get("why_bull",[])
        if bull:
            p3.append(f"\n  Bull Case:")
            for r in bull[:3]: p3.append(f"  + {r}")

        bear=ai.get("why_bear",[])
        if bear:
            p3.append(f"\n  Bear Case:")
            for r in bear[:3]: p3.append(f"  - {r}")

        pc=ai.get("pump_catalyst"); dt=ai.get("dump_trigger")
        if pc: p3.append(f"\n  Pump: {pc}")
        if dt: p3.append(f"  Dump: {dt}")

        sa=ai.get("spot_action","N/A")
        s_sl=ai.get("spot_sl"); s_tp=ai.get("spot_tp1")
        p3.append(f"\n  AI Spot: *{sa}*")
        if s_sl and float(s_sl)>0:
            p3.append(f"  SL: `${fmt(s_sl,6)}`  TP1: `${fmt(s_tp,6)}`")

        fd=ai.get("fut_dir","N/A")
        f_e=ai.get("fut_entry"); f_sl=ai.get("fut_sl"); f_tp=ai.get("fut_tp1")
        p3.append(f"\n  AI Futures 25x: *{fd}*")
        if f_e and float(f_e)>0:
            p3.append(f"  Entry: `${fmt(f_e,6)}`  SL: `${fmt(f_sl,6)}`  TP1: `${fmt(f_tp,6)}`")

        rl=ai.get("risk","N/A"); rn=ai.get("risk_note","")
        p3.append(f"\n  Risk: *{rl}* — {rn}")
    else:
        p3.append("  AI decision layer unavailable.")
        p3.append("  Quant setups on previous page are still valid.")

    p3.append("\n---")
    p3.append("Not financial advice. 25x = extreme risk. DYOR.")
    pages.append("\n".join(p3))
    return pages

# ---- SCANNER COMMAND ----
async def run_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT: return
    msg = await update.message.reply_text(
        "Scanning all Binance futures for OB/SMC setups...\n~60 seconds",
        parse_mode="Markdown")
    try:
        conn = aiohttp.TCPConnector(ssl=False, limit=30)
        hdrs = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession(connector=conn, headers=hdrs) as s:
            setups = await scan_futures(s, top_n=100)

        if not setups:
            await msg.edit_text("No strong OB/SMC setups found right now. Try again later.")
            return

        lines = ["*BINANCE FUTURES SCANNER*"]
        lines.append("_OB / SMC / ICT setups (4H)_")
        lines.append("---")
        for i, st in enumerate(setups, 1):
            sym   = st["symbol"].replace("USDT","")
            d_sym = "LONG" if st["dir"]=="LONG" else "SHORT"
            lines.append(
                f"\n{i}. *{sym}* — {d_sym} (score:{st['score']})"
            )
            lines.append(f"   Price: `${fmt(st['price'],6)}`  RSI:`{st['rsi']}`")
            lines.append(f"   Trend: `{st['trend']}`")
            lines.append(f"   Entry: `${fmt(st['entry'],6)}`")
            lines.append(f"   SL: `${fmt(st['sl'],6)}`  TP1: `${fmt(st['tp1'],6)}`  TP2: `${fmt(st['tp2'],6)}`")
            for r in st["reasons"][:2]:
                lines.append(f"   - {r}")

        lines.append("\n---")
        lines.append(f"Scanned at {datetime.utcnow().strftime('%H:%M UTC')}")
        lines.append("Not financial advice. DYOR.")
        await msg.edit_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.error("Scan error: %s", e, exc_info=True)
        await msg.edit_text(f"Scan error: {str(e)[:150]}")

# ---- TELEGRAM ----
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT: return
    text = update.message.text or ""
    cmd  = text.split()[0].lstrip("/").split("@")[0].strip()

    if cmd.lower() in ("start", "help"):
        await update.message.reply_text(
            "*Crypto Intelligence Bot v11*\n\n"
            "Commands:\n"
            "/BTC /ETH /SOL /PEPE — full analysis\n"
            "/scan — scan all futures for OB/SMC setups\n\n"
            "Features:\n"
            "- Directional price prediction (LONG/SHORT)\n"
            "- SMC/ICT: OB, FVG, BOS, CHOCH\n"
            "- Spot + Futures 25x setup\n"
            "- CryptoPanic news\n"
            "- Groq AI analysis",
            parse_mode="Markdown")
        return

    if cmd.lower() == "scan":
        await run_scan(update, context)
        return

    if not cmd: return

    msg = await update.message.reply_text(
        f"Analyzing {cmd.upper()}/USDT... (~25 sec)",
        parse_mode="Markdown")
    try:
        pages = await analyze(cmd)
        await msg.delete()
        for i, page in enumerate(pages):
            if page.strip():
                await update.message.reply_text(page.strip(), parse_mode="Markdown")
                if i < len(pages)-1: await asyncio.sleep(0.5)
    except Exception as e:
        logger.error("Error: %s", e, exc_info=True)
        try: await msg.edit_text(f"Error: {str(e)[:200]}")
        except Exception: pass

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", handler))
    app.add_handler(CommandHandler("help",  handler))
    app.add_handler(CommandHandler("scan",  run_scan))
    app.add_handler(MessageHandler(filters.COMMAND, handler))
    logger.info("Crypto Intelligence Bot v11 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
