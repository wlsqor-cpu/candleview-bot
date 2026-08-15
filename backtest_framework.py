# -*- coding: utf-8 -*-
"""
backtest_framework.py
======================
CandleView 임계값 검증 — 일반화된 몬테카를로/사후검증 백테스트 프레임워크.

State1(FindCoin)에 국한하지 않고, 캔들·거래량(+필요시 RSI 등 지표) 기반 임계값 규칙을
"규칙(rule)" 단위로 등록해 공통 인프라 위에서 검증한다.

[아키텍처 — 모듈식 무간섭 확장 원칙]
- 인프라(재사용, 규칙과 무관): 데이터수집(fetch) → 지표 사전계산(precompute_indicators)
  → 슬라이딩윈도우 루프 → 향후수익률 계산 → 텔레그램 요약/CSV 리포팅
- 규칙(플러그인, 독립 추가): 각 규칙 = (daily_window, indicators, idx, params) →
  (통과여부 True/False/None, 진단값 dict) 함수 하나. 새 규칙 추가는 RULES 리스트에
  항목 하나 더하는 것으로 끝나며 기존 규칙·인프라 코드는 전혀 건드리지 않는다.
  반환값 None = "이 시점엔 해당 진단 자체가 성립하지 않음"(표본에서 제외, False와 구분).

[중요 — main.py를 import하지 않는 이유]
main.py는 `if __name__ == "__main__":` 가드가 없다(grep 확인: 매치 0건) — 모듈 로드
즉시 while True 텔레그램 폴링 루프가 실행된다. 따라서 `from main import ...`는 임포트
시점에 무한루프에 걸려 영원히 반환되지 않는다. 이 파일은 필요한 함수(calculate_rma_rsi 등)를
main.py 원문 그대로 복제해서 쓴다 — 진짜 SSOT 공유(import)는 구조상 불가능하므로, main.py의
해당 함수가 바뀌면 이 파일도 수동 동기화가 필요하다는 점을 인지하고 사용할 것.

[SSOT 값 출처 — CandleView_API.txt 대조 완료]
- VOL_AVG_LOOKBACK=20봉(L797), VOL_ABSORPTION_MIN=1.5배/BODY_MAX=30.0%(L779-780)
- VOL_CLIMAX_MIN=3.0배(L779), RSI 극단 30/70(L1185)
- COMPRESSION_RANGE_MAX=5.0%, FC_MIN_BARS_STANDARD=40(main.py L1263)
- RSI: main.py L300 calculate_rma_rsi 그대로 복제(14기간 Wilder RMA, 판정42 NaN→50.0 처리 포함)
- 노디맨드/노서플라이: "직전 2봉 각각 대비 거래량감소 + 몸통비율 30%이하"(L1182)

[알려진 한계]
FC-1 1차게이트·range_expansion 하드게이트·구조판정(BOS/CHoCH/OB/FVG/Confluence/S_final
전체 스코어링) 등은 포함하지 않는다 — 이런 항목은 현재도 Gemini 프롬프트 판단 영역이라
Python 재현 자체가 별도의 큰 작업(A4 "Python화 확대 논증", 미착수)이며 이 프레임워크의
1차 범위 밖이다. 여기서 다루는 건 캔들+거래량(+RSI)만으로 판정되는 임계값 규칙뿐이다.

[기간 지정 — 텔레그램 명령 인자]
/backtest                        → 기본(최근 약 200봉, 거래소 1회 호출)
/backtest 2023-01-01              → 2023-01-01부터 현재까지
/backtest 2023-01-01 2023-08-01   → 2023-01-01~2023-08-01 구간에서 발생한 신호만 평가
  (향후수익률 계산용 데이터는 종료일 이후까지 추가로 받아온다 — 종료일 근처 신호도
  정상적으로 20봉 뒤 결과를 채점하기 위함. 페이지네이션으로 거래소 1회 호출당 캔들수
  제한을 우회하며, 통신량 방지를 위해 최대 15회 호출로 상한을 둔다.)
"""

import ccxt
import numpy as np
import pandas as pd
import requests
import os
import io
import csv
from collections import defaultdict
from datetime import datetime, timezone

# ============================================================
# 설정
# ============================================================
ADMIN_CHAT_ID = 517008099
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

VOL_AVG_LOOKBACK = 20
COMPRESSION_RANGE_MAX = 5.0
FC_MIN_BARS_STANDARD = 40
FORWARD_WINDOW = 20
RSI_PERIOD = 14

SUPPORTED_EXCHANGES = {
    "upbit":    {"quote": "KRW", "ccxt_id": "upbit"},
    "bithumb":  {"quote": "KRW", "ccxt_id": "bithumb"},
    "coinbase": {"quote": "USD", "ccxt_id": "coinbase"},
}
UNIVERSE_LIMIT_PER_EXCHANGE = 40

# 시세분출 개념이 적용되지 않는 스테이블코인 제외 — main.py L1310 FC_STABLECOIN_BASES 원문 그대로 복제.
# [결함수정] 이 목록 없이 심볼을 그냥 잘라 쓰면(예: coinbase USDT/USD) 스테이블코인이 항상
# "압축률≤5%"를 만족해 State1 표본을 통째로 잠식한다 — 실제 백테스트에서 확인된 결함.
FC_STABLECOIN_BASES = {"USDT", "USDC", "DAI", "TUSD", "PAX", "GUSD", "PYUSD", "BUSD", "USDP", "FDUSD"}


# ============================================================
# 텔레그램 전송
# ============================================================
def send_telegram_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e:
        print(f"[backtest_framework] 텔레그램 전송 실패: {e}")


def send_telegram_document(chat_id, filename, content_bytes, caption=""):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    try:
        files = {"document": (filename, content_bytes)}
        data = {"chat_id": chat_id, "caption": caption}
        requests.post(url, data=data, files=files, timeout=60)
    except Exception as e:
        print(f"[backtest_framework] 텔레그램 파일 전송 실패: {e}")


# ============================================================
# 지표 레이어 — main.py L300 calculate_rma_rsi 원문 그대로 복제
# ============================================================
def calculate_rma_rsi(prices, period=RSI_PERIOD):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    # 완전 무변동(avg_gain==avg_loss==0) → rs=NaN → RSI 중립균형점 50.0으로 처리(main.py 판정42 동일)
    return rsi.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)


def precompute_indicators(daily):
    """규칙이 필요로 하는 지표를 심볼당 1회만 계산해 캐싱. 새 지표 추가 시 여기 한 곳만 확장."""
    closes = pd.Series([row[4] for row in daily])
    return {"rsi": calculate_rma_rsi(closes, RSI_PERIOD)}


# ============================================================
# 공통 유틸 (여러 규칙이 공유)
# ============================================================
def avg_volume_before(daily_window, lookback=VOL_AVG_LOOKBACK):
    """window[-1]("오늘") 직전 lookback개 완성봉 평균거래량. 신호봉 자체는 제외
    (자기참조 오염 방지 원칙, 스펙 L162-164)."""
    seg = daily_window[-(lookback + 1):-1]
    if len(seg) < 5:
        return None
    return sum(r[5] for r in seg) / len(seg)


def body_ratio_pct(bar):
    """main.py classify_candle_shape와 동일 정의: abs(c-o)/(h-l)*100."""
    o, h, l, c = bar[1], bar[2], bar[3], bar[4]
    return abs(c - o) / (h - l + 1e-8) * 100.0


# ============================================================
# 규칙 1 — State1 옥석검증 (FindCoin, CandleView_API.txt L2031-2047)
# ============================================================
def _check_compression(daily_window):
    past = daily_window[:-1][-FC_MIN_BARS_STANDARD:]
    if len(past) < 5:
        return None
    period_high = max(row[2] for row in past)
    period_low = min(row[3] for row in past)
    current_price = daily_window[-1][4]
    if period_high <= 0 or current_price > period_high:
        return None
    return (period_high - period_low) / period_high * 100.0


def _detect_sl(daily_window, lookback=FC_MIN_BARS_STANDARD):
    past = daily_window[:-1][-lookback:]
    if len(past) < 5:
        return None
    return min(row[3] for row in past)


def rule_state1(daily_window, indicators, idx, params):
    comp_max = params.get("comp_max", COMPRESSION_RANGE_MAX)
    comp_pct = _check_compression(daily_window)
    if comp_pct is None or comp_pct > comp_max:
        return None, {}
    sl = _detect_sl(daily_window)
    if sl is None:
        return None, {}

    last = daily_window[-1]
    o, h, l, c, v = last[1], last[2], last[3], last[4], last[5]

    if params["a_mode"] == "strict":
        a_pass = (l < sl) and (c > sl)
    else:  # loose
        a_pass = (l <= sl * 1.05) and (c > o)

    avg_vol = avg_volume_before(daily_window)
    if avg_vol is None:
        return None, {}
    rv_t = v / (avg_vol + 1e-8)
    br = body_ratio_pct(last)
    b_pass = (rv_t >= params["rv_min"]) and (br <= params["body_max"])

    return (a_pass and b_pass), {"compression_pct": comp_pct, "rv_t": rv_t, "body_ratio": br}


STATE1_GRID = [
    # --- A/B 조건 스윕 (압축률은 스펙 원값 5.0% 고정) ---
    {"name": "현재설정(comp5%/strict/1.5/30%)",    "comp_max": 5.0,  "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "스윕만완화(comp5%/loose/1.5/30%)",    "comp_max": 5.0,  "a_mode": "loose",  "rv_min": 1.5, "body_max": 30.0},
    {"name": "제안-균형안(comp5%/loose/1.3/40%)",   "comp_max": 5.0,  "a_mode": "loose",  "rv_min": 1.3, "body_max": 40.0},
    {"name": "제안-대폭완화(comp5%/loose/1.2/45%)", "comp_max": 5.0,  "a_mode": "loose",  "rv_min": 1.2, "body_max": 45.0},
    # --- 압축률 자체 스윕 (A/B는 스펙 원값 strict/1.5/30% 고정 — 압축조건 단독 영향 확인용) ---
    {"name": "압축8%(strict/1.5/30%)",  "comp_max": 8.0,  "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축12%(strict/1.5/30%)", "comp_max": 12.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축15%(strict/1.5/30%)", "comp_max": 15.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축20%(strict/1.5/30%)", "comp_max": 20.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
]


# ============================================================
# 규칙 2 — 노디맨드/노서플라이 (CandleView_API.txt L1182)
# "직전 2봉 각각 대비 거래량 감소 + 몸통비율 body_max 이하"
# ============================================================
def rule_no_demand_supply(daily_window, indicators, idx, params):
    if len(daily_window) < 3:
        return None, {}
    today, prev1, prev2 = daily_window[-1], daily_window[-2], daily_window[-3]
    vol_decreasing = (today[5] < prev1[5]) and (today[5] < prev2[5])
    br = body_ratio_pct(today)
    passed = vol_decreasing and (br <= params["body_max"])
    return passed, {"body_ratio": br, "vol_today": today[5], "vol_prev1": prev1[5], "vol_prev2": prev2[5]}


NDNS_GRID = [
    {"name": "현재설정(body≤30%)", "body_max": 30.0},
    {"name": "완화안(body≤35%)",   "body_max": 35.0},
]


# ============================================================
# 규칙 3 — VSA 클라이맥스 (CandleView_API.txt L1183-1185)
# "거래량 VOL_CLIMAX_MIN배 이상 + RSI 극단(30이하 또는 70이상)"
# ============================================================
def rule_climax(daily_window, indicators, idx, params):
    avg_vol = avg_volume_before(daily_window)
    if avg_vol is None:
        return None, {}
    last = daily_window[-1]
    rv_t = last[5] / (avg_vol + 1e-8)

    rsi_series = indicators.get("rsi")
    if rsi_series is None or idx >= len(rsi_series):
        return None, {}
    rsi_val = rsi_series.iloc[idx]
    if pd.isna(rsi_val):
        return None, {}

    extreme = (rsi_val <= params["rsi_low"]) or (rsi_val >= params["rsi_high"])
    passed = (rv_t >= params["vol_min"]) and extreme
    return passed, {"rv_t": rv_t, "rsi": float(rsi_val)}


CLIMAX_GRID = [
    {"name": "현재설정(3.0배/RSI30·70)", "vol_min": 3.0, "rsi_low": 30.0, "rsi_high": 70.0},
    {"name": "완화안(2.5배/RSI35·65)",   "vol_min": 2.5, "rsi_low": 35.0, "rsi_high": 65.0},
]


# ============================================================
# 규칙 4 — VSA 흡수 단독형 (CandleView_API.txt L1186-1187, 본체 모듈B 원형)
# State1의 B조건과 같은 공식이지만, 압축(State1 진단)·스윕(A조건) 선행조건 없이
# "모든 시점"에서 독립 평가한다 — 본체 모듈B가 실제로 쓰는 범위와 동일(FindCoin 한정 아님).
# ============================================================
def rule_absorption(daily_window, indicators, idx, params):
    avg_vol = avg_volume_before(daily_window)
    if avg_vol is None:
        return None, {}
    last = daily_window[-1]
    rv_t = last[5] / (avg_vol + 1e-8)
    br = body_ratio_pct(last)
    passed = (rv_t >= params["rv_min"]) and (br <= params["body_max"])
    return passed, {"rv_t": rv_t, "body_ratio": br}


ABSORPTION_GRID = [
    {"name": "현재설정(1.5배/30%)", "rv_min": 1.5, "body_max": 30.0},
    {"name": "완화안(1.3배/40%)",   "rv_min": 1.3, "body_max": 40.0},
]


# ============================================================
# 규칙 5 — State3 매도고갈 (CandleView_API.txt L2058-2060, FindCoin State3 옥석검증 A)
# "조정구간 3봉 연속 거래량 계단식 감소 AND 조정봉(오늘) 몸통비율 ≤ BODY_RATIO_WEAK"
# SEQUENCE_DECAY_BARS=3봉 명명과 일치시켜 3봉(그전>직전>오늘) 비교로 구현.
# ============================================================
def rule_state3_selling_exhaustion(daily_window, indicators, idx, params):
    if len(daily_window) < 3:
        return None, {}
    b2, b1, b0 = daily_window[-3], daily_window[-2], daily_window[-1]
    decreasing = (b2[5] > b1[5]) and (b1[5] > b0[5])
    br = body_ratio_pct(b0)
    passed = decreasing and (br <= params["body_max"])
    return passed, {"body_ratio": br, "vol_2ago": b2[5], "vol_1ago": b1[5], "vol_today": b0[5]}


STATE3_GRID = [
    {"name": "현재설정(body≤32%, BODY_RATIO_WEAK)", "body_max": 32.0},
]


# ============================================================
# 규칙 6 — 극저거래량 응축 (CandleView_API.txt L1192)
# "거래량 VOL_LOW_CONDITION(0.8배) 미만 4봉이상 연속 + 구간내 도지/잉태형 2회이상"
# [해석 선택 — 명시] '잉태형(하라미)' 판정함수는 main.py에 없어 표준 정의(오늘 몸통이
# 전일 몸통 범위 안에 완전히 포함)로 이번에 새로 구현했다. 스펙 문언 자체는 이견 없지만
# main.py 기존 함수 재사용이 아닌 신규 구현이라는 점을 명시한다.
# ============================================================
def _is_doji(bar):
    o, h, l, c = bar[1], bar[2], bar[3], bar[4]
    rng = h - l
    if rng <= 0:
        return False
    return abs(c - o) / rng * 100.0 <= 10.0  # BODY_RATIO_DOJI


def _is_harami(prev_bar, bar):
    po, pc = prev_bar[1], prev_bar[4]
    o, c = bar[1], bar[4]
    prev_lo, prev_hi = min(po, pc), max(po, pc)
    lo, hi = min(o, c), max(o, c)
    return lo >= prev_lo and hi <= prev_hi


def rule_low_volume_squeeze(daily_window, indicators, idx, params):
    n = 4
    if len(daily_window) < n + 1:
        return None, {}
    recent = daily_window[-n:]
    avg_vol = avg_volume_before(daily_window)
    if avg_vol is None:
        return None, {}
    all_low_vol = all(bar[5] < params["vol_low"] * avg_vol for bar in recent)
    pattern_count = 0
    for i, bar in enumerate(recent):
        if _is_doji(bar):
            pattern_count += 1
        elif i > 0 and _is_harami(recent[i - 1], bar):
            pattern_count += 1
    passed = all_low_vol and (pattern_count >= 2)
    return passed, {"pattern_count": pattern_count, "vol_ratio_today": recent[-1][5] / (avg_vol + 1e-8)}


LOWVOL_GRID = [
    {"name": "현재설정(0.8배미만4봉+패턴2회)", "vol_low": 0.8},
]


# ============================================================
# 규칙 레지스트리 — 새 규칙은 이 리스트에 한 항목만 추가하면 됨
# ============================================================
RULES = [
    {"id": "state1",           "needs": [],      "fn": rule_state1,                    "grid": STATE1_GRID},
    {"id": "no_demand_supply", "needs": [],      "fn": rule_no_demand_supply,          "grid": NDNS_GRID},
    {"id": "climax",           "needs": ["rsi"], "fn": rule_climax,                    "grid": CLIMAX_GRID},
    {"id": "absorption",       "needs": [],      "fn": rule_absorption,                "grid": ABSORPTION_GRID},
    {"id": "state3_exhaustion","needs": [],      "fn": rule_state3_selling_exhaustion, "grid": STATE3_GRID},
    {"id": "low_vol_squeeze",  "needs": [],      "fn": rule_low_volume_squeeze,        "grid": LOWVOL_GRID},
]


# ============================================================
# 백테스트 실행 (인프라 — 규칙 무관 공통 로직)
# ============================================================
def fetch_ohlcv_range(ex, symbol, timeframe, since_ms, per_call_limit=200, max_calls=15):
    """since_ms부터 현재까지 페이지네이션으로 전 구간 일봉을 가져온다.
    거래소 1회 호출당 캔들 반환 개수 제한(대부분 200개 내외)을 우회하기 위함.
    max_calls는 통신량 상한(200*15=최대 약 3000봉≈8년)이자 무한루프 방지 안전장치."""
    all_bars = []
    since = since_ms
    for _ in range(max_calls):
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=per_call_limit)
        except Exception:
            break
        if not batch:
            break
        all_bars.extend(batch)
        last_ts = batch[-1][0]
        if last_ts <= since:  # 진행 없음 — 무한루프 방지
            break
        since = last_ts + 1
        if len(batch) < per_call_limit:
            break  # 마지막 페이지(더 이상 데이터 없음)
    return all_bars


def _parse_date_to_ms(date_str):
    return int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def run_backtest_for_exchange(ex_id, quote, start_date=None, end_date=None):
    ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 8000})
    markets = ex.load_markets()
    symbols = [
        s for s in markets
        if s.endswith(f"/{quote}") and s.split("/")[0] not in FC_STABLECOIN_BASES
    ][:UNIVERSE_LIMIT_PER_EXCHANGE]

    records = []
    min_bars_needed = max(FC_MIN_BARS_STANDARD, RSI_PERIOD) + FORWARD_WINDOW + 10

    start_ms = _parse_date_to_ms(start_date) if start_date else None
    end_ms = _parse_date_to_ms(end_date) if end_date else None

    for sym in symbols:
        try:
            if start_ms is not None:
                daily = fetch_ohlcv_range(ex, sym, "1d", start_ms)
            else:
                daily = ex.fetch_ohlcv(sym, timeframe="1d", limit=200)
        except Exception:
            continue
        if len(daily) < min_bars_needed:
            continue

        indicators = precompute_indicators(daily)  # 심볼당 1회만 계산, 모든 규칙이 공유
        start_idx = max(FC_MIN_BARS_STANDARD + 5, RSI_PERIOD + 5)

        for t in range(start_idx, len(daily) - FORWARD_WINDOW):
            bar_ts = daily[t][0]
            if end_ms is not None and bar_ts > end_ms:
                break  # 시간순 정렬 데이터이므로, 종료일 초과 시점부턴 신호평가 대상 아님

            window = daily[:t + 1]
            entry_price = window[-1][4]
            future_price = daily[t + FORWARD_WINDOW][4]
            fwd_return_pct = (future_price - entry_price) / entry_price * 100.0

            for rule in RULES:
                for params in rule["grid"]:
                    result, diag = rule["fn"](window, indicators, t, params)
                    if result is None:
                        continue  # 이 시점엔 해당 진단 자체가 성립하지 않음 — 표본 제외

                    row = {
                        "exchange": ex_id, "symbol": sym, "t_index": t,
                        "bar_date": datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                        "rule": rule["id"], "combo": params["name"],
                        "gate_pass": result, "fwd_return_pct": round(fwd_return_pct, 4),
                    }
                    for k, v in diag.items():
                        row[k] = round(v, 4) if isinstance(v, float) else v
                    records.append(row)

    return records


def summarize(records):
    baseline_by_rule = defaultdict(list)
    for r in records:
        baseline_by_rule[r["rule"]].append(r["fwd_return_pct"])

    by_key = defaultdict(list)
    for r in records:
        if r["gate_pass"]:
            by_key[(r["rule"], r["combo"])].append(r["fwd_return_pct"])

    lines = ["<b>CandleView 임계값 규칙 검증 — 독립 재검증 결과</b>", ""]
    for rule in RULES:
        rid = rule["id"]
        baseline = baseline_by_rule.get(rid, [])
        baseline_avg = (sum(baseline) / len(baseline)) if baseline else 0.0
        lines.append(f"■ [{rid}] 표본 {len(baseline)}건 | 기준선 평균 {baseline_avg:+.2f}%")
        for params in rule["grid"]:
            rets = np.array(by_key.get((rid, params["name"]), []))
            n = len(rets)
            if n == 0:
                lines.append(f"  - {params['name']}: 통과 0건")
                continue
            avg = rets.mean()
            excess = avg - baseline_avg
            winrate = (rets > 0).mean() * 100
            lines.append(
                f"  - {params['name']}: 통과{n}건 | 평균{avg:+.2f}% | "
                f"초과{excess:+.2f}%p | 승률{winrate:.1f}%"
            )
        lines.append("")
    return "\n".join(lines)


def execute_and_report(chat_id, start_date=None, end_date=None):
    """관리자 명령으로 트리거되는 진입점. main.py에서 백그라운드 스레드로 호출.
    start_date/end_date: "YYYY-MM-DD" 문자열 또는 None(기본 = 최근 약 200봉)."""
    if chat_id != ADMIN_CHAT_ID:
        return

    if start_date:
        try:
            datetime.strptime(start_date, "%Y-%m-%d")
            if end_date:
                datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            send_telegram_message(
                chat_id,
                "❌ 날짜 형식 오류. 예: /backtest 2023-01-01 2023-08-01 (YYYY-MM-DD)"
            )
            return

    period_label = f"{start_date} ~ {end_date or '현재'}" if start_date else "최근 약 200봉(기본)"
    send_telegram_message(chat_id, f"🔬 CandleView 임계값 검증 백테스트 시작 (기간: {period_label})... (수 분 소요)")

    all_records = []
    for ex_id, cfg in SUPPORTED_EXCHANGES.items():
        try:
            recs = run_backtest_for_exchange(cfg["ccxt_id"], cfg["quote"], start_date, end_date)
            all_records.extend(recs)
            send_telegram_message(chat_id, f"✓ {ex_id} 완료 ({len(recs)}건 수집)")
        except Exception as e:
            send_telegram_message(chat_id, f"⚠️ {ex_id} 처리 중 오류: {e}")

    if not all_records:
        send_telegram_message(chat_id, "❌ 결과 없음 — 데이터 수집 실패 가능성. 서버 로그 확인 필요.")
        return

    send_telegram_message(chat_id, f"기간: {period_label}\n" + summarize(all_records))

    buf = io.StringIO()
    all_keys = set()
    for r in all_records:
        all_keys.update(r.keys())
    fieldnames = sorted(all_keys)
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_records)
    send_telegram_document(
        chat_id, "candleview_backtest_raw.csv", buf.getvalue().encode("utf-8"),
        caption="원본 결과 전체 (Claude 재검증용 — 이 파일을 그대로 업로드하면 됨)"
    )
    send_telegram_message(chat_id, "✅ 백테스트 완료.")
