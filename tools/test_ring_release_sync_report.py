#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from ring_release_sync_report import summarize


class RingReleaseSyncReportTests(unittest.TestCase):
    def test_summarizes_pre_pool_and_token_tail_events(self) -> None:
        rows = [
            {"kind": "meta", "version": 1, "layers": 3},
            {"kind": "marker", "name": "decode_begin"},
            {"kind": "marker", "name": "decode_token"},
            {"kind": "release", "layer": 0, "verifier": False, "query_status": 34,
             "event_pending": True, "query_us": 2.0, "sync_status": 0, "sync_wall_us": 20.0},
            {"kind": "release", "layer": 1, "verifier": False, "query_status": 0,
             "event_pending": False, "query_us": 1.0, "sync_status": 0, "sync_wall_us": 1.5},
            {"kind": "marker", "name": "decode_token"},
            {"kind": "release", "layer": 2, "verifier": False, "query_status": 0,
             "event_pending": False, "query_us": 1.0, "sync_status": 0, "sync_wall_us": 1.0},
            {"kind": "marker", "name": "decode_end"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            trace = pathlib.Path(temp) / "trace.jsonl"
            trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            report = summarize(trace)

        self.assertEqual(report["decode_dispatches_estimate"], 6)
        self.assertEqual(report["pre_pool_release_events"], 2)
        self.assertEqual(report["pre_pool_sync_calls"], 2)
        self.assertEqual(report["pre_pool_sync_successes"], 2)
        self.assertEqual(report["pre_pool_event_pending_before_sync"], 1)
        self.assertEqual(report["pre_pool_event_ready_before_sync"], 1)
        self.assertEqual(report["pre_pool_event_query_errors"], 0)
        self.assertEqual(report["token_tail_release_events"], 1)
        self.assertEqual(report["sync_wall_us"]["total_us"], 21.5)
        self.assertEqual(report["pending_sync_wall_us"]["count"], 1)

    def test_rejects_uninstrumented_trace(self) -> None:
        rows = [
            {"kind": "meta", "version": 1, "layers": 2},
            {"kind": "marker", "name": "decode_begin"},
            {"kind": "marker", "name": "decode_token"},
            {"kind": "release", "layer": 0, "verifier": False},
            {"kind": "marker", "name": "decode_end"},
        ]
        with tempfile.TemporaryDirectory() as temp:
            trace = pathlib.Path(temp) / "trace.jsonl"
            trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "lack event timing fields"):
                summarize(trace)


if __name__ == "__main__":
    unittest.main()
