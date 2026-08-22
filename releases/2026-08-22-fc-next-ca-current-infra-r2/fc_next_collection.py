"""FC-Next FC-0 completed-candle collection adapter.

The adapter is intentionally transport-free: it accepts an exchange-like object
with CCXT-compatible read methods but never imports CandleView main.py, starts
a Telegram loop, calls an LLM, or writes a ledger.  It produces the normalized
input contract consumed by fc_next_core.scan_completed_candles.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from math import isfinite
from typing import Any, Callable, Mapping
import time

from fc_next_core import ScanConfig, state3_intraday_data_required

STABLECOIN_BASES = {"USDT", "USDC", "DAI", "TUSD", "PAX", "GUSD", "PYUSD", "BUSD", "USDP", "FDUSD"}
INTRADAY_TIMEFRAME_BY_EXCHANGE = {"upbit": "4h", "bithumb": "4h", "coinbase": "6h"}
TIMEFRAME_MS = {"1d": 86_400_000, "4h": 14_400_000, "6h": 21_600_000}


@dataclass(frozen=True)
class CollectionConfig:
    daily_limit: int = 60
    intraday_limit: int = 32
    # None is a production full-manifest scan.  Any cap is a probe only and
    # must remain visibly labelled as such in the result/export.
    max_symbols: int | None = None
    # At most this many exchange requests are in flight for the collection.
    max_workers: int = 1
    # None means no wall-clock cutoff.  A cutoff emits deferred manifest rows
    # rather than cancelling the entire nonblocking FindCoin result.
    time_budget_seconds: float | None = None


def _as_epoch_ms(value: Any) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime timestamp must be timezone-aware UTC")
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        text = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    raise TypeError("timestamp must be epoch milliseconds or ISO-8601")


def _iso_utc(epoch_ms: int) -> str:
    return datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _finite_positive(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(parsed) and parsed > 0


def _normalize_raw_ohlcv(raw_rows: Any, *, timeframe: str, collected_at_ms: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate timestamps and return rows with an explicit completed-candle flag.

    An internal time gap invalidates this TF for State use rather than silently
    compressing calendar time.  The error is returned to the observation
    manifest; caller passes an empty core series so it becomes pending_data.
    """
    tf_ms = TIMEFRAME_MS[timeframe]
    if not isinstance(raw_rows, list) or not raw_rows:
        return [], [f"{timeframe.upper()}_UNAVAILABLE"]
    normalized: list[dict[str, Any]] = []
    previous_ts: int | None = None
    for raw in raw_rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            return [], [f"{timeframe.upper()}_MALFORMED"]
        try:
            timestamp = int(raw[0])
            o, h, l, c, v = (float(raw[1]), float(raw[2]), float(raw[3]), float(raw[4]), float(raw[5]))
        except (TypeError, ValueError):
            return [], [f"{timeframe.upper()}_MALFORMED"]
        if timestamp < 0 or not all(map(_finite_positive, (o, h, l, c))) or not isfinite(v) or v < 0:
            return [], [f"{timeframe.upper()}_INVALID_OHLCV"]
        if h < l or h < max(o, c) or l > min(o, c):
            return [], [f"{timeframe.upper()}_INVALID_OHLCV"]
        if previous_ts is not None:
            if timestamp <= previous_ts:
                return [], [f"{timeframe.upper()}_NONMONOTONIC_TIMESTAMP"]
            if timestamp - previous_ts != tf_ms:
                return [], [f"{timeframe.upper()}_INTERNAL_GAP"]
        previous_ts = timestamp
        normalized.append({
            "timestamp": _iso_utc(timestamp),
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
            "complete": timestamp + tf_ms <= collected_at_ms,
        })
    return normalized, []


def _weekly_from_completed_daily(daily_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Mirror the main UTC Monday weekly grouping, emitting only full 7-day buckets."""
    completed = [row for row in daily_rows if row.get("complete") is True]
    if not completed:
        return [], ["WEEKLY_DAILY_UNAVAILABLE"]
    buckets: dict[datetime, list[dict[str, Any]]] = {}
    for row in completed:
        dt = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")).astimezone(timezone.utc)
        monday = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc) - timedelta(days=dt.weekday())
        buckets.setdefault(monday, []).append(row)
    weekly: list[dict[str, Any]] = []
    internal_incomplete_bucket_seen = False
    monday_keys = sorted(buckets)
    for bucket_index, monday in enumerate(monday_keys):
        rows = sorted(buckets[monday], key=lambda item: item["timestamp"])
        expected_dates = [monday + timedelta(days=offset) for offset in range(7)]
        actual_dates = [datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).date() for row in rows]
        if len(rows) != 7 or actual_dates != [date.date() for date in expected_dates]:
            # History may begin mid-week and the current week is normally incomplete.
            # Only an incomplete bucket strictly between those edges is a data-gap error.
            if 0 < bucket_index < len(monday_keys) - 1:
                internal_incomplete_bucket_seen = True
            continue
        weekly.append({
            "timestamp": monday.isoformat().replace("+00:00", "Z"),
            "open": rows[0]["open"],
            "high": max(row["high"] for row in rows),
            "low": min(row["low"] for row in rows),
            "close": rows[-1]["close"],
            "volume": sum(row["volume"] for row in rows),
            "complete": True,
        })
    errors = ["WEEKLY_INTERNAL_GAP"] if internal_incomplete_bucket_seen else []
    return weekly, errors


def _fetch_ohlcv(exchange: Any, symbol: str, timeframe: str, limit: int, collected_at_ms: int) -> tuple[list[dict[str, Any]], list[str]]:
    try:
        raw_rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as exc:
        return [], [f"{timeframe.upper()}_FETCH_{type(exc).__name__.upper()}"]
    return _normalize_raw_ohlcv(raw_rows, timeframe=timeframe, collected_at_ms=collected_at_ms)


def _manifest(exchange: Any, quote: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Mapping[str, Any]], list[str]]:
    try:
        loaded = exchange.load_markets()
        markets = loaded if isinstance(loaded, Mapping) else getattr(exchange, "markets", {})
    except Exception as exc:
        return [], [], {}, [f"MARKETS_FETCH_{type(exc).__name__.upper()}"]
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for symbol, market_value in sorted(markets.items()):
        market = dict(market_value or {})
        base = str(market.get("base") or str(symbol).split("/")[0]).upper()
        market_quote = str(market.get("quote") or str(symbol).split("/")[-1]).upper()
        if market_quote != quote:
            continue
        if market.get("active") is False:
            excluded.append({"symbol": symbol, "reason": "INACTIVE_MARKET"})
            continue
        if market.get("spot") is False:
            excluded.append({"symbol": symbol, "reason": "NONSPOT_MARKET"})
            continue
        if base in STABLECOIN_BASES:
            excluded.append({"symbol": symbol, "reason": "STABLECOIN_BASE"})
            continue
        included.append({"symbol": symbol, "base": base})
    return included, excluded, markets, []


def _fetch_tickers_after_features(exchange: Any, symbols: list[str]) -> tuple[dict[str, Mapping[str, Any]], list[str]]:
    """Fetch live ticker data only after all deterministic OHLCV feature calls."""
    if not symbols:
        return {}, []
    try:
        tickers = exchange.fetch_tickers(symbols)
    except Exception as exc:
        return {}, [f"TICKERS_FETCH_{type(exc).__name__.upper()}"]
    return {symbol: dict(value) for symbol, value in (tickers or {}).items() if isinstance(value, Mapping)}, []


def _live_observation_from_ticker(ticker: Mapping[str, Any] | None, observed_at_ms: int) -> tuple[dict[str, Any], list[str], str]:
    """Keep P0 provenance separate from completed feature collection."""
    if not isinstance(ticker, Mapping):
        return {"observed_at_utc": _iso_utc(observed_at_ms), "source": "bulk_ticker"}, ["TICKER_UNAVAILABLE", "P0_UNAVAILABLE"], "unavailable"
    def number_or_none(value: Any) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if isfinite(parsed) and parsed > 0 else None
    bid, ask, last = number_or_none(ticker.get("bid")), number_or_none(ticker.get("ask")), number_or_none(ticker.get("last"))
    errors: list[str] = []
    if bid is not None and ask is not None and bid > ask:
        errors.append("P0_CROSSED_BOOK")
    if bid is not None and ask is not None and bid <= ask:
        p0_status = "mid_available"
    elif last is not None:
        p0_status = "last_fallback"
    else:
        p0_status = "unavailable"
        errors.append("P0_UNAVAILABLE")
    if not _finite_positive(ticker.get("quoteVolume")):
        errors.append("TICKER_LIMITED")
    observation = {
        "bid": bid,
        "ask": ask,
        "last": last,
        "observed_at_utc": _iso_utc(observed_at_ms),
        "source": "bulk_ticker",
        "exchange_timestamp": ticker.get("timestamp"),
        "p0_status": p0_status,
    }
    if ticker.get("timestamp") is None:
        errors.append("P0_TICKER_TIMESTAMP_UNAVAILABLE")
    return observation, errors, ("available" if p0_status != "unavailable" else "limited")


def collect_fc0_completed_data(
    exchange: Any,
    *,
    exchange_id: str,
    quote: str,
    scan_started_at_utc: str | int | float,
    collected_at_utc: str | int | float,
    config: CollectionConfig | None = None,
    scan_config: ScanConfig | None = None,
) -> dict[str, Any]:
    """Collect the whole FC-0 manifest before any State/rank gate.

    The return is a core-compatible input mapping plus a full manifest.  It
    captures no orderbook/trade signal.  It collects the P0 ticker snapshot only
    after deterministic completed-candle retrieval, and P0 never changes State
    or rank.
    """
    config = config or CollectionConfig()
    scan_config = scan_config or ScanConfig()
    exchange_id = str(exchange_id).lower().strip()
    quote = str(quote).upper().strip()
    if exchange_id not in INTRADAY_TIMEFRAME_BY_EXCHANGE:
        raise ValueError("unsupported FC-Next exchange")
    if config.daily_limit < 40 or config.intraday_limit < 12:
        raise ValueError("collection limits are below FC-Next completed-feature minimums")
    if config.max_workers < 1:
        raise ValueError("collection max_workers must be at least 1")
    if config.time_budget_seconds is not None and config.time_budget_seconds <= 0:
        raise ValueError("collection time_budget_seconds must be positive when provided")
    scan_started_ms = _as_epoch_ms(scan_started_at_utc)
    collected_ms = _as_epoch_ms(collected_at_utc)
    if collected_ms < scan_started_ms:
        raise ValueError("collected_at must not precede scan_started")
    included, excluded, _markets, manifest_errors = _manifest(exchange, quote)
    all_symbols = [item["symbol"] for item in included]
    symbols_to_collect = included if config.max_symbols is None else included[:config.max_symbols]
    deferred = included[len(symbols_to_collect):]
    intraday_tf = INTRADAY_TIMEFRAME_BY_EXCHANGE[exchange_id]
    deadline_monotonic = (time.monotonic() + float(config.time_budget_seconds)) if config.time_budget_seconds is not None else None
    def exchange_factory():
        try:
            return type(exchange)({"enableRateLimit": False, "timeout": 8000})
        except Exception:
            # Deterministic mocks commonly do not accept a CCXT-style config;
            # their tests use one worker and remain serial.
            return exchange
    benchmark_symbol = f"BTC/{quote}"
    # Benchmark is completed-data input and must exist before lazy State3
    # planning; P0 remains strictly after all feature collection.
    benchmark_daily, benchmark_errors = _fetch_ohlcv(exchange, benchmark_symbol, "1d", config.daily_limit, collected_ms)
    target_symbols = [item["symbol"] for item in symbols_to_collect]
    daily_map, daily_deferred = _collect_ohlcv_batched(
        source_exchange=exchange, exchange_factory=exchange_factory, symbols=target_symbols,
        timeframe="1d", limit=config.daily_limit, collected_at_ms=collected_ms,
        max_workers=config.max_workers, deadline_monotonic=deadline_monotonic,
    )
    normalized_symbols: list[dict[str, Any]] = []
    state3_intraday_requested: list[str] = []
    state3_intraday_not_required: list[str] = []
    state3_intraday_deferred: list[str] = []
    records_by_symbol: dict[str, dict[str, Any]] = {}
    intraday_needed_symbols: list[str] = []
    for item in symbols_to_collect:
        symbol = item["symbol"]
        if symbol in daily_deferred:
            daily, daily_errors = [], ["COLLECTION_DEFERRED_TIME_BUDGET"]
            weekly, weekly_errors = [], ["WEEKLY_DAILY_UNAVAILABLE"]
            needs_intraday, precondition_reasons = False, ["DAILY_COLLECTION_DEFERRED"]
            intraday_status = "deferred"
        else:
            daily, daily_errors = daily_map.get(symbol, ([], ["1D_UNAVAILABLE"]))
            weekly, weekly_errors = _weekly_from_completed_daily(daily) if daily else ([], ["WEEKLY_DAILY_UNAVAILABLE"])
            needs_intraday, precondition_reasons = state3_intraday_data_required(daily, weekly, scan_config)
            intraday_status = "pending" if needs_intraday else "not_required"
        record = {
            "symbol": symbol, "market": symbol, "daily": daily, "weekly": weekly, "intraday": [],
            "live_observation": {}, "collection_error_codes": daily_errors + weekly_errors,
            "collection_status": {
                "ticker": "not_attempted", "daily": "available" if daily else ("deferred" if symbol in daily_deferred else "unavailable"),
                "weekly": "available" if weekly else "unavailable", "intraday": intraday_status,
                "state3_intraday_precondition": "eligible" if needs_intraday else "pending_data",
                "state3_intraday_precondition_reasons": precondition_reasons,
            },
        }
        normalized_symbols.append(record)
        records_by_symbol[symbol] = record
        if needs_intraday:
            intraday_needed_symbols.append(symbol)
            state3_intraday_requested.append(symbol)
        else:
            state3_intraday_not_required.append(symbol)

    intraday_map, intraday_deferred = _collect_ohlcv_batched(
        source_exchange=exchange, exchange_factory=exchange_factory, symbols=intraday_needed_symbols,
        timeframe=intraday_tf, limit=config.intraday_limit, collected_at_ms=collected_ms,
        max_workers=config.max_workers, deadline_monotonic=deadline_monotonic,
    )
    for symbol in intraday_needed_symbols:
        record = records_by_symbol[symbol]
        if symbol in intraday_deferred:
            record["collection_error_codes"].append("INTRADAY_DEFERRED_TIME_BUDGET")
            record["collection_status"]["intraday"] = "deferred"
            state3_intraday_deferred.append(symbol)
            continue
        intraday, intraday_errors = intraday_map.get(symbol, ([], [f"{intraday_tf.upper()}_UNAVAILABLE"]))
        record["intraday"] = intraday
        record["collection_error_codes"].extend(intraday_errors)
        record["collection_status"]["intraday"] = "available" if intraday else "unavailable"
    for item in deferred:
        normalized_symbols.append({
            "symbol": item["symbol"], "market": item["symbol"], "daily": [], "weekly": [], "intraday": [],
            "live_observation": {"p0_status": "not_attempted"}, "collection_error_codes": ["COLLECTION_DEFERRED", "P0_NOT_ATTEMPTED"],
            "collection_status": {"ticker": "not_attempted", "daily": "deferred", "weekly": "deferred", "intraday": "deferred"},
        })
    # P0 belongs after all completed feature inputs have been collected.  Deferred
    # or daily-unavailable symbols deliberately remain P0_NOT_ATTEMPTED.
    for record in normalized_symbols:
        if not record["daily"] and "P0_NOT_ATTEMPTED" not in record["collection_error_codes"]:
            record["collection_error_codes"].append("P0_NOT_ATTEMPTED")
            record["live_observation"] = {"p0_status": "not_attempted"}
    p0_symbols = [record["symbol"] for record in normalized_symbols if record["daily"]]
    if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
        tickers, ticker_errors = {}, ["TICKERS_DEFERRED_TIME_BUDGET"]
    else:
        tickers, ticker_errors = _fetch_tickers_after_features(exchange, p0_symbols)
    local_now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    p0_observed_ms = max(collected_ms, local_now_ms)
    for record in normalized_symbols:
        if not record["daily"]:
            continue
        observation, p0_errors, ticker_status = _live_observation_from_ticker(tickers.get(record["symbol"]), p0_observed_ms)
        record["live_observation"] = observation
        record["collection_error_codes"].extend(p0_errors)
        record["collection_status"]["ticker"] = ticker_status
    time_budget_expired = bool(daily_deferred or intraday_deferred)
    manifest_hash = sha256("|".join(sorted(item["symbol"] for item in included)).encode("utf-8")).hexdigest()
    return {
        "exchange_id": exchange_id,
        "quote": quote,
        "scan_started_at_utc": _iso_utc(scan_started_ms),
        "scan_finished_at_utc": _iso_utc(p0_observed_ms),
        "benchmark_daily": benchmark_daily,
        "symbols": normalized_symbols,
        "collection_manifest": {
            "target_quote": quote,
            "collection_mode": "full" if config.max_symbols is None else "limited_probe",
            "included_symbols": [item["symbol"] for item in included],
            "excluded": excluded,
            "deferred_symbols": [item["symbol"] for item in deferred],
            "universe_hash": manifest_hash,
            "intraday_timeframe": intraday_tf,
            "p0_observed_at_utc": _iso_utc(p0_observed_ms),
            "p0_symbols_attempted": p0_symbols,
            "state3_intraday_requested_symbols": state3_intraday_requested,
            "state3_intraday_not_required_symbols": state3_intraday_not_required,
            "state3_intraday_deferred_symbols": state3_intraday_deferred,
            "time_budget_seconds": config.time_budget_seconds,
            "time_budget_daily_deferred_symbols": daily_deferred,
            "manifest_error_codes": manifest_errors + ticker_errors + benchmark_errors + (["TIME_BUDGET_EXPIRED"] if time_budget_expired else []),
        },
    }


# NOTE: Helper definitions below are intentionally placed after the public
# collector during the draft stage and are called only after module load.
def _clone_market_state(source: Any, worker: Any) -> Any:
    """Reuse already-loaded read-only CCXT market metadata for a worker instance."""
    for name in ("markets", "markets_by_id", "currencies", "symbols"):
        value = getattr(source, name, None)
        if value is not None:
            try:
                setattr(worker, name, value)
            except Exception:
                pass
    return worker


def _collect_ohlcv_batched(
    *,
    source_exchange: Any,
    exchange_factory: Callable[[], Any] | None,
    symbols: list[str],
    timeframe: str,
    limit: int,
    collected_at_ms: int,
    max_workers: int,
    deadline_monotonic: float | None,
) -> tuple[dict[str, tuple[list[dict[str, Any]], list[str]]], list[str]]:
    """Bounded batch collection with explicit deferred symbols.

    At most `max_workers` requests start in a batch.  The time budget is checked
    between batches so a slow in-flight request can finish cleanly but no new
    batch starts after expiry.  Deferred rows are never silently dropped.
    """
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    results: dict[str, tuple[list[dict[str, Any]], list[str]]] = {}
    deferred: list[str] = []
    cursor = 0
    while cursor < len(symbols):
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            deferred.extend(symbols[cursor:])
            break
        batch = symbols[cursor: cursor + max_workers]
        cursor += len(batch)
        def task(symbol: str):
            worker = _clone_market_state(source_exchange, exchange_factory()) if exchange_factory else source_exchange
            return symbol, _fetch_ohlcv(worker, symbol, timeframe, limit, collected_at_ms)
        if len(batch) == 1:
            symbol, value = task(batch[0])
            results[symbol] = value
            continue
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = [pool.submit(task, symbol) for symbol in batch]
            for future in as_completed(futures):
                symbol, value = future.result()
                results[symbol] = value
    return results, deferred
