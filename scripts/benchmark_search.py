"""Benchmark kasane search latency.

Usage:
    uv run python scripts/benchmark_search.py --query "Tailscale 設定" --top-k 3
    uv run python scripts/benchmark_search.py --query "Tailscale 設定" --top-k 3 --runs 1
"""

from __future__ import annotations

import argparse
import statistics
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Measurement:
    name: str
    runs: int
    seconds: list[float]

    @property
    def average(self) -> float:
        return statistics.fmean(self.seconds)

    @property
    def minimum(self) -> float:
        return min(self.seconds)

    @property
    def maximum(self) -> float:
        return max(self.seconds)


def _time_call(call: Callable[[], object]) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


def _measure(name: str, runs: int, call: Callable[[], object]) -> Measurement:
    return Measurement(
        name=name,
        runs=runs,
        seconds=[_time_call(call) for _ in range(runs)],
    )


def _run_cli_search(query: str, top_k: int, no_vector: bool) -> None:
    command = [
        "uv",
        "run",
        "kasane",
        "search",
        "--query",
        query,
        "--top-k",
        str(top_k),
    ]
    if no_vector:
        command.append("--no-vector")
    subprocess.run(command, check=True, capture_output=True, text=True)


def _run_in_process_search(query: str, top_k: int, use_vector: bool) -> int:
    from kasane import search, storage

    storage.init_db()
    return len(search.hybrid_search(query, top_k=top_k, use_vector=use_vector))


def _print_measurement(measurement: Measurement) -> None:
    print(
        f"{measurement.name}: runs={measurement.runs} "
        f"avg={measurement.average:.4f}s "
        f"min={measurement.minimum:.4f}s "
        f"max={measurement.maximum:.4f}s"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark kasane search latency")
    parser.add_argument("--query", required=True, help="Search query to benchmark")
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="Number of search results to request (default: 3)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Number of runs per benchmark target (default: 3)",
    )
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    measurements = [
        _measure(
            "cli search",
            args.runs,
            lambda: _run_cli_search(args.query, args.top_k, no_vector=False),
        ),
        _measure(
            "cli search --no-vector",
            args.runs,
            lambda: _run_cli_search(args.query, args.top_k, no_vector=True),
        ),
        _measure(
            "in-process hybrid_search",
            args.runs,
            lambda: _run_in_process_search(args.query, args.top_k, use_vector=True),
        ),
        _measure(
            "in-process hybrid_search use_vector=False",
            args.runs,
            lambda: _run_in_process_search(args.query, args.top_k, use_vector=False),
        ),
    ]

    print(f"query={args.query!r} top_k={args.top_k}")
    for measurement in measurements:
        _print_measurement(measurement)


if __name__ == "__main__":
    main()
