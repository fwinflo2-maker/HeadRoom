#!/usr/bin/env python3
"""Replay real IBM Bob sessions through baseline/token/cache simulations.

The Bob counterpart to claude_session_mode_benchmark.py, deliberately much
smaller. That harness models Anthropic's cache-prefix economics — TTL expiry,
cache-bust turns, separate cache-read/cache-write pricing. Bob bills every
token class at one flat rate with no cache discount, so none of that applies
and including it would only mislead: under flat pricing the only thing that
matters is how many tokens go over the wire.

Reads ~/.bob/db/bob.db directly, reconstructs the OpenAI-shaped chat payload
Bob would have sent on each turn, and runs it through Headroom's real
openai_pipeline in each mode. No network, no coins, no run-to-run variance —
so it can be re-run on every change, unlike a live A/B.

    python benchmarks/bob_session_mode_benchmark.py
    python benchmarks/bob_session_mode_benchmark.py --since 2026-08-27 --json out.json
    python benchmarks/bob_session_mode_benchmark.py --task c38d643f

What it does NOT measure: whether compression changes Bob's behaviour. A
replay reproduces the payload, not the decisions the model makes about it, so
turn inflation from degraded context is invisible here and needs live runs.
"""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".bob" / "db" / "bob.db"

# Bob's flat rate: input, cacheRead, cacheWrite and output all bill the same.
# Verified against ~/.bob/db/bob.db spend records with zero residual over 41
# turns, which is why this harness reports one number instead of four.
BOB_RATE_USD_PER_MTOK = 2.0

MODES = ("baseline", "token", "cache")


@dataclass
class BobTurn:
    """One reconstructed request: everything Bob would have posted upstream."""

    task_id: str
    seq: int
    model: str
    messages: list[dict[str, Any]]
    observed_context_tokens: int = 0


@dataclass
class TurnResult:
    task_id: str
    seq: int
    raw_tokens: int
    forwarded_tokens: int

    @property
    def saved(self) -> int:
        return self.raw_tokens - self.forwarded_tokens

    @property
    def saved_pct(self) -> float:
        return 100.0 * self.saved / self.raw_tokens if self.raw_tokens else 0.0


@dataclass
class ModeSummary:
    mode: str
    turns: list[TurnResult] = field(default_factory=list)

    @property
    def raw(self) -> int:
        return sum(t.raw_tokens for t in self.turns)

    @property
    def forwarded(self) -> int:
        return sum(t.forwarded_tokens for t in self.turns)

    @property
    def saved(self) -> int:
        return self.raw - self.forwarded

    @property
    def saved_pct(self) -> float:
        return 100.0 * self.saved / self.raw if self.raw else 0.0

    @property
    def cost_usd(self) -> float:
        return self.forwarded * BOB_RATE_USD_PER_MTOK / 1e6


def load_turns(
    db_path: Path,
    since: str | None = None,
    task_prefix: str | None = None,
    max_tasks: int | None = None,
) -> list[BobTurn]:
    """Reconstruct per-turn request payloads from Bob's message log.

    Bob stores one row per message rather than per request, so a turn's payload
    is the conversation *up to and including* that point — which is exactly the
    residency effect that makes long sessions expensive, and what compression
    has to act on.
    """
    if not db_path.exists():
        raise SystemExit(f"bob db not found: {db_path}")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    q = "select id, costs, created_at from tasks"
    where: list[str] = []
    params: list[str | int] = []
    if task_prefix:
        where.append("id like ?")
        params.append(task_prefix + "%")
    if since:
        try:
            cutoff = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise SystemExit(f"--since must be ISO date (YYYY-MM-DD), got {since!r}") from exc
        where.append("created_at > ?")
        params.append(int(cutoff.timestamp() * 1000))
    if where:
        q += " where " + " and ".join(where)
    q += " order by created_at"

    tasks = con.execute(q, params).fetchall()
    if max_tasks:
        tasks = tasks[-max_tasks:]

    turns: list[BobTurn] = []
    for t in tasks:
        try:
            costs = json.loads(t["costs"] or "{}")
        except (TypeError, ValueError):
            costs = {}
        rows = con.execute(
            "select role, data from messages where task_id=? order by created_at",
            (t["id"],),
        ).fetchall()

        convo: list[dict[str, Any]] = []
        seq = 0
        for r in rows:
            try:
                d = json.loads(r["data"])
            except (TypeError, ValueError):
                continue
            msg = {"role": r["role"], "content": d.get("content") or ""}
            # Tool calls and results carry the bulk of the payload; keep them
            # in the shape the OpenAI pipeline expects to see.
            if d.get("toolCalls"):
                msg["tool_calls"] = d["toolCalls"]
            convo.append(msg)
            # A request is sent whenever the assistant is about to speak, so
            # the payload is everything before that assistant message.
            if r["role"] == "assistant" and len(convo) > 1:
                seq += 1
                turns.append(
                    BobTurn(
                        task_id=t["id"],
                        seq=seq,
                        model=costs.get("model") or "gpt-4o",
                        messages=copy.deepcopy(convo[:-1]),
                        observed_context_tokens=costs.get("contextTokens", 0),
                    )
                )
    con.close()
    return turns


def build_proxy(mode: str) -> Any:
    """A proxy instance configured for one mode, with no network listener."""
    from headroom.proxy.models import ProxyConfig
    from headroom.proxy.server import HeadroomProxy

    cfg_kwargs: dict[str, Any] = {}
    try:
        cfg = ProxyConfig(mode=mode, **cfg_kwargs)
    except TypeError:
        # Older/newer ProxyConfig signatures: fall back to the default and set
        # the attribute directly rather than guessing at keyword names.
        cfg = ProxyConfig()
        cfg.mode = mode
    return HeadroomProxy(cfg)


def count_tokens(tokenizer: Any, messages: list[dict[str, Any]]) -> int:
    text = json.dumps(messages, separators=(",", ":"))
    try:
        return len(tokenizer.encode(text))
    except Exception:
        # The estimate only has to be consistent between arms; a tokenizer
        # that refuses one payload must not abort the whole run.
        return len(text) // 4


def replay(turns: list[BobTurn], mode: str, verbose: bool = False) -> ModeSummary:
    from headroom.tokenizers import get_tokenizer
    from headroom.utils import extract_user_query

    summary = ModeSummary(mode=mode)
    tokenizer = get_tokenizer("gpt-4o")

    if mode == "baseline":
        for t in turns:
            n = count_tokens(tokenizer, t.messages)
            summary.turns.append(TurnResult(t.task_id, t.seq, n, n))
        return summary

    proxy = build_proxy(mode)
    pipeline = proxy.openai_pipeline
    provider = proxy.openai_provider

    # Verify the treatment actually took. ProxyConfig silently accepts an
    # unknown mode, and a benchmark that reports a mode it did not run is
    # worse than no benchmark — this is the failure that invalidated a live
    # session earlier: nothing asserted the mode before the work started.
    effective = getattr(proxy.config, "mode", None)
    if effective != mode:
        raise SystemExit(f"mode not applied: asked for {mode!r}, proxy reports {effective!r}")

    for t in turns:
        raw = count_tokens(tokenizer, t.messages)
        try:
            limit = provider.get_context_limit(t.model)
        except Exception:
            limit = 128_000
        # Cache mode freezes every prior turn and mutates only the latest one
        # ("Prefix freeze: strict / Mutations: latest turn only"). Replaying it
        # with frozen_message_count=0 lets it rewrite the whole conversation and
        # over-credits it badly — it scored above token mode here while live
        # measurement had it at 0.30% against token's 11.32%. Token mode
        # re-freezes after compression, so 0 is the right floor there.
        frozen = max(len(t.messages) - 1, 0) if mode == "cache" else 0
        try:
            result = pipeline.apply(
                messages=copy.deepcopy(t.messages),
                model=t.model,
                model_limit=limit,
                context=extract_user_query(t.messages),
                frozen_message_count=frozen,
            )
            forwarded = count_tokens(tokenizer, result.messages)
        except Exception as exc:  # noqa: BLE001 - one bad turn must not kill the run
            if verbose:
                print(f"  ! {t.task_id[:8]}#{t.seq} {mode}: {exc}", file=sys.stderr)
            forwarded = raw
        summary.turns.append(TurnResult(t.task_id, t.seq, raw, forwarded))
    return summary


def scaling_curve(s: ModeSummary) -> list[tuple[str, int, int, float]]:
    """Savings bucketed by payload size.

    This is the load-bearing view. A live measurement showed cache mode saving
    a near-constant ~74 tokens whether the payload was 16K or 115K — so its
    percentage collapsed exactly as sessions got expensive — while token mode's
    savings grew with the payload. An aggregate percentage hides that.
    """
    buckets = [
        ("<10K", 0, 10_000),
        ("10-25K", 10_000, 25_000),
        ("25-50K", 25_000, 50_000),
        ("50K+", 50_000, 10**9),
    ]
    out = []
    for label, lo, hi in buckets:
        group = [t for t in s.turns if lo <= t.raw_tokens < hi]
        if not group:
            continue
        raw = sum(t.raw_tokens for t in group)
        saved = sum(t.saved for t in group)
        out.append((label, len(group), saved // len(group), 100.0 * saved / raw if raw else 0.0))
    return out


def report(summaries: dict[str, ModeSummary], turns: list[BobTurn]) -> None:
    tasks = len({t.task_id for t in turns})
    print(
        f"\nreplayed {len(turns)} turns from {tasks} Bob tasks "
        f"(flat ${BOB_RATE_USD_PER_MTOK}/Mtok, no cache discount)\n"
    )

    base = summaries["baseline"]
    print(
        f"{'mode':<10}{'forwarded tok':>15}{'saved':>12}{'saved %':>10}{'cost':>10}{'vs base':>10}"
    )
    for mode in MODES:
        s = summaries.get(mode)
        if not s:
            continue
        delta = base.cost_usd - s.cost_usd
        vs_base = f"-${delta:.2f}" if delta > 0 else "—"
        print(
            f"{mode:<10}{s.forwarded:>15,}{s.saved:>12,}{s.saved_pct:>9.2f}%"
            f"{s.cost_usd:>10.2f}{vs_base:>10}"
        )

    for mode in MODES:
        s = summaries.get(mode)
        if not s or mode == "baseline":
            continue
        curve = scaling_curve(s)
        if not curve:
            continue
        print(f"\n{mode} mode — does saving scale with payload?")
        print(f"  {'payload':<10}{'turns':>7}{'avg saved':>12}{'saved %':>10}")
        for label, n, avg, pct in curve:
            print(f"  {label:<10}{n:>7}{avg:>12,}{pct:>9.2f}%")

    for mode in MODES:
        s = summaries.get(mode)
        if not s or mode == "baseline" or not s.turns:
            continue
        pcts = sorted(t.saved_pct for t in s.turns)
        print(
            f"\n{mode} per-turn spread: median {statistics.median(pcts):.2f}%  "
            f"p10 {pcts[len(pcts) // 10]:.2f}%  p90 {pcts[9 * len(pcts) // 10]:.2f}%"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--since", help="ISO date; only tasks created after it")
    ap.add_argument("--task", help="task id prefix, e.g. c38d643f")
    ap.add_argument("--max-tasks", type=int, help="keep only the N most recent")
    ap.add_argument("--modes", default=",".join(MODES))
    ap.add_argument("--json", type=Path, help="write full per-turn results here")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    turns = load_turns(args.db, since=args.since, task_prefix=args.task, max_tasks=args.max_tasks)
    if not turns:
        print("no turns matched", file=sys.stderr)
        return 1

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if "baseline" not in modes:
        modes.insert(0, "baseline")

    summaries: dict[str, ModeSummary] = {}
    for mode in modes:
        if args.verbose:
            print(f"replaying {len(turns)} turns in {mode} mode…", file=sys.stderr)
        summaries[mode] = replay(turns, mode, verbose=args.verbose)

    report(summaries, turns)

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "rate_usd_per_mtok": BOB_RATE_USD_PER_MTOK,
                    "turns": len(turns),
                    "tasks": len({t.task_id for t in turns}),
                    "modes": {
                        m: {
                            "raw_tokens": s.raw,
                            "forwarded_tokens": s.forwarded,
                            "saved_tokens": s.saved,
                            "saved_pct": s.saved_pct,
                            "cost_usd": s.cost_usd,
                            "turns": [
                                {
                                    "task": t.task_id,
                                    "seq": t.seq,
                                    "raw": t.raw_tokens,
                                    "forwarded": t.forwarded_tokens,
                                    "saved_pct": t.saved_pct,
                                }
                                for t in s.turns
                            ],
                        }
                        for m, s in summaries.items()
                    },
                },
                indent=2,
            )
        )
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
