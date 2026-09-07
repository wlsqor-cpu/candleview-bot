"""
fc_core_scoring.py — FindCoin FC-2(State 자동진단) + FC-3(8대 미시모듈·S_scout) 순수 파이썬 구현.

[배경] 기존엔 Gemini가 State1/2/3 진단과 8개 모듈 계산을 텍스트 프롬프트 안에서 수행했다.
그런데 이 계산들은 전부 CandleView_API 본체 14장에 이미 정확한 수식으로 명시된 결정론적
연산이라 — 처음부터 판단(judgment)이 필요한 부분이 아니었다. 실제 판단이 필요한 부분은
FC-4(진입가·목표가, 매물대/컨플루언스존 기반)뿐이며, 이번 재설계에서 FindCoin은 진입가를
산출하지 않기로 확정됐으므로(사용자 지시 — 가격분석은 캔들뷰 본체가 담당), FindCoin
전체가 Gemini 없이 순수 파이썬으로 완결 가능해졌다.

[정의 출처] 모든 수식·임계값은 CandleView_API 본체 14장 스펙에서 그대로 이식했다(신규
발명 아님). 상수는 main.py에서 _spec_number()로 스펙 원문을 직접 파싱해 전달받는다(SSOT
유지 — 이 모듈은 숫자를 재정의하지 않음).

[재사용] State1 옥석A(바닥유동성사냥)는 fc_observation_tier1.tier1_spring_reclaim의
strict_reclaim과 완전히 동일한 정의라 그대로 재사용한다. State3 옥석A(매도고갈)는
tier1_selling_exhaustion과 동일해 재사용한다. State3 옥석B(매물대지지)는
fc_state3_structure.py(OB/FVG/Confluence/RoleReversal)를 그대로 사용한다. 이 모듈
자체는 순수함수이며 main.py를 참조하지 않는다 — 필요한 상수는 전부 함수 인자로 전달받는다.

[입력 형식] daily_bars: ccxt OHLCV [ts,o,h,l,c,v], 오름차순. 이 모듈의 모든 함수는
"완성봉만" 받는 것을 전제로 한다(진행봉 포함 여부는 호출측 책임 — main.py의 진행봉
처리 통일 규칙을 그대로 따를 것).
"""

from __future__ import annotations
import numpy as np

MAJOR_SWING_AMP_RATIO = 0.60


def _find_pivots(series_high, series_low, left=2, right=2):
    n = len(series_high)
    highs, lows = [], []
    for i in range(left, n - right):
        window_h = series_high[i - left: i + right + 1]
        window_l = series_low[i - left: i + right + 1]
        if series_high[i] >= window_h.max() and list(window_h).count(series_high[i]) == 1:
            highs.append(i)
        if series_low[i] <= window_l.min() and list(window_l).count(series_low[i]) == 1:
            lows.append(i)
    return highs, lows


def _filter_major_price_pivots(highs, lows, high_arr, low_arr, amp_ratio=MAJOR_SWING_AMP_RATIO):
    events = sorted([(i, "H", float(high_arr[i])) for i in highs] +
                     [(i, "L", float(low_arr[i])) for i in lows])
    if len(events) < 2:
        return highs, lows
    amps = []
    keep_H, keep_L = set(), set()
    keep_H.add(events[0][0]) if events[0][1] == "H" else keep_L.add(events[0][0])
    for k in range(1, len(events)):
        _, _, v_prev = events[k - 1]
        i_cur, t_cur, v_cur = events[k]
        amp = abs(v_cur - v_prev)
        if not amps:
            (keep_H if t_cur == "H" else keep_L).add(i_cur)
            amps.append(amp)
            continue
        ref = sum(amps[-3:]) / len(amps[-3:])
        if amp >= ref * amp_ratio:
            (keep_H if t_cur == "H" else keep_L).add(i_cur)
            amps.append(amp)
    return sorted(keep_H), sorted(keep_L)


def find_major_swings(bars):
    """완성봉 배열에서 주요 스윙고점/저점 인덱스 리스트 반환(오름차순)."""
    if len(bars) < 6:
        return [], []
    highs = np.array([b[2] for b in bars])
    lows = np.array([b[3] for b in bars])
    highs_raw, lows_raw = _find_pivots(highs, lows, left=2, right=2)
    return _filter_major_price_pivots(highs_raw, lows_raw, highs, lows)


def compute_rv_t(bars, at_idx, lookback):
    """RV_t = Volume_t / (직전 lookback봉 평균거래량 + EPSILON). 본체 스펙 정의 그대로."""
    if at_idx < 0 or at_idx >= len(bars):
        return None
    window = bars[max(0, at_idx - lookback):at_idx]
    if not window:
        return None
    avg_vol = sum(b[5] for b in window) / len(window)
    return float(bars[at_idx][5] / (avg_vol + 1e-8))


def _body_ratio_pct(bar):
    rng = bar[2] - bar[3]
    body = abs(bar[4] - bar[1])
    return (body / rng * 100.0) if rng > 0 else 100.0


def module_absorption(bars, at_idx, vol_absorption_min, vol_absorption_body_max):
    """모듈4 S_absorption. RV_t<MIN→0. vol_score=min((RV_t-MIN)/MIN,1.0).
    body_ratio>BODY_MAX→0. body_score=(BODY_MAX-body_ratio)/BODY_MAX. S=vol_score*body_score."""
    rv_t = compute_rv_t(bars, at_idx, lookback=20)
    if rv_t is None or rv_t < vol_absorption_min:
        return 0.0
    vol_score = min((rv_t - vol_absorption_min) / vol_absorption_min, 1.0)
    body_ratio = _body_ratio_pct(bars[at_idx])
    if body_ratio > vol_absorption_body_max:
        return 0.0
    body_score = (vol_absorption_body_max - body_ratio) / vol_absorption_body_max
    return float(vol_score * body_score)


def module_vol_accel(bars, at_idx, vroc_min, vroc_max):
    """모듈6 S_vol_accel(VROC). recent_avg=최근3일, prior_avg=그 직전3일.
    6일 미만 확보 시 균등분할(축소룩백 규정 준용)."""
    available = bars[:at_idx + 1]
    n = len(available)
    if n < 2:
        return 0.0
    if n >= 6:
        recent, prior = available[-3:], available[-6:-3]
    else:
        half = n // 2
        prior = available[:half] if half > 0 else available[:1]
        recent = available[half:] if half < n else available[-1:]
    if not recent or not prior:
        return 0.0
    recent_avg = sum(b[5] for b in recent) / len(recent)
    prior_avg = sum(b[5] for b in prior) / len(prior)
    ratio = recent_avg / (prior_avg + 1e-8)
    if ratio <= vroc_min:
        return 0.0
    if ratio >= vroc_max:
        return 1.0
    return float(ratio - vroc_min)


def module_depth(bid_depth_10, ask_depth_10):
    """모듈3 S_depth. ratio=Ask/(Bid+EPSILON). ratio≥1.0→0(매도벽우위).
    0.5<ratio<1.0→(1.0-ratio)/0.5. ratio≤0.5→1.0(매수벽2배)."""
    if bid_depth_10 is None or ask_depth_10 is None:
        return None
    ratio = ask_depth_10 / (bid_depth_10 + 1e-8)
    if ratio >= 1.0:
        return 0.0
    if ratio <= 0.5:
        return 1.0
    return float((1.0 - ratio) / 0.5)


def compute_rs_raw(bars, btc_bars, at_idx, windows_weights):
    """모듈7 1단계: RS_raw = 가중합 P_N(코인)-P_N(BTC). windows_weights: [(N, weight), ...].
    데이터 부족 구간은 제외 후 남은 가중치로 재정규화(스펙 명시 원칙)."""
    def pct_change(arr, idx, n):
        if idx - n < 0:
            return None
        base = arr[idx - n][4]
        if base <= 0:
            return None
        return (arr[idx][4] - base) / base

    total_w, weighted_sum = 0.0, 0.0
    for n, w in windows_weights:
        p_coin = pct_change(bars, at_idx, n)
        p_btc = pct_change(btc_bars, at_idx, n)
        if p_coin is None or p_btc is None:
            continue
        weighted_sum += w * (p_coin - p_btc)
        total_w += w
    if total_w <= 0:
        return None
    return float(weighted_sum / total_w)


def compute_relstrength_percentile(rs_raw_list):
    """모듈7 2단계: RS_raw 교차 백분위 → [0,1]. FC-1[2]와 동일 산식 구조 재사용.
    입력: [(candidate_id, rs_raw_or_None), ...]. None은 순위 계산에서 제외하고 0.0 반환."""
    valid = [(cid, v) for cid, v in rs_raw_list if v is not None]
    n = len(valid)
    result = {cid: 0.0 for cid, _ in rs_raw_list}
    if n == 0:
        return result
    ranked = sorted(valid, key=lambda x: x[1], reverse=True)
    for rank, (cid, _) in enumerate(ranked, start=1):
        percentile = (1 - (rank - 1) / n) * 100.0
        result[cid] = float(percentile / 100.0)
    return result


def diagnose_state1(bars, at_idx, compression_pct, compression_max,
                     vol_absorption_min, vol_absorption_body_max,
                     spring_reclaim_fn):
    """State1(분출전 매집/응축). 진단: compression_pct≤MAX(이미 계산됨, None이면 미압축/이미돌파).
    옥석A: spring_reclaim_fn(바닥유동성사냥, tier1_spring_reclaim.strict_reclaim 재사용).
    옥석B: RV_t≥VOL_ABSORPTION_MIN AND body_ratio≤VOL_ABSORPTION_BODY_MAX."""
    if compression_pct is None or compression_pct > compression_max:
        return {"diagnosed": False}
    spring = spring_reclaim_fn(bars[:at_idx + 1], 40)
    ok_a = bool(spring.get("strict_reclaim")) if spring.get("status") == "ok" else False
    rv_t = compute_rv_t(bars, at_idx, lookback=20)
    body_ratio = _body_ratio_pct(bars[at_idx])
    ok_b = (rv_t is not None and rv_t >= vol_absorption_min and body_ratio <= vol_absorption_body_max)
    if not (ok_a and ok_b):
        return {"diagnosed": False}
    s_structure = min((rv_t - vol_absorption_min) / vol_absorption_min, 1.0) if rv_t else 0.0
    return {"diagnosed": True, "state": "State1", "s_structure": float(s_structure),
            "ok_a": ok_a, "ok_b": ok_b}


def diagnose_state2(bars, at_idx, major_highs, body_ratio_strong):
    """State2(거래량급증 돌파초입). 진단: 최근3봉 이내 주요SH 종가돌파 AND RV_t≥2.0.
    옥석A: Close>SH AND body_ratio>BODY_RATIO_STRONG.
    옥석B: RV_{t+1}≥1.0(직후봉 존재 시에만 판정, 오늘이 돌파봉이면 보류)."""
    for i in range(at_idx, max(at_idx - 3, -1), -1):
        prior_highs = [h for h in major_highs if h < i]
        if not prior_highs:
            continue
        sh_level = bars[prior_highs[-1]][2]
        if bars[i][4] <= sh_level:
            continue
        rv_t = compute_rv_t(bars, i, lookback=20)
        if rv_t is None or rv_t < 2.0:
            continue
        body_ratio = _body_ratio_pct(bars[i])
        ok_a = body_ratio > body_ratio_strong
        if i + 1 <= at_idx:
            rv_next = compute_rv_t(bars, i + 1, lookback=20)
            ok_b = rv_next is not None and rv_next >= 1.0
        else:
            ok_b = None
        if not (ok_a and ok_b):
            continue
        s_structure = min((body_ratio - body_ratio_strong) / (100.0 - body_ratio_strong), 1.0)
        return {"diagnosed": True, "state": "State2", "s_structure": float(s_structure),
                "breakout_idx": i, "ok_a": ok_a, "ok_b": ok_b}
    return {"diagnosed": False}


def diagnose_state3(bars, at_idx, body_ratio_weak, selling_exhaustion_fn,
                     ob_fvg_detector_fn, touch_checker_fn, role_reversal_tolerance):
    """State3(상위추세 중 눌림목).
    옥석A: 매도고갈(tier1_selling_exhaustion.volume_stepdown_3bar AND body_ratio≤WEAK, 재사용).
    옥석B: 매물대(OB/FVG/Confluence/RoleReversal) 접촉+반등(fc_state3_structure 재사용)."""
    sliced = bars[:at_idx + 1] + [bars[at_idx]]
    exhaustion = selling_exhaustion_fn(sliced, 40)
    ok_a = False
    body_ratio = None
    if exhaustion.get("status") == "ok":
        body_ratio = exhaustion.get("last_body_ratio")
        ok_a = bool(exhaustion.get("volume_stepdown_3bar")) and (
            body_ratio is not None and body_ratio * 100.0 <= body_ratio_weak
        )
    if not ok_a:
        return {"diagnosed": False}

    structure = ob_fvg_detector_fn(bars[:at_idx])
    today = bars[at_idx]
    touch = touch_checker_fn(today[3], today[1], today[4], structure, tolerance=role_reversal_tolerance)
    ok_b = bool(touch.get("state3_condition_b_met")) if touch.get("status") == "ok" else False
    if not ok_b:
        return {"diagnosed": False}

    s_structure = (body_ratio_weak - (body_ratio * 100.0)) / body_ratio_weak if body_ratio else 0.0
    return {"diagnosed": True, "state": "State3", "s_structure": float(max(0.0, s_structure)),
            "ok_a": ok_a, "ok_b": ok_b, "touched": touch.get("touched_structures")}


def resolve_state(state1_result, state2_result, state3_result):
    """State 판정 우선순위: State2 > State3 > State1(확정성 높은 순, 본체 스펙 명시)."""
    if state2_result.get("diagnosed"):
        return state2_result
    if state3_result.get("diagnosed"):
        return state3_result
    if state1_result.get("diagnosed"):
        return state1_result
    return {"diagnosed": False, "state": None, "s_structure": 0.0}


def aggregate_s_scout(modules, weights, taker_supported, confidence=1.0):
    """8모듈 가중합 → S_scout(0~10). taker_supported=False면 W_TAKERBUY=0 처리 후 나머지
    7개를 W_NO_TAKER_DENOM으로 나눠 재정규화(스펙 명시)."""
    w = dict(weights)
    if not taker_supported:
        denom = weights.get("no_taker_denom", 1.0)
        w = {k: (0.0 if k == "taker_buy" else v / denom) for k, v in weights.items() if k != "no_taker_denom"}
        modules = dict(modules)
        modules["taker_buy"] = 0.0
    weighted_sum = sum(modules.get(k, 0.0) * w.get(k, 0.0) for k in
                        ["vol_squeeze", "taker_buy", "depth", "absorption",
                         "structure", "vol_accel", "relstrength", "boxrange"])
    s_scout = weighted_sum * 10.0 * confidence
    return float(s_scout)
