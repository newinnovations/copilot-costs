#!/usr/bin/env python3

"""
Estimate the cost of local GitHub Copilot sessions.

By default this reads Copilot CLI sessions from ~/.copilot/session-state.
Pass --source vscode (or --vscode) to read GitHub Copilot Chat sessions saved by
Visual Studio Code instead.

For Copilot CLI sessions we read events.jsonl, take the modelMetrics from the
last session.shutdown event (they are cumulative for the session), and:
  * Compute an estimated cost using the GitHub Copilot post-June token pricing
    (1 AI credit == $0.01 USD).
  * Also surface the actually-charged AI credits when the session recorded a
    `totalNanoAiu` field (nano-AIU / 1e9 == AI credits == AIC).

For VS Code sessions we read persisted chat transcript content from
workspaceStorage/globalStorage. VS Code does not save Copilot's server-side
token metrics, so token counts are approximate and based only on local transcript
content (user text, attachments, visible assistant text, reasoning and tool
payloads).

We print summaries per session, per month and a grand total.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
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


def pricing_key(model: str) -> str | None:
    """Return the key used by PRICING for a model identifier."""
    candidates = [model]
    if "/" in model:
        candidates.append(model.split("/", 1)[1])
    for candidate in candidates:
        normalized = candidate.strip()
        # Auto-routed VS Code sessions can persist release-suffixed backend
        # names, e.g. claude-sonnet-4.6-2026-03-05 or gpt-5.4-20260305.
        for suffix_len in (11, 9):
            if (
                len(normalized) > suffix_len
                and normalized[-suffix_len:].replace("-", "").isdigit()
            ):
                normalized = normalized[:-suffix_len]
                break
        if normalized in PRICING:
            return normalized
    return None


def token_cost_usd(model: str, usage: dict) -> float | None:
    """Return estimated USD cost for a model's aggregated token usage."""
    key = pricing_key(model)
    if not key:
        return None
    p = PRICING.get(key)
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
    summary: str | None = None
    started: datetime | None = None
    source: str = "cli"
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
        return [m for m in self.models if pricing_key(m) is None]


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read_workspace_summary(session_dir: Path) -> str | None:
    """Return the session label shown by `copilot resume`, when available."""
    workspace = session_dir / "workspace.yaml"
    if not workspace.exists():
        return None

    fields: dict[str, str] = {}
    try:
        with workspace.open() as fh:
            for line in fh:
                key, sep, value = line.partition(":")
                if not sep:
                    continue
                key = key.strip()
                if key in {"summary", "name"}:
                    fields[key] = value.strip().strip("'\"")
    except OSError:
        return None

    return fields.get("summary") or fields.get("name")


def fixed_width(text: str, width: int) -> str:
    text = " ".join(text.split())
    if len(text) > width:
        return text[: width - 3] + "..."
    return f"{text:{width}}"


def load_cli_session(events_path: Path) -> SessionStats | None:
    st = SessionStats(
        session_id=events_path.parent.name,
        summary=read_workspace_summary(events_path.parent),
        source="cli",
    )
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


def vscode_workspace_storage_candidates() -> list[Path]:
    """Return common VS Code workspaceStorage roots for this platform."""
    home = Path.home()
    candidates: list[Path] = []
    system = platform.system()
    if system == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        candidates.extend(
            [
                appdata / "Code" / "User" / "workspaceStorage",
                appdata / "Code - Insiders" / "User" / "workspaceStorage",
            ]
        )
    elif system == "Darwin":
        candidates.extend(
            [
                home
                / "Library"
                / "Application Support"
                / "Code"
                / "User"
                / "workspaceStorage",
                home
                / "Library"
                / "Application Support"
                / "Code - Insiders"
                / "User"
                / "workspaceStorage",
            ]
        )
    else:
        # Prefer server/remote installs before desktop Code because this script
        # is often run inside dev containers or remote shells.
        candidates.extend(
            [
                home / ".vscode-server" / "data" / "User" / "workspaceStorage",
                home / ".vscode-remote" / "data" / "User" / "workspaceStorage",
                home / ".vscode" / "data" / "User" / "workspaceStorage",
                home / ".config" / "Code" / "User" / "workspaceStorage",
                home / ".config" / "Code - Insiders" / "User" / "workspaceStorage",
            ]
        )
    return candidates


def autodetect_vscode_workspace_storage() -> Path | None:
    for candidate in vscode_workspace_storage_candidates():
        if candidate.is_dir():
            return candidate
    return None


def normalize_title(text: str) -> str:
    first = text.splitlines()[0] if text else ""
    marker = first.find(". ")
    if marker >= 0:
        first = first[: marker + 1]
    return " ".join(first.split())


def make_title(text: str, max_len: int = 60) -> str:
    title = normalize_title(text)
    if len(title) <= max_len:
        return title
    cut = title.rfind(" ", 0, max_len)
    if cut < max_len // 2:
        cut = max_len
    return title[:cut] + "..."


def approx_tokens(parts: list[str]) -> int:
    """Approximate persisted transcript tokens without third-party tokenizers."""
    if not parts:
        return 0
    text = "\n".join(p for p in parts if p)
    if not text:
        return 0
    # A dependency-free approximation. It intentionally counts persisted local
    # content only; hidden Copilot prompts/context are not present in VS Code's
    # saved chat files.
    return max(1, (len(text) + 3) // 4)


def find_resolved_model(value) -> str:
    if isinstance(value, dict):
        resolved = value.get("resolvedModel")
        if isinstance(resolved, str) and resolved.strip():
            return resolved.strip()
        for nested in value.values():
            found = find_resolved_model(nested)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_resolved_model(item)
            if found:
                return found
    return ""


def normalize_resolved_model_name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    name = value.strip()
    for suffix_len in (11, 9):
        if len(name) > suffix_len and name[-suffix_len:].replace("-", "").isdigit():
            return name[:-suffix_len]
    return name


def effective_model_id(model_id, resolved_model=None) -> str:
    model = (
        model_id.strip()
        if isinstance(model_id, str) and model_id.strip()
        else "(unknown)"
    )
    if model != "copilot/auto":
        return model
    resolved = normalize_resolved_model_name(resolved_model)
    if resolved:
        return f"copilot-auto/{resolved}"
    return model


def effective_request_model_id(request: dict) -> str:
    return effective_model_id(
        request.get("modelId"), find_resolved_model(request.get("result"))
    )


def vscode_report_model_id(model_id: str) -> str:
    """Return the model name shown for VS Code sessions."""
    id = model_id.split("/", 1)[1] if "/" in model_id else model_id
    id = id.replace("-4-", "-4.")
    return id


def add_vscode_request(
    st: SessionStats, request: dict, extra_response_parts: list, model_id: str
) -> None:
    user_parts: list[str] = []
    message_text = request.get("message", {}).get("text")
    if isinstance(message_text, str) and message_text:
        user_parts.append(message_text)
    variables = request.get("variableData", {}).get("variables")
    if isinstance(variables, list) and variables:
        user_parts.append(json.dumps(variables, separators=(",", ":")))

    assistant_parts: list[str] = []
    response_parts = []
    if isinstance(request.get("response"), list):
        response_parts.extend(request["response"])
    response_parts.extend(extra_response_parts or [])
    for part in response_parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind")
        value = part.get("value")
        content = part.get("content")
        if kind == "thinking" and isinstance(value, str) and value:
            assistant_parts.append(value)
        elif kind == "toolInvocationSerialized":
            assistant_parts.append(json.dumps(part, separators=(",", ":")))
        elif isinstance(value, str) and value:
            assistant_parts.append(value)
        elif isinstance(content, str) and content:
            assistant_parts.append(content)
        elif kind is not None:
            serialized = json.dumps(part, separators=(",", ":"))
            if len(serialized) > 2:
                assistant_parts.append(serialized)

    s = st.models[vscode_report_model_id(model_id)]
    s.input += approx_tokens(user_parts)
    s.output += approx_tokens(assistant_parts)
    s.requests += 1


def load_vscode_flat_session(path: Path) -> SessionStats | None:
    with path.open() as fh:
        data = json.load(fh)
    st = SessionStats(
        session_id=data.get("sessionId") or path.stem,
        summary=normalize_title(data.get("customTitle", "")) or None,
        source="vscode",
    )
    if isinstance(data.get("creationDate"), (int, float)):
        st.started = datetime.fromtimestamp(
            data["creationDate"] / 1000, tz=timezone.utc
        )
    requests = data.get("requests") if isinstance(data.get("requests"), list) else []
    for request in requests:
        if isinstance(request, dict):
            add_vscode_request(st, request, [], effective_request_model_id(request))
    if not st.models:
        return None
    if not st.summary:
        first_text = (
            requests[0].get("message", {}).get("text")
            if requests and isinstance(requests[0], dict)
            else ""
        )
        if isinstance(first_text, str):
            st.summary = normalize_title(first_text)
    return st


def load_vscode_log_session(path: Path) -> SessionStats | None:
    st = SessionStats(session_id=path.stem, source="vscode")
    requests: list[dict] = []
    extra_response_parts: dict[int, list] = defaultdict(list)
    request_models: dict[int, str] = {}

    with path.open() as fh:
        for line_number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Failed to parse JSON in {path} at line {line_number}: {e}"
                ) from e
            kind = entry.get("kind")
            key = entry.get("k")
            value = entry.get("v")

            if kind == 0 and isinstance(value, dict):
                if isinstance(value.get("creationDate"), (int, float)):
                    st.started = datetime.fromtimestamp(
                        value["creationDate"] / 1000, tz=timezone.utc
                    )
                if isinstance(value.get("sessionId"), str) and value["sessionId"]:
                    st.session_id = value["sessionId"]
                base_requests = (
                    value.get("requests", [])
                    if isinstance(value.get("requests"), list)
                    else []
                )
                for offset, request in enumerate(base_requests):
                    if isinstance(request, dict):
                        request_models[len(requests) + offset] = (
                            effective_request_model_id(request)
                        )
                requests.extend(r for r in base_requests if isinstance(r, dict))
                continue

            if kind == 1:
                if (
                    isinstance(key, list)
                    and len(key) == 3
                    and key[0] == "requests"
                    and isinstance(key[1], int)
                    and key[2] == "result"
                    and request_models.get(key[1]) == "copilot/auto"
                ):
                    request_models[key[1]] = effective_model_id(
                        "copilot/auto", find_resolved_model(value)
                    )
                if (
                    isinstance(key, list)
                    and key == ["customTitle"]
                    and isinstance(value, str)
                    and value.strip()
                ):
                    st.summary = normalize_title(value)
                continue

            if kind == 2 and isinstance(key, list) and isinstance(value, list):
                if key == ["requests"]:
                    for offset, request in enumerate(value):
                        if isinstance(request, dict):
                            request_models[len(requests) + offset] = (
                                effective_request_model_id(request)
                            )
                    requests.extend(r for r in value if isinstance(r, dict))
                elif (
                    len(key) == 3
                    and key[0] == "requests"
                    and isinstance(key[1], int)
                    and key[2] == "response"
                ):
                    extra_response_parts[key[1]].extend(value)

    for index, request in enumerate(requests):
        add_vscode_request(
            st,
            request,
            extra_response_parts.get(index, []),
            request_models.get(index) or effective_request_model_id(request),
        )

    if not st.models:
        return None
    if not st.summary:
        first_text = requests[0].get("message", {}).get("text") if requests else ""
        if isinstance(first_text, str):
            st.summary = normalize_title(first_text)
    return st


def load_vscode_legacy_transcript(path: Path) -> SessionStats | None:
    st = SessionStats(session_id=path.stem, source="vscode")
    model = "copilot/vscode-transcript"
    with path.open() as fh:
        for line_number, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(
                    f"Failed to parse JSON in {path} at line {line_number}: {e}"
                ) from e
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            if event.get("type") == "session.start" and st.started is None:
                st.started = parse_ts(data.get("startTime"))
                continue
            if event.get("type") == "user.message":
                parts = []
                content = data.get("content")
                if isinstance(content, str) and content:
                    parts.append(content)
                    if not st.summary:
                        st.summary = make_title(content)
                attachments = data.get("attachments")
                if isinstance(attachments, list) and attachments:
                    parts.append(json.dumps(attachments, separators=(",", ":")))
                st.models[model].input += approx_tokens(parts)
                continue
            if event.get("type") == "assistant.message":
                parts = []
                for key in ("content", "reasoningText"):
                    value = data.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
                tool_requests = data.get("toolRequests")
                if isinstance(tool_requests, list) and tool_requests:
                    parts.append(json.dumps(tool_requests, separators=(",", ":")))
                st.models[model].output += approx_tokens(parts)
                st.models[model].requests += 1
    return st if st.models else None


def discover_vscode_session_files(
    workspace_storage: Path, global_storage: Path | None
) -> list[tuple[Path, str]]:
    if not workspace_storage.is_dir():
        raise FileNotFoundError(
            f"workspaceStorage directory not found: {workspace_storage}"
        )
    by_session_id: dict[str, tuple[Path, str]] = {}

    def add_files(directory: Path, suffix: str, fmt: str) -> None:
        if not directory.is_dir():
            return
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.endswith(suffix):
                by_session_id[entry.name[: -len(suffix)]] = (entry, fmt)

    for workspace_dir in sorted(p for p in workspace_storage.iterdir() if p.is_dir()):
        add_files(
            workspace_dir / "GitHub.copilot-chat" / "transcripts", ".jsonl", "legacy"
        )
    for workspace_dir in sorted(p for p in workspace_storage.iterdir() if p.is_dir()):
        add_files(workspace_dir / "chatSessions", ".json", "new-json")
    for workspace_dir in sorted(p for p in workspace_storage.iterdir() if p.is_dir()):
        add_files(workspace_dir / "chatSessions", ".jsonl", "new-jsonl")

    if global_storage:
        empty_window = global_storage / "emptyWindowChatSessions"
        add_files(empty_window, ".json", "new-json")
        add_files(empty_window, ".jsonl", "new-jsonl")

    return sorted(by_session_id.values(), key=lambda item: str(item[0]))


def load_vscode_sessions(
    workspace_storage: Path, global_storage: Path | None
) -> list[SessionStats]:
    sessions: list[SessionStats] = []
    for path, fmt in discover_vscode_session_files(workspace_storage, global_storage):
        if fmt == "legacy":
            session = load_vscode_legacy_transcript(path)
        elif fmt == "new-json":
            session = load_vscode_flat_session(path)
        else:
            session = load_vscode_log_session(path)
        if session:
            sessions.append(session)
    return sessions


def fmt_int(n: int | float) -> str:
    return f"{int(n):>12,}"


def header(title: str, length: int) -> None:
    print()
    print()
    print(f"=== {title} {'=' * (length - len(title) - 5)}")
    print()


def print_session_table(sessions: list[SessionStats]) -> None:
    header("Per-session breakdown", 159)
    hdr = f"{'Date':10}  {'Summary':36}  {'Model':20}  {'Reqs':>5}  {'Input':>12}  {'Output':>12}  {'CacheR':>12}  {'CacheW':>12}  {'est AIC':>11}  {'actual AIC':>11}"
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
            est_aic = f"{est * 100:11.2f}" if est is not None else "       n/a "
            act_aic = f"{s.nano_aiu / 1e9:11.2f}" if s.nano_aiu else "        -  "
            summary = fixed_width(sess.summary or sess.session_id, 36)
            print(
                f"{date:10}  {summary}  {model:20}  {s.requests:>5}  "
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
                f"{fmt_int(s.cache_write)}  {est_aic:12.2f}  "
                f"{act_aic:12.2f}"
            )
        print(f"{'-' * 33:>129}")
        print(
            f"{'TOTAL':>101}  {m_est:12.2f}  {m_act:12.2f}   (${m_est / 100:.2f} est / ${m_act / 100:.2f} actual)"
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
        est_str = f"{est_aic:12.2f}" if est is not None else "        n/a "
        print(
            f"{model:20}  {s.requests:>6}  {int(s.input):>13,}  {int(s.output):>13,}  "
            f"{int(s.cache_read):>13,}  {int(s.cache_write):>13,}  "
            f"{est_str}  {act_aic:12.2f}"
        )
    print("-" * len(hdr))
    print(f"{'TOTAL':>88}  {total_est:12.2f}  {total_act:12.2f}")

    header("Summary", 107)
    print(f"Sessions analysed: {len(sessions)}")
    print(
        f"Estimated cost (post-June pricing):  {total_est:>10,.2f} AIC  = ${total_est / 100:>8,.2f} USD"
    )
    print(
        f"Actual charged (nano-AIU/1e9):       {total_act:>10,.2f} AIC  = ${total_act / 100:>8,.2f} USD"
        f"   (only for sessions that recorded it)"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Estimate local GitHub Copilot token usage and AI-credit cost."
    )
    parser.add_argument(
        "--source",
        choices=("cli", "vscode", "all"),
        default="cli",
        help="session source to scan (default: cli)",
    )
    parser.add_argument(
        "--vscode",
        action="store_true",
        help="shortcut for --source vscode",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="shortcut for --source all",
    )
    parser.add_argument(
        "--cli-root",
        type=Path,
        default=Path(os.path.expanduser("~/.copilot/session-state")),
        help="Copilot CLI session-state directory",
    )
    parser.add_argument(
        "--vscode-user-dir",
        type=Path,
        help="VS Code User directory; derives workspaceStorage and globalStorage",
    )
    parser.add_argument(
        "--vscode-user",
        help="Windows username shortcut for /mnt/c/Users/<username>/AppData/Roaming/Code/User",
    )
    parser.add_argument(
        "--vscode-workspace-storage",
        type=Path,
        help="VS Code workspaceStorage directory",
    )
    parser.add_argument(
        "--vscode-global-storage",
        type=Path,
        help="VS Code globalStorage directory",
    )
    args = parser.parse_args(argv)
    if args.vscode:
        args.source = "vscode"
    if args.all:
        args.source = "all"
    return args


def resolve_vscode_roots(args: argparse.Namespace) -> tuple[Path | None, Path | None]:
    if args.vscode_user:
        user_dir = (
            Path("/mnt/c/Users")
            / args.vscode_user
            / "AppData"
            / "Roaming"
            / "Code"
            / "User"
        )
        return user_dir / "workspaceStorage", user_dir / "globalStorage"
    if args.vscode_user_dir:
        user_dir = args.vscode_user_dir.expanduser()
        return user_dir / "workspaceStorage", user_dir / "globalStorage"
    workspace_storage = (
        args.vscode_workspace_storage.expanduser()
        if args.vscode_workspace_storage
        else None
    )
    if workspace_storage is None:
        workspace_storage = autodetect_vscode_workspace_storage()
    if workspace_storage is None:
        return None, None
    global_storage = (
        args.vscode_global_storage.expanduser()
        if args.vscode_global_storage
        else workspace_storage.parent / "globalStorage"
    )
    return workspace_storage, global_storage


def load_cli_sessions(root: Path) -> list[SessionStats]:
    if not root.is_dir():
        raise FileNotFoundError(f"No session-state directory at {root}")
    sessions: list[SessionStats] = []
    for events in sorted(root.glob("*/events.jsonl")):
        session = load_cli_session(events)
        if session is not None:
            sessions.append(session)
    return sessions


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    sessions: list[SessionStats] = []
    try:
        if args.source in {"cli", "all"}:
            sessions.extend(load_cli_sessions(args.cli_root.expanduser()))
        if args.source in {"vscode", "all"}:
            workspace_storage, global_storage = resolve_vscode_roots(args)
            if workspace_storage is None:
                checked = "\n  ".join(
                    str(p) for p in vscode_workspace_storage_candidates()
                )
                print(
                    "No VS Code workspaceStorage directory found. Checked:\n  "
                    f"{checked}\nPass --vscode-user-dir or --vscode-workspace-storage to override.",
                    file=sys.stderr,
                )
                return 1
            sessions.extend(load_vscode_sessions(workspace_storage, global_storage))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as e:
        print(e, file=sys.stderr)
        return 1

    unpriced: set[str] = set()
    for session in sessions:
        unpriced.update(session.unpriced_models())

    sessions.sort(
        key=lambda s: (
            s.started or datetime.min.replace(tzinfo=timezone.utc),
            s.source,
            s.session_id,
        )
    )

    print()
    label = {
        "cli": "GitHub Copilot CLI",
        "vscode": "GitHub Copilot VS Code Chat",
        "all": "GitHub Copilot local",
    }[args.source]
    print(
        f"{label} cost estimation  --  (c) 2026 Martin van der Werff  (github at newinnovations.nl)"
    )
    if args.source in {"vscode", "all"}:
        print(
            "VS Code token counts are approximate and include only content persisted in local chat session files."
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
