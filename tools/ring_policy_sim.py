#!/usr/bin/env python3
"""Offline replay for STRATA Ring access traces.

This tool is deliberately hardware-independent. It compares replacement policies against the same recorded
Ring-source request sequence; it does not change the runtime policy or claim that simulated I/O time is wall time.
Trace format is JSONL version 1, produced by STRATA_RING_ACCESS_TRACE.
"""
from __future__ import annotations

import argparse
import collections
import heapq
import json
import pathlib
import statistics
import sys
from dataclasses import dataclass
from typing import Any

Key = tuple[int, int]


@dataclass
class Counts:
    requests: int = 0
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    blocked: int = 0

    def delta(self, other: "Counts") -> "Counts":
        return Counts(*(getattr(self, field) - getattr(other, field)
                        for field in ("requests", "hits", "misses", "evictions", "blocked")))


def load_trace(path: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
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
            events.append(row)
    if meta is None or meta.get("version") != 1:
        raise ValueError("expected one Ring access trace metadata record, version 1")
    return meta, events


def io_costs(events: list[dict[str, Any]]) -> dict[Key, float]:
    """Estimate per-expert read cost from full-trace batch timings; this is offline, not an online estimate."""
    samples: dict[Key, list[float]] = collections.defaultdict(list)
    for row in events:
        if row.get("kind") != "io_batch" or not row.get("success"):
            continue
        members = row.get("members", [])
        if not members:
            continue
        estimate = float(row.get("elapsed_ms", 0.0)) / len(members)
        for member in members:
            if len(member) >= 2 and member[0] >= 0 and member[1] >= 0:
                samples[(int(member[0]), int(member[1]))].append(estimate)
    return {key: statistics.mean(values) for key, values in samples.items() if values}


class Replay:
    def __init__(self, capacity: int, policy: str, hot_fraction: float = 0.2,
                 decay_factor: float = 0.5, decay_interval: int = 4096,
                 costs: dict[Key, float] | None = None):
        if capacity < 1:
            raise ValueError("capacity must be at least one slot")
        if policy not in {"lru", "slru", "lfu-decay", "cost-aware-lfu"}:
            raise ValueError(f"unknown policy: {policy}")
        self.capacity = capacity
        self.policy = policy
        self.hot_cap = max(1, min(capacity, round(capacity * hot_fraction)))
        self.decay_factor = decay_factor
        self.decay_interval = max(1, decay_interval)
        self.costs = costs or {}
        known = [v for v in self.costs.values() if v > 0]
        self.default_cost = statistics.mean(known) if known else 1.0
        # Ordered dictionaries store LRU -> MRU order. Slots in the trace are initially empty at Ring open.
        self.order: collections.OrderedDict[Key, None] = collections.OrderedDict()
        self.cold: collections.OrderedDict[Key, None] = collections.OrderedDict()
        self.hot: collections.OrderedDict[Key, None] = collections.OrderedDict()
        self.frequency: collections.defaultdict[Key, float] = collections.defaultdict(float)
        self.held: set[Key] = set()
        self.recency: dict[Key, int] = {}
        self.versions: collections.defaultdict[Key, int] = collections.defaultdict(int)
        self.heap: list[tuple[float, int, Key, int]] = []
        self.clock = 0
        self.counts = Counts()
        self.accesses_since_decay = 0
        self.snapshots: dict[str, Counts] = {}
        self.failed_reads = 0

    def snapshot(self, name: str) -> None:
        self.snapshots[name] = Counts(**vars(self.counts))

    def _rebuild_heap(self) -> None:
        self.heap.clear()
        for key in self.order:
            self.versions[key] += 1
            cost = self.costs.get(key, self.default_cost) if self.policy == "cost-aware-lfu" else 1.0
            score = self.frequency[key] * cost
            heapq.heappush(self.heap, (score, self.recency.get(key, self.clock), key, self.versions[key]))

    def _push_heap(self, key: Key) -> None:
        self.versions[key] += 1
        # Cost-aware LFU uses per-expert costs measured over the full trace; it is an offline experiment.
        cost = self.costs.get(key, self.default_cost) if self.policy == "cost-aware-lfu" else 1.0
        score = self.frequency[key] * cost
        heapq.heappush(self.heap, (score, self.recency.get(key, self.clock), key, self.versions[key]))
        if len(self.heap) > max(1024, 4 * self.capacity):
            self._rebuild_heap()

    def _touch(self, key: Key) -> None:
        self.clock += 1
        self.recency[key] = self.clock
        if key in self.order:
            self.order.move_to_end(key)
        else:
            self.order[key] = None
        if self.policy == "slru":
            if key in self.hot:
                self.hot.move_to_end(key)
            elif key in self.cold:
                del self.cold[key]
                if len(self.hot) >= self.hot_cap:
                    demoted, _ = self.hot.popitem(last=False)
                    self.cold[demoted] = None
                self.hot[key] = None
            else:
                self.cold[key] = None
        if self.policy in {"lfu-decay", "cost-aware-lfu"}:
            self._push_heap(key)

    def _victim(self) -> Key | None:
        if self.policy == "lru":
            return next((key for key in self.order if key not in self.held), None)
        if self.policy == "slru":
            for key in self.cold:
                if key not in self.held:
                    return key
            for key in self.hot:
                if key not in self.held:
                    return key
            return None
        if self.policy in {"lfu-decay", "cost-aware-lfu"}:
            skipped = []
            while self.heap:
                item = heapq.heappop(self.heap)
                _, _, key, version = item
                if key not in self.order or version != self.versions[key]:
                    continue
                if key in self.held:
                    skipped.append(item)
                    continue
                for held_item in skipped:
                    heapq.heappush(self.heap, held_item)
                return key
            for held_item in skipped:
                heapq.heappush(self.heap, held_item)
            return None
        return candidates[0]

    def _decay(self) -> None:
        if self.policy not in {"lfu-decay", "cost-aware-lfu"}:
            return
        self.accesses_since_decay += 1
        if self.accesses_since_decay >= self.decay_interval:
            for key in list(self.frequency):
                self.frequency[key] *= self.decay_factor
            if self.policy in {"lfu-decay", "cost-aware-lfu"}:
                self._rebuild_heap()
            self.accesses_since_decay = 0

    def process(self, events: list[dict[str, Any]]) -> None:
        for row in events:
            kind = row.get("kind")
            if kind == "initial_lru":
                for entry in row.get("slots", []):
                    if len(entry) >= 3 and int(entry[1]) >= 0 and int(entry[2]) >= 0:
                        key = (int(entry[1]), int(entry[2]))
                        if key not in self.order and len(self.order) < self.capacity:
                            self._touch(key)
            elif kind == "marker":
                name = row.get("name")
                if name in {"decode_begin", "decode_end"}:
                    self.snapshot(str(name))
            elif kind == "release" and not row.get("verifier", False):
                layer = int(row["layer"])
                self.held = {key for key in self.held if key[0] != layer}
            elif kind == "access" and not row.get("verifier", False):
                key = (int(row["layer"]), int(row["expert"]))
                self.counts.requests += 1
                self._decay()
                self.frequency[key] += 1.0
                if key in self.order:
                    self.counts.hits += 1
                    self._touch(key)
                    self.held.add(key)
                    continue
                self.counts.misses += 1
                if len(self.order) >= self.capacity:
                    victim = self._victim()
                    if victim is None:
                        self.counts.blocked += 1
                        continue
                    del self.order[victim]
                    self.cold.pop(victim, None)
                    self.hot.pop(victim, None)
                    self.counts.evictions += 1
                self._touch(key)
                self.held.add(key)
            elif kind == "io_batch" and not row.get("success", False):
                self.failed_reads += 1

    def phase_counts(self, phase: str) -> Counts | None:
        start = self.snapshots.get("decode_begin")
        end = self.snapshots.get("decode_end")
        if phase == "all":
            return Counts(**vars(self.counts))
        if start is None or end is None:
            return None
        return end.delta(start)


def transition_report(events: list[dict[str, Any]], prefetch_k: int) -> dict[str, Any]:
    """Online expert-ID transition predictor, trained only on earlier decode tokens."""
    token_layers: list[dict[int, set[int]]] = []
    token_misses: list[dict[int, set[int]]] = []
    active = False
    current: dict[int, set[int]] | None = None
    misses: dict[int, set[int]] | None = None
    for row in events:
        if row.get("kind") == "marker" and row.get("name") == "decode_begin":
            active = True
            continue
        if row.get("kind") == "marker" and row.get("name") == "decode_end":
            active = False
            if current is not None:
                token_layers.append(current)
                token_misses.append(misses or {})
                current = misses = None
            continue
        if not active:
            continue
        if row.get("kind") == "marker" and row.get("name") == "decode_token":
            if current is not None:
                token_layers.append(current)
                token_misses.append(misses or {})
            current = collections.defaultdict(set)
            misses = collections.defaultdict(set)
        elif row.get("kind") == "access" and not row.get("verifier", False) and current is not None:
            layer, expert = int(row["layer"]), int(row["expert"])
            current[layer].add(expert)
            if not row.get("hit", False):
                misses[layer].add(expert)
    if current is not None:
        token_layers.append(current)
        token_misses.append(misses or {})

    # transitions[layer][source expert][target expert] = prior co-occurrence count
    transitions: dict[int, dict[int, collections.Counter[int]]] = collections.defaultdict(
        lambda: collections.defaultdict(collections.Counter))
    predicted = actual = useful = 0
    precision_hits = recall_hits = 0
    opportunities = 0
    for t, layers in enumerate(token_layers):
        for layer, sources in layers.items():
            targets = layers.get(layer + 1, set())
            if sources and targets:
                scores: collections.Counter[int] = collections.Counter()
                for source in sources:
                    scores.update(transitions[layer].get(source, {}))
                if scores:
                    guess = {expert for expert, _ in scores.most_common(prefetch_k)}
                    predicted += len(guess)
                    actual += len(targets)
                    precision_hits += len(guess & targets)
                    recall_hits += len(guess & targets)
                    useful += len(guess & token_misses[t].get(layer + 1, set()))
                    opportunities += 1
            for source in sources:
                for target in targets:
                    transitions[layer][source][target] += 1
    return {
        "decode_tokens": len(token_layers),
        "prefetch_k": prefetch_k,
        "prediction_opportunities": opportunities,
        "predicted_experts": predicted,
        "actual_next_layer_experts": actual,
        "precision": precision_hits / predicted if predicted else 0.0,
        "recall": recall_hits / actual if actual else 0.0,
        "predicted_actual_ring_misses": useful,
        "useful_fraction_of_predictions": useful / predicted if predicted else 0.0,
        "note": "Ring-request transitions only; this measures ID accuracy, not I/O overlap or latency saved.",
    }


def as_dict(counts: Counts) -> dict[str, Any]:
    return {
        **vars(counts),
        "hit_rate": counts.hits / counts.requests if counts.requests else 0.0,
        "miss_rate": counts.misses / counts.requests if counts.requests else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=pathlib.Path)
    parser.add_argument("--capacity", type=int, help="Ring slots to simulate (default: trace normal_slots)")
    parser.add_argument("--phase", choices=("all", "decode"), default="decode")
    parser.add_argument("--policies", default="lru,slru,lfu-decay,cost-aware-lfu")
    parser.add_argument("--hot-fraction", type=float, default=0.2)
    parser.add_argument("--decay-factor", type=float, default=0.5)
    parser.add_argument("--decay-interval", type=int, default=4096)
    parser.add_argument("--prefetch-k", type=int, default=4)
    parser.add_argument("--json-out", type=pathlib.Path)
    parser.add_argument("--check-baseline", action="store_true",
                        help="require simulated LRU counts to match the trace summary")
    args = parser.parse_args(argv)
    try:
        meta, events = load_trace(args.trace)
        capacity = args.capacity or int(meta["normal_slots"])
        costs = io_costs(events)
        output: dict[str, Any] = {
            "trace": str(args.trace),
            "capacity_slots": capacity,
            "trace_capacity_slots": int(meta["normal_slots"]),
            "phase": args.phase,
            "policies": {},
        }
        lru_all: Counts | None = None
        for policy in args.policies.split(","):
            replay = Replay(capacity, policy.strip(), args.hot_fraction, args.decay_factor,
                           args.decay_interval, costs)
            replay.process(events)
            counts = replay.phase_counts(args.phase)
            if counts is None:
                raise ValueError("trace is missing decode_begin/decode_end markers")
            output["policies"][policy.strip()] = as_dict(counts)
            if policy.strip() == "lru":
                lru_all = Counts(**vars(replay.counts))
        transition = transition_report(events, max(1, args.prefetch_k))
        output["transition_prefetch"] = transition
        summary = next((row for row in reversed(events) if row.get("kind") == "summary"), None)
        if summary is not None:
            output["trace_summary"] = summary
        if args.check_baseline:
            if args.phase != "all" or lru_all is None or summary is None:
                raise ValueError("--check-baseline requires --phase all, lru in --policies, and a trace summary")
            checks = {"hits": lru_all.hits, "misses": lru_all.misses, "evictions": lru_all.evictions}
            mismatch = {key: (value, int(summary.get(key, -1))) for key, value in checks.items()
                        if value != int(summary.get(key, -1))}
            if mismatch:
                output["baseline_match"] = False
                output["baseline_mismatch"] = mismatch
            else:
                output["baseline_match"] = True
        rendered = json.dumps(output, indent=2, sort_keys=True)
        if args.json_out:
            args.json_out.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return 1 if args.check_baseline and not output.get("baseline_match", False) else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ring_policy_sim: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
