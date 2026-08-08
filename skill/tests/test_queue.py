#!/usr/bin/env python3
"""Tests for the /queue v2 core, hooks, and taskwatch snapshots.
unittest + temporary directories only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path.home() / ".claude" / "skills" / "queue" / "scripts"
TASKWATCH = Path.home() / ".claude" / "bin" / "taskwatch"
sys.path.insert(0, str(SCRIPTS))
import queue_lib as ql  # noqa: E402


def write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


class TempProject(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proj = Path(self._tmp.name)
        self.md = self.proj / "TASKS.md"

    def tearDown(self):
        self._tmp.cleanup()

    def refresh(self, session="s1"):
        return ql.refresh(self.proj, self.md, session_id=session)


class TestParsing(TempProject):
    def test_parse_tasks_and_hidden_ids(self):
        write(self.md, "# Q\n- [ ] Alpha <!-- queue:id=01AAA -->\n"
                       "  - [x] one\n  - [ ] two\n- [x] Beta\n")
        tasks, warnings = ql.parse_tasks_md(self.md.read_text())
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]["id"], "01AAA")
        self.assertEqual(tasks[0]["title"], "Alpha")
        self.assertIsNone(tasks[1]["id"])
        self.assertEqual(len(tasks[0]["subs"]), 2)
        self.assertEqual(warnings, [])

    def test_stable_id_assignment_and_duplicate_repair(self):
        write(self.md, "- [ ] One\n- [ ] Two <!-- queue:id=01DUP -->\n"
                       "- [ ] Three <!-- queue:id=01DUP -->\n")
        tasks, _, changed = ql.ensure_ids(self.proj, self.md)
        self.assertTrue(changed)
        ids = [t["id"] for t in tasks]
        self.assertTrue(all(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[1], "01DUP")  # first keeper wins
        # ids persist in the file
        tasks2, _ = ql.parse_tasks_md(self.md.read_text())
        self.assertEqual([t["id"] for t in tasks2], ids)

    def test_priority_parsing(self):
        write(self.md, "- [ ] [urgent] Fix login\n- [ ] [high] Demo\n- [ ] Normal\n")
        tasks, _, _ = ql.ensure_ids(self.proj, self.md)
        self.assertEqual([t["priority"] for t in tasks], ["urgent", "high", "normal"])
        self.assertEqual(tasks[0]["title"], "Fix login")

    def test_malformed_markdown_warning(self):
        write(self.md, "- [ ] Fine\n- [z] broken mark\n")
        _, warnings = ql.parse_tasks_md(self.md.read_text())
        self.assertTrue(any("malformed" in w for w in warnings))


class TestStateTransitions(TempProject):
    def test_transitions_and_timestamps(self):
        write(self.md, "- [ ] Job\n")
        tasks, st, _ = self.refresh()
        tid = tasks[0]["id"]
        rec = st["tasks"][tid]
        self.assertEqual(rec["status"], "queued")
        self.assertTrue(rec["created_at"])
        self.assertIsNone(rec["started_at"])

        write(self.md, self.md.read_text().replace("- [ ]", "- [~]", 1))
        _, st, _ = self.refresh()
        rec = st["tasks"][tid]
        self.assertEqual(rec["status"], "active")
        self.assertTrue(rec["started_at"])

        write(self.md, self.md.read_text().replace("- [~]", "- [x]", 1))
        _, st, _ = self.refresh()
        rec = st["tasks"][tid]
        self.assertEqual(rec["status"], "completed")
        self.assertTrue(rec["completed_at"])

    def test_blocked_state(self):
        write(self.md, "- [ ] Deploy\n  - BLOCKED: need production credentials\n")
        tasks, st, _ = self.refresh()
        rec = st["tasks"][tasks[0]["id"]]
        self.assertEqual(rec["status"], "blocked")
        self.assertTrue(rec["blocked_at"])
        self.assertEqual(tasks[0]["blocked_note"], "need production credentials")

    def test_reopen_preserves_history(self):
        write(self.md, "- [x] Done thing\n")
        tasks, st, _ = self.refresh()
        tid = tasks[0]["id"]
        self.assertEqual(st["tasks"][tid]["status"], "completed")
        write(self.md, self.md.read_text().replace("- [x]", "- [ ]", 1))
        _, st, _ = self.refresh()
        self.assertEqual(st["tasks"][tid]["status"], "queued")
        self.assertIsNone(st["tasks"][tid]["completed_at"])
        events = [e["event"] for e in ql.read_events(self.proj)]
        self.assertIn("task_reopened", events)

    def test_interrupted_detection(self):
        write(self.md, "- [~] Long job\n")
        _, st, _ = self.refresh(session="old-session")
        tid = next(iter(st["tasks"]))
        # stale heartbeat for the owner, activity from a new session
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            st["meta"]["sessions"]["old-session"] = "2020-01-01T00:00:00+00:00"
            ql.save_state(self.proj, st)
        tasks, _ = ql.parse_tasks_md(self.md.read_text())
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            ql.reconcile(self.proj, tasks, st, session_id="new-session")
            ql.save_state(self.proj, st)
        self.assertEqual(st["tasks"][tid]["status"], "interrupted")
        # resuming from the new session re-claims it
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            st["tasks"][tid]["session_id"] = None
            ql.reconcile(self.proj, tasks, st, session_id="new-session")
        self.assertEqual(st["tasks"][tid]["status"], "active")


class TestSummaries(TempProject):
    MSG = ("All set.\n\nShipped — The report page now loads twice as fast and "
           "shows a printable layout that matches the on-screen view.\n\n"
           "Verified — 42 tests passed and the page was checked in the browser.")

    def test_extraction(self):
        shipped, verified = ql.extract_summary(self.MSG)
        self.assertIn("printable layout", shipped)
        self.assertTrue(verified.startswith("42 tests passed"))

    def test_extraction_absent(self):
        self.assertEqual(ql.extract_summary("did stuff, done"), (None, None))

    def run_stop_hook(self, message, cwd=None):
        payload = {"cwd": str(cwd or self.proj), "session_id": "s1",
                   "last_assistant_message": message}
        p = subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                           input=json.dumps(payload), capture_output=True, text=True)
        out = p.stdout.strip()
        return json.loads(out) if out else {}

    def test_completion_summary_capture_via_stop_hook(self):
        write(self.md, "- [x] Build report\n")
        out = self.run_stop_hook(self.MSG)
        st = ql.load_state(self.proj)
        rec = next(iter(st["tasks"].values()))
        self.assertIn("printable layout", rec["summary"])
        self.assertIn("42 tests", rec["verification"])
        self.assertEqual(rec["raw_completion_message"], self.MSG)
        self.assertEqual(out, {})  # nothing else actionable → allowed to stop

    def test_missing_summary_blocks(self):
        write(self.md, "- [x] Build report\n")
        out = self.run_stop_hook("ok done")
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("Shipped", out.get("reason", ""))

    def test_multiple_completions_get_separate_summaries(self):
        write(self.md, "- [x] Task A\n- [x] Task B\n")
        out = self.run_stop_hook(self.MSG)  # one summary for two completions
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("Another completed task", out["reason"])
        st = ql.load_state(self.proj)
        summaries = [r.get("summary") for r in st["tasks"].values()]
        self.assertEqual(sum(1 for s in summaries if s), 1)  # only one assigned
        out2 = self.run_stop_hook(self.MSG.replace("printable layout", "second task result"))
        st = ql.load_state(self.proj)
        summaries = [r.get("summary") for r in st["tasks"].values()]
        self.assertEqual(sum(1 for s in summaries if s), 2)
        self.assertEqual(out2, {})

    def test_actionable_tasks_block_stop(self):
        write(self.md, "- [ ] Still to do\n")
        out = self.run_stop_hook("bye")
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("Still to do", out["reason"])


class TestInbox(TempProject):
    def test_capture_and_ack(self):
        write(self.md, "- [ ] T\n")
        self.refresh()
        pid = ql.capture_prompt(self.proj, "please also add dark mode", "s1", str(self.proj))
        st = ql.load_state(self.proj)
        self.assertFalse(st["prompts"][pid]["acked"])
        self.assertEqual(len(ql.unacked_prompts(st)), 1)
        # inbox line is verbatim
        line = json.loads((self.proj / ".claude/taskwatch/inbox.jsonl").read_text().splitlines()[-1])
        self.assertEqual(line["prompt"], "please also add dark mode")
        self.assertTrue(ql.ack_prompt(self.proj, pid, actionable=True))
        st = ql.load_state(self.proj)
        self.assertTrue(st["prompts"][pid]["acked"])
        self.assertEqual(ql.unacked_prompts(st), [])

    def test_unacked_prompt_blocks_stop(self):
        write(self.md, "- [x] T\n")
        # give the completed task a summary so only the prompt blocks
        tasks, st, _ = self.refresh()
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            for r in st["tasks"].values():
                r["summary"], r["verification"] = "done", "checked"
            ql.save_state(self.proj, st)
        pid = ql.capture_prompt(self.proj, "one more thing", "s1", str(self.proj))
        payload = {"cwd": str(self.proj), "session_id": "s1",
                   "last_assistant_message": "all done"}
        p = subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                           input=json.dumps(payload), capture_output=True, text=True)
        out = json.loads(p.stdout)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn(pid, out["reason"])


class TestOrdering(TempProject):
    def test_queue_ordering_and_sections(self):
        write(self.md,
              "- [ ] old normal\n- [~] running\n- [ ] [urgent] fire\n"
              "- [ ] new normal\n- [x] finished\n- [ ] stuck\n  - BLOCKED: waiting\n")
        tasks, st, warnings = self.refresh()
        # make 'old normal' older than the others
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            for r in st["tasks"].values():
                if r["title"] == "old normal":
                    r["created_at"] = "2026-01-01T00:00:00+00:00"
            ql.save_state(self.proj, st)
            st = ql.load_state(self.proj)
        snap = ql.build_snapshot(self.proj, tasks, st, warnings)
        self.assertEqual([m["title"] for m in snap["active"]], ["running"])
        self.assertEqual([m["title"] for m in snap["blocked"]], ["stuck"])
        self.assertEqual([m["title"] for m in snap["queued"]],
                         ["fire", "old normal", "new normal"])  # urgent, then FIFO
        self.assertEqual([m["title"] for m in snap["completed"]], ["finished"])
        self.assertTrue(snap["queued"][2]["new"])   # fresh task carries NEW badge
        self.assertFalse(snap["queued"][1]["new"])  # backdated one does not

    def test_honest_progress_requires_incremental_change(self):
        write(self.md, "- [~] Big job\n  - [ ] a\n  - [ ] b\n")
        tasks, st, w = self.refresh()
        snap = ql.build_snapshot(self.proj, tasks, st, w)
        self.assertFalse(snap["active"][0]["determinate"])  # 0/2, one observation
        write(self.md, self.md.read_text().replace("- [ ] a", "- [x] a"))
        tasks, st, w = self.refresh()
        snap = ql.build_snapshot(self.proj, tasks, st, w)
        self.assertTrue(snap["active"][0]["determinate"])   # 0/2 → 1/2 observed


class TestSnapshotsAndExport(TempProject):
    def seed(self):
        write(self.md, "- [~] Working on it\n  - [x] a\n  - [ ] b\n- [ ] Next up\n- [x] Old win\n")
        tasks, st, _ = self.refresh()
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            for r in st["tasks"].values():
                if r["title"] == "Old win":
                    r["summary"] = "Users can now export their data with one click."
                    r["verification"] = "Checked by exporting a sample account."
            ql.save_state(self.proj, st)

    def test_once_output(self):
        self.seed()
        p = subprocess.run([sys.executable, str(TASKWATCH), str(self.md), "--once"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        out = p.stdout
        self.assertIn("ACTIVE", out)
        self.assertIn("QUEUED", out)
        self.assertIn("COMPLETED", out)
        self.assertIn("Working on it", out)
        self.assertIn("1 active", out)
        self.assertNotIn("Traceback", p.stderr)

    def test_json_output(self):
        self.seed()
        p = subprocess.run([sys.executable, str(TASKWATCH), str(self.md), "--json"],
                           capture_output=True, text=True)
        snap = json.loads(p.stdout)
        self.assertEqual(snap["counts"]["active"], 1)
        self.assertEqual(len(snap["queued"]), 1)

    def test_export_output(self):
        self.seed()
        p = subprocess.run([sys.executable, str(TASKWATCH), str(self.md), "--export"],
                           capture_output=True, text=True)
        self.assertIn("# Completed work log", p.stdout)
        self.assertIn("Old win", p.stdout)
        self.assertIn("one click", p.stdout)
        self.assertIn("**Verified:**", p.stdout)

    def test_doctor(self):
        self.seed()
        p = subprocess.run([sys.executable, str(TASKWATCH), str(self.md), "--doctor"],
                           capture_output=True, text=True)
        self.assertIn("skill files present", p.stdout)
        self.assertIn("TASKS.md parses", p.stdout)


class TestRecovery(TempProject):
    def test_event_log_recovery(self):
        write(self.md, "- [x] Rescue me\n")
        tasks, st, _ = self.refresh()
        tid = tasks[0]["id"]
        with ql.FileLock(self.proj):
            st = ql.load_state(self.proj)
            st["tasks"][tid]["summary"] = "sum"
            ql.append_event(self.proj, "task_updated", id=tid,
                            title="Rescue me", summary="sum")
            ql.save_state(self.proj, st)
        # corrupt the state file
        ql.state_path(self.proj).write_text("{not json", encoding="utf-8")
        st2 = ql.load_state(self.proj)
        self.assertIn(tid, st2["tasks"])
        self.assertEqual(st2["tasks"][tid]["status"], "completed")
        self.assertEqual(st2["tasks"][tid]["summary"], "sum")

    def test_git_exclude(self):
        subprocess.run(["git", "init", "-q", str(self.proj)], capture_output=True)
        write(self.md, "- [ ] T\n")
        self.refresh()
        exclude = (self.proj / ".git/info/exclude").read_text()
        self.assertIn(".claude/taskwatch/", exclude)
        self.refresh()  # idempotent
        self.assertEqual(exclude.count(".claude/taskwatch/"),
                         (self.proj / ".git/info/exclude").read_text().count(".claude/taskwatch/"))


class TestStopLoopSafety(TempProject):
    def test_block_limit_is_exactly_25_consecutive_nochange_blocks(self):
        """Empirically count blocks: identical input must be blocked exactly
        STOP_NOCHANGE_LIMIT times, then allowed to stop."""
        write(self.md, "- [ ] Never gets done\n")
        self.refresh()
        payload = json.dumps({"cwd": str(self.proj), "session_id": "s1",
                              "last_assistant_message": "no progress"})
        blocks = 0
        for _ in range(ql.STOP_NOCHANGE_LIMIT + 5):
            p = subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                               input=payload, capture_output=True, text=True)
            out = p.stdout.strip()
            if out and json.loads(out).get("decision") == "block":
                blocks += 1
            else:
                break
        self.assertEqual(blocks, ql.STOP_NOCHANGE_LIMIT)
        events = [e for e in ql.read_events(self.proj)
                  if e.get("event") == "hook_error" and "continuation limit" in e.get("detail", "")]
        self.assertEqual(len(events), 1)

    def test_observable_progress_resets_the_limit(self):
        write(self.md, "- [ ] Task one\n")
        self.refresh()
        payload = json.dumps({"cwd": str(self.proj), "session_id": "s1",
                              "last_assistant_message": "x"})
        for _ in range(3):
            subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                           input=payload, capture_output=True, text=True)
        st = ql.load_state(self.proj)
        self.assertEqual(st["meta"]["nochange_blocks"], 2)
        write(self.md, self.md.read_text() + "- [ ] Task two\n")  # progress
        subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                       input=payload, capture_output=True, text=True)
        st = ql.load_state(self.proj)
        self.assertEqual(st["meta"]["nochange_blocks"], 0)


class TestTranscriptFallback(TempProject):
    def test_summary_from_transcript_when_field_missing(self):
        write(self.md, "- [x] Fallback task\n")
        transcript = self.proj / "transcript.jsonl"
        msg = ("Shipped — The importer now handles duplicate rows gracefully.\n\n"
               "Verified — Re-ran the import on the sample file without errors.")
        transcript.write_text(
            json.dumps({"type": "assistant",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": msg}]}}) + "\n",
            encoding="utf-8")
        payload = {"cwd": str(self.proj), "session_id": "s1",
                   "transcript_path": str(transcript)}  # no last_assistant_message
        p = subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                           input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(p.stdout.strip(), "")
        st = ql.load_state(self.proj)
        rec = next(iter(st["tasks"].values()))
        self.assertIn("duplicate rows", rec["summary"])

    def test_field_preferred_over_transcript(self):
        write(self.md, "- [x] Field wins\n")
        transcript = self.proj / "transcript.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text",
                                                 "text": "Shipped — WRONG.\n\nVerified — wrong."}]}}) + "\n",
            encoding="utf-8")
        msg = ("Shipped — The right summary from the hook field.\n\n"
               "Verified — Confirmed via the field path.")
        payload = {"cwd": str(self.proj), "session_id": "s1",
                   "transcript_path": str(transcript), "last_assistant_message": msg}
        subprocess.run([sys.executable, str(SCRIPTS / "check_queue.py")],
                       input=json.dumps(payload), capture_output=True, text=True)
        st = ql.load_state(self.proj)
        rec = next(iter(st["tasks"].values()))
        self.assertIn("right summary from the hook field", rec["summary"])


class TestActivityHook(TempProject):
    def test_post_tool_batch_payload(self):
        write(self.md, "- [~] Working\n")
        self.refresh()
        payload = {"cwd": str(self.proj), "session_id": "s1",
                   "hook_event_name": "PostToolBatch",
                   "tool_results": [
                       {"tool_use_id": "t1", "tool_name": "Read",
                        "tool_input": {"file_path": "/a/b.py"}, "output": "…", "error": None},
                       {"tool_use_id": "t2", "tool_name": "Edit",
                        "tool_input": {"file_path": "/a/b.py"}, "output": "…", "error": None}]}
        p = subprocess.run([sys.executable, str(SCRIPTS / "activity.py")],
                           input=json.dumps(payload), capture_output=True, text=True)
        self.assertEqual(p.stdout, "")  # completely quiet
        st = ql.load_state(self.proj)
        self.assertEqual(st["meta"]["activity_window"][-2:], ["read", "build"])
        rec = next(iter(st["tasks"].values()))
        self.assertIn("b.py", rec["last_activity"])

    def test_post_tool_use_shape_still_works(self):
        write(self.md, "- [~] Working\n")
        self.refresh()
        payload = {"cwd": str(self.proj), "session_id": "s1",
                   "tool_name": "Bash", "tool_input": {"command": "python3 -m pytest"}}
        subprocess.run([sys.executable, str(SCRIPTS / "activity.py")],
                       input=json.dumps(payload), capture_output=True, text=True)
        st = ql.load_state(self.proj)
        self.assertEqual(st["meta"]["activity_window"][-1], "verify")


if __name__ == "__main__":
    unittest.main(verbosity=2)
