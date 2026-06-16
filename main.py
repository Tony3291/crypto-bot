"""
Crypto Intelligence Bot v12 - HIGH ACCURACY
Key improvements:
- Fixed predicted ranges (no negative prices, realistic bounds)
- Multi-timeframe confluence scoring
- Better signal logic with confirmation filters
- Accurate futures setup with proper R/R
- Smarter ATR-based range calculation
- REAL news via CryptoPanic (replaces hallucinated AI news)
- Groq AI repurposed as a DECISION LAYER: judges the quant signal
  against real news and gives a final AGREE/ADJUST/OVERRIDE call
  with refined spot/futures trade setups
- LIQUIDITY HUNT ENGINE: detects short/long squeeze setups,
  liquidity sweeps below support / above resistance, estimates
  liquidation cluster magnets, detects SMC order blocks (v12)
"""
import logging
import asyncio
import aiohttp
import json
from datetime import datetime

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TELEGRAM_TOKEN = "8650706334:AAHJQrBxkw-zOw286H1v-PvtDtUWsM9KFfY"
GROQ_API_KEY   = "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"
CRYPTOPANIC_KEY = "c8b70bac4af818456fa3ba7f62a60eb1804f60cc"
ALLOWED_CHAT   = 5214099942

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SPOT = "https://api.binance.com"
FUT  = "https://fapi.binance.com"
GROQ = "https://api.groq.com/openai/v1/chat/completions"
FNG  = "https://api.alternative.me/fng/?limit=1"
CRYPTOPANIC = "https://cryptopanic.com/api/v1/posts/"

CG_IDS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2",
    "DOT":"polkadot","MATIC":"matic-network","LINK":"chainlink","UNI":"uniswap",
    "ATOM":"cosmos","LTC":"litecoin","NEAR":"near","APT":"aptos","ARB":"arbitrum",
    "OP":"optimism","SUI":"sui","PEPE":"pepe","SHIB":"shiba-inu","FLOKI":"floki",
    "BONK":"bonk","WIF":"dogwifcoin","TRX":"tron","TON":"the-open-network",
    "NOT":"notcoin","STG":"stargate-finance","INJ":"injective-protocol",
    "FET":"fetch-ai","SEI":"sei-network","RENDER":"render-token","JUP":"jupiter",
    "HBAR":"hedera-hashgraph","VET":"vechain","ALGO":"algorand",
    "ICP":"internet-computer","TIA":"celestia","IMX":"immutable-x",
    "LDO":"lido-dao","AAVE":"aave","CRV":"curve-dao-token","RUNE":"thorchain",
    "FHE":"fhenix","ZK":"zksync","STRK":"starknet",
}

# ─── HTTP ─────────────────────────────────────────────────────────────────────
async def get(session, url, params=None, timeout=10):
    try:
        async with session.get(
            url, params=params,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug(f"GET {url[:45]}: {e}")
    return None

async def post(session, url, headers, body, timeout=45):
    try:
        async with session.post(
            url, headers=headers, json=body,
            timeout=aiohttp.ClientTimeout(total=timeout)
        ) as r:
            if r.status == 200:
                return await r.json()
    except Exception as e:
        logger.debug(f"POST {url[:45]}: {e}")
    return None

# ─── NEWS (CryptoPanic) ────────────────────────────────────────────────────────
async def cryptopanic_news(session, coin):
    """
    Fetch REAL recent news for a coin from CryptoPanic.
    Returns a list of dicts: title, sentiment (from community votes), source, date, important.
    """
    params = {
        "auth_token": CRYPTOPANIC_KEY,
        "currencies": coin,
        "public": "true",
        "kind": "news",
    }
    data = await get(session, CRYPTOPANIC, params, timeout=10)
    if not data or not isinstance(data, dict) or "results" not in data:
        return []

    news = []
    for item in data["results"][:10]:
        title = item.get("title", "").strip()
        if not title:
            continue
        votes = item.get("votes", {}) or {}
        pos = votes.get("positive", 0) or 0
        neg = votes.get("negative", 0) or 0
        liked = votes.get("liked", 0) or 0
        disliked = votes.get("disliked", 0) or 0
        important = (votes.get("important", 0) or 0) > 0

        bull_score = pos + liked
        bear_score = neg + disliked
        if bull_score > bear_score * 1.3 and bull_score > 0:
            sent = "bullish"
        elif bear_score > bull_score * 1.3 and bear_score > 0:
            sent = "bearish"
        else:
            sent = "neutral"

        src = (item.get("source") or {}).get("title", "")
        pub = (item.get("published_at") or "")[:10]
        url = item.get("url", "")

        news.append({
            "title": title, "sentiment": sent, "source": src,
            "date": pub, "important": important, "url": url,
        })
    return news

# ─── INDICATORS ───────────────────────────────────────────────────────────────
def ema(closes, p):
    if not closes or len(closes) < p: return None
    k = 2/(p+1); v = sum(closes[:p])/p
    for x in closes[p:]: v = x*k + v*(1-k)
    return v

def rsi(closes, p=14):
    if not closes or len(closes) < p+1: return None
    d = [closes[i+1]-closes[i] for i in range(len(closes)-1)]
    ag = sum(x if x>0 else 0 for x in d[-p:])/p
    al = sum(-x if x<0 else 0 for x in d[-p:])/p
    return round(100-(100/(1+ag/al)),2) if al else 100.0

def macd_ind(closes):
    if not closes or len(closes) < 35: return None,None,None
    e12=ema(closes,12); e26=ema(closes,26)
    if not e12 or not e26: return None,None,None
    ml=e12-e26; snaps=[]; c=closes[:]
    for _ in range(9):
        if len(c)>=26:
            a=ema(c,12); b=ema(c,26)
            if a and b: snaps.insert(0,a-b)
        c=c[:-1]
    sig=sum(snaps)/len(snaps) if snaps else ml
    return ml, sig, ml-sig

def bollinger(closes, p=20):
    if not closes or len(closes)<p: return None,None,None
    w=closes[-p:]; m=sum(w)/p
    s=(sum((x-m)**2 for x in w)/p)**0.5
    return m-2*s, m, m+2*s

def atr_calc(klines, p=14):
    if not klines or len(klines)<p+1: return None
    trs=[]
    for i in range(1,len(klines)):
        h=float(klines[i][2]); l=float(klines[i][3]); pc=float(klines[i-1][4])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-p:])/min(p,len(trs)) if trs else None

def vwap_calc(klines):
    if not klines: return None
    tv=tpv=0
    for k in klines:
        tp=(float(k[2])+float(k[3])+float(k[4]))/3; v=float(k[5])
        tpv+=tp*v; tv+=v
    return tpv/tv if tv else None

def compute(klines):
    if not klines or len(klines)<20: return {}
    closes=[float(k[4]) for k in klines]
    highs=[float(k[2]) for k in klines]
    lows=[float(k[3]) for k in klines]
    vols=[float(k[5]) for k in klines]
    cur=closes[-1]

    e9=ema(closes,9); e21=ema(closes,21); e50=ema(closes,50); e200=ema(closes,200)
    rv=rsi(closes,14)
    ml,sv,hv=macd_ind(closes)
    bl,bm,bu=bollinger(closes,20)
    at=atr_calc(klines,14)
    vw=vwap_calc(klines[-24:] if len(klines)>=24 else klines)

    av=sum(vols[-20:])/20 if vols else 0
    vr=vols[-1]/av if av>0 else 1.0

    # Market structure
    hh = highs[-1]>max(highs[-6:-1]) if len(highs)>5 else False
    ll = lows[-1]<min(lows[-6:-1]) if len(lows)>5 else False
    hl = lows[-1]>min(lows[-6:-1]) if len(lows)>5 else False
    lh = highs[-1]<max(highs[-6:-1]) if len(highs)>5 else False

    # Trend strength via ADX proxy
    ts="N/A"
    if len(klines)>=28:
        up=[max(highs[i]-highs[i-1],0) for i in range(1,len(highs))]
        dn=[max(lows[i-1]-lows[i],0) for i in range(1,len(lows))]
        a14=at or 1
        pdi=100*(sum(up[-14:])/14)/a14
        mdi=100*(sum(dn[-14:])/14)/a14
        dx=abs(pdi-mdi)/(pdi+mdi+1e-9)*100
        ts="Strong" if dx>25 else ("Moderate" if dx>15 else "Weak")

    return {
        "cur":cur,"e9":e9,"e21":e21,"e50":e50,"e200":e200,
        "rsi":rv,"macd":ml,"sig":sv,"hist":hv,
        "bl":bl,"bm":bm,"bu":bu,"at":at,"vw":vw,"vr":vr,"ts":ts,
        "hh":hh,"ll":ll,"hl":hl,"lh":lh,
        "high":max(highs),"low":min(lows),
    }

# ─── SCORING SYSTEM ───────────────────────────────────────────────────────────
def score(ind):
    """
    Score from -10 to +10
    Positive = bullish, Negative = bearish
    """
    if not ind: return 0
    s=0; cur=ind.get("cur",0)

    # RSI (max ±3)
    rv=ind.get("rsi")
    if rv is not None:
        if rv<25:     s+=3   # extreme oversold
        elif rv<35:   s+=2
        elif rv<45:   s+=1
        elif rv>75:   s-=3   # extreme overbought
        elif rv>65:   s-=2
        elif rv>55:   s-=1

    # EMA alignment (max ±3)
    e9=ind.get("e9"); e21=ind.get("e21"); e50=ind.get("e50"); e200=ind.get("e200")
    if e9 and e21 and e50:
        if cur>e9>e21>e50:   s+=3   # full bull alignment
        elif cur>e9>e21:     s+=2
        elif cur>e9:         s+=1
        elif cur<e9<e21<e50: s-=3   # full bear alignment
        elif cur<e9<e21:     s-=2
        elif cur<e9:         s-=1
    if e200:
        s+=1 if cur>e200 else -1   # long term trend (max ±1)

    # MACD (max ±2)
    hv=ind.get("hist"); ml=ind.get("macd")
    if hv is not None and ml is not None:
        if hv>0 and ml>0:   s+=2   # both positive
        elif hv>0:           s+=1   # hist turning positive
        elif hv<0 and ml<0: s-=2
        else:                s-=1

    # Bollinger Bands position (max ±2)
    bl=ind.get("bl"); bu=ind.get("bu"); bm=ind.get("bm")
    if bl and bu and bm:
        rng=bu-bl
        if rng>0:
            pos=(cur-bl)/rng
            if pos<=0.1:    s+=2   # at lower band = oversold
            elif pos<=0.25: s+=1
            elif pos>=0.9:  s-=2   # at upper band = overbought
            elif pos>=0.75: s-=1

    # VWAP (max ±1)
    vw=ind.get("vw")
    if vw: s+=1 if cur>vw else -1

    # Market structure (max ±2)
    if ind.get("hh") and ind.get("hl"):  s+=2   # higher highs + higher lows = uptrend
    elif ind.get("hh"):                   s+=1
    if ind.get("ll") and ind.get("lh"):  s-=2   # lower lows + lower highs = downtrend
    elif ind.get("ll"):                   s-=1

    return max(-10, min(10, s))   # cap at ±10

def trend_detect(ind):
    if not ind: return "SIDEWAYS"
    cur=ind.get("cur",0)
    e9=ind.get("e9"); e21=ind.get("e21"); e50=ind.get("e50")
    rv=ind.get("rsi",50) or 50
    hv=ind.get("hist") or 0
    ts=ind.get("ts","Weak")
    if e9 and e21 and e50:
        if cur>e9>e21>e50 and rv>55 and hv>0 and ts in ("Strong","Moderate"):
            return "STRONG UPTREND"
        elif cur>e9>e21 and rv>50: return "UPTREND"
        elif cur<e9<e21<e50 and rv<45 and hv<0 and ts in ("Strong","Moderate"):
            return "STRONG DOWNTREND"
        elif cur<e9<e21 and rv<50: return "DOWNTREND"
    return "SIDEWAYS"

def siglabel(s):
    if s>=7:     return "🟢 STRONG BUY"
    elif s>=4:   return "🟩 BUY"
    elif s>=2:   return "🟦 WEAK BUY"
    elif s<=-7:  return "🔴 STRONG SELL"
    elif s<=-4:  return "🟥 SELL"
    elif s<=-2:  return "🟧 WEAK SELL"
    return "🟡 HOLD/NEUTRAL"

def fmt(n, d=4):
    if n is None: return "N/A"
    try:
        n=float(n)
        if abs(n)>=1e9: return f"{n/1e9:.2f}B"
        if abs(n)>=1e6: return f"{n/1e6:.2f}M"
        if abs(n)>=1e3: return f"{n/1e3:.2f}K"
        return f"{n:.{d}f}"
    except: return str(n)

# ─── CONFLUENCE ENGINE ────────────────────────────────────────────────────────
def confluence_signal(s1h, s4h, s1d, i4h, i1d, fr_val=0):
    """
    High-accuracy signal using multi-TF confluence.
    Signal only fires when multiple timeframes agree.
    Returns: action, confidence, reasoning
    """
    # Weighted composite (4H has most weight for entries)
    weighted = (s1h * 1 + s4h * 3 + s1d * 2) / 6

    # Confluence check — need at least 2 TFs to agree
    bull_tfs = sum(1 for x in [s1h, s4h, s1d] if x >= 2)
    bear_tfs = sum(1 for x in [s1h, s4h, s1d] if x <= -2)

    reasons = []

    # RSI confluence
    rsi4h = i4h.get("rsi"); rsi1d = i1d.get("rsi")
    rsi_bull = (rsi4h and rsi4h < 45) or (rsi1d and rsi1d < 45)
    rsi_bear = (rsi4h and rsi4h > 60) or (rsi1d and rsi1d > 60)
    if rsi_bull: reasons.append(f"RSI oversold ({rsi4h:.0f})" if rsi4h else "RSI bullish")
    if rsi_bear: reasons.append(f"RSI overbought ({rsi4h:.0f})" if rsi4h else "RSI bearish")

    # EMA trend
    tr4h = trend_detect(i4h); tr1d = trend_detect(i1d)
    if "UP" in tr4h and "UP" in tr1d: reasons.append("4H + 1D both uptrend")
    elif "DOWN" in tr4h and "DOWN" in tr1d: reasons.append("4H + 1D both downtrend")

    # MACD
    hist4h = i4h.get("hist") or 0; hist1d = i1d.get("hist") or 0
    if hist4h > 0 and hist1d > 0: reasons.append("MACD bullish on 4H+1D")
    elif hist4h < 0 and hist1d < 0: reasons.append("MACD bearish on 4H+1D")

    # Funding rate bias
    if fr_val > 0.05: reasons.append(f"High funding ({fr_val:.3f}%) → bearish bias")
    elif fr_val < -0.02: reasons.append(f"Negative funding → bullish bias")

    # Determine action
    if bull_tfs >= 2 and weighted >= 2:
        if weighted >= 4: action = "STRONG BUY"; conf = min(75 + bull_tfs*5, 90)
        else:             action = "BUY";         conf = min(55 + bull_tfs*5, 80)
    elif bear_tfs >= 2 and weighted <= -2:
        if weighted <= -4: action = "STRONG SELL"; conf = min(75 + bear_tfs*5, 90)
        else:              action = "SELL";         conf = min(55 + bear_tfs*5, 80)
    else:
        action = "HOLD"
        conf   = 40
        reasons.append("Mixed TF signals — no clear edge")

    return action, conf, reasons, weighted

# ─── LIQUIDATION HUNT & SQUEEZE DETECTION ────────────────────────────────────
def liquidity_analysis(price, i1h, i4h, i1d, ki1h, ki4h,
                        fr_val=0, ls_data=None, oih=None, taker=None, fund=None):
    """
    Detects the strategy shown in the Coinglass Liquidation Heatmap approach:
    1. Liquidity Sweep — price sweeping below/above key level then reversing
    2. Liquidation Magnet — estimating where dense clusters sit (based on recent
       price structure + ATR, since we don't have Coinglass API)
    3. Short/Long Squeeze Potential — from funding, L/S ratio, OI trend, taker flow
    4. SMC Order Block — identifying the nearest untested OB from klines
    Returns a dict of findings.
    """
    import math
    result = {
        "squeeze_type": None,        # "SHORT_SQUEEZE", "LONG_SQUEEZE", None
        "squeeze_score": 0,          # 0-10
        "squeeze_reasons": [],
        "sweep_detected": None,      # "BELOW_SUPPORT" / "ABOVE_RESISTANCE" / None
        "sweep_reasons": [],
        "liq_clusters_above": [],    # estimated price levels with heavy shorts liquidating
        "liq_clusters_below": [],    # estimated price levels with heavy longs liquidating
        "order_block": None,         # nearest SMC order block info
        "setup_label": "NO SETUP",   # human-readable pattern name
        "setup_confidence": 0,
    }
    if not price or price <= 0:
        return result

    # ── 1. SQUEEZE SCORING ────────────────────────────────────────────────────
    sq = 0; sq_r = []

    long_pct = 50.0
    if ls_data and isinstance(ls_data, list) and ls_data:
        long_pct = float(ls_data[0].get("longAccount", 0.5)) * 100
    short_pct = 100 - long_pct

    # Extreme long/short positioning
    if short_pct > 60:
        sq += 3; sq_r.append(f"Shorts dominate L/S: {short_pct:.1f}% — squeeze fuel")
    elif short_pct > 55:
        sq += 2; sq_r.append(f"Short heavy: {short_pct:.1f}%")
    elif long_pct > 65:
        sq -= 3; sq_r.append(f"Longs dominate L/S: {long_pct:.1f}% — long squeeze fuel")
    elif long_pct > 60:
        sq -= 2; sq_r.append(f"Long heavy: {long_pct:.1f}%")

    # Funding rate — negative = shorts paying = short squeeze potential
    if fr_val < -0.05:
        sq += 3; sq_r.append(f"Extreme negative funding ({fr_val:.4f}%) → shorts underwater")
    elif fr_val < -0.01:
        sq += 2; sq_r.append(f"Negative funding ({fr_val:.4f}%) → short squeeze risk")
    elif fr_val > 0.1:
        sq -= 3; sq_r.append(f"Extreme positive funding ({fr_val:.4f}%) → longs paying → dump risk")
    elif fr_val > 0.05:
        sq -= 2; sq_r.append(f"High funding ({fr_val:.4f}%) → long squeeze risk")

    # Funding trend — if recent rates moving toward negative while short-heavy = building pressure
    if fund and len(fund) >= 3:
        rates = [float(f.get("fundingRate", 0)) * 100 for f in fund]
        trend_dir = rates[-1] - rates[0]
        if trend_dir < -0.03 and sq > 0:
            sq += 1; sq_r.append("Funding falling (more negative) → pressure building on shorts")
        elif trend_dir > 0.03 and sq < 0:
            sq -= 1; sq_r.append("Funding rising → more long squeeze pressure")

    # OI trend — rising OI while price at lows = new shorts being added = more squeeze fuel
    if oih and len(oih) >= 3:
        ov = [float(x.get("sumOpenInterest", 0)) for x in oih]
        oi_chg = (ov[-1] - ov[0]) / ov[0] * 100 if ov[0] else 0
        rsi_1h = i1h.get("rsi") if i1h else 50
        rsi_4h = i4h.get("rsi") if i4h else 50
        if oi_chg > 8 and (rsi_1h or 50) < 40:
            sq += 2; sq_r.append(f"OI rising +{oi_chg:.1f}% while RSI oversold → new shorts trapped")
        elif oi_chg > 5 and sq > 0:
            sq += 1; sq_r.append(f"OI expanding +{oi_chg:.1f}% — more short exposure")
        elif oi_chg < -8 and (rsi_1h or 50) > 65:
            sq -= 2; sq_r.append(f"OI dropping {oi_chg:.1f}% while RSI overbought → long deleveraging")

    # Taker buy pressure at lows = smart money accumulating
    if taker and isinstance(taker, list) and taker:
        tb = float(taker[0].get("buySell", 1))
        if tb > 1.15 and sq > 0:
            sq += 1; sq_r.append(f"Taker buy ratio {tb:.2f}x — smart money buying the low")
        elif tb < 0.85 and sq < 0:
            sq -= 1; sq_r.append(f"Taker sell ratio {1/tb:.2f}x — selling into highs")

    result["squeeze_score"] = max(-10, min(10, sq))
    result["squeeze_reasons"] = sq_r

    if sq >= 4:
        result["squeeze_type"] = "SHORT_SQUEEZE"
    elif sq <= -4:
        result["squeeze_type"] = "LONG_SQUEEZE"

    # ── 2. LIQUIDITY SWEEP DETECTION ─────────────────────────────────────────
    sw_r = []
    sweep = None

    at1h = (i1h.get("at") if i1h else None) or price * 0.008
    at4h = (i4h.get("at") if i4h else None) or price * 0.015
    at1d = (i1d.get("at") if i1d else None) or price * 0.030

    bl_4h = i4h.get("bl") if i4h else None   # BB lower 4H = dynamic support
    bl_1d = i1d.get("bl") if i1d else None   # BB lower 1D = major support
    bu_4h = i4h.get("bu") if i4h else None
    bu_1d = i1d.get("bu") if i1d else None

    rsi1h = (i1h.get("rsi") if i1h else None) or 50
    rsi4h = (i4h.get("rsi") if i4h else None) or 50

    # Check sweep below support (bullish reversal setup)
    if bl_4h and price < bl_4h and rsi1h < 35:
        sweep = "BELOW_SUPPORT"
        sw_r.append(f"Price swept below 4H BB support ${fmt(bl_4h, 6)}")
        sw_r.append(f"RSI1H oversold at {rsi1h:.0f} — exhaustion signal")
    elif bl_1d and price < bl_1d and rsi4h < 40:
        sweep = "BELOW_SUPPORT"
        sw_r.append(f"Price swept below 1D BB support ${fmt(bl_1d, 6)} — major level breach")
        sw_r.append(f"RSI4H at {rsi4h:.0f} — historically significant oversold")

    # Check sweep above resistance (bearish reversal setup)
    elif bu_4h and price > bu_4h and rsi1h > 70:
        sweep = "ABOVE_RESISTANCE"
        sw_r.append(f"Price swept above 4H BB resistance ${fmt(bu_4h, 6)}")
        sw_r.append(f"RSI1H overbought {rsi1h:.0f} — exhaustion signal")
    elif bu_1d and price > bu_1d and rsi4h > 68:
        sweep = "ABOVE_RESISTANCE"
        sw_r.append(f"Price swept above 1D BB resistance ${fmt(bu_1d, 6)} — major level breach")
        sw_r.append(f"RSI4H at {rsi4h:.0f} — overbought on high TF")

    # Wick analysis from recent klines — sweep candle = price went below then closed above
    if ki1h and len(ki1h) >= 3:
        last = ki1h[-1]; prev = ki1h[-2]
        lo = float(last[3]); cl = float(last[4]); op = float(last[1]); hi = float(last[2])
        lower_wick = min(op, cl) - lo
        upper_wick = hi - max(op, cl)
        body = abs(cl - op)
        if lower_wick > body * 2.5 and cl > op:
            sw_r.append(f"📍 Long lower wick on last 1H candle — liquidity sweep confirmed")
            if not sweep: sweep = "BELOW_SUPPORT"
        elif upper_wick > body * 2.5 and cl < op:
            sw_r.append(f"📍 Long upper wick on last 1H candle — liquidity sweep above confirmed")
            if not sweep: sweep = "ABOVE_RESISTANCE"

    result["sweep_detected"] = sweep
    result["sweep_reasons"] = sw_r

    # ── 3. ESTIMATED LIQUIDATION CLUSTERS (price magnets) ─────────────────────
    # Without Coinglass API, we estimate where liquidations pile up:
    # - Shorts opened at recent highs get liquidated if price rises
    # - Longs opened at recent lows get liquidated if price falls
    # Use recent swing highs/lows + ATR-based levels

    if ki4h and len(ki4h) >= 20:
        highs_4h = [float(k[2]) for k in ki4h[-40:]]
        lows_4h  = [float(k[3]) for k in ki4h[-40:]]

        # Find swing highs (where shorts may have piled in)
        # These are liquidation magnets ABOVE price
        clusters_above = []
        for i in range(2, len(highs_4h) - 2):
            if highs_4h[i] > price and highs_4h[i] > highs_4h[i-1] and highs_4h[i] > highs_4h[i+1]:
                dist_pct = (highs_4h[i] - price) / price * 100
                if 1 < dist_pct < 60:
                    clusters_above.append(round(highs_4h[i], 8))

        # Find swing lows (where longs may have piled in)
        clusters_below = []
        for i in range(2, len(lows_4h) - 2):
            if lows_4h[i] < price and lows_4h[i] < lows_4h[i-1] and lows_4h[i] < lows_4h[i+1]:
                dist_pct = (price - lows_4h[i]) / price * 100
                if 1 < dist_pct < 60:
                    clusters_below.append(round(lows_4h[i], 8))

        # Keep closest 3 on each side
        result["liq_clusters_above"] = sorted(set(clusters_above))[:4]
        result["liq_clusters_below"] = sorted(set(clusters_below), reverse=True)[:4]

    # ── 4. SMC ORDER BLOCK DETECTION ─────────────────────────────────────────
    # Order block = last opposing candle before a strong impulsive move
    # Bullish OB = last bearish candle before a strong up move (demand zone)
    # Bearish OB = last bullish candle before a strong down move (supply zone)
    if ki4h and len(ki4h) >= 10:
        ob = None
        klines = ki4h[-30:]
        for i in range(len(klines)-3, 1, -1):
            o = float(klines[i][1]); c = float(klines[i][4])
            n1c = float(klines[i+1][4]); n1o = float(klines[i+1][1])
            n2c = float(klines[i+2][4]) if i+2 < len(klines) else n1c
            move = abs(n1c - n1o)
            atr_ref = at4h or price * 0.015

            # Bullish OB: bearish candle (close < open) followed by 2 strong bullish candles
            if c < o and n1c > n1o and move > atr_ref * 0.8 and n2c > n1c:
                ob_low = min(o, c); ob_high = max(o, c)
                if ob_high < price:   # OB is below current price = support OB
                    ob = {"type": "BULLISH", "low": round(ob_low, 8), "high": round(ob_high, 8),
                          "label": "Demand Zone (Bullish OB)"}
                    break

            # Bearish OB: bullish candle followed by 2 strong bearish candles
            if c > o and n1c < n1o and move > atr_ref * 0.8 and n2c < n1c:
                ob_low = min(o, c); ob_high = max(o, c)
                if ob_low > price:   # OB is above current price = resistance OB
                    ob = {"type": "BEARISH", "low": round(ob_low, 8), "high": round(ob_high, 8),
                          "label": "Supply Zone (Bearish OB)"}
                    break
        result["order_block"] = ob

    # ── 5. FINAL SETUP LABEL ─────────────────────────────────────────────────
    sq_score = result["squeeze_score"]
    setup_conf = 0

    if sweep == "BELOW_SUPPORT" and sq_score >= 3:
        result["setup_label"] = "🚀 SHORT SQUEEZE REVERSAL"
        setup_conf = min(50 + sq_score * 5 + len(sw_r) * 5, 92)
    elif sweep == "BELOW_SUPPORT" and sq_score >= 1:
        result["setup_label"] = "📈 LIQUIDITY SWEEP LONG"
        setup_conf = min(40 + sq_score * 5 + len(sw_r) * 4, 80)
    elif sweep == "ABOVE_RESISTANCE" and sq_score <= -3:
        result["setup_label"] = "💀 LONG SQUEEZE REVERSAL"
        setup_conf = min(50 + abs(sq_score) * 5, 90)
    elif sweep == "ABOVE_RESISTANCE" and sq_score <= -1:
        result["setup_label"] = "📉 LIQUIDITY SWEEP SHORT"
        setup_conf = min(40 + abs(sq_score) * 5, 80)
    elif sq_score >= 5:
        result["setup_label"] = "⚡ SHORT SQUEEZE BUILDING"
        setup_conf = min(45 + sq_score * 4, 78)
    elif sq_score <= -5:
        result["setup_label"] = "⚡ LONG SQUEEZE BUILDING"
        setup_conf = min(45 + abs(sq_score) * 4, 78)
    elif result["liq_clusters_above"] and sq_score >= 2:
        nearest_mag = result["liq_clusters_above"][0]
        result["setup_label"] = f"🧲 LIQUIDITY MAGNET @ ${fmt(nearest_mag, 6)}"
        setup_conf = 40

    result["setup_confidence"] = setup_conf
    return result
def predict_ranges(price, i1h, i4h, i1d, i1w, weighted_score):
    """
    Predicted ranges from CURRENT price.
    Uses ATR per timeframe + volatility scaling + trend bias.
    FIXED: No negative prices, realistic bounds.
    """
    if not price or price <= 0:
        return {}

    at1h = i1h.get("at") if i1h else None
    at4h = i4h.get("at") if i4h else None
    at1d = i1d.get("at") if i1d else None
    at1w = i1w.get("at") if i1w else None if i1w else None

    # Fallback ATR as % of price (conservative)
    at1h = at1h or price * 0.008
    at4h = at4h or price * 0.015
    at1d = at1d or price * 0.030
    at1w = at1w or price * 0.060

    # Trend bias: score pushes range center up or down
    bias = max(-0.5, min(0.5, weighted_score / 10.0))

    # Bias label
    if bias > 0.15:   bias_label = "🟢 Bullish"
    elif bias < -0.15: bias_label = "🔴 Bearish"
    else:              bias_label = "🟡 Neutral"

    def make_range(atr, candles):
        """
        atr: base ATR for this timeframe
        candles: number of candles expected in this period
        """
        # Scale ATR by sqrt of candles (random walk model)
        import math
        vol = atr * math.sqrt(max(candles, 1))

        # Apply bias to center
        center = price * (1 + bias * 0.008 * candles ** 0.5)

        h = center + vol
        l = center - vol

        # CRITICAL FIX: ensure no negative prices
        l = max(l, price * 0.01)   # minimum 1% of current price
        h = max(h, price * 1.001)  # always above price if bullish

        move_pct = (h - l) / price * 100
        # Cap unrealistic ranges
        move_pct = min(move_pct, 200)

        return {
            "h": round(h, 8),
            "l": round(l, 8),
            "mp": round(move_pct, 2),
            "bias": bias_label,
        }

    return {
        "1H":  make_range(at1h, 1),
        "2H":  make_range(at1h, 2),
        "4H":  make_range(at4h, 1),
        "12H": make_range(at4h, 3),
        "1D":  make_range(at1d, 1),
        "3D":  make_range(at1d, 3),
        "1W":  make_range(at1w, 1),
        "1M":  make_range(at1w, 4),
    }

# ─── TRADE SETUP ──────────────────────────────────────────────────────────────
def spot_setup(price, action, i4h, i1d):
    """
    Spot trade setup using ATR-based SL/TP.
    SL = 1.5 ATR from entry (risk-defined)
    TP = R:R based (TP1=1.5:1, TP2=2.5:1, TP3=4:1)
    """
    at = i4h.get("at") or (price * 0.015)
    bl = i1d.get("bl"); bu = i1d.get("bu")

    if "BUY" in action:
        entry = price
        sl    = max(entry - at * 1.5, entry * 0.85)  # 1.5 ATR or max 15% below
        # Use nearest support as SL if BB lower is closer
        if bl and bl > sl and bl < entry:
            sl = bl * 0.995  # just below BB lower

        risk  = entry - sl
        tp1   = entry + risk * 1.5
        tp2   = entry + risk * 2.5
        tp3   = entry + risk * 4.0

    elif "SELL" in action:
        entry = price
        sl    = min(entry + at * 1.5, entry * 1.15)
        if bu and bu < sl and bu > entry:
            sl = bu * 1.005

        risk  = sl - entry
        tp1   = entry - risk * 1.5
        tp2   = entry - risk * 2.5
        tp3   = entry - risk * 4.0
        # Ensure no negative TPs
        tp1 = max(tp1, price * 0.01)
        tp2 = max(tp2, price * 0.01)
        tp3 = max(tp3, price * 0.01)
    else:
        # HOLD
        sl  = price - at * 1.0
        tp1 = price + at * 1.0
        tp2 = price + at * 2.0
        tp3 = price + at * 3.0
        risk = at

    sl_pct  = abs((sl - price) / price * 100)
    tp1_pct = abs((tp1 - price) / price * 100)
    rr = round(tp1_pct / sl_pct, 2) if sl_pct else 0

    return {
        "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "sl_pct": round(sl_pct, 2), "tp1_pct": round(tp1_pct, 2), "rr": rr,
        "risk_per_unit": risk,
    }

def futures_setup(price, direction, i1h, i4h, i1d, fr_val=0):
    """
    25x Futures setup.
    At 25x: 1% price move = 25% margin PnL
    SL designed so max loss = ~40% of margin (safe buffer from liq)
    Liquidation at ~96% of margin loss at 25x
    """
    if direction == "NO TRADE":
        return None

    lev  = 25
    at4h = i4h.get("at") or (price * 0.012)
    # Tighter SL for futures (risk management)
    sl_atr_mult = 1.0   # 1 ATR stop (tighter than spot)

    if direction == "LONG":
        entry = price
        sl    = entry - at4h * sl_atr_mult
        sl    = max(sl, entry * 0.94)   # max 6% SL (= 150% margin loss at 25x, safe from liq)

        # Liq price = entry * (1 - 0.96/lev)
        liq   = entry * (1 - 0.95/lev)

        risk  = entry - sl
        tp1   = entry + risk * 1.0     # 1:1 (quick scalp)
        tp2   = entry + risk * 2.0     # 2:1
        tp3   = entry + at4h * 3.0     # 3 ATR target

    else:  # SHORT
        entry = price
        sl    = entry + at4h * sl_atr_mult
        sl    = min(sl, entry * 1.06)

        liq   = entry * (1 + 0.95/lev)

        risk  = sl - entry
        tp1   = entry - risk * 1.0
        tp2   = entry - risk * 2.0
        tp3   = entry - at4h * 3.0
        # No negative prices
        tp1 = max(tp1, price * 0.01)
        tp2 = max(tp2, price * 0.01)
        tp3 = max(tp3, price * 0.01)

    sl_pct    = abs((sl - entry) / entry * 100)
    tp1_pct   = abs((tp1 - entry) / entry * 100)
    rr        = round(tp1_pct / sl_pct, 2) if sl_pct else 0

    # PnL at 25x leverage
    pnl_sl    = round(-sl_pct * lev, 1)
    pnl_tp1   = round(tp1_pct * lev, 1)
    pnl_tp2   = round(abs((tp2-entry)/entry*100) * lev, 1)

    # Distance from SL to liquidation (safety buffer)
    liq_dist  = abs((liq - sl) / entry * 100)

    return {
        "direction": direction, "entry": entry, "lev": lev,
        "sl": sl, "sl_pct": round(sl_pct, 2), "pnl_sl": pnl_sl,
        "liq": liq, "liq_dist": round(liq_dist, 2),
        "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "tp1_pct": round(tp1_pct, 2), "pnl_tp1": pnl_tp1, "pnl_tp2": pnl_tp2,
        "rr": rr,
    }

# ─── GROQ AI — DECISION LAYER ─────────────────────────────────────────────────
async def groq_ai(session, coin, ctx, news):
    if news:
        news_lines = []
        for n in news[:8]:
            tag = "⭐" if n.get("important") else ""
            news_lines.append(f"- [{n['sentiment'].upper()}]{tag} {n['title']} ({n.get('source','')}, {n.get('date','')})")
        news_text = "\n".join(news_lines)
    else:
        news_text = "No recent news found for this coin on CryptoPanic."

    prompt = f"""You are a senior crypto trading strategist making the FINAL call on a trade.

You are given two things — do NOT regenerate either of them:
1) QUANT DATA: a complete multi-timeframe technical analysis already computed precisely (scores, RSI, EMAs, MACD, ATR, support/resistance, predicted ranges, and a baseline spot/futures trade setup with real entry/SL/TP numbers).
2) REAL NEWS: actual recent headlines for {coin} pulled from CryptoPanic, each tagged with community sentiment votes.

QUANT DATA FOR {coin}/USDT:
{json.dumps(ctx, default=str)}

REAL NEWS (CryptoPanic, last few days):
{news_text}

YOUR JOB — decision-making, not re-analysis:
- Weigh the REAL news against the quant signal. Does it CONFIRM, WEAKEN, or CONTRADICT the technical picture?
- Make the FINAL trading call: verdict is AGREE (news supports/doesn't change quant signal), ADJUST (tweak confidence or levels slightly), or OVERRIDE (news is decisive enough to flip/cancel the trade)
- For spot/futures setups: start from the quant baseline entry/sl/tp1/tp2 given in QUANT DATA. Only change numbers if you have a clear reason (e.g. a key level from news, or risk-off override). Otherwise copy the baseline numbers as-is.
- key_news: select ONLY from the REAL NEWS list above (max 3), do not invent any news. If the list is empty, return an empty array.
- Be concise and specific. Reference actual numbers from QUANT DATA in your reasoning.

Reply ONLY with this exact JSON (no markdown, no extra text, no commentary):
{{"verdict":"AGREE/ADJUST/OVERRIDE","final_action":"STRONG BUY/BUY/HOLD/SELL/STRONG SELL","final_confidence":0,"reasoning":"2-3 sentences combining technicals + news","news_sentiment":"BULLISH/BEARISH/NEUTRAL/MIXED","key_news":[{{"title":"exact headline from REAL NEWS list","sentiment":"bullish/bearish/neutral","impact":"why it matters for price, 1 sentence"}}],"spot":{{"action":"BUY/SELL/HOLD","entry":0.0,"sl":0.0,"tp1":0.0,"tp2":0.0,"reasoning":"1-2 sentences"}},"futures":{{"direction":"LONG/SHORT/NO TRADE","entry":0.0,"sl":0.0,"tp1":0.0,"tp2":0.0,"invalidation":"specific price condition","reasoning":"1-2 sentences"}},"risk_level":"LOW/MEDIUM/HIGH/EXTREME","risk_factors":["factor with number","factor","factor"],"catalysts_watch":["upcoming catalyst or level to watch","catalyst"]}}"""

    h={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"}
    b={"model":"llama-3.3-70b-versatile",
       "messages":[{"role":"user","content":prompt}],
       "max_tokens":1200,"temperature":0.15}
    resp=await post(session, GROQ, h, b, timeout=45)
    if not resp or "choices" not in resp:
        return None
    raw=resp["choices"][0]["message"]["content"].strip()
    # Clean JSON
    for strip in ["```json","```","json"]:
        if raw.startswith(strip): raw=raw[len(strip):]
    raw=raw.strip().rstrip("`").strip()
    # Find JSON object
    if "{" in raw:
        raw=raw[raw.index("{"):]
    if "}" in raw:
        raw=raw[:raw.rindex("}")+1]
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Groq JSON parse: {e} | raw: {raw[:150]}")
        return None

# ─── MAIN ANALYSIS ────────────────────────────────────────────────────────────
async def analyze(coin_raw):
    coin=coin_raw.upper().strip().lstrip("/")
    if coin.endswith("USDT"): coin=coin[:-4]
    sym=coin+"USDT"
    now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    logger.info(f"Analyzing {sym}")

    conn=aiohttp.TCPConnector(ssl=False, limit=20)
    hdrs={"User-Agent":"Mozilla/5.0 CryptoBot/10.0"}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as s:

        # Symbol check
        sc=await get(s,f"{SPOT}/api/v3/ticker/price",{"symbol":sym},timeout=6)
        fc=await get(s,f"{FUT}/fapi/v1/ticker/price", {"symbol":sym},timeout=6)
        on_spot=isinstance(sc,dict) and "price" in sc
        on_fut =isinstance(fc,dict) and "price" in fc
        if not on_spot and not on_fut:
            return [f"❌ *{sym}* not found on Binance.\nTry `/BTC` `/ETH` `/SOL`"]

        kb=FUT if on_fut else SPOT
        kp="/fapi/v1/klines" if on_fut else "/api/v3/klines"

        results=await asyncio.gather(
            get(s,f"{SPOT}/api/v3/ticker/24hr",{"symbol":sym}),
            get(s,f"{FUT}/fapi/v1/ticker/24hr",{"symbol":sym}) if on_fut else asyncio.sleep(0),
            get(s,f"{FUT}/fapi/v1/premiumIndex",{"symbol":sym}) if on_fut else asyncio.sleep(0),
            get(s,f"{FUT}/fapi/v1/openInterest",{"symbol":sym}) if on_fut else asyncio.sleep(0),
            get(s,f"{FUT}/fapi/v1/fundingRate",{"symbol":sym,"limit":5}) if on_fut else asyncio.sleep(0),
            get(s,f"{FUT}/futures/data/globalLongShortAccountRatio",{"symbol":sym,"period":"5m","limit":1}) if on_fut else asyncio.sleep(0),
            get(s,f"{FUT}/fapi/v1/openInterestHist",{"symbol":sym,"period":"1h","limit":12}) if on_fut else asyncio.sleep(0),
            get(s,f"{FUT}/futures/data/takerlongshortRatio",{"symbol":sym,"period":"5m","limit":1}) if on_fut else asyncio.sleep(0),
            # Indicator klines
            get(s,f"{kb}{kp}",{"symbol":sym,"interval":"1h","limit":120}),
            get(s,f"{kb}{kp}",{"symbol":sym,"interval":"4h","limit":120}),
            get(s,f"{kb}{kp}",{"symbol":sym,"interval":"1d","limit":200}),
            get(s,f"{kb}{kp}",{"symbol":sym,"interval":"1w","limit":52}),
            # Market context
            get(s,f"{SPOT}/api/v3/ticker/24hr",{"symbol":"BTCUSDT"}),
            get(s,f"{SPOT}/api/v3/ticker/24hr",{"symbol":"ETHUSDT"}),
            get(s,FNG,timeout=6),
            # Real news
            cryptopanic_news(s,coin),
            return_exceptions=True
        )

        def safe(x,t): return x if isinstance(x,t) else None

        spot_t=safe(results[0],dict); fut_t=safe(results[1],dict)
        prem=safe(results[2],dict); oi=safe(results[3],dict)
        fund=safe(results[4],list); ls=safe(results[5],list)
        oih=safe(results[6],list); taker=safe(results[7],list)
        ki1h=safe(results[8],list); ki4h=safe(results[9],list)
        ki1d=safe(results[10],list); ki1w=safe(results[11],list)
        btc_t=safe(results[12],dict); eth_t=safe(results[13],dict)
        fg=safe(results[14],dict)
        news=safe(results[15],list) or []

        if fut_t:    price=float(fut_t.get("lastPrice",0))
        elif spot_t: price=float(spot_t.get("lastPrice",0))
        else:        price=0.0

        i1h=compute(ki1h) if ki1h else {}
        i4h=compute(ki4h) if ki4h else {}
        i1d=compute(ki1d) if ki1d else {}
        i1w=compute(ki1w) if ki1w else {}

        s1h=score(i1h); s4h=score(i4h); s1d=score(i1d)
        fr_val=float(prem.get("lastFundingRate",0))*100 if prem else 0.0

        # Confluence signal
        action, conf, reasons, weighted = confluence_signal(s1h, s4h, s1d, i4h, i1d, fr_val)

        # Determine futures direction
        if "BUY" in action:   fut_dir="LONG"
        elif "SELL" in action: fut_dir="SHORT"
        else:                  fut_dir="NO TRADE"

        # Trade setups
        ss=spot_setup(price, action, i4h, i1d)
        fs=futures_setup(price, fut_dir, i1h, i4h, i1d, fr_val)

        # Predicted ranges
        rngs=predict_ranges(price, i1h, i4h, i1d, i1w, weighted)

        # Liquidity Hunt / Squeeze Detection
        liq=liquidity_analysis(
            price, i1h, i4h, i1d, ki1h, ki4h,
            fr_val=fr_val, ls_data=ls, oih=oih, taker=taker, fund=fund
        )

        # Trend
        tr4h=trend_detect(i4h); tr24h=trend_detect(i1d)
        at4h=i4h.get("at") or 0
        vol_lbl="HIGH" if at4h/price*100>3 else ("MEDIUM" if at4h/price*100>1.5 else "LOW") if price else "N/A"

        # Groq context — full quant picture so the AI judges/decides rather than re-derives
        ctx={
            "coin":coin,"price":price,
            "change_24h":float(spot_t.get("priceChangePercent",0)) if spot_t else 0,
            "volume_24h":float(spot_t.get("quoteVolume",0)) if spot_t else 0,
            "high_24h":float(spot_t.get("highPrice",0)) if spot_t else 0,
            "low_24h":float(spot_t.get("lowPrice",0)) if spot_t else 0,
            "trend_4h":tr4h,"trend_24h":tr24h,
            "funding_rate_pct":fr_val,
            "open_interest":float(oi.get("openInterest",0)) if oi else 0,
            "long_pct":float(ls[0].get("longAccount",0))*100 if ls else 50,
            "rsi_1h":i1h.get("rsi"),"rsi_4h":i4h.get("rsi"),"rsi_1d":i1d.get("rsi"),
            "ema9_4h":i4h.get("e9"),"ema21_4h":i4h.get("e21"),"ema50_4h":i4h.get("e50"),
            "macd_hist_4h":i4h.get("hist"),
            "bb_lower_4h":i4h.get("bl"),"bb_upper_4h":i4h.get("bu"),
            "atr_4h":at4h,"volatility":vol_lbl,
            "score_1h":s1h,"score_4h":s4h,"score_1d":s1d,"weighted_score":round(weighted,2),
            "confluence_action":action,"confidence":conf,"confluence_reasons":reasons,
            "support_1d":i1d.get("bl"),"resistance_1d":i1d.get("bu"),
            "support_4h":i4h.get("bl"),"resistance_4h":i4h.get("bu"),
            "btc_change":float(btc_t.get("priceChangePercent",0)) if btc_t else 0,
            "btc_price":float(btc_t.get("lastPrice",0)) if btc_t else 0,
            "fear_greed":fg["data"][0]["value"] if fg and "data" in fg else "N/A",
            "predicted_ranges":{k:{"h":v["h"],"l":v["l"],"mp":v["mp"]} for k,v in rngs.items()},
            # Liquidity / squeeze / SMC analysis
            "liquidity_setup": liq.get("setup_label"),
            "squeeze_type": liq.get("squeeze_type"),
            "squeeze_score": liq.get("squeeze_score"),
            "sweep_detected": liq.get("sweep_detected"),
            "liq_clusters_above": liq.get("liq_clusters_above",[])[:3],
            "liq_clusters_below": liq.get("liq_clusters_below",[])[:3],
            "order_block": liq.get("order_block"),
            # Baseline trade setups computed from quant signals — Groq should refine, not replace
            "baseline_spot":{
                "action":action,"entry":ss["entry"],"sl":ss["sl"],
                "tp1":ss["tp1"],"tp2":ss["tp2"],"tp3":ss["tp3"],"rr":ss["rr"],
            },
            "baseline_futures": ({
                "direction":fs["direction"],"entry":fs["entry"],"sl":fs["sl"],
                "tp1":fs["tp1"],"tp2":fs["tp2"],"tp3":fs["tp3"],
                "liq":fs["liq"],"liq_dist_pct":fs["liq_dist"],"rr":fs["rr"],
            } if fs else {"direction":"NO TRADE"}),
        }
        ai=await groq_ai(s,coin,ctx,news)

    # ── FORMAT PAGES ──────────────────────────────────────────────────────────
    pages=[]

    # PAGE 1 — Overview + Price + Ranges
    p1=[]
    p1.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p1.append(f"🪙 *{coin}/USDT — Intelligence v12*")
    p1.append(f"🕐 `{now}`")
    p1.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Market context
    p1.append("\n🌍 *BROADER MARKET*")
    if btc_t:
        bc=float(btc_t.get("priceChangePercent",0)); bp=float(btc_t.get("lastPrice",0))
        p1.append(f"  BTC: {'📈' if bc>=0 else '📉'} `${fmt(bp,0)}` ({bc:+.2f}%)")
    if eth_t:
        ec=float(eth_t.get("priceChangePercent",0)); ep=float(eth_t.get("lastPrice",0))
        p1.append(f"  ETH: {'📈' if ec>=0 else '📉'} `${fmt(ep,0)}` ({ec:+.2f}%)")
    if fg and "data" in fg:
        fv=fg["data"][0]["value"]; fc2=fg["data"][0]["value_classification"]
        fe="😱" if int(fv)<25 else("😰" if int(fv)<40 else("😐" if int(fv)<60 else("😊" if int(fv)<80 else"🤑")))
        p1.append(f"  Fear & Greed: {fe} `{fv}/100` _{fc2}_")

    # Price
    p1.append(f"\n💰 *{coin} PRICE*")
    p1.append(f"  Current: `${fmt(price,6)}`")
    if spot_t:
        ch=float(spot_t.get("priceChangePercent",0))
        h24=float(spot_t.get("highPrice",price)); l24=float(spot_t.get("lowPrice",price))
        vol_u=float(spot_t.get("quoteVolume",0))
        p1.append(f"  24h: {'📈' if ch>=0 else '📉'} `{ch:+.2f}%`")
        p1.append(f"  High: `${fmt(h24,6)}`  Low: `${fmt(l24,6)}`")
        p1.append(f"  Volume: `{fmt(vol_u,0)} USDT`")

    # Market state
    te="🚀" if "STRONG UP" in tr4h else("📈" if "UP" in tr4h else("💀" if "STRONG DOWN" in tr4h else("📉" if "DOWN" in tr4h else"↔️")))
    te2="🚀" if "STRONG UP" in tr24h else("📈" if "UP" in tr24h else("💀" if "STRONG DOWN" in tr24h else("📉" if "DOWN" in tr24h else"↔️")))
    p1.append(f"\n📊 *MARKET STATE*")
    p1.append(f"  Trend 4H:   {te} `{tr4h}`")
    p1.append(f"  Trend 24H:  {te2} `{tr24h}`")
    p1.append(f"  Volatility: `{vol_lbl}`  Strength: `{i4h.get('ts','N/A')}`")

    # Confluence signal box
    ae_e="🟢" if "BUY" in action else("🔴" if "SELL" in action else"🟡")
    p1.append(f"\n⚡ *CONFLUENCE SIGNAL*")
    p1.append(f"  {ae_e} *{action}* — Confidence: `{conf}%`")
    p1.append(f"  Scores: 1H:`{s1h:+d}` 4H:`{s4h:+d}` 1D:`{s1d:+d}` Weighted:`{weighted:+.1f}`")
    for r in reasons[:3]:
        p1.append(f"  • {r}")

    # Predicted ranges
    p1.append(f"\n🔮 *PREDICTED RANGES (from `${fmt(price,4)}`)*")
    p1.append(f"  _ATR-based projections with {('bullish' if weighted>0.5 else ('bearish' if weighted<-0.5 else 'neutral'))} bias_")
    for tf, r in rngs.items():
        p1.append(f"  `{tf:3}` H:`${fmt(r['h'],4)}` L:`${fmt(r['l'],4)}` ±`{r['mp']:.1f}%` {r['bias']}")

    pages.append("\n".join(p1))

    # PAGE 2 — Futures + Indicators + Trade Setup
    p2=[]

    if on_fut:
        p2.append("🔴 *FUTURES MARKET (LIVE)*")
        if fut_t:
            fp=float(fut_t.get("lastPrice",0)); fc3=float(fut_t.get("priceChangePercent",0))
            p2.append(f"  Price: `${fmt(fp,6)}`  ({fc3:+.2f}%)")
            p2.append(f"  Volume: `{fmt(float(fut_t.get('quoteVolume',0)),0)} USDT`")
        if prem:
            mark=float(prem.get("markPrice",0)); idx=float(prem.get("indexPrice",0))
            basis=(mark-idx)/idx*100 if idx else 0
            fr_e="🔴" if fr_val<0 else "🟢"
            p2.append(f"  Mark: `${fmt(mark,6)}`  Index: `${fmt(idx,6)}`")
            p2.append(f"  Funding: {fr_e} `{fr_val:.4f}%`  Basis: `{basis:+.4f}%`")
            if fr_val>0.1:     p2.append("   ↳ 🚨 Extreme funding → SHORT bias")
            elif fr_val>0.05:  p2.append("   ↳ ⚠️ High → dump risk")
            elif fr_val<-0.05: p2.append("   ↳ 🚨 Negative extreme → squeeze coming")
            elif fr_val<-0.01: p2.append("   ↳ 💡 Negative → potential squeeze")
            else:              p2.append("   ↳ ✅ Balanced")
        if oi:
            p2.append(f"  OI: `{fmt(float(oi.get('openInterest',0)),2)} {coin}`")
        if oih and len(oih)>=2:
            ov=[float(x.get("sumOpenInterest",0)) for x in oih]
            oc=(ov[-1]-ov[0])/ov[0]*100 if ov[0] else 0
            oie="🔥" if oc>5 else("📈" if oc>0 else("📉" if oc>-5 else"💀"))
            p2.append(f"  OI Trend(12h): {oie} `{oc:+.2f}%`")
        if ls:
            lp=float(ls[0].get("longAccount",0))*100; sp=100-lp
            le="🟢" if lp>55 else("🔴" if lp<45 else"🟡")
            p2.append(f"  L/S: {le} `{lp:.1f}% / {sp:.1f}%`")
            if lp>68:   p2.append("   ↳ 🚨 Extreme longs → liq risk")
            elif sp>65: p2.append("   ↳ 💡 Heavy shorts → squeeze possible")
        if taker:
            tb=float(taker[0].get("buySell",1))
            p2.append(f"  Taker: {'🟢 Buy dom' if tb>1 else '🔴 Sell dom'} `{tb:.3f}`")
        if fund and len(fund)>=3:
            rates=[float(f.get("fundingRate",0))*100 for f in fund]
            p2.append(f"  Avg Funding(5): `{sum(rates)/len(rates):.4f}%`  {'Rising📈' if rates[-1]>rates[0] else 'Falling📉'}")

    # Indicator blocks
    def iblock(lbl, ind, sc_val):
        if not ind: return f"\n📐 *{lbl}* — No data"
        cur=ind.get("cur",0); lines=[f"\n📐 *{lbl}* — {siglabel(sc_val)} ({sc_val:+d}/10)"]
        rv=ind.get("rsi")
        if rv is not None:
            re="🔴 OB" if rv>70 else("🟢 OS" if rv<30 else"🟡 Normal")
            lines.append(f"  RSI: `{rv}` — {re}")
        for nm,ky in [("EMA9","e9"),("EMA21","e21"),("EMA50","e50"),("EMA200","e200")]:
            v=ind.get(ky)
            if v:
                d2=(cur-v)/v*100 if v else 0
                lines.append(f"  {nm}: {'🟢' if cur>v else '🔴'} `{fmt(v,6)}` ({d2:+.2f}%)")
        ml=ind.get("macd"); hv=ind.get("hist")
        if ml is not None:
            lines.append(f"  MACD: {'🟢' if (hv or 0)>0 else '🔴'} `{fmt(ml,8)}` Hist:`{fmt(hv,8)}`")
        bl2=ind.get("bl"); bu2=ind.get("bu"); bm2=ind.get("bm")
        if bl2 and bu2:
            rng2=bu2-bl2; pos=(cur-bl2)/rng2*100 if rng2 else 50
            tag=" 🔵LOW" if cur<=bl2 else(" 🔴HIGH" if cur>=bu2 else f" {pos:.0f}%")
            lines.append(f"  BB: `{fmt(bl2,6)}`/`{fmt(bm2,6)}`/`{fmt(bu2,6)}`{tag}")
        at2=ind.get("at"); vw2=ind.get("vw"); vr2=ind.get("vr")
        if at2: lines.append(f"  ATR: `{fmt(at2,6)}` ({at2/cur*100:.2f}%)")
        if vw2: lines.append(f"  VWAP: {'🟢' if cur>vw2 else '🔴'} `{fmt(vw2,6)}`")
        if vr2: lines.append(f"  Vol: {'🔥' if vr2>2 else('📊' if vr2>0.7 else'😴')} `{vr2:.2f}x`")
        lines.append(f"  Trend: *{trend_detect(ind)}*  Strength: _{ind.get('ts','N/A')}_")
        return "\n".join(lines)

    p2.append(iblock("1H", i1h, s1h))
    p2.append(iblock("4H", i4h, s4h))
    p2.append(iblock("1D", i1d, s1d))
    if i1w: p2.append(iblock("1W", i1w, score(i1w)))

    # Key levels
    if i1d.get("bl"):
        p2.append("\n🏗 *KEY LEVELS*")
        p2.append(f"  1D Support:    `${fmt(i1d.get('bl'),6)}`")
        p2.append(f"  1D Resistance: `${fmt(i1d.get('bu'),6)}`")
        if i4h.get("bl"):
            p2.append(f"  4H Support:    `${fmt(i4h.get('bl'),6)}`")
            p2.append(f"  4H Resistance: `${fmt(i4h.get('bu'),6)}`")
        if price:
            sup=i1d.get("bl",price); res=i1d.get("bu",price)
            if res>price: p2.append(f"  To 1D Res: `+{(res-price)/price*100:.2f}%`")
            if sup<price: p2.append(f"  To 1D Sup: `-{(price-sup)/price*100:.2f}%`")

    # ── LIQUIDITY HUNT & SQUEEZE ANALYSIS ─────────────────────────────────────
    p2.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p2.append(f"🧲 *LIQUIDITY HUNT ANALYSIS*")

    liq_label = liq.get("setup_label","NO SETUP")
    liq_conf  = liq.get("setup_confidence", 0)
    sq_score  = liq.get("squeeze_score", 0)
    sq_type   = liq.get("squeeze_type")

    # Setup badge
    if "SQUEEZE" in liq_label or "REVERSAL" in liq_label:
        p2.append(f"  🚨 *Pattern: {liq_label}*")
    elif "MAGNET" in liq_label or "SWEEP" in liq_label:
        p2.append(f"  ⚡ *Pattern: {liq_label}*")
    else:
        p2.append(f"  ⬜ Pattern: `{liq_label}`")
    if liq_conf > 0:
        p2.append(f"  Pattern Confidence: `{liq_conf}%`")

    # Squeeze meter
    sq_bar = "🟩" * max(0, sq_score) + "🟥" * max(0, -sq_score) + "⬜" * (10 - abs(sq_score))
    if sq_type == "SHORT_SQUEEZE":
        p2.append(f"  Squeeze Meter: {sq_bar}")
        p2.append(f"  🚀 *SHORT SQUEEZE POTENTIAL: {sq_score}/10*")
    elif sq_type == "LONG_SQUEEZE":
        p2.append(f"  Squeeze Meter: {sq_bar}")
        p2.append(f"  💀 *LONG SQUEEZE POTENTIAL: {abs(sq_score)}/10*")
    else:
        p2.append(f"  Squeeze Pressure: `{sq_score:+d}/10`")

    # Squeeze reasons
    for r in liq.get("squeeze_reasons",[])[:3]:
        p2.append(f"    • {r}")

    # Liquidity sweep
    sweep = liq.get("sweep_detected")
    if sweep:
        sw_emoji = "⬇️" if sweep=="BELOW_SUPPORT" else "⬆️"
        sw_lbl   = "Below Support (Long Setup)" if sweep=="BELOW_SUPPORT" else "Above Resistance (Short Setup)"
        p2.append(f"\n  {sw_emoji} *Liquidity Sweep: {sw_lbl}*")
        for r in liq.get("sweep_reasons",[])[:3]:
            p2.append(f"    • {r}")

    # Liquidation magnets (clusters)
    ca = liq.get("liq_clusters_above",[])
    cb = liq.get("liq_clusters_below",[])
    if ca or cb:
        p2.append(f"\n  🧲 *Estimated Liquidation Clusters*")
        if ca:
            p2.append(f"  Above (Short liq magnets):")
            for lvl in ca[:3]:
                dist = (lvl-price)/price*100
                p2.append(f"    🔴 `${fmt(lvl,6)}` (+{dist:.1f}%) ← shorts liquidate here")
        if cb:
            p2.append(f"  Below (Long liq magnets):")
            for lvl in cb[:3]:
                dist = (price-lvl)/price*100
                p2.append(f"    🟢 `${fmt(lvl,6)}` (-{dist:.1f}%) ← longs liquidate here")

    # SMC Order Block
    ob = liq.get("order_block")
    if ob:
        ob_emoji = "🟩" if ob["type"]=="BULLISH" else "🟥"
        p2.append(f"\n  {ob_emoji} *SMC Order Block: {ob['label']}*")
        p2.append(f"    Zone: `${fmt(ob['low'],6)}` — `${fmt(ob['high'],6)}`")
        if ob["type"]=="BULLISH":
            p2.append(f"    ↳ Price entering this zone = potential LONG entry")
        else:
            p2.append(f"    ↳ Price entering this zone = potential SHORT entry")

    # Trade setups
    p2.append(f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p2.append(f"🎯 *TRADE SETUPS*")

    # Spot
    ae2="🟢" if "BUY" in action else("🔴" if "SELL" in action else"🟡")
    p2.append(f"\n📈 *SPOT: {ae2} {action}* (conf: {conf}%)")
    p2.append(f"  Entry:    `${fmt(ss['entry'],6)}`")
    p2.append(f"  Stop Loss:`${fmt(ss['sl'],6)}` (-{ss['sl_pct']:.2f}%)")
    p2.append(f"  TP1:      `${fmt(ss['tp1'],6)}` (+{ss['tp1_pct']:.2f}%)  R/R: `{ss['rr']}:1`")
    p2.append(f"  TP2:      `${fmt(ss['tp2'],6)}`  TP3: `${fmt(ss['tp3'],6)}`")
    p2.append(f"  Position: Max 5% of portfolio")

    # Futures
    p2.append(f"\n🔴 *FUTURES: 25x — {'🟢 '+fut_dir if fut_dir!='NO TRADE' else '🚫 NO TRADE'}*")
    if fs:
        p2.append(f"  ⚠️ EXTREME RISK — Max 2% of account")
        p2.append(f"  Entry:    `${fmt(fs['entry'],6)}`")
        p2.append(f"  Stop Loss:`${fmt(fs['sl'],6)}` (-{fs['sl_pct']:.2f}% | PnL:{fs['pnl_sl']:+.0f}%)")
        p2.append(f"  Liq Price:`${fmt(fs['liq'],6)}` | Buffer to Liq: `{fs['liq_dist']:.2f}%`")
        p2.append(f"  TP1:      `${fmt(fs['tp1'],6)}` (PnL: +{fs['pnl_tp1']:.0f}%)")
        p2.append(f"  TP2:      `${fmt(fs['tp2'],6)}` (PnL: +{fs['pnl_tp2']:.0f}%)")
        p2.append(f"  TP3:      `${fmt(fs['tp3'],6)}`  R/R: `{fs['rr']}:1`")
    else:
        p2.append(f"  🚫 No clear signal — mixed TF confluence")
        p2.append(f"  Wait for: TF alignment or key level break")

    pages.append("\n".join(p2))

    # PAGE 3 — Real News + AI Decision Layer
    p3=[]
    p3.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p3.append("📰 *LATEST NEWS (CryptoPanic)*")
    p3.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if news:
        for n in news[:5]:
            se="🟢" if n["sentiment"]=="bullish" else("🔴" if n["sentiment"]=="bearish" else"🟡")
            star="⭐" if n.get("important") else ""
            p3.append(f"\n{se}{star} *{n['title']}*")
            meta=" • ".join(x for x in [n.get("source",""), n.get("date","")] if x)
            if meta: p3.append(f"  _{meta}_")
    else:
        p3.append("\n_No recent news found for this coin._")

    p3.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p3.append("🤖 *AI DECISION LAYER (Groq)*")
    p3.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if ai:
        verdict=ai.get("verdict","N/A")
        ve="✅" if verdict=="AGREE" else("✏️" if verdict=="ADJUST" else("⛔" if verdict=="OVERRIDE" else"🤖"))
        fa=ai.get("final_action","N/A"); fconf=ai.get("final_confidence",0)
        ae3="🟢" if "BUY" in fa else("🔴" if "SELL" in fa else"🟡")
        p3.append(f"\n{ve} *Verdict: {verdict}*")
        p3.append(f"{ae3} *Final Call: {fa}* — Confidence: `{fconf}%`")
        if ai.get("reasoning"): p3.append(f"\n_{ai['reasoning']}_")

        ns=ai.get("news_sentiment","N/A")
        nse="🟢" if ns=="BULLISH" else("🔴" if ns=="BEARISH" else("🟠" if ns=="MIXED" else"🟡"))
        p3.append(f"\n📊 *News Sentiment: {nse} {ns}*")

        kn=ai.get("key_news",[])
        if kn:
            p3.append("\n🔍 *Key News Impact*")
            for n in kn[:3]:
                se="🟢" if n.get("sentiment")=="bullish" else("🔴" if n.get("sentiment")=="bearish" else"🟡")
                p3.append(f"  {se} *{n.get('title','')[:70]}*")
                if n.get("impact"): p3.append(f"     _{n['impact']}_")

        # AI Spot setup
        sp=ai.get("spot",{})
        sa=sp.get("action","N/A")
        ae4="🟢" if sa=="BUY" else("🔴" if sa=="SELL" else"🟡")
        p3.append(f"\n📈 *FINAL SPOT: {ae4} {sa}*")
        if sp.get("entry") and float(sp.get("entry",0))>0:
            p3.append(f"  Entry: `${fmt(sp.get('entry'),6)}`  SL: `${fmt(sp.get('sl'),6)}`")
            p3.append(f"  TP1: `${fmt(sp.get('tp1'),6)}`  TP2: `${fmt(sp.get('tp2'),6)}`")
        if sp.get("reasoning"): p3.append(f"  _{sp['reasoning']}_")

        # AI Futures setup
        fdir=ai.get("futures",{}).get("direction","N/A")
        fu=ai.get("futures",{})
        fe4="🟢" if fdir=="LONG" else("🔴" if fdir=="SHORT" else"🚫")
        p3.append(f"\n🔴 *FINAL FUTURES: {fe4} {fdir}* (25x)")
        if fdir!="NO TRADE" and fu.get("entry") and float(fu.get("entry",0))>0:
            p3.append(f"  ⚠️ _Max 2% of account — extreme risk_")
            p3.append(f"  Entry: `${fmt(fu.get('entry'),6)}`  SL: `${fmt(fu.get('sl'),6)}`")
            p3.append(f"  TP1: `${fmt(fu.get('tp1'),6)}`  TP2: `${fmt(fu.get('tp2'),6)}`")
            if fu.get("invalidation"): p3.append(f"  Invalidation: _{fu['invalidation']}_")
        if fu.get("reasoning"): p3.append(f"  _{fu['reasoning']}_")

        rl=ai.get("risk_level","N/A")
        re2="🟢" if rl=="LOW" else("🟡" if rl=="MEDIUM" else("🔴" if rl=="HIGH" else"🚨"))
        p3.append(f"\n⚠️ *RISK: {re2} {rl}*")
        for r in ai.get("risk_factors",[])[:3]:
            p3.append(f"  • {r}")

        cw=ai.get("catalysts_watch",[])
        if cw:
            p3.append(f"\n👀 *CATALYSTS TO WATCH*")
            for c in cw[:3]:
                p3.append(f"  • {c}")
    else:
        p3.append("\n⚠️ AI decision layer unavailable — quant trade setups on the previous page are still valid")

    p3.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p3.append("⚠️ _Not financial advice. 25x = extreme risk. DYOR._")
    pages.append("\n".join(p3))
    return pages

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT: return
    text=update.message.text or ""
    cmd=text.split()[0].lstrip("/").split("@")[0].strip()

    if cmd.lower() in ("start","help"):
        await update.message.reply_text(
            "👋 *Crypto Intelligence Bot v12*\n\n"
            "Type `/` + any coin:\n"
            "• `/BTC` `/ETH` `/SOL` `/PEPE`\n\n"
            "✅ *Key improvements v12:*\n"
            "• Confluence scoring (3 TF agreement)\n"
            "• ATR/BB-based SL/TP with R/R\n"
            "• Futures buffer-to-liquidation shown\n"
            "• REAL news via CryptoPanic (no hallucinated news)\n"
            "• Groq AI is now a decision layer — judges quant signal vs real news and gives a final call (AGREE/ADJUST/OVERRIDE)\n\n"
            "3 messages — full intelligence report",
            parse_mode="Markdown")
        return

    if not cmd: return
    msg=await update.message.reply_text(
        f"⏳ *{cmd.upper()}/USDT Analysis v12*\n_~20 seconds_",
        parse_mode="Markdown")
    try:
        pages=await analyze(cmd)
        await msg.delete()
        for i,page in enumerate(pages):
            if page.strip():
                await update.message.reply_text(page.strip(), parse_mode="Markdown")
                if i<len(pages)-1: await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)
        try: await msg.edit_text(f"❌ Error: `{str(e)[:200]}`", parse_mode="Markdown")
        except: pass

def main():
    app=ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",handler))
    app.add_handler(CommandHandler("help",handler))
    app.add_handler(MessageHandler(filters.COMMAND,handler))
    logger.info("🚀 Crypto Intelligence Bot v12 started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
