"""
MEXC Futures Trading Bot v3
Strategia: Stoch K crossover/crossunder D + BB proximity + MA200 filter
Multi-para, zapis konfiguracji, system logowania
"""

import hmac, hashlib, time, requests, numpy as np, asyncio, logging, json, os
from typing import Optional, List, Dict
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta
import uvicorn
import secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="MEXC Futures Bot v3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MEXC_BASE    = "https://api.mexc.com"
CONFIG_FILE  = "config.json"
AUTH_FILE    = "auth.json"
LOG_FILE     = "logs.json"
security     = HTTPBearer(auto_error=False)

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "8828080533:AAFrSbgunu3LJ8IhKMmjnvesNl6rD5j9tKk"
TELEGRAM_CHAT_ID = "5137354808"

def tg(msg: str):
    """Wyslij wiadomosc na Telegram"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ─── AUTH ────────────────────────────────────────────────────────────────────

class AuthManager:
    def __init__(self):
        self.password_hash = ""
        self.tokens: Dict[str, datetime] = {}
        self.token_ttl = timedelta(hours=24)
        self._load()

    def _load(self):
        if os.path.exists(AUTH_FILE):
            try:
                d = json.load(open(AUTH_FILE))
                self.password_hash = d.get("password_hash", "")
            except: pass
        if not self.password_hash:
            self.password_hash = hashlib.sha256("admin123".encode()).hexdigest()
            self._save()

    def _save(self):
        json.dump({"password_hash": self.password_hash}, open(AUTH_FILE, "w"))

    def verify_password(self, password: str) -> bool:
        return hashlib.sha256(password.encode()).hexdigest() == self.password_hash

    def change_password(self, old_password: str, new_password: str) -> bool:
        if not self.verify_password(old_password): return False
        self.password_hash = hashlib.sha256(new_password.encode()).hexdigest()
        self._save()
        return True

    def create_token(self) -> str:
        token = secrets.token_hex(32)
        self.tokens[token] = datetime.now() + self.token_ttl
        return token

    def verify_token(self, token: str) -> bool:
        if token not in self.tokens: return False
        if datetime.now() > self.tokens[token]:
            del self.tokens[token]; return False
        return True

    def revoke_token(self, token: str):
        self.tokens.pop(token, None)

auth_manager = AuthManager()

def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials or not auth_manager.verify_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return credentials.credentials


# ─── MEXC CLIENT ─────────────────────────────────────────────────────────────

class MEXCClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key    = api_key
        self.api_secret = api_secret

    def _sign(self, s: str) -> str:
        return hmac.new(self.api_secret.encode(), s.encode(), hashlib.sha256).hexdigest()

    def _headers(self, ts: str, sign: str) -> dict:
        return {"ApiKey": self.api_key, "Request-Time": ts,
                "Signature": sign, "Content-Type": "application/json"}

    def _get(self, path: str, params: dict = None) -> dict:
        ts   = str(int(time.time() * 1000))
        ps   = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        sign = self._sign(self.api_key + ts + ps)
        r    = requests.get(MEXC_BASE + path, params=params,
                            headers=self._headers(ts, sign), timeout=10)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        ts   = str(int(time.time() * 1000))
        bs   = json.dumps(body)
        sign = self._sign(self.api_key + ts + bs)
        r    = requests.post(MEXC_BASE + path, data=bs,
                             headers=self._headers(ts, sign), timeout=10)
        r.raise_for_status()
        return r.json()

    def get_klines_full(self, symbol: str, interval: str, limit: int = 500) -> dict:
        d = self._get(f"/api/v1/contract/kline/{symbol}",
                      {"interval": interval, "limit": limit})
        return d.get("data", {})

    def get_ticker(self, symbol: str) -> dict:
        d = self._get("/api/v1/contract/ticker", {"symbol": symbol})
        return d.get("data", {})

    def set_leverage(self, symbol: str, leverage: int):
        try:
            self._post("/api/v1/private/position/change_leverage",
                       {"symbol": symbol, "leverage": leverage,
                        "openType": 1, "positionType": 1})
        except: pass

    def place_order(self, symbol: str, side: int, vol: float, leverage: int,
                    sl: Optional[float] = None, tp: Optional[float] = None) -> dict:
        body = {"symbol": symbol, "side": side, "openType": gstate.margin_mode,
                "type": 5, "vol": vol, "leverage": leverage}
        if sl: body["stopLossPrice"]   = sl
        if tp: body["takeProfitPrice"] = tp
        return self._post("/api/v1/private/order/submit", body)

    def get_positions(self, symbol: str = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        d = self._get("/api/v1/private/position/open_positions", params)
        return d.get("data", [])

    def close_position(self, symbol: str, pos_type: int, vol: int, leverage: int) -> dict:
        """Zamknij pozycję: pos_type 1=long(close side=4), 2=short(close side=2)"""
        close_side = 4 if pos_type == 1 else 2
        body = {"symbol": symbol, "side": close_side, "openType": gstate.margin_mode,
                "type": 5, "vol": vol, "leverage": leverage}
        return self._post("/api/v1/private/order/submit", body)

    def set_tp_sl(self, position_id: str, symbol: str,
                  tp: Optional[float] = None, sl: Optional[float] = None) -> dict:
        body = {"positionId": position_id, "symbol": symbol}
        if tp: body["takeProfitPrice"] = tp
        if sl: body["stopLossPrice"]   = sl
        try:
            return self._post("/api/v1/private/position/change_margin", body)
        except:
            return {}

    def cancel_all_tpsl_orders(self, symbol: str) -> bool:
        """Anuluj wszystkie aktywne zlecenia TP/SL dla symbolu"""
        try:
            result = self._get("/api/v1/private/stoporder/list/orders",
                               {"symbol": symbol})
            orders = result.get("data", [])
            active = [o["id"] for o in orders 
                      if o.get("state") in [1, 2] and o.get("isFinished") == 0]
            if not active:
                return True
            cancel = self._post("/api/v1/private/stoporder/cancel",
                    {"orderIdList": ",".join(str(i) for i in active), "symbol": symbol})
            logger.info(f"cancel_tpsl: {cancel}")
            return cancel.get("success", False)
        except Exception as e:
            logger.error(f"cancel_all_tpsl error: {e}")
            return False

    def update_position_tp_sl(self, symbol: str, side: str,
                               tp, sl, leverage: int) -> bool:
        """Modyfikuj istniejace zlecenie TP/SL zamiast tworzyc nowe"""
        try:
            result = self._get("/api/v1/private/stoporder/list/orders",
                               {"symbol": symbol})
            orders = result.get("data", [])
            active = [o for o in orders
                      if o.get("state") in [1, 2] and o.get("isFinished") == 0]
            if not active:
                logger.info(f"Brak aktywnych zlecen TP/SL dla {symbol}")
                return False
            o = active[0]
            order_id = o.get("orderId")
            body = {"orderId": order_id, "symbol": symbol}
            if tp: body["takeProfitPrice"] = tp
            if sl: body["stopLossPrice"] = sl
            r = self._post("/api/v1/private/stoporder/change_price", body)
            logger.info(f"change_price response: {r}")
            return r.get("success", False)
        except Exception as e:
            logger.error(f"update_position_tp_sl error: {e}")
            return False
# ─── INDICATORS ──────────────────────────────────────────────────────────────

def bollinger_bands(closes, period=20, std_dev=2.0):
    arr = np.array(closes, dtype=float)
    if len(arr) < period: return None, None, None
    mid   = np.mean(arr[-period:])
    std   = np.std(arr[-period:])
    return mid + std_dev * std, mid, mid - std_dev * std

def calc_stoch_k_series(closes, highs, lows, stoch_period=14, smooth_k=3):
    """TradingView: ta.stoch(close,high,low,14) wygładzony SMA(k,3)"""
    n = len(closes)
    if n < stoch_period + smooth_k: return []
    raw = []
    for i in range(stoch_period - 1, n):
        h = max(highs[i - stoch_period + 1:i + 1])
        l = min(lows[i  - stoch_period + 1:i + 1])
        raw.append(50.0 if h == l else 100 * (closes[i] - l) / (h - l))
    smooth = []
    for i in range(smooth_k - 1, len(raw)):
        smooth.append(float(np.mean(raw[i - smooth_k + 1:i + 1])))
    return smooth

def calc_stoch_d_series(k_series, smooth_d=3):
    if len(k_series) < smooth_d: return []
    return [float(np.mean(k_series[i - smooth_d + 1:i + 1]))
            for i in range(smooth_d - 1, len(k_series))]

def calc_ma200(closes, period=200):
    if len(closes) < period: return None
    return float(np.mean(closes[-period:]))

def calc_rsi_series(closes, period=14):
    arr = np.array(closes, dtype=float)
    if len(arr) < period + 1: return []
    deltas = np.diff(arr)
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    ag = np.mean(gains[:period])
    al = np.mean(losses[:period])
    rsi = []
    for i in range(period, len(deltas)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        rs = ag / al if al != 0 else 999
        rsi.append(100 - 100 / (1 + rs))
    return rsi

def detect_divergences(closes, highs, lows, rsi_period=14, max_dist=50,
                        ob=70.0, os_=30.0):
    rsi = calc_rsi_series(closes, rsi_period)
    if len(rsi) < max_dist: return False, False
    rsi_w  = rsi[-max_dist:]
    low_w  = lows[-(max_dist):]
    high_w = highs[-(max_dist):]
    bullish = bearish = False
    os_idx = [i for i, r in enumerate(rsi_w) if r < os_]
    if len(os_idx) >= 2:
        i1, i2 = os_idx[-2], os_idx[-1]
        if i2 - i1 <= max_dist and low_w[i2] < low_w[i1] and rsi_w[i2] > rsi_w[i1]:
            bullish = True
    ob_idx = [i for i, r in enumerate(rsi_w) if r > ob]
    if len(ob_idx) >= 2:
        i1, i2 = ob_idx[-2], ob_idx[-1]
        if i2 - i1 <= max_dist and high_w[i2] > high_w[i1] and rsi_w[i2] < rsi_w[i1]:
            bearish = True
    return bullish, bearish


# ─── PER-PAIR STATE ───────────────────────────────────────────────────────────

class PyramidEntry:
    def __init__(self, price, vol, side):
        self.price = price; self.vol = vol; self.side = side
        self.time  = datetime.now().strftime("%H:%M:%S")

class PairState:
    def __init__(self, symbol):
        self.symbol = symbol; self.enabled = True
        self.interval = "Min5"; self.direction = "BOTH"
        self.entry_timing = "CLOSE"
        self.bb_period = 20; self.bb_std = 2.0; self.bb_proximity = 0.0; self.bb_breakout_pct = 0.0
        self.stoch_period = 14; self.stoch_smooth_k = 3; self.stoch_smooth_d = 3
        self.stoch_overbought = 80; self.stoch_oversold = 20
        self.ma200_enabled = False; self.ma200_tf = "Min60"
        self.pyramid_levels = [
            {"enabled": True,  "amount_usd": 10.0, "offset_pct": 0.0},
            {"enabled": True,  "amount_usd": 20.0, "offset_pct": 0.3},
            {"enabled": True,  "amount_usd": 30.0, "offset_pct": 0.6},
            {"enabled": False, "amount_usd": 40.0, "offset_pct": 1.0},
        ]
        self.leverage = 10; self.tp_mode = "FROM_AVG"
        self.tp_pct = 1.0; self.sl_pct = 1.5
        self.last_price = self.last_bb_upper = self.last_bb_lower = None
        self.last_bb_mid = self.last_stoch_k = self.last_stoch_d = None
        self.last_ma200 = None; self.last_signal = "NONE"; self.last_check = None
        self.divergence_bull = False; self.divergence_bear = False
        self.stoch_window = 5
        self.bb_breakout_candle: Optional[int] = None
        self.bb_breakout_side: Optional[str] = None
        self.pyramid_entries: List[PyramidEntry] = []
        self.pyramid_active = False; self.pyramid_side = None
        self.current_tp: Optional[float] = None
        self.current_sl: Optional[float] = None
        self.pyramid_limit_order_ids: List = []
        self.open_positions = []; self.logs: List[dict] = []
        self.hedge_entries: List[PyramidEntry] = []
        self.hedge_active = False; self.hedge_side = None
        self.hedge_current_tp: Optional[float] = None
        self.hedge_current_sl: Optional[float] = None
        self.hedge_limit_order_ids: List = []

    def log(self, msg, level="INFO"):
        self.logs.insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                              "level": level, "msg": msg})
        self.logs = self.logs[:200]
        logger.info(f"[{self.symbol}] {msg}")
        # Zapisz logi do pliku co 10 wpisów
        if len(self.logs) % 10 == 0:
            try: save_logs()
            except: pass

    @property
    def pyramid_avg_entry(self):
        if not self.pyramid_entries: return None
        tv = sum(e.vol for e in self.pyramid_entries)
        return sum(e.price * e.vol for e in self.pyramid_entries) / tv if tv else None

    @property
    def pyramid_last_price(self):
        return self.pyramid_entries[-1].price if self.pyramid_entries else None

    @property
    def pyramid_count(self): return len(self.pyramid_entries)

    def reset_pyramid(self):
        self.pyramid_entries = []; self.pyramid_active = False; self.pyramid_side = None
        self.pyramid_limit_order_ids = []

    def reset_hedge(self):
        self.hedge_entries = []; self.hedge_active = False; self.hedge_side = None
        self.hedge_limit_order_ids = []

    def to_dict(self):
        return {
            "symbol": self.symbol, "enabled": self.enabled,
            "interval": self.interval, "direction": self.direction,
            "entry_timing": self.entry_timing,
            "bb_period": self.bb_period, "bb_std": self.bb_std,
            "bb_proximity": self.bb_proximity,
            "bb_breakout_pct": self.bb_breakout_pct,
            "stoch_period": self.stoch_period,
            "stoch_smooth_k": self.stoch_smooth_k,
            "stoch_smooth_d": self.stoch_smooth_d,
            "stoch_overbought": self.stoch_overbought,
            "stoch_oversold": self.stoch_oversold,
            "stoch_window": self.stoch_window,
            "ma200_enabled": self.ma200_enabled, "ma200_tf": self.ma200_tf,
            "pyramid_levels": self.pyramid_levels,
            "leverage": self.leverage, "tp_mode": self.tp_mode,
            "tp_pct": self.tp_pct, "sl_pct": self.sl_pct,
            "last_price": self.last_price,
            "last_bb_upper": self.last_bb_upper, "last_bb_lower": self.last_bb_lower,
            "last_bb_mid": self.last_bb_mid,
            "last_stoch_k": self.last_stoch_k, "last_stoch_d": self.last_stoch_d,
            "last_ma200": self.last_ma200,
            "last_signal": self.last_signal, "last_check": self.last_check,
            "divergence_bull": self.divergence_bull,
            "divergence_bear": self.divergence_bear,
            "pyramid": {
                "active": self.pyramid_active, "side": self.pyramid_side,
                "count": self.pyramid_count, "avg_entry": self.pyramid_avg_entry,
                "entries": [{"price": e.price, "vol": e.vol,
                             "side": e.side, "time": e.time}
                            for e in self.pyramid_entries],
            },
            "current_tp": self.current_tp,
            "current_sl": self.current_sl,
            "open_positions": self.open_positions,
            "logs": self.logs[:40],
        }


# ─── GLOBAL STATE ────────────────────────────────────────────────────────────

class GlobalState:
    def __init__(self):
        self.running = False; self.api_key = ""; self.api_secret = ""
        self.signals_only = False
        self.max_positions = 1
        self.margin_mode = 1  # 1=Isolated, 2=Cross
        self.tp_sl_enabled = True
        self.hedging_enabled = False
        self.pairs: Dict[str, PairState] = {}
        self.global_logs: List[dict] = []

    def log(self, msg, level="INFO"):
        self.global_logs.insert(0, {"time": datetime.now().strftime("%H:%M:%S"),
                                     "level": level, "msg": msg})
        self.global_logs = self.global_logs[:60]
        logger.info(f"[GLOBAL] {msg}")

    def get_or_create(self, symbol):
        if symbol not in self.pairs: self.pairs[symbol] = PairState(symbol)
        return self.pairs[symbol]

gstate    = GlobalState()
_bot_task = None

# ─── CONFIG PERSISTENCE ──────────────────────────────────────────────────────

def save_config():
    try:
        data = {"api_key": gstate.api_key, "api_secret": gstate.api_secret, "pairs": []}
        for ps in gstate.pairs.values():
            data["pairs"].append({
                k: getattr(ps, k) for k in [
                    "symbol","enabled","interval","direction","entry_timing",
                    "bb_period","bb_std","bb_proximity","bb_breakout_pct","stoch_period",
                    "stoch_smooth_k","stoch_smooth_d","stoch_overbought","stoch_oversold",
                    "stoch_window","ma200_enabled","ma200_tf","pyramid_levels",
                    "leverage","tp_mode","tp_pct","sl_pct"
                ]
            })
        data["signals_only"] = gstate.signals_only
        data["max_positions"] = gstate.max_positions
        data["margin_mode"] = gstate.margin_mode
        data["tp_sl_enabled"] = gstate.tp_sl_enabled
        data["hedging_enabled"] = gstate.hedging_enabled
        json.dump(data, open(CONFIG_FILE, "w"), indent=2)
        logger.info("✅ Config saved")
    except Exception as e:
        logger.error(f"Save error: {e}")

def load_config():
    if not os.path.exists(CONFIG_FILE): return
    try:
        data = json.load(open(CONFIG_FILE))
        gstate.api_key    = data.get("api_key", "")
        gstate.api_secret = data.get("api_secret", "")
        gstate.signals_only = data.get("signals_only", False)
        gstate.max_positions = data.get("max_positions", 1)
        gstate.margin_mode = data.get("margin_mode", 1)
        gstate.tp_sl_enabled = data.get("tp_sl_enabled", True)
        gstate.hedging_enabled = data.get("hedging_enabled", False)
        for pd in data.get("pairs", []):
            sym = pd.get("symbol"); 
            if not sym: continue
            ps = gstate.get_or_create(sym)
            for k in ["enabled","interval","direction","entry_timing",
                      "bb_period","bb_std","bb_proximity","bb_breakout_pct","stoch_period",
                      "stoch_smooth_k","stoch_smooth_d","stoch_overbought","stoch_oversold",
                      "stoch_window","ma200_enabled","ma200_tf","pyramid_levels",
                      "leverage","tp_mode","tp_pct","sl_pct"]:
                if k in pd: setattr(ps, k, pd[k])
        logger.info(f"✅ Config loaded — {len(gstate.pairs)} pairs")
    except Exception as e:
        logger.error(f"Load error: {e}")

load_config()


# ─── LOG PERSISTENCE ─────────────────────────────────────────────────────────

def save_logs():
    try:
        data = {}
        for sym, ps in gstate.pairs.items():
            data[sym] = ps.logs[:200]
        json.dump(data, open(LOG_FILE, "w"), ensure_ascii=False)
    except Exception as e:
        logger.error(f"Log save error: {e}")

def load_logs():
    if not os.path.exists(LOG_FILE): return
    try:
        data = json.load(open(LOG_FILE))
        for sym, logs in data.items():
            if sym in gstate.pairs:
                gstate.pairs[sym].logs = logs
        logger.info("✅ Logi wczytane z pliku")
    except Exception as e:
        logger.error(f"Log load error: {e}")

load_logs()


# ─── STRATEGY ────────────────────────────────────────────────────────────────

def run_pair_strategy(client: MEXCClient, ps: PairState):
    try:
        kdata  = client.get_klines_full(ps.symbol, ps.interval, 500)
        closes = [float(x) for x in kdata.get("close", [])]
        highs  = [float(x) for x in kdata.get("high",  [])]
        lows   = [float(x) for x in kdata.get("low",   [])]

        if len(closes) < 250: ps.log("Za mało danych", "WARN"); return

        price = closes[-1]
        ps.last_price = price
        ps.last_check = datetime.now().strftime("%H:%M:%S")

        # Bollinger Bands
        upper, mid, lower = bollinger_bands(closes, ps.bb_period, ps.bb_std)
        if upper is None: ps.log("Błąd BB", "WARN"); return
        ps.last_bb_upper = round(upper, 4)
        ps.last_bb_lower = round(lower, 4)
        ps.last_bb_mid   = round(mid,   4)
        dev = upper - mid   # = std_dev * stdev

        # Bliskość BB (TradingView: close < lowerBB + dev*proximity)
        breakout_margin = upper * ps.bb_breakout_pct / 100
        # Sprawdz czy cena wybiła BB w ostatnich 3 świecach
        near_lower = any(
            closes[-(i+1)] < (lower - breakout_margin + dev * ps.bb_proximity)
            for i in range(3) if len(closes) > i
        )
        near_upper = any(
            closes[-(i+1)] > (upper + breakout_margin - dev * ps.bb_proximity)
            for i in range(3) if len(closes) > i
        )

        # Stochastic K & D
        k_series = calc_stoch_k_series(closes, highs, lows,
                                        ps.stoch_period, ps.stoch_smooth_k)
        d_series = calc_stoch_d_series(k_series, ps.stoch_smooth_d)

        if len(k_series) < 2 or len(d_series) < 2:
            ps.log("Za mało danych Stoch", "WARN"); return

        k_now  = k_series[-1]; k_prev = k_series[-2]
        d_now  = d_series[-1]; d_prev = d_series[-2]
        ps.last_stoch_k = round(k_now, 2)
        ps.last_stoch_d = round(d_now, 2)

        # Crossover / Crossunder - tylko biezaca swieca z minimalnym przekroczeniem 0.5pkt
        candle_idx = len(closes) - 1
        cross_min = 0.5
        k_cross_over_now  = (len(k_series) >= 2 and len(d_series) >= 2 and
                             k_series[-2] <= d_series[-2] and
                             k_series[-1] > d_series[-1] + cross_min and
                             k_now < ps.stoch_oversold)
        k_cross_under_now = (len(k_series) >= 2 and len(d_series) >= 2 and
                             k_series[-2] >= d_series[-2] and
                             k_series[-1] < d_series[-1] - cross_min and
                             k_now > ps.stoch_overbought)

        # Pobierz timestamp ostatniej swieci
        kdata_times = kdata.get("time", [])
        last_ts = int(kdata_times[-1]) if kdata_times else 0
        iv_seconds = {"Min1":60,"Min5":300,"Min15":900,"Min30":1800,"Min60":3600,"Hour4":14400}.get(ps.interval, 300)

        # Zapamiętaj wybicie BB (timestamp pierwszej swieci wybicia)
        if near_lower and ps.bb_breakout_side != "LONG":
            ps.bb_breakout_candle = last_ts
            ps.bb_breakout_side = "LONG"
        if near_upper and ps.bb_breakout_side != "SHORT":
            ps.bb_breakout_candle = last_ts
            ps.bb_breakout_side = "SHORT"

        # Reset wybicia jesli za stare (okno w swiecach)
        window = ps.stoch_window
        if ps.bb_breakout_candle is not None:
            # last_ts jest w sekundach, iv_seconds tez w sekundach
            ts_now = int(kdata_times[-1]) if kdata_times else 0
            ts_now = ts_now // 1000 if ts_now > 9999999999 else ts_now
            bb_ts  = ps.bb_breakout_candle // 1000 if ps.bb_breakout_candle > 9999999999 else ps.bb_breakout_candle
            candles_elapsed = (ts_now - bb_ts) // iv_seconds
            if candles_elapsed > window:
                ps.log(f"BB breakout wygas ({candles_elapsed} swiec > {window})")
                ps.bb_breakout_candle = None
                ps.bb_breakout_side = None

        # Crossover w oknie po wybiciu BB
        k_cross_over  = (k_cross_over_now and
                         ps.bb_breakout_side == "LONG" and
                         ps.bb_breakout_candle is not None)
        k_cross_under = (k_cross_under_now and
                         ps.bb_breakout_side == "SHORT" and
                         ps.bb_breakout_candle is not None)

        # MA200 filter
        ma200_ok_long = ma200_ok_short = True
        if ps.ma200_enabled:
            ma_data   = client.get_klines_full(ps.symbol, ps.ma200_tf, 210)
            ma_closes = [float(x) for x in ma_data.get("close", [])]
            ma200     = calc_ma200(ma_closes, 200)
            ps.last_ma200      = round(ma200, 4) if ma200 else None
            if ma200:
                ma200_ok_long  = price > ma200
                ma200_ok_short = price < ma200
        else:
            ps.last_ma200 = None

        # Dywergencje
        try:
            ps.divergence_bull, ps.divergence_bear = detect_divergences(closes, highs, lows)
        except:
            ps.divergence_bull = ps.divergence_bear = False

        # Sygnał
        signal = "WAIT"
        long_ok  = (k_cross_over  and k_now < ps.stoch_oversold
                    and ma200_ok_long  and ps.direction in ("LONG",  "BOTH"))
        short_ok = (k_cross_under and k_now > ps.stoch_overbought
                    and ma200_ok_short and ps.direction in ("SHORT", "BOTH"))

        if long_ok:   signal = "LONG"
        elif short_ok: signal = "SHORT"
        ps.last_signal = signal

        div = (" 📈DivBull" if ps.divergence_bull else "") + \
              (" 📉DivBear" if ps.divergence_bear else "")
        ma  = f" MA:{round(ps.last_ma200,0) if ps.last_ma200 else 'off'}"
        ps.log(f"P:{price} BB:[{round(lower,2)}-{round(upper,2)}] bm:{round(breakout_margin,3)} K:{round(k_now,1)} D:{round(d_now,1)} "
               f"xO:{k_cross_over} xU:{k_cross_under} "
               f"nL:{near_lower} nU:{near_upper}{ma}{div} -> {signal}")

        # Piramida
        if signal in ("LONG","SHORT") and not ps.pyramid_active:
            # Sprawdz czy jest juz aktywna pozycja na innej parze
            other_active = any(
                other_ps.pyramid_active
                for sym, other_ps in gstate.pairs.items()
                if sym != ps.symbol
            )
            active_count = sum(1 for other_ps in gstate.pairs.values() if other_ps.pyramid_active)
            if active_count >= gstate.max_positions:
                ps.log(f"Blokada: {active_count}/{gstate.max_positions} aktywnych pozycji", "WARN")
            elif gstate.signals_only:
                ps.log(f"📡 SYGNAŁ {signal} @ {price} (tryb sygnałów — brak wejścia)")
                iv_label = {"Min1":"1min","Min5":"5min","Min15":"15min","Min30":"30min","Hour1":"1h","Hour4":"4h"}.get(ps.interval, ps.interval)
                tg(f"📡 <b>{ps.symbol}</b> SYGNAŁ {signal}\n"
                   f"🕐 {datetime.now().strftime('%H:%M:%S')} ({iv_label})\n"
                   f"Cena: {price}\n"
                   f"BB: [{round(ps.last_bb_lower,2)}-{round(ps.last_bb_upper,2)}]\n"
                   f"StochK: {ps.last_stoch_k} D: {ps.last_stoch_d}\n"
                   f"MA200: {round(ps.last_ma200,0) if ps.last_ma200 else 'off'}")
            else:
                _open_pyramid_level(client, ps, signal, price, 0)
        elif gstate.hedging_enabled and signal in ("LONG","SHORT") and ps.pyramid_active and ps.pyramid_side != signal and not ps.hedge_active:
            _open_hedge_level(client, ps, signal, price)
        elif ps.pyramid_active and ps.pyramid_side == signal:
            _check_pyramid_continuation(client, ps, price)
        elif gstate.hedging_enabled and ps.hedge_active and ps.hedge_side == signal:
            _check_hedge_continuation(client, ps, price)

    except Exception as e:
        ps.log(f"Błąd: {e}", "ERROR")

    try:
        all_pos = client.get_positions()
        current = [p for p in all_pos if p.get("symbol") == ps.symbol]

        # Sprawdź czy pozycja została zamknięta (TP/SL hit)
        if ps.pyramid_active and not current:
            ps.log("✅ Pozycja zamknięta (TP/SL) — resetuję piramidę", "SUCCESS")
            ps.reset_pyramid()
            ps.current_tp = None
            ps.current_sl = None

        # Pobierz aktualny PnL
        for p in current:
            try:
                pnl = float(p.get("unrealizedProfit", p.get("unrealizedValue", 0)))
                p["unrealizedValue"] = round(pnl, 4)
            except: pass

        ps.open_positions = current
    except: pass


def _calc_sl(exec_price: float, side: str, sl_pct: float, prec: int = 4) -> float:
    """SL od podanej ceny wejścia"""
    if side == "LONG":
        return round(exec_price * (1 - sl_pct / 100), prec)
    else:
        return round(exec_price * (1 + sl_pct / 100), prec)

def _calc_tp(avg_price: float, first_price: float, side: str,
             tp_pct: float, tp_mode: str, prec: int = 4) -> float:
    """TP od średniej lub pierwszego wejścia"""
    base = avg_price if tp_mode == "FROM_AVG" else first_price
    if side == "LONG":
        return round(base * (1 + tp_pct / 100), prec)
    else:
        return round(base * (1 - tp_pct / 100), prec)

def _open_pyramid_level(client, ps, side, price, level_idx):
    """
    Nowa logika:
    - Wejscie 1: market order + od razu limit orders na dokładki + SL od ostatniej dokładki
    - Dokładki wchodza automatycznie w MEXC bez monitora bota
    - TP tylko w pamieci bota (monitor co 5s)
    """
    active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
    if level_idx >= len(active): return
    lvl = active[level_idx]

    # Wywolujemy tylko dla pierwszego wejscia - dokładki sa od razu ustawiane
    if level_idx != 0: return

    try:
        client.set_leverage(ps.symbol, ps.leverage)
        ticker     = client.get_ticker(ps.symbol)
        exec_price = float(ticker.get("lastPrice", price))

        contract_size = {"BTC_USDT": 0.0001, "ETH_USDT": 0.01, "SOL_USDT": 0.1, "SUI_USDT": 1.0, "DOGE_USDT": 100.0, "ADA_USDT": 1.0, "LINK_USDT": 0.1, "HYPE_USDT": 0.1, "NAS100_USDT": 0.00001, "SP500_USDT": 0.0001, "BNB_USDT": 0.01, "XRP_USDT": 1.0, "TRX_USDT": 10.0, "LTC_USDT": 0.01, "AVAX_USDT": 0.1, "ONDO_USDT": 10.0, "UNI_USDT": 0.1, "TAO_USDT": 0.01, "XAU_USDT": 0.001, "ARB_USDT": 1.0, "GALA_USDT": 10.0, "ATOM_USDT": 0.1, "DOT_USDT": 0.1, "ALGO_USDT": 1.0, "JUP_USDT": 10.0, "KAITO_USDT": 1.0, "PENGU_USDT": 10.0, "WLFI_USDT": 1.0, "BCH_USDT": 0.01}.get(ps.symbol, 1.0)

        # ── Wejście 1 (market order) ──────────────────────────────────────────
        vol0 = max(1, round(lvl["amount_usd"] / (exec_price * contract_size / ps.leverage)))
        result = client.place_order(ps.symbol, 1 if side == "LONG" else 3,
                                    vol0, ps.leverage, None, None)
        if not result.get("success", False):
            ps.log(f"MEXC odrzucil wejscie: {result.get('message','')}", "ERROR")
            return

        ps.pyramid_entries.append(PyramidEntry(exec_price, vol0, side))
        ps.pyramid_active = True
        ps.pyramid_side   = side

        # ── Dokładki jako limit orders ────────────────────────────────────────
        limit_order_ids = []
        last_dok_price  = exec_price
        last_dok_vol    = vol0

        price_precision = {"BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4, "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3, "NAS100_USDT": 0, "SP500_USDT": 2, "BNB_USDT": 1, "XRP_USDT": 4, "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4, "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5, "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4, "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5, "BCH_USDT": 2}.get(ps.symbol, 4)

        for i, dok_lvl in enumerate(active[1:], start=1):
            if side == "LONG":
                dok_price = round(last_dok_price * (1 - dok_lvl["offset_pct"] / 100), price_precision)
                dok_side  = 1  # Buy Long
            else:
                dok_price = round(last_dok_price * (1 + dok_lvl["offset_pct"] / 100), price_precision)
                dok_side  = 3  # Sell Short

            dok_vol = max(1, round(dok_lvl["amount_usd"] / (dok_price * contract_size / ps.leverage)))

            dok_result = client._post("/api/v1/private/order/submit", {
                "symbol":   ps.symbol,
                "side":     dok_side,
                "openType": gstate.margin_mode,
                "type":     1,  # limit order
                "vol":      dok_vol,
                "leverage": ps.leverage,
                "price":    dok_price
            })

            if dok_result.get("success", False):
                limit_order_ids.append(dok_result.get("data"))
                ps.log(f"Dokladka {i+1} limit @ {dok_price} ({dok_lvl['offset_pct']}%, {dok_lvl['amount_usd']}$) — ID:{dok_result.get('data')}")
                last_dok_price = dok_price
                last_dok_vol   = dok_vol
            else:
                ps.log(f"Blad dokladki {i+1}: {dok_result.get('message','')}", "WARN")
            time.sleep(2.0)

        ps.pyramid_limit_order_ids = limit_order_ids

        # ── SL od ostatniej dokładki ──────────────────────────────────────────
        price_prec = {"BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4, "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3, "NAS100_USDT": 0, "SP500_USDT": 2, "BNB_USDT": 1, "XRP_USDT": 4, "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4, "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5, "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4, "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5, "BCH_USDT": 2}.get(ps.symbol, 4)
        sl_price = _calc_sl(last_dok_price, side, ps.sl_pct, price_prec)

        # Oblicz srednia (wejscie + wszystkie dokładki)
        all_vols   = [vol0] + [max(1, round(l["amount_usd"] / (round(exec_price * (1 - l["offset_pct"]/100), 4) * contract_size / ps.leverage))) for l in active[1:]]
        all_prices = [exec_price] + [round(exec_price * (1 - l["offset_pct"]/100), 4) if side == "LONG"
                                     else round(exec_price * (1 + l["offset_pct"]/100), 4) for l in active[1:]]
        total_vol  = sum(all_vols)
        avg_price  = sum(p*v for p,v in zip(all_prices, all_vols)) / total_vol if total_vol else exec_price

        # TP od wejscia 1 (bez dokładek)
        tp_price = _calc_tp(exec_price, exec_price, side, ps.tp_pct, ps.tp_mode, price_prec)
        ps.current_tp = tp_price
        ps.current_sl = sl_price

        # Ustaw SL w MEXC przez stoporder/place z positionId
        if not gstate.tp_sl_enabled:
            ps.log(f"TP/SL wyłączone — pozycja bez SL w MEXC", "WARN")
        else:
            try:
                import time as _time
                _time.sleep(1.0)
                positions = client.get_positions(ps.symbol)
                if positions:
                    pos_id = positions[0].get("positionId")
                    hold_vol = positions[0].get("holdVol", 0)
                    pos_type = positions[0].get("positionType", 1)
                    price_prec = {"BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4, "BNB_USDT": 1, "XRP_USDT": 4, "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3, "NAS100_USDT": 0, "SP500_USDT": 2, "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4, "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5, "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4, "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5, "BCH_USDT": 2}.get(ps.symbol, 4)
                    sl_result = client._post("/api/v1/private/stoporder/place", {
                        "positionId": pos_id, "symbol": ps.symbol, "vol": hold_vol,
                        "lossTrend": 1, "profitTrend": 1, "stopLossPrice": round(sl_price, price_prec)
                    })
                    if sl_result.get("success"):
                        ps.log(f"SL:{sl_price} TP:{tp_price} ustawione w MEXC")
                    else:
                        ps.log(f"Blad SL/TP MEXC: {sl_result.get('message')} — bot monitoruje", "WARN")
                else:
                    ps.log(f"Brak pozycji — bot monitoruje SL @ {sl_price}", "WARN")
            except Exception as e:
                ps.log(f"Blad SL MEXC: {e}", "WARN")

        ps.log(
            f"Poz.1/{len(active)} {side} | Cena:{exec_price} | Vol:{vol0} | "
            f"TP(mem):{tp_price} | SL:{sl_price} | Dokladki:{len(limit_order_ids)} ustawione",
            "SUCCESS"
        )
        tg(f"🚀 <b>{ps.symbol}</b> {side}\n"
           f"Cena: {exec_price}\n"
           f"TP: {tp_price} | SL: {sl_price}\n"
           f"Dokładki: {len(limit_order_ids)}")

    except Exception as e:
        ps.log(f"Blad wejscia: {e}", "ERROR")

def _check_pyramid_continuation(client, ps, price):
    """
    Dokładki sa teraz limit orders w MEXC - wchodza automatycznie.
    Ta funkcja tylko aktualizuje stan piramidy gdy MEXC wykona limit order.
    """
    # Sprawdz czy weszly nowe dokładki (holdVol wzrosl)
    try:
        positions = client.get_positions(ps.symbol)
        if not positions: return
        pos = positions[0]
        hold_vol = pos.get("holdVol", 0)

        # Policz oczekiwany vol po wszystkich wejsciach
        expected_entries = len(ps.pyramid_entries)
        active = [l for l in ps.pyramid_levels if l.get("enabled", True)]

        # Jesli holdVol wiekszy niz suma znanych wejsc - weszla dokładka
        known_vol = sum(e.vol for e in ps.pyramid_entries)
        if hold_vol > known_vol and expected_entries < len(active):
            new_vol = hold_vol - known_vol
            exec_price = float(pos.get("openAvgPrice", price))
            ps.pyramid_entries.append(PyramidEntry(exec_price, new_vol, ps.pyramid_side))
            ps.log(f"Dokladka {len(ps.pyramid_entries)}/{len(active)} wykryta @ {exec_price} | Vol:{new_vol}", "SUCCESS")
            tg(f"📌 <b>{ps.symbol}</b> Dokładka {len(ps.pyramid_entries)}/{len(active)}\n"
               f"Cena: {exec_price} | Vol: {new_vol}\n"
               f"TP: {ps.current_tp}")

            # Zaktualizuj TP od ostatniej dokładki
            last_price = ps.pyramid_entries[-1].price
            p_prec = {"BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4, "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3, "NAS100_USDT": 0, "SP500_USDT": 2, "BNB_USDT": 1, "XRP_USDT": 4, "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4, "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5, "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4, "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5, "BCH_USDT": 2}.get(ps.symbol, 4)
            ps.current_tp = _calc_tp(last_price, last_price,
                                     ps.pyramid_side, ps.tp_pct, ps.tp_mode, p_prec)
            ps.log(f"TP zaktualizowany: {ps.current_tp}")
    except Exception as e:
        ps.log(f"check_continuation blad: {e}", "ERROR")


def _open_hedge_level(client, ps, side, price):
    """Otwiera hedge pozycję (przeciwna strona)"""
    active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
    if not active: return
    lvl = active[0]
    try:
        ticker = client.get_ticker(ps.symbol)
        exec_price = float(ticker.get("lastPrice", price))
        contract_size = {"BTC_USDT": 0.0001, "ETH_USDT": 0.01, "SOL_USDT": 0.1, "SUI_USDT": 1.0, "DOGE_USDT": 100.0, "ADA_USDT": 1.0, "LINK_USDT": 0.1, "HYPE_USDT": 0.1, "NAS100_USDT": 0.00001, "SP500_USDT": 0.0001, "BNB_USDT": 0.01, "XRP_USDT": 1.0, "TRX_USDT": 10.0, "LTC_USDT": 0.01, "AVAX_USDT": 0.1, "ONDO_USDT": 10.0, "UNI_USDT": 0.1, "TAO_USDT": 0.01, "XAU_USDT": 0.001, "ARB_USDT": 1.0, "GALA_USDT": 10.0, "ATOM_USDT": 0.1, "DOT_USDT": 0.1, "ALGO_USDT": 1.0, "JUP_USDT": 10.0, "KAITO_USDT": 1.0, "PENGU_USDT": 10.0, "WLFI_USDT": 1.0, "BCH_USDT": 0.01}.get(ps.symbol, 1.0)
        price_prec = {"BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4, "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3, "NAS100_USDT": 0, "SP500_USDT": 2, "BNB_USDT": 1, "XRP_USDT": 4, "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4, "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5, "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4, "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5, "BCH_USDT": 2}.get(ps.symbol, 4)
        vol0 = max(1, round(lvl["amount_usd"] / (exec_price * contract_size / ps.leverage)))
        result = client.place_order(ps.symbol, 1 if side == "LONG" else 3, vol0, ps.leverage, None, None)
        if not result.get("success", False):
            ps.log(f"HEDGE odrzucony: {result.get('message','')}", "ERROR"); return
        ps.hedge_entries.append(PyramidEntry(exec_price, vol0, side))
        ps.hedge_active = True; ps.hedge_side = side
        limit_ids = []; last_dok_price = exec_price
        for i, dok_lvl in enumerate(active[1:], start=1):
            dok_price = round(last_dok_price * (1 - dok_lvl["offset_pct"]/100) if side == "LONG" else last_dok_price * (1 + dok_lvl["offset_pct"]/100), price_prec)
            dok_vol = max(1, round(dok_lvl["amount_usd"] / (dok_price * contract_size / ps.leverage)))
            dr = client._post("/api/v1/private/order/submit", {"symbol": ps.symbol, "side": 1 if side=="LONG" else 3, "openType": gstate.margin_mode, "type": 1, "vol": dok_vol, "leverage": ps.leverage, "price": dok_price})
            if dr.get("success"): limit_ids.append(dr.get("data")); last_dok_price = dok_price
            time.sleep(2.0)
        ps.hedge_limit_order_ids = limit_ids
        sl_price = _calc_sl(last_dok_price, side, ps.sl_pct, price_prec)
        tp_price = _calc_tp(exec_price, exec_price, side, ps.tp_pct, ps.tp_mode, price_prec)
        ps.hedge_current_tp = tp_price; ps.hedge_current_sl = sl_price
        ps.log(f"HEDGE {side} | Cena:{exec_price} | TP:{tp_price} | SL:{sl_price} | Dok:{len(limit_ids)}", "SUCCESS")
        tg(f"⚡ <b>{ps.symbol}</b> HEDGE {side}\nCena: {exec_price}\nTP: {tp_price} | SL: {sl_price}")
    except Exception as e:
        ps.log(f"HEDGE blad: {e}", "ERROR")


def _check_hedge_continuation(client, ps, price):
    """Sprawdza dokładki hedge pozycji"""
    try:
        positions = client.get_positions(ps.symbol)
        if not positions: return
        h_type = 1 if ps.hedge_side == "LONG" else 2
        h_pos = [p for p in positions if p.get("positionType") == h_type]
        if not h_pos: return
        pos = h_pos[0]; hold_vol = pos.get("holdVol", 0)
        active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
        known_vol = sum(e.vol for e in ps.hedge_entries)
        if hold_vol > known_vol and len(ps.hedge_entries) < len(active):
            new_vol = hold_vol - known_vol
            exec_price = float(pos.get("openAvgPrice", price))
            ps.hedge_entries.append(PyramidEntry(exec_price, new_vol, ps.hedge_side))
            ps.log(f"HEDGE Dokladka {len(ps.hedge_entries)}/{len(active)} @ {exec_price}", "SUCCESS")
            price_prec = {"BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4, "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3, "NAS100_USDT": 0, "SP500_USDT": 2, "BNB_USDT": 1, "XRP_USDT": 4, "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4, "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5, "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4, "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5, "BCH_USDT": 2}.get(ps.symbol, 4)
            last_price = ps.hedge_entries[-1].price
            ps.hedge_current_tp = _calc_tp(last_price, last_price, ps.hedge_side, ps.tp_pct, ps.tp_mode, price_prec)
            ps.log(f"HEDGE TP zaktualizowany: {ps.hedge_current_tp}")
    except Exception as e:
        ps.log(f"HEDGE check blad: {e}", "ERROR")


# ─── BOT LOOP ────────────────────────────────────────────────────────────────

async def pyramid_monitor_loop():
    """
    Monitor co 15 sekund:
    1. Sprawdza dokladki
    2. Sprawdza TP/SL z pamieci i zamyka pozycje jesli osiagniete
    3. Sprawdza czy pozycja nadal otwarta
    """
    while gstate.running:
        await asyncio.sleep(5)
        if not gstate.api_key or not gstate.running: continue
        try:
            client  = MEXCClient(gstate.api_key, gstate.api_secret)
            actives = [ps for ps in gstate.pairs.values()
                       if ps.enabled and (ps.pyramid_active or (gstate.hedging_enabled and ps.hedge_active))]
            for ps in actives:
                try:
                    ticker = client.get_ticker(ps.symbol)
                    price  = float(ticker.get("lastPrice", 0))
                    if price <= 0: continue
                    ps.last_price = price

                    # Sprawdz dokladki
                    _check_pyramid_continuation(client, ps, price)

                    # Sprawdz TP/SL z pamieci bota
                    if gstate.tp_sl_enabled and ps.current_tp and ps.current_sl and ps.pyramid_side:
                        tp_hit = (price >= ps.current_tp if ps.pyramid_side == "LONG"
                                  else price <= ps.current_tp)
                        sl_hit = (price <= ps.current_sl if ps.pyramid_side == "LONG"
                                  else price >= ps.current_sl)

                        if tp_hit or sl_hit:
                            reason = "TP" if tp_hit else "SL"
                            ps.log(f"{reason} osiagniety @ {price} "
                                   f"(TP:{ps.current_tp} SL:{ps.current_sl}) - zamykam",
                                   "SUCCESS")
                            tg(f"{'✅' if reason=='TP' else '❌'} <b>{ps.symbol}</b> {reason} osiągnięty\n"
                               f"Cena: {price}\n"
                               f"TP: {ps.current_tp} | SL: {ps.current_sl}")
                            all_pos = client.get_positions()
                            for pos in all_pos:
                                if pos.get("symbol") == ps.symbol:
                                    pos_type = pos.get("positionType", 1)
                                    hold_vol = pos.get("holdVol", 0)
                                    if hold_vol > 0:
                                        client.close_position(
                                            ps.symbol, pos_type, hold_vol, ps.leverage
                                        )
                            # Anuluj otwarte limit orders (dokładki)
                            try:
                                client._post("/api/v1/private/order/cancel_all", {"symbol": ps.symbol})
                                ps.log(f"Anulowano limit orders dla {ps.symbol}")
                            except Exception as e:
                                ps.log(f"Blad anulowania orders: {e}", "WARN")
                            ps.reset_pyramid()
                            ps.current_tp = None
                            ps.current_sl = None
                            continue

                    # Sprawdz czy pozycja nadal otwarta
                    all_pos = client.get_positions()
                    current = [p for p in all_pos if p.get("symbol") == ps.symbol]
                    if not current and ps.pyramid_active:
                        ps.log("Pozycja zamknieta (MEXC TP/SL) - resetuje piramide", "SUCCESS")
                        ps.reset_pyramid()
                        ps.current_tp = None
                        ps.current_sl = None
                    else:
                        ps.open_positions = current
                    # Hedge monitoring
                    if gstate.hedging_enabled and ps.hedge_active:
                        _check_hedge_continuation(client, ps, price)
                        if gstate.tp_sl_enabled and ps.hedge_current_tp and ps.hedge_current_sl:
                            h_tp = (price >= ps.hedge_current_tp if ps.hedge_side == "LONG" else price <= ps.hedge_current_tp)
                            h_sl = (price <= ps.hedge_current_sl if ps.hedge_side == "LONG" else price >= ps.hedge_current_sl)
                            if h_tp or h_sl:
                                reason = "TP" if h_tp else "SL"
                                ps.log(f"HEDGE {reason} @ {price} (TP:{ps.hedge_current_tp} SL:{ps.hedge_current_sl})", "SUCCESS")
                                tg(f"{'✅' if reason=='TP' else '❌'} <b>{ps.symbol}</b> HEDGE {reason}\nCena: {price}")
                                h_pos_type = 1 if ps.hedge_side == "LONG" else 2
                                for pos in all_pos:
                                    if pos.get("symbol") == ps.symbol and pos.get("positionType") == h_pos_type:
                                        client.close_position(ps.symbol, h_pos_type, pos.get("holdVol",0), ps.leverage)
                                try: client._post("/api/v1/private/order/cancel_all", {"symbol": ps.symbol})
                                except: pass
                                ps.reset_hedge(); ps.hedge_current_tp = None; ps.hedge_current_sl = None
                        else:
                            h_pos_type = 1 if ps.hedge_side == "LONG" else 2
                            h_current = [p for p in all_pos if p.get("symbol") == ps.symbol and p.get("positionType") == h_pos_type]
                            if not h_current:
                                ps.log("HEDGE pozycja zamknieta (MEXC) - resetuje", "SUCCESS")
                                ps.reset_hedge(); ps.hedge_current_tp = None; ps.hedge_current_sl = None
                except Exception as e:
                    ps.log(f"Monitor blad: {e}", "ERROR")
                await asyncio.sleep(0.3)
        except Exception as e:
            gstate.log(f"Pyramid monitor error: {e}", "ERROR")

async def bot_loop():
    iv_map = {"Min1":60,"Min5":300,"Min15":900,"Min30":1800,"Min60":3600,"Hour4":14400}
    while gstate.running:
        if not gstate.api_key:
            await asyncio.sleep(5); continue
        client  = MEXCClient(gstate.api_key, gstate.api_secret)
        actives = [ps for ps in gstate.pairs.values() if ps.enabled]
        if not actives:
            gstate.log("Brak aktywnych par", "WARN")
        else:
            for ps in actives:
                run_pair_strategy(client, ps)
                await asyncio.sleep(0.5)
        min_iv = min((iv_map.get(ps.interval,300) for ps in actives), default=300)
        gstate.log(f"Następne za {min_iv}s ({len(actives)} par)")
        try: save_logs()
        except: pass
        for _ in range(min_iv):
            if not gstate.running: break
            await asyncio.sleep(1)


# ─── MODELS ──────────────────────────────────────────────────────────────────

class LoginReq(BaseModel):
    password: str

class ChangePwReq(BaseModel):
    old_password: str
    new_password: str

class PyramidLevelIn(BaseModel):
    enabled: bool = True; amount_usd: float = 33.0; offset_pct: float = 0.0

class PairConfig(BaseModel):
    symbol: str; enabled: bool = True
    interval: str = "Min5"; direction: str = "BOTH"; entry_timing: str = "CLOSE"
    bb_period: int = 20; bb_std: float = 2.0; bb_proximity: float = 0.0; bb_breakout_pct: float = 0.0
    stoch_period: int = 14; stoch_smooth_k: int = 3; stoch_smooth_d: int = 3
    stoch_overbought: int = 80; stoch_oversold: int = 20
    stoch_window: int = 5
    ma200_enabled: bool = False; ma200_tf: str = "Min60"
    pyramid_levels: List[PyramidLevelIn] = []
    leverage: int = 10; tp_mode: str = "FROM_AVG"; tp_pct: float = 1.0; sl_pct: float = 1.5

class GlobalConfig(BaseModel):
    api_key: str; api_secret: str; pairs: List[PairConfig]


# ─── ENDPOINTS ───────────────────────────────────────────────────────────────

@app.post("/api/login")
def login(req: LoginReq):
    if not auth_manager.verify_password(req.password):
        raise HTTPException(401, "Nieprawidłowe hasło")
    return {"token": auth_manager.create_token()}

@app.post("/api/logout")
def logout(token: str = Depends(require_auth)):
    auth_manager.revoke_token(token); return {"ok": True}

@app.post("/api/change_password")
def change_pw(req: ChangePwReq, _=Depends(require_auth)):
    if not auth_manager.change_password(req.old_password, req.new_password):
        raise HTTPException(400, "Stare hasło nieprawidłowe")
    return {"ok": True}

@app.post("/api/config")
def set_config(cfg: GlobalConfig, _=Depends(require_auth)):
    gstate.api_key = cfg.api_key; gstate.api_secret = cfg.api_secret
    configured = {pc.symbol for pc in cfg.pairs}
    for sym in list(gstate.pairs.keys()):
        if sym not in configured: del gstate.pairs[sym]
    for pc in cfg.pairs:
        ps = gstate.get_or_create(pc.symbol)
        for k in ["enabled","interval","direction","entry_timing","bb_period","bb_std",
                  "bb_proximity","bb_breakout_pct","stoch_period","stoch_smooth_k","stoch_smooth_d",
                  "stoch_overbought","stoch_oversold","stoch_window","ma200_enabled","ma200_tf",
                  "leverage","tp_mode","tp_pct","sl_pct"]:
            setattr(ps, k, getattr(pc, k))
        if pc.pyramid_levels:
            ps.pyramid_levels = [l.dict() for l in pc.pyramid_levels]
    save_config()
    gstate.log(f"✅ Zapisano — {len(cfg.pairs)} par")
    return {"ok": True}

@app.get("/api/config")
def get_config(_=Depends(require_auth)):
    return {"api_key": gstate.api_key, "api_secret": gstate.api_secret,
            "pairs": [ps.to_dict() for ps in gstate.pairs.values()]}

@app.post("/api/start")
async def start_bot(_=Depends(require_auth)):
    global _bot_task
    if gstate.running: return {"ok": False, "msg": "Bot już działa"}
    if not gstate.api_key: raise HTTPException(400, "Brak API")
    gstate.running = True; gstate.log("🚀 Bot uruchomiony")
    _bot_task = asyncio.create_task(bot_loop())
    asyncio.create_task(pyramid_monitor_loop())
    return {"ok": True}

@app.post("/api/stop")
async def stop_bot(_=Depends(require_auth)):
    gstate.running = False; gstate.log("🛑 Bot zatrzymany")
    return {"ok": True}

@app.post("/api/margin_mode/{val}")
async def set_margin_mode(val: int, _=Depends(require_auth)):
    gstate.margin_mode = 1 if val == 1 else 2
    mode = "Isolated" if gstate.margin_mode == 1 else "Cross"
    save_config()
    gstate.log(f"Tryb marginu: {mode}")
    return {"ok": True, "margin_mode": gstate.margin_mode}

@app.post("/api/max_positions/{val}")
async def set_max_positions(val: int, _=Depends(require_auth)):
    gstate.max_positions = max(1, val)
    save_config()
    gstate.log(f"Maks. pozycji: {gstate.max_positions}")
    return {"ok": True, "max_positions": gstate.max_positions}

@app.post("/api/tp_sl/{enabled}")
async def set_tp_sl(enabled: int, _=Depends(require_auth)):
    gstate.tp_sl_enabled = bool(enabled)
    mode = "✅ TP/SL włączone" if gstate.tp_sl_enabled else "⛔ TP/SL wyłączone"
    save_config(); gstate.log(mode); tg(mode)
    return {"ok": True, "tp_sl_enabled": gstate.tp_sl_enabled}

@app.post("/api/hedging/{enabled}")
async def set_hedging(enabled: int, _=Depends(require_auth)):
    gstate.hedging_enabled = bool(enabled)
    mode = "⚡ Hedging włączony" if gstate.hedging_enabled else "⚡ Hedging wyłączony"
    save_config(); gstate.log(mode)
    return {"ok": True, "hedging_enabled": gstate.hedging_enabled}

@app.post("/api/signals_only/{enabled}")
async def set_signals_only(enabled: int, _=Depends(require_auth)):
    gstate.signals_only = bool(enabled)
    mode = "📡 Tryb sygnałów" if gstate.signals_only else "🚀 Tryb tradingu"
    gstate.log(f"{mode} aktywny")
    tg(f"{mode} aktywny")
    save_config()
    return {"ok": True, "signals_only": gstate.signals_only}

@app.get("/api/status")
def get_status(_=Depends(require_auth)):
    return {"running": gstate.running,
            "signals_only": gstate.signals_only,
            "max_positions": gstate.max_positions,
            "margin_mode": gstate.margin_mode,
            "tp_sl_enabled": gstate.tp_sl_enabled,
            "hedging_enabled": gstate.hedging_enabled,
            "pairs": [ps.to_dict() for ps in gstate.pairs.values()],
            "global_logs": gstate.global_logs[:20]}

@app.post("/api/test/{symbol}")
def test_pair(symbol: str, _=Depends(require_auth)):
    if not gstate.api_key: raise HTTPException(400, "Brak API")
    ps = gstate.get_or_create(symbol)
    run_pair_strategy(MEXCClient(gstate.api_key, gstate.api_secret), ps)
    return {"ok": True}

@app.post("/api/reset_pyramid/{symbol}")
def reset_pyramid(symbol: str, _=Depends(require_auth)):
    if symbol in gstate.pairs:
        gstate.pairs[symbol].reset_pyramid()
        gstate.pairs[symbol].log("🔄 Piramida zresetowana")
    return {"ok": True}

@app.delete("/api/pair/{symbol}")
def remove_pair(symbol: str, _=Depends(require_auth)):
    if symbol in gstate.pairs:
        del gstate.pairs[symbol]
        save_config()
        gstate.log(f"Usunięto parę {symbol}")
    return {"ok": True}

@app.get("/api/balance")
def get_balance(_=Depends(require_auth)):
    if not gstate.api_key: return {"balance": []}
    try:
        client = MEXCClient(gstate.api_key, gstate.api_secret)
        data = client._get("/api/v1/private/account/assets")
        assets = data.get("data", [])
        result = []
        for a in assets:
            equity = float(a.get("equity", 0))
            if equity > 0:
                result.append({
                    "currency": a.get("currency", ""),
                    "equity": round(equity, 4),
                    "available": round(float(a.get("availableBalance", 0)), 4),
                    "position_margin": round(float(a.get("positionMargin", 0)), 4),
                    "unrealized_pnl": round(float(a.get("unrealizedProfit", 0)), 4),
                })
        return {"balance": result}
    except Exception as e:
        return {"balance": [], "error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
