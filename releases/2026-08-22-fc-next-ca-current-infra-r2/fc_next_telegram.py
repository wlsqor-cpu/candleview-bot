"""FC-Next nonblocking Telegram rendering and detail-launch boundary.

This module consumes only a deterministic ScanResult from fc_next_core.  It
never calls an LLM and never creates/reads CandleView PHASE sessions.  The
actual production callback handler may later use `parse_detail_callback` to
start its existing fresh run_phase1(exchange, symbol, default_tfs) flow.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Mapping

STATE_TITLES = {
    "S2": "완료 종가 기준 구조 돌파",
    "S3": "상위 추세 안의 완료 인트라데이 조정",
    "S1": "응축·상단 안정",
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
    """Validate only the safe inputs allowed to launch a fresh CandleView Phase 1."""
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
    features = candidate.get("features_completed") or {}
    state = candidate.get("primary_state")
    if state == "S2":
        body = features.get("body_ratio")
        volume = features.get("volume_ratio")
        if isinstance(body, (int, float)) and isinstance(volume, (int, float)):
            return f"완료 종가 구조 돌파 · 몸통비율 {body:.2f} · 거래량비율 {volume:.2f}"
        return "완료 종가 구조 돌파 관측"
    if state == "S3":
        pullback = features.get("pullback_range")
        if isinstance(pullback, (int, float)):
            return f"주·일봉 맥락 유지 · 완료 인트라데이 조정폭 {pullback:.2f}"
        return "주·일봉 맥락 안의 완료 인트라데이 조정 관측"
    if state == "S1":
        box = features.get("box_width")
        position = features.get("range_position")
        if isinstance(box, (int, float)) and isinstance(position, (int, float)):
            return f"완료봉 응축 · 박스폭 {box:.3f} · 범위 위치 {position:.2f}"
        return "완료봉 응축·상단 안정 관측"
    return "완료봉 데이터 관측"


def _candidate_blocks(scan_result: Mapping[str, Any], per_state_limit: int) -> tuple[list[str], list[list[dict[str, str]]]]:
    candidates = list(scan_result.get("display_candidates") or [])
    exchange_id = str((scan_result.get("snapshot") or {}).get("exchange_id", ""))
    lines: list[str] = []
    keyboard: list[list[dict[str, str]]] = []
    for state in STATE_ORDER:
        state_candidates = [candidate for candidate in candidates if candidate.get("primary_state") == state]
        if not state_candidates:
            continue
        lines.append(f"[{state} — {STATE_TITLES[state]}]")
        for candidate in state_candidates[:per_state_limit]:
            symbol = str(candidate.get("symbol", ""))
            rank = candidate.get("rank_within_state")
            coverage = candidate.get("coverage_status", "limited")
            lines.append(f"• {symbol} · {state}-{rank} · {_candidate_reason(candidate)} · 데이터 {coverage}")
            payload = make_detail_callback(exchange_id, symbol)
            if payload is not None:
                keyboard.append([{
                    "text": f"📊 {state}-{rank} {symbol.split('/')[0]} 상세 차트 분석",
                    "callback_data": payload,
                }])
    return lines, keyboard


def render_scan_result(scan_result: Mapping[str, Any], per_state_limit: int = 3) -> dict[str, Any]:
    """Create a complete publishable nonblocking FindCoin result.

    The return shape is intentionally Telegram-transport-neutral:
      {"text": str, "reply_markup": {"inline_keyboard": [...] } | None}
    A future integration can pass it into the existing send_telegram_message.
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
    lines = [
        "<b>CandleView — FindCoin 관측 스캔</b>",
        f"{exchange} · {quote} · {finished_at}",
        f"feature contract: {feature_version}",
        "",
        "<b>1️⃣ 수집 범위와 데이터 상태</b>",
        f"• FC-0 유니버스: {coverage.get('universe_total', 0)}개",
        f"• 기본 완료봉 수집: {coverage.get('basic_ohlcv', 0)}/{coverage.get('universe_total', 0)}개",
        f"• 데이터 제한: {coverage.get('data_limited', 0)}개 · State 보류: {coverage.get('state_pending', 0)}개",
    ]
    snapshot_errors = list(snapshot.get("error_codes") or [])
    collection_manifest = dict(scan_result.get("collection_manifest") or {})
    if collection_manifest.get("collection_mode") == "limited_probe":
        lines.append("• 현재 결과는 제한 표본 probe입니다. 전체 FC-0 유니버스 결과·성과 비교로 사용하지 않습니다.")
    if snapshot_errors:
        lines.append(f"• 수집 제한 코드: {', '.join(snapshot_errors)}")
    candidate_lines, keyboard = _candidate_blocks(scan_result, per_state_limit)
    lines.extend(["", "<b>2️⃣ 완료봉 관측 후보</b>"])
    if candidate_lines:
        lines.extend(candidate_lines)
    elif status == "data_limited":
        lines.append("기본 시장 데이터는 일부 수집됐지만, 이번 시점에서 비교 가능한 완료봉 후보가 없습니다.")
    else:
        lines.append("이번 스캔에서 완료봉 State 조건을 충족한 관측 후보가 없습니다.")
    lines.extend([
        "",
        "<b>3️⃣ 고확신 조건 상태</b>",
        "• 고확신 라벨은 성과 cohort 검증 전까지 비활성입니다.",
        "• 위 목록은 완료봉 특징을 기준으로 한 관측 순서이며, 성과 우위를 의미하지 않습니다.",
        "",
        "<b>4️⃣ 데이터 제한과 상세 분석</b>",
        "• 표시된 관측 후보는 최신 CandleView 상세 차트 분석을 별도로 시작할 수 있습니다.",
    ])
    if scan_result.get("ledger_status") == "ledger_not_saved":
        lines.append("• 이번 관측 결과는 발행됐으며, 성과 원장 기록은 보류되었습니다.")
    elif scan_result.get("ledger_status") == "not_attempted":
        lines.append("• 성과 원장 기록은 아직 시도하지 않았습니다.")
    return {
        "text": "\n".join(lines),
        "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        "status": status,
    }
