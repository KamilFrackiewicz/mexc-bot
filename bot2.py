"""
MEXC Futures Trading Bot v2 - Strategia TikTak
Strategia: MACD crossover + opcjonalny filtr EMA 10/30 + opcjonalny filtr EMA200
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

app = FastAPI(title="MEXC Futures Bot TikTak")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

MEXC_BASE   = "https://api.mexc.com"
CONFIG_FILE = "config2.json"
AUTH_FILE   = "auth.json"
LOG_FILE    = "logs2.json"
security    = HTTPBearer(auto_error=False)

TELEGRAM_TOKEN   = "8828080533:AAFrSbgunu3LJ8IhKMmjnvesNl6rD5j9tKk"
TELEGRAM_CHAT_ID = "5137354808"

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
def calc_ema(closes, period):
    arr = np.array(closes, dtype=float)
    if len(arr) < period: return []
    k = 2.0 / (period + 1)
    ema = [float(np.mean(arr[:period]))]
    for price in arr[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal: return [], [], []
    ema_fast = calc_ema(closes, fast)
    ema_slow = calc_ema(closes, slow)
    # Wyrownaj dlugosci
    diff = len(ema_fast) - len(ema_slow)
    ema_fast = ema_fast[diff:]
    macd_line = [f - s for f, s in zip(ema_fast, ema_slow)]
    if len(macd_line) < signal: return [], [], []
    signal_line = calc_ema(macd_line, signal)
    diff2 = len(macd_line) - len(signal_line)
    macd_line = macd_line[diff2:]
    histogram = [m - s for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, histogram

def calc_ma200(closes, period=200):
    if len(closes) < period: return None
    return float(np.mean(closes[-period:]))

# ─── PER-PAIR STATE ───────────────────────────────────────────────────────────
class PyramidEntry:
    def __init__(self, price, vol, side):
        self.price = price; self.vol = vol; self.side = side
        self.time  = datetime.now().strftime("%H:%M:%S")

class PairState:
    def __init__(self, symbol):
        self.symbol = symbol; self.enabled = True
        self.interval = "Min15"; self.direction = "BOTH"
        self.ema_filter_enabled = False
        self.ema_window = 5
        self.ma200_enabled = False; self.ma200_tf = "Min60"
        self.pyramid_levels = [
            {"enabled": True,  "amount_usd": 5.0,  "offset_pct": 0.0},
            {"enabled": False, "amount_usd": 5.0,  "offset_pct": 0.4},
            {"enabled": False, "amount_usd": 10.0, "offset_pct": 0.4},
            {"enabled": False, "amount_usd": 20.0, "offset_pct": 0.4},
        ]
        self.leverage = 10
        self.tp_pct = 0.8; self.sl_pct = 0.4
        # Wskazniki
        self.last_price = None; self.last_check = None
        self.last_macd = None; self.last_signal = None
        self.last_ema10 = None; self.last_ema30 = None
        self.last_ma200 = None
        self.last_signal_dir = "NONE"
        self.macd_cross_candle = None
        # Stan piramidy
        self.pyramid_entries: List[PyramidEntry] = []
        self.pyramid_active = False; self.pyramid_side = None
        self.current_tp: Optional[float] = None
        self.current_sl: Optional[float] = None
        self.pyramid_limit_order_ids: List = []
        self.open_positions = []; self.logs: List[dict] = []
        # Hedge
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
            "ema_filter_enabled": self.ema_filter_enabled,
            "ema_window": self.ema_window,
            "ma200_enabled": self.ma200_enabled, "ma200_tf": self.ma200_tf,
            "pyramid_levels": self.pyramid_levels,
            "leverage": self.leverage,
            "tp_pct": self.tp_pct, "sl_pct": self.sl_pct,
            "last_price": self.last_price, "last_check": self.last_check,
            "last_macd": self.last_macd, "last_signal_line": self.last_signal,
            "last_ema10": self.last_ema10, "last_ema30": self.last_ema30,
            "last_ma200": self.last_ma200,
            "last_signal_dir": self.last_signal_dir,
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
        self.free_entries = False
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
                "ema_filter_enabled","ema_window",
                "ma200_enabled","ma200_tf","pyramid_levels",
                "leverage","tp_pct","sl_pct"
            ]})
        data["signals_only"] = gstate.signals_only
        data["max_positions"] = gstate.max_positions
        data["margin_mode"] = gstate.margin_mode
        data["tp_sl_enabled"] = gstate.tp_sl_enabled
        data["hedging_enabled"] = gstate.hedging_enabled
        data["free_entries"] = gstate.free_entries
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
        gstate.free_entries = data.get("free_entries", False)
        for pd in data.get("pairs", []):
            sym = pd.get("symbol")
            if not sym: continue
            ps = gstate.get_or_create(sym)
            for k in ["enabled","interval","direction",
                      "ema_filter_enabled","ema_window",
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
    except Exception as e:
        logger.error(f"Log load error: {e}")

load_logs()

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

# ─── STRATEGY ─────────────────────────────────────────────────────────────────
def run_pair_strategy(client: MEXCClient, ps: PairState):
    try:
        kdata  = client.get_klines_full(ps.symbol, ps.interval, 500)
        closes = [float(x) for x in kdata.get("close", [])]
        if len(closes) < 100:
            ps.log("Za malo danych", "WARN"); return

        price = closes[-1]
        ps.last_price = price
        ps.last_check = datetime.now().strftime("%H:%M:%S")

        # ── MACD ──────────────────────────────────────────────────────────────
        macd_line, signal_line, histogram = calc_macd(closes)
        if len(macd_line) < 4:
            ps.log("Za malo danych MACD", "WARN"); return

        ps.last_macd   = round(macd_line[-1], 6)
        ps.last_signal = round(signal_line[-1], 6)

        # Crossover MACD z linią sygnałową — tylko bieżąca świeca
        # LONG: MACD przecina sygnał od dołu PONIŻEJ zera
        # SHORT: MACD przecina sygnał od góry POWYŻEJ zera
        # Crossover MACD z linia sygnalowa — bez warunku strefy
        macd_cross_long = (
            macd_line[-2] <= signal_line[-2] and
            macd_line[-1] > signal_line[-1] + 0.0001 and
            macd_line[-1] < 0
        )
        macd_cross_short = (
            macd_line[-2] >= signal_line[-2] and
            macd_line[-1] < signal_line[-1] - 0.0001 and
            macd_line[-1] > 0
        )

        # Zapisz numer świecy gdy nastąpił crossover MACD (do filtra EMA)
        candle_idx = len(closes) - 1
        # Blokuj wielokrotne wejscia na tej samej swiece
        if ps.macd_cross_candle == candle_idx:
            macd_cross_long = False
            macd_cross_short = False
        else:
            if macd_cross_long or macd_cross_short:
                ps.macd_cross_candle = candle_idx

        # ── EMA 10/30 (opcjonalny filtr) ──────────────────────────────────────
        ema_ok_long = ema_ok_short = True
        ema10_series = calc_ema(closes, 10)
        ema30_series = calc_ema(closes, 30)
        if ema10_series and ema30_series:
            ps.last_ema10 = round(ema10_series[-1], _prec(ps.symbol))
            ps.last_ema30 = round(ema30_series[-1], _prec(ps.symbol))

        if ps.ema_filter_enabled and ema10_series and ema30_series:
            window = ps.ema_window
            # EMA cross w ciągu ostatnich `window` świec
            ema_cross_long = any(
                ema10_series[-(i+2)] <= ema30_series[-(i+2)] and
                ema10_series[-(i+1)] > ema30_series[-(i+1)]
                for i in range(window) if len(ema10_series) > i+2 and len(ema30_series) > i+2
            )
            ema_cross_short = any(
                ema10_series[-(i+2)] >= ema30_series[-(i+2)] and
                ema10_series[-(i+1)] < ema30_series[-(i+1)]
                for i in range(window) if len(ema10_series) > i+2 and len(ema30_series) > i+2
            )
            ema_ok_long  = ema_cross_long
            ema_ok_short = ema_cross_short

        # ── MA200 filtr trendu ─────────────────────────────────────────────────
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

        # ── Sygnał ────────────────────────────────────────────────────────────
        signal = "WAIT"
        long_ok  = (macd_cross_long  and ema_ok_long  and ma200_ok_long
                    and ps.direction in ("LONG",  "BOTH"))
        short_ok = (macd_cross_short and ema_ok_short and ma200_ok_short
                    and ps.direction in ("SHORT", "BOTH"))

        if long_ok:    signal = "LONG"
        elif short_ok: signal = "SHORT"
        ps.last_signal_dir = signal

        ema_str = f" EMA10:{round(ps.last_ema10,4) if ps.last_ema10 else '-'} EMA30:{round(ps.last_ema30,4) if ps.last_ema30 else '-'}"
        ma_str  = f" MA200:{round(ps.last_ma200,4) if ps.last_ma200 else 'off'}"
        ps.log(f"P:{price} MACD:{round(ps.last_macd,5)} SIG:{round(ps.last_signal,5)} "
               f"xL:{macd_cross_long} xS:{macd_cross_short}{ema_str}{ma_str} -> {signal}")

        # ── Wejście ───────────────────────────────────────────────────────────
        if signal in ("LONG", "SHORT") and (not ps.pyramid_active or gstate.free_entries):
            active_count = sum(1 for p in gstate.pairs.values() if p.pyramid_active)
            if active_count >= gstate.max_positions:
                ps.log(f"Blokada: {active_count}/{gstate.max_positions} pozycji", "WARN")
            elif gstate.signals_only:
                ps.log(f"📡 SYGNAŁ {signal} @ {price}")
                iv_label = {"Min1":"1min","Min5":"5min","Min15":"15min","Min30":"30min","Hour1":"1h","Hour4":"4h"}.get(ps.interval, ps.interval)
                tg(f"📡 <b>{ps.symbol}</b> [TikTak] SYGNAŁ {signal}\n"
                   f"🕐 {datetime.now().strftime('%H:%M:%S')} ({iv_label})\n"
                   f"Cena: {price}\n"
                   f"MACD: {round(ps.last_macd,5)} | Signal: {round(ps.last_signal,5)}\n"
                   f"MA200: {round(ps.last_ma200,0) if ps.last_ma200 else 'off'}")
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
            ps.log("Pozycja zamknieta (MEXC) - resetuje", "SUCCESS")
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
    lvl = active[0]
    prec  = _prec(ps.symbol)
    csize = _csize(ps.symbol)
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

        # Dokładki jako limit orders
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
                ps.log(f"Dokladka {i+1} limit @ {dok_price} ({dok_lvl['offset_pct']}%) ID:{dr.get('data')}")
                last_dok_price = dok_price
            else:
                ps.log(f"Blad dokladki {i+1}: {dr.get('message','')}", "WARN")
            time.sleep(2.0)
        ps.pyramid_limit_order_ids = limit_ids

        sl_price = _calc_sl(last_dok_price, side, ps.sl_pct, prec)
        tp_price = _calc_tp(exec_price, side, ps.tp_pct, prec)
        ps.current_tp = tp_price; ps.current_sl = sl_price

        if not gstate.tp_sl_enabled:
            ps.log("TP/SL wyłączone — pozycja bez SL w MEXC", "WARN")
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
                        ps.log(f"Blad SL MEXC: {sl_result.get('message')} — bot monitoruje", "WARN")
                else:
                    ps.log(f"Brak pozycji — bot monitoruje SL @ {sl_price}", "WARN")
            except Exception as e:
                ps.log(f"Blad SL MEXC: {e}", "WARN")

        ps.log(f"Poz.1/{len(active)} {side} | Cena:{exec_price} | TP:{tp_price} | SL:{sl_price} | Dok:{len(limit_ids)}", "SUCCESS")
        tg(f"🚀 <b>{ps.symbol}</b> [TikTak] {side}\nCena: {exec_price}\nTP: {tp_price} | SL: {sl_price}\nDokładki: {len(limit_ids)}")
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
        limit_ids = []; last_dok_price = exec_price
        for i, dok_lvl in enumerate(active[1:], start=1):
            dok_price = round(last_dok_price * (1 - dok_lvl["offset_pct"]/100) if side == "LONG" else last_dok_price * (1 + dok_lvl["offset_pct"]/100), prec)
            dok_vol = max(1, round(dok_lvl["amount_usd"] / (dok_price * csize / ps.leverage)))
            dr = client._post("/api/v1/private/order/submit", {"symbol": ps.symbol, "side": 1 if side=="LONG" else 3, "openType": gstate.margin_mode, "type": 1, "vol": dok_vol, "leverage": ps.leverage, "price": dok_price})
            if dr.get("success"): limit_ids.append(dr.get("data")); last_dok_price = dok_price
            time.sleep(2.0)
        ps.hedge_limit_order_ids = limit_ids
        sl_price = _calc_sl(last_dok_price, side, ps.sl_pct, prec)
        tp_price = _calc_tp(exec_price, side, ps.tp_pct, prec)
        ps.hedge_current_tp = tp_price; ps.hedge_current_sl = sl_price
        ps.log(f"HEDGE {side} | Cena:{exec_price} | TP:{tp_price} | SL:{sl_price}", "SUCCESS")
        tg(f"⚡ <b>{ps.symbol}</b> [TikTak] HEDGE {side}\nCena: {exec_price}\nTP: {tp_price} | SL: {sl_price}")
    except Exception as e:
        ps.log(f"HEDGE blad: {e}", "ERROR")

def _check_hedge_continuation(client, ps, price):
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
            prec = _prec(ps.symbol)
            last_price = ps.hedge_entries[-1].price
            ps.hedge_current_tp = _calc_tp(last_price, ps.hedge_side, ps.tp_pct, prec)
            ps.log(f"HEDGE Dokladka {len(ps.hedge_entries)}/{len(active)} @ {exec_price}", "SUCCESS")
    except Exception as e:
        ps.log(f"HEDGE check blad: {e}", "ERROR")

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
                            tg(f"{'✅' if reason=='TP' else '❌'} <b>{ps.symbol}</b> [TikTak] {reason}\nCena: {price}\nTP: {ps.current_tp} | SL: {ps.current_sl}")
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
                        _check_hedge_continuation(client, ps, price)
                        if gstate.tp_sl_enabled and ps.hedge_current_tp and ps.hedge_current_sl:
                            h_tp = (price >= ps.hedge_current_tp if ps.hedge_side == "LONG" else price <= ps.hedge_current_tp)
                            h_sl = (price <= ps.hedge_current_sl if ps.hedge_side == "LONG" else price >= ps.hedge_current_sl)
                            if h_tp or h_sl:
                                reason = "TP" if h_tp else "SL"
                                ps.log(f"HEDGE {reason} @ {price}", "SUCCESS")
                                tg(f"{'✅' if reason=='TP' else '❌'} <b>{ps.symbol}</b> [TikTak] HEDGE {reason}\nCena: {price}")
                                h_type = 1 if ps.hedge_side == "LONG" else 2
                                for pos in all_pos:
                                    if pos.get("symbol") == ps.symbol and pos.get("positionType") == h_type:
                                        client.close_position(ps.symbol, h_type, pos.get("holdVol",0), ps.leverage)
                                try: client._post("/api/v1/private/order/cancel_all", {"symbol": ps.symbol})
                                except: pass
                                ps.reset_hedge(); ps.hedge_current_tp = None; ps.hedge_current_sl = None
                        else:
                            h_type = 1 if ps.hedge_side == "LONG" else 2
                            h_cur = [p for p in all_pos if p.get("symbol") == ps.symbol and p.get("positionType") == h_type]
                            if not h_cur:
                                ps.log("HEDGE zamkniety (MEXC) - reset", "SUCCESS")
                                ps.reset_hedge(); ps.hedge_current_tp = None; ps.hedge_current_sl = None
                except Exception as e:
                    ps.log(f"Monitor blad: {e}", "ERROR")
                await asyncio.sleep(0.3)
        except Exception as e:
            gstate.log(f"Monitor error: {e}", "ERROR")

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
        min_iv = min((iv_map.get(ps.interval,900) for ps in actives), default=900)
        gstate.log(f"Następne za {min_iv}s ({len(actives)} par)")
        try: save_logs()
        except: pass
        for _ in range(min_iv):
            if not gstate.running: break
            await asyncio.sleep(1)

# ─── MODELS ───────────────────────────────────────────────────────────────────
class LoginReq(BaseModel): password: str
class ChangePwReq(BaseModel): old_password: str; new_password: str
class PyramidLevelIn(BaseModel): enabled: bool = True; amount_usd: float = 5.0; offset_pct: float = 0.0
class PairConfig(BaseModel):
    symbol: str; enabled: bool = True
    interval: str = "Min15"; direction: str = "BOTH"
    ema_filter_enabled: bool = False; ema_window: int = 5
    ma200_enabled: bool = False; ma200_tf: str = "Hour1"
    pyramid_levels: List[PyramidLevelIn] = []
    leverage: int = 10; tp_pct: float = 0.8; sl_pct: float = 0.4
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
        for k in ["enabled","interval","direction","ema_filter_enabled","ema_window",
                  "ma200_enabled","ma200_tf","leverage","tp_pct","sl_pct"]:
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
    gstate.running = True; gstate.log("🚀 Bot TikTak uruchomiony")
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
    save_config()
    gstate.log(f"Tryb marginu: {'Isolated' if gstate.margin_mode==1 else 'Cross'}")
    return {"ok": True, "margin_mode": gstate.margin_mode}

@app.post("/api/max_positions/{val}")
async def set_max_positions(val: int, _=Depends(require_auth)):
    gstate.max_positions = max(1, val)
    save_config()
    gstate.log(f"Maks. pozycji: {gstate.max_positions}")
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

@app.post("/api/free_entries/{enabled}")
async def set_free_entries(enabled: int, _=Depends(require_auth)):
    gstate.free_entries = bool(enabled)
    save_config(); gstate.log(f"Free entries: {gstate.free_entries}")
    return {"ok": True, "free_entries": gstate.free_entries}

@app.post("/api/hedging/{enabled}")
async def set_hedging(enabled: int, _=Depends(require_auth)):
    gstate.hedging_enabled = bool(enabled)
    save_config(); gstate.log(f"Hedging: {gstate.hedging_enabled}")
    return {"ok": True, "hedging_enabled": gstate.hedging_enabled}

@app.get("/api/status")
def get_status(_=Depends(require_auth)):
    return {"running": gstate.running,
            "signals_only": gstate.signals_only,
            "max_positions": gstate.max_positions,
            "margin_mode": gstate.margin_mode,
            "tp_sl_enabled": gstate.tp_sl_enabled,
            "hedging_enabled": gstate.hedging_enabled,
            "free_entries": gstate.free_entries,
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
    uvicorn.run(app, host="0.0.0.0", port=8001)
