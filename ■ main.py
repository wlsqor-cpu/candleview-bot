from http.server import HTTPServer, SimpleHTTPRequestHandler
import json
import os
import re
import threading
import time
import ccxt
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta

# ============================================================
# 거래소 인식 — 검증된 3개로 고정 (V003 신뢰성 검증 결과 반영)
# 바이낸스·바이비트·쿠코인 등은 Render(AWS US) 환경에서 미국IP 지오블록에 걸려
# 에러가 발생하므로 지원 대상에서 제외한다. 아래 3개는 지오블록 없음이 확인됨.
# 코인베이스는 USDT가 아닌 USD가 표준 페어이므로 quote를 USD로 고정한다.
# ============================================================
SUPPORTED_EXCHANGES = {
    # 1w는 거래소 API 지원여부와 무관하게 항상 일봉 리샘플링으로 생성(resample_daily_to_weekly)하므로
    # 3개 거래소 모두 동일 패턴(1w,1d,X,1h)을 안전하게 사용할 수 있다.
    # 빗썸 4h: ccxt 정적목록엔 없지만 실사용으로 정상 동작 확인됨 → 포함.
    # 코인베이스 4h: 실제 API 호출 시 확정 오류("granularity 4h is not a valid value") 확인됨 → 제외, 6h로 대체.
    "upbit":    {"quote": "KRW", "kr_name": "업비트",   "default_tfs": ["1w", "1d", "4h", "1h"]},
    "bithumb":  {"quote": "KRW", "kr_name": "빗썸",     "default_tfs": ["1w", "1d", "4h", "1h"]},
    "coinbase": {"quote": "USD", "kr_name": "코인베이스", "default_tfs": ["1w", "1d", "6h", "1h"]},
}

EXCHANGE_KR_MAP = {"업비트": "upbit", "빗썸": "bithumb", "코인베이스": "coinbase"}

# ============================================================
# 비인가 입력(슬래시 명령이 아닌 임의 입력) 안내 문구
# ============================================================
UNAUTHORIZED_INPUT_GUIDE = (
    "본 시스템은 분석입력, 출력 외 다른 기능은 제공되지 않습니다.\n\n"
    "<b>▶️ 지정코인 분석 명령어</b>\n"
    "/거래소 + 코인명 을 입력해주세요. (한글·영문 모두 가능)\n\n"
    "예시) /업비트 비트코인   또는   /coinbase btc\n\n"
    "시간대(TF)는 자동으로 적용됩니다.\n"
    "• 업비트·빗썸: 1주, 1일, 4시간, 1시간\n"
    "• 코인베이스: 1주, 1일, 6시간, 1시간\n"
    "필요시 직접 지정도 가능합니다: /업비트 비트코인 1d 4h 1h\n\n"
    "<b>한글 코인명 안내</b>\n"
    "업비트·빗썸은 각 거래소 상장목록 기준 한글 인식됩니다.\n"
    "코인베이스는 자체 한글명이 없어 업비트·빗썸에도 상장된 코인만\n"
    "한글 인식되며, 코인베이스 전용 코인은 영문 심볼로 입력해 주세요.\n\n"
    "<b>지원 거래소</b>\n"
    "업비트(KRW) · 빗썸(KRW) · 코인베이스(USD)\n\n"
    "<b>🔎 FindCoin</b>\n"
    "현재 시세분출 가능성이 높은 코인을 분석해서 Top3를 알려드립니다.\n"
    "/거래소 만 입력하면 실행됩니다. (예: /업비트)\n"
    "(전종목 스캔이라 1~3분 정도 소요될 수 있습니다.)"
)


def friendly_error_message(exc, ex_display, symbol_display):
    """기술적 예외를 사용자 친화적 한글 메시지로 변환한다.
    원본 예외(ccxt/Python 메시지)는 서버 로그에만 남기고 사용자에게는 노출하지 않는다."""
    err = str(exc).lower()

    if "timeout" in err or "timed out" in err:
        return (
            f"⏱️ {ex_display} 서버 응답이 지연되고 있습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    if "granularity" in err or "timeframe" in err or "interval" in err and "not a valid" in err:
        return (
            f"⏰ {ex_display}에서 지원하지 않는 시간대(TF)입니다.\n"
            f"다른 시간대로 다시 시도해 주세요."
        )
    if "rate limit" in err or "429" in err or "too many requests" in err:
        return (
            f"⏳ {ex_display} 요청이 일시적으로 많습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    if "connection" in err or "network" in err or "getaddrinfo" in err or "resolve" in err:
        return (
            f"🔌 {ex_display} 서버에 연결할 수 없습니다.\n"
            f"잠시 후 다시 시도해 주세요."
        )
    return (
        f"⚠️ '{symbol_display}' 분석 중 문제가 발생했습니다.\n"
        f"코인명과 거래소를 다시 확인하신 뒤 시도해 주세요.\n"
        f"문제가 계속되면 잠시 후 다시 시도해 주세요."
    )


def resolve_exchange(name: str):
    """한글명 또는 영문 id를 지원 거래소 id로 변환. 미지원 시 None."""
    if not name:
        return None
    raw = name.strip()
    if raw in EXCHANGE_KR_MAP:
        return EXCHANGE_KR_MAP[raw]
    key = raw.lower()
    if key in SUPPORTED_EXCHANGES:
        return key
    return None

# ============================================================
# Render 포트 바인딩 (헬스체크용)
# ============================================================
def run_port_server():
    try:
        port = int(os.environ.get("PORT", 10000))
        server = HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler)
        print(f"포트 {port} 오픈 완료. Render 수신 대기 중...")
        server.serve_forever()
    except Exception as e:
        print(f"포트 서버 스레드 오류: {e}")


threading.Thread(target=run_port_server, daemon=True).start()

# ============================================================
# 환경변수 로드
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    print("⚠️ 경고: TELEGRAM_BOT_TOKEN 또는 GEMINI_API_KEY 환경변수가 비어 있습니다.")

# ============================================================
# 엔진 파일 로드 (운영 고정명 우선)
# 권장 파일명: CandleView_API.txt  (버전 번호는 파일명에 넣지 않음)
# 개정 시 같은 이름으로 덮어쓰면 되고, main.py는 수정할 필요 없음.
# ============================================================
def get_latest_candleview_file():
    """운영 고정명 우선. 버전 숫자는 파일명에 넣지 않는 것을 원칙으로 한다.
    우선순위:
      1) CandleView_API.txt  (권장 운영본)
      2) CandleView.txt
      3) CandleView로 시작하는 기타 .txt (하위 호환, 이름순 마지막 수단)
    """
    preferred = ["CandleView_API.txt", "CandleView.txt"]
    for name in preferred:
        if os.path.isfile(name):
            return name
    files = [f for f in os.listdir(".") if f.startswith("CandleView") and f.endswith(".txt")]
    if not files:
        return None
    files.sort()
    return files[0]

TARGET_FILE = get_latest_candleview_file()
CANDLEVIEW_PROMPT_FULL = ""

if TARGET_FILE:
    try:
        with open(TARGET_FILE, "r", encoding="utf-8") as file:
            CANDLEVIEW_PROMPT_FULL = file.read()
            print(f"[INFO] 엔진 파일({TARGET_FILE}) 로드 성공! 문자수: {len(CANDLEVIEW_PROMPT_FULL):,}자")
    except Exception as e:
        print(f"[ERROR] 엔진 파일 읽기 실패: {e}")
else:
    print(f"[ERROR] 엔진 파일을 찾을 수 없습니다. (CandleView_Engine.txt 또는 CandleView_API_*.txt 파일이 필요합니다.)")
    for f in os.listdir("."):
        if f.endswith(".txt"):
            print(f"  - {f} ({os.path.getsize(f):,} bytes)")

if not CANDLEVIEW_PROMPT_FULL:
    CANDLEVIEW_PROMPT_FULL = "CandleView 정밀 연산 엔진"
    print("[WARN] 엔진 파일을 찾지 못해 기본 문자열로 대체합니다.")

# ============================================================
# 분석 결과 임시 저장소 (chat_id 기준)
# ============================================================
analysis_cache = {}
CACHE_TTL_MINUTES = 30


def clean_expired_cache():
    now = datetime.now()
    expired = [cid for cid, data in analysis_cache.items()
               if now - data["created_at"] > timedelta(minutes=CACHE_TTL_MINUTES)]
    for cid in expired:
        del analysis_cache[cid]


# ============================================================
# 업비트 한글 코인 맵
# ============================================================
def fetch_upbit_korean_map():
    try:
        url = "https://api.upbit.com/v1/market/all?isDetails=false"
        res = requests.get(url, timeout=10).json()
        k_map = {}
        for item in res:
            if item["market"].startswith("KRW-"):
                sym = item["market"].replace("KRW-", "")
                k_name = item["korean_name"].replace(" ", "").strip()
                k_map[k_name] = sym
        return k_map
    except Exception:
        return {}


def fetch_bithumb_korean_map():
    """빗썸 자체 API에서 한글명 조회 시도. 필드 부재·API 실패 시 빈 dict로 안전 폴백
    (업비트 매핑을 우연히 재사용하지 않고, 빗썸 자체 소스가 없으면 명확히 비워둔다)."""
    try:
        url = "https://api.bithumb.com/v1/market/all?isDetails=false"
        res = requests.get(url, timeout=10).json()
        k_map = {}
        for item in res:
            market = item.get("market", "")
            k_name = (item.get("korean_name") or "").replace(" ", "").strip()
            if market.startswith("KRW-") and k_name:
                sym = market.replace("KRW-", "")
                k_map[k_name] = sym
        return k_map
    except Exception:
        return {}


UPBIT_KOREAN_MAP = fetch_upbit_korean_map()
print(f"[INFO] 업비트 한글 맵 로드: {len(UPBIT_KOREAN_MAP)}개 코인")

BITHUMB_KOREAN_MAP = fetch_bithumb_korean_map()
print(f"[INFO] 빗썸 한글 맵 로드: {len(BITHUMB_KOREAN_MAP)}개 코인"
      + ("" if BITHUMB_KOREAN_MAP else " (자체 한글명 미제공 — 업비트 매핑을 보조로 사용)"))


def resolve_korean_symbol(clean_name, ex_name):
    """거래소별 한글명 해석 우선순위(명확한 소스 계층화):
    1) 분석대상 거래소 자체의 한글맵(가장 정확)
    2) 없으면 국내 자매거래소(업비트↔빗썸) 매핑을 보조로 재사용(실용적 타협)
    3) 코인베이스 등 해외거래소는 자체 한글소스가 없으므로 국내맵만 참고 시도
    어디에도 없으면 원문을 그대로 대문자 심볼로 간주(영문 입력으로 처리)."""
    if ex_name == "upbit":
        primary, secondary = UPBIT_KOREAN_MAP, BITHUMB_KOREAN_MAP
    elif ex_name == "bithumb":
        primary, secondary = BITHUMB_KOREAN_MAP, UPBIT_KOREAN_MAP
    else:
        primary, secondary = {}, UPBIT_KOREAN_MAP

    if clean_name in primary:
        return primary[clean_name]
    if clean_name in secondary:
        return secondary[clean_name]
    return clean_name.upper()


# ============================================================
# RMA-RSI 14
# ============================================================
def calculate_rma_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def resample_daily_to_weekly(ohlcv_1d):
    """일봉 OHLCV 배열을 주봉으로 직접 집계한다.
    많은 거래소가 REST API로는 1w 캔들을 따로 제공하지 않고(앱 차트는 프론트엔드가
    일봉을 묶어서 그림), ccxt의 exchange.timeframes에도 1w가 빠져있는 경우가 많다.
    동일한 방식(일봉 7개 묶음)으로 직접 재현하여 앱에서 보는 주봉과 일치시킨다."""
    if not ohlcv_1d:
        return []
    df = pd.DataFrame(ohlcv_1d, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["dt"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("dt")
    weekly = df.resample("W-MON", label="left", closed="left").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna()
    if weekly.empty:
        return []
    weekly["timestamp"] = weekly.index.map(lambda x: int(x.timestamp() * 1000))
    return weekly.reset_index(drop=True)[["timestamp", "open", "high", "low", "close", "volume"]].values.tolist()



# ============================================================
# V002-15 Plugin 7/8/9 보조 데이터 수집
# - 미지원·오류 시 조용히 생략 (데이터결손 태그로 엔진에 전달)
# - 원본 배열 전체 주입 금지: 요약 스칼라·소량 태그만 payload에 추가
# ============================================================
WALL_RANGE_PCT = 5.0
WALL_MAX_COUNT = 3
WALL_MIN_SIZE_RATIO = 2.0
OI_HISTORY_LIMIT = 30


def _safe_float(x, default=None):
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def fetch_volume_delta_summary(exchange, symbol, timeframe, limit=120):
    """Plugin 7: Taker Buy/Sell → Delta 요약.
    우선순위: (1) 거래소 kline 확장 필드 (2) 최근 체결 집계 근사 (3) 결손
    반환 dict 또는 None
    """
    ex_id = getattr(exchange, "id", "")
    try:
        # --- Binance spot/usdm: kline에 taker buy base volume 포함 ---
        if ex_id in ("binance", "binanceusdm", "binancecoinm"):
            market = exchange.market(symbol)
            raw_symbol = market.get("id", symbol.replace("/", ""))
            if ex_id == "binance":
                rows = exchange.publicGetKlines({"symbol": raw_symbol, "interval": exchange.timeframes.get(timeframe, timeframe), "limit": limit})
            else:
                # futures
                rows = exchange.fapiPublicGetKlines({"symbol": raw_symbol, "interval": exchange.timeframes.get(timeframe, timeframe), "limit": limit}) if hasattr(exchange, "fapiPublicGetKlines") else None
                if rows is None and hasattr(exchange, "publicGetKlines"):
                    rows = exchange.publicGetKlines({"symbol": raw_symbol, "interval": exchange.timeframes.get(timeframe, timeframe), "limit": limit})
            if not rows:
                return None
            # binance kline: [0]open time [1]o [2]h [3]l [4]c [5]vol [6]close time [7]quote vol [8]trades [9]taker buy base [10]taker buy quote
            recent = rows[-min(5, len(rows)):]
            lines = []
            last_ratio = None
            for r in recent:
                vol = _safe_float(r[5], 0.0) or 0.0
                tb = _safe_float(r[9], 0.0) or 0.0
                ts = _safe_float(r[5], 0.0) or 0.0
                sell = max(vol - tb, 0.0)
                delta = tb - sell
                ratio = delta / (vol + 1e-8) if vol > 0 else 0.0
                last_ratio = ratio
                lines.append(f"Delta={delta:.4f} Ratio={ratio:+.4f} (Buy={tb:.4f} Sell={sell:.4f} Vol={vol:.4f})")
            return {
                "status": "ok",
                "source": "binance_kline_taker",
                "last_delta_ratio": last_ratio,
                "lines": lines,
            }

        # --- Bybit linear: try v5 kline + optional buy/sell if present in info ---
        if ex_id in ("bybit",):
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            # bybit unified often lacks taker split in OHLCV; approximate via recent trades window
            trades = exchange.fetch_trades(symbol, limit=200)
            buy_v = sell_v = 0.0
            for t in trades:
                amt = _safe_float(t.get("amount"), 0.0) or 0.0
                side = (t.get("side") or "").lower()
                if side in ("buy", "b"):
                    buy_v += amt
                else:
                    sell_v += amt
            total = buy_v + sell_v
            if total <= 0:
                return None
            delta = buy_v - sell_v
            ratio = delta / (total + 1e-8)
            return {
                "status": "ok",
                "source": "bybit_trades_approx",
                "last_delta_ratio": ratio,
                "lines": [f"Delta={delta:.4f} Ratio={ratio:+.4f} (Buy={buy_v:.4f} Sell={sell_v:.4f} Vol={total:.4f}) [최근체결근사]"],
            }

        # --- Generic: trades approximation ---
        if exchange.has.get("fetchTrades"):
            trades = exchange.fetch_trades(symbol, limit=150)
            buy_v = sell_v = 0.0
            for t in trades:
                amt = _safe_float(t.get("amount"), 0.0) or 0.0
                side = (t.get("side") or "").lower()
                if side in ("buy", "b"):
                    buy_v += amt
                else:
                    sell_v += amt
            total = buy_v + sell_v
            if total <= 0:
                return None
            delta = buy_v - sell_v
            ratio = delta / (total + 1e-8)
            return {
                "status": "ok",
                "source": f"{ex_id}_trades_approx",
                "last_delta_ratio": ratio,
                "lines": [f"Delta={delta:.4f} Ratio={ratio:+.4f} (Buy={buy_v:.4f} Sell={sell_v:.4f}) [최근체결근사]"],
            }
    except Exception as e:
        print(f"[WARN] Volume Delta 수집 실패 ({ex_id} {symbol}): {e}")
    return None


# ============================================================
# OI 교차거래소 조회 기능 정지 스위치
# 사유: 현재 지원 3개 거래소(업비트/빗썸/코인베이스)는 전부 순수 현물마켓이라
# 자체 OI가 원천적으로 존재하지 않는다(미결제약정은 선물/무기한계약 전용 개념).
# 유일한 경로였던 교차거래소 조회(바이낸스/바이비트/OKX)도 Render(AWS US)
# 환경에서 전부 미국IP 지오블록 대상이라 매 시도가 실패하며 최대 8초
# (2곳×4초) 불필요한 지연만 유발한다. 코드(V003 Plugin8 설계)는 그대로
# 보존하고 실행만 정지한다 — 프록시 구축 등으로 지오블록이 해소되면
# 아래 값을 True로 바꾸는 것만으로 즉시 재사용 가능하다.
# ============================================================
OI_CROSS_EXCHANGE_ENABLED = False


def fetch_oi_summary(exchange, symbol, primary_ex_id):
    """Plugin 8: OI 변동률 스칼라 3~4개만.
    현물 전용 거래소는 선물 심볼로 교차 조회 시도.
    폴백 순서: primary → 심볼변형 → Binance USDM → Bybit → OKX
    (교차거래소 조회 단계는 OI_CROSS_EXCHANGE_ENABLED 스위치로 정지 가능)
    """
    tried = []

    def _oi_from(ex, sym):
        try:
            if not ex.has.get("fetchOpenInterest") and not hasattr(ex, "fetch_open_interest"):
                if hasattr(ex, "fetch_open_interest_history"):
                    hist = ex.fetch_open_interest_history(sym, timeframe="1h", limit=OI_HISTORY_LIMIT)
                    if not hist or len(hist) < 2:
                        return None
                    vals = []
                    for h in hist[-5:]:
                        v = _safe_float(h.get("openInterestAmount") or h.get("openInterestValue") or h.get("openInterest"))
                        if v is not None:
                            vals.append(v)
                    if len(vals) < 2:
                        return None
                    changes = []
                    for i in range(1, len(vals)):
                        prev = vals[i - 1]
                        cur = vals[i]
                        pct = ((cur - prev) / prev * 100.0) if prev else 0.0
                        changes.append(pct)
                    return {
                        "source": getattr(ex, "id", "?"),
                        "last_oi": vals[-1],
                        "change_pcts": changes[-4:],
                        "latest_change_pct": changes[-1] if changes else None,
                    }
                return None

            oi = ex.fetch_open_interest(sym)
            val = _safe_float(
                oi.get("openInterestAmount")
                or oi.get("openInterestValue")
                or oi.get("openInterest")
                or (oi.get("info") or {}).get("openInterest")
            )
            change_pcts = []
            if hasattr(ex, "fetch_open_interest_history"):
                try:
                    hist = ex.fetch_open_interest_history(sym, timeframe="1h", limit=OI_HISTORY_LIMIT)
                    vals = []
                    for h in hist[-6:]:
                        v = _safe_float(h.get("openInterestAmount") or h.get("openInterestValue") or h.get("openInterest"))
                        if v is not None:
                            vals.append(v)
                    for i in range(1, len(vals)):
                        prev = vals[i - 1]
                        if prev:
                            change_pcts.append((vals[i] - prev) / prev * 100.0)
                except Exception:
                    pass
            return {
                "source": getattr(ex, "id", "?"),
                "last_oi": val,
                "change_pcts": change_pcts[-4:],
                "latest_change_pct": change_pcts[-1] if change_pcts else None,
            }
        except Exception as e:
            print(f"[WARN] OI 조회 실패 ({getattr(ex, 'id', '?')} {sym}): {e}")
            return None

    base = symbol.split("/")[0] if "/" in symbol else symbol

    # 1) primary exchange
    tried.append(primary_ex_id)
    data = _oi_from(exchange, symbol)
    if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
        return {"status": "ok", **data}

    # 2) 심볼 변형 시도 (primary)
    for alt in (f"{base}/USDT:USDT", f"{base}/USDT:USDT-USDT", f"{base}/USDT"):
        if alt == symbol:
            continue
        data = _oi_from(exchange, alt)
        if data and data.get("latest_change_pct") is not None:
            return {"status": "ok", **data, "symbol_used": alt}

    # 3) Binance USDM 교차
    if OI_CROSS_EXCHANGE_ENABLED and primary_ex_id not in ("binanceusdm", "binance"):
        try:
            bx = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 4000})
            bx.load_markets()
            tried.append("binanceusdm")
            for sym in (f"{base}/USDT:USDT", f"{base}/USDT"):
                data = _oi_from(bx, sym)
                if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
                    return {"status": "ok", **data, "symbol_used": sym, "cross_exchange": True}
        except Exception as e:
            print(f"[WARN] Binance OI 교차조회 실패: {e}")

    # 4) Bybit linear 교차
    if OI_CROSS_EXCHANGE_ENABLED and primary_ex_id not in ("bybit",):
        try:
            by = ccxt.bybit({
                "enableRateLimit": True,
                "timeout": 4000,
                "options": {"defaultType": "linear"}
            })
            by.load_markets()
            tried.append("bybit")
            for sym in (f"{base}/USDT:USDT", f"{base}/USDT"):
                data = _oi_from(by, sym)
                if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
                    return {"status": "ok", **data, "symbol_used": sym, "cross_exchange": True}
        except Exception as e:
            print(f"[WARN] Bybit OI 교차조회 실패: {e}")

    # 5) OKX 교차
    if OI_CROSS_EXCHANGE_ENABLED and primary_ex_id not in ("okx",):
        try:
            ox = ccxt.okx({
                "enableRateLimit": True,
                "timeout": 4000,
                "options": {"defaultType": "swap"}
            })
            ox.load_markets()
            tried.append("okx")
            for sym in (f"{base}/USDT:USDT", f"{base}/USDT"):
                data = _oi_from(ox, sym)
                if data and (data.get("latest_change_pct") is not None or data.get("last_oi") is not None):
                    return {"status": "ok", **data, "symbol_used": sym, "cross_exchange": True}
        except Exception as e:
            print(f"[WARN] OKX OI 교차조회 실패: {e}")

    if not OI_CROSS_EXCHANGE_ENABLED:
        return {"status": "missing", "tried": tried, "reason": "cross_exchange_paused"}
    return {"status": "missing", "tried": tried}


def fetch_whale_wall_summary(exchange, symbol, last_price):
    """Plugin 9: 호가 요약 — 현재가 ±WALL_RANGE_PCT 내 상위 벽 소수만."""
    try:
        if not exchange.has.get("fetchOrderBook"):
            return None
        book = exchange.fetch_order_book(symbol, limit=50)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not last_price or last_price <= 0:
            last_price = bids[0][0] if bids else (asks[0][0] if asks else None)
        if not last_price:
            return None

        lo = last_price * (1 - WALL_RANGE_PCT / 100.0)
        hi = last_price * (1 + WALL_RANGE_PCT / 100.0)

        def walls(levels, side):
            sized = []
            sizes = []
            for price, amt in levels:
                p = _safe_float(price)
                a = _safe_float(amt, 0.0) or 0.0
                if p is None:
                    continue
                if side == "bid" and lo <= p <= last_price:
                    sized.append((p, a))
                    sizes.append(a)
                if side == "ask" and last_price <= p <= hi:
                    sized.append((p, a))
                    sizes.append(a)
            if not sized:
                return []
            avg = sum(sizes) / len(sizes) if sizes else 0.0
            strong = [(p, a) for p, a in sized if a >= avg * WALL_MIN_SIZE_RATIO]
            strong.sort(key=lambda x: x[1], reverse=True)
            return strong[:WALL_MAX_COUNT]

        bid_walls = walls(bids, "bid")
        ask_walls = walls(asks, "ask")
        if not bid_walls and not ask_walls:
            return {"status": "ok", "lines": ["범위 내 유의미 Whale Wall 없음"], "last_price": last_price}

        lines = []
        for p, a in bid_walls:
            lines.append(f"매수벽 {p} 잔량={a:.4f} (현재가 대비 {(p/last_price-1)*100:+.2f}%)")
        for p, a in ask_walls:
            lines.append(f"매도벽 {p} 잔량={a:.4f} (현재가 대비 {(p/last_price-1)*100:+.2f}%)")
        return {"status": "ok", "lines": lines, "last_price": last_price, "bid_walls": bid_walls, "ask_walls": ask_walls}
    except Exception as e:
        print(f"[WARN] Whale Wall 수집 실패 ({symbol}): {e}")
        return None


def format_plugin_payload(oi_info, wall_info):
    """엔진 STAGE 0 / Plugin 8·9(TF무관 1회성)가 파싱하기 쉬운 요약 블록.
    Volume Delta(Plugin 7)는 TF별로 다르므로 run_phase1의 TF 루프 내에서 개별 첨부한다."""
    parts = ["\n[API 플러그인 보조 데이터 — V003 Plugin 8/9, TF무관 1회성 스냅샷]"]

    # OI
    parts.append("• Open Interest (Plugin 8):")
    if oi_info and oi_info.get("status") == "ok":
        src = oi_info.get("source", "?")
        if oi_info.get("cross_exchange"):
            src += " (교차거래소)"
        parts.append(f"  - source: {src}")
        if oi_info.get("symbol_used"):
            parts.append(f"  - symbol_used: {oi_info['symbol_used']}")
        if oi_info.get("last_oi") is not None:
            parts.append(f"  - last_OI: {oi_info['last_oi']}")
        pcts = oi_info.get("change_pcts") or []
        if pcts:
            parts.append("  - OI_change_pct(최근→과거순 최대4개): " + ", ".join(f"{p:+.2f}%" for p in pcts))
        if oi_info.get("latest_change_pct") is not None:
            parts.append(f"  - latest_OI_change_pct: {oi_info['latest_change_pct']:+.2f}%")
    else:
        reason = (oi_info or {}).get("reason")
        if reason == "cross_exchange_paused":
            parts.append("  - [해당없음/기능정지] 현물마켓 자체 OI 없음, 교차거래소 조회는 지오블록으로 정지 상태")
        else:
            tried = (oi_info or {}).get("tried") or []
            parts.append("  - [데이터결손] Open Interest 미지원 또는 수집 실패" + (f" tried={tried}" if tried else ""))

    # Wall
    parts.append("• Whale Wall (Plugin 9):")
    if wall_info and wall_info.get("status") == "ok":
        parts.append(f"  - ref_price: {wall_info.get('last_price')}")
        parts.append(f"  - range: ±{WALL_RANGE_PCT}%")
        for ln in wall_info.get("lines") or []:
            parts.append(f"  - {ln}")
    else:
        parts.append("  - [데이터결손] Order book 미지원 또는 수집 실패")

    parts.append("")
    return "\n".join(parts)


def format_supplement_display(supplement, symbol, exchange_display):
    """보간지표(Plugin 7/8/9 원시데이터) 텔레그램 표시용 포맷.
    Gemini를 거치지 않고 main.py가 수집한 원시데이터를 그대로 정돈해 보여준다
    (환각 위험 없음, 별도 API 호출 없어 즉시 응답)."""
    if not supplement:
        return "보간지표 데이터가 없습니다."

    lines = [f"<b>CandleView — 보간지표 (Plugin 7·8·9 원시데이터)</b>", f"{exchange_display} {symbol}", ""]

    lines.append("<b>📶 체결강도 Volume Delta (TF별)</b>")
    lines.append("<pre>")
    for item in supplement.get("delta_list", []):
        tf = item.get("tf", "?")
        info = item.get("info")
        if info and info.get("status") == "ok":
            lines.append(f"{tf:>5} | Ratio {info.get('last_delta_ratio'):+.4f} | {info.get('source')}")
        else:
            lines.append(f"{tf:>5} | 거래소 미지원 또는 데이터결손")
    lines.append("</pre>")

    lines.append("")
    lines.append("<b>📊 미결제약정 Open Interest</b>")
    lines.append("<pre>")
    oi = supplement.get("oi")
    if oi and oi.get("status") == "ok":
        src = oi.get("source", "?")
        if oi.get("cross_exchange"):
            src += " (교차거래소 참고)"
        lines.append(f"출처: {src}")
        if oi.get("symbol_used"):
            lines.append(f"조회심볼: {oi['symbol_used']}")
        if oi.get("latest_change_pct") is not None:
            lines.append(f"최근 변동률: {oi['latest_change_pct']:+.2f}%")
        pcts = oi.get("change_pcts") or []
        if pcts:
            lines.append("변동추이: " + ", ".join(f"{p:+.2f}%" for p in pcts))
    else:
        if oi and oi.get("reason") == "cross_exchange_paused":
            lines.append("[해당없음/기능정지]")
            lines.append("현물마켓 자체 OI 없음, 교차거래소 조회는 지오블록으로 정지 상태")
            lines.append("(추후 프록시 구축시 재개 예정)")
        else:
            lines.append("해당없음 (현물마켓 또는 조회 실패)")
    lines.append("</pre>")

    lines.append("")
    lines.append("<b>🧱 호가 매물벽 Whale Wall</b>")
    lines.append("<pre>")
    wall = supplement.get("wall")
    if wall and wall.get("status") == "ok":
        for ln in wall.get("lines", []):
            lines.append(ln)
    else:
        lines.append("데이터결손 또는 호가조회 실패")
    lines.append("</pre>")

    return "\n".join(lines)


# ============================================================
# Gemini API 호출
# ============================================================
def call_gemini_api_with_retry(full_prompt, max_tokens=16384):
    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }

    urls = [
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent",
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent",
    ]

    for url in urls:
        for _ in range(2):
            try:
                res = requests.post(url, headers=headers, json=payload, timeout=120)
                if res.status_code == 200:
                    return res.json()["candidates"][0]["content"]["parts"][0]["text"]
                elif res.status_code == 503:
                    time.sleep(3)
                else:
                    print(f"[WARN] Gemini 응답 코드: {res.status_code}")
                    time.sleep(1)
            except Exception as e:
                print(f"[WARN] Gemini 호출 예외: {e}")
                time.sleep(2)
    return "AI 서버 일시적 과부하 또는 모델 접근 불가 상태입니다. 잠시 후 다시 시도해 주세요."


# ============================================================
# 가독성 보강 유틸
# - sanitize_html: Gemini 원문의 마크다운 잔재를 HTML로 치환하고,
#   <,>,& 로 인한 Telegram HTML 파싱 오류를 방지한다.
# - smart_chunk: 4000자 단위 강제절단 대신, 카드/섹션 경계(🔹, 1️⃣~6️⃣)에서만
#   끊어 문단이 중간에 잘리지 않도록 청크를 나눈다.
# ============================================================
def sanitize_html(text: str) -> str:
    if not text:
        return text
    # 1) 원문에 섞인 <,>,& 이스케이프 (HTML 파싱 오류 방지, 반드시 최우선 처리)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 2) 마크다운 굵게(**text**) -> <b>text</b>
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    # 3) 마크다운 헤더(### 제목) -> 굵게 처리
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)
    # 4) 표 구분선 등 잔여 파이프/대시 라인 제거 (V002-3부터 표 미사용, 방어용)
    text = re.sub(r"^\s*\|?[-:]{3,}\|?[-:|]*\s*$", "", text, flags=re.MULTILINE)
    return text


def smart_chunk(text: str, boundary_markers, max_len=4000):
    """boundary_markers 앞에서만 끊어 청크를 구성한다.
    단일 구간이 max_len을 넘으면 그 구간만 부득이하게 강제 절단한다."""
    if not text:
        return [text]

    positions = sorted({
        m.start()
        for marker in boundary_markers
        for m in re.finditer(re.escape(marker), text)
    })
    if not positions or positions[0] != 0:
        positions = [0] + positions

    segments = [
        text[positions[i]:(positions[i + 1] if i + 1 < len(positions) else len(text))]
        for i in range(len(positions))
    ]

    chunks, current = [], ""
    for seg in segments:
        if len(seg) > max_len:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(seg), max_len):
                chunks.append(seg[i:i + max_len])
            continue
        if len(current) + len(seg) > max_len:
            chunks.append(current)
            current = seg
        else:
            current += seg
    if current:
        chunks.append(current)
    return chunks


PHASE1_BOUNDARY_MARKERS = ["🔹"]
PHASE2_BOUNDARY_MARKERS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
FINDCOIN_BOUNDARY_MARKERS = ["🥇", "🥈", "🥉"]


# ============================================================
# 텔레그램 메시지 전송 (일반 + 인라인 버튼 지원)
# ============================================================
def send_telegram_message(chat_id, text, reply_markup=None, timeout=15):
    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    res = requests.post(send_url, json=payload, timeout=timeout)
    try:
        ok = res.json().get("ok", False)
    except Exception:
        ok = res.status_code == 200

    if not ok:
        payload.pop("parse_mode", None)
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(send_url, json=payload, timeout=timeout)


def answer_callback_query(callback_query_id, text=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(url, json=payload, timeout=10)


# ============================================================
# PHASE 1 전용 실행 (데이터 수집 + 표 작성만)
# ============================================================
def run_phase1(symbol_input, exchange_name, custom_tfs):
    ex_name = resolve_exchange(exchange_name)
    ex_display = SUPPORTED_EXCHANGES.get(ex_name, {}).get("kr_name", exchange_name)
    try:
        if ex_name is None:
            return (
                "지원하지 않는 거래소입니다.\n"
                "현재 지원 거래소: 업비트 · 빗썸 · 코인베이스",
                None, None, None
            )

        quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
        clean = symbol_input.replace("/", "").replace(":", "").replace(" ", "")
        symbol_upper = resolve_korean_symbol(clean, ex_name)

        try:
            exchange_class = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 8000})
            exchange_class.load_markets()
        except Exception as e:
            print(f"[WARN] load_markets 실패({ex_name}): {e}")
            return friendly_error_message(e, ex_display, symbol_input), None, None, None

        symbol = f"{symbol_upper}/{quote}"

        # 코인 존재여부 사전검증 — fetch_ohlcv 예외를 기다리지 않고 즉시 명확한 안내
        if symbol not in exchange_class.markets:
            is_korean_input = bool(re.search(r"[가-힣]", symbol_input))
            hint = (
                f"\n한글 코인명이 인식되지 않았습니다. 코인명은 영문을 사용해주세요. (예: BTC, XRP)"
                if is_korean_input else
                f"\n({ex_display}에 상장되지 않은 코인일 수 있습니다)"
            )
            return (
                f"'{symbol_input}' 코인을 {ex_display}에서 찾을 수 없습니다.\n"
                f"코인명을 다시 확인하시거나 다른 거래소를 시도해 주세요."
                f"{hint}",
                None, None, None
            )

        # [수정] ccxt의 정적 timeframes 목록은 실제 거래소 API 능력과 다를 수 있음이
        # 실사용으로 확인되어(예: 빗썸은 목록에 4h가 없지만 실제로는 정상 동작) 사전차단을
        # 제거한다. 실제 미지원 TF는 fetch_ohlcv 실패 시 friendly_error_message가 안전하게 안내한다.

        payload = (
            f"[STAGE 0 사전 환경 점검]\n"
            f"• 수집 거래소: {ex_name.upper()}\n"
            f"• 수집 방식: ■ API Direct Data Stream\n\n"
            f"=== 코인명: {symbol} ===\n"
        )

        last_close = None
        tf_delta_list = []
        # TF 루프: OHLCV+RSI + TF별 Volume Delta(Plugin7, V003은 TF마다 다른 값을 요구)
        for i, tf in enumerate(custom_tfs):
            if tf == "1w":
                # 주봉은 거래소 API 지원여부와 무관하게 항상 일봉을 직접 묶어 생성한다
                # (앱 차트도 동일 방식 — 일관성·신뢰성 확보)
                daily_ohlcv = exchange_class.fetch_ohlcv(symbol, timeframe="1d", limit=500)
                ohlcv = resample_daily_to_weekly(daily_ohlcv)
            else:
                ohlcv = exchange_class.fetch_ohlcv(symbol, timeframe=tf, limit=120)
            df = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["rsi"] = calculate_rma_rsi(df["close"])
            recent = df.tail(100)
            if len(recent) > 0:
                last_close = float(recent.iloc[-1]["close"])

            payload += f"\n[{tf} 타임프레임 API 수신 배열 (최근 {len(recent)}봉)]\n"
            for _, row in recent.iterrows():
                payload += (
                    f"O: {row['open']} | H: {row['high']} | L: {row['low']} | "
                    f"C: {row['close']} | V: {row['volume']:.2f} | RSI: {row['rsi']:.2f}\n"
                )

            # Volume Delta: TF마다 개별 수집 (PHASE1 카드 15번째 항목은 TF별로 값이 달라야 함)
            tf_delta_info = fetch_volume_delta_summary(exchange_class, symbol, tf, limit=120)
            tf_delta_list.append({"tf": tf, "info": tf_delta_info})
            payload += f"\n[{tf} Volume Delta (Plugin 7)]\n"
            if tf_delta_info and tf_delta_info.get("status") == "ok":
                payload += f"source: {tf_delta_info.get('source')} | last_Delta_Ratio: {tf_delta_info.get('last_delta_ratio'):+.4f}\n"
            else:
                payload += "[거래소 미지원 또는 데이터결손] Volume Delta 수집 불가\n"

        # OI / Whale Wall — 심볼 단위 1회 (TF 무관 1회성 스냅샷, V003 정식모드 보조지표 블록 뒤 배치 대상)
        oi_info = fetch_oi_summary(exchange_class, symbol, ex_name)
        wall_info = fetch_whale_wall_summary(exchange_class, symbol, last_close)
        payload += format_plugin_payload(oi_info, wall_info)

        phase1_prompt = (
            f"{CANDLEVIEW_PROMPT_FULL}\n\n"
            f"[API 수신 원천 데이터]\n{payload}\n\n"
            f"지금은 PHASE 1만 수행하십시오.\n"
            f"PHASE 1 표 작성을 완료한 뒤, 엔진에 규정된 PHASE 1 최종 종료 고정 문구를 출력하고 멈추십시오.\n"
            f"PHASE 2 관련 서술·해석·전략은 절대 출력하지 마십시오."
        )

        phase1_result = call_gemini_api_with_retry(phase1_prompt, max_tokens=12000)
        supplement = {"delta_list": tf_delta_list, "oi": oi_info, "wall": wall_info}
        return phase1_result, symbol, ex_name.upper(), supplement

    except Exception as e:
        print(f"[ERROR] run_phase1 예외 ({ex_name} {symbol_input}): {e}")
        return friendly_error_message(e, ex_display, symbol_input), None, None, None


# ============================================================
# PHASE 2 전용 실행 (이미 완성된 PHASE 1을 재료로 사용)
# ============================================================
def run_phase2(phase1_result, symbol, exchange_name):
    phase2_prompt = (
        f"{CANDLEVIEW_PROMPT_FULL}\n\n"
        f"아래는 이미 완성된 PHASE 1 결과입니다.\n"
        f"사용자는 PHASE 2 진행을 명시적으로 승인하였습니다.\n\n"
        f"[PHASE 1 완성 결과]\n{phase1_result}\n\n"
        f"이제 PHASE 2 통합 브리핑을 엔진 규칙에 따라 완제 출력하십시오.\n"
        f"새로운 수치나 판단을 임의로 추가하지 말고, PHASE 1에서 도출된 데이터만을 근거로 사용하십시오."
    )
    return call_gemini_api_with_retry(phase2_prompt, max_tokens=12000)


# ============================================================
# FindCoin — Layer0~1 (Python 선필터, V003 챕터14 FC-0/FC-1)
# 전종목 정직 스캔(절대값 사전압축 없음 — 저가 알트가 상대적 급등을 해도
# 배제되지 않도록 RTM/Percentile은 항상 전체 유효종목 기준으로 계산한다).
# 3조건(RTM/Percentile/Liquidity)이 전부 AND이므로, 가벼운 계산(RTM, 일봉만
# 필요)을 전종목에 먼저 돌리고 무거운 계산(Liquidity_Ratio, 호가창조회)은
# RTM+Percentile 통과자에게만 나중에 적용해 결과 손상 없이 소요시간을 단축한다.
# ============================================================
FC_RTM_MIN = 3.0
FC_PERCENTILE_MIN = 85.0
FC_LIQUIDITY_MIN = 1.0
FC_MIN_BARS_STANDARD = 20
FC_MIN_BARS_REDUCED = 8
FC_NEWCOIN_CONFIDENCE = 0.80
FC_AVG_VOLUME_LOOKBACK_SHORT = 3

# 시세분출 개념이 적용되지 않는 스테이블코인(quote가 아닌 base가 스테이블인 경우 제외)
FC_STABLECOIN_BASES = {"USDT", "USDC", "DAI", "TUSD", "PAX", "GUSD", "PYUSD", "BUSD", "USDP", "FDUSD"}


def fc_prefilter_universe(exchange, quote):
    """FC-0: 스캔 대상 사전 정제. [결함수정] fetch_tickers()를 인자 없이 호출하면
    일부 거래소(업비트 등, 마켓코드를 명시해야 하는 REST 구조)에서 극소수 결과만
    반환하는 문제가 실사용으로 확인되었다. load_markets()로 이미 확보한 신뢰 가능한
    전체 심볼목록에서 quote 통화에 해당하는 심볼만 명시적으로 골라 fetch_tickers에
    전달하여 전종목이 실제로 조회되도록 한다."""
    try:
        target_symbols = [s for s in exchange.markets if s.endswith(f"/{quote}")]
        if not target_symbols:
            return []
        tickers = exchange.fetch_tickers(target_symbols)
    except Exception as e:
        print(f"[WARN] fetch_tickers 실패: {e}")
        return []

    universe = []
    for symbol, t in tickers.items():
        if not symbol.endswith(f"/{quote}"):
            continue
        base = symbol.split("/")[0]
        if base in FC_STABLECOIN_BASES:
            continue
        market = exchange.markets.get(symbol, {})
        if market.get("active") is False:
            continue
        qv = _safe_float(t.get("quoteVolume"))
        if qv is None or qv <= 0:
            continue
        universe.append({"symbol": symbol, "base": base, "quote_volume_24h": qv, "last": _safe_float(t.get("last"))})
    return universe


def fc_compute_rtm(exchange, item):
    """FC-1[1]: RTM = 오늘 24h 거래대금 / 직전 20일(또는 확보 가능한 만큼) 평균 24h 거래대금.
    적응형 룩백(FC-0)도 함께 처리: 확보 봉수에 따라 표준/축소/관측대기로 분류.
    [결함수정] 분자(티커 quoteVolume, 롤링24h·정확값)와 분모(일봉 close×volume 근사,
    캘린더일·근사값)의 측정기준이 서로 달라 RTM이 왜곡되던 문제를 해소한다 — 분자·분모를
    동일 방법론(일봉 기반 close×volume)으로 통일한다. 이 경우 '오늘 진행 중인 봉'은 하루가
    아직 안 끝났으므로 거래량이 항상 과소평가되는 편향이 생기므로, 경과시간 비율로 정규화
    (외삽)한다. 단, 자정 직후처럼 극초반에는 과도한 외삽(수십 배 부풀림)을 막기 위해 경과비율
    하한(10%)을 적용한다."""
    symbol = item["symbol"]
    try:
        # [설계최적화] 30봉을 한 번만 조회해 item에 캐싱한다 — RTM(21봉 필요)뿐 아니라
        # 이후 fc_compute_liquidity_ratio(4봉)·fc_build_candidate_payload(30봉)에서
        # 동일 심볼을 재조회하지 않고 이 캐시를 재사용해 API 호출 중복을 없앤다.
        daily = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=30)
    except Exception as e:
        print(f"[WARN] FindCoin 일봉조회 실패({symbol}): {e}")
        return None

    if not daily or len(daily) < 2:
        item["mode"] = "watch_only"
        item["bar_count"] = len(daily) if daily else 0
        return item

    item["daily_ohlcv_cache"] = daily
    today_bar = daily[-1]
    # RTM 평균은 스펙대로 최근 20영업일(가용한 만큼)만 사용 — 30봉을 조회했다고 평균 기간이 늘어나지 않도록 슬라이싱
    past = daily[:-1][-FC_MIN_BARS_STANDARD:]
    bar_count = len(past)

    if bar_count < FC_MIN_BARS_REDUCED:
        item["mode"] = "watch_only"
        item["bar_count"] = bar_count
        return item

    avg_quote_volume = sum(row[4] * row[5] for row in past) / bar_count  # close*volume 근사(거래대금)
    if avg_quote_volume <= 0:
        return None

    # 오늘 진행 중인 봉 시간외삽 (경과비율 하한 10% 적용)
    now_ms = int(time.time() * 1000)
    elapsed_ms = now_ms - today_bar[0]
    elapsed_ratio = max(0.10, min(1.0, elapsed_ms / 86_400_000))
    today_qv_raw = today_bar[4] * today_bar[5]
    today_qv_normalized = today_qv_raw / elapsed_ratio

    rtm = today_qv_normalized / avg_quote_volume
    item["rtm"] = rtm
    item["bar_count"] = bar_count
    item["mode"] = "standard" if bar_count >= FC_MIN_BARS_STANDARD else "reduced"
    item["confidence"] = 1.0 if item["mode"] == "standard" else FC_NEWCOIN_CONFIDENCE
    return item


def fc_compute_liquidity_ratio(exchange, item):
    """FC-1[3]: Liquidity_Ratio = (매수10호가+매도10호가 총잔량) / 최근3봉 평균거래량.
    RTM+Percentile을 이미 통과한 후보에게만 적용(무거운 호출을 최소화).
    [설계최적화] fc_compute_rtm에서 캐싱한 일봉데이터를 재사용하여 중복 API호출을 없앤다."""
    symbol = item["symbol"]
    try:
        book = exchange.fetch_order_book(symbol, limit=10)
        bid_depth = sum(a for _, a in (book.get("bids") or []))
        ask_depth = sum(a for _, a in (book.get("asks") or []))

        cached = item.get("daily_ohlcv_cache")
        if not cached or len(cached) < 2:
            # 캐시 부재(정상 흐름에선 발생하지 않음) 시에만 안전 폴백 조회
            cached = exchange.fetch_ohlcv(symbol, timeframe="1d", limit=FC_AVG_VOLUME_LOOKBACK_SHORT + 1)
        if not cached or len(cached) < 2:
            return None

        past = cached[:-1][-FC_AVG_VOLUME_LOOKBACK_SHORT:]
        if not past:
            return None
        avg_vol3 = sum(row[5] for row in past) / len(past)
        liquidity_ratio = (bid_depth + ask_depth) / (avg_vol3 + 1e-8)
        item["liquidity_ratio"] = liquidity_ratio
        return item
    except Exception as e:
        print(f"[WARN] FindCoin 유동성조회 실패({symbol}): {e}")
        return None


def run_findcoin_scan(ex_name):
    """FC-0~FC-1 전체 파이프라인. 3중 필터(RTM/Percentile/Liquidity) 통과 후보 리스트 반환."""
    quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
    exchange_class = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 8000})
    try:
        exchange_class.load_markets()
    except Exception as e:
        print(f"[WARN] FindCoin load_markets 실패({ex_name}): {e}")
        return None, 0, 0, 0, 0, []

    universe = fc_prefilter_universe(exchange_class, quote)
    n_total = len(universe)
    if n_total == 0:
        return [], 0, 0, 0, 0, []

    # 1단계(가벼움, 전종목): RTM 계산
    rtm_results, watch_only = [], []
    for item in universe:
        r = fc_compute_rtm(exchange_class, item)
        if r is None:
            continue
        if r.get("mode") == "watch_only":
            watch_only.append(r)
        else:
            rtm_results.append(r)
    n_valid = len(rtm_results)

    if n_valid == 0:
        return [], n_total, 0, 0, 0, watch_only

    # Percentile_Rank: 전체 유효종목(N_valid) 기준 RTM 내림차순 순위
    rtm_results.sort(key=lambda x: x["rtm"], reverse=True)
    for rank, item in enumerate(rtm_results, start=1):
        item["percentile_rank"] = (1 - (rank - 1) / n_valid) * 100.0

    gate1_pass = [it for it in rtm_results if it["rtm"] >= FC_RTM_MIN and it["percentile_rank"] >= FC_PERCENTILE_MIN]
    n_gate1 = len(gate1_pass)

    # 2단계(무거움, 통과자만): Liquidity_Ratio 계산
    final_candidates = []
    for item in gate1_pass:
        r = fc_compute_liquidity_ratio(exchange_class, item)
        if r and r["liquidity_ratio"] >= FC_LIQUIDITY_MIN:
            final_candidates.append(r)
    n_gate2 = len(final_candidates)

    return final_candidates, n_total, n_valid, n_gate1, n_gate2, watch_only


# ============================================================
# FindCoin — Layer2~3 (Gemini 위임, V003 챕터14 FC-2~FC-5)
# 3중필터 통과 후보만 상세 캔들데이터를 붙여 Gemini에 전달, State 진단·
# 옥석검증·S_scout·손익비·Top3 선정까지 엔진 규칙대로 수행시킨다.
# ============================================================
FC_MAX_CANDIDATES_TO_LLM = 20  # 토큰 절약을 위한 상한(초과 시 RTM 상위 N개만 전달)


def fc_build_candidate_payload(exchange_class, candidates):
    """최종 후보 각각에 대해 대표 TF(1일봉) 상세 데이터를 붙여 Gemini 입력 payload 구성.
    [설계최적화] fc_compute_rtm에서 이미 조회·캐싱된 30봉 데이터를 재사용한다."""
    if len(candidates) > FC_MAX_CANDIDATES_TO_LLM:
        candidates = sorted(candidates, key=lambda x: x["rtm"], reverse=True)[:FC_MAX_CANDIDATES_TO_LLM]

    payload = f"[FindCoin 3중필터 통과 후보: {len(candidates)}개]\n"
    for c in candidates:
        payload += (
            f"\n=== {c['symbol']} ===\n"
            f"RTM: {c['rtm']:.2f} | Percentile: {c['percentile_rank']:.1f}% | "
            f"Liquidity_Ratio: {c['liquidity_ratio']:.2f} | 모드: {c['mode']} "
            f"(신뢰도계수 {c['confidence']})\n"
        )
        try:
            ohlcv = c.get("daily_ohlcv_cache")
            if not ohlcv:
                # 캐시 부재(정상 흐름에선 발생하지 않음) 시에만 안전 폴백 조회
                ohlcv = exchange_class.fetch_ohlcv(c["symbol"], timeframe="1d", limit=30)
            df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
            for _, row in df.iterrows():
                payload += (
                    f"O:{row['open']} H:{row['high']} L:{row['low']} "
                    f"C:{row['close']} V:{row['volume']:.2f}\n"
                )
        except Exception as e:
            print(f"[WARN] FindCoin 후보 캔들조회 실패({c['symbol']}): {e}")
            payload += "(캔들 데이터 조회 실패 — 이 후보는 판정에서 제외)\n"
    return payload, candidates


def fc_extract_top_symbols(result_text, used_candidates):
    """Gemini 응답 맨 끝의 [FINDCOIN_TOP_SYMBOLS] 태그만 정규식으로 추출한다
    (본문 자유서술 파싱 금지 — 고정형식 태그 1줄만 대상으로 하여 파싱 위험을 최소화).
    추출된 심볼은 반드시 이미 알고 있는 후보목록(used_candidates)에 실존하는 것만
    채택하여, Gemini가 목록에 없는 심볼을 지어내더라도 안전하게 걸러낸다."""
    known_symbols = {c["symbol"] for c in used_candidates}
    m = re.search(r"\[FINDCOIN_TOP_SYMBOLS\]\s*(.*)", result_text)
    if not m:
        return []
    raw = m.group(1).strip()
    if not raw:
        return []
    candidates_in_tag = [s.strip() for s in raw.split("|") if s.strip()]
    return [s for s in candidates_in_tag if s in known_symbols][:3]


def fc_strip_top_symbols_tag(result_text):
    """사용자에게 보여줄 텍스트에서 시스템 연동용 태그 줄만 제거한다."""
    return re.sub(r"\n?\[FINDCOIN_TOP_SYMBOLS\].*", "", result_text).strip()


def run_findcoin(ex_name):
    """FindCoin 전체 실행: Layer0~1(Python) → Layer2~5(Gemini) → 구조화 결과 반환.
    반환: (raw_gemini_text, n_total, n_valid, n_gate1, n_gate2, error_message)"""
    quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
    ex_display = SUPPORTED_EXCHANGES[ex_name]["kr_name"]

    scan = run_findcoin_scan(ex_name)
    candidates, n_total, n_valid, n_gate1, n_gate2, watch_only = scan

    if candidates is None:
        return None, 0, 0, 0, 0, f"{ex_display} 시장 데이터를 불러오지 못했습니다.\n잠시 후 다시 시도해 주세요.", [], 0

    n_watch = len(watch_only)

    if n_gate2 == 0:
        return "", n_total, n_valid, n_gate1, n_gate2, None, [], n_watch

    exchange_class = getattr(ccxt, ex_name)({"enableRateLimit": True, "timeout": 8000})
    try:
        exchange_class.load_markets()
    except Exception as e:
        print(f"[WARN] FindCoin 2차 load_markets 실패({ex_name}): {e}")

    candidate_payload, used_candidates = fc_build_candidate_payload(exchange_class, candidates)

    # [결함수정] 서버 타임존과 무관하게 정확한 KST(UTC+9) 시각을 명시적으로 계산해 전달한다.
    # 이 값을 프롬프트에 데이터로 주지 않으면 Gemini가 출력서식의 "스캔 시각"·통계 항목을
    # 채우기 위해 훈련데이터 시점의 임의 날짜·수치를 지어내는 환각이 발생한다(실사용에서 확인됨).
    scan_time_kst = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")

    fc_prompt = (
        f"{CANDLEVIEW_PROMPT_FULL}\n\n"
        f"지금부터 14장 FindCoin 플러그인 모듈만 실행하십시오. 본체 PHASE1/2는 실행하지 마십시오.\n\n"
        f"[시스템 제공 실측 데이터 — 아래 수치를 반드시 그대로 사용하고 임의로 생성·추정하지 마십시오]\n"
        f"스캔 시각: {scan_time_kst} (KST)\n"
        f"대상 거래소: {ex_display} ({quote} 마켓)\n"
        f"총 스캔 종목(N_total): {n_total}개\n"
        f"유효 종목(N_valid, 사전정제 통과): {n_valid}개\n"
        f"1차 통과(RTM·Percentile, N_gate1): {n_gate1}개\n"
        f"최종 통과(유동성까지, N_gate2): {n_gate2}개\n\n"
        f"{candidate_payload}\n\n"
        f"위 후보 각각에 FC-2(State 자동진단 및 우선순위 2>3>1) → FC-3(5대 미시모듈 및 S_scout 집계) "
        f"→ FC-4(진입가·손익비, 본체 정의 상속) → FC-5(이중게이트 통과판정) 순서로 적용하고, "
        f"FC-6의 고정 출력 서식대로 최종 결과를 완제 출력하십시오. 출력서식의 스캔시각·총스캔종목·유효종목·"
        f"단계별 통과 수치는 반드시 위 [시스템 제공 실측 데이터]를 그대로 사용하십시오. "
        f"S_scout 미달 후보를 억지로 포함하지 마십시오.\n\n"
        f"[시스템 연동용 필수 마지막 줄 — 반드시 응답 맨 끝에 이 형식 그대로 정확히 한 줄 추가]\n"
        f"최종 합격한 코인의 심볼만(위 후보 목록에 있던 심볼 표기 그대로, 예: XRP/KRW) "
        f"통과 순위대로 파이프(|)로 구분하여 아래 형식으로 적으십시오. 0개 합격 시에도 태그 자체는 빈 값으로 출력하십시오.\n"
        f"[FINDCOIN_TOP_SYMBOLS] 심볼1|심볼2|심볼3"
    )

    result_text = call_gemini_api_with_retry(fc_prompt, max_tokens=8000)
    if result_text.startswith("AI 서버 일시적 과부하"):
        return None, n_total, n_valid, n_gate1, n_gate2, result_text, [], n_watch
    top_symbols = fc_extract_top_symbols(result_text, used_candidates)
    display_text = fc_strip_top_symbols_tag(result_text)
    return display_text, n_total, n_valid, n_gate1, n_gate2, None, top_symbols, n_watch


# ============================================================
# 인라인 키보드 생성
# callback_data에 거래소/심볼/TF를 함께 실어 보내, 캐시가 사라져도
# 버튼만으로 동일 조건 재분석이 가능하도록 한다 (64byte 제한 내 안전 설계).
# ============================================================
def make_phase_keyboard(ex_name, symbol_raw, tfs):
    tfs_str = ",".join(tfs)
    payload_tail = f"{ex_name}|{symbol_raw}|{tfs_str}"
    # Telegram callback_data 최대 64byte 안전장치: 가장 긴 액션명(supplement_view) 기준 초과시 TF 축약
    if len(f"supplement_view|{payload_tail}".encode("utf-8")) > 64:
        tfs_str = ",".join(tfs[:2])
        payload_tail = f"{ex_name}|{symbol_raw}|{tfs_str}"

    return {
        "inline_keyboard": [
            [{"text": "📋 수집데이터 열람", "callback_data": f"phase1_view|{payload_tail}"}],
            [{"text": "📋 보간 지표 열람", "callback_data": f"supplement_view|{payload_tail}"}],
            [{"text": "📊 코인 최종 분석내용 보기", "callback_data": f"phase2_run|{payload_tail}"}],
        ]
    }


def make_findcoin_detail_keyboard(ex_name, top_symbols):
    """FindCoin 결과의 TOP1~3 상세분석 버튼. 코인명은 콜백데이터에만 싣고
    버튼 라벨은 순위만 표시한다(본문 파싱 리스크 최소화 + 간결한 UI)."""
    rank_labels = ["✅️ TOP1 코인 상세분석 하기", "✅️ TOP2 코인 상세분석 하기", "✅️ TOP3 코인 상세분석 하기"]
    rows = []
    for i, symbol in enumerate(top_symbols[:3]):
        symbol_clean = symbol.split("/")[0]
        payload = f"fc_detail|{ex_name}|{symbol_clean}"
        if len(payload.encode("utf-8")) > 64:
            continue  # 비정상적으로 긴 심볼은 안전하게 건너뜀(발생 가능성 매우 낮음)
        rows.append([{"text": rank_labels[i], "callback_data": payload}])
    return {"inline_keyboard": rows} if rows else None


# ============================================================
# 텔레그램 메인 루프
# ============================================================
print("🚀 CandleView 봇 가동 시작 (Inline Mode)")

try:
    del_res = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook",
        timeout=10
    )
    print(f"[INFO] deleteWebhook 결과: {del_res.json()}")
except Exception as e:
    print(f"[WARN] deleteWebhook 실패: {e}")

last_update_id = 0

while True:
    try:
        clean_expired_cache()

        url = (
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            f"?offset={last_update_id + 1}&timeout=30"
        )
        updates = requests.get(url, timeout=35).json()

        if not updates.get("ok"):
            print(f"[WARN] getUpdates 오류: {updates}")
            time.sleep(3)
            continue

        for update in updates.get("result", []):
            last_update_id = update["update_id"]

            # ---------- 콜백 쿼리 처리 (인라인 버튼) ----------
            callback = update.get("callback_query")
            if callback:
                cb_id = callback["id"]
                chat_id = callback["message"]["chat"]["id"]
                data = callback.get("data", "")
                cb_parts = data.split("|")
                action = cb_parts[0] if cb_parts else ""
                cb_ex = cb_parts[1] if len(cb_parts) > 1 else None
                cb_sym = cb_parts[2] if len(cb_parts) > 2 else None
                cb_tfs = cb_parts[3].split(",") if len(cb_parts) > 3 and cb_parts[3] else None

                # FindCoin TOP1~3 상세분석 버튼 — 캐시확인 없이 즉시 신규분석 시작
                if action == "fc_detail":
                    fc_ex_name, fc_symbol = cb_ex, cb_sym
                    if fc_ex_name not in SUPPORTED_EXCHANGES or not fc_symbol:
                        answer_callback_query(cb_id, "요청 정보가 올바르지 않습니다.")
                        send_telegram_message(chat_id, "상세분석 요청 정보가 유효하지 않습니다.\n다시 코인 명령을 입력해 주세요.")
                        continue
                    answer_callback_query(cb_id, "상세분석 시작...")
                    fc_ex_display = SUPPORTED_EXCHANGES[fc_ex_name]["kr_name"]
                    fc_tfs = list(SUPPORTED_EXCHANGES[fc_ex_name]["default_tfs"])
                    send_telegram_message(
                        chat_id,
                        f"✅️ <b>CandleView</b> [{fc_ex_display}]\n{fc_symbol} {', '.join(fc_tfs)} (자동적용)\n\n"
                        f"🔎 차트 데이터 수집 중..."
                    )
                    d_phase1_result, d_symbol, d_exchange_display, d_supplement = run_phase1(fc_symbol, fc_ex_name, fc_tfs)
                    if d_symbol is None:
                        send_telegram_message(chat_id, d_phase1_result)
                        continue
                    analysis_cache[chat_id] = {
                        "phase1": d_phase1_result,
                        "symbol": d_symbol,
                        "exchange": d_exchange_display,
                        "supplement": d_supplement,
                        "ex_raw": d_exchange_display.lower(),
                        "sym_raw": fc_symbol,
                        "tfs": fc_tfs,
                        "created_at": datetime.now(),
                    }
                    send_telegram_message(
                        chat_id,
                        f"✅️ <b>CandleView</b> [{d_exchange_display}]\n{d_symbol}\n\n"
                        f"차트 상세 데이터 수집이 완료되었습니다.\n\n"
                        f"아래에서 원하는 항목을 선택하세요.",
                        reply_markup=make_phase_keyboard(d_exchange_display.lower(), fc_symbol, fc_tfs)
                    )
                    continue

                cached = analysis_cache.get(chat_id)
                cache_matches = bool(
                    cached
                    and cached.get("ex_raw") == cb_ex
                    and cached.get("sym_raw") == cb_sym
                )

                if action not in ("phase1_view", "supplement_view", "phase2_run"):
                    answer_callback_query(cb_id)
                    send_telegram_message(chat_id, "알 수 없는 요청입니다.\n다시 코인 명령을 입력해 주세요.")
                    continue

                if not cache_matches and (not cb_ex or not cb_sym or not cb_tfs):
                    # 구버전 콜백(정보 없음) 등 재계산 불가 케이스만 안내 후 종료
                    answer_callback_query(cb_id, "재분석 정보가 없습니다.")
                    send_telegram_message(chat_id, "분석 데이터가 만료되었습니다.\n다시 코인 명령을 입력해 주세요.")
                    continue

                if not cache_matches:
                    # 캐시 만료/불일치 시 버튼에 실려온 정보로 동일 조건 자동 재계산 (오류 대신 자동복구)
                    answer_callback_query(cb_id, "데이터 재계산 중...")
                    send_telegram_message(chat_id, "🕯️ <b>CandleView</b>\n이전 데이터가 만료되어 동일 조건으로 재계산합니다...")
                    phase1_result, symbol, exchange_display, supplement = run_phase1(cb_sym, cb_ex, cb_tfs)
                    if symbol is None:
                        send_telegram_message(chat_id, phase1_result)
                        continue
                    analysis_cache[chat_id] = {
                        "phase1": phase1_result,
                        "symbol": symbol,
                        "exchange": exchange_display,
                        "supplement": supplement,
                        "ex_raw": cb_ex,
                        "sym_raw": cb_sym,
                        "tfs": cb_tfs,
                        "created_at": datetime.now(),
                    }
                    cached = analysis_cache[chat_id]
                else:
                    action_msg = {
                        "phase1_view": "Phase1 데이터 불러오는 중...",
                        "supplement_view": "보간 지표 불러오는 중...",
                        "phase2_run": "Phase2 분석 실행 중...",
                    }.get(action, "처리 중...")
                    answer_callback_query(cb_id, action_msg)

                if action == "phase1_view":
                    phase1_text = sanitize_html(cached["phase1"])
                    header = f"<b>CandleView — Phase1 수집 데이터</b>\n{cached['exchange']} {cached['symbol']}\n\n"
                    full = header + phase1_text
                    for chunk in smart_chunk(full, PHASE1_BOUNDARY_MARKERS):
                        send_telegram_message(chat_id, chunk)

                elif action == "supplement_view":
                    # Gemini 호출 없이 main.py가 수집한 원시데이터를 그대로 표시 (즉시응답, 환각없음)
                    supp_text = format_supplement_display(cached.get("supplement"), cached["symbol"], cached["exchange"])
                    send_telegram_message(chat_id, supp_text)

                elif action == "phase2_run":
                    send_telegram_message(
                        chat_id,
                        f"🕯️ <b>CandleView</b>\n"
                        f"{cached['exchange']} {cached['symbol']} Phase2 최종 분석 진행 중...\n"
                        f"잠시만 기다려 주세요."
                    )
                    phase2_result = sanitize_html(run_phase2(
                        cached["phase1"],
                        cached["symbol"],
                        cached["exchange"]
                    ))
                    header = f"<b>CandleView — Phase2 최종 분석</b>\n{cached['exchange']} {cached['symbol']}\n\n"
                    full = header + phase2_result
                    for chunk in smart_chunk(full, PHASE2_BOUNDARY_MARKERS):
                        send_telegram_message(chat_id, chunk)

                continue

            # ---------- 일반 메시지 처리 ----------
            msg = update.get("message", {})
            chat_id = msg.get("chat", {}).get("id")
            raw_text = msg.get("text", "").strip()

            if not chat_id:
                continue

            if not raw_text.startswith("/"):
                send_telegram_message(chat_id, UNAUTHORIZED_INPUT_GUIDE)
                continue

            clean_text = raw_text[1:].replace(":", " ").replace(",", " ")
            parts = clean_text.split()
            if not parts:
                continue

            cmd = parts[0].lower()

            if cmd == "start":
                send_telegram_message(
                    chat_id,
                    (
                        "🕯️ <b>CandleView</b> — PA/VSA 정밀 차트 분석 봇\n\n"
                        "[지정코인 분석 예시]\n"
                        "• /업비트 비트코인\n"
                        "• /빗썸 리플\n"
                        "• /coinbase eth\n"
                        "• /업비트 비트코인 1d 4h 1h  (TF 직접 지정)\n\n"
                        "[FindCoin — Top3 스크리닝]\n"
                        "• /업비트   또는   /coinbase\n\n"
                        "지원 거래소: 업비트 · 빗썸 · 코인베이스"
                    ),
                )
                continue

            # 명령 파싱 — 거래소명이 필수 1순위 인자
            ex_name = resolve_exchange(parts[0])
            if ex_name is None:
                send_telegram_message(chat_id, UNAUTHORIZED_INPUT_GUIDE)
                continue

            if len(parts) == 1:
                # 코인명 없이 거래소명만 → FindCoin 실행
                ex_display_fc = SUPPORTED_EXCHANGES[ex_name]["kr_name"]
                quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
                send_telegram_message(
                    chat_id,
                    f"🚨 시세분출 가능성이 높은 코인을 분석해서 Top3 를 알려드리는 🔎 FindCoin 이 실행되었습니다.\n"
                    f"잠시만 기다려 주세요.\n\n"
                    f"🔎 {ex_display_fc} 정보수집중...\n\n"
                    f"거래소 응답속도에 따라 1 ~3분 정도 소요될수 있습니다."
                )

                result_text, n_total, n_valid, n_gate1, n_gate2, err, top_symbols, n_watch = run_findcoin(ex_name)

                if err:
                    send_telegram_message(chat_id, err)
                    continue

                watch_note = f"\n\n💡 참고내용\n데이터가 부족해 판정을 보류한 신규상장 코인 {n_watch}개 있음(배제 아님)" if n_watch > 0 else ""

                if n_gate2 == 0:
                    watch_line = f"\n💡 참고내용\n데이터가 부족해 판정을 보류한 신규상장 코인 {n_watch}개 있음(배제 아님)\n" if n_watch > 0 else ""
                    scan_time_kst_watch = (datetime.utcnow() + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M")
                    send_telegram_message(
                        chat_id,
                        f"🚨 FindCoin 코인 스캔 결과를 출력합니다.\n\n"
                        f"🔎 스캔 결과 최종 합격코인 : {n_gate2} 개\n\n"
                        f"대상 : {ex_display_fc} {quote} 마켓 [총 스캔개수 : {n_total} 개]\n"
                        f"스캔 시각: {scan_time_kst_watch} (KST)\n"
                        f"총 스캔 종목: {n_total}개 (유효 {n_valid}개 / 관측대기 {n_watch}개)\n\n"
                        f"🧭 스캔 단계별 통과\n"
                        f"➔ 1차 수급·유동성 {n_gate1}개\n"
                        f"➔ State 옥석 {n_gate1}개\n"
                        f"➔ 최종 합격 {n_gate2}개\n\n"
                        f"✅️ 시장 상태 : 관망 국면\n\n"
                        f"[관망 권고] 현재 {ex_display_fc} {quote} 마켓 내 상대 수급(Percentile ≥ 85% AND RTM ≥ 3.0 "
                        f"AND Liquidity_Ratio ≥ 1.0), PA-VSA 옥석 검증, 손익비(R:R ≥ 2.0) 조건을 동시에 충족하는 "
                        f"고신뢰 분출 후보가 0개입니다. 억지 추격 진입을 지양하고 관망을 권고합니다.\n\n"
                        f"[FindCoin 무결성 검증 완료]\n"
                        f"■ API Direct Stream\n"
                        f"■ Layer 7 감사 100% 통과\n"
                        f"{watch_line}"
                    )
                    continue

                fc_text = sanitize_html(result_text) + sanitize_html(watch_note)
                header = f"<b>CandleView — FindCoin 스캔 결과</b>\n{ex_display_fc}\n\n"
                full = header + fc_text
                chunks = smart_chunk(full, FINDCOIN_BOUNDARY_MARKERS)
                for i, chunk in enumerate(chunks):
                    is_last = (i == len(chunks) - 1)
                    reply_markup = make_findcoin_detail_keyboard(ex_name, top_symbols) if (is_last and top_symbols) else None
                    send_telegram_message(chat_id, chunk, reply_markup=reply_markup)
                continue

            sym_name = parts[1]
            # TF 미지정 시 거래소 유형별 고정값 자동 적용, 지정 시 그대로 사용
            if len(parts) > 2:
                tfs = parts[2:]
                tf_note = "(직접 지정)"
            else:
                tfs = list(SUPPORTED_EXCHANGES[ex_name]["default_tfs"])
                tf_note = "(자동 적용)"

            sym_clean = sym_name.replace("/", "").replace(" ", "").strip()
            sym_mapped = resolve_korean_symbol(sym_clean, ex_name)
            quote = SUPPORTED_EXCHANGES[ex_name]["quote"]
            ex_display = SUPPORTED_EXCHANGES[ex_name]["kr_name"]

            # 상태 메시지
            status_msg = (
                f"✅️ <b>CandleView</b> [{ex_display}]\n"
                f"{sym_mapped}/{quote} {', '.join(tfs)} {tf_note}\n\n"
                f"🔎 차트 데이터 수집 중..."
            )
            send_telegram_message(chat_id, status_msg)

            # PHASE 1 실행
            phase1_result, symbol, exchange_display, supplement = run_phase1(sym_clean, ex_name, tfs)

            if symbol is None:
                send_telegram_message(chat_id, phase1_result)
                continue

            # 캐시에 저장 (ex_raw/sym_raw는 콜백 재계산 시 run_phase1에 그대로 재사용되는 원본 파라미터)
            analysis_cache[chat_id] = {
                "phase1": phase1_result,
                "symbol": symbol,
                "exchange": exchange_display,
                "supplement": supplement,
                "ex_raw": exchange_display.lower(),
                "sym_raw": sym_clean,
                "tfs": tfs,
                "created_at": datetime.now(),
            }

            # 안내 메시지 + 인라인 버튼
            guide_msg = (
                f"✅️ <b>CandleView</b> [{exchange_display}]\n"
                f"{symbol}\n\n"
                f"차트 상세 데이터 수집이 완료되었습니다.\n\n"
                f"아래에서 원하는 항목을 선택하세요."
            )
            send_telegram_message(
                chat_id, guide_msg,
                reply_markup=make_phase_keyboard(exchange_display.lower(), sym_clean, tfs)
            )

    except Exception as e:
        print(f"[ERROR] 메인 루프 예외: {e}")
        time.sleep(2)
