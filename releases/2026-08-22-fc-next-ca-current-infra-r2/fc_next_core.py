"""FC-Next C-stage-1 deterministic completed-candle scanner.

This module is deliberately independent from CandleView's production main.py.
It accepts pre-collected market data, never calls an exchange/LLM/Telegram API,
and never mutates CandleView PHASE sessions.  It implements the feature, state,
missingness, ranking and immutable ScanResult boundaries documented in the
FC-Next design contracts.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from math import isfinite, log
from statistics import median
from typing import Any, Iterable, Mapping
import json

FEATURE_CONTRACT_VERSION = "fcnext-feature-v2-draft"
SCHEMA_VERSION = "fcnext-scan-v1"


@dataclass(frozen=True)
class ScanConfig:
    """Versioned draft constants, never evidence of predictive superiority.

    `state1_boxwidth_quantile_max` and `minimum_peer_count` are explicit
    shadow-stage configuration values.  They must be stored in the output and
    may only be changed through a new feature-contract version.
    """

    daily_window: int = 40
    intraday_window: int = 12
    state1_boxwidth_quantile_max: float = 0.35
    state1_range_position_min: float = 0.50
    minimum_peer_count: int = 3
    support_confirmation_bars: int = 2


def _finite_number(value: Any, *, positive: bool = False) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(result) or (positive and result <= 0):
        return None
    return result


def _completed(rows: Any) -> list[dict[str, Any]]:
    """Keep only explicitly complete candles in chronological source order."""
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping) and row.get("complete") is True]


def _valid_ohlcv(rows: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    valid: list[dict[str, Any]] = []
    for row in rows:
        timestamp = row.get("timestamp")
        o = _finite_number(row.get("open"), positive=True)
        h = _finite_number(row.get("high"), positive=True)
        l = _finite_number(row.get("low"), positive=True)
        c = _finite_number(row.get("close"), positive=True)
        v = _finite_number(row.get("volume"))
        if timestamp is None:
            return [], "MISSING_TIMESTAMP"
        if None in (o, h, l, c, v):
            return [], "INVALID_OHLCV"
        if h < l or h < max(o, c) or l > min(o, c) or v < 0:
            return [], "INVALID_OHLCV"
        valid.append({"timestamp": str(timestamp), "open": o, "high": h, "low": l, "close": c, "volume": v})
    return valid, None


def _completed_valid(rows: Any) -> tuple[list[dict[str, Any]], str | None]:
    completed = _completed(rows)
    valid, error = _valid_ohlcv(completed)
    if error:
        return [], error
    return valid, None


def _safe_log_return(start: float | None, end: float | None) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return log(end / start)


def _benchmark_return_at_matching_timestamps(
    candidate_rows: list[dict[str, Any]], benchmark_rows: list[dict[str, Any]], lookback: int
) -> float | None:
    if len(candidate_rows) < lookback + 1:
        return None
    benchmark_by_timestamp = {row["timestamp"]: row["close"] for row in benchmark_rows}
    start_ts = candidate_rows[-1 - lookback]["timestamp"]
    end_ts = candidate_rows[-1]["timestamp"]
    return _safe_log_return(benchmark_by_timestamp.get(start_ts), benchmark_by_timestamp.get(end_ts))


def _relative_strength(
    candidate_rows: list[dict[str, Any]], benchmark_rows: list[dict[str, Any]], lookback: int
) -> float | None:
    candidate_return = _safe_log_return(
        candidate_rows[-1 - lookback]["close"] if len(candidate_rows) >= lookback + 1 else None,
        candidate_rows[-1]["close"] if candidate_rows else None,
    )
    benchmark_return = _benchmark_return_at_matching_timestamps(candidate_rows, benchmark_rows, lookback)
    if candidate_return is None or benchmark_return is None:
        return None
    return candidate_return - benchmark_return


def _percentile(value: float, values: Iterable[float]) -> float:
    """Tie-stable empirical percentile in [0, 1]."""
    clean = [float(item) for item in values if _finite_number(item) is not None]
    if not clean:
        raise ValueError("cannot calculate percentile without values")
    less = sum(item < value for item in clean)
    equal = sum(item == value for item in clean)
    return (less + 0.5 * equal) / len(clean)


def _median_or_none(values: Iterable[float]) -> float | None:
    clean = [float(item) for item in values if _finite_number(item) is not None]
    return float(median(clean)) if clean else None


def compute_confirmed_daily_support(
    daily_rows: list[dict[str, Any]], *, window: int, confirmation_bars: int
) -> tuple[float | None, str | None]:
    """Return the nearest completed, confirmed daily pivot-low support.

    A pivot at index j requires `confirmation_bars` complete candles on both
    sides.  The asymmetric < / <= tie rule keeps consecutive equal lows from
    becoming multiple supports while selecting the earliest equal low.  No fallback minimum is fabricated when the
    current completed structure contains no confirmed pivot below close.
    """
    if confirmation_bars < 1 or window < (2 * confirmation_bars + 1):
        return None, "SUPPORT_CONFIG_INVALID"
    if len(daily_rows) < window:
        return None, "DAILY_SUPPORT_WINDOW_INSUFFICIENT"
    rows = daily_rows[-window:]
    current_close = rows[-1]["close"]
    supports: list[float] = []
    k = confirmation_bars
    for index in range(k, len(rows) - k):
        low = rows[index]["low"]
        left_lows = [rows[pos]["low"] for pos in range(index - k, index)]
        right_lows = [rows[pos]["low"] for pos in range(index + 1, index + k + 1)]
        if low < min(left_lows) and low <= min(right_lows) and low <= current_close:
            supports.append(low)
    if not supports:
        return None, "CONFIRMED_DAILY_SUPPORT_UNAVAILABLE"
    return max(supports), None


def state3_intraday_data_required(
    daily_rows: Any, weekly_rows: Any, config: ScanConfig | None = None
) -> tuple[bool, list[str]]:
    """Decide whether intraday data can still affect State3.

    This is only a completed daily/weekly *data-availability* precondition.  It
    never declares State3 confirmed or not-matched; daily/weekly trend values
    remain evaluated by the full core after intraday collection.  Therefore it
    is safe to skip an intraday API call only when State3 is already pending
    from missing completed structural inputs.
    """
    config = config or ScanConfig()
    daily, daily_error = _completed_valid(daily_rows)
    weekly, weekly_error = _completed_valid(weekly_rows)
    reasons: list[str] = []
    if daily_error:
        reasons.append(daily_error)
    if weekly_error:
        reasons.append(weekly_error)
    if len(daily) < max(config.daily_window, 15):
        reasons.append("DAILY_CONTEXT_INSUFFICIENT")
    if len(weekly) < 6:
        reasons.append("WEEKLY_CONTEXT_INSUFFICIENT")
    support, support_status = compute_confirmed_daily_support(
        daily, window=config.daily_window, confirmation_bars=config.support_confirmation_bars
    )
    if support is None:
        reasons.append(support_status or "CONFIRMED_DAILY_SUPPORT_UNAVAILABLE")
    return not reasons, sorted(set(reasons))


def _p0_from_live(live: Mapping[str, Any] | None) -> tuple[float | None, str | None, list[str]]:
    live = dict(live or {})
    if live.get("p0_status") == "not_attempted":
        # The collector intentionally skipped P0 because no completed feature
        # record exists.  Preserve P0_NOT_ATTEMPTED provenance without calling it
        # an API/value failure.
        return None, None, []
    if live.get("p0_status") == "unavailable":
        # The collector already recorded P0_UNAVAILABLE with its source details.
        # Do not duplicate the same provenance code in the core layer.
        return None, None, []
    bid = _finite_number(live.get("bid"), positive=True)
    ask = _finite_number(live.get("ask"), positive=True)
    last = _finite_number(live.get("last"), positive=True)
    errors: list[str] = []
    if bid is not None and ask is not None and bid <= ask:
        return (bid + ask) / 2.0, "mid", errors
    if bid is not None and ask is not None and bid > ask:
        errors.append("P0_CROSSED_BOOK")
    if last is not None:
        errors.append("P0_LAST_FALLBACK")
        return last, "last", errors
    return None, None, ["P0_UNAVAILABLE"]


def _base_feature_record(
    symbol_input: Mapping[str, Any], benchmark_daily: list[dict[str, Any]], config: ScanConfig
) -> dict[str, Any]:
    symbol = str(symbol_input.get("symbol", "")).strip().upper()
    daily, daily_error = _completed_valid(symbol_input.get("daily"))
    weekly, weekly_error = _completed_valid(symbol_input.get("weekly"))
    intraday, intraday_error = _completed_valid(symbol_input.get("intraday"))
    p0, price_source, p0_errors = _p0_from_live(symbol_input.get("live_observation"))
    record: dict[str, Any] = {
        "symbol": symbol,
        "market": symbol_input.get("market") or symbol,
        "p0": p0,
        "price_source": price_source,
        "price_timestamp_utc": (symbol_input.get("live_observation") or {}).get("observed_at_utc"),
        "bid": _finite_number((symbol_input.get("live_observation") or {}).get("bid"), positive=True),
        "ask": _finite_number((symbol_input.get("live_observation") or {}).get("ask"), positive=True),
        "last": _finite_number((symbol_input.get("live_observation") or {}).get("last"), positive=True),
        "daily_cutoff_utc": daily[-1]["timestamp"] if daily else None,
        "weekly_cutoff_utc": weekly[-1]["timestamp"] if weekly else None,
        "intraday_cutoff_utc": intraday[-1]["timestamp"] if intraday else None,
        "features_completed": {},
        "live_observation": dict(symbol_input.get("live_observation") or {}),
        "module_status": {
            "daily": "available" if daily_error is None and daily else "unavailable",
            "weekly": "available" if weekly_error is None and weekly else "unavailable",
            "intraday": "available" if intraday_error is None and intraday else "unavailable",
            "benchmark_daily": "available" if benchmark_daily else "unavailable",
            "orderbook": "available" if _finite_number((symbol_input.get("live_observation") or {}).get("bid"), positive=True) is not None and _finite_number((symbol_input.get("live_observation") or {}).get("ask"), positive=True) is not None else "limited",
            "trade_side": "available" if (symbol_input.get("live_observation") or {}).get("trade_side_raw") is not None else "limited",
        },
        "state_status": {"S1": "pending_data", "S2": "pending_data", "S3": "pending_data"},
        "state_reason_codes": {"S1": [], "S2": [], "S3": []},
        "state_flags": [],
        "primary_state": None,
        "rank_within_state": None,
        "display_tier": "data_limited",
        "coverage_status": "limited",
        "collection_status": dict(symbol_input.get("collection_status") or {}),
        "error_codes": list(symbol_input.get("collection_error_codes") or []) + [code for code in (daily_error, weekly_error, intraday_error) if code] + p0_errors,
        "_daily": daily,
        "_weekly": weekly,
        "_intraday": intraday,
        "_confirmed_daily_support": None,
        "_confirmed_daily_support_status": None,
    }
    support, support_status = compute_confirmed_daily_support(
        daily, window=config.daily_window, confirmation_bars=config.support_confirmation_bars
    )
    record["_confirmed_daily_support"] = support
    record["_confirmed_daily_support_status"] = support_status
    if not symbol:
        record["error_codes"].append("MISSING_SYMBOL")
    _compute_daily_features(record, benchmark_daily, config)
    _compute_state3_features(record, benchmark_daily, config)
    return record


def _compute_daily_features(record: dict[str, Any], benchmark_daily: list[dict[str, Any]], config: ScanConfig) -> None:
    daily = record["_daily"]
    window = config.daily_window
    if len(daily) < window:
        record["state_reason_codes"]["S1"].append("DAILY_WINDOW_INSUFFICIENT")
        record["state_reason_codes"]["S2"].append("DAILY_WINDOW_INSUFFICIENT")
        return
    state1_rows = daily[-window:]
    highs = [row["high"] for row in state1_rows]
    lows = [row["low"] for row in state1_rows]
    closes = [row["close"] for row in state1_rows]
    close_median = _median_or_none(closes)
    box_range = max(highs) - min(lows)
    if close_median is None or close_median <= 0 or box_range <= 0:
        record["state_reason_codes"]["S1"].append("BOX_WIDTH_UNAVAILABLE")
    else:
        range_pos = (closes[-1] - min(lows)) / box_range
        rs7 = _relative_strength(daily, benchmark_daily, 7)
        liquidity = _median_or_none([row["close"] * row["volume"] for row in state1_rows])
        record["features_completed"].update({
            "box_width": box_range / close_median,
            "range_position": range_pos,
            "relative_strength_7": rs7,
            "liquidity": liquidity,
        })
        if rs7 is None:
            record["state_reason_codes"]["S1"].append("BTC_ALIGNMENT_UNAVAILABLE")
        if liquidity is None or liquidity <= 0:
            record["state_reason_codes"]["S1"].append("LIQUIDITY_UNAVAILABLE")
    if len(daily) < window + 1:
        record["state_reason_codes"]["S2"].append("PRIOR_HIGH_WINDOW_INSUFFICIENT")
        return
    prior_rows = daily[-window - 1:-1]
    current = daily[-1]
    prior_high = max(row["high"] for row in prior_rows)
    current_range = current["high"] - current["low"]
    prior_trs: list[float] = []
    prior_start_index = len(daily) - window - 1
    for index, row in enumerate(prior_rows):
        row_index = prior_start_index + index
        if row_index <= 0:
            # The earliest available prior candle has no earlier completed close.
            # Omit only that TR observation rather than reading outside the series.
            continue
        previous_close = daily[row_index - 1]["close"]
        prior_trs.append(max(row["high"] - row["low"], abs(row["high"] - previous_close), abs(row["low"] - previous_close)))
    median_tr = _median_or_none(prior_trs)
    median_volume = _median_or_none([row["volume"] for row in prior_rows])
    rs7 = _relative_strength(daily, benchmark_daily, 7)
    if current_range <= 0:
        record["state_reason_codes"]["S2"].append("BODY_RATIO_UNAVAILABLE")
    elif median_volume is None or median_volume <= 0:
        record["state_reason_codes"]["S2"].append("VOLUME_RATIO_UNAVAILABLE")
    elif rs7 is None:
        record["state_reason_codes"]["S2"].append("BTC_ALIGNMENT_UNAVAILABLE")
    elif median_tr is None or median_tr <= 0:
        record["state_reason_codes"]["S2"].append("TRUE_RANGE_UNAVAILABLE")
    else:
        record["features_completed"].update({
            "prior_high": prior_high,
            "breakout_confirmed": current["close"] > prior_high,
            "body_ratio": abs(current["close"] - current["open"]) / current_range,
            "volume_ratio": current["volume"] / median_volume,
            "extension": (current["close"] - prior_high) / median_tr,
            "relative_strength_7": rs7,
        })


def _compute_state3_features(record: dict[str, Any], benchmark_daily: list[dict[str, Any]], config: ScanConfig) -> None:
    daily = record["_daily"]
    weekly = record["_weekly"]
    intraday = record["_intraday"]
    reasons = record["state_reason_codes"]["S3"]
    if len(daily) < max(config.daily_window, 15):
        reasons.append("DAILY_CONTEXT_INSUFFICIENT")
    if len(weekly) < 6:
        reasons.append("WEEKLY_CONTEXT_INSUFFICIENT")
    if len(intraday) < config.intraday_window:
        reasons.append("INTRADAY_WINDOW_INSUFFICIENT")
    support = record["_confirmed_daily_support"]
    if support is None:
        reasons.append(record["_confirmed_daily_support_status"] or "CONFIRMED_DAILY_SUPPORT_UNAVAILABLE")
    if reasons:
        return
    weekly_trend = weekly[-1]["close"] > weekly[-5]["close"] and weekly[-2]["close"] > weekly[-6]["close"]
    daily_context = daily[-1]["close"] > float(median([row["close"] for row in daily[-config.daily_window:]]))
    window_rows = intraday[-config.intraday_window:]
    high_max = max(row["high"] for row in window_rows)
    low_min = min(row["low"] for row in window_rows)
    spread = high_max - low_min
    if spread <= 0:
        reasons.append("PULLBACK_RANGE_UNAVAILABLE")
        return
    pullback_range = (high_max - intraday[-1]["close"]) / spread
    pullback_hold = intraday[-1]["close"] >= support
    rs14 = _relative_strength(daily, benchmark_daily, 14)
    liquidity = record["features_completed"].get("liquidity")
    if rs14 is None:
        reasons.append("BTC_ALIGNMENT_UNAVAILABLE")
        return
    if liquidity is None or liquidity <= 0:
        reasons.append("LIQUIDITY_UNAVAILABLE")
        return
    record["features_completed"].update({
        "weekly_trend": weekly_trend,
        "daily_context": daily_context,
        "pullback_range": pullback_range,
        "pullback_hold": pullback_hold,
        "relative_strength_14": rs14,
    })


def _confirm_states(records: list[dict[str, Any]], config: ScanConfig) -> None:
    # State1 requires a valid peer comparison group; ranks never compare across exchanges/snapshots.
    s1_eligible = [
        record for record in records
        if all(record["features_completed"].get(key) is not None for key in ("box_width", "range_position", "relative_strength_7", "liquidity"))
        and not record["state_reason_codes"]["S1"]
    ]
    if len(s1_eligible) < config.minimum_peer_count:
        for record in s1_eligible:
            record["state_reason_codes"]["S1"].append("PEER_GROUP_INSUFFICIENT")
    else:
        box_widths = [record["features_completed"]["box_width"] for record in s1_eligible]
        for record in s1_eligible:
            features = record["features_completed"]
            compression_percentile = _percentile(-features["box_width"], [-value for value in box_widths])
            features["box_compression_percentile"] = compression_percentile
            if (
                compression_percentile >= 1.0 - config.state1_boxwidth_quantile_max
                and features["range_position"] >= config.state1_range_position_min
                and features["liquidity"] > 0
            ):
                record["state_status"]["S1"] = "confirmed"
                record["state_flags"].append("S1")
            else:
                record["state_status"]["S1"] = "not_matched"
    for record in records:
        features = record["features_completed"]
        s2_reasons = record["state_reason_codes"]["S2"]
        if not s2_reasons and all(features.get(key) is not None for key in ("breakout_confirmed", "body_ratio", "volume_ratio", "relative_strength_7", "extension", "liquidity")):
            if features["breakout_confirmed"]:
                record["state_status"]["S2"] = "confirmed"
                record["state_flags"].append("S2")
            else:
                record["state_status"]["S2"] = "not_matched"
        s3_reasons = record["state_reason_codes"]["S3"]
        if not s3_reasons and all(features.get(key) is not None for key in ("weekly_trend", "daily_context", "pullback_range", "pullback_hold", "relative_strength_14", "liquidity")):
            if features["weekly_trend"] and features["daily_context"] and features["pullback_hold"]:
                record["state_status"]["S3"] = "confirmed"
                record["state_flags"].append("S3")
            else:
                record["state_status"]["S3"] = "not_matched"
        record["state_flags"] = sorted(set(record["state_flags"]))
        record["primary_state"] = next((state for state in ("S2", "S3", "S1") if state in record["state_flags"]), None)
        if record["primary_state"] is not None:
            record["display_tier"] = "observed"  # qualified remains disabled during C-stage-1.
            record["coverage_status"] = "available"
        elif record["module_status"]["daily"] == "available":
            record["coverage_status"] = "limited"


def _rank_state(records: list[dict[str, Any]], state: str) -> None:
    confirmed = [record for record in records if state in record["state_flags"]]
    if not confirmed:
        return
    features = [record["features_completed"] for record in confirmed]
    liquidity_values = [feature["liquidity"] for feature in features]
    if state == "S1":
        rs_values = [feature["relative_strength_7"] for feature in features]
        def key(record: dict[str, Any]) -> tuple[Any, ...]:
            f = record["features_completed"]
            return (-f["box_compression_percentile"], -f["range_position"], -_percentile(f["relative_strength_7"], rs_values), -_percentile(f["liquidity"], liquidity_values), record["symbol"])
    elif state == "S2":
        vol_values = [feature["volume_ratio"] for feature in features]
        rs_values = [feature["relative_strength_7"] for feature in features]
        def key(record: dict[str, Any]) -> tuple[Any, ...]:
            f = record["features_completed"]
            return (-f["body_ratio"], -_percentile(f["volume_ratio"], vol_values), -_percentile(f["relative_strength_7"], rs_values), -_percentile(f["liquidity"], liquidity_values), record["symbol"])
    else:
        rs_values = [feature["relative_strength_14"] for feature in features]
        pullback_median = float(median([feature["pullback_range"] for feature in features]))
        def key(record: dict[str, Any]) -> tuple[Any, ...]:
            f = record["features_completed"]
            return (-_percentile(f["relative_strength_14"], rs_values), -int(bool(f["pullback_hold"])), -int(bool(f["daily_context"])), abs(f["pullback_range"] - pullback_median), -_percentile(f["liquidity"], liquidity_values), record["symbol"])
    for rank, record in enumerate(sorted(confirmed, key=key), start=1):
        record.setdefault("state_ranks", {})[state] = rank
        if record["primary_state"] == state:
            record["rank_within_state"] = rank


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    output = {key: value for key, value in record.items() if not key.startswith("_")}
    output["state_reason_codes"] = {key: sorted(set(value)) for key, value in output["state_reason_codes"].items()}
    return output


def scan_completed_candles(scan_input: Mapping[str, Any], config: ScanConfig | None = None) -> dict[str, Any]:
    """Build an immutable, nonblocking ScanResult from pre-collected data.

    No output condition depends on high-confidence candidates, a ledger, LLM,
    optional orderbook/trade data or a live API.  `qualified_enabled` remains
    false until a later, separately approved outcome cohort.
    """
    config = config or ScanConfig()
    if not isinstance(scan_input, Mapping):
        raise TypeError("scan_input must be a mapping")
    exchange_id = str(scan_input.get("exchange_id", "")).strip().lower()
    quote = str(scan_input.get("quote", "")).strip().upper()
    raw_symbols = scan_input.get("symbols")
    if not exchange_id or not quote or not isinstance(raw_symbols, list):
        raise ValueError("exchange_id, quote and symbols[] are required")
    benchmark_daily, benchmark_error = _completed_valid(scan_input.get("benchmark_daily"))
    universe_symbols = sorted(str(item.get("symbol", "")).strip().upper() for item in raw_symbols if isinstance(item, Mapping))
    universe_hash = sha256("|".join(universe_symbols).encode("utf-8")).hexdigest()
    records = [_base_feature_record(item, benchmark_daily, config) for item in raw_symbols if isinstance(item, Mapping)]
    _confirm_states(records, config)
    for state in ("S1", "S2", "S3"):
        _rank_state(records, state)
    observations = [_clean_record(record) for record in sorted(records, key=lambda item: item["symbol"])]
    basic_ohlcv_count = sum(record["module_status"]["daily"] == "available" for record in observations)
    display_candidates = [record for record in observations if record["display_tier"] == "observed"]
    limited_count = len(observations) - basic_ohlcv_count
    snapshot_status = "complete" if basic_ohlcv_count == len(observations) and benchmark_error is None else ("limited" if basic_ohlcv_count else "data_unavailable")
    collection_manifest = dict(scan_input.get("collection_manifest") or {})
    snapshot_errors: list[str] = list(collection_manifest.get("manifest_error_codes") or [])
    if benchmark_error:
        snapshot_errors.append(f"BENCHMARK_{benchmark_error}")
    if not benchmark_daily:
        snapshot_errors.append("BENCHMARK_UNAVAILABLE")
    scan_started = scan_input.get("scan_started_at_utc")
    scan_finished = scan_input.get("scan_finished_at_utc")
    snapshot_seed = {
        "schema_version": SCHEMA_VERSION,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "exchange_id": exchange_id,
        "quote": quote,
        "scan_started_at_utc": scan_started,
        "scan_finished_at_utc": scan_finished,
        "universe_hash": universe_hash,
    }
    snapshot_id = sha256(json.dumps(snapshot_seed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "rank_policy_version": "fcnext-rank-v1-draft",
        "qualified_enabled": False,
        "status": "complete" if snapshot_status == "complete" else ("partial" if basic_ohlcv_count else "data_limited"),
        "snapshot": {
            "snapshot_id": snapshot_id,
            "schema_version": SCHEMA_VERSION,
            "feature_contract_version": FEATURE_CONTRACT_VERSION,
            "scan_started_at_utc": scan_started,
            "scan_finished_at_utc": scan_finished,
            "exchange_id": exchange_id,
            "quote": quote,
            "universe_hash": universe_hash,
            "universe_count": len(observations),
            "basic_ohlcv_coverage": (basic_ohlcv_count / len(observations)) if observations else 0.0,
            "snapshot_status": snapshot_status,
            "error_codes": snapshot_errors,
            "config": {
                "daily_window": config.daily_window,
                "intraday_window": config.intraday_window,
                "state1_boxwidth_quantile_max": config.state1_boxwidth_quantile_max,
                "state1_range_position_min": config.state1_range_position_min,
                "minimum_peer_count": config.minimum_peer_count,
                "support_confirmation_bars": config.support_confirmation_bars,
            },
        },
        "collection_manifest": collection_manifest,
        "coverage": {
            "universe_total": len(observations),
            "basic_ohlcv": basic_ohlcv_count,
            "data_limited": limited_count,
            "observation_candidates": len(display_candidates),
            "state_pending": sum(any(value == "pending_data" for value in record["state_status"].values()) for record in observations),
        },
        "observations": observations,
        "display_candidates": sorted(display_candidates, key=lambda item: ({"S2": 0, "S3": 1, "S1": 2}[item["primary_state"]], item["rank_within_state"], item["symbol"])),
        "ledger_status": "not_attempted",
    }
