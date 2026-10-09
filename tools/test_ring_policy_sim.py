import pathlib
import tempfile
import unittest

from ring_policy_sim import Replay, transition_report


class RingPolicyReplayTests(unittest.TestCase):
    def test_lru_replays_hits_and_evictions_while_respecting_layer_holds(self):
        events = [
            {"kind": "access", "layer": 0, "expert": 1, "hit": False},
            {"kind": "release", "layer": 0},
            {"kind": "access", "layer": 1, "expert": 2, "hit": False},
            {"kind": "release", "layer": 1},
            {"kind": "access", "layer": 0, "expert": 1, "hit": True},
            {"kind": "release", "layer": 0},
            {"kind": "access", "layer": 1, "expert": 3, "hit": False},
            {"kind": "release", "layer": 1},
            {"kind": "access", "layer": 0, "expert": 2, "hit": False},
        ]
        replay = Replay(2, "lru")
        replay.process(events)
        self.assertEqual((replay.counts.requests, replay.counts.hits, replay.counts.misses,
                          replay.counts.evictions, replay.counts.blocked), (5, 1, 4, 2, 0))

    def test_slru_and_decay_policies_run_on_same_trace(self):
        events = []
        for expert in [1, 2, 1, 3, 1, 2, 4, 1]:
            events.extend(({"kind": "access", "layer": 0, "expert": expert},
                           {"kind": "release", "layer": 0}))
        for policy in ("slru", "lfu-decay", "cost-aware-lfu"):
            replay = Replay(2, policy, hot_fraction=0.5, decay_interval=2,
                            costs={(0, 1): 2.0, (0, 2): 1.0})
            replay.process(events)
            self.assertEqual(replay.counts.requests, 8)
            self.assertEqual(replay.counts.hits + replay.counts.misses, 8)
            self.assertEqual(replay.counts.blocked, 0)

    def test_lfu_heap_is_bounded_by_cache_capacity(self):
        events = []
        for i in range(5000):
            events.extend(({"kind": "access", "layer": 0, "expert": i % 12},
                           {"kind": "release", "layer": 0}))
        replay = Replay(8, "lfu-decay", decay_interval=100000)
        replay.process(events)
        self.assertLessEqual(len(replay.heap), max(1024, 4 * replay.capacity))
        self.assertLessEqual(len(replay.order), replay.capacity)

    def test_transition_predictor_uses_only_prior_tokens(self):
        events = [{"kind": "marker", "name": "decode_begin"}]
        for _ in range(3):
            events.append({"kind": "marker", "name": "decode_token"})
            events.extend((
                {"kind": "access", "layer": 0, "expert": 7, "hit": False},
                {"kind": "access", "layer": 1, "expert": 9, "hit": False},
            ))
        events.append({"kind": "marker", "name": "decode_end"})
        result = transition_report(events, 1)
        self.assertEqual(result["decode_tokens"], 3)
        self.assertGreater(result["prediction_opportunities"], 0)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["useful_fraction_of_predictions"], 1.0)

    def test_trace_loader_requires_versioned_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "trace.jsonl"
            path.write_text('{"kind":"meta","version":1}\n{"kind":"access"}\n', encoding="utf-8")
            from ring_policy_sim import load_trace
            meta, rows = load_trace(path)
            self.assertEqual(meta["version"], 1)
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
