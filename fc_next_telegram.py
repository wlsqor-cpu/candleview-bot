"""Deterministic user renderer and detail callback boundary for FC-Next."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping


# These labels explain a completed-candle observation.  They do not change a
# State condition, priority, rank, or a detail callback.
STATE_TITLES = {
    "S2": "전날 마감가격이 최근 가격대를 넘긴 모습",
    "S3": "상위 흐름 안에서 단기 조정 뒤 반응을 확인한 모습",
    "S1": "가격 범위가 좁아지며 움직임을 준비하는 모습",
}
STATE_ORDER = ("S2", "S3", "S1")


def make_detail_callback(exchange_id: str, symbol: str) -> str | None:
    """Return the legacy-compatible callback without snapshot/rank/price data."""
    base_symbol = str(symbol).split("/")[0].strip().upper()
    payload = f"fc_detail|{exchange_id}|{base_symbol}"
    if not exchange_id or not base_symbol or len(payload.encode("utf-8")) > 64:
        return None
    return payload


def parse_detail_callback(payload: str, supported_exchanges: set[str]) -> dict[str, str] | None:
    """Validate only safe inputs allowed to launch a fresh CandleView Phase 1."""
    parts = str(payload).split("|")
    if len(parts) != 3 or parts[0] != "fc_detail":
        return None
    exchange_id, symbol = parts[1], parts[2]
    if exchange_id not in supported_exchanges or not symbol:
        return None
    # No FindCoin snapshot ID, P0, State, rank or outcome may cross this boundary.
    return {"exchange_id": exchange_id, "symbol": symbol, "mode": "fresh_phase1_default_tfs"}


def attach_ledger_status_nonblocking(
    scan_result: Mapping[str, Any], ledger_writer: Callable[[Mapping[str, Any]], Any] | None
) -> dict[str, Any]:
    """Attempt record persistence without making result publication depend on it."""
    result = deepcopy(dict(scan_result))
    if ledger_writer is None:
        result["ledger_status"] = "not_attempted"
        return result
    try:
        ledger_writer(result)
        result["ledger_status"] = "saved"
    except Exception as exc:  # Intentionally isolated from scanner/render flow.
        result["ledger_status"] = "ledger_not_saved"
        result["ledger_error_code"] = type(exc).__name__
    return result


def _candidate_reason(candidate: Mapping[str, Any]) -> str:
    """Explain existing completed features without adding a price forecast."""
    features = candidate.get("features_completed") or {}
    state = candidate.get("primary_state")
    if state == "S2":
        body = features.get("body_ratio")
        volume = features.get("volume_ratio")
        if isinstance(body, (int, float)) and isinstance(volume, (int, float)):
            return f"전날 마감 봉의 몸통 비중 {body:.2f}, 거래량은 최근 평균의 {volume:.2f}배"
        return "전날 마감가격이 최근 가격대 상단을 넘긴 모습"
    if state == "S3":
        pullback = features.get("pullback_range")
        if isinstance(pullback, (int, float)):
            return f"상위 흐름을 유지한 채 단기 조정 폭 {pullback:.2f}를 확인"
        return "상위 흐름 안에서 단기 조정 뒤 반응을 확인한 모습"
    if state == "S1":
        box = features.get("box_width")
        position = features.get("range_position")
        if isinstance(box, (int, float)) and isinstance(position, (int, float)):
            return f"최근 가격 범위 폭 {box:.3f}, 마감은 범위 안의 {position:.2f} 지점"
        return "가격 범위가 좁아지며 움직임을 준비하는 모습"
    return "완료된 일봉 데이터에서 특징을 확인"


def _candidate_blocks(scan_result: Mapping[str, Any], per_state_limit: int) -> tuple[list[str], list[list[dict[str, str]]]]:
    candidates = sorted(
        list(scan_result.get("display_candidates") or []),
        key=lambda candidate: (
            {"S2": 0, "S3": 1, "S1": 2}.get(candidate.get("primary_state"), 99),
            candidate.get("rank_within_state") if isinstance(candidate.get("rank_within_state"), int) else 10**9,
            str(candidate.get("symbol", "")),
        ),
    )
    exchange_id = str((scan_result.get("snapshot") or {}).get("exchange_id", ""))
    lines: list[str] = []
    keyboard: list[list[dict[str, str]]] = []
    for state in STATE_ORDER:
        state_candidates = [candidate for candidate in candidates if candidate.get("primary_state") == state]
        if not state_candidates:
            continue
        lines.append(f"<b>{STATE_TITLES[state]}</b>")
        for candidate in state_candidates[:per_state_limit]:
            symbol = str(candidate.get("symbol", ""))
            rank = candidate.get("rank_within_state")
            lines.append(f"• {symbol} · 이 유형 안에서 {rank}번째 · {_candidate_reason(candidate)}")
            payload = make_detail_callback(exchange_id, symbol)
            if payload is not None:
                keyboard.append([{
                    "text": f"📊 {symbol.split('/')[0]} 상세 차트 보기",
                    "callback_data": payload,
                }])
    return lines, keyboard


def _collection_limit_lines(
    coverage: Mapping[str, Any], collection_manifest: Mapping[str, Any]
) -> list[str]:
    """Translate exact manifest facts; never infer a missing-data reason."""
    lines: list[str] = []
    daily_deferred = len(collection_manifest.get("time_budget_daily_deferred_symbols") or [])
    intraday_requested = len(collection_manifest.get("state3_intraday_requested_symbols") or [])
    intraday_deferred = len(collection_manifest.get("state3_intraday_deferred_symbols") or [])
    p0_not_attempted = int(coverage.get("p0_not_attempted", 0) or 0)
    p0_unavailable = int(coverage.get("p0_unavailable", 0) or 0)
    p0_valid = int(coverage.get("p0_valid", 0) or 0)

    if daily_deferred:
        lines.append(f"• {daily_deferred}개는 일봉 수집 시간이 부족해 이번 계산에 반영하지 못했습니다.")
    if intraday_deferred:
        lines.append(
            f"• 상위 흐름 속 단기 조정 확인에 필요한 4시간/6시간 데이터는 "
            f"요청 대상 {intraday_requested}개 중 {intraday_deferred}개에서 이번 실행 시간 안에 확보하지 못했습니다."
        )
    if p0_not_attempted:
        lines.append(f"• 이후 성과 기록의 시작가격은 {p0_not_attempted}개에서 이번 실행 시간 안에 조회하지 못했습니다.")
    if p0_unavailable:
        lines.append(f"• 시작가격을 조회했지만 유효한 값이 없었던 코인은 {p0_unavailable}개입니다.")
    if p0_valid == 0:
        lines.append("• 이번 결과는 유효한 시작가격이 없어 이후 성과를 비교하는 표본으로 사용하지 않습니다.")
    return lines


def render_scan_result(scan_result: Mapping[str, Any], per_state_limit: int = 3) -> dict[str, Any]:
    """Create a complete, nonblocking, plain-language FindCoin result.

    This function consumes a prebuilt ScanResult only.  It never decides a State,
    changes a rank, or recalculates coverage.
    """
    if per_state_limit < 1:
        raise ValueError("per_state_limit must be at least 1")
    snapshot = dict(scan_result.get("snapshot") or {})
    coverage = dict(scan_result.get("coverage") or {})
    status = scan_result.get("status", "data_limited")
    exchange = str(snapshot.get("exchange_id", "")).upper()
    quote = snapshot.get("quote", "")
    finished_at = snapshot.get("scan_finished_at_utc") or "미기록"
    feature_version = scan_result.get("feature_contract_version", "미기록")
    collection_manifest = dict(scan_result.get("collection_manifest") or {})
    observations = list(scan_result.get("observations") or [])
    total = int(coverage.get("universe_total", len(observations)) or 0)
    daily_available = int(coverage.get("basic_ohlcv", 0) or 0)
    data_limited = int(coverage.get("data_limited", 0) or 0)
    observed = int(coverage.get("observation_candidates", 0) or 0)

    def pending_count(state: str) -> int:
        key = f"{state.lower()}_pending"
        if key in coverage:
            return int(coverage.get(key, 0) or 0)
        return sum((row.get("state_status") or {}).get(state) == "pending_data" for row in observations)

    if "all_states_pending" in coverage:
        all_pending = int(coverage.get("all_states_pending", 0) or 0)
    else:
        all_pending = sum(
            bool(row.get("state_status"))
            and all(value == "pending_data" for value in (row.get("state_status") or {}).values())
            for row in observations
        )
    s1_pending, s2_pending, s3_pending = pending_count("S1"), pending_count("S2"), pending_count("S3")

    lines = [
        "<b>CandleView — FindCoin 완료봉 관측</b>",
        f"{exchange} · {quote} · 수집 완료 {finished_at}",
        f"수집 기준 버전: {feature_version}",
        "",
        "<b>1️⃣ 이번에 어디까지 확인했는지</b>",
        f"• 이번 기준 목록: {total}개",
        f"• 일봉 마감 데이터 확보: {daily_available}/{total}개",
        f"• 이번 계산에 반영하지 못한 코인: {data_limited}개",
        (
            "• 조건별 데이터 미확인: "
            f"마감 돌파 {s2_pending}개 · "
            f"범위 응축 {s1_pending}개 · "
            f"상위 흐름 속 단기 조정 {s3_pending}개"
        ),
        f"• 세 조건을 모두 판단하지 못한 코인: {all_pending}개",
    ]
    if collection_manifest.get("collection_mode") == "limited_probe":
        lines.append("• 이 결과는 일부만 확인한 시험 결과입니다. 전체 목록 결과나 성과 비교에 사용하지 않습니다.")

    candidate_lines, keyboard = _candidate_blocks(scan_result, per_state_limit)
    lines.extend(["", "<b>2️⃣ 이번에 관측된 모습</b>"])
    if candidate_lines:
        lines.append(f"• 조건을 확인한 코인은 {observed}개이며, 화면에는 각 유형의 앞 {per_state_limit}개만 표시합니다.")
        lines.extend(candidate_lines)
    elif status == "data_limited":
        lines.append("일봉 데이터는 일부 확보했지만, 이번 시점에는 비교할 수 있는 완료봉 관측 결과가 없습니다.")
    else:
        lines.append("이번 스캔에서는 완료된 일봉 기준으로 조건을 확인한 코인이 없습니다.")

    lines.extend([
        "",
        "<b>3️⃣ 이 목록을 해석하는 방법</b>",
        "• 위 순서는 완료된 캔들에서 확인한 특징의 순서일 뿐, 이후 성과가 더 좋다는 뜻은 아닙니다.",
        "• 아직 장기간 성과 기록이 쌓이지 않아 ‘고확신’이나 성과 우위 표시는 사용하지 않습니다.",
        "",
        "<b>4️⃣ 이번 결과의 제한과 상세 차트</b>",
    ])
    limit_lines = _collection_limit_lines(coverage, collection_manifest)
    if limit_lines:
        lines.extend(limit_lines)
    else:
        lines.append("• 이번 수집 범위 안에서는 추가 제한 사유가 기록되지 않았습니다.")
    lines.append("• 각 버튼을 누르면 해당 코인의 최신 CandleView 차트 분석을 새로 시작합니다.")
    if scan_result.get("ledger_status") == "ledger_not_saved":
        lines.append("• 관측 결과는 표시됐지만, 이번 기록 파일 저장은 완료하지 못했습니다.")
    elif scan_result.get("ledger_status") == "not_attempted":
        lines.append("• 이번 관측 결과의 기록 파일은 아직 저장을 시도하지 않았습니다.")
    text = "\n".join(lines)
    if len(text) > 4096:
        raise ValueError("FindCoin renderer exceeds Telegram text budget")
    return {
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        "status": status,
    }
