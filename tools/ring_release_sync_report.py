#!/usr/bin/env python3
"""Summarize Ring release fences and host wait times from STRATA_RING_ACCESS_TRACE JSONL."""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
import sys
from typing import Any


def _distribution(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    ordered = sorted(values)

    def percentile(p: float) -> float:
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    return {
        "count": len(ordered),
        "total_us": round(sum(ordered), 3),
        "mean_us": round(statistics.mean(ordered), 3),
        "p50_us": round(percentile(0.50), 3),
        "p95_us": round(percentile(0.95), 3),
        "p99_us": round(percentile(0.99), 3),
        "max_us": round(ordered[-1], 3),
    }


def summarize(path: pathlib.Path) -> dict[str, Any]:
    meta: dict[str, Any] | None = None
    phase: str | None = None
    decode_tokens = 0
    release_rows: list[dict[str, Any]] = []
    sync_errors: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8-sig") as stream:
        for lineno, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if row.get("kind") == "meta":
                if meta is not None:
                    raise ValueError(f"{path}:{lineno}: duplicate metadata")
                meta = row
            elif row.get("kind") == "marker":
                name = row.get("name")
                if name == "decode_begin":
                    phase = "decode"
                elif name == "decode_end":
                    phase = "after_decode"
                elif name == "decode_token" and phase == "decode":
                    decode_tokens += 1
            elif phase == "decode" and row.get("kind") == "release" and not row.get("verifier", False):
                release_rows.append(row)
            elif phase == "decode" and row.get("kind") == "release_sync_error":
                sync_errors.append(row)

    if meta is None or meta.get("version") != 1:
        raise ValueError("expected one Ring access trace metadata record, version 1")
    if phase != "after_decode" or decode_tokens == 0:
        raise ValueError("trace must contain decode_begin, decode_token, and decode_end markers")
    layers = int(meta.get("layers", 0))
    if layers < 2:
        raise ValueError("trace metadata must contain at least two layers")

    pre_pool_events = [row for row in release_rows if int(row["layer"]) < layers - 1]
    token_tail_events = [row for row in release_rows if int(row["layer"]) == layers - 1]
    for row in pre_pool_events:
        if "sync_status" not in row or "query_status" not in row or "event_pending" not in row:
            raise ValueError("decode release rows lack event timing fields; rebuild with release-sync instrumentation")
    pre_pool_sync_error_rows = [row for row in sync_errors if int(row["layer"]) < layers - 1]
    token_tail_sync_errors = [row for row in sync_errors if int(row["layer"]) == layers - 1]
    timed_calls = [row for row in pre_pool_events if int(row["sync_status"]) >= 0]
    timed_calls.extend(pre_pool_sync_error_rows)

    if not timed_calls and not pre_pool_events and not pre_pool_sync_error_rows:
        raise ValueError("trace contains no pre-pool Ring release events")

    successful = [row for row in timed_calls if int(row["sync_status"]) == 0]
    failed = [row for row in timed_calls if int(row["sync_status"]) != 0]
    pending = [row for row in timed_calls if row.get("event_pending") is True]
    ready = [row for row in timed_calls if row.get("event_pending") is False]
    query_errors = [row for row in timed_calls if row.get("event_pending") is None and int(row["query_status"]) >= 0]
    wait_us = [float(row["sync_wall_us"]) for row in successful]
    pending_wait_us = [float(row["sync_wall_us"]) for row in successful if row["event_pending"] is True]
    query_us = [float(row["query_us"]) for row in timed_calls if int(row["query_status"]) >= 0]
    dispatches = decode_tokens * layers

    return {
        "schema_version": 1,
        "trace_version": int(meta["version"]),
        "layers": layers,
        "decode_tokens": decode_tokens,
        "decode_dispatches_estimate": dispatches,
        "decode_release_events": len(release_rows),
        "pre_pool_release_events": len(pre_pool_events),
        "pre_pool_release_without_event": sum(int(row["sync_status"]) < 0 for row in pre_pool_events),
        "pre_pool_sync_calls": len(timed_calls),
        "pre_pool_sync_successes": len(successful),
        "pre_pool_sync_fraction_of_dispatches": round(len(timed_calls) / dispatches, 6),
        "pre_pool_event_pending_before_sync": len(pending),
        "pre_pool_event_ready_before_sync": len(ready),
        "pre_pool_event_query_errors": len(query_errors),
        "pre_pool_sync_errors": len(failed),
        "token_tail_release_events": len(token_tail_events),
        "token_tail_sync_errors": len(token_tail_sync_errors),
        "sync_wall_us": _distribution(wait_us),
        "pending_sync_wall_us": _distribution(pending_wait_us),
        "event_query_us": _distribution(query_us),
        "interpretation": (
            "A pre-pool release with an event is synchronized before the next layer's CPU pool call; a row without "
            "an event has no device fence. event_pending records cudaEventQuery immediately before "
            "cudaEventSynchronize; sync_wall_us is host time inside cudaEventSynchronize. Rows without an event "
            "have query_status and sync_status -1. Token-tail releases are excluded from the pre-pool count."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=pathlib.Path, help="STRATA_RING_ACCESS_TRACE JSONL file")
    parser.add_argument("--output", type=pathlib.Path, help="write the summary JSON to this path")
    args = parser.parse_args()
    try:
        report = summarize(args.trace)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ring_release_sync_report: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
