# Build it yourself

Two ways to use this spec:

1. **Read it** to understand exactly how Claude Queue works before installing.
2. **Hand it to Claude Code** as a build brief and have it grow your own version, tuned to how you work.

The whole system is ~1,500 lines of standard-library Python. No frameworks, no services. If you understand the five pieces below, you can rebuild or fork it confidently.

---

## The one rule that makes it work

**`queue.md` is the single source of truth. Everything in `.claude/queue/` is a derived cache that can be deleted and rebuilt.**

You (or the agent) edit a plain Markdown checklist. Hooks observe those edits and maintain a state file alongside it. If the state file is ever lost or corrupted, it is rebuilt from an append-only event log. This is why the system is crash-safe and why your queue is never trapped in a database.

---

## Piece 1 — the `queue.md` format

Unindented lines are top-level tasks; indented lines are substeps.

```markdown
# Claude Task Queue

- [ ] Pending task <!-- queue:id=01ABC… -->
  - [ ] A concrete substep
  - [ ] Another substep

- [~] Active task <!-- queue:id=01DEF… -->
  - [x] Completed substep
  - [ ] Current substep

- [x] Completed task <!-- queue:id=01GHI… -->

- [ ] Blocked task <!-- queue:id=01JKL… -->
  - BLOCKED: The exact missing credential, decision, or dependency.
```

- Marks: `[ ]` queued, `[~]` active, `[x]` done. A task with a `BLOCKED:` child (and not `[x]`) is blocked.
- An optional visible `[urgent]` / `[high]` prefix sets priority.
- Each top-level task carries a hidden, stable `<!-- queue:id=… -->` comment. **The agent never writes these** — a hook assigns them (a ULID-flavoured base32 id: 48-bit millisecond timestamp + random bits). Stable ids are what let the tracker follow a task across title edits.

## Piece 2 — the state cache

Next to `queue.md`, in `.claude/queue/`:

- `state.json` — per-task record: status, `created_at` / `started_at` / `completed_at` / `blocked_at` timestamps, priority, the Shipped/Verified summary, owning session id, and a short history of substep counts for honest progress. Also per-prompt inbox records. Written **atomically** (temp file + `os.replace`) under an **flock**.
- `events.jsonl` — append-only audit log (`task_created`, `task_started`, `task_completed`, `prompt_captured`, …). `state.json` can be fully reconstructed from this if it goes missing.
- `inbox.jsonl` — the lossless record of every captured prompt.

Add `.claude/queue/` to `.git/info/exclude` (never the committed `.gitignore`) so personal queue state never shows up in `git status`.

## Piece 3 — the three hooks

Declare these in the skill's `SKILL.md` front-matter. Each is a tiny Python entry point over a shared `queue_lib`.

| Hook | When | Responsibility |
|------|------|----------------|
| `UserPromptSubmit` | every prompt | Append the prompt to `inbox.jsonl` and register it unacknowledged. Nothing the user types can be silently lost. |
| `PostToolBatch` | after tool batches | Stamp `last_activity` so the tracker can tell a working session from a stalled one. |
| `Stop` | session tries to end | The important one — see below. |

### The `Stop` hook contract

1. **Reconcile** `queue.md` into `state.json`: assign missing ids, set automatic timestamps on status transitions, emit events.
2. **Capture summaries**: when a task has just flipped to `[x]`, require the final assistant message to contain `Shipped — …` and `Verified — …`. Parse and store them as the task's permanent record. If missing, block the stop and ask for the summary.
3. **Refuse to end** while any task is still actionable (queued/active and not blocked) or any captured prompt is unacknowledged.
4. **Loop safety**: blocking a Stop makes the agent continue. If state stops changing across ~25 consecutive no-change blocks, allow the stop and log an error instead of looping forever.

## Piece 4 — `queue_lib`

The shared core imported by every hook and by the tracker. Pure functions over the file formats above:

- `parse_tasks_md(text)` → tasks + warnings, touching nothing else in the file.
- `ensure_ids()` → assign/repair stable ids, atomic write-back.
- `reconcile()` → fold Markdown truth into state, emit events, detect interruptions (a task left `active` by a session whose heartbeat went stale for 15 min becomes `interrupted`/"stale").
- `build_snapshot()` → ordered display model: active, blocked/interrupted, queued (priority then FIFO), completed (newest first), archived (>30 days).
- `extract_summary()` → pull the `Shipped —` / `Verified —` bodies out of a message.
- Atomic write + `FileLock` helpers used everywhere state is touched.

Keep it standard-library only. The determinism and the crash-safety both live here.

## Piece 5 — the `queue` tracker

A single-file curses TUI that reads the same `queue_lib` snapshot and renders it live in a second pane. Give it non-interactive modes so it's testable and scriptable:

```
queue queue.md            interactive TUI
queue queue.md --once     plain-text snapshot
queue queue.md --json     JSON snapshot
queue queue.md --export   Markdown completion log
queue queue.md --doctor   health checks
```

Indeterminate vs. determinate progress bars: only show a real percentage once a task has ≥2 substeps and you've actually observed progress move — otherwise an indeterminate shimmer. Honesty over a bar that always looks busy.

---

## A build brief you can paste into Claude Code

> Build a persistent task-queue skill for Claude Code, standard-library Python only, crash-safe, with `queue.md` as the single source of truth and a derived, rebuildable state cache in `.claude/queue/`. Implement: (1) a `queue_lib` with Markdown parsing that preserves the file, stable ULID-style ids assigned by hooks, atomic + flock-protected state writes, an append-only event log the state can be rebuilt from, reconcile-with-timestamps, interruption detection, and Shipped/Verified summary extraction; (2) three hooks — `UserPromptSubmit` captures every prompt to a lossless inbox, `PostToolBatch` records activity, `Stop` reconciles, requires a Shipped/Verified summary for each just-completed task, and refuses to end while actionable tasks or unacknowledged prompts remain (with a no-change loop breaker); (3) a single-file `queue` curses tracker with `--once/--json/--export/--doctor` modes and honest determinate-vs-indeterminate progress. Then write tests covering the parser, id assignment, reconcile transitions, interruption detection, and summary extraction.

Start there, then shape the behavior to your own workflow.
