#!/usr/bin/env python3
"""Two-project isolation tests for qw + queue.

Guards the invariant that broke on 2026-08-05: launching `qw` in a fresh
project (no queue.md yet) must bind to THAT project and wait — never open
the previously remembered project's queue — and all per-project state
(state.json, events.jsonl, inbox.jsonl) must stay separate.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path.home() / ".claude" / "skills" / "queue" / "scripts"
BIN = Path.home() / ".claude" / "bin"
TASKWATCH = BIN / "queue"
QW = BIN / "qw"
sys.path.insert(0, str(SCRIPTS))
import queue_lib as ql  # noqa: E402


def load_qw_module():
    loader = importlib.machinery.SourceFileLoader("qw_mod", str(QW))
    spec = importlib.util.spec_from_loader("qw_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


qw = load_qw_module()


def run_qw(args, cwd, memory, extra_env=None):
    """Run qw with queue replaced by /bin/echo so execv prints its argv.
    stdout line = path qw bound to. Returns (returncode, stdout, stderr)."""
    env = dict(os.environ)
    env["QW_TASKWATCH"] = "/bin/echo"
    env["QW_MEMORY"] = str(memory)
    if extra_env:
        env.update(extra_env)
    p = subprocess.run([sys.executable, str(QW)] + args, cwd=str(cwd),
                       env=env, capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def run_queue(args, timeout=30):
    p = subprocess.run([sys.executable, str(TASKWATCH)] + args,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


class TwoProjects(unittest.TestCase):
    """Project A: several completed tasks. Project B: starts with no queue.md."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()  # macOS: /var → /private/var
        self.memory = root / "qw-projects.json"
        self.proj_a = root / "projA"
        self.proj_b = root / "projB"
        self.proj_a.mkdir()
        self.proj_b.mkdir()
        (self.proj_a / "queue.md").write_text(
            "# Claude Task Queue\n\n"
            "- [x] A-task-one alpha done <!-- queue:id=01AAAAAAAAAAAAAAAAAAAAAA -->\n"
            "- [x] A-task-two beta done <!-- queue:id=01AAAAAAAAAAAAAAAAAAAAAB -->\n"
            "- [ ] A-task-three gamma queued <!-- queue:id=01AAAAAAAAAAAAAAAAAAAAAC -->\n",
            encoding="utf-8")
        ql.refresh(self.proj_a, self.proj_a / "queue.md", session_id="sessA")
        st = ql.load_state(self.proj_a)
        for tid, rec in st["tasks"].items():
            if rec["status"] == "completed":
                rec["summary"] = f"Shipped summary for {rec['title']}"
                rec["verification"] = "checked in project A"
        ql.save_state(self.proj_a, st)
        ql.capture_prompt(self.proj_a, "prompt only for project A", "sessA", str(self.proj_a))
        # remembered project = A, exactly the pre-fix leak scenario
        self.memory.write_text(json.dumps(
            {"projects": {str(self.proj_a): "2026-08-05T00:00:00+00:00"},
             "last": str(self.proj_a)}), encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    # ── qw binding rules ────────────────────────────────────────────────────

    def test_plain_qw_in_empty_project_never_opens_remembered(self):
        (self.proj_b / ".git").mkdir()
        rc, out, err = run_qw([], self.proj_b, self.memory)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, str(self.proj_b / "queue.md"))
        self.assertNotIn(str(self.proj_a), out)

    def test_plain_qw_binds_to_cwd_without_any_markers(self):
        rc, out, err = run_qw([], self.proj_b, self.memory)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, str(self.proj_b / "queue.md"))

    def test_resolution_prefers_tasks_md_ancestor(self):
        sub = self.proj_a / "deep" / "er"
        sub.mkdir(parents=True)
        rc, out, err = run_qw([], sub, self.memory)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, str(self.proj_a / "queue.md"))

    def test_resolution_git_ancestor_when_no_tasks_md(self):
        (self.proj_b / ".git").mkdir()
        sub = self.proj_b / "src" / "lib"
        sub.mkdir(parents=True)
        rc, out, err = run_qw([], sub, self.memory)
        self.assertEqual(rc, 0, err)
        self.assertEqual(out, str(self.proj_b / "queue.md"))

    def test_explicit_path_and_last(self):
        rc, out, _ = run_qw([str(self.proj_b)], self.proj_b, self.memory)
        self.assertEqual(out, str(self.proj_b / "queue.md"))
        mem = json.loads(self.memory.read_text())
        self.assertEqual(mem["last"], str(self.proj_b))
        rc, out, _ = run_qw(["--last"], self.proj_a, self.memory)
        self.assertEqual(out, str(self.proj_b / "queue.md"))

    def test_list_and_forget(self):
        rc, out, _ = run_qw(["--list"], self.proj_b, self.memory)
        self.assertEqual(rc, 0)
        self.assertIn(str(self.proj_a), out)
        rc, out, _ = run_qw(["--forget"], self.proj_b, self.memory)
        self.assertEqual(rc, 0)
        mem = json.loads(self.memory.read_text())
        self.assertEqual(mem, {"projects": {}, "last": None})

    def test_resolve_local_project_unit(self):
        self.assertEqual(qw.resolve_local_project(self.proj_a), self.proj_a.resolve())
        (self.proj_b / ".git").mkdir()
        self.assertEqual(qw.resolve_local_project(self.proj_b), self.proj_b.resolve())

    def test_superset_workspace_root_detection(self):
        base = Path.home() / ".superset" / "projects"
        self.assertEqual(qw.superset_workspace_root(base / "Foo" / "sub" / "dir"),
                         base / "Foo")
        self.assertIsNone(qw.superset_workspace_root(self.proj_b))
        self.assertIsNone(qw.superset_workspace_root(base))

    # ── queue wait mode ─────────────────────────────────────────────────

    def test_queue_waits_for_missing_tasks_md_and_shows_no_foreign_tasks(self):
        rc, out, err = run_queue([str(self.proj_b / "queue.md"), "--once"])
        self.assertEqual(rc, 0, err)
        self.assertIn("Waiting for this project's queue.md", out)
        self.assertIn(str(self.proj_b.name), out)
        self.assertNotIn("A-task-one", out)
        self.assertNotIn("A-task-two", out)
        # waiting must not create state in project B
        self.assertFalse(ql.state_path(self.proj_b).exists())

    def test_queue_shows_queue_once_file_appears(self):
        (self.proj_b / "queue.md").write_text(
            "# Claude Task Queue\n\n- [ ] B-task-one delta\n", encoding="utf-8")
        rc, out, err = run_queue([str(self.proj_b / "queue.md"), "--once"])
        self.assertEqual(rc, 0, err)
        self.assertIn("B-task-one", out)
        self.assertNotIn("A-task-one", out)

    def test_project_header_lines_present(self):
        rc, out, _ = run_queue([str(self.proj_a / "queue.md"), "--once"])
        self.assertEqual(rc, 0)
        self.assertIn("Project:", out)
        self.assertIn("Queue:", out)
        self.assertIn("queue.md", out)

    # ── state isolation ─────────────────────────────────────────────────────

    def test_concurrent_edits_stay_isolated(self):
        (self.proj_b / "queue.md").write_text(
            "# Claude Task Queue\n\n- [~] B-active epsilon\n", encoding="utf-8")
        # touch both projects "simultaneously"
        ql.refresh(self.proj_b, self.proj_b / "queue.md", session_id="sessB")
        ql.capture_prompt(self.proj_b, "prompt only for project B", "sessB", str(self.proj_b))
        a_md = self.proj_a / "queue.md"
        a_md.write_text(a_md.read_text() + "- [ ] A-task-four zeta\n", encoding="utf-8")
        ql.refresh(self.proj_a, a_md, session_id="sessA")

        st_a = ql.load_state(self.proj_a)
        st_b = ql.load_state(self.proj_b)
        a_titles = {r["title"] for r in st_a["tasks"].values()}
        b_titles = {r["title"] for r in st_b["tasks"].values()}
        self.assertFalse(a_titles & b_titles)
        self.assertTrue(any("A-task-four" in t for t in a_titles))
        self.assertTrue(any("B-active" in t for t in b_titles))
        # task IDs are disjoint
        self.assertFalse(set(st_a["tasks"]) & set(st_b["tasks"]))
        # prompts / inbox are disjoint
        self.assertFalse(set(st_a["prompts"]) & set(st_b["prompts"]))
        inbox_b = ql.inbox_path(self.proj_b).read_text()
        self.assertIn("project B", inbox_b)
        self.assertNotIn("project A", inbox_b)
        inbox_a = ql.inbox_path(self.proj_a).read_text()
        self.assertNotIn("project B", inbox_a)
        # events are disjoint
        ev_a = json.dumps(ql.read_events(self.proj_a))
        ev_b = json.dumps(ql.read_events(self.proj_b))
        self.assertNotIn("B-active", ev_a)
        self.assertNotIn("A-task", ev_b)
        # summaries and timestamps stay with A only
        self.assertTrue(any(r.get("summary") for r in st_a["tasks"].values()))
        self.assertFalse(any(r.get("summary") for r in st_b["tasks"].values()))
        # tracker output for each shows only its own tasks
        _, out_a, _ = run_queue([str(a_md), "--once"])
        _, out_b, _ = run_queue([str(self.proj_b / "queue.md"), "--once"])
        self.assertIn("A-task-one", out_a)
        self.assertNotIn("B-active", out_a)
        self.assertIn("B-active", out_b)
        self.assertNotIn("A-task", out_b)

    def test_state_files_live_inside_each_project(self):
        for proj in (self.proj_a,):
            sd = ql.state_dir(proj)
            self.assertEqual(sd, proj / ".claude" / "queue")
            self.assertTrue(ql.state_path(proj).exists())

    def test_json_snapshot_reports_bound_project(self):
        rc, out, _ = run_queue([str(self.proj_a / "queue.md"), "--json"])
        snap = json.loads(out)
        self.assertEqual(snap["project"], str(self.proj_a))
        rc, out, _ = run_queue([str(self.proj_b / "queue.md"), "--json"])
        snap = json.loads(out)
        self.assertEqual(snap.get("waiting"), True)
        self.assertEqual(snap["project"], str(self.proj_b))


class HomeFallback(unittest.TestCase):
    """Remembered project is allowed ONLY from exactly $HOME (rule 5)."""

    def test_home_uses_remembered_only_when_home_not_a_project(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            fake_home = root / "home"
            fake_home.mkdir()
            proj = root / "remembered"
            proj.mkdir()
            (proj / "queue.md").write_text("# Claude Task Queue\n", encoding="utf-8")
            memory = root / "mem.json"
            memory.write_text(json.dumps(
                {"projects": {str(proj): "2026-08-05T00:00:00+00:00"},
                 "last": str(proj)}), encoding="utf-8")
            rc, out, err = run_qw([], fake_home, memory, extra_env={"HOME": str(fake_home)})
            self.assertEqual(rc, 0, err)
            self.assertEqual(out, str(proj / "queue.md"))
            # but if $HOME is itself a project, bind to $HOME instead
            (fake_home / "queue.md").write_text("# Claude Task Queue\n", encoding="utf-8")
            rc, out, err = run_qw([], fake_home, memory, extra_env={"HOME": str(fake_home)})
            self.assertEqual(rc, 0, err)
            self.assertEqual(out, str(fake_home / "queue.md"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
