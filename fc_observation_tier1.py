"""
fc_observation_tier1.py — FindCoin Phase A: Tier 1 관측 지표 모듈 (API 비용 0)

[모듈식 무간섭 확장 원칙]
기존 main.py FC-0~FC-5 코어 게이트 로직(fc_compute_rtm/compression/range_expansion/
liquidity_ratio/box_range, run_findcoin_scan)을 전혀 수정·호출하지 않는다. main.py를
import하지도 않는다(순환참조 회피 — Phase E 연결 시 항상 main.py → 이 모듈 단방향).
필요한 SSOT 상수(lookback 등)는 호출측(main.py)이 파라미터로 넘긴다. 이 모듈은
숫자를 재정의하지 않고 전달받아 쓰기만 한다.

[Phase C(관측전용) 설계 원칙]
이 모듈은 통과/탈락 이진 게이트를 만들지 않는다. RTM·State1이 검증 없이 먼저 컷오프를
확정했다가 실측(2026-09-04)에서 기각된 전례의 재발 방지 — 여기서는 원값(raw)과
방향정규화 연속점수(normalized, 0~1, 높을수록 '분출직전 형태'에 가까움)만 산출한다.
실제 컷오프는 Phase D에서 baseline 대비 사후채점으로만 결정한다.

[데이터 결손 처리 — 긍정적 상태수렴 원칙]
계산 불가 시 status="insufficient_data"로 명시한다("실패"로 표기하지 않음). 개별 지표
결손이 전체 산출을 막지 않는다(coverage-first, FC-Next 설계원칙과 정합).

[입력 형식]
daily_bars: ccxt OHLCV 형식 [timestamp, open, high, low, close, volume], 오름차순,
            마지막 원소는 오늘의 진행봉(미종료). fc_compute_rtm의 daily_ohlcv_cache와
            동일 형식 — 신규 fetch 없이 그대로 전달 가능.
lookback:   구조적 관측 창(main.py FC_MIN_BARS_STANDARD를 그대로 전달할 것. 이 모듈에서
            재정의하지 않음).

[Phase A 범위에서 재구현하지 않는 항목 — 기존 main.py 함수를 Phase E에서 그대로 재사용]
- #2/#8 (ATR수축비/저거래량압축): fc_compute_range_expansion()이 이미 계산하는
  vol_squeeze_ratio·s_vol_squeeze를 그대로 쓸 것. 여기서 재계산하지 않음(로직 중복 방지).
- #5 (VCP/압축): fc_compute_compression()의 compression_pct를 그대로 쓸 것.
- #15 (저항직하응축): fc_compute_box_range()의 s_boxrange/box_status를 그대로 쓸 것.

[Phase A 범위에서 보류 — 정정 사항]
- #14 (거래대금 순위의 점진적 상승 추세): 원래 설계에서 "단일 스냅샷 기반 Tier1"로
  분류했으나, 문자 그대로의 "순위 추이"는 과거 각 시점의 유니버스 전체 순위가 필요해
  단일 스냅샷(오늘 하루의 daily_bars)만으로는 계산할 수 없음 — 재검토 중 자체 발견,
  Tier3(D7/D8 영속축적 필요)로 재분류. 이번 모듈에 구현하지 않음.
- #18 (장기 하락추세선 이탈 후 재테스트 성공): 추세선 적합 + 이탈 + 재테스트 3단계
  시퀀스 탐지는 과적합 위험이 커 근거 없는 휴리스틱을 만들지 않기 위해 보류.
"""

from __future__ import annotations


# ------------------------------------------------------------------
# 공통 헬퍼
# ------------------------------------------------------------------

def _completed_bars(daily_bars):
    """오늘 진행봉 제외, 완성봉만. main.py의 compression_pct/range_expansion과 동일 원칙
    (진행봉 처리 통일 규칙 — 신규 원칙 발명 아님, 그대로 재사용)."""
    if not daily_bars or len(daily_bars) < 2:
        return []
    return daily_bars[:-1]


def _linreg_slope(values):
    """values의 단순 선형회귀 기울기(x=0..n-1). n<3이면 None."""
    n = len(values)
    if n < 3:
        return None
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den == 0:
        return None
    return num / den


# ------------------------------------------------------------------
# 개별 지표 함수 — 각각 독립적으로 실패해도 무관(insufficient_data 반환)
# ------------------------------------------------------------------

def tier1_nr_n(daily_bars, lookback):
    """#1 NR-N: 오늘 완성봉의 레인지가 최근 lookback봉 중 얼마나 좁은 축에 속하는가."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 5:
        return {"status": "insufficient_data"}
    ranges = [b[2] - b[3] for b in window]
    today_range = ranges[-1]
    narrower_count = sum(1 for r in ranges if r < today_range)
    pct = narrower_count / len(ranges)  # 낮을수록(0에 가까울수록) 압축 — 방향 주의
    return {
        "status": "ok",
        "n_bars": len(window),
        "today_range": today_range,
        "narrower_than_pct": round(pct, 4),
        "normalized": round(1 - pct, 4),  # 방향정규화: 높을수록 압축
        "is_narrowest_in_window": today_range == min(ranges),
    }


def tier1_range_pctile_self_history(daily_bars, lookback):
    """#3 밴드폭 백분위: 오늘 압축도가 이 종목 자기이력(최대 200봉) 상 몇 백분위인가.
    D10 실측(200봉 조회 가능, 비용 0) 확정 이후에만 표본이 충분해지는 지표."""
    completed = _completed_bars(daily_bars)
    if len(completed) < lookback + 20:
        return {"status": "insufficient_data"}
    series = []
    for i in range(lookback, len(completed) + 1):
        window = completed[i - lookback:i]
        hi = max(b[2] for b in window)
        lo = min(b[3] for b in window)
        if hi <= 0:
            continue
        series.append((hi - lo) / hi * 100.0)
    if len(series) < 10:
        return {"status": "insufficient_data"}
    today_val = series[-1]
    lower_count = sum(1 for v in series if v < today_val)
    pct = lower_count / len(series)
    return {
        "status": "ok",
        "sample_n": len(series),
        "today_compression_pct": round(today_val, 4),
        "self_history_percentile": round(pct, 4),  # 낮을수록 자기이력상 이례적으로 좁음
        "normalized": round(1 - pct, 4),
    }


def tier1_inside_bar_streak(daily_bars, lookback):
    """#4 인사이드바 연속출현(레인지 포괄조건). 몸통비율 완화판정은 별도 스펙 지표라 여기선
    다루지 않음 — 레인지 포괄 여부만 순수하게 측정."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 3:
        return {"status": "insufficient_data"}
    streak = 0
    for i in range(len(window) - 1, 0, -1):
        cur_h, cur_l = window[i][2], window[i][3]
        prev_h, prev_l = window[i - 1][2], window[i - 1][3]
        if cur_h <= prev_h and cur_l >= prev_l:
            streak += 1
        else:
            break
    return {
        "status": "ok",
        "inside_bar_streak": streak,
        "normalized": round(min(streak / 4.0, 1.0), 4),  # 스케일용 상한일 뿐 게이트 아님
    }


def tier1_no_supply(daily_bars, lookback):
    """#6 No-Supply 근사: 직전 3봉 연속 거래량 감소 + 최근봉 몸통 축소."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 4:
        return {"status": "insufficient_data"}
    v = [b[5] for b in window[-3:]]
    declining = v[0] > v[1] > v[2]
    avg_range = sum(b[2] - b[3] for b in window) / len(window)
    last_range = window[-1][2] - window[-1][3]
    small_body = (last_range <= avg_range * 0.7) if avg_range > 0 else False
    return {
        "status": "ok",
        "volume_declining_3bar": declining,
        "last_range_vs_avg": round(last_range / (avg_range + 1e-8), 4),
        "normalized": round((1.0 if declining else 0.0) * 0.5 + (1.0 if small_body else 0.0) * 0.5, 4),
    }


def tier1_selling_exhaustion(daily_bars, lookback):
    """#9 매도고갈: 조정구간 3봉 연속 거래량 계단식 감소 + 오늘 몸통 작음.
    spec 원문(pre-breakout-signal-characteristics.md 항목6, rule_state3_selling_exhaustion)의
    daily-bar 버전 — 판정기준 신규 발명 아님, 기존 정의 그대로 이식."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 4:
        return {"status": "insufficient_data"}
    vols = [b[5] for b in window[-3:]]
    stepdown = vols[0] > vols[1] > vols[2]
    last_body = abs(window[-1][4] - window[-1][1])
    last_range = window[-1][2] - window[-1][3]
    body_ratio = (last_body / last_range) if last_range > 0 else 0.0
    small_body = body_ratio <= 0.3
    return {
        "status": "ok",
        "volume_stepdown_3bar": stepdown,
        "last_body_ratio": round(body_ratio, 4),
        "normalized": round((1.0 if stepdown else 0.0) * 0.5 + (1.0 if small_body else 0.0) * 0.5, 4),
    }


def tier1_absorption(daily_bars, lookback):
    """#10 흡수: 거래량 1.5배↑ + 몸통비율 30%↓. spec 항목3(rule_absorption)과 동일 정의."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 5:
        return {"status": "insufficient_data"}
    avg_vol = sum(b[5] for b in window[:-1]) / max(len(window) - 1, 1)
    last_vol = window[-1][5]
    vol_ratio = last_vol / (avg_vol + 1e-8)
    last_range = window[-1][2] - window[-1][3]
    last_body = abs(window[-1][4] - window[-1][1])
    body_pct = (last_body / last_range) if last_range > 0 else 1.0
    is_absorption = vol_ratio >= 1.5 and body_pct <= 0.3
    return {
        "status": "ok",
        "volume_ratio_vs_avg": round(vol_ratio, 4),
        "body_pct_of_range": round(body_pct, 4),
        "normalized": 1.0 if is_absorption else 0.0,
    }


def tier1_relative_strength_btc(daily_bars, btc_daily_bars, lookback):
    """#11 BTC 상대강도. [근사 고지] btc_daily_bars는 코인 daily_bars와 별도 호출로 받아온
    배열이라 봉 개수는 같아도 타임스탬프 1:1 정합은 보장되지 않음(±수 시간 오차 가능) —
    관측용 근사치로만 취급, 정밀 정합이 필요하면 타임스탬프 교차검증 별도 필요."""
    completed = _completed_bars(daily_bars)
    btc_completed = _completed_bars(btc_daily_bars) if btc_daily_bars else []
    if len(completed) < lookback or len(btc_completed) < lookback:
        return {"status": "insufficient_data"}
    coin_w = completed[-lookback:]
    btc_w = btc_completed[-lookback:]
    if coin_w[0][4] <= 0 or btc_w[0][4] <= 0:
        return {"status": "insufficient_data"}
    coin_ret = (coin_w[-1][4] / coin_w[0][4]) - 1.0
    btc_ret = (btc_w[-1][4] / btc_w[0][4]) - 1.0
    rs = coin_ret - btc_ret
    return {
        "status": "ok",
        "coin_return": round(coin_ret, 4),
        "btc_return": round(btc_ret, 4),
        "relative_strength": round(rs, 4),  # 양수 = BTC 대비 초과수익
        "normalized": round(max(0.0, min(1.0, 0.5 + rs)), 4),  # ±50%p를 0~1로 근사 매핑(스케일용)
    }


def tier1_universe_relative_return(item_return, universe_returns):
    """#12 하락미참여율: 동일 스냅샷 유니버스 수익률 분포 대비 이 종목의 위치.
    run_findcoin_scan의 rtm_results 전체에서 각 item의 coin_return(위 #11과 동일 정의,
    lookback 수익률)을 모아 universe_returns로 넘길 것 — 2차 패스(교차단면) 함수."""
    if item_return is None or not universe_returns or len(universe_returns) < 10:
        return {"status": "insufficient_data"}
    s = sorted(universe_returns)
    n = len(s)
    median = s[n // 2] if n % 2 == 1 else (s[n // 2 - 1] + s[n // 2]) / 2
    rank = sum(1 for r in s if r < item_return) / n
    return {
        "status": "ok",
        "universe_n": n,
        "universe_median_return": round(median, 4),
        "excess_vs_median": round(item_return - median, 4),
        "universe_percentile": round(rank, 4),
        "normalized": round(rank, 4),
    }


def tier1_turnover_trend(daily_bars, lookback, recent_window=5):
    """#13 거래대금 회전율 추세: 최근 recent_window봉 평균 거래대금 / 그 이전 구간 평균.
    Phase C 원칙상 등급 판정 없이 비율(turnover_ratio)만 산출 — '완만한 상승' 여부의 판단은
    Phase D 사후채점 이후로 유보."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < recent_window * 2:
        return {"status": "insufficient_data"}

    def qv(bar):
        return bar[4] * bar[5]

    recent_avg = sum(qv(b) for b in window[-recent_window:]) / recent_window
    prior = window[:-recent_window]
    prior_avg = sum(qv(b) for b in prior) / len(prior) if prior else 0.0
    if prior_avg <= 0:
        return {"status": "insufficient_data"}
    ratio = recent_avg / prior_avg
    return {
        "status": "ok",
        "recent_avg_quote_volume": round(recent_avg, 2),
        "prior_avg_quote_volume": round(prior_avg, 2),
        "turnover_ratio": round(ratio, 4),
    }


def tier1_ascending_triangle(daily_bars, lookback):
    """#16 저점절상+고점평탄: 스윙포인트 탐지 대신 선형회귀 기울기 사용.
    [설계근거] main.py box_range 설계기록(판정52번)이 극단압축에서 스윙탐지가 실패함을
    시뮬레이션으로 이미 확인한 전례가 있어(연속봉 인접 시 스윙이 1개로 뭉개짐), 동일 실패를
    피하기 위해 회귀 기울기로 대체 — 압축 국면에서도 죽지 않는 연속값."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 10:
        return {"status": "insufficient_data"}
    highs = [b[2] for b in window]
    lows = [b[3] for b in window]
    high_slope = _linreg_slope(highs)
    low_slope = _linreg_slope(lows)
    avg_price = sum(b[4] for b in window) / len(window)
    if high_slope is None or low_slope is None or avg_price <= 0:
        return {"status": "insufficient_data"}
    high_slope_pct = high_slope / avg_price
    low_slope_pct = low_slope / avg_price
    lows_rising = low_slope_pct > 0
    highs_flat = abs(high_slope_pct) < abs(low_slope_pct) * 0.5
    return {
        "status": "ok",
        "high_slope_pct_per_bar": round(high_slope_pct, 6),
        "low_slope_pct_per_bar": round(low_slope_pct, 6),
        "lows_rising": lows_rising,
        "highs_flat_relative": highs_flat,
        "normalized": 1.0 if (lows_rising and highs_flat) else (0.5 if lows_rising else 0.0),
    }


def tier1_spring_reclaim(daily_bars, lookback):
    """#17 스프링/스탑헌트 후 회복. spec 원문(pre-breakout-signal-characteristics.md 항목2,
    State1 A조건) 그대로 이식 — 신규 판단기준 발명 아님.
    strict: 오늘 저가가 최근 lookback봉(오늘 제외) 최저가(SL) 아래로 뚫었다가 종가는 SL 위로 마감.
    loose: 종가가 SL의 105% 이내에서 양봉 마감."""
    completed = _completed_bars(daily_bars)
    if len(completed) < lookback or not daily_bars:
        return {"status": "insufficient_data"}
    window = completed[-lookback:]
    sl = min(b[3] for b in window)
    today = daily_bars[-1]
    today_open, today_low, today_close = today[1], today[3], today[4]
    strict = (today_low < sl) and (today_close >= sl)
    loose = (today_close <= sl * 1.05) and (today_close > today_open)
    return {
        "status": "ok",
        "recent_low_sl": sl,
        "today_low": today_low,
        "today_close": today_close,
        "strict_reclaim": strict,
        "loose_reclaim": loose,
        "normalized": 1.0 if strict else (0.5 if loose else 0.0),
    }


def tier1_base_maturity(daily_bars, lookback, anchor_bars=5):
    """#19 베이스 성숙도: 최근 anchor_bars로 밴드를 고정한 뒤, 그 밴드 안에 얼마나 오래(봉수)
    머물러 있었는지 역방향으로 연장 측정. [설계주의] 밴드를 lookback 전체 창에서 뽑으면
    구성상 모든 봉이 항상 밴드 안에 있어(항상 만점) 항진명제가 되므로, 반드시 짧은 anchor로
    밴드를 먼저 고정한 뒤 확장하는 순서를 지킨다."""
    completed = _completed_bars(daily_bars)
    if len(completed) < anchor_bars + 1:
        return {"status": "insufficient_data"}
    anchor = completed[-anchor_bars:]
    band_high = max(b[2] for b in anchor)
    band_low = min(b[3] for b in anchor)
    if band_high <= 0:
        return {"status": "insufficient_data"}
    window = completed[-lookback:]
    streak = 0
    for b in reversed(window):
        if b[2] <= band_high and b[3] >= band_low:
            streak += 1
        else:
            break
    return {
        "status": "ok",
        "band_high": band_high,
        "band_low": band_low,
        "base_maturity_bars": streak,
        "normalized": round(min(streak / lookback, 1.0), 4),
    }


def tier1_days_since_low(daily_bars, lookback):
    """#20 N일 신저가 이후 경과일. 동률 시 가장 최근 발생 시점을 채택(보수적 — 신저가를
    아직 소화 중인 것으로 취급)."""
    completed = _completed_bars(daily_bars)
    window = completed[-lookback:]
    if len(window) < 5:
        return {"status": "insufficient_data"}
    lows = [b[3] for b in window]
    min_low = min(lows)
    idx_of_min = max(i for i, v in enumerate(lows) if v == min_low)
    days_since = (len(lows) - 1) - idx_of_min
    return {
        "status": "ok",
        "period_low": min_low,
        "days_since_period_low": days_since,
        "normalized": round(min(days_since / lookback, 1.0), 4),
    }


# ------------------------------------------------------------------
# 오케스트레이터
# ------------------------------------------------------------------

def compute_tier1_observation(
    daily_bars,
    lookback,
    btc_daily_bars=None,
    universe_returns=None,
    item_return_for_universe=None,
):
    """13개 신규 지표를 단일 dict로 통합 산출.
    [coverage-first] 개별 지표의 insufficient_data는 전체 산출을 막지 않는다 — 결손을
    숨기지 않고 coverage_summary로 그대로 노출한다(FC-Next 설계원칙과 정합).

    Phase E 연결 시 main.py 쪽에서 다음도 함께 채워 최종 관측 레코드를 구성할 것
    (이 함수의 반환값과 병합, 이 모듈에서 재계산하지 않음):
      - vol_squeeze: item["vol_squeeze_ratio"], item["s_vol_squeeze"]  (fc_compute_range_expansion)
      - compression: item["compression_pct"]                          (fc_compute_compression)
      - box_range:   item["s_boxrange"], item["box_status"]           (fc_compute_box_range)
    """
    obs = {
        "nr_n": tier1_nr_n(daily_bars, lookback),
        "range_pctile_self_history": tier1_range_pctile_self_history(daily_bars, lookback),
        "inside_bar_streak": tier1_inside_bar_streak(daily_bars, lookback),
        "no_supply": tier1_no_supply(daily_bars, lookback),
        "selling_exhaustion": tier1_selling_exhaustion(daily_bars, lookback),
        "absorption": tier1_absorption(daily_bars, lookback),
        "relative_strength_btc": (
            tier1_relative_strength_btc(daily_bars, btc_daily_bars, lookback)
            if btc_daily_bars else {"status": "insufficient_data"}
        ),
        "universe_relative_return": (
            tier1_universe_relative_return(item_return_for_universe, universe_returns)
            if (item_return_for_universe is not None and universe_returns is not None)
            else {"status": "insufficient_data"}
        ),
        "turnover_trend": tier1_turnover_trend(daily_bars, lookback),
        "ascending_triangle": tier1_ascending_triangle(daily_bars, lookback),
        "spring_reclaim": tier1_spring_reclaim(daily_bars, lookback),
        "base_maturity": tier1_base_maturity(daily_bars, lookback),
        "days_since_low": tier1_days_since_low(daily_bars, lookback),
    }
    n_total = len(obs)  # coverage_summary 추가 전 시점의 항목 수(13개 지표)
    n_ok = sum(1 for v in obs.values() if v.get("status") == "ok")
    obs["coverage_summary"] = {
        "computed": n_ok,
        "total": n_total,
        "insufficient": n_total - n_ok,
    }
    return obs
