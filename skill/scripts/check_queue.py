#!/usr/bin/env python3
"""
Stop hook for the /queue skill v2.

On every Stop:
  1. Reconcile TASKS.md with .claude/taskwatch/state.json (auto IDs, automatic
     timestamps, event log).
  2. Capture completion summaries: when a task has just transitioned to [x],
     the final assistant message must contain "Shipped — …" and "Verified — …".
     The exact message is stored as raw_completion_message; the parsed parts
     become the task's summary/verification. Otherwise Claude is continued
     with feedback asking for the summary. Multiple simultaneous completions
     are summarised one at a time, never with one ambiguous blob.
  3. Refuse to let the session end while actionable tasks or unacknowledged
     prompts remain.

Loop safety: blocking a Stop makes Claude continue; if state stops changing,
we allow the stop after STOP_NOCHANGE_LIMIT consecutive no-change blocks and
log a hook_error event instead of looping forever.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_lib as ql  # noqa: E402


def block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def last_assistant_from_transcript(transcript_path: str) -> str:
    """Fallback when the hook payload lacks last_assistant_message."""
    try:
        best = ""
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("message") or {}
                if rec.get("type") == "assistant" or msg.get("role") == "assistant":
                    content = msg.get("content")
                    if isinstance(content, list):
                        text = "\n".join(c.get("text", "") for c in content
                                         if isinstance(c, dict) and c.get("type") == "text")
                    else:
                        text = content if isinstance(content, str) else ""
                    if text.strip():
                        best = text
        return best
    except OSError:
        return ""


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except Exception:
        return 0  # fail open

    cwd = Path(event.get("cwd") or ".").expanduser().resolve()
    session_id = event.get("session_id")
    tasks_file = cwd / "TASKS.md"

    if not tasks_file.exists():
        block("Queue mode is active, but TASKS.md does not exist. Create TASKS.md "
              "in the project root, capture the user's actionable requests as "
              "top-level checklist items, and begin processing them.")
        return 0

    try:
        tasks, st, warnings = ql.refresh(cwd, tasks_file, session_id=session_id)
    except Exception as exc:  # never trap Claude on our own bug
        ql.append_event(cwd, "hook_error", detail=f"check_queue refresh failed: {exc}")
        return 0

    message = event.get("last_assistant_message") or ""
    if not message and event.get("transcript_path"):
        message = last_assistant_from_transcript(event["transcript_path"])

    with ql.FileLock(cwd):
        st = ql.load_state(cwd)
        meta = st.setdefault("meta", {})
        pending: list = meta.setdefault("pending_summaries", [])

        # queue newly-completed tasks that still need a plain-English summary
        for tid, rec in st["tasks"].items():
            if rec.get("status") == "completed" and not rec.get("summary") and tid not in pending:
                pending.append(tid)

        # try to satisfy the FIRST pending task from the final message
        feedback = None
        if pending:
            shipped, verified = ql.extract_summary(message)
            tid = pending[0]
            rec = st["tasks"].get(tid) or {}
            title = rec.get("title", tid)
            if shipped and verified:
                rec["summary"] = shipped
                rec["verification"] = verified
                rec["raw_completion_message"] = message
                rec["updated_at"] = ql.now_iso()
                ql.append_event(cwd, "task_updated", id=tid, title=title,
                                summary=shipped, verification=verified)
                pending.pop(0)
                if pending:
                    nxt = st["tasks"].get(pending[0], {})
                    feedback = (
                        f"Summary captured for '{title}'. Another completed task still "
                        f"needs its own summary: '{nxt.get('title', pending[0])}'. End "
                        "your next message with exactly:\n"
                        "Shipped — <one or two plain-English paragraphs for THAT task>\n\n"
                        "Verified — <one sentence on how it was checked>\n"
                        "One task per summary; do not combine tasks."
                    )
            else:
                feedback = (
                    f"The task '{title}' was just marked complete, but the final "
                    "message did not include the required user-facing summary. End "
                    "your next message with exactly this structure:\n"
                    "Shipped — <one or two plain-English paragraphs describing what "
                    "is now different and why it matters; avoid file paths and "
                    "implementation jargon>\n\n"
                    "Verified — <one concise sentence explaining how the result was "
                    "checked>\n"
                    "Then continue with the queue."
                )

        # actionable-work check
        actionable = [t["title"] for t in tasks
                      if ql.md_status(t) in ("queued", "active")]
        unacked = ql.unacked_prompts(st)

        parts = []
        if feedback:
            parts.append(feedback)
        if actionable and not feedback:
            shown = "; ".join(actionable[:8])
            if len(actionable) > 8:
                shown += f"; and {len(actionable) - 8} more"
            parts.append(
                "Queue mode is still active. Re-read TASKS.md and continue working "
                f"without asking whether to proceed. Actionable tasks remain: {shown}. "
                "Work one task at a time, keep checkbox state current, and end each "
                "completed task with the Shipped/Verified summary."
            )
        if unacked:
            ids = ", ".join(pid for pid, _ in unacked[:6])
            parts.append(
                f"{len(unacked)} captured prompt(s) are not yet triaged ({ids}). For "
                "each: add its actionable requests to TASKS.md, then run "
                f"python3 {Path(__file__).parent / 'ack_prompt.py'} <prompt_id> "
                "(or --non-actionable for conversational messages). The queue is "
                "not finished while a prompt is untriaged."
            )

        # loop-safety valve
        md_hash = ql.md_file_hash(tasks_file)
        sig = json.dumps([md_hash, sorted(pid for pid, _ in unacked), len(pending)])
        if parts:
            if meta.get("last_block_sig") == sig:
                meta["nochange_blocks"] = meta.get("nochange_blocks", 0) + 1
            else:
                meta["nochange_blocks"] = 0
            meta["last_block_sig"] = sig
            if meta["nochange_blocks"] >= ql.STOP_NOCHANGE_LIMIT:
                ql.append_event(cwd, "hook_error",
                                detail="stop-hook continuation limit reached; allowing stop")
                ql.save_state(cwd, st)
                return 0
            ql.save_state(cwd, st)
            block(" ".join(parts))
            return 0

        meta["nochange_blocks"] = 0
        meta.pop("last_block_sig", None)
        ql.save_state(cwd, st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
