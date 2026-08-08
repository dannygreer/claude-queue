# Claude Queue

A persistent task queue for [Claude Code](https://claude.com/claude-code) — with a live terminal tracker, a lossless prompt inbox, and plain-English "here's what I finished" summaries. Type `/queue`, keep feeding it work, and watch progress in a second pane.

> Standard-library Python. No accounts, no services, no dependencies. Your queue is a plain `TASKS.md` file in your project.

---

## Why I built this

I run big work through [Linear](https://linear.app). It's great for tracking epics and issues at the project level — the *what* and the *why* over weeks.

But the actual work happens in the Claude Code CLI, and two things kept biting me:

1. **I'd lose the thread of what had actually been done.** A long session would scroll past, tasks would get finished, and by the end I couldn't easily reconstruct what shipped versus what I'd only talked about. The CLI is a river; Linear is too coarse to catch the day-to-day.
2. **I like to keep feeding Claude tasks while it's still working.** I don't want to wait for it to finish before I add the next three things I just thought of. But new requests mid-flight are easy to drop — for me *and* for the agent.

So I built Claude Queue. It turns a `TASKS.md` into a real work queue: every task gets a stable ID and automatic timestamps, every prompt you send mid-session is captured to an inbox so nothing you ask gets silently dropped, and every completed task ends with a short **Shipped / Verified** record you can actually read back later. A live tracker in a second pane shows exactly what's active, queued, blocked, and done.

It's the memory layer between "the CLI firehose" and "my Linear board."

---

## What it does

- **`/queue` activates queue mode.** Claude works the list top-to-bottom, one task at a time, and doesn't stop until everything is done or explicitly blocked.
- **Keep typing.** Every prompt you send while it's working is captured to a lossless inbox and folded into the queue — no dropped requests.
- **Honest progress.** Tasks break into substeps that get checked off *as they're completed*, not all at once at the end.
- **Readable history.** Each finished task stores a plain-English `Shipped —` / `Verified —` summary. Export the whole log any time.
- **Live tracker.** `taskwatch TASKS.md` gives you a curses TUI: active / queued / blocked / done, priority badges, search, filter, and per-task detail.
- **Crash-safe.** State is atomic and flock-protected, with an append-only event log it can rebuild from if anything gets corrupted.

## The tracker

```
taskwatch TASKS.md            interactive TUI
taskwatch TASKS.md --once     plain-text snapshot
taskwatch TASKS.md --json     JSON snapshot
taskwatch TASKS.md --export   Markdown completion log, newest first
taskwatch TASKS.md --doctor   installation & queue health checks
```

Keys: `j`/`k` move · `Enter` expand · `t` technical detail · `/` search · `f` filter · `a` archive · `c` copy summary · `q` quit.

---

## Install

**One-liner** (clones nothing permanent; copies the skill into `~/.claude`):

```bash
git clone https://github.com/dannygreer/claude-queue.git
cd claude-queue
./install.sh
```

The installer copies the skill to `~/.claude/skills/queue/`, the tracker to `~/.claude/bin/taskwatch`, and tells you if `~/.claude/bin` needs to go on your `PATH`. Re-run it any time to update.

**Verify:**

```bash
taskwatch --doctor      # or: ~/.claude/bin/taskwatch --doctor
```

### Requirements

- [Claude Code](https://claude.com/claude-code)
- Python 3.9+ (standard library only — nothing to `pip install`)
- macOS or Linux (uses `fcntl` file locking)

---

## Usage

In any project, start a session and run:

```
/queue add the login bug, then the export feature, then update the changelog
```

Claude turns that into tasks and starts working. In a second terminal pane:

```bash
taskwatch TASKS.md
```

Add more work at any time — just type it. It gets queued, not dropped.

The queue lives in `TASKS.md` at your project root. Tracker state lives in `.claude/taskwatch/` next to it (auto-excluded from Git via `.git/info/exclude`, so it never clutters your commits).

---

## How it works

Three Claude Code hooks, declared in the skill's `SKILL.md`:

| Hook | Script | Job |
|------|--------|-----|
| `UserPromptSubmit` | `prompt_capture.py` | Capture every prompt to the inbox so nothing is lost |
| `PostToolBatch` | `activity.py` | Record live activity for the tracker's progress signals |
| `Stop` | `check_queue.py` | Reconcile `TASKS.md` ↔ state, capture Shipped/Verified summaries, and refuse to end while actionable work or unacknowledged prompts remain |

All three share `queue_lib.py`, which also backs `taskwatch`. `TASKS.md` is the single source of truth; the `.claude/taskwatch/` state is a derived, rebuildable cache.

---

## Build it yourself

Prefer to grow your own version (or have Claude Code build it for you) instead of installing this one? The full design spec and a copy-paste build brief are in **[docs/BUILD.md](docs/BUILD.md)** — architecture, the `TASKS.md` format, the hook contracts, and the invariants that keep it crash-safe.

---

## License

[MIT](LICENSE) © Danny Greer
