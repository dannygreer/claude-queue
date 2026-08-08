#!/usr/bin/env python3
"""
UserPromptSubmit hook for the /queue skill v2 — the lossless prompt inbox.

Every prompt typed while queue mode is active is appended verbatim to
.claude/taskwatch/inbox.jsonl with a generated prompt ID, and Claude is told
the ID via quiet additionalContext so it can triage and acknowledge it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_lib as ql  # noqa: E402


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0

    prompt = event.get("prompt") or ""
    if not prompt.strip():
        return 0
    cwd = Path(event.get("cwd") or ".").expanduser().resolve()
    try:
        pid = ql.capture_prompt(cwd, prompt, event.get("session_id"), str(cwd))
    except Exception as exc:
        try:
            ql.append_event(cwd, "hook_error", detail=f"prompt_capture failed: {exc}")
        except Exception:
            pass
        return 0

    ack = Path(__file__).with_name("ack_prompt.py")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                f"[queue] Prompt captured as {pid}. After adding its actionable "
                f"requests to TASKS.md (or deciding it is conversational), run: "
                f"python3 {ack} {pid} [--non-actionable]. Do not create tasks for "
                "pure acknowledgments like 'thanks'."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
