#!/usr/bin/env python3
"""
PostToolBatch hook for the /queue skill v2 — low-noise activity + phase updates.

Fires once per batch of parallel tool calls (Claude Code >= 2.0; payload field
tool_results). Also tolerates the older per-call PostToolUse payload shape
(tool_name/tool_input at top level) so the hook works under either event.

Completely quiet: never writes to stdout. Records the recent tool mix in
state.json so the tracker can show a conservative phase label (INVESTIGATING /
BUILDING / VERIFYING) and a short last_activity string for the active task.
Also reconciles queue.md whenever the file has changed, so timestamps are set
promptly rather than only at Stop.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_lib as ql  # noqa: E402

READ_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "Agent", "Task", "ToolSearch"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
VERIFY_RE = re.compile(
    r"\b(pytest|unittest|py_compile|npm test|pnpm test|vitest|jest|tsc|eslint|biome|"
    r"lint|build|mypy|ruff|cargo test|go test|make test|check)\b", re.IGNORECASE)


def classify(tool: str, tool_input: dict) -> str:
    if tool in WRITE_TOOLS:
        return "build"
    if tool in READ_TOOLS:
        return "read"
    if tool == "Bash":
        cmd = str(tool_input.get("command", ""))
        return "verify" if VERIFY_RE.search(cmd) else "build"
    return "read"


def phase_from_window(window: list[str]) -> str:
    recent = window[-5:]
    if not recent:
        return "PLANNING"
    counts = {k: recent.count(k) for k in ("read", "build", "verify")}
    if counts["verify"] and counts["verify"] >= counts["build"]:
        return "VERIFYING"
    if counts["build"] >= counts["read"]:
        return "BUILDING"
    return "INVESTIGATING"


def activity_line(tool: str, tool_input: dict) -> str:
    if tool == "Bash":
        d = str(tool_input.get("description") or tool_input.get("command", ""))
        return ("$ " + " ".join(d.split()))[:80]
    target = tool_input.get("file_path") or tool_input.get("pattern") or tool_input.get("query") or ""
    if isinstance(target, str) and target:
        return f"{tool} {Path(target).name if '/' in target else target}"[:80]
    return tool[:80]


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0
    cwd = Path(event.get("cwd") or ".").expanduser().resolve()
    tasks_file = cwd / "queue.md"
    if not tasks_file.exists():
        return 0
    # PostToolBatch: {"tool_results": [{tool_name, tool_input, ...}, ...]}
    # PostToolUse fallback: {"tool_name": ..., "tool_input": ...}
    items = event.get("tool_results")
    if not isinstance(items, list):
        items = [{"tool_name": event.get("tool_name") or "",
                  "tool_input": event.get("tool_input") or {}}]
    items = [i for i in items if isinstance(i, dict) and i.get("tool_name")]
    if not items:
        return 0
    session_id = event.get("session_id")
    try:
        with ql.FileLock(cwd):
            st = ql.load_state(cwd)
            meta = st.setdefault("meta", {})
            window = meta.setdefault("activity_window", [])
            for it in items:
                window.append(classify(it["tool_name"], it.get("tool_input") or {}))
            del window[:-10]
            meta["phase"] = phase_from_window(window)
            meta["phase_ts"] = ql.now_iso()
            if session_id:
                meta.setdefault("sessions", {})[session_id] = ql.now_iso()
            last = items[-1]
            line = activity_line(last["tool_name"], last.get("tool_input") or {})
            for rec in st["tasks"].values():
                if rec.get("status") == "active" and (
                        not session_id or rec.get("session_id") in (None, session_id)):
                    rec["last_activity"] = line
            # reconcile when queue.md changed since we last looked
            h = ql.md_file_hash(tasks_file)
            if meta.get("seen_md_hash") != h:
                meta["seen_md_hash"] = h
                tasks, _, _ = ql.ensure_ids(cwd, tasks_file)
                ql.reconcile(cwd, tasks, st, session_id=session_id)
            ql.save_state(cwd, st)
    except Exception as exc:
        try:
            ql.append_event(cwd, "hook_error", detail=f"activity hook failed: {exc}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
