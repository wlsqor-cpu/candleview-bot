"""In-process FC-Next C-A runtime adapter.

This adapter deliberately reuses the existing Telegram bot process for the same
on-demand full daily scan class as legacy FindCoin.  It does not import
CandleView main.py, call Gemini, write a durable ledger, consume Telegram
updates, or touch CandleView PHASE sessions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from fc_next_collection import CollectionConfig, collect_fc0_completed_data
from fc_next_core import ScanConfig, scan_completed_candles
from fc_next_export import build_capture_replay_export
from fc_next_telegram import render_scan_result


# Conservative C-A defaults: public market-data calls remain below the known
# UPBIT/Coinbase per-second public limits, while a result is still published as
# partial if the wall-clock budget is exhausted.  BITHUMB uses the lower default
# until its production-like smoke timing is separately verified.
CURRENT_INFRA_COLLECTION_CONFIG = {
    "upbit": CollectionConfig(max_workers=8, time_budget_seconds=90),
    "bithumb": CollectionConfig(max_workers=4, time_budget_seconds=90),
    "coinbase": CollectionConfig(max_workers=4, time_budget_seconds=90),
}


def current_infra_collection_config(exchange_id: str) -> CollectionConfig:
    key = str(exchange_id).lower().strip()
    if key not in CURRENT_INFRA_COLLECTION_CONFIG:
        raise ValueError("unsupported FC-Next exchange")
    return CURRENT_INFRA_COLLECTION_CONFIG[key]


def run_fc_next_ca_scan(
    *,
    exchange: Any,
    exchange_id: str,
    quote: str,
    now: datetime | None = None,
    collection_config: CollectionConfig | None = None,
    scan_config: ScanConfig | None = None,
) -> dict[str, Any]:
    """Run one Gemini-free current-infrastructure FC-Next C-A scan.

    Full FC-0 daily collection is the default (`max_symbols=None`).  A supplied
    `max_symbols` is an explicit test/probe mode, labelled by the collection
    adapter and never eligible for performance evaluation.
    """
    now = now or datetime.now(timezone.utc)
    collection = collect_fc0_completed_data(
        exchange,
        exchange_id=exchange_id,
        quote=quote,
        scan_started_at_utc=now,
        collected_at_utc=now,
        config=collection_config or current_infra_collection_config(exchange_id),
        scan_config=scan_config or ScanConfig(),
    )
    scan_result = scan_completed_candles(collection, config=scan_config or ScanConfig())
    rendered = render_scan_result(scan_result)
    export = build_capture_replay_export(scan_result)
    return {
        "scan_result": scan_result,
        "rendered": rendered,
        "capture_replay_export": export,
        "gemini_called": False,
    }


def run_fc_next_ca_with_factory(
    *,
    exchange_factory: Callable[[], Any],
    exchange_id: str,
    quote: str,
    now: datetime | None = None,
    collection_config: CollectionConfig | None = None,
    scan_config: ScanConfig | None = None,
) -> dict[str, Any]:
    """Instantiate an exchange only at the FindCoin boundary for testability."""
    return run_fc_next_ca_scan(
        exchange=exchange_factory(), exchange_id=exchange_id, quote=quote, now=now,
        collection_config=collection_config, scan_config=scan_config,
    )
