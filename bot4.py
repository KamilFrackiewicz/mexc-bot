"""
MEXC Futures Trading Bot v4 - Strategia ORB
Opening Range Breakout - otwarcie sesji NY (15:30 CET)
"""

import hmac, hashlib, time, requests, numpy as np, asyncio, logging, json, os
from typing import Optional, List, Dict
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from datetime import datetime, timedelta, time as dtime
import uvicorn
import secrets

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="MEXC Futures Bot ORB")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MEXC_BASE   = "https://api.mexc.com"
CONFIG_FILE = "config4.json"
AUTH_FILE   = "auth.json"
LOG_FILE    = "logs4.json"
security    = HTTPBearer(auto_error=False)

TELEGRAM_TOKEN   = "8828080533:AAFrSbgunu3LJ8IhKMmjnvesNl6rD5j9tKk"
TELEGRAM_CHAT_ID = "5137354808"

# ─── CZAS SESJI NY ────────────────────────────────────────────────────────────
# Otwarcie NY: 15:30 CET (lato) / 16:30 CET (zima)
# Serwer w Europe/Warsaw więc używamy lokalnego czasu

def get_ny_open_time(interval_minutes: int) -> tuple:
    """Zwraca (start, end) okna ORB w lokalnym czasie serwera"""
    now = datetime.now()
    # Sprawdz czy jest DST (lato: +2, zima: +1)
    import time as _time
    is_dst = bool(_time.daylight) and _time.localtime().tm_isdst
    # NY otwiera o 9:30 ET = 15:30 CET (lato) = 16:30 CET (zima)
    if is_dst:
        ny_open_hour, ny_open_min = 15, 30
    else:
        ny_open_hour, ny_open_min = 16, 30
    orb_start = now.replace(hour=ny_open_hour, minute=ny_open_min, second=0, microsecond=0)
    orb_end   = orb_start + timedelta(minutes=interval_minutes)
    return orb_start, orb_end

def is_orb_candle_closed(interval_minutes: int) -> bool:
    """Czy pierwsza świeca ORB już się zamknęła?"""
    orb_start, orb_end = get_ny_open_time(interval_minutes)
    now = datetime.now()
    return now >= orb_end

def is_trading_window(interval_minutes: int) -> bool:
    """Czy jesteśmy w oknie tradingowym (po zamknięciu pierwszej świecy, max 2h po otwarciu)"""
    orb_start, orb_end = get_ny_open_time(interval_minutes)
    now = datetime.now()
    trading_end = orb_start + timedelta(hours=2)
    return orb_end <= now <= trading_end

def tg(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ─── AUTH ─────────────────────────────────────────────────────────────────────
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

# ─── MEXC CLIENT ──────────────────────────────────────────────────────────────
class MEXCClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
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
        d = self._get(f"/api/v1/contract/kline/{symbol}", {"interval": interval, "limit": limit})
        return d.get("data", {})

    def get_ticker(self, symbol: str) -> dict:
        d = self._get("/api/v1/contract/ticker", {"symbol": symbol})
        return d.get("data", {})

    def set_leverage(self, symbol: str, leverage: int):
        try:
            self._post("/api/v1/private/position/change_leverage",
                       {"symbol": symbol, "leverage": leverage, "openType": 1, "positionType": 1})
        except: pass

    def place_order(self, symbol: str, side: int, vol: float, leverage: int) -> dict:
        body = {"symbol": symbol, "side": side, "openType": gstate.margin_mode,
                "type": 5, "vol": vol, "leverage": leverage}
        return self._post("/api/v1/private/order/submit", body)

    def get_positions(self, symbol: str = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        d = self._get("/api/v1/private/position/open_positions", params)
        return d.get("data", [])

    def close_position(self, symbol: str, pos_type: int, vol: int, leverage: int) -> dict:
        close_side = 4 if pos_type == 1 else 2
        body = {"symbol": symbol, "side": close_side, "openType": gstate.margin_mode,
                "type": 5, "vol": vol, "leverage": leverage}
        return self._post("/api/v1/private/order/submit", body)

# ─── INDICATORS ───────────────────────────────────────────────────────────────
def calc_ma200(closes, period=200):
    if len(closes) < period: return None
    return float(np.mean(closes[-period:]))

# ─── PRICE PRECISION ──────────────────────────────────────────────────────────
PRICE_PREC = {
    "BTC_USDT": 1, "ETH_USDT": 2, "SOL_USDT": 2, "SUI_USDT": 4,
    "DOGE_USDT": 5, "ADA_USDT": 4, "LINK_USDT": 3, "HYPE_USDT": 3,
    "NAS100_USDT": 0, "SP500_USDT": 2, "BNB_USDT": 1, "XRP_USDT": 4,
    "TRX_USDT": 5, "LTC_USDT": 2, "AVAX_USDT": 3, "ONDO_USDT": 4,
    "UNI_USDT": 3, "TAO_USDT": 2, "XAU_USDT": 2, "ARB_USDT": 5,
    "GALA_USDT": 6, "ATOM_USDT": 3, "DOT_USDT": 3, "ALGO_USDT": 4,
    "JUP_USDT": 4, "KAITO_USDT": 4, "PENGU_USDT": 6, "WLFI_USDT": 5,
    "BCH_USDT": 2
}
CONTRACT_SIZE = {
    "BTC_USDT": 0.0001, "ETH_USDT": 0.01, "SOL_USDT": 0.1, "SUI_USDT": 1.0,
    "DOGE_USDT": 100.0, "ADA_USDT": 1.0, "LINK_USDT": 0.1, "HYPE_USDT": 0.1,
    "NAS100_USDT": 0.00001, "SP500_USDT": 0.0001, "BNB_USDT": 0.01,
    "XRP_USDT": 1.0, "TRX_USDT": 10.0, "LTC_USDT": 0.01, "AVAX_USDT": 0.1,
    "ONDO_USDT": 10.0, "UNI_USDT": 0.1, "TAO_USDT": 0.01, "XAU_USDT": 0.001,
    "ARB_USDT": 1.0, "GALA_USDT": 10.0, "ATOM_USDT": 0.1, "DOT_USDT": 0.1,
    "ALGO_USDT": 1.0, "JUP_USDT": 10.0, "KAITO_USDT": 1.0, "PENGU_USDT": 10.0,
    "WLFI_USDT": 1.0, "BCH_USDT": 0.01
}

def _prec(symbol): return PRICE_PREC.get(symbol, 4)
def _csize(symbol): return CONTRACT_SIZE.get(symbol, 1.0)

def _calc_sl(price, side, sl_pct, prec):
    if side == "LONG": return round(price * (1 - sl_pct / 100), prec)
    else: return round(price * (1 + sl_pct / 100), prec)

def _calc_tp(price, side, tp_pct, prec):
    if side == "LONG": return round(price * (1 + tp_pct / 100), prec)
    else: return round(price * (1 - tp_pct / 100), prec)

# ─── PER-PAIR STATE ───────────────────────────────────────────────────────────
class PyramidEntry:
    def __init__(self, price, vol, side):
        self.price = price; self.vol = vol; self.side = side
        self.time  = datetime.now().strftime("%H:%M:%S")

class PairState:
    def __init__(self, symbol):
        self.symbol = symbol; self.enabled = True
        self.interval = "Min5"; self.direction = "BOTH"
        self.ma200_enabled = False; self.ma200_tf = "Hour4"
        self.pyramid_levels = [
            {"enabled": True,  "amount_usd": 5.0,  "offset_pct": 0.0},
            {"enabled": False, "amount_usd": 5.0,  "offset_pct": 0.5},
            {"enabled": False, "amount_usd": 10.0, "offset_pct": 0.5},
            {"enabled": False, "amount_usd": 20.0, "offset_pct": 0.5},
        ]
        self.leverage = 10
        self.tp_pct = 1.0; self.sl_pct = 0.5
        # ORB state
        self.orb_high: Optional[float] = None
        self.orb_low:  Optional[float] = None
        self.orb_date: Optional[str]   = None
        self.orb_set:  bool = False
        # Wskazniki
        self.last_price = None; self.last_check = None
        self.last_ma200 = None
        self.last_signal = "NONE"
        # Piramida
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
        if len(self.logs) % 10 == 0:
            try: save_logs()
            except: pass

    def reset_orb(self):
        self.orb_high = None; self.orb_low = None
        self.orb_set = False

    @property
    def pyramid_avg_entry(self):
        if not self.pyramid_entries: return None
        tv = sum(e.vol for e in self.pyramid_entries)
        return sum(e.price * e.vol for e in self.pyramid_entries) / tv if tv else None

    @property
    def pyramid_count(self): return len(self.pyramid_entries)

    def reset_pyramid(self):
        self.pyramid_entries = []; self.pyramid_active = False
        self.pyramid_side = None; self.pyramid_limit_order_ids = []

    def reset_hedge(self):
        self.hedge_entries = []; self.hedge_active = False
        self.hedge_side = None; self.hedge_limit_order_ids = []

    def to_dict(self):
        return {
            "symbol": self.symbol, "enabled": self.enabled,
            "interval": self.interval, "direction": self.direction,
            "ma200_enabled": self.ma200_enabled, "ma200_tf": self.ma200_tf,
            "pyramid_levels": self.pyramid_levels,
            "leverage": self.leverage,
            "tp_pct": self.tp_pct, "sl_pct": self.sl_pct,
            "orb_high": self.orb_high, "orb_low": self.orb_low,
            "orb_set": self.orb_set, "orb_date": self.orb_date,
            "last_price": self.last_price, "last_check": self.last_check,
            "last_ma200": self.last_ma200,
            "last_signal": self.last_signal,
            "pyramid": {
                "active": self.pyramid_active, "side": self.pyramid_side,
                "count": self.pyramid_count, "avg_entry": self.pyramid_avg_entry,
                "entries": [{"price": e.price, "vol": e.vol,
                             "side": e.side, "time": e.time}
                            for e in self.pyramid_entries],
            },
            "current_tp": self.current_tp, "current_sl": self.current_sl,
            "open_positions": self.open_positions,
            "logs": self.logs[:40],
        }

# ─── GLOBAL STATE ─────────────────────────────────────────────────────────────
class GlobalState:
    def __init__(self):
        self.running = False; self.api_key = ""; self.api_secret = ""
        self.signals_only = False
        self.max_positions = 1
        self.margin_mode = 1
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

# ─── CONFIG ───────────────────────────────────────────────────────────────────
def save_config():
    try:
        data = {"api_key": gstate.api_key, "api_secret": gstate.api_secret, "pairs": []}
        for ps in gstate.pairs.values():
            data["pairs"].append({k: getattr(ps, k) for k in [
                "symbol","enabled","interval","direction",
                "ma200_enabled","ma200_tf","pyramid_levels",
                "leverage","tp_pct","sl_pct"
            ]})
        data["signals_only"] = gstate.signals_only
        data["max_positions"] = gstate.max_positions
        data["margin_mode"] = gstate.margin_mode
        data["tp_sl_enabled"] = gstate.tp_sl_enabled
        data["hedging_enabled"] = gstate.hedging_enabled
        json.dump(data, open(CONFIG_FILE, "w"), indent=2)
    except Exception as e:
        logger.error(f"Save error: {e}")

def load_config():
    if not os.path.exists(CONFIG_FILE): return
    try:
        data = json.load(open(CONFIG_FILE))
        gstate.api_key    = data.get("api_key", "")
        gstate.api_secret = data.get("api_secret", "")
        gstate.signals_only   = data.get("signals_only", False)
        gstate.max_positions  = data.get("max_positions", 1)
        gstate.margin_mode    = data.get("margin_mode", 1)
        gstate.tp_sl_enabled  = data.get("tp_sl_enabled", True)
        gstate.hedging_enabled = data.get("hedging_enabled", False)
        for pd in data.get("pairs", []):
            sym = pd.get("symbol")
            if not sym: continue
            ps = gstate.get_or_create(sym)
            for k in ["enabled","interval","direction",
                      "ma200_enabled","ma200_tf","pyramid_levels",
                      "leverage","tp_pct","sl_pct"]:
                if k in pd: setattr(ps, k, pd[k])
        logger.info(f"✅ Config loaded — {len(gstate.pairs)} pairs")
    except Exception as e:
        logger.error(f"Load error: {e}")

load_config()

def save_logs():
    try:
        data = {sym: ps.logs[:200] for sym, ps in gstate.pairs.items()}
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
    except: pass

load_logs()

# ─── STRATEGY ─────────────────────────────────────────────────────────────────
def run_pair_strategy(client: MEXCClient, ps: PairState):
    try:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        iv_minutes = {"Min1":1,"Min5":5,"Min15":15}.get(ps.interval, 5)
        orb_start, orb_end = get_ny_open_time(iv_minutes)

        # ── Sprawdz czy to nowy dzien — reset ORB ─────────────────────────────
        if ps.orb_date != today:
            ps.reset_orb()
            ps.orb_date = today
            ps.log(f"Nowy dzien — reset ORB ({today})")

        # ── Krok 1: Wyznacz poziomy ORB ───────────────────────────────────────
        if not ps.orb_set and now >= orb_end:
            kdata  = client.get_klines_full(ps.symbol, ps.interval, 10)
            closes = [float(x) for x in kdata.get("close", [])]
            highs  = [float(x) for x in kdata.get("high",  [])]
            lows   = [float(x) for x in kdata.get("low",   [])]
            times  = kdata.get("time", [])

            if not closes:
                ps.log("Brak danych swiec", "WARN"); return

            # Znajdz swice ORB — pierwsza swica po 15:30
            orb_ts = int(orb_start.timestamp() * 1000)
            orb_candle_idx = None
            for i, t in enumerate(times):
                if int(t) >= orb_ts:
                    orb_candle_idx = i
                    break

            if orb_candle_idx is None:
                ps.log(f"Czekam na swiece ORB (15:{30 if now.dst() else 30})", "INFO")
                return

            ps.orb_high = highs[orb_candle_idx]
            ps.orb_low  = lows[orb_candle_idx]
            ps.orb_set  = True
            prec = _prec(ps.symbol)
            ps.log(f"ORB wyznaczony: H:{round(ps.orb_high,prec)} L:{round(ps.orb_low,prec)}", "SUCCESS")
            tg(f"📐 <b>{ps.symbol}</b> [ORB] Poziomy wyznaczone\nHigh: {round(ps.orb_high,prec)}\nLow: {round(ps.orb_low,prec)}")
            return

        # ── Krok 2: Czekaj na sygnał breakout ────────────────────────────────
        if not ps.orb_set:
            time_to_open = (orb_end - now).total_seconds()
            if time_to_open > 0:
                ps.log(f"Czekam na otwarcie NY za {int(time_to_open)}s")
            return

        if not is_trading_window(iv_minutes):
            ps.last_signal = "NONE"
            ps.log("Poza oknem tradingowym (max 2h po otwarciu)")
            return

        # Pobierz aktualne dane
        kdata  = client.get_klines_full(ps.symbol, ps.interval, 5)
        closes = [float(x) for x in kdata.get("close", [])]
        highs  = [float(x) for x in kdata.get("high",  [])]
        lows   = [float(x) for x in kdata.get("low",   [])]

        if len(closes) < 2:
            ps.log("Za malo danych", "WARN"); return

        price = closes[-1]
        ps.last_price = price
        ps.last_check = datetime.now().strftime("%H:%M:%S")

        # MA200 filtr
        ma200_ok_long = ma200_ok_short = True
        if ps.ma200_enabled:
            ma_data   = client.get_klines_full(ps.symbol, ps.ma200_tf, 210)
            ma_closes = [float(x) for x in ma_data.get("close", [])]
            ma200     = calc_ma200(ma_closes)
            ps.last_ma200 = round(ma200, 4) if ma200 else None
            if ma200:
                ma200_ok_long  = price > ma200
                ma200_ok_short = price < ma200
        else:
            ps.last_ma200 = None

        # Sygnał breakout
        signal = "WAIT"
        prec = _prec(ps.symbol)

        # Ostatnia zamknieta swica przebila ORB
        last_close = closes[-1]
        prev_close = closes[-2]

        long_ok  = (prev_close <= ps.orb_high and last_close > ps.orb_high and
                    ma200_ok_long and ps.direction in ("LONG", "BOTH"))
        short_ok = (prev_close >= ps.orb_low and last_close < ps.orb_low and
                    ma200_ok_short and ps.direction in ("SHORT", "BOTH"))

        if long_ok:    signal = "LONG"
        elif short_ok: signal = "SHORT"
        ps.last_signal = signal

        ma_str = f" MA200:{round(ps.last_ma200,4) if ps.last_ma200 else 'off'}"
        ps.log(f"P:{price} ORB H:{round(ps.orb_high,prec)} L:{round(ps.orb_low,prec)}"
               f" prev:{round(prev_close,prec)} close:{round(last_close,prec)}{ma_str} -> {signal}")

        if signal in ("LONG","SHORT") and not ps.pyramid_active:
            active_count = sum(1 for p in gstate.pairs.values() if p.pyramid_active)
            if active_count >= gstate.max_positions:
                ps.log(f"Blokada: {active_count}/{gstate.max_positions} pozycji", "WARN")
            elif gstate.signals_only:
                ps.log(f"📡 SYGNAŁ {signal} @ {price}")
                iv_label = {"Min1":"1min","Min5":"5min","Min15":"15min"}.get(ps.interval, ps.interval)
                tg(f"📡 <b>{ps.symbol}</b> [ORB] SYGNAŁ {signal}\n"
                   f"🕐 {datetime.now().strftime('%H:%M:%S')} ({iv_label})\n"
                   f"Cena: {price}\n"
                   f"ORB High: {round(ps.orb_high,prec)} | Low: {round(ps.orb_low,prec)}\n"
                   f"MA200: {round(ps.last_ma200,4) if ps.last_ma200 else 'off'}")
            else:
                _open_position(client, ps, signal, price)
        elif gstate.hedging_enabled and signal in ("LONG","SHORT") and ps.pyramid_active and ps.pyramid_side != signal and not ps.hedge_active:
            _open_hedge(client, ps, signal, price)
        elif ps.pyramid_active and ps.pyramid_side == signal:
            _check_continuation(client, ps, price)

    except Exception as e:
        ps.log(f"Blad: {e}", "ERROR")

    try:
        all_pos = client.get_positions()
        current = [p for p in all_pos if p.get("symbol") == ps.symbol]
        if ps.pyramid_active and not current:
            ps.log("Pozycja zamknieta (MEXC) - reset", "SUCCESS")
            ps.reset_pyramid(); ps.current_tp = None; ps.current_sl = None
        for p in current:
            try:
                pnl = float(p.get("unrealizedProfit", p.get("unrealizedValue", 0)))
                p["unrealizedValue"] = round(pnl, 4)
            except: pass
        ps.open_positions = current
    except: pass

def _open_position(client, ps, side, price):
    active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
    if not active: return
    lvl = active[0]; prec = _prec(ps.symbol); csize = _csize(ps.symbol)
    try:
        client.set_leverage(ps.symbol, ps.leverage)
        ticker     = client.get_ticker(ps.symbol)
        exec_price = float(ticker.get("lastPrice", price))
        vol0 = max(1, round(lvl["amount_usd"] / (exec_price * csize / ps.leverage)))
        result = client.place_order(ps.symbol, 1 if side == "LONG" else 3, vol0, ps.leverage)
        if not result.get("success", False):
            ps.log(f"MEXC odrzucil: {result.get('message','')}", "ERROR"); return
        ps.pyramid_entries.append(PyramidEntry(exec_price, vol0, side))
        ps.pyramid_active = True; ps.pyramid_side = side
        limit_ids = []; last_dok_price = exec_price
        for i, dok_lvl in enumerate(active[1:], start=1):
            if side == "LONG":
                dok_price = round(last_dok_price * (1 - dok_lvl["offset_pct"] / 100), prec)
                dok_side  = 1
            else:
                dok_price = round(last_dok_price * (1 + dok_lvl["offset_pct"] / 100), prec)
                dok_side  = 3
            dok_vol = max(1, round(dok_lvl["amount_usd"] / (dok_price * csize / ps.leverage)))
            dr = client._post("/api/v1/private/order/submit", {
                "symbol": ps.symbol, "side": dok_side,
                "openType": gstate.margin_mode, "type": 1,
                "vol": dok_vol, "leverage": ps.leverage, "price": dok_price
            })
            if dr.get("success"):
                limit_ids.append(dr.get("data"))
                ps.log(f"Dokladka {i+1} limit @ {dok_price}")
                last_dok_price = dok_price
            else:
                ps.log(f"Blad dokladki {i+1}: {dr.get('message','')}", "WARN")
            time.sleep(2.0)
        ps.pyramid_limit_order_ids = limit_ids
        sl_price = _calc_sl(last_dok_price, side, ps.sl_pct, prec)
        tp_price = _calc_tp(exec_price, side, ps.tp_pct, prec)
        ps.current_tp = tp_price; ps.current_sl = sl_price
        if not gstate.tp_sl_enabled:
            ps.log("TP/SL wylaczone", "WARN")
        else:
            try:
                time.sleep(1.0)
                positions = client.get_positions(ps.symbol)
                if positions:
                    pos_id   = positions[0].get("positionId")
                    hold_vol = positions[0].get("holdVol", 0)
                    sl_result = client._post("/api/v1/private/stoporder/place", {
                        "positionId": pos_id, "symbol": ps.symbol, "vol": hold_vol,
                        "lossTrend": 1, "profitTrend": 1,
                        "stopLossPrice": round(sl_price, prec)
                    })
                    if sl_result.get("success"):
                        ps.log(f"SL:{sl_price} ustawiony w MEXC")
                    else:
                        ps.log(f"Blad SL: {sl_result.get('message')} — bot monitoruje", "WARN")
            except Exception as e:
                ps.log(f"Blad SL: {e}", "WARN")
        ps.log(f"[ORB] {side} | Cena:{exec_price} | TP:{tp_price} | SL:{sl_price} | Dok:{len(limit_ids)}", "SUCCESS")
        tg(f"🚀 <b>{ps.symbol}</b> [ORB] {side}\nCena: {exec_price}\nTP: {tp_price} | SL: {sl_price}\nORB H:{round(ps.orb_high,prec)} L:{round(ps.orb_low,prec)}")
    except Exception as e:
        ps.log(f"Blad wejscia: {e}", "ERROR")

def _check_continuation(client, ps, price):
    try:
        positions = client.get_positions(ps.symbol)
        if not positions: return
        pos = positions[0]; hold_vol = pos.get("holdVol", 0)
        active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
        known_vol = sum(e.vol for e in ps.pyramid_entries)
        if hold_vol > known_vol and len(ps.pyramid_entries) < len(active):
            new_vol = hold_vol - known_vol
            exec_price = float(pos.get("openAvgPrice", price))
            ps.pyramid_entries.append(PyramidEntry(exec_price, new_vol, ps.pyramid_side))
            ps.log(f"Dokladka {len(ps.pyramid_entries)}/{len(active)} @ {exec_price}", "SUCCESS")
            prec = _prec(ps.symbol)
            last_price = ps.pyramid_entries[-1].price
            ps.current_tp = _calc_tp(last_price, ps.pyramid_side, ps.tp_pct, prec)
            ps.log(f"TP zaktualizowany: {ps.current_tp}")
    except Exception as e:
        ps.log(f"check_continuation blad: {e}", "ERROR")

def _open_hedge(client, ps, side, price):
    active = [l for l in ps.pyramid_levels if l.get("enabled", True)]
    if not active: return
    lvl = active[0]; prec = _prec(ps.symbol); csize = _csize(ps.symbol)
    try:
        ticker = client.get_ticker(ps.symbol)
        exec_price = float(ticker.get("lastPrice", price))
        vol0 = max(1, round(lvl["amount_usd"] / (exec_price * csize / ps.leverage)))
        result = client.place_order(ps.symbol, 1 if side == "LONG" else 3, vol0, ps.leverage)
        if not result.get("success", False):
            ps.log(f"HEDGE odrzucony: {result.get('message','')}", "ERROR"); return
        ps.hedge_entries.append(PyramidEntry(exec_price, vol0, side))
        ps.hedge_active = True; ps.hedge_side = side
        sl_price = _calc_sl(exec_price, side, ps.sl_pct, prec)
        tp_price = _calc_tp(exec_price, side, ps.tp_pct, prec)
        ps.hedge_current_tp = tp_price; ps.hedge_current_sl = sl_price
        ps.log(f"HEDGE {side} | Cena:{exec_price} | TP:{tp_price} | SL:{sl_price}", "SUCCESS")
        tg(f"⚡ <b>{ps.symbol}</b> [ORB] HEDGE {side}\nCena: {exec_price}\nTP: {tp_price} | SL: {sl_price}")
    except Exception as e:
        ps.log(f"HEDGE blad: {e}", "ERROR")

# ─── BOT LOOP ─────────────────────────────────────────────────────────────────
async def monitor_loop():
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
                    _check_continuation(client, ps, price)
                    if gstate.tp_sl_enabled and ps.current_tp and ps.current_sl and ps.pyramid_side:
                        tp_hit = (price >= ps.current_tp if ps.pyramid_side == "LONG" else price <= ps.current_tp)
                        sl_hit = (price <= ps.current_sl if ps.pyramid_side == "LONG" else price >= ps.current_sl)
                        if tp_hit or sl_hit:
                            reason = "TP" if tp_hit else "SL"
                            ps.log(f"{reason} @ {price} (TP:{ps.current_tp} SL:{ps.current_sl})", "SUCCESS")
                            tg(f"{'✅' if reason=='TP' else '❌'} <b>{ps.symbol}</b> [ORB] {reason}\nCena: {price}\nTP: {ps.current_tp} | SL: {ps.current_sl}")
                            all_pos = client.get_positions()
                            for pos in all_pos:
                                if pos.get("symbol") == ps.symbol:
                                    client.close_position(ps.symbol, pos.get("positionType",1), pos.get("holdVol",0), ps.leverage)
                            try: client._post("/api/v1/private/order/cancel_all", {"symbol": ps.symbol})
                            except: pass
                            ps.reset_pyramid(); ps.current_tp = None; ps.current_sl = None
                            continue
                    all_pos = client.get_positions()
                    current = [p for p in all_pos if p.get("symbol") == ps.symbol]
                    if not current and ps.pyramid_active:
                        ps.log("Pozycja zamknieta (MEXC) - reset", "SUCCESS")
                        ps.reset_pyramid(); ps.current_tp = None; ps.current_sl = None
                    else:
                        ps.open_positions = current
                    if gstate.hedging_enabled and ps.hedge_active:
                        if gstate.tp_sl_enabled and ps.hedge_current_tp and ps.hedge_current_sl:
                            h_tp = (price >= ps.hedge_current_tp if ps.hedge_side == "LONG" else price <= ps.hedge_current_tp)
                            h_sl = (price <= ps.hedge_current_sl if ps.hedge_side == "LONG" else price >= ps.hedge_current_sl)
                            if h_tp or h_sl:
                                reason = "TP" if h_tp else "SL"
                                ps.log(f"HEDGE {reason} @ {price}", "SUCCESS")
                                tg(f"{'✅' if reason=='TP' else '❌'} <b>{ps.symbol}</b> [ORB] HEDGE {reason}\nCena: {price}")
                                h_type = 1 if ps.hedge_side == "LONG" else 2
                                for pos in all_pos:
                                    if pos.get("symbol") == ps.symbol and pos.get("positionType") == h_type:
                                        client.close_position(ps.symbol, h_type, pos.get("holdVol",0), ps.leverage)
                                try: client._post("/api/v1/private/order/cancel_all", {"symbol": ps.symbol})
                                except: pass
                                ps.reset_hedge(); ps.hedge_current_tp = None; ps.hedge_current_sl = None
                except Exception as e:
                    ps.log(f"Monitor blad: {e}", "ERROR")
                await asyncio.sleep(0.3)
        except Exception as e:
            gstate.log(f"Monitor error: {e}", "ERROR")

async def bot_loop():
    while gstate.running:
        if not gstate.api_key:
            await asyncio.sleep(5); continue
        now = datetime.now()
        # Sprawdz czy dzien roboczy (pon=0 ... pt=4)
        if now.weekday() > 4:
            gstate.log(f"Weekend — czekam do poniedzialku")
            for _ in range(3600):
                if not gstate.running: break
                await asyncio.sleep(1)
            continue
        # Sprawdz czy jestesmy w okolicach sesji NY (14:00-18:00 CET)
        if not (14 <= now.hour < 18):
            mins_to_open = ((14 - now.hour) * 60 - now.minute) % (24*60)
            gstate.log(f"Poza oknem sesji NY — czekam {mins_to_open}min")
            for _ in range(1800):
                if not gstate.running: break
                await asyncio.sleep(1)
            continue
        client  = MEXCClient(gstate.api_key, gstate.api_secret)
        actives = [ps for ps in gstate.pairs.values() if ps.enabled]
        if not actives:
            gstate.log("Brak aktywnych par", "WARN")
            await asyncio.sleep(30); continue
        for ps in actives:
            run_pair_strategy(client, ps)
            await asyncio.sleep(0.5)
        try: save_logs()
        except: pass
        # Sprawdzaj co minute
        gstate.log(f"Sprawdzam za 60s ({len(actives)} par)")
        for _ in range(60):
            if not gstate.running: break
            await asyncio.sleep(1)

# ─── MODELS ───────────────────────────────────────────────────────────────────
class LoginReq(BaseModel): password: str
class ChangePwReq(BaseModel): old_password: str; new_password: str
class PyramidLevelIn(BaseModel): enabled: bool = True; amount_usd: float = 5.0; offset_pct: float = 0.0
class PairConfig(BaseModel):
    symbol: str; enabled: bool = True
    interval: str = "Min5"; direction: str = "BOTH"
    ma200_enabled: bool = False; ma200_tf: str = "Hour4"
    pyramid_levels: List[PyramidLevelIn] = []
    leverage: int = 10; tp_pct: float = 1.0; sl_pct: float = 0.5
class GlobalConfig(BaseModel): api_key: str; api_secret: str; pairs: List[PairConfig]

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
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
        for k in ["enabled","interval","direction","ma200_enabled","ma200_tf",
                  "leverage","tp_pct","sl_pct"]:
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
    gstate.running = True; gstate.log("🚀 Bot ORB uruchomiony")
    _bot_task = asyncio.create_task(bot_loop())
    asyncio.create_task(monitor_loop())
    return {"ok": True}

@app.post("/api/stop")
async def stop_bot(_=Depends(require_auth)):
    gstate.running = False; gstate.log("🛑 Bot zatrzymany")
    return {"ok": True}

@app.post("/api/margin_mode/{val}")
async def set_margin_mode(val: int, _=Depends(require_auth)):
    gstate.margin_mode = 1 if val == 1 else 2
    save_config(); gstate.log(f"Tryb marginu: {'Isolated' if gstate.margin_mode==1 else 'Cross'}")
    return {"ok": True, "margin_mode": gstate.margin_mode}

@app.post("/api/max_positions/{val}")
async def set_max_positions(val: int, _=Depends(require_auth)):
    gstate.max_positions = max(1, val)
    save_config(); gstate.log(f"Maks. pozycji: {gstate.max_positions}")
    return {"ok": True, "max_positions": gstate.max_positions}

@app.post("/api/signals_only/{enabled}")
async def set_signals_only(enabled: int, _=Depends(require_auth)):
    gstate.signals_only = bool(enabled)
    mode = "📡 Tryb sygnałów" if gstate.signals_only else "🚀 Tryb tradingu"
    gstate.log(f"{mode} aktywny"); tg(f"{mode} aktywny"); save_config()
    return {"ok": True, "signals_only": gstate.signals_only}

@app.post("/api/tp_sl/{enabled}")
async def set_tp_sl(enabled: int, _=Depends(require_auth)):
    gstate.tp_sl_enabled = bool(enabled)
    mode = "✅ TP/SL włączone" if gstate.tp_sl_enabled else "⛔ TP/SL wyłączone"
    save_config(); gstate.log(mode); tg(mode)
    return {"ok": True, "tp_sl_enabled": gstate.tp_sl_enabled}

@app.post("/api/hedging/{enabled}")
async def set_hedging(enabled: int, _=Depends(require_auth)):
    gstate.hedging_enabled = bool(enabled)
    save_config(); gstate.log(f"Hedging: {gstate.hedging_enabled}")
    return {"ok": True, "hedging_enabled": gstate.hedging_enabled}

@app.post("/api/reset_orb/{symbol}")
def reset_orb(symbol: str, _=Depends(require_auth)):
    if symbol in gstate.pairs:
        gstate.pairs[symbol].reset_orb()
        gstate.pairs[symbol].log("🔄 ORB zresetowany")
    return {"ok": True}

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
        del gstate.pairs[symbol]; save_config()
        gstate.log(f"Usunięto {symbol}")
    return {"ok": True}

@app.get("/api/balance")
def get_balance(_=Depends(require_auth)):
    if not gstate.api_key: return {"balance": []}
    try:
        client = MEXCClient(gstate.api_key, gstate.api_secret)
        data = client._get("/api/v1/private/account/assets")
        result = []
        for a in data.get("data", []):
            equity = float(a.get("equity", 0))
            if equity > 0:
                result.append({
                    "currency": a.get("currency", ""),
                    "equity": round(equity, 4),
                    "available": round(float(a.get("availableBalance", 0)), 4),
                    "unrealized_pnl": round(float(a.get("unrealizedProfit", 0)), 4),
                })
        return {"balance": result}
    except Exception as e:
        return {"balance": [], "error": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003)
