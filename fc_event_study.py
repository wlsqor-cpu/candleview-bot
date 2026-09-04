"""
fc_event_study.py — FindCoin 사후사건연구(retrospective event study) 엔진

[설계 전환 배경 — 2026-09-04]
Phase A~C(실시간 관측 후 N일 대기해 사후채점)는 방향은 맞지만 검증까지 몇 주가 걸린다.
사용자 제안: "이미 시세분출이 나온 코인들의 과거 데이터를 모아 터지기 전 특징을 찾자" —
목적(baseline 대비 초과 여부 검증)은 동일하지만 이미 벌어진 과거 사건을 쓰므로 대기시간이
없다. Phase A의 fc_observation_tier1.py는 순수 함수(라이브 API 의존 없음, main.py도
import하지 않음)라 이 엔진에 코드 변경 없이 그대로 재사용된다 — 폐기가 아니라 다른
방향(사후관측 대신 사후사건연구)에서의 재사용.

[핵심 원칙 — 컷오프 하드코딩 금지 재적용]
"시세분출"의 정의(며칠 안에 몇 % 상승) 자체가 임의 결정이다. RTM·State1이 검증 없이
단일 임계값을 먼저 확정했다가 실패한 전례(2026-09-04 실측기각)를 반복하지 않기 위해,
이 엔진은 단일 정의를 쓰지 않고 여러 정의(forward_days × threshold_pct 조합)를 동시에
스윕해 결과가 정의 선택에 얼마나 민감한지 함께 보고한다 —
pre-breakout-signal-characteristics.md의 State1 실측(COMPRESSION_RANGE_MAX 8~20% 5개 값
스윕)과 동일한 방법론 재사용, 신규 검증관행 발명 아님.

[쿨다운 20일] backtest_framework.py의 기존 "신호 쿨다운 20봉"(pre-breakout 문서에 이미
기록됨)을 그대로 재사용 — 임의 신규값 아님.

[입력 데이터 형식]
historical_data: {symbol: daily_bars} 형태의 dict. daily_bars는 fc_observation_tier1과
동일한 ccxt OHLCV 형식([ts,o,h,l,c,v], 오름차순, 완성봉만 — 과거 데이터라 진행봉 구분 불필요
하지만 아래 주의사항 참조). 이 모듈은 historical_data를 어디서 어떻게 구했는지 모른다
(backtest_framework.py의 기존 캐시를 변환해 넣든, 별도로 fetch_ohlcv_range 방식으로
새로 모으든 무관 — 느슨한 결합).

[과거 재현(historical replay) 시 주의 — look-ahead 방지]
fc_observation_tier1의 함수들은 입력 배열의 "마지막 봉"을 항상 "오늘 진행봉(미종료)"으로
취급해 완성봉 계산에서 제외한다. 이 엔진이 과거 특정 날짜를 "관측 시점"으로 재현할 때도
(snapshot_at 함수) 그 규칙을 그대로 따른다 — 관측 시점 당일 봉은 미완성 취급되어 창 계산에서
빠진다. 이는 라이브 스캔과 동일한 조건을 재현하기 위한 의도적 설계이며 버그가 아니다.
동시에 관측 시점 "이후"의 봉은 배열 자체에서 물리적으로 잘려나가 있으므로(daily_bars[:idx+1]),
미래 정보가 지표 계산에 섞여들 수 없다 — 이 보장이 이 엔진 전체의 정합성 근거다.
"""

from __future__ import annotations
import random as _random

from fc_observation_tier1 import compute_tier1_observation


TIER1_INDICATOR_KEYS = [
    "nr_n", "range_pctile_self_history", "inside_bar_streak", "no_supply",
    "selling_exhaustion", "absorption", "relative_strength_btc",
    "universe_relative_return", "turnover_trend", "ascending_triangle",
    "spring_reclaim", "base_maturity", "days_since_low",
]


def find_pump_events(daily_bars, forward_days, threshold_pct, cooldown_days=20):
    """day i 기준, i일 종가 대비 이후 forward_days일 내 고가가 threshold_pct% 이상
    상승한 최초 지점을 이벤트로 판정한다. 쿨다운 적용 — 쿨다운 기간 내 재발생은 무시
    (같은 급등의 연장을 별개 이벤트로 중복 계산하지 않기 위함, backtest_framework와 동일 원칙)."""
    n = len(daily_bars)
    events = []
    last_event = -cooldown_days - 1
    for i in range(n - forward_days):
        base_close = daily_bars[i][4]
        if base_close <= 0:
            continue
        window = daily_bars[i + 1: i + 1 + forward_days]
        if not window:
            continue
        max_high = max(b[2] for b in window)
        pct_move = (max_high / base_close - 1.0) * 100.0
        if pct_move >= threshold_pct and (i - last_event) > cooldown_days:
            events.append(i)
            last_event = i
    return events


def sample_baseline_indices(n, exclude_indices, n_samples, min_index, seed=None):
    """비이벤트 대조군 인덱스 무작위 추출. min_index 이전(관측용 lookback 미확보)은 제외."""
    rng = _random.Random(seed)
    pool = [i for i in range(min_index, n) if i not in exclude_indices]
    if len(pool) <= n_samples:
        return pool
    return rng.sample(pool, n_samples)


def snapshot_at(daily_bars, as_of_index, tier1_lookback, btc_daily_bars=None,
                 universe_returns=None, item_return=None):
    """as_of_index까지의 배열만 잘라(daily_bars[:as_of_index+1]) 그 시점 기준 관측을
    재현한다 — 미래 데이터 유출(look-ahead) 방지가 이 함수의 핵심 책임."""
    sliced = daily_bars[: as_of_index + 1]
    return compute_tier1_observation(
        daily_bars=sliced,
        lookback=tier1_lookback,
        btc_daily_bars=btc_daily_bars,
        universe_returns=universe_returns,
        item_return_for_universe=item_return,
    )


def run_event_study(historical_data, forward_days, threshold_pct, pre_event_offset,
                     tier1_lookback, cooldown_days=20, n_baseline_per_symbol=10, seed=42):
    """단일 (forward_days, threshold_pct, pre_event_offset) 조합으로 전체 유니버스를
    스캔해 pre-event 그룹과 baseline 그룹의 Tier1 관측치를 모은다.
    pre_event_offset: 이벤트 발생일(i) 기준 며칠 '전' 시점을 관측 스냅샷으로 쓸지."""
    pre_event_obs, baseline_obs = [], []
    min_index_needed = tier1_lookback + 25  # range_pctile_self_history 등이 요구하는 최소여유

    for symbol, bars in historical_data.items():
        n = len(bars)
        if n < min_index_needed + forward_days:
            continue
        events = find_pump_events(bars, forward_days, threshold_pct, cooldown_days)

        for e in events:
            snap_idx = e - pre_event_offset
            if snap_idx < min_index_needed:
                continue
            obs = snapshot_at(bars, snap_idx, tier1_lookback)
            obs["_symbol"] = symbol
            obs["_event_day_index"] = e
            obs["_snapshot_day_index"] = snap_idx
            pre_event_obs.append(obs)

        exclude = set()
        for e in events:
            exclude.update(range(max(0, e - cooldown_days), min(n, e + forward_days + 1)))
        baseline_idx = sample_baseline_indices(n, exclude, n_baseline_per_symbol, min_index_needed, seed=seed)
        for bi in baseline_idx:
            obs = snapshot_at(bars, bi, tier1_lookback)
            obs["_symbol"] = symbol
            obs["_snapshot_day_index"] = bi
            baseline_obs.append(obs)

    return pre_event_obs, baseline_obs


def _extract_normalized(obs_list, indicator_key):
    vals = []
    for obs in obs_list:
        field = obs.get(indicator_key)
        if isinstance(field, dict) and field.get("status") == "ok" and field.get("normalized") is not None:
            vals.append(field["normalized"])
    return vals


def summarize_indicator_diff(pre_event_obs, baseline_obs, indicator_key):
    """backtest_framework의 'baseline 대비 pass_avg_return' 리포팅 방식을 재사용 — 여기서는
    '정규화 점수 평균'을 baseline vs pre-event로 비교한다. 표본 부족(10 미만)은 결측을
    유의미한 0차이로 오인하지 않도록 insufficient_sample로 명시한다."""
    pre_vals = _extract_normalized(pre_event_obs, indicator_key)
    base_vals = _extract_normalized(baseline_obs, indicator_key)
    if len(pre_vals) < 10 or len(base_vals) < 10:
        return {"status": "insufficient_sample", "pre_n": len(pre_vals), "base_n": len(base_vals)}
    pre_mean = sum(pre_vals) / len(pre_vals)
    base_mean = sum(base_vals) / len(base_vals)
    return {
        "status": "ok",
        "pre_n": len(pre_vals),
        "base_n": len(base_vals),
        "pre_event_mean_normalized": round(pre_mean, 4),
        "baseline_mean_normalized": round(base_mean, 4),
        "diff": round(pre_mean - base_mean, 4),
    }


def summarize_all_indicators(pre_event_obs, baseline_obs):
    return {k: summarize_indicator_diff(pre_event_obs, baseline_obs, k) for k in TIER1_INDICATOR_KEYS}


def run_sensitivity_sweep(historical_data, forward_days_list, threshold_pct_list, pre_event_offset,
                           tier1_lookback, cooldown_days=20, n_baseline_per_symbol=10, seed=42):
    """복수 이벤트정의 조합 스윕 — 단일 정의 하드코딩 금지 원칙 적용. 결과가 정의(조합)마다
    크게 달라지면 그 지표는 '우연히 한 정의에서만 유의미'했을 위험이 있다는 뜻이다."""
    sweep_results = []
    for fd in forward_days_list:
        for th in threshold_pct_list:
            pre_obs, base_obs = run_event_study(
                historical_data, fd, th, pre_event_offset, tier1_lookback,
                cooldown_days=cooldown_days, n_baseline_per_symbol=n_baseline_per_symbol, seed=seed,
            )
            summary = summarize_all_indicators(pre_obs, base_obs)
            sweep_results.append({
                "forward_days": fd, "threshold_pct": th,
                "n_events": len(pre_obs), "n_baseline": len(base_obs),
                "indicator_summary": summary,
            })
    return sweep_results
