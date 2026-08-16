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
import zipfile
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
# 시작일 신호에도 과거 40봉 압축·거래량·RSI 계산을 적용하기 위한 사전 수집 구간.
WARMUP_BARS = max(FC_MIN_BARS_STANDARD, RSI_PERIOD) + 5

SUPPORTED_EXCHANGES = {
    "upbit":    {"quote": "KRW", "ccxt_id": "upbit"},
    "bithumb":  {"quote": "KRW", "ccxt_id": "bithumb"},
    "coinbase": {"quote": "USD", "ccxt_id": "coinbase"},
}
UNIVERSE_LIMIT_PER_EXCHANGE = 40

# [결함수정 — 재현성] 이전 버전은 실행할 때마다 ex.load_markets()가 그 순간 돌려주는 목록의
# 앞부분을 그대로 잘라 썼다. 거래소 API가 반환하는 순서·구성이 실행 시점(신규상장 등)에
# 따라 달라질 수 있어, 같은 기간을 다시 돌려도 실제로는 다른 코인 집합을 보고 있었다
# (동일 2023-01-01~현재 구간을 두 번 실행 → State1 결과가 서로 모순되는 현상으로 실증됨).
# 아래 목록은 2026-08-16 실행(3.4년 전체기간, 스테이블코인 제외 후 실제 3년+ 데이터가
# 있었던 것으로 확인된 심볼만)에서 그대로 추출한 것 — 매 실행마다 동일한 심볼만 사용해
# 재현성을 보장한다. 유니버스를 바꾸고 싶으면 이 리스트만 수정하면 된다(SSOT).
FIXED_UNIVERSE = {
    "upbit": [
        "ALGO/KRW", "ANKR/KRW", "BAT/KRW", "BORA/KRW", "BOUNTY/KRW", "DKA/KRW",
        "HUNT/KRW", "LSK/KRW", "PUNDIX/KRW", "SHIB/KRW", "WAVES/KRW", "WAXP/KRW",
    ],
    "bithumb": [
        "A/KRW", "ADA/KRW", "AMO/KRW", "ANKR/KRW", "BAT/KRW", "BCH/KRW", "BSV/KRW",
        "BTC/KRW", "CHR/KRW", "COS/KRW", "CRO/KRW", "CVC/KRW", "EL/KRW", "ELF/KRW",
        "ENJ/KRW", "ETC/KRW", "ETH/KRW", "FCT2/KRW", "GLM/KRW", "ICX/KRW", "IOST/KRW",
        "KNC/KRW", "LINK/KRW", "MBL/KRW", "MEV/KRW", "MTL/KRW", "ORBS/KRW", "POWR/KRW",
        "QTUM/KRW", "SNT/KRW", "STEEM/KRW", "TFUEL/KRW", "THETA/KRW", "TRX/KRW",
        "VET/KRW", "WAVES/KRW", "WAXP/KRW", "XRP/KRW", "ZIL/KRW", "ZRX/KRW",
    ],
    "coinbase": [
        "AAVE/USD", "ADA/USD", "AVAX/USD", "BCH/USD", "BICO/USD", "BTC/USD", "DOGE/USD",
        "DOT/USD", "ETH/USD", "FET/USD", "HBAR/USD", "ICP/USD", "LINK/USD", "LTC/USD",
        "NEAR/USD", "SOL/USD", "UNI/USD", "XLM/USD", "ZEC/USD",
    ],
}

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


# 실험 설계 원칙: 한 단계에서 한 변수군만 바꾼다.
# A단계는 압축률만 비교하고, B단계는 A단계에서 표본이 나오는 10% 압축률에 조건을 고정해 비교한다.
# 이 값들은 백테스트 전용 후보군이며 main.py/CandleView_API.txt의 운영 상수를 변경하지 않는다.
STATE1_GRID = [
    # A단계 — 압축률 단독 스윕: strict / RV 1.5 / body 30% 고정
    {"name": "압축5%(strict/1.5/30%)",  "experiment_stage": "A_compression", "comp_max": 5.0,  "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축6%(strict/1.5/30%)",  "experiment_stage": "A_compression", "comp_max": 6.0,  "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축7%(strict/1.5/30%)",  "experiment_stage": "A_compression", "comp_max": 7.0,  "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축8%(strict/1.5/30%)",  "experiment_stage": "A_compression", "comp_max": 8.0,  "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축10%(strict/1.5/30%)", "experiment_stage": "A_compression", "comp_max": 10.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축12%(strict/1.5/30%)", "experiment_stage": "A_compression", "comp_max": 12.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축15%(strict/1.5/30%)", "experiment_stage": "A_compression", "comp_max": 15.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축20%(strict/1.5/30%)", "experiment_stage": "A_compression", "comp_max": 20.0, "a_mode": "strict", "rv_min": 1.5, "body_max": 30.0},

    # B단계 — 조건 스윕: 압축률 10% 고정. A단계와 분리해 조건 효과를 해석한다.
    {"name": "압축10%(loose/1.5/30%)", "experiment_stage": "B_condition", "comp_max": 10.0, "a_mode": "loose", "rv_min": 1.5, "body_max": 30.0},
    {"name": "압축10%(loose/1.3/40%)", "experiment_stage": "B_condition", "comp_max": 10.0, "a_mode": "loose", "rv_min": 1.3, "body_max": 40.0},
    {"name": "압축10%(loose/1.2/45%)", "experiment_stage": "B_condition", "comp_max": 10.0, "a_mode": "loose", "rv_min": 1.2, "body_max": 45.0},
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
# 배포·실행 검증 로그 — Render에서 실제 import한 파일과 조합을 확인한다.
# 백테스트 규칙·계산·CSV 컬럼에는 영향을 주지 않는다.
# ============================================================
FRAMEWORK_VERSION = "experiment-v4-zip-bundle-collection-diagnostics-20260816"
EXPERIMENT_NAME = "state1_compression_then_condition"
SIGNAL_COOLDOWN_BARS = FORWARD_WINDOW
COLLECTION_MAX_ERROR_EXAMPLES = 3

def log_framework_identity(context):
    """실제 실행 파일과 현재 등록된 그리드를 Render 로그에 남긴다."""
    print(f"[BACKTEST_FRAMEWORK] context={context} version={FRAMEWORK_VERSION}")
    print(f"[BACKTEST_MODULE] path={os.path.abspath(__file__)}")
    print(f"[BACKTEST_PID] pid={os.getpid()}")
    print(f"[STATE1_COUNT] {len(STATE1_GRID)}")
    print(f"[STATE1_NAMES] {[p['name'] for p in STATE1_GRID]}")
    print(f"[RULE_GRID_COUNTS] {[(rule['id'], len(rule['grid'])) for rule in RULES]}")

# main.py가 이 모듈을 import할 때 1회 출력한다.
log_framework_identity("module_import")


# ============================================================
# 백테스트 실행 (인프라 — 규칙 무관 공통 로직)
# ============================================================
def fetch_ohlcv_range(ex, symbol, timeframe, since_ms, per_call_limit=200, max_calls=15, raise_on_error=False):
    """since_ms부터 현재까지 페이지네이션으로 전 구간 일봉을 가져온다.
    거래소 1회 호출당 캔들 반환 개수 제한(대부분 200개 내외)을 우회하기 위함.
    max_calls는 통신량 상한(200*15=최대 약 3000봉≈8년)이자 무한루프 방지 안전장치."""
    all_bars = []
    since = since_ms
    for _ in range(max_calls):
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=per_call_limit)
        except Exception:
            if raise_on_error:
                raise
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


# ============================================================
# 백테스트 실행 (인프라 — 규칙 무관 공통 로직)
#
# [메모리 결함수정] 이전 버전은 전체 결과(수십만 행)를 파이썬 리스트에 전부 쌓아뒀다가
# 한 번에 CSV로 뽑는 구조였다 — 규칙이 6개(조합 16개)로 늘고 기간도 3년치가 되며
# Render 서비스 메모리 한도를 초과, 운영 봇(같은 프로세스)까지 재시작되는 결함이
# 실제로 발생했다. 지금은 (1) 각 행을 즉시 디스크의 CSV 파일에 기록하고 (2) 텔레그램
# 요약에 필요한 통계(평균·승률)는 원본 수익률을 전부 보관하지 않고 합계/건수/승리건수만
# 누적하는 방식으로 바꿔, 표본 크기와 무관하게 메모리 사용량이 거의 일정하게 유지된다.
# ============================================================
def fetch_ohlcv_range(ex, symbol, timeframe, since_ms, per_call_limit=200, max_calls=15, raise_on_error=False):
    """since_ms부터 현재까지 페이지네이션으로 전 구간 일봉을 가져온다.
    거래소 1회 호출당 캔들 반환 개수 제한(대부분 200개 내외)을 우회하기 위함.
    max_calls는 통신량 상한(200*15=최대 약 3000봉≈8년)이자 무한루프 방지 안전장치."""
    all_bars = []
    since = since_ms
    for _ in range(max_calls):
        try:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=per_call_limit)
        except Exception:
            if raise_on_error:
                raise
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


# CSV 컬럼 고정 목록 — 6개 규칙의 diag 반환값 전체 합집합(직접 코드 대조로 도출).
# 스트리밍 기록을 위해 fieldnames를 실행 전에 확정해야 하므로 미리 열거해둔다.
CSV_FIELDNAMES = [
    "framework_version", "experiment_name", "experiment_stage", "signal_cooldown_bars",
    "exchange", "symbol", "t_index", "bar_date", "rule", "combo", "gate_pass", "fwd_return_pct",
    "compression_pct", "rv_t", "body_ratio", "vol_today", "vol_prev1", "vol_prev2",
    "vol_1ago", "vol_2ago", "rsi", "pattern_count", "vol_ratio_today",
]

SUMMARY_FIELDNAMES = [
    "framework_version", "experiment_name", "period_start", "period_end", "scope", "exchange",
    "rule", "combo", "experiment_stage", "evaluated", "none_count", "eligible_count",
    "diagnostic_rate_pct", "pass_count", "pass_rate_of_eligible_pct", "eligible_avg_return_pct",
    "pass_avg_return_pct", "pass_win_rate_pct", "dedup_pass_count", "dedup_pass_avg_return_pct",
    "dedup_pass_win_rate_pct", "signal_cooldown_bars",
]

COLLECTION_FIELDNAMES = [
    "framework_version", "experiment_name", "period_start", "period_end", "exchange",
    "configured_symbols", "fetch_attempted", "fetch_success", "api_error_count",
    "insufficient_bars_count", "usable_symbols", "evaluation_events", "raw_rows_written",
    "min_bars_required", "error_examples",
]


def _new_stat():
    # evaluated = 함수 호출 수, none = 진단 불성립, eligible = True/False 판정 가능 수.
    return {
        "evaluated": 0, "none": 0, "eligible": 0,
        "eligible_sum": 0.0, "eligible_win": 0,
        "pass": 0, "pass_sum": 0.0, "pass_win": 0,
        "dedup_pass": 0, "dedup_sum": 0.0, "dedup_win": 0,
    }


def _new_collection_diag(ex_id, configured_symbols, min_bars_required):
    return {
        "exchange": ex_id,
        "configured_symbols": configured_symbols,
        "fetch_attempted": 0,
        "fetch_success": 0,
        "api_error_count": 0,
        "insufficient_bars_count": 0,
        "usable_symbols": 0,
        "evaluation_events": 0,
        "raw_rows_written": 0,
        "min_bars_required": min_bars_required,
        "error_examples": [],
    }


def _add_collection_error(diag, symbol, exc):
    diag["api_error_count"] += 1
    if len(diag["error_examples"]) < COLLECTION_MAX_ERROR_EXAMPLES:
        error_name = type(exc).__name__
        error_text = str(exc).replace("\n", " ")[:180]
        diag["error_examples"].append(f"{symbol} | {error_name}: {error_text}")


def _collection_row(diag, start_date, end_date):
    return {
        "framework_version": FRAMEWORK_VERSION,
        "experiment_name": EXPERIMENT_NAME,
        "period_start": start_date or "recent_200_bars",
        "period_end": end_date or "latest_available",
        "exchange": diag["exchange"],
        "configured_symbols": diag["configured_symbols"],
        "fetch_attempted": diag["fetch_attempted"],
        "fetch_success": diag["fetch_success"],
        "api_error_count": diag["api_error_count"],
        "insufficient_bars_count": diag["insufficient_bars_count"],
        "usable_symbols": diag["usable_symbols"],
        "evaluation_events": diag["evaluation_events"],
        "raw_rows_written": diag["raw_rows_written"],
        "min_bars_required": diag["min_bars_required"],
        "error_examples": " || ".join(diag["error_examples"]),
    }


def build_collection_csv(collection_diags, start_date, end_date):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=COLLECTION_FIELDNAMES)
    writer.writeheader()
    for ex_id in SUPPORTED_EXCHANGES:
        writer.writerow(_collection_row(collection_diags[ex_id], start_date, end_date))
    return output.getvalue().encode("utf-8-sig")


def build_backtest_bundle(bundle_path, raw_path, collection_bytes, summary_bytes, period_label):
    """모바일 전송용 단일 ZIP을 디스크에서 생성한다. 원본 raw는 메모리에 올리지 않는다."""
    readme_text = (
        "CandleView 실험형 백테스트 ZIP 번들\n"
        f"framework_version: {FRAMEWORK_VERSION}\n"
        f"experiment_name: {EXPERIMENT_NAME}\n"
        f"period: {period_label}\n"
        f"warmup_bars: {WARMUP_BARS}\n"
        f"signal_cooldown_bars: {SIGNAL_COOLDOWN_BARS}\n\n"
        "파일 구성\n"
        "1. collection_diagnostics.csv: 거래소별 요청·성공·API오류·봉수미달·오류 예시\n"
        "2. summary.csv: 전체·거래소별 E/N/D/P/DP 실험 집계\n"
        "3. raw.csv: 진단 성립 개별 이벤트 원본행 (None은 summary에 집계)\n"
    )
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("README.txt", readme_text.encode("utf-8"))
        bundle.writestr("collection_diagnostics.csv", collection_bytes)
        bundle.writestr("summary.csv", summary_bytes)
        bundle.write(raw_path, arcname="raw.csv")


def summarize_collection_diagnostics(collection_diags):
    lines = ["<b>거래소 수집 진단</b>"]
    for ex_id in SUPPORTED_EXCHANGES:
        diag = collection_diags[ex_id]
        lines.append(
            f"[{ex_id}] 설정{diag['configured_symbols']} 요청{diag['fetch_attempted']} "
            f"성공{diag['fetch_success']} 사용가능{diag['usable_symbols']} "
            f"API오류{diag['api_error_count']} 봉수미달{diag['insufficient_bars_count']} "
            f"평가{diag['evaluation_events']} 원본행{diag['raw_rows_written']}"
        )
        for example in diag["error_examples"]:
            lines.append(f"  오류예: {example}")
    return "\n".join(lines)


def _mean(total, count):
    return (total / count) if count else 0.0


def _pct(part, whole):
    return (part / whole * 100.0) if whole else 0.0


def _record_result(stat, result, fwd_return_pct, dedup_accepted=False):
    """조합별 실행·진단·통과·중복제거 통계를 동시에 누적한다."""
    stat["evaluated"] += 1
    if result is None:
        stat["none"] += 1
        return

    stat["eligible"] += 1
    stat["eligible_sum"] += fwd_return_pct
    if fwd_return_pct > 0:
        stat["eligible_win"] += 1

    if not result:
        return

    stat["pass"] += 1
    stat["pass_sum"] += fwd_return_pct
    if fwd_return_pct > 0:
        stat["pass_win"] += 1

    if dedup_accepted:
        stat["dedup_pass"] += 1
        stat["dedup_sum"] += fwd_return_pct
        if fwd_return_pct > 0:
            stat["dedup_win"] += 1


def _summary_row(scope, exchange, rule_id, params, stat, start_date, end_date):
    eligible = stat["eligible"]
    passed = stat["pass"]
    dedup_passed = stat["dedup_pass"]
    return {
        "framework_version": FRAMEWORK_VERSION,
        "experiment_name": EXPERIMENT_NAME,
        "period_start": start_date or "recent_200_bars",
        "period_end": end_date or "latest_available",
        "scope": scope,
        "exchange": exchange,
        "rule": rule_id,
        "combo": params["name"],
        "experiment_stage": params.get("experiment_stage", "base_rule"),
        "evaluated": stat["evaluated"],
        "none_count": stat["none"],
        "eligible_count": eligible,
        "diagnostic_rate_pct": round(_pct(eligible, stat["evaluated"]), 4),
        "pass_count": passed,
        "pass_rate_of_eligible_pct": round(_pct(passed, eligible), 4),
        "eligible_avg_return_pct": round(_mean(stat["eligible_sum"], eligible), 4),
        "pass_avg_return_pct": round(_mean(stat["pass_sum"], passed), 4),
        "pass_win_rate_pct": round(_pct(stat["pass_win"], passed), 4),
        "dedup_pass_count": dedup_passed,
        "dedup_pass_avg_return_pct": round(_mean(stat["dedup_sum"], dedup_passed), 4),
        "dedup_pass_win_rate_pct": round(_pct(stat["dedup_win"], dedup_passed), 4),
        "signal_cooldown_bars": SIGNAL_COOLDOWN_BARS,
    }


def build_summary_csv(combo_stats, exchange_combo_stats, start_date, end_date):
    """전체·거래소별 집계를 작은 별도 CSV로 생성한다. 원본 행 CSV의 해석을 보완한다."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SUMMARY_FIELDNAMES)
    writer.writeheader()

    for rule in RULES:
        rid = rule["id"]
        for params in rule["grid"]:
            key = (rid, params["name"])
            writer.writerow(_summary_row("all", "all", rid, params, combo_stats.get(key, _new_stat()), start_date, end_date))
            for ex_id in SUPPORTED_EXCHANGES:
                ex_key = (ex_id, rid, params["name"])
                writer.writerow(_summary_row("exchange", ex_id, rid, params, exchange_combo_stats.get(ex_key, _new_stat()), start_date, end_date))

    return output.getvalue().encode("utf-8-sig")


def run_backtest_for_exchange_streaming(ex_id, quote, start_date, end_date, csv_writer,
                                         combo_stats, exchange_combo_stats):
    """원본 행·실험 통계와 함께 거래소별 수집 성공·실패 원인을 반환한다."""
    min_bars_needed = WARMUP_BARS + FORWARD_WINDOW + 10

    # 고정 유니버스가 있으면 load_markets 실패와 무관하게 어떤 심볼을 시도했는지 남긴다.
    if ex_id in FIXED_UNIVERSE:
        symbols = FIXED_UNIVERSE[ex_id][:UNIVERSE_LIMIT_PER_EXCHANGE]
    else:
        symbols = []

    collection_diag = _new_collection_diag(ex_id, len(symbols), min_bars_needed)
    try:
        ex = getattr(ccxt, ex_id)({"enableRateLimit": True, "timeout": 8000})
        if ex_id not in FIXED_UNIVERSE:
            markets = ex.load_markets()
            symbols = [
                s for s in markets
                if s.endswith(f"/{quote}") and s.split("/")[0] not in FC_STABLECOIN_BASES
            ][:UNIVERSE_LIMIT_PER_EXCHANGE]
            collection_diag["configured_symbols"] = len(symbols)
    except Exception as exc:
        _add_collection_error(collection_diag, "<exchange_init>", exc)
        return 0, collection_diag

    start_ms = _parse_date_to_ms(start_date) if start_date else None
    end_ms = _parse_date_to_ms(end_date) if end_date else None
    # start_date 직전 WARMUP_BARS 일봉을 함께 받아, 기간 첫날부터 지표가 계산되게 한다.
    fetch_since_ms = start_ms - (WARMUP_BARS * 86_400_000) if start_ms is not None else None
    row_count = 0

    for sym in symbols:
        collection_diag["fetch_attempted"] += 1
        try:
            daily = fetch_ohlcv_range(ex, sym, "1d", fetch_since_ms, raise_on_error=True) if fetch_since_ms is not None else ex.fetch_ohlcv(sym, timeframe="1d", limit=200)
            collection_diag["fetch_success"] += 1
        except Exception as exc:
            _add_collection_error(collection_diag, sym, exc)
            continue
        if len(daily) < min_bars_needed:
            collection_diag["insufficient_bars_count"] += 1
            continue

        collection_diag["usable_symbols"] += 1
        indicators = precompute_indicators(daily)
        start_idx = max(FC_MIN_BARS_STANDARD + 5, RSI_PERIOD + 5)
        # 동일 코인의 인접 신호가 같은 20봉 미래 구간을 공유하는 문제를 완화한다.
        last_accepted_idx = defaultdict(lambda: -SIGNAL_COOLDOWN_BARS - 1)

        for t in range(start_idx, len(daily) - FORWARD_WINDOW):
            bar_ts = daily[t][0]
            if end_ms is not None and bar_ts > end_ms:
                break
            if start_ms is not None and bar_ts < start_ms:
                continue

            collection_diag["evaluation_events"] += 1
            window = daily[:t + 1]
            entry_price = window[-1][4]
            future_price = daily[t + FORWARD_WINDOW][4]
            fwd_return_pct = (future_price - entry_price) / entry_price * 100.0
            bar_date_str = datetime.fromtimestamp(bar_ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

            for rule in RULES:
                rid = rule["id"]
                for params in rule["grid"]:
                    combo_name = params["name"]
                    combo_key = (rid, combo_name)
                    result, signal_diag = rule["fn"](window, indicators, t, params)

                    dedup_accepted = False
                    if result and (t - last_accepted_idx[combo_key] > SIGNAL_COOLDOWN_BARS):
                        dedup_accepted = True
                        last_accepted_idx[combo_key] = t

                    _record_result(combo_stats[combo_key], result, fwd_return_pct, dedup_accepted)
                    _record_result(exchange_combo_stats[(ex_id, rid, combo_name)], result, fwd_return_pct, dedup_accepted)

                    # 원본 행 CSV는 기존 호환성을 위해 진단 성립(True/False) 행만 기록한다.
                    # None 수와 전체 평가 수는 별도 summary CSV에 반드시 남긴다.
                    if result is None:
                        continue

                    row = {
                        "framework_version": FRAMEWORK_VERSION,
                        "experiment_name": EXPERIMENT_NAME,
                        "experiment_stage": params.get("experiment_stage", "base_rule"),
                        "signal_cooldown_bars": SIGNAL_COOLDOWN_BARS,
                        "exchange": ex_id, "symbol": sym, "t_index": t, "bar_date": bar_date_str,
                        "rule": rid, "combo": combo_name,
                        "gate_pass": result, "fwd_return_pct": round(fwd_return_pct, 4),
                    }
                    for k, v in signal_diag.items():
                        row[k] = round(v, 4) if isinstance(v, float) else v
                    csv_writer.writerow(row)
                    row_count += 1
                    collection_diag["raw_rows_written"] += 1

        del daily, indicators

    return row_count, collection_diag


def summarize_from_stats(combo_stats, start_date, end_date):
    """Telegram에는 간결한 실행·진단·통과·중복제거 요약을 전송한다."""
    period_label = f"{start_date or '최근200봉'} ~ {end_date or '현재'}"
    lines = [
        "<b>CandleView 실험형 백테스트 요약</b>",
        f"버전: {FRAMEWORK_VERSION}",
        f"기간: {period_label} | 워밍업: {WARMUP_BARS}봉 | 중복제거: 동일 코인·조합 {SIGNAL_COOLDOWN_BARS}봉",
        "E=평가, N=None, D=진단성립, P=통과, DP=중복제거 통과",
        "",
    ]
    for rule in RULES:
        rid = rule["id"]
        lines.append(f"■ [{rid}]")
        for params in rule["grid"]:
            stat = combo_stats[(rid, params["name"])]
            pass_avg = _mean(stat["pass_sum"], stat["pass"])
            dedup_avg = _mean(stat["dedup_sum"], stat["dedup_pass"])
            lines.append(
                f"{params['name']}: E{stat['evaluated']} N{stat['none']} D{stat['eligible']} "
                f"P{stat['pass']}({pass_avg:+.2f}%) DP{stat['dedup_pass']}({dedup_avg:+.2f}%)"
            )
        lines.append("")
    return "\n".join(lines)


def execute_and_report(chat_id, start_date=None, end_date=None):
    """관리자 명령으로 트리거되는 실험형 백테스트 진입점."""
    log_framework_identity("execute_and_report")
    if chat_id != ADMIN_CHAT_ID:
        return

    if end_date and not start_date:
        send_telegram_message(chat_id, "❌ 종료일만 지정할 수 없습니다. 예: /backtest 2023-01-01 2023-08-01")
        return

    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None
            if end_dt is not None and end_dt < start_dt:
                raise ValueError("종료일이 시작일보다 빠름")
        except ValueError:
            send_telegram_message(chat_id, "❌ 날짜 형식 또는 순서 오류. 예: /backtest 2023-01-01 2023-08-01")
            return

    period_label = f"{start_date} ~ {end_date or '현재'}" if start_date else "최근 약 200봉(탐색용)"
    send_telegram_message(
        chat_id,
        f"🔬 CandleView 실험형 백테스트 시작\n"
        f"기간: {period_label}\n"
        f"State1: 압축률 단독(A) + 조건 단독(B), 워밍업 {WARMUP_BARS}봉, 중복제거 {SIGNAL_COOLDOWN_BARS}봉\n"
        f"(수 분 소요; 명령을 중복 전송하지 마세요)"
    )

    run_id = int(datetime.now().timestamp())
    tmp_path = f"/tmp/candleview_backtest_raw_{run_id}.csv"
    tmp_bundle_path = f"/tmp/candleview_backtest_bundle_{run_id}.zip"
    combo_stats = defaultdict(_new_stat)
    exchange_combo_stats = defaultdict(_new_stat)
    collection_diags = {}
    total_rows = 0

    try:
        with open(tmp_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDNAMES, extrasaction="ignore")
            writer.writeheader()
            for ex_id, cfg in SUPPORTED_EXCHANGES.items():
                try:
                    n, collection_diag = run_backtest_for_exchange_streaming(
                        cfg["ccxt_id"], cfg["quote"], start_date, end_date,
                        writer, combo_stats, exchange_combo_stats
                    )
                except Exception as exc:
                    # 예상하지 못한 교환소 단위 오류도 summary에 남기고 다른 거래소는 계속 수행한다.
                    collection_diag = _new_collection_diag(ex_id, 0, WARMUP_BARS + FORWARD_WINDOW + 10)
                    _add_collection_error(collection_diag, "<exchange_run>", exc)
                    n = 0
                collection_diags[ex_id] = collection_diag
                total_rows += n
                send_telegram_message(
                    chat_id,
                    f"✓ {ex_id}: 요청{collection_diag['fetch_attempted']} 성공{collection_diag['fetch_success']} "
                    f"사용{collection_diag['usable_symbols']} API오류{collection_diag['api_error_count']} "
                    f"봉수미달{collection_diag['insufficient_bars_count']} 원본행{n}"
                )

        evaluated_total = sum(stat["evaluated"] for stat in combo_stats.values())
        if evaluated_total == 0:
            send_telegram_message(chat_id, "❌ 평가 이벤트가 없습니다. 데이터 수집·기간·거래소 로그를 확인하세요.")
            return

        collection_text = summarize_collection_diagnostics(collection_diags)
        print(collection_text.replace("<b>", "").replace("</b>", ""))
        send_telegram_message(chat_id, collection_text)

        summary_text = summarize_from_stats(combo_stats, start_date, end_date)
        print(summary_text.replace("<b>", "").replace("</b>", ""))
        send_telegram_message(chat_id, summary_text)

        collection_bytes = build_collection_csv(collection_diags, start_date, end_date)
        summary_bytes = build_summary_csv(combo_stats, exchange_combo_stats, start_date, end_date)
        # 모바일에서 파일 하나만 받도록, 세 CSV와 README를 디스크 기반 ZIP으로 묶는다.
        build_backtest_bundle(tmp_bundle_path, tmp_path, collection_bytes, summary_bytes, period_label)

        with open(tmp_bundle_path, "rb") as f:
            send_telegram_document(
                chat_id, f"candleview_backtest_bundle_{run_id}.zip", f.read(),
                caption="단일 ZIP 번들: collection_diagnostics.csv + summary.csv + raw.csv"
            )
        send_telegram_message(
            chat_id,
            "✅ 실험형 백테스트 완료. ZIP 파일 하나만 보관·업로드하면 됩니다. 상수 반영 전에는 탐색·검증·사후검증 기간을 분리해 summary.csv를 비교하세요."
        )
    finally:
        for path in (tmp_path, tmp_bundle_path):
            if os.path.exists(path):
                os.remove(path)
