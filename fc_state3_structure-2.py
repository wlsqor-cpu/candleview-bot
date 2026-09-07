"""
fc_state3_structure.py — FindCoin State3(눌림목) 옥석검증 B조건: OB/FVG/Confluence Zone/
Role Reversal 매물대 지지 판정.

[정의 출처] CandleView_API 본체 스펙(신규 발명 아님, 그대로 이식):
- BOS: 추세방향 종가가 직전 주요 스윙 극값을 몸통(종가) 마감으로 돌파
- CHoCH: 반대방향 종가가 직전 주요 스윙을 돌파(성격전환) — 본 모듈은 OB 판정 목적상 BOS/CHoCH를
  구분하지 않는다(스펙상 둘 다 OB 트리거 자격이 동일하므로 구분 불필요).
- OB(Order Block): 급격한 BOS/CHoCH 발생 직전의 마지막 반대방향 캔들 가격 범위
- FVG(Fair Value Gap): 3봉 구조 내 1번봉 꼬리와 3번봉 꼬리가 겹치지 않는 가격 공백,
  중심가 CE=(상단+하단)/2
- Confluence Zone: FVG와 OB의 면적 교집합이 CONFLUENCE_AREA_MIN(50.0%) 이상
- Role Reversal: 현재가가 돌파된 직전 주요 스윙 ROLE_REVERSAL_TOLERANCE(±1.5%) 이내에서
  종가 기준 유지

[재사용] 스윙탐지는 main.py _find_pivots/_filter_major_price_pivots(RSI다이버전스에 이미
쓰이는 함수)를 그대로 재사용 — 새 스윙탐지 로직 발명 아님. 이 모듈은 순수 함수(main.py 미참조).
"""

from __future__ import annotations
import numpy as np

MAJOR_SWING_AMP_RATIO = 0.60  # main.py와 동일 SSOT 값(호출측이 다르게 넘기면 그 값 우선)


def _find_pivots(series_high, series_low, left=2, right=2):
    """main.py _find_pivots와 동일 로직(중복 재구현 아님 — 순수함수 모듈 분리 원칙상 복사,
    값/알고리즘 변경 없음)."""
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


def detect_ob_fvg_confluence_rolereversal(bars, confluence_area_min=0.50):
    """OB/FVG/Confluence Zone/Role Reversal 탐지. bars: [ts,o,h,l,c,v] 완성봉만(진행봉 제외).
    가장 최근 상방 돌파 1건만 탐지(State3는 '최근' 매물대 지지 여부만 필요 — 전체 이력 불필요).
    [타입 안전] 모든 반환값을 순수 float로 명시 변환(numpy 타입이 JSON 직렬화 시 깨지는 결함
    2026-09-04에 이미 한 차례 발견·수정 — 동일 결함 재발 방지 차원에서 여기서도 선제 적용)."""
    n = len(bars)
    if n < 20:
        return {"status": "insufficient_data"}
    highs = np.array([b[2] for b in bars])
    lows = np.array([b[3] for b in bars])
    closes = np.array([b[4] for b in bars])
    opens = np.array([b[1] for b in bars])

    highs_raw, lows_raw = _find_pivots(highs, lows, left=2, right=2)
    major_highs, major_lows = _filter_major_price_pivots(highs_raw, lows_raw, highs, lows)
    if not major_highs:
        return {"status": "insufficient_data"}

    breakout_idx, broken_level = None, None
    for i in range(len(closes) - 1, -1, -1):
        prior_major_highs = [h for h in major_highs if h < i]
        if not prior_major_highs:
            continue
        last_major_high_idx = prior_major_highs[-1]
        last_major_high_price = highs[last_major_high_idx]
        if closes[i] > last_major_high_price:
            breakout_idx = i
            broken_level = float(last_major_high_price)
            break
    if breakout_idx is None:
        return {"status": "no_breakout_found"}

    ob_idx = None
    for i in range(breakout_idx - 1, max(breakout_idx - 10, -1), -1):
        if closes[i] < opens[i]:
            ob_idx = i
            break
    ob_range = (float(lows[ob_idx]), float(highs[ob_idx])) if ob_idx is not None else None

    fvg_range = None
    if breakout_idx >= 2:
        b1_high = float(highs[breakout_idx - 2])
        b3_low = float(lows[breakout_idx])
        if b3_low > b1_high:
            fvg_range = (b1_high, b3_low)

    confluence_range = None
    if ob_range and fvg_range:
        ob_lo, ob_hi = ob_range
        fvg_lo, fvg_hi = fvg_range
        overlap_lo, overlap_hi = max(ob_lo, fvg_lo), min(ob_hi, fvg_hi)
        overlap = max(0.0, overlap_hi - overlap_lo)
        smaller_width = min(ob_hi - ob_lo, fvg_hi - fvg_lo)
        smaller_width = smaller_width if smaller_width > 0 else 1e-8
        if overlap / smaller_width >= confluence_area_min:
            confluence_range = (max(ob_lo, fvg_lo), min(ob_hi, fvg_hi))

    return {
        "status": "ok",
        "breakout_idx": int(breakout_idx),
        "role_reversal_level": broken_level,
        "ob_range": ob_range,
        "fvg_range": fvg_range,
        "confluence_range": confluence_range,
    }


def check_pullback_support_touch(today_low, today_open, today_close, structure, tolerance=0.015):
    """State3 옥석검증B: 오늘(조정봉) 저가가 OB/FVG/Confluence/RoleReversal 중 하나에
    ±tolerance(ROLE_REVERSAL_TOLERANCE=1.5%, 본체 SSOT 재사용) 이내 접촉 + 양봉(아래꼬리반발) 여부."""
    if structure.get("status") != "ok":
        return {"status": "insufficient_data"}

    def near(level_lo, level_hi, price, tol):
        band = (level_hi - level_lo) * tol if level_hi > level_lo else level_hi * tol
        return (level_lo - band) <= price <= (level_hi + band)

    touched = []
    rr = structure.get("role_reversal_level")
    if rr and rr > 0 and abs(today_low - rr) / rr <= tolerance:
        touched.append("Role Reversal")
    ob = structure.get("ob_range")
    if ob and near(ob[0], ob[1], today_low, tolerance):
        touched.append("OB")
    fvg = structure.get("fvg_range")
    if fvg and near(fvg[0], fvg[1], today_low, tolerance):
        touched.append("FVG")
    conf = structure.get("confluence_range")
    if conf and near(conf[0], conf[1], today_low, tolerance):
        touched.append("Confluence Zone")

    is_bounce = bool(today_close > today_open)
    return {
        "status": "ok",
        "touched_structures": touched,
        "has_touch": len(touched) > 0,
        "is_bounce": is_bounce,
        "state3_condition_b_met": bool(len(touched) > 0 and is_bounce),
    }
