#!/usr/bin/env python3
"""
queue_lib — shared core for the /queue skill v2.

Used by the skill hooks (check_queue.py, prompt_capture.py, activity.py,
ack_prompt.py) and by ~/.claude/bin/taskwatch. Standard library only.

Responsibilities:
  - parse TASKS.md (top-level tasks, substeps, hidden stable IDs, priority,
    BLOCKED notes) without disturbing anything else in the file
  - assign missing stable IDs atomically
  - maintain .claude/taskwatch/state.json (atomic, flock-protected)
  - append .claude/taskwatch/events.jsonl (audit log) and inbox.jsonl
  - reconcile markdown truth with state: status transitions + timestamps
  - extract "Shipped —" / "Verified —" completion summaries
  - build display snapshots (ordering, sections, archive) for the tracker
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE_DIR_NAME = os.path.join(".claude", "taskwatch")
ARCHIVE_DAYS = 30
NEW_BADGE_MINUTES = 10
INTERRUPT_STALE_SECONDS = 15 * 60
STOP_NOCHANGE_LIMIT = 25  # safety valve for custom Stop-hook continuation loops

CHECKBOX_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+\[(?P<mark>[ xX~])\]\s+(?P<text>.*?)\s*$")
ID_RE = re.compile(r"<!--\s*queue:id=(?P<id>[A-Za-z0-9_-]+)\s*-->")
BLOCKED_RE = re.compile(r"^\s+(?:[-*]\s+)?BLOCKED\s*:\s*(?P<note>.*)$", re.IGNORECASE)
PRIORITY_RE = re.compile(r"^\[(?P<p>urgent|high)\]\s+", re.IGNORECASE)
SHIPPED_RE = re.compile(
    r"Shipped\s*[—–-]\s*(?P<body>.+?)(?=(?:\n\s*\n\s*Verified\s*[—–-])|(?:\n\s*Verified\s*[—–-])|\Z)",
    re.DOTALL,
)
VERIFIED_RE = re.compile(r"Verified\s*[—–-]\s*(?P<body>.+?)(?=(?:\n\s*\n\s*Shipped\s*[—–-])|\Z)", re.DOTALL)

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    """ULID-flavoured stable ID: 48-bit ms timestamp + 60 random bits, base32."""
    ms = int(time.time() * 1000)
    chars = []
    for shift in range(45, -1, -5):
        chars.append(_B32[(ms >> shift) & 31])
    rand = int.from_bytes(os.urandom(8), "big")
    for shift in range(55, -1, -5):
        chars.append(_B32[(rand >> shift) & 31])
    return "01" + "".join(chars)[:22]


# ─── paths ────────────────────────────────────────────────────────────────────

def state_dir(project_dir: Path) -> Path:
    return project_dir / STATE_DIR_NAME


def state_path(project_dir: Path) -> Path:
    return state_dir(project_dir) / "state.json"


def events_path(project_dir: Path) -> Path:
    return state_dir(project_dir) / "events.jsonl"


def inbox_path(project_dir: Path) -> Path:
    return state_dir(project_dir) / "inbox.jsonl"


def ensure_state_dir(project_dir: Path) -> Path:
    d = state_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)
    _ensure_git_exclude(project_dir)
    return d


def _ensure_git_exclude(project_dir: Path) -> None:
    """Add .claude/taskwatch/ to .git/info/exclude (never the committed .gitignore)."""
    git = project_dir / ".git"
    if not git.is_dir():
        return
    pattern = ".claude/taskwatch/"
    exclude = git / "info" / "exclude"
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if pattern in existing.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(pattern + "\n")
    except OSError:
        pass


# ─── locking & atomic io ─────────────────────────────────────────────────────

class FileLock:
    def __init__(self, project_dir: Path):
        self._path = state_dir(project_dir) / ".lock"
        self._fh = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "w")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
        except OSError:
            pass
        return False


def atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ─── events ──────────────────────────────────────────────────────────────────

def append_event(project_dir: Path, event: str, **data) -> None:
    ensure_state_dir(project_dir)
    rec = {"ts": now_iso(), "event": event}
    rec.update(data)
    try:
        with open(events_path(project_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_events(project_dir: Path) -> list[dict]:
    out = []
    try:
        with open(events_path(project_dir), encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


# ─── state ───────────────────────────────────────────────────────────────────

def default_state() -> dict:
    return {"version": 2, "tasks": {}, "prompts": {}, "meta": {}}


def load_state(project_dir: Path) -> dict:
    p = state_path(project_dir)
    try:
        with open(p, encoding="utf-8") as f:
            st = json.load(f)
        if not isinstance(st, dict) or "tasks" not in st:
            raise ValueError("bad shape")
        st.setdefault("prompts", {})
        st.setdefault("meta", {})
        return st
    except FileNotFoundError:
        return default_state()
    except (ValueError, OSError):
        # Corrupt state: rebuild what we can from the event log.
        st = rebuild_state_from_events(project_dir)
        append_event(project_dir, "hook_error", detail="state.json unreadable; rebuilt from events")
        return st


def save_state(project_dir: Path, st: dict) -> None:
    ensure_state_dir(project_dir)
    atomic_write(state_path(project_dir), json.dumps(st, indent=1, ensure_ascii=False))


def rebuild_state_from_events(project_dir: Path) -> dict:
    st = default_state()
    for ev in read_events(project_dir):
        kind = ev.get("event")
        tid = ev.get("id")
        ts = ev.get("ts")
        if kind == "prompt_captured" and ev.get("prompt_id"):
            st["prompts"][ev["prompt_id"]] = {
                "ts": ts, "acked": False, "actionable": None,
                "session_id": ev.get("session_id"), "preview": ev.get("preview", ""),
            }
            continue
        if kind == "prompt_acknowledged" and ev.get("prompt_id"):
            rec = st["prompts"].setdefault(ev["prompt_id"], {"ts": ts})
            rec["acked"] = True
            rec["actionable"] = ev.get("actionable")
            continue
        if not tid:
            continue
        t = st["tasks"].setdefault(tid, {"id": tid, "status": "queued", "priority": "normal",
                                         "created_at": ts, "started_at": None, "completed_at": None,
                                         "blocked_at": None, "updated_at": ts, "summary": None,
                                         "verification": None, "raw_completion_message": None,
                                         "session_id": None, "last_activity": None, "title": ev.get("title", "")})
        if ev.get("title"):
            t["title"] = ev["title"]
        t["updated_at"] = ts
        if kind == "task_created":
            t["created_at"] = t.get("created_at") or ts
        elif kind == "task_started":
            t["status"] = "active"
            t["started_at"] = t.get("started_at") or ts
        elif kind == "task_completed":
            t["status"] = "completed"
            t["completed_at"] = ts
            if ev.get("summary"):
                t["summary"] = ev["summary"]
            if ev.get("verification"):
                t["verification"] = ev["verification"]
        elif kind == "task_blocked":
            t["status"] = "blocked"
            t["blocked_at"] = ts
        elif kind == "task_reopened":
            t["status"] = "queued"
            t["completed_at"] = None
        elif kind == "task_interrupted":
            t["status"] = "interrupted"
        elif kind == "task_updated":
            for k in ("priority", "summary", "verification", "last_activity"):
                if ev.get(k) is not None:
                    t[k] = ev[k]
    return st


# ─── TASKS.md parsing ────────────────────────────────────────────────────────

def parse_tasks_md(text: str) -> tuple[list[dict], list[str]]:
    """Return (tasks, warnings). Each task:
    {line_no, mark, raw_title, title, id, priority, blocked, blocked_note,
     subs: [{mark, text}], body_lines: [...]}"""
    lines = text.splitlines()
    warnings: list[str] = []
    starts: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        m = CHECKBOX_RE.match(line)
        if m and not m.group("indent"):
            starts.append((i, m.group("mark").lower(), m.group("text")))
        elif m is None and re.match(r"^[-*]\s+\[", line):
            warnings.append(f"line {i + 1}: looks like a task but is malformed: {line.strip()[:60]}")

    tasks: list[dict] = []
    seen_ids: dict[str, int] = {}
    for pos, (start, mark, raw) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        body = lines[start + 1:end]
        idm = ID_RE.search(raw)
        tid = idm.group("id") if idm else None
        title = ID_RE.sub("", raw).strip()
        pm = PRIORITY_RE.match(title)
        priority = "normal"
        if pm:
            priority = pm.group("p").lower()
            title = title[pm.end():].strip()
        blocked_note = None
        subs = []
        for b in body:
            bm = BLOCKED_RE.match(b)
            if bm and blocked_note is None:
                blocked_note = bm.group("note").strip() or "blocked"
            sm = CHECKBOX_RE.match(b)
            if sm and sm.group("indent"):
                subs.append({"mark": sm.group("mark").lower(), "text": ID_RE.sub("", sm.group("text")).strip()})
        if tid:
            if tid in seen_ids:
                warnings.append(f"duplicate id {tid} (lines {seen_ids[tid] + 1} and {start + 1})")
            else:
                seen_ids[tid] = start
        tasks.append({
            "line_no": start, "mark": mark, "raw_title": raw, "title": title,
            "id": tid, "priority": priority,
            "blocked": blocked_note is not None and mark != "x",
            "blocked_note": blocked_note, "subs": subs, "body_lines": body,
        })
    return tasks, warnings


def ensure_ids(project_dir: Path, tasks_file: Path) -> tuple[list[dict], list[str], bool]:
    """Assign stable IDs to top-level tasks missing one, and repair duplicate
    IDs (later duplicate gets a fresh ID). Atomic write-back. Returns
    (tasks, warnings, changed)."""
    try:
        text = tasks_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], ["TASKS.md unreadable"], False
    tasks, warnings = parse_tasks_md(text)
    lines = text.splitlines()
    seen: set[str] = set()
    changed = False
    for t in tasks:
        tid = t["id"]
        if tid and tid not in seen:
            seen.add(tid)
            continue
        fresh = new_id()
        line = lines[t["line_no"]]
        if tid:  # duplicate — replace the comment on this line
            line = ID_RE.sub(f"<!-- queue:id={fresh} -->", line)
            append_event(project_dir, "task_updated", id=fresh, title=t["title"],
                         detail=f"repaired duplicate id {tid}")
        else:
            line = line.rstrip() + f" <!-- queue:id={fresh} -->"
        lines[t["line_no"]] = line
        t["id"] = fresh
        seen.add(fresh)
        changed = True
    if changed:
        trailing = "\n" if text.endswith("\n") or not text else ""
        atomic_write(tasks_file, "\n".join(lines) + trailing)
    return tasks, warnings, changed


# ─── status mapping & reconcile ──────────────────────────────────────────────

def md_status(task: dict) -> str:
    if task["mark"] == "x":
        return "completed"
    if task["mark"] == "~":
        return "active"
    if task["blocked"]:
        return "blocked"
    return "queued"


def reconcile(project_dir: Path, tasks: list[dict], st: dict,
              session_id: str | None = None) -> dict:
    """Fold the markdown truth into state. Sets automatic timestamps, emits
    events, tracks incremental substep progress, flags interruptions.
    Returns {"newly_completed": [ids], "reopened": [ids]}."""
    out = {"newly_completed": [], "reopened": []}
    now = now_iso()
    seen_ids = set()
    for t in tasks:
        tid = t["id"]
        if not tid:
            continue
        seen_ids.add(tid)
        rec = st["tasks"].get(tid)
        if rec is None:
            rec = {"id": tid, "title": t["title"], "status": "queued",
                   "priority": t["priority"], "created_at": now, "started_at": None,
                   "completed_at": None, "blocked_at": None, "updated_at": now,
                   "summary": None, "verification": None,
                   "raw_completion_message": None, "session_id": None,
                   "last_activity": None, "sub_counts_seen": []}
            st["tasks"][tid] = rec
            append_event(project_dir, "task_created", id=tid, title=t["title"])
        new_status = md_status(t)
        old_status = rec.get("status", "queued")
        dirty = False
        if rec.get("title") != t["title"]:
            rec["title"] = t["title"]
            dirty = True
        if rec.get("priority") != t["priority"]:
            rec["priority"] = t["priority"]
            append_event(project_dir, "task_updated", id=tid, title=t["title"], priority=t["priority"])
            dirty = True
        # incremental-substep tracking for honest progress
        done = sum(1 for s in t["subs"] if s["mark"] == "x")
        total = len(t["subs"])
        seen_counts = rec.setdefault("sub_counts_seen", [])
        key = f"{done}/{total}"
        if total and (not seen_counts or seen_counts[-1] != key):
            seen_counts.append(key)
            del seen_counts[:-12]
            dirty = True

        if new_status != old_status:
            if new_status == "completed":
                if not rec.get("completed_at"):
                    rec["completed_at"] = now
                rec["status"] = "completed"
                append_event(project_dir, "task_completed", id=tid, title=t["title"])
                out["newly_completed"].append(tid)
            elif new_status == "active":
                if old_status == "completed":
                    _reopen(project_dir, rec, t, now)
                    out["reopened"].append(tid)
                rec["status"] = "active"
                if not rec.get("started_at"):
                    rec["started_at"] = now
                    append_event(project_dir, "task_started", id=tid, title=t["title"])
                if session_id:
                    rec["session_id"] = session_id
            elif new_status == "blocked":
                if old_status == "completed":
                    _reopen(project_dir, rec, t, now)
                    out["reopened"].append(tid)
                rec["status"] = "blocked"
                if not rec.get("blocked_at"):
                    rec["blocked_at"] = now
                append_event(project_dir, "task_blocked", id=tid, title=t["title"],
                             note=t.get("blocked_note"))
            else:  # queued
                if old_status == "completed":
                    _reopen(project_dir, rec, t, now)
                    out["reopened"].append(tid)
                elif old_status == "blocked":
                    rec["blocked_at"] = None
                rec["status"] = "queued"
            dirty = True
        elif new_status == "active" and session_id and rec.get("status") == "interrupted":
            rec["session_id"] = session_id
            rec["status"] = "active"  # resuming an interrupted task re-claims it
            dirty = True
        if dirty:
            rec["updated_at"] = now
    # interruption detection: active in state, owner session stale
    sessions = st["meta"].setdefault("sessions", {})
    if session_id:
        sessions[session_id] = now
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=INTERRUPT_STALE_SECONDS)
    for tid, rec in st["tasks"].items():
        if rec.get("status") != "active":
            continue
        owner = rec.get("session_id")
        if session_id and owner == session_id:
            continue
        beat = sessions.get(owner or "", None)
        stale = True
        if beat:
            try:
                stale = datetime.fromisoformat(beat) < cutoff
            except ValueError:
                stale = True
        if stale and owner != session_id:
            rec["status"] = "interrupted"
            rec["updated_at"] = now
            append_event(project_dir, "task_interrupted", id=tid, title=rec.get("title", ""))
    return out


def _reopen(project_dir: Path, rec: dict, t: dict, now: str) -> None:
    append_event(project_dir, "task_reopened", id=rec["id"], title=t["title"],
                 prior_completed_at=rec.get("completed_at"),
                 prior_summary=rec.get("summary"))
    rec["completed_at"] = None


# ─── completion summaries ────────────────────────────────────────────────────

def extract_summary(message: str) -> tuple[str | None, str | None]:
    """Extract ('Shipped — …' body, 'Verified — …' body) from a message."""
    if not message:
        return None, None
    sm = SHIPPED_RE.search(message)
    vm = VERIFIED_RE.search(message)
    shipped = re.sub(r"\s+\n", "\n", sm.group("body")).strip() if sm else None
    verified = " ".join(vm.group("body").split()) if vm else None
    return shipped, verified


# ─── inbox ───────────────────────────────────────────────────────────────────

def capture_prompt(project_dir: Path, prompt: str, session_id: str | None, cwd: str) -> str:
    ensure_state_dir(project_dir)
    pid = "P" + new_id()[2:12]
    rec = {"prompt_id": pid, "ts": now_iso(), "session_id": session_id,
           "cwd": cwd, "prompt": prompt}
    with FileLock(project_dir):
        try:
            with open(inbox_path(project_dir), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass
        st = load_state(project_dir)
        st["prompts"][pid] = {"ts": rec["ts"], "acked": False, "actionable": None,
                              "session_id": session_id,
                              "preview": " ".join(prompt.split())[:120]}
        save_state(project_dir, st)
        append_event(project_dir, "prompt_captured", prompt_id=pid,
                     session_id=session_id, preview=st["prompts"][pid]["preview"])
    return pid


def ack_prompt(project_dir: Path, prompt_id: str, actionable: bool, note: str = "") -> bool:
    with FileLock(project_dir):
        st = load_state(project_dir)
        rec = st["prompts"].get(prompt_id)
        if rec is None:
            return False
        rec["acked"] = True
        rec["actionable"] = actionable
        if note:
            rec["note"] = note
        save_state(project_dir, st)
        append_event(project_dir, "prompt_acknowledged", prompt_id=prompt_id,
                     actionable=actionable, note=note)
    return True


def unacked_prompts(st: dict) -> list[tuple[str, dict]]:
    return sorted(((pid, r) for pid, r in st.get("prompts", {}).items()
                   if not r.get("acked")), key=lambda kv: kv[1].get("ts") or "")


# ─── snapshot / ordering ─────────────────────────────────────────────────────

_PRI_ORDER = {"urgent": 0, "high": 1, "normal": 2}


def build_snapshot(project_dir: Path, tasks: list[dict], st: dict,
                   warnings: list[str] | None = None) -> dict:
    """Merge md + state into display order. Sections: active,
    blocked_interrupted, queued (priority then FIFO), completed (newest
    first), archived (completed > ARCHIVE_DAYS)."""
    now = datetime.now(timezone.utc)
    merged = []
    for i, t in enumerate(tasks):
        rec = st["tasks"].get(t["id"] or "", {})
        status = md_status(t)
        if status == "active" and rec.get("status") == "interrupted":
            status = "interrupted"
        counts = rec.get("sub_counts_seen", [])
        distinct = {c for c in counts}
        done = sum(1 for s in t["subs"] if s["mark"] == "x")
        total = len(t["subs"])
        merged.append({
            "id": t["id"], "title": t["title"], "status": status,
            "priority": t["priority"], "blocked_note": t.get("blocked_note"),
            "subs": t["subs"], "subs_done": done, "subs_total": total,
            "determinate": total >= 2 and len(distinct) >= 2 and done > 0,
            "created_at": rec.get("created_at"), "started_at": rec.get("started_at"),
            "completed_at": rec.get("completed_at"), "updated_at": rec.get("updated_at"),
            "summary": rec.get("summary"), "verification": rec.get("verification"),
            "last_activity": rec.get("last_activity"), "order": i,
        })
    active = [m for m in merged if m["status"] == "active"]
    blocked = [m for m in merged if m["status"] in ("blocked", "interrupted")]
    queued = sorted((m for m in merged if m["status"] == "queued"),
                    key=lambda m: (_PRI_ORDER.get(m["priority"], 2),
                                   m["created_at"] or "", m["order"]))
    completed_all = sorted((m for m in merged if m["status"] == "completed"),
                           key=lambda m: m["completed_at"] or "", reverse=True)
    completed, archived = [], []
    for m in completed_all:
        old = False
        if m["completed_at"]:
            try:
                old = (now - datetime.fromisoformat(m["completed_at"])) > timedelta(days=ARCHIVE_DAYS)
            except ValueError:
                old = False
        (archived if old else completed).append(m)
    for m in queued:
        fresh = False
        if m["created_at"]:
            try:
                fresh = (now - datetime.fromisoformat(m["created_at"])) < timedelta(minutes=NEW_BADGE_MINUTES)
            except ValueError:
                fresh = False
        m["new"] = fresh
    return {
        "generated_at": now_iso(),
        "counts": {"active": len(active), "queued": len(queued),
                   "blocked": len(blocked), "completed": len(completed_all),
                   "untriaged": len(unacked_prompts(st))},
        "active": active, "blocked": blocked, "queued": queued,
        "completed": completed, "archived": archived,
        "warnings": warnings or [], "untriaged": [
            {"prompt_id": pid, "ts": r.get("ts"), "preview": r.get("preview", "")}
            for pid, r in unacked_prompts(st)
        ],
    }


def md_file_hash(tasks_file: Path) -> str:
    try:
        return hashlib.sha256(tasks_file.read_bytes()).hexdigest()
    except OSError:
        return ""


def refresh(project_dir: Path, tasks_file: Path, session_id: str | None = None,
            write: bool = True) -> tuple[list[dict], dict, list[str]]:
    """One-stop: lock, ensure IDs, load state, reconcile, save. Returns
    (tasks, state, warnings)."""
    ensure_state_dir(project_dir)
    with FileLock(project_dir):
        if write:
            tasks, warnings, _ = ensure_ids(project_dir, tasks_file)
        else:
            try:
                tasks, warnings = parse_tasks_md(tasks_file.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                return [], default_state(), ["TASKS.md unreadable"]
        st = load_state(project_dir)
        reconcile(project_dir, tasks, st, session_id=session_id)
        if write:
            save_state(project_dir, st)
        return tasks, st, warnings
