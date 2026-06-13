"""
Crypto Trading Intelligence Bot v8 - Production Grade
- Real-time WebSocket + REST Binance data
- Predicted price ranges (not historical) from current price
- Multi-timeframe trend detection
- Spot + Futures signals (25x)
- Groq AI news & sentiment
"""
import logging
import asyncio
import aiohttp
import json
import math
from datetime import datetime
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

TELEGRAM_TOKEN = "8650706334:AAHJQrBxkw-zOw286H1v-PvtDtUWsM9KFfY"
GROQ_API_KEY   = "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"
ALLOWED_CHAT   = 5214099942

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

SPOT_REST = "https://api.binance.com"
FUT_REST  = "https://fapi.binance.com"
SPOT_WS   = "wss://stream.binance.com:9443/ws"
FUT_WS    = "wss://fstream.binance.com/ws"
CG_BASE   = "https://api.coingecko.com/api/v3"
GROQ_URL  = "https://api.groq.com/openai/v1/chat/completions"
FNG_URL   = "https://api.alternative.me/fng/?limit=1"

CG_IDS = {
    "BTC":"bitcoin","ETH":"ethereum","SOL":"solana","BNB":"binancecoin",
    "XRP":"ripple","ADA":"cardano","DOGE":"dogecoin","AVAX":"avalanche-2",
    "DOT":"polkadot","MATIC":"matic-network","LINK":"chainlink","UNI":"uniswap",
    "ATOM":"cosmos","LTC":"litecoin","NEAR":"near","APT":"aptos","ARB":"arbitrum",
    "OP":"optimism","SUI":"sui","PEPE":"pepe","SHIB":"shiba-inu","FLOKI":"floki",
    "BONK":"bonk","WIF":"dogwifcoin","TRX":"tron","TON":"the-open-network",
    "NOT":"notcoin","STG":"stargate-finance","INJ":"injective-protocol",
    "FET":"fetch-ai","SEI":"sei-network","RENDER":"render-token","JUP":"jupiter",
    "PYTH":"pyth-network","JTO":"jito","ONDO":"ondo-finance",
    "HBAR":"hedera-hashgraph","VET":"vechain","ALGO":"algorand",
    "ICP":"internet-computer","SAND":"the-sandbox","MANA":"decentraland",
    "AXS":"axie-infinity","GMX":"gmx","AAVE":"aave","CRV":"curve-dao-token",
    "LDO":"lido-dao","RUNE":"thorchain","TIA":"celestia","BLUR":"blur",
    "IMX":"immutable-x","ZK":"zksync","STRK":"starknet","FHE":"fhenix",
}

# ─── HTTP ─────────────────────────────────────────────────────────────────────
async def fetch(session, url, params=None, headers=None,
                json_body=None, method="GET", timeout=12):
    try:
        if method == "POST":
            async with session.post(
                url, headers=headers, json=json_body,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as r:
                if r.status == 200: return await r.json()
        else:
            async with session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as r:
                if r.status == 200: return await r.json()
    except Exception as e:
        logger.debug(f"fetch {url[:45]}: {e}")
    return None

# ─── WEBSOCKET REAL-TIME PRICE ────────────────────────────────────────────────
async def ws_price(symbol: str, futures: bool = False) -> float | None:
    url = f"{FUT_WS}/{symbol.lower()}@ticker" if futures \
          else f"{SPOT_WS}/{symbol.lower()}@ticker"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, timeout=aiohttp.ClientTimeout(total=5)) as ws:
                msg = await asyncio.wait_for(ws.receive(), timeout=4.0)
                if msg.type == aiohttp.WSMsgType.TEXT:
                    d = json.loads(msg.data)
                    p = float(d.get("c") or d.get("p") or 0)
                    if p > 0: return p
    except Exception as e:
        logger.debug(f"ws {symbol}: {e}")
    return None

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
    return round(100-(100/(1+ag/al)), 2) if al else 100.0

def macd(closes):
    if not closes or len(closes) < 35: return None, None, None
    e12=ema(closes,12); e26=ema(closes,26)
    if not e12 or not e26: return None, None, None
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

def atr_v(klines, p=14):
    if not klines or len(klines)<p+1: return None
    trs=[]
    for i in range(1,len(klines)):
        h=float(klines[i][2]); l=float(klines[i][3]); pc=float(klines[i-1][4])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    return sum(trs[-p:])/min(p,len(trs)) if trs else None

def vwap_v(klines):
    if not klines: return None
    tv=tpv=0
    for k in klines:
        tp=(float(k[2])+float(k[3])+float(k[4]))/3; v=float(k[5])
        tpv+=tp*v; tv+=v
    return tpv/tv if tv else None

def stoch_rsi(closes, p=14):
    if len(closes)<p*2: return None,None
    rvals=[]
    for i in range(p, len(closes)):
        r=rsi(closes[:i+1],p)
        if r is not None: rvals.append(r)
    if len(rvals)<p: return None,None
    rec=rvals[-p:]; mn=min(rec); mx=max(rec)
    if mx==mn: return 50.0,50.0
    k=100*(rec[-1]-mn)/(mx-mn)
    d=100*(sum(rec[-3:])/3-mn)/(mx-mn)
    return round(k,2), round(d,2)

def compute(klines):
    if not klines or len(klines)<20: return {}
    closes=[float(k[4]) for k in klines]
    highs=[float(k[2]) for k in klines]
    lows=[float(k[3]) for k in klines]
    vols=[float(k[5]) for k in klines]
    cur=closes[-1]
    e9=ema(closes,9); e21=ema(closes,21); e50=ema(closes,50); e200=ema(closes,200)
    rv=rsi(closes,14); ml,sv,hv=macd(closes); bl,bm,bu=bollinger(closes,20)
    at=atr_v(klines,14); vw=vwap_v(klines[-24:] if len(klines)>=24 else klines)
    sk,sd=stoch_rsi(closes,14)
    av=sum(vols[-20:])/20 if vols else 0
    vr=vols[-1]/av if av>0 else 1
    hh=highs[-1]>max(highs[-6:-1]) if len(highs)>5 else False
    ll=lows[-1]<min(lows[-6:-1]) if len(lows)>5 else False
    hl=lows[-1]>min(lows[-6:-1]) if len(lows)>5 else False
    lh=highs[-1]<max(highs[-6:-1]) if len(highs)>5 else False
    # ADX-proxy for trend strength
    if len(klines)>=28:
        up=[max(highs[i]-highs[i-1],0) for i in range(1,len(highs))]
        dn=[max(lows[i-1]-lows[i],0) for i in range(1,len(lows))]
        a14=at or 1
        pdi=100*(sum(up[-14:])/14)/a14
        mdi=100*(sum(dn[-14:])/14)/a14
        dx=abs(pdi-mdi)/(pdi+mdi+1e-9)*100
        ts="Strong" if dx>25 else ("Moderate" if dx>15 else "Weak")
    else:
        ts="N/A"
    return {
        "cur":cur,"e9":e9,"e21":e21,"e50":e50,"e200":e200,
        "rsi":rv,"macd":ml,"sig":sv,"hist":hv,
        "bl":bl,"bm":bm,"bu":bu,"at":at,"vw":vw,"sk":sk,"sd":sd,
        "vr":vr,"ts":ts,"hh":hh,"ll":ll,"hl":hl,"lh":lh,
    }

def score(ind):
    if not ind: return 0
    s=0; cur=ind.get("cur",0)
    rv=ind.get("rsi")
    if rv is not None:
        if rv<25: s+=3
        elif rv<35: s+=2
        elif rv>75: s-=3
        elif rv>65: s-=2
        elif rv>55: s+=1
        elif rv<45: s-=1
    e9=ind.get("e9"); e21=ind.get("e21"); e50=ind.get("e50"); e200=ind.get("e200")
    if e9 and e21:
        if cur>e9>e21: s+=2
        elif cur<e9<e21: s-=2
    if e50:  s+=1 if cur>e50  else -1
    if e200: s+=1 if cur>e200 else -1
    hv=ind.get("hist"); ml=ind.get("macd")
    if hv is not None:
        if hv>0 and (ml or 0)>0: s+=2
        elif hv>0: s+=1
        elif hv<0 and (ml or 0)<0: s-=2
        else: s-=1
    bl=ind.get("bl"); bu=ind.get("bu")
    if bl and bu:
        rng=bu-bl; pos=(cur-bl)/rng if rng else 0.5
        if pos<=0.05: s+=3
        elif pos<=0.15: s+=2
        elif pos>=0.95: s-=3
        elif pos>=0.85: s-=2
        elif pos>0.65: s+=1
        elif pos<0.35: s-=1
    vw=ind.get("vw")
    if vw: s+=1 if cur>vw else -1
    sk=ind.get("sk")
    if sk is not None:
        if sk<20: s+=1
        elif sk>80: s-=1
    if ind.get("hh") and ind.get("hl"): s+=1
    if ind.get("ll") and ind.get("lh"): s-=1
    return s

def trend(ind):
    if not ind: return "SIDEWAYS"
    cur=ind.get("cur",0); e9=ind.get("e9"); e21=ind.get("e21"); e50=ind.get("e50")
    rv=ind.get("rsi",50) or 50; hv=ind.get("hist") or 0
    if e9 and e21 and e50:
        if cur>e9>e21>e50 and rv>55 and hv>0: return "STRONG UPTREND"
        elif cur>e9>e21 and rv>50:             return "UPTREND"
        elif cur<e9<e21<e50 and rv<45 and hv<0: return "STRONG DOWNTREND"
        elif cur<e9<e21 and rv<50:             return "DOWNTREND"
    return "SIDEWAYS"

def mkt_condition(ind):
    ts=ind.get("ts","Weak"); rv=ind.get("rsi",50) or 50
    if ts=="Strong" and abs(rv-50)>10: return "TRENDING"
    if ts=="Moderate" and abs(rv-50)>5: return "TRENDING"
    return "RANGING"

def volatility(ind, price):
    at=ind.get("at")
    if not at or not price: return "UNKNOWN"
    pct=at/price*100
    return "HIGH" if pct>3 else ("MEDIUM" if pct>1.5 else "LOW")

def siglabel(s):
    if s>=6:    return "🟢 STRONG BUY"
    elif s>=3:  return "🟩 BUY"
    elif s<=-6: return "🔴 STRONG SELL"
    elif s<=-3: return "🟥 SELL"
    return "🟡 HOLD"

def fmt(n, d=4):
    if n is None: return "N/A"
    try:
        n=float(n)
        if abs(n)>=1e9: return f"{n/1e9:.2f}B"
        if abs(n)>=1e6: return f"{n/1e6:.2f}M"
        if abs(n)>=1e3: return f"{n/1e3:.2f}K"
        return f"{n:.{d}f}"
    except: return str(n)

# ─── PREDICTED PRICE RANGES ───────────────────────────────────────────────────
def predict_ranges(price: float, i1h: dict, i4h: dict, i1d: dict,
                   i1w: dict, spot_score: int) -> dict:
    """
    Predict future price ranges from CURRENT price using:
    - ATR-based volatility projection
    - Trend bias
    - EMA distances
    - Score direction
    """
    at1h = i1h.get("at") or (price * 0.005)
    at4h = i4h.get("at") or (price * 0.010)
    at1d = i1d.get("at") or (price * 0.020)
    at1w = i1w.get("at") if i1w else (price * 0.040)

    bias = spot_score / 10.0   # -1.0 to +1.0 directional bias
    # Cap bias
    bias = max(-0.8, min(0.8, bias))

    def make_range(center, atr_mult, atr_val, up_skew=0):
        volatility_factor = atr_val * atr_mult
        # Bias shifts center up or down
        center_adj = center * (1 + bias * 0.01 * atr_mult)
        h = center_adj + volatility_factor * (1 + up_skew)
        l = center_adj - volatility_factor * (1 - up_skew * 0.5)
        move_pct = (h - l) / price * 100
        return {
            "predicted_high": round(h, 8),
            "predicted_low":  round(l, 8),
            "move_pct":       round(move_pct, 2),
            "bias":           "BULLISH" if bias > 0.1 else ("BEARISH" if bias < -0.1 else "NEUTRAL"),
        }

    return {
        "1H":  make_range(price, 1.0,  at1h),
        "2H":  make_range(price, 1.5,  at1h),
        "4H":  make_range(price, 1.0,  at4h),
        "12H": make_range(price, 2.5,  at4h),
        "1D":  make_range(price, 1.0,  at1d),
        "3D":  make_range(price, 2.5,  at1d),
        "1W":  make_range(price, 1.0,  at1w),
        "1M":  make_range(price, 3.0,  at1w),
    }

# ─── SPOT SIGNAL ──────────────────────────────────────────────────────────────
def spot_signal(price: float, i4h: dict, i1d: dict) -> dict:
    s4=score(i4h); s1d=score(i1d)
    combined=(s4*2+s1d)//3
    at=i4h.get("at") or (price*0.02)
    if combined>=3:
        action="BUY"; sl=round(price-at*1.5,10)
        tp1=round(price+at*1.0,10); tp2=round(price+at*2.0,10); tp3=round(price+at*3.5,10)
    elif combined<=-3:
        action="SELL"; sl=round(price+at*1.5,10)
        tp1=round(price-at*1.0,10); tp2=round(price-at*2.0,10); tp3=round(price-at*3.5,10)
    else:
        action="HOLD"; sl=round(price-at*1.0,10)
        tp1=round(price+at*1.0,10); tp2=round(price+at*2.0,10); tp3=round(price+at*3.0,10)
    sl_pct=abs((sl-price)/price*100); tp1_pct=abs((tp1-price)/price*100)
    rr=round(tp1_pct/sl_pct,2) if sl_pct else 0
    return {
        "action":action,"entry":price,"sl":sl,"sl_pct":round(sl_pct,3),
        "tp1":tp1,"tp2":tp2,"tp3":tp3,"tp1_pct":round(tp1_pct,3),
        "rr":rr,"score":combined,"confidence":min(abs(combined)*14,90),
    }

# ─── FUTURES SIGNAL 25x ───────────────────────────────────────────────────────
def fut_signal(price: float, i1h: dict, i4h: dict, i1d: dict, fr: float=0) -> dict:
    s1h=score(i1h); s4h=score(i4h); s1d=score(i1d)
    combined=(s1h+s4h*2+s1d)//4
    at=i4h.get("at") or (price*0.015)
    lev=25
    if fr>0.05:  combined-=1
    elif fr<-0.03: combined+=1
    if combined>=2:
        direction="LONG"
        sl=round(price-at*1.2,10); liq=round(price*(1-0.96/lev),10)
        tp1=round(price+at*0.8,10); tp2=round(price+at*1.8,10); tp3=round(price+at*3.0,10)
    elif combined<=-2:
        direction="SHORT"
        sl=round(price+at*1.2,10); liq=round(price*(1+0.96/lev),10)
        tp1=round(price-at*0.8,10); tp2=round(price-at*1.8,10); tp3=round(price-at*3.0,10)
    else:
        return {"direction":"NO TRADE","reason":"Insufficient confluence","entry":price,
                "sl":None,"tp1":None,"tp2":None,"tp3":None,"liq":None,
                "leverage":lev,"score":combined,"confidence":0}
    sl_pct=abs((sl-price)/price*100); tp1_pct=abs((tp1-price)/price*100)
    rr=round(tp1_pct/sl_pct,2) if sl_pct else 0
    return {
        "direction":direction,"entry":price,"leverage":lev,
        "sl":sl,"sl_pct":round(sl_pct,3),"pnl_sl":round(-sl_pct*lev,1),
        "liq":liq,"tp1":tp1,"tp2":tp2,"tp3":tp3,
        "tp1_pct":round(tp1_pct,3),"pnl_tp1":round(tp1_pct*lev,1),
        "pnl_tp2":round(abs((tp2-price)/price*100)*lev,1),
        "rr":rr,"score":combined,"confidence":min(abs(combined)*12,85),
    }

# ─── GROQ AI ──────────────────────────────────────────────────────────────────
async def groq_ai(session, coin: str, ctx: dict) -> dict:
    prompt = f"""You are a senior crypto analyst. Analyze {coin}/USDT with this LIVE data:

{json.dumps(ctx, default=str, indent=2)}

Reply ONLY with valid JSON (no markdown fences, no extra text):
{{
  "news": [
    {{"headline":"...", "date":"~Jun 2026", "sentiment":"bullish/bearish/neutral", "impact":"1 line"}},
    {{"headline":"...", "date":"...", "sentiment":"...", "impact":"..."}},
    {{"headline":"...", "date":"...", "sentiment":"...", "impact":"..."}},
    {{"headline":"...", "date":"...", "sentiment":"...", "impact":"..."}}
  ],
  "overall_sentiment": "BULLISH/BEARISH/NEUTRAL",
  "sentiment_score": 7,
  "market_context": "2-3 sentences: BTC dominance, altcoin season, macro",
  "short_term": {{
    "direction": "BULLISH/BEARISH/NEUTRAL",
    "confidence": 72,
    "target_low": 0.0,
    "target_high": 0.0,
    "support": 0.0,
    "resistance": 0.0,
    "reasons": ["r1","r2","r3"]
  }},
  "long_term": {{
    "direction": "BULLISH/BEARISH/NEUTRAL",
    "confidence": 60,
    "target_low": 0.0,
    "target_high": 0.0,
    "reasons": ["r1","r2","r3"]
  }},
  "pump_catalysts": ["c1","c2","c3","c4"],
  "dump_risks":     ["r1","r2","r3","r4"],
  "spot_rec": {{"action":"BUY/SELL/HOLD","rationale":"...","key_risk":"..."}},
  "futures_rec": {{
    "direction":"LONG/SHORT/NO TRADE",
    "warning":"25x = extreme risk, 1-3% of account only",
    "rationale":"...","invalidation":"Trade invalid if price goes below/above $X"
  }},
  "risk_level": "LOW/MEDIUM/HIGH/VERY HIGH",
  "risk_reason": "..."
}}
Fill ALL price fields with real numbers from the data."""

    h={"Authorization":f"Bearer {GROQ_API_KEY}","Content-Type":"application/json"}
    b={"model":"llama-3.3-70b-versatile",
       "messages":[{"role":"user","content":prompt}],
       "max_tokens":1800,"temperature":0.3}
    resp=await fetch(session, GROQ_URL, headers=h, json_body=b, method="POST", timeout=50)
    if not resp or "choices" not in resp:
        return {"error":"Groq unavailable"}
    raw=resp["choices"][0]["message"]["content"].strip()
    if raw.startswith("```"):
        parts=raw.split("```")
        raw=parts[1][4:] if parts[1].startswith("json") else parts[1]
    try:
        return json.loads(raw.strip())
    except:
        return {"error":"JSON parse failed","raw":raw[:300]}

# ─── MAIN ENGINE ──────────────────────────────────────────────────────────────
async def analyze(coin_raw: str) -> list[str]:
    coin=coin_raw.upper().strip().lstrip("/")
    if coin.endswith("USDT"): coin=coin[:-4]
    sym=coin+"USDT"
    now=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    logger.info(f"=== {sym} ===")

    conn=aiohttp.TCPConnector(ssl=False, limit=30)
    hdrs={"User-Agent":"Mozilla/5.0 CryptoBot/8.0"}

    async with aiohttp.ClientSession(connector=conn, headers=hdrs) as s:

        # Symbol check
        spot_chk=await fetch(s,f"{SPOT_REST}/api/v3/ticker/price",{"symbol":sym},timeout=6)
        fut_chk =await fetch(s,f"{FUT_REST}/fapi/v1/ticker/price", {"symbol":sym},timeout=6)
        on_spot=isinstance(spot_chk,dict) and "price" in spot_chk
        on_fut =isinstance(fut_chk, dict) and "price" in fut_chk
        if not on_spot and not on_fut:
            return [f"❌ *{sym}* not found on Binance.\nTry `/BTC` `/ETH` `/SOL`"]

        kb=FUT_REST if on_fut else SPOT_REST
        kp="/fapi/v1/klines" if on_fut else "/api/v3/klines"

        # WebSocket prices (parallel with REST batch)
        ws_s_task=asyncio.create_task(ws_price(sym, False))
        ws_f_task=asyncio.create_task(ws_price(sym, True)) if on_fut else asyncio.sleep(0)

        # REST batch — all at once
        batch=await asyncio.gather(
            fetch(s,f"{SPOT_REST}/api/v3/ticker/24hr",{"symbol":sym}),
            fetch(s,f"{FUT_REST}/fapi/v1/ticker/24hr",{"symbol":sym}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/fapi/v1/premiumIndex",{"symbol":sym}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/fapi/v1/openInterest",{"symbol":sym}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/fapi/v1/fundingRate",{"symbol":sym,"limit":5}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/futures/data/globalLongShortAccountRatio",{"symbol":sym,"period":"5m","limit":1}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/fapi/v1/openInterestHist",{"symbol":sym,"period":"1h","limit":12}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/futures/data/topLongShortPositionRatio",{"symbol":sym,"period":"1h","limit":1}) if on_fut else asyncio.sleep(0),
            fetch(s,f"{FUT_REST}/futures/data/takerlongshortRatio",{"symbol":sym,"period":"5m","limit":1}) if on_fut else asyncio.sleep(0),
            # Indicator klines (100-200 candles for accuracy)
            fetch(s,f"{kb}{kp}",{"symbol":sym,"interval":"1h", "limit":100}),
            fetch(s,f"{kb}{kp}",{"symbol":sym,"interval":"4h", "limit":100}),
            fetch(s,f"{kb}{kp}",{"symbol":sym,"interval":"1d", "limit":200}),
            fetch(s,f"{kb}{kp}",{"symbol":sym,"interval":"1w", "limit":52}),
            # Market context
            fetch(s,f"{SPOT_REST}/api/v3/ticker/24hr",{"symbol":"BTCUSDT"}),
            fetch(s,f"{SPOT_REST}/api/v3/ticker/24hr",{"symbol":"ETHUSDT"}),
            fetch(s,FNG_URL,timeout=6),
            return_exceptions=True
        )
        def d(x,t): return x if isinstance(x,t) else None

        spot_t=d(batch[0],dict); fut_t=d(batch[1],dict); prem=d(batch[2],dict)
        oi=d(batch[3],dict); fund=d(batch[4],list); ls=d(batch[5],list)
        oih=d(batch[6],list); lstop=d(batch[7],list); taker=d(batch[8],list)
        ki1h=d(batch[9],list); ki4h=d(batch[10],list)
        ki1d=d(batch[11],list); ki1w=d(batch[12],list)
        btc_t=d(batch[13],dict); eth_t=d(batch[14],dict); fg=d(batch[15],dict)

        # Best price
        wsp=await ws_s_task
        wsf=await ws_f_task if on_fut else None
        if wsf and on_fut:         price=float(wsf)
        elif wsp and on_spot:      price=float(wsp)
        elif fut_t:                price=float(fut_t.get("lastPrice",0))
        elif spot_t:               price=float(spot_t.get("lastPrice",0))
        else:                      price=0.0

        ws_live=bool(wsp or wsf)

        # Compute indicators
        i1h=compute(ki1h) if ki1h else {}
        i4h=compute(ki4h) if ki4h else {}
        i1d=compute(ki1d) if ki1d else {}
        i1w=compute(ki1w) if ki1w else {}

        # Derived
        tr4h=trend(i4h); tr24h=trend(i1d)
        is_trending=mkt_condition(i4h)=="TRENDING"
        mkc=mkt_condition(i4h); vol=volatility(i4h,price)
        s4h=score(i4h); s1d=score(i1d); s1h=score(i1h)
        combined_spot=(s4h*2+s1d)//3

        # Funding rate
        fr_val=float(prem.get("lastFundingRate",0))*100 if prem else 0.0

        # Signals
        ss=spot_signal(price,i4h,i1d)
        fs=fut_signal(price,i1h,i4h,i1d,fr_val)

        # Predicted price ranges
        ranges=predict_ranges(price,i1h,i4h,i1d,i1w,combined_spot)

        # Groq context (compact)
        gctx={
            "symbol":sym,"price":price,
            "24h_change":float(spot_t.get("priceChangePercent",0)) if spot_t else 0,
            "24h_volume":float(spot_t.get("quoteVolume",0)) if spot_t else 0,
            "trend_4h":tr4h,"trend_24h":tr24h,"is_trending":is_trending,
            "market_condition":mkc,"volatility":vol,
            "on_futures":on_fut,"funding_rate_pct":fr_val,
            "open_interest":float(oi.get("openInterest",0)) if oi else 0,
            "long_pct":float(ls[0].get("longAccount",0))*100 if ls else 50,
            "rsi_1h":i1h.get("rsi"),"rsi_4h":i4h.get("rsi"),"rsi_1d":i1d.get("rsi"),
            "ema9_4h":i4h.get("e9"),"ema21_4h":i4h.get("e21"),
            "macd_hist_4h":i4h.get("hist"),
            "score_1h":s1h,"score_4h":s4h,"score_1d":s1d,
            "spot_signal":ss["action"],"futures_signal":fs.get("direction"),
            "btc_change":float(btc_t.get("priceChangePercent",0)) if btc_t else 0,
            "fear_greed":fg["data"][0]["value"] if fg and "data" in fg else "N/A",
            "predicted_1h_high":ranges["1H"]["predicted_high"],
            "predicted_1h_low":ranges["1H"]["predicted_low"],
            "predicted_1d_high":ranges["1D"]["predicted_high"],
            "predicted_1d_low":ranges["1D"]["predicted_low"],
            "atr_4h":i4h.get("at"),"trend_strength":i4h.get("ts"),
        }

        # Groq call
        ai=await groq_ai(s,coin,gctx)

    # ─── BUILD OUTPUT PAGES ───────────────────────────────────────────────────
    pages=[]

    # ── PAGE 1: Price + Ranges + Market ──
    p1=[]
    p1.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p1.append(f"🪙 *{coin}/USDT — Intelligence Report*")
    p1.append(f"🕐 `{now}`")
    p1.append(f"⚡ _{'WebSocket Live' if ws_live else 'Binance REST'}_  |  "
              f"📡 _{'Spot+Futures' if on_fut else 'Spot only'}_")
    p1.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Broader market
    p1.append("\n🌍 *MARKET CONTEXT*")
    if btc_t:
        bc=float(btc_t.get("priceChangePercent",0)); bp=float(btc_t.get("lastPrice",0))
        p1.append(f"  BTC: {'📈' if bc>=0 else '📉'} `${fmt(bp,0)}` ({bc:+.2f}%)")
    if eth_t:
        ec=float(eth_t.get("priceChangePercent",0)); ep=float(eth_t.get("lastPrice",0))
        p1.append(f"  ETH: {'📈' if ec>=0 else '📉'} `${fmt(ep,0)}` ({ec:+.2f}%)")
    if fg and "data" in fg:
        fgv=fg["data"][0]["value"]; fgc=fg["data"][0]["value_classification"]
        fge="😱" if int(fgv)<25 else("😰" if int(fgv)<40 else("😐" if int(fgv)<60 else("😊" if int(fgv)<80 else"🤑")))
        p1.append(f"  Fear & Greed: {fge} `{fgv}/100` — _{fgc}_")

    # Coin price
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
    p1.append(f"\n📊 *MARKET STATE*")
    te="🚀" if "STRONG UP" in tr4h else("📈" if "UP" in tr4h else("💀" if "STRONG DOWN" in tr4h else("📉" if "DOWN" in tr4h else"↔️")))
    p1.append(f"  Trend (4H):     {te} `{tr4h}`")
    te2="🚀" if "STRONG UP" in tr24h else("📈" if "UP" in tr24h else("💀" if "STRONG DOWN" in tr24h else("📉" if "DOWN" in tr24h else"↔️")))
    p1.append(f"  Trend (24H):    {te2} `{tr24h}`")
    p1.append(f"  Trending Today: `{'✅ YES' if is_trending else '❌ NO (Ranging)'}`")
    p1.append(f"  Condition:      `{mkc}`")
    p1.append(f"  Volatility:     `{vol}`")
    p1.append(f"  Trend Strength: `{i4h.get('ts','N/A')}`")

    # Predicted price ranges
    p1.append(f"\n🔮 *PREDICTED PRICE RANGES (from ${fmt(price,4)})*")
    p1.append(f"  _Based on ATR + trend bias + current indicators_")
    bias_txt={"BULLISH":"🟢 Bullish bias","BEARISH":"🔴 Bearish bias","NEUTRAL":"🟡 Neutral"}
    for tf,rng in ranges.items():
        if rng:
            bt=bias_txt.get(rng.get("bias","NEUTRAL"),"🟡")
            p1.append(
                f"  `{tf:3}` H:`${fmt(rng['predicted_high'],4)}` "
                f"L:`${fmt(rng['predicted_low'],4)}` "
                f"±`{rng['move_pct']:.2f}%` {bt}"
            )

    pages.append("\n".join(p1))

    # ── PAGE 2: Futures + Indicators + Signals ──
    p2=[]

    if on_fut:
        p2.append("🔴 *FUTURES MARKET (LIVE)*")
        if fut_t:
            fp=float(fut_t.get("lastPrice",0)); fc=float(fut_t.get("priceChangePercent",0))
            fv=float(fut_t.get("quoteVolume",0))
            p2.append(f"  Price: `${fmt(fp,6)}`  ({fc:+.2f}%)")
            p2.append(f"  Volume: `{fmt(fv,0)} USDT`")
        if prem:
            mark=float(prem.get("markPrice",0)); idx=float(prem.get("indexPrice",0))
            basis=(mark-idx)/idx*100 if idx else 0
            fe="🔴" if fr_val<0 else "🟢"
            p2.append(f"  Mark: `${fmt(mark,6)}`  Index: `${fmt(idx,6)}`")
            p2.append(f"  Funding: {fe} `{fr_val:.4f}%`  Basis: `{basis:+.4f}%`")
            if fr_val>0.1:     p2.append("   ↳ 🚨 Extreme → SHORT bias")
            elif fr_val>0.05:  p2.append("   ↳ ⚠️ High → dump risk")
            elif fr_val<-0.05: p2.append("   ↳ 🚨 Extreme negative → squeeze coming")
            elif fr_val<-0.01: p2.append("   ↳ 💡 Negative → squeeze possible")
            else:              p2.append("   ↳ ✅ Balanced")
        if oi:
            oiv=float(oi.get("openInterest",0))
            p2.append(f"  Open Interest: `{fmt(oiv,2)} {coin}`")
        if oih and len(oih)>=2:
            ov=[float(x.get("sumOpenInterest",0)) for x in oih]
            oc=(ov[-1]-ov[0])/ov[0]*100 if ov[0] else 0
            oie="🔥" if oc>5 else("📈" if oc>0 else("📉" if oc>-5 else"💀"))
            p2.append(f"  OI Trend(12h): {oie} `{oc:+.2f}%`")
        if ls:
            lp=float(ls[0].get("longAccount",0))*100; sp=100-lp
            le="🟢" if lp>55 else("🔴" if lp<45 else"🟡")
            p2.append(f"  L/S: {le} `{lp:.1f}% Long / {sp:.1f}% Short`")
            if lp>68:   p2.append("   ↳ 🚨 Extreme longs → liquidation risk")
            elif sp>65: p2.append("   ↳ 💡 Heavy shorts → squeeze possible")
        if lstop:
            ltp=float(lstop[0].get("longAccount",0))*100
            p2.append(f"  Top Traders L/S: `{ltp:.1f}% / {100-ltp:.1f}%`")
        if taker:
            tb=float(taker[0].get("buySell",1))
            p2.append(f"  Taker B/S: {'🟢 Buy dom' if tb>1 else '🔴 Sell dom'} `{tb:.3f}`")
        if fund and len(fund)>=3:
            rates=[float(f.get("fundingRate",0))*100 for f in fund]
            avg=sum(rates)/len(rates)
            p2.append(f"  Avg Funding(5): `{avg:.4f}%`  {'Rising📈' if rates[-1]>rates[0] else 'Falling📉'}")

    # Indicators
    def iblock(lbl, ind):
        if not ind: return f"\n📐 *{lbl}* — Insufficient data"
        cur=ind.get("cur",0); lines=[f"\n📐 *{lbl} Technical*"]
        rv=ind.get("rsi")
        if rv is not None:
            re="🔴 Overbought⚠️" if rv>70 else("🟢 Oversold💡" if rv<30 else"🟡 Normal")
            lines.append(f"  RSI(14): `{rv}` — {re}")
        sk=ind.get("sk"); sd=ind.get("sd")
        if sk is not None:
            se="🔴" if sk>80 else("🟢" if sk<20 else"🟡")
            lines.append(f"  StochRSI: {se} K=`{sk}` D=`{sd}`")
        for nm,ky in [("EMA9","e9"),("EMA21","e21"),("EMA50","e50"),("EMA200","e200")]:
            v=ind.get(ky)
            if v:
                diff=(cur-v)/v*100 if v else 0
                lines.append(f"  {nm}: {'🟢' if cur>v else '🔴'} `{fmt(v,6)}` ({diff:+.2f}%)")
        ml=ind.get("macd"); hv=ind.get("hist")
        if ml is not None:
            cross="Bullish📈" if (hv or 0)>0 else "Bearish📉"
            lines.append(f"  MACD: {cross} `{fmt(ml,8)}` Hist:`{fmt(hv,8)}`")
        bl=ind.get("bl"); bu=ind.get("bu"); bm=ind.get("bm")
        if bl and bu:
            rng=bu-bl; pos=(cur-bl)/rng*100 if rng else 50
            tag=" 🔵LOWER" if cur<=bl else(" 🔴UPPER" if cur>=bu else f" {pos:.0f}%")
            lines.append(f"  BB: `{fmt(bl,6)}`/`{fmt(bm,6)}`/`{fmt(bu,6)}`{tag}")
        at=ind.get("at"); vw=ind.get("vw")
        if at: lines.append(f"  ATR(14): `{fmt(at,6)}` ({at/cur*100:.2f}% of price)")
        if vw: lines.append(f"  VWAP: {'🟢' if cur>vw else '🔴'} `{fmt(vw,6)}`")
        vr=ind.get("vr")
        if vr: lines.append(f"  Volume: {'🔥SPIKE' if vr>2 else('📊Normal' if vr>0.7 else'😴Low')} `{vr:.2f}x`")
        sc=score(ind)
        lines.append(f"  *Signal: {siglabel(sc)}* ({sc:+d})")
        lines.append(f"  *Trend: {trend(ind)}*")
        return "\n".join(lines)

    p2.append(iblock("1H",i1h))
    p2.append(iblock("4H",i4h))
    p2.append(iblock("1D",i1d))
    if i1w: p2.append(iblock("1W",i1w))

    # Key levels
    if i1d.get("bl"):
        p2.append("\n🏗 *KEY LEVELS*")
        p2.append(f"  Daily Support:    `${fmt(i1d.get('bl'),6)}`")
        p2.append(f"  Daily Resistance: `${fmt(i1d.get('bu'),6)}`")
        if i1d.get("vw"):
            p2.append(f"  VWAP(1D):         `${fmt(i1d.get('vw'),6)}`")
        if price:
            sup=i1d.get("bl",price); res=i1d.get("bu",price)
            if res>price: p2.append(f"  To Resistance: `+{(res-price)/price*100:.2f}%`")
            if sup<price: p2.append(f"  To Support:    `-{(price-sup)/price*100:.2f}%`")

    # Signals
    p2.append("\n🎯 *TRADE SIGNALS*")
    p2.append(f"\n📈 *SPOT*")
    ae="🟢" if ss["action"]=="BUY" else("🔴" if ss["action"]=="SELL" else"🟡")
    p2.append(f"  {ae} *{ss['action']}*  (confidence: {ss['confidence']}%)")
    p2.append(f"  Entry: `${fmt(ss['entry'],6)}`  SL: `${fmt(ss['sl'],6)}` (-{ss['sl_pct']:.2f}%)")
    p2.append(f"  TP1: `${fmt(ss['tp1'],6)}` (+{ss['tp1_pct']:.2f}%)  R/R: `{ss['rr']}:1`")
    p2.append(f"  TP2: `${fmt(ss['tp2'],6)}`  TP3: `${fmt(ss['tp3'],6)}`")

    p2.append(f"\n🔴 *FUTURES (25x) ⚠️ EXTREME RISK*")
    fd=fs.get("direction","NO TRADE")
    if fd!="NO TRADE":
        fe2="🟢" if fd=="LONG" else"🔴"
        p2.append(f"  {fe2} *{fd}*  (confidence: {fs.get('confidence',0)}%)")
        p2.append(f"  Entry: `${fmt(fs['entry'],6)}`")
        p2.append(f"  SL: `${fmt(fs['sl'],6)}` ({fs['sl_pct']:.2f}% | PnL: {fs['pnl_sl']:+.0f}%)")
        p2.append(f"  Liq: `${fmt(fs['liq'],6)}` ← NEVER let reach here")
        p2.append(f"  TP1: `${fmt(fs['tp1'],6)}` (PnL: +{fs['pnl_tp1']:.0f}%)  TP2: `${fmt(fs['tp2'],6)}` (+{fs['pnl_tp2']:.0f}%)")
        p2.append(f"  TP3: `${fmt(fs['tp3'],6)}`  R/R: `{fs['rr']}:1`")
        p2.append(f"  Max margin: 3% of account only")
    else:
        p2.append(f"  🚫 *NO TRADE* — {fs.get('reason','Weak signal — wait')}")

    pages.append("\n".join(p2))

    # ── PAGE 3: Groq AI ──
    p3=[]
    p3.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p3.append("🤖 *GROQ AI — INTELLIGENCE*")
    p3.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if "error" in ai and "raw" not in ai:
        p3.append(f"⚠️ {ai.get('error','AI error')}")
    else:
        # News
        p3.append("\n📰 *LATEST NEWS*")
        for n in ai.get("news",[])[:4]:
            se="🟢" if n.get("sentiment")=="bullish" else("🔴" if n.get("sentiment")=="bearish" else"🟡")
            p3.append(f"  {se} *{n.get('headline','N/A')}*")
            if n.get("date"): p3.append(f"     _{n['date']}_ → {n.get('impact','')}")

        # Sentiment
        sent=ai.get("overall_sentiment","N/A"); ss_=ai.get("sentiment_score",5)
        se2="🟢" if sent=="BULLISH" else("🔴" if sent=="BEARISH" else"🟡")
        p3.append(f"\n📊 *SENTIMENT: {se2} {sent}* ({ss_}/10)")
        p3.append(f"  _{ai.get('market_context','')}_")

        # Short term
        sto=ai.get("short_term",{}); lto=ai.get("long_term",{})
        p3.append(f"\n⏱ *SHORT TERM (24-72h)*")
        p3.append(f"  *{sto.get('direction','N/A')}*  Confidence: `{sto.get('confidence',0)}%`")
        if sto.get("target_low") and float(sto.get("target_low",0))>0:
            p3.append(f"  Target: `${fmt(sto.get('target_low'),4)}` — `${fmt(sto.get('target_high'),4)}`")
        if sto.get("support") and float(sto.get("support",0))>0:
            p3.append(f"  Support: `${fmt(sto.get('support'),6)}`  Resistance: `${fmt(sto.get('resistance'),6)}`")
        for r in sto.get("reasons",[]):
            p3.append(f"  • {r}")

        p3.append(f"\n📅 *LONG TERM (1-4 weeks)*")
        p3.append(f"  *{lto.get('direction','N/A')}*  Confidence: `{lto.get('confidence',0)}%`")
        if lto.get("target_low") and float(lto.get("target_low",0))>0:
            p3.append(f"  Target: `${fmt(lto.get('target_low'),4)}` — `${fmt(lto.get('target_high'),4)}`")
        for r in lto.get("reasons",[]):
            p3.append(f"  • {r}")

        p3.append(f"\n🚀 *PUMP CATALYSTS*")
        for i,c in enumerate(ai.get("pump_catalysts",[])[:4],1):
            p3.append(f"  {i}. {c}")
        p3.append(f"\n💥 *DUMP RISKS*")
        for i,r in enumerate(ai.get("dump_risks",[])[:4],1):
            p3.append(f"  {i}. {r}")

        sr=ai.get("spot_rec",{}); fr2=ai.get("futures_rec",{})
        p3.append(f"\n📈 *AI SPOT:* *{sr.get('action','N/A')}*")
        p3.append(f"  {sr.get('rationale','')}")
        if sr.get("key_risk"): p3.append(f"  Risk: _{sr['key_risk']}_")

        p3.append(f"\n🔴 *AI FUTURES (25x):* *{fr2.get('direction','N/A')}*")
        if fr2.get("warning"): p3.append(f"  ⚠️ _{fr2['warning']}_")
        p3.append(f"  {fr2.get('rationale','')}")
        if fr2.get("invalidation"): p3.append(f"  Invalid if: _{fr2['invalidation']}_")

        rl=ai.get("risk_level","N/A")
        re2="🟢" if rl=="LOW" else("🟡" if rl=="MEDIUM" else("🔴" if rl=="HIGH" else"🚨"))
        p3.append(f"\n⚠️ *RISK: {re2} {rl}*")
        p3.append(f"  {ai.get('risk_reason','')}")

    p3.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    p3.append("⚠️ _Not financial advice. 25x leverage = extreme risk. DYOR._")
    pages.append("\n".join(p3))
    return pages

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ALLOWED_CHAT: return
    text=update.message.text or ""
    cmd=text.split()[0].lstrip("/").split("@")[0].strip()
    if cmd.lower() in ("start","help"):
        await update.message.reply_text(
            "👋 *Crypto Intelligence Bot v8*\n\n"
            "Type `/` + any coin:\n"
            "• `/BTC` `/ETH` `/SOL` `/PEPE` `/DOGE`\n\n"
            "⚡ WebSocket real-time price\n"
            "🔮 *Predicted* price ranges (1H→1M)\n"
            "📊 Trend: 4H + 24H detection\n"
            "📐 RSI, EMA, MACD, BB, ATR, VWAP, StochRSI\n"
            "🔴 Futures data: OI, Funding, L/S, Taker\n"
            "📈 Spot + Futures signals (25x setup)\n"
            "🤖 Groq AI: news, sentiment, pump/dump\n\n"
            "3 messages — full intelligence report",
            parse_mode="Markdown")
        return
    if not cmd: return
    msg=await update.message.reply_text(
        f"⏳ *{cmd.upper()}/USDT Intelligence Report*\n"
        f"_WebSocket → Binance REST → Groq AI_\n"
        f"_~25 seconds..._",
        parse_mode="Markdown")
    try:
        pages=await analyze(cmd)
        await msg.delete()
        for i,page in enumerate(pages):
            if page.strip():
                await update.message.reply_text(page.strip(),parse_mode="Markdown")
                if i<len(pages)-1: await asyncio.sleep(0.5)
    except Exception as e:
        logger.error(f"Error: {e}",exc_info=True)
        try: await msg.edit_text(f"❌ Error: `{str(e)[:200]}`",parse_mode="Markdown")
        except: pass

def main():
    app=Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",handler))
    app.add_handler(CommandHandler("help",handler))
    app.add_handler(MessageHandler(filters.COMMAND,handler))
    logger.info("🚀 Crypto Intelligence Bot v8 started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=="__main__":
    main()
