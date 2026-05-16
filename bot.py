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
        body = {"symbol": symbol, "side": side, "openType": 1,
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
        body = {"symbol": symbol, "side": close_side, "openType": 1,
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
        """Anuluj wszystkie zlecenia TP/SL dla symbolu"""
        try:
            result = self._post("/api/v1/private/planorder/cancel/tpsl/all",
                                {"symbol": symbol})
            return result.get("success", False)
        except Exception as e:
            logger.error(f"cancel_all_tpsl error: {e}")
            return False

    def update_position_tp_sl(self, symbol: str, side: str,
                               tp, sl, leverage: int) -> bool:
        """Ustaw TP/SL na pozycji przez dedykowany endpoint"""
        try:
            # Najpierw anuluj stare zlecenia TP/SL
            self.cancel_all_tpsl_orders(symbol)
            # Postaw nowe TP/SL przez place_tpsl endpoint
            pos_type = 1 if side == "LONG" else 2
            body = {"symbol": symbol, "positionType": pos_type}
            if tp: body["takeProfitPrice"] = tp
            if sl: body["stopLossPrice"]   = sl
            result = self._post("/api/v1/private/planorder/place/tpsl", body)
            return result.get("success", False)
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
        self.ma200_enabled = False; self.ma200_tf = "Hour1"
        self.pyramid_levels = [
            {"enabled": True,  "amount_usd": 33.0, "offset_pct": 0.0},
            {"enabled": True,  "amount_usd": 33.0, "offset_pct": 1.0},
            {"enabled": False, "amount_usd": 33.0, "offset_pct": 1.0},
        ]
        self.leverage = 10; self.tp_mode = "FROM_AVG"
        self.tp_pct = 1.0; self.sl_pct = 1.5
        self.last_price = self.last_bb_upper = self.last_bb_lower = None
        self.last_bb_mid = self.last_stoch_k = self.last_stoch_d = None
        self.last_ma200 = None; self.last_signal = "NONE"; self.last_check = None
        self.divergence_bull = False; self.divergence_bear = False
        self.pyramid_entries: List[PyramidEntry] = []
        self.pyramid_active = False; self.pyramid_side = None
        self.current_tp: Optional[float] = None
        self.current_sl: Optional[float] = None
        self.open_positions = []; self.logs: List[dict] = []

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
                    "ma200_enabled","ma200_tf","pyramid_levels",
                    "leverage","tp_mode","tp_pct","sl_pct"
                ]
            })
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
        for pd in data.get("pairs", []):
            sym = pd.get("symbol"); 
            if not sym: continue
            ps = gstate.get_or_create(sym)
            for k in ["enabled","interval","direction","entry_timing",
                      "bb_period","bb_std","bb_proximity","bb_breakout_pct","stoch_period",
                      "stoch_smooth_k","stoch_smooth_d","stoch_overbought","stoch_oversold",
                      "ma200_enabled","ma200_tf","pyramid_levels",
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
        near_lower = price < (lower - breakout_margin + dev * ps.bb_proximity)
        near_upper = price > (upper + breakout_margin - dev * ps.bb_proximity)

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

        # Crossover / Crossunder - sprawdz ostatnie 3 swiece
        k_cross_over  = any(
            k_series[-(i+2)] <= d_series[-(i+2)] and k_series[-(i+1)] > d_series[-(i+1)]
            for i in range(3) if len(k_series) > i+2 and len(d_series) > i+2
        ) and k_now < ps.stoch_oversold
        k_cross_under = any(
            k_series[-(i+2)] >= d_series[-(i+2)] and k_series[-(i+1)] < d_series[-(i+1)]
            for i in range(3) if len(k_series) > i+2 and len(d_series) > i+2
        ) and k_now > ps.stoch_overbought

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
        long_ok  = (k_cross_over  and k_now < ps.stoch_oversold   and near_lower
                    and ma200_ok_long  and ps.direction in ("LONG",  "BOTH"))
        short_ok = (k_cross_under and k_now > ps.stoch_overbought and near_upper
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
            _open_pyramid_level(client, ps, signal, price, 0)
        elif ps.pyramid_active and ps.pyramid_side == signal:
            _check_pyramid_continuation(client, ps, price)

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


def _calc_sl(exec_price: float, side: str, sl_pct: float) -> float:
    """SL od podanej ceny wejścia"""
    if side == "LONG":
        return round(exec_price * (1 - sl_pct / 100), 4)
    else:
        return round(exec_price * (1 + sl_pct / 100), 4)

def _calc_tp(avg_price: float, first_price: float, side: str,
             tp_pct: float, tp_mode: str) -> float:
    """TP od średniej lub pierwszego wejścia"""
    base = avg_price if tp_mode == "FROM_AVG" else first_price
    if side == "LONG":
        return round(base * (1 + tp_pct / 100), 4)
    else:
        return round(base * (1 - tp_pct / 100), 4)

def _open_pyramid_level(client, ps, side, price, level_idx):
    """
    Opcja C: SL od bieżącego (najdalszego) wejścia, aktualizowany po każdej dokładce.
    Dokładki wchodzą gdy cena idzie PRZECIWKO pozycji.
    """
    active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
    if level_idx >= len(active): return
    lvl = active[level_idx]
    try:
        client.set_leverage(ps.symbol, ps.leverage)
        ticker     = client.get_ticker(ps.symbol)
        exec_price = float(ticker.get("lastPrice", price))

        # Wolumen jako liczba całkowita kontraktów
        contract_size = {"BTC_USDT": 0.0001, "ETH_USDT": 0.01}.get(ps.symbol, 1.0)
        raw_vol = lvl["amount_usd"] / (exec_price * contract_size / ps.leverage)
        vol = max(1, round(raw_vol))

        # SL od BIEŻĄCEGO wejścia (najdalszego od pierwszego)
        # Opcja C: SL aktualizowany po każdej dokładce
        sl = _calc_sl(exec_price, side, ps.sl_pct)

        # Oblicz TP przed zleceniem
        avg_tmp = (sum(e.price*e.vol for e in ps.pyramid_entries) + exec_price*vol) /                   (sum(e.vol for e in ps.pyramid_entries) + vol) if ps.pyramid_entries else exec_price
        base_tmp = avg_tmp if ps.tp_mode == "FROM_AVG" else (ps.pyramid_entries[0].price if ps.pyramid_entries else exec_price)
        tp = (round(base_tmp*(1+ps.tp_pct/100),4) if side=="LONG"
              else round(base_tmp*(1-ps.tp_pct/100),4))
        result = client.place_order(ps.symbol, 1 if side == "LONG" else 3,
                                    vol, ps.leverage, sl, tp)

        if not result.get("success", False):
            ps.log(f"❌ MEXC odrzucił zlecenie: {result.get('message','')}", "ERROR")
            return

        ps.pyramid_entries.append(PyramidEntry(exec_price, vol, side))
        if level_idx == 0:
            ps.pyramid_active = True
            ps.pyramid_side   = side
        # SL od ostatniego wejscia, TP od sredniej wazonej
        avg = ps.pyramid_avg_entry
        if avg:
            ps.current_sl = _calc_sl(exec_price, side, ps.sl_pct)
            ps.current_tp = _calc_tp(avg, ps.pyramid_entries[0].price, side, ps.tp_pct, ps.tp_mode)
        else:
            ps.current_sl = _calc_sl(exec_price, side, ps.sl_pct)
            ps.current_tp = _calc_tp(exec_price, exec_price, side, ps.tp_pct, ps.tp_mode)
        # Aktualizuj TP/SL na calej pozycji w MEXC
        if level_idx > 0 and ps.current_tp and ps.current_sl:
            try:
                client.cancel_all_orders(ps.symbol)
                updated = client.update_position_tp_sl(
                    ps.symbol, side, ps.current_tp, ps.current_sl, ps.leverage
                )
                if updated:
                    ps.log(f"TP/SL od sr.{round(avg,2) if avg else exec_price}: TP:{ps.current_tp} SL:{ps.current_sl}")
                else:
                    ps.log("Nie udalo sie zaktualizowac TP/SL", "WARN")
            except Exception as e:
                ps.log(f"Blad aktualizacji TP/SL: {e}", "WARN")

        # Info o następnej dokładce
        is_last = level_idx == len(active) - 1
        if not is_last:
            next_lvl    = active[level_idx + 1]
            next_offset = next_lvl["offset_pct"]
            next_amt    = next_lvl["amount_usd"]
            if side == "LONG":
                next_price = round(exec_price * (1 - next_offset / 100), 4)
            else:
                next_price = round(exec_price * (1 + next_offset / 100), 4)
            next_info = f" | Dok.{level_idx+2} @ {next_price} ({next_offset}%, {next_amt}$)"
        else:
            next_info = " | Ostatnia dokładka"

        ps.log(
            f"📌 Poz.{level_idx+1}/{len(active)} {side} | Cena:{exec_price} | "
            f"Vol:{vol} | SL:{sl} | TP:{ps.current_tp}{next_info}",
            "SUCCESS"
        )

    except Exception as e:
        ps.log(f"❌ Poz.{level_idx+1}: {e}", "ERROR")


def _check_pyramid_continuation(client, ps, price):
    """
    Sprawdź czy należy otworzyć kolejną dokładkę.
    Dokładka wchodzi gdy cena idzie PRZECIWKO pozycji o offset_pct od ostatniego wejścia.
    LONG: cena spada o offset% od ostatniego wejścia
    SHORT: cena rośnie o offset% od ostatniego wejścia
    """
    active   = [l for l in ps.pyramid_levels if l.get("enabled", True)]
    next_idx = ps.pyramid_count
    if next_idx >= len(active): return

    lvl = active[next_idx]
    lp  = ps.pyramid_last_price
    if not lp or lvl.get("offset_pct", 0) <= 0: return

    if ps.pyramid_side == "LONG":
        # Dokładka gdy cena spada (przeciwko long)
        target    = round(lp * (1 - lvl["offset_pct"] / 100), 4)
        triggered = price <= target
    else:
        # Dokładka gdy cena rośnie (przeciwko short)
        target    = round(lp * (1 + lvl["offset_pct"] / 100), 4)
        triggered = price >= target

    if triggered:
        ps.log(
            f"🔄 Dokładka {next_idx+1} wyzwolona @ {price} "
            f"(target:{target}, offset:{lvl['offset_pct']}%)",
            "SIGNAL"
        )
        _open_pyramid_level(client, ps, ps.pyramid_side, price, next_idx)


# ─── BOT LOOP ────────────────────────────────────────────────────────────────

async def pyramid_monitor_loop():
    """Osobna petla sprawdzajaca dokladki co 30 sekund."""
    while gstate.running:
        await asyncio.sleep(30)
        if not gstate.api_key or not gstate.running: continue
        try:
            client  = MEXCClient(gstate.api_key, gstate.api_secret)
            actives = [ps for ps in gstate.pairs.values()
                       if ps.enabled and ps.pyramid_active]
            for ps in actives:
                try:
                    ticker = client.get_ticker(ps.symbol)
                    price  = float(ticker.get("lastPrice", 0))
                    if price <= 0: continue
                    ps.last_price = price
                    _check_pyramid_continuation(client, ps, price)
                    all_pos = client.get_positions()
                    current = [p for p in all_pos if p.get("symbol") == ps.symbol]
                    if not current and ps.pyramid_active:
                        ps.log("Pozycja zamknieta (TP/SL) - resetuje piramide", "SUCCESS")
                        ps.reset_pyramid()
                        ps.current_tp = None
                        ps.current_sl = None
                    else:
                        ps.open_positions = current
                except Exception as e:
                    ps.log(f"Monitor dokladek blad: {e}", "ERROR")
                await asyncio.sleep(0.3)
        except Exception as e:
            gstate.log(f"Pyramid monitor error: {e}", "ERROR")


async def bot_loop():
    iv_map = {"Min1":60,"Min5":300,"Min15":900,"Min30":1800,"Hour1":3600,"Hour4":14400}
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
    ma200_enabled: bool = False; ma200_tf: str = "Hour1"
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
                  "stoch_overbought","stoch_oversold","ma200_enabled","ma200_tf",
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

@app.get("/api/status")
def get_status(_=Depends(require_auth)):
    return {"running": gstate.running,
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
