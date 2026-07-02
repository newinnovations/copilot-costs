#!/usr/bin/env python3

"""
Estimate the cost of all Copilot IDE sessions found in ~/.copilot/session-state.

For each session we read events.jsonl, take the modelMetrics from the last
session.shutdown event (they are cumulative for the session), and:
  * Compute an estimated cost using the GitHub Copilot post-June token pricing
    (1 AI credit == $0.01 USD).
  * Also surface the actually-charged AI credits when the session recorded a
    `totalNanoAiu` field (nano-AIU / 1e9 == AI credits == AIC).

We print summaries per session, per month and a grand total.
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# -- Pricing (USD per 1M tokens). Source:
# https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing
# Each entry: input, cached_input, output, cache_write (Anthropic only).
PRICING: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-5-mini": {"in": 0.25, "cache": 0.025, "out": 2.00},
    "gpt-5.3-codex": {"in": 1.75, "cache": 0.175, "out": 14.00},
    "gpt-5.4": {"in": 2.50, "cache": 0.25, "out": 15.00},
    "gpt-5.4-mini": {"in": 0.75, "cache": 0.075, "out": 4.50},
    "gpt-5.4-nano": {"in": 0.20, "cache": 0.02, "out": 1.25},
    "gpt-5.5": {"in": 5.00, "cache": 0.50, "out": 30.00},
    # Anthropic
    "claude-haiku-4.5": {"in": 1.00, "cache": 0.10, "cw": 1.25, "out": 5.00},
    "claude-sonnet-4": {"in": 3.00, "cache": 0.30, "cw": 3.75, "out": 15.00},
    "claude-sonnet-4.5": {"in": 3.00, "cache": 0.30, "cw": 3.75, "out": 15.00},
    "claude-sonnet-4.6": {"in": 3.00, "cache": 0.30, "cw": 3.75, "out": 15.00},
    "claude-sonnet-5": {"in": 2.00, "cache": 0.20, "cw": 2.50, "out": 10.00},
    "claude-opus-4.5": {"in": 5.00, "cache": 0.50, "cw": 6.25, "out": 25.00},
    "claude-opus-4.6": {"in": 5.00, "cache": 0.50, "cw": 6.25, "out": 25.00},
    "claude-opus-4.7": {"in": 5.00, "cache": 0.50, "cw": 6.25, "out": 25.00},
    "claude-opus-4.8": {"in": 5.00, "cache": 0.50, "cw": 6.25, "out": 25.00},
    # Google
    "gemini-2.5-pro": {"in": 1.25, "cache": 0.125, "out": 10.00},
    "gemini-3-flash": {"in": 0.50, "cache": 0.05, "out": 3.00},
    "gemini-3.1-pro": {"in": 2.00, "cache": 0.20, "out": 12.00},
    "gemini-3.5-flash": {"in": 1.50, "cache": 0.15, "out": 9.00},
}


def token_cost_usd(model: str, usage: dict) -> float | None:
    """Return estimated USD cost for a model's aggregated token usage."""
    p = PRICING.get(model)
    if not p:
        return None
    inp = usage.get("inputTokens", 0)
    out = usage.get("outputTokens", 0)
    cr = usage.get("cacheReadTokens", 0)
    cw = usage.get("cacheWriteTokens", 0)
    # 'inputTokens' in the metrics is the *non-cached* input; cached read is
    # billed separately at the cached rate, cache-write at the cw rate.
    inp -= cr + cw
    if inp < 0:
        inp = 0
    cost = (inp * p["in"] + out * p["out"] + cr * p["cache"]) / 1_000_000
    if "cw" in p:
        cost += cw * p["cw"] / 1_000_000
    return cost


@dataclass
class ModelStats:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    requests: int = 0
    nano_aiu: int = 0  # actual charged AI credits (in nano units)

    def add(self, other: "ModelStats") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        self.requests += other.requests
        self.nano_aiu += other.nano_aiu


@dataclass
class SessionStats:
    session_id: str
    started: datetime | None = None
    models: dict[str, ModelStats] = field(
        default_factory=lambda: defaultdict(ModelStats)
    )

    @property
    def month(self) -> str:
        if not self.started:
            return "unknown"
        return self.started.strftime("%Y-%m")

    def est_usd(self) -> float:
        total = 0.0
        for m, s in self.models.items():
            c = token_cost_usd(
                m,
                {
                    "inputTokens": s.input,
                    "outputTokens": s.output,
                    "cacheReadTokens": s.cache_read,
                    "cacheWriteTokens": s.cache_write,
                },
            )
            if c is not None:
                total += c
        return total

    def est_aic(self) -> float:
        return self.est_usd() * 100.0  # 1 AIC = $0.01

    def actual_aic(self) -> float:
        return sum(m.nano_aiu for m in self.models.values()) / 1e9

    def has_actual(self) -> bool:
        return any(m.nano_aiu > 0 for m in self.models.values())

    def unpriced_models(self) -> list[str]:
        return [m for m in self.models if m not in PRICING]


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_session(events_path: Path) -> SessionStats | None:
    st = SessionStats(session_id=events_path.parent.name)
    last_shutdown = None
    first_ts: datetime | None = None
    with events_path.open() as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = parse_ts(ev.get("timestamp"))
            if ts and first_ts is None:
                first_ts = ts
            if ev.get("type") == "session.shutdown":
                last_shutdown = ev
    if not last_shutdown:
        return None
    d = last_shutdown.get("data", {})
    start_ms = d.get("sessionStartTime")
    if isinstance(start_ms, (int, float)):
        st.started = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    else:
        st.started = first_ts
    for model, mm in d.get("modelMetrics", {}).items():
        usage = mm.get("usage", {})
        req = mm.get("requests", {})
        s = st.models[model]
        s.input = usage.get("inputTokens", 0)
        s.output = usage.get("outputTokens", 0)
        s.cache_read = usage.get("cacheReadTokens", 0)
        s.cache_write = usage.get("cacheWriteTokens", 0)
        s.reasoning = usage.get("reasoningTokens", 0)
        s.requests = req.get("count", 0)
        s.nano_aiu = mm.get("totalNanoAiu", 0)
    return st


def fmt_int(n: int | float) -> str:
    return f"{int(n):>12,}"


def header(title: str, length: int) -> None:
    print()
    print()
    print(f"=== {title} {'=' * (length - len(title) - 5)}")
    print()


def print_session_table(sessions: list[SessionStats]) -> None:
    header("Per-session breakdown", 159)
    hdr = f"{'Date':10}  {'Session':36}  {'Model':20}  {'Reqs':>5}  {'Input':>12}  {'Output':>12}  {'CacheR':>12}  {'CacheW':>12}  {'est AIC':>11}  {'actual AIC':>11}"
    print(hdr)
    print("-" * len(hdr))
    for sess in sessions:
        date = sess.started.strftime("%Y-%m-%d") if sess.started else "unknown   "
        for model, s in sorted(sess.models.items()):
            est = token_cost_usd(
                model,
                {
                    "inputTokens": s.input,
                    "outputTokens": s.output,
                    "cacheReadTokens": s.cache_read,
                    "cacheWriteTokens": s.cache_write,
                },
            )
            est_aic = f"{est * 100:11.3f}" if est is not None else "       n/a "
            act_aic = f"{s.nano_aiu / 1e9:11.3f}" if s.nano_aiu else "        -  "
            print(
                f"{date:10}  {sess.session_id:36}  {model:20}  {s.requests:>5}  "
                f"{fmt_int(s.input)}  {fmt_int(s.output)}  {fmt_int(s.cache_read)}  "
                f"{fmt_int(s.cache_write)}  {est_aic}  {act_aic}"
            )


def print_monthly(sessions: list[SessionStats]) -> None:
    per_month: dict[str, dict] = defaultdict(
        lambda: {
            "sessions": 0,
            "models": defaultdict(ModelStats),
        }
    )
    for s in sessions:
        m = s.month
        per_month[m]["sessions"] += 1
        for model, stats in s.models.items():
            per_month[m]["models"][model].add(stats)

    header("Monthly summary", 129)
    hdr = f"{'Month':8}  {'Sess':>5}  {'Model':20}  {'Reqs':>6}  {'Input':>12}  {'Output':>12}  {'CacheR':>12}  {'CacheW':>12}  {'est AIC':>12}  {'actual AIC':>12}"
    print(hdr)
    print("-" * len(hdr))
    for month in sorted(per_month):
        info = per_month[month]
        m_est = m_act = 0.0
        for model, s in sorted(info["models"].items()):
            est = token_cost_usd(
                model,
                {
                    "inputTokens": s.input,
                    "outputTokens": s.output,
                    "cacheReadTokens": s.cache_read,
                    "cacheWriteTokens": s.cache_write,
                },
            )
            est_aic = (est or 0) * 100
            act_aic = s.nano_aiu / 1e9
            m_est += est_aic
            m_act += act_aic
            print(
                f"{month:8}  {info['sessions']:>5}  {model:20}  {s.requests:>6}  "
                f"{fmt_int(s.input)}  {fmt_int(s.output)}  {fmt_int(s.cache_read)}  "
                f"{fmt_int(s.cache_write)}  {est_aic:12.3f}  "
                f"{act_aic:12.3f}"
            )
        print(f"{'-' * 33:>129}")
        print(
            f"{'TOTAL':>101}  {m_est:12.3f}  {m_act:12.3f}   (${m_est / 100:.2f} est / ${m_act / 100:.2f} actual)"
        )
        print()


def print_totals(sessions: list[SessionStats]) -> None:
    totals: dict[str, ModelStats] = defaultdict(ModelStats)
    for s in sessions:
        for model, stats in s.models.items():
            totals[model].add(stats)

    header("Grand totals by model", 116)
    hdr = f"{'Model':20}  {'Reqs':>6}  {'Input':>13}  {'Output':>13}  {'CacheR':>13}  {'CacheW':>13}  {'est AIC':>12}  {'actual AIC':>12}"
    print(hdr)
    print("-" * len(hdr))
    total_est = total_act = 0.0
    for model, s in sorted(totals.items()):
        est = token_cost_usd(
            model,
            {
                "inputTokens": s.input,
                "outputTokens": s.output,
                "cacheReadTokens": s.cache_read,
                "cacheWriteTokens": s.cache_write,
            },
        )
        est_aic = (est or 0) * 100
        act_aic = s.nano_aiu / 1e9
        total_est += est_aic
        total_act += act_aic
        est_str = f"{est_aic:12.3f}" if est is not None else "       n/a  "
        print(
            f"{model:20}  {s.requests:>6}  {int(s.input):>13,}  {int(s.output):>13,}  "
            f"{int(s.cache_read):>13,}  {int(s.cache_write):>13,}  "
            f"{est_str}  {act_aic:12.3f}"
        )
    print("-" * len(hdr))
    print(f"{'TOTAL':>88}  {total_est:12.3f}  {total_act:12.3f}")

    header("Summary", 107)
    print(f"Sessions analysed: {len(sessions)}")
    print(
        f"Estimated cost (post-June pricing):  {total_est:>10,.2f} AIC  = ${total_est / 100:>8,.2f} USD"
    )
    print(
        f"Actual charged (nano-AIU/1e9):       {total_act:>10,.2f} AIC  = ${total_act / 100:>8,.2f} USD"
        f"   (only for sessions that recorded it)"
    )


def main() -> int:
    root = Path(os.path.expanduser("~/.copilot/session-state"))
    if not root.is_dir():
        print(f"No session-state directory at {root}", file=sys.stderr)
        return 1

    sessions: list[SessionStats] = []
    unpriced: set[str] = set()
    for events in sorted(root.glob("*/events.jsonl")):
        s = load_session(events)
        if s is None:
            continue
        sessions.append(s)
        unpriced.update(s.unpriced_models())

    sessions.sort(
        key=lambda s: (s.started or datetime.min.replace(tzinfo=timezone.utc))
    )

    print()
    print(
        "GitHub Copilot CLI cost estimation  --  (c) 2026 Martin van der Werff  (github at newinnovations.nl)"
    )

    print_session_table(sessions)
    print_monthly(sessions)
    print_totals(sessions)

    if unpriced:
        print()
        print(
            "Note: no pricing entry for these models (estimated cost = 0):",
            ", ".join(sorted(unpriced)),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
