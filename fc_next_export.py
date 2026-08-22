"""Pure C-A Capture-Replay export builder; it never writes a ledger or calls Notion."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from typing import Any, Mapping


HORIZONS_HOURS = (24, 72, 168)
EXPORT_SCHEMA_VERSION = "fcnext-capture-replay-v1-draft"


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("snapshot scan_finished_at_utc is required for Capture-Replay export")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _outcome_schedule(scan_finished: datetime) -> list[dict[str, Any]]:
    return [
        {"horizon_hours": hours, "due_at_utc": _iso(scan_finished + timedelta(hours=hours)), "status": "pending_manual"}
        for hours in HORIZONS_HOURS
    ]


def build_capture_replay_export(scan_result: Mapping[str, Any]) -> dict[str, Any]:
    """Build immutable C-A export without selecting only display candidates."""
    snapshot = dict(scan_result.get("snapshot") or {})
    scan_finished = _parse_utc(snapshot.get("scan_finished_at_utc"))
    observations = list(scan_result.get("observations") or [])
    export = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "snapshot": snapshot,
        "collection_manifest": dict(scan_result.get("collection_manifest") or {}),
        "coverage": dict(scan_result.get("coverage") or {}),
        "qualified_enabled": bool(scan_result.get("qualified_enabled")),
        "ledger_status": "manual_capture_required",
        "outcome_schedule": _outcome_schedule(scan_finished),
        # All observations are included to prevent outcome cherry-picking.
        "observations": observations,
    }
    json_text = json.dumps(export, ensure_ascii=False, indent=2, sort_keys=True)
    lines = [
        "# FindCoin FC-Next — C-A Capture-Replay Snapshot",
        "",
        f"- Snapshot ID: `{snapshot.get('snapshot_id', '')}`",
        f"- Exchange / Quote: `{snapshot.get('exchange_id', '')}` / `{snapshot.get('quote', '')}`",
        f"- Scan finished (UTC): `{snapshot.get('scan_finished_at_utc', '')}`",
        f"- Feature contract: `{snapshot.get('feature_contract_version', '')}`",
        f"- Universe hash: `{snapshot.get('universe_hash', '')}`",
        f"- Coverage: total `{export['coverage'].get('universe_total', 0)}`, basic OHLCV `{export['coverage'].get('basic_ohlcv', 0)}`, data-limited `{export['coverage'].get('data_limited', 0)}`",
        f"- State pending: S1 `{export['coverage'].get('s1_pending', 0)}`, S2 `{export['coverage'].get('s2_pending', 0)}`, S3 `{export['coverage'].get('s3_pending', 0)}`, all-state `{export['coverage'].get('all_states_pending', export['coverage'].get('state_pending', 0))}`",
        f"- P0 baseline: valid `{export['coverage'].get('p0_valid', 0)}`, not attempted `{export['coverage'].get('p0_not_attempted', 0)}`, unavailable `{export['coverage'].get('p0_unavailable', 0)}`",
        "- High-confidence label: disabled; this record is an observation snapshot.",
        "",
        "## Manual outcome schedule",
        "",
        "| Horizon | Due at (UTC) | Status |",
        "|---:|---|---|",
    ]
    for row in export["outcome_schedule"]:
        lines.append(f"| {row['horizon_hours']}h | {row['due_at_utc']} | {row['status']} |")
    lines.extend([
        "",
        "## Recording rule",
        "",
        "Store the attached JSON without removing non-candidates, data-limited symbols, deferred symbols, P0-not-attempted observations, or P0-unavailable observations. Only observations with a valid P0 baseline can enter a P0-based return cohort. Record outcome availability as `available`, `absolute_only`, `stale`, `unavailable`, or `delisted`; never replace a missing outcome with zero.",
    ])
    snapshot_id = str(snapshot.get("snapshot_id") or "unknown")
    return {
        "filename": f"fcnext_capture_replay_{snapshot_id[:16]}.json",
        "json_text": json_text,
        "markdown_text": "\n".join(lines) + "\n",
        "export": export,
    }
