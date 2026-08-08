---
name: queue
description: Activate and process a persistent TASKS.md work queue with live progress, automatic timestamps, a lossless prompt inbox, and plain-English completion summaries, until every actionable task is complete or explicitly blocked.
argument-hint: "[optional tasks to add]"
disable-model-invocation: true
disallowed-tools:
  - AskUserQuestion
hooks:
  Stop:
    - hooks:
        - type: command
          command: 'python3 "$HOME/.claude/skills/queue/scripts/check_queue.py"'
          timeout: 15
  UserPromptSubmit:
    - hooks:
        - type: command
          command: 'python3 "$HOME/.claude/skills/queue/scripts/prompt_capture.py"'
          timeout: 10
  PostToolBatch:
    - hooks:
        - type: command
          command: 'python3 "$HOME/.claude/skills/queue/scripts/activity.py"'
          timeout: 10
---

Activate task-queue mode for this session and begin work now. Do not merely
describe the workflow.

## Queue file

Use `TASKS.md` in `${CLAUDE_PROJECT_DIR}` as the authoritative queue.

If it does not exist, create it with this heading:

```markdown
# Claude Task Queue
```

If `$ARGUMENTS` contains work, convert that text into one or more distinct
top-level tasks and append them before starting. Also capture any actionable
requests from the current user message that are not already represented.

Do not delete completed tasks. Do not silently combine or omit distinct user
requests. New tasks are appended to the end of the file.

When this is a Git repository and `TASKS.md` is untracked, add `TASKS.md` to
`.git/info/exclude` so the personal queue does not appear in Git status. Never
untrack or exclude a `TASKS.md` file that is already committed. (The hooks
handle `.claude/taskwatch/` exclusion automatically.)

## Required format

Use unindented top-level tasks and indented substeps:

```markdown
- [ ] Pending task <!-- queue:id=01ABC... -->
  - [ ] Inspect the existing implementation
  - [ ] Implement the change
  - [ ] Run relevant verification

- [~] Active task <!-- queue:id=01DEF... -->
  - [x] Completed substep
  - [ ] Current substep

- [x] Completed task <!-- queue:id=01GHI... -->

- [ ] Blocked task <!-- queue:id=01JKL... -->
  - BLOCKED: Exact missing credential, decision, dependency, or external action.
```

The hidden `<!-- queue:id=... -->` comments are stable identifiers assigned
automatically by the hooks — never remove, invent, or duplicate them, and keep
them on the task's line when editing a title. Timestamps are automatic; never
type timestamps into `TASKS.md`.

Priority is an optional visible prefix that the tracker renders as a badge:

```markdown
- [ ] [urgent] Fix broken production login
- [ ] [high] Prepare client demo
```

## Queue behavior

1. Before implementation, break each meaningful top-level task into 3–6
   concrete and verifiable substeps. Do not create filler steps merely to make
   the progress bar move. Check substeps off **as you complete them**, not all
   at once at the end — the tracker only trusts incremental progress.
2. Work on **one top-level task at a time**. Complete no more than one
   top-level task between completion summaries.
3. Mark the active top-level task `[~]` before beginning.
4. Mark each substep `[x]` immediately after it is actually complete.
5. Re-read `TASKS.md` before selecting the next task so requests added while
   you were working are not missed. If a task is shown as *stale (possibly
   interrupted)* — active in a prior session with no recent activity — evaluate
   and resume it first; working on it clears the stale flag automatically.
6. Run the relevant tests, build, lint, type checks, or manual verification
   before marking a top-level task complete.
7. Mark a top-level task `[x]` only after its acceptance criteria are met.
8. After completing a task, immediately begin the next actionable task.
9. Do not pause for routine progress reports and do not ask whether to
   continue.
10. Do not use `AskUserQuestion`. When essential information is unavailable,
    add an indented `BLOCKED:` note with the exact missing information and
    continue to the next actionable task.
11. Treat every later actionable user request in this session as queue input:
    add it to `TASKS.md` before implementing it.
12. Stop only when every top-level task is complete or every remaining
    unchecked task has an explicit indented `BLOCKED:` note, and every
    captured prompt is acknowledged.

## Completion summaries — required

When you mark a top-level task `[x]`, end that same message with exactly this
structure (the Stop hook parses it and stores it as the task's permanent
record):

```text
Shipped — <one or two plain-English paragraphs describing what is now
different and why it matters. Avoid file paths, function names, and
implementation jargon unless the user needs them.>

Verified — <one concise sentence explaining how the result was checked.>
```

One summary per task. If several tasks somehow complete together, provide a
separate Shipped/Verified pair for each, one message at a time, when the Stop
hook asks.

## Prompt inbox — required

While queue mode is active, every user prompt is captured to a lossless inbox
and you are given its prompt ID via context. For each captured prompt:

1. Add its actionable requests to `TASKS.md` (append new top-level tasks), or
   decide it is conversational (e.g. "thanks") with no action needed.
2. Then acknowledge it:

```bash
python3 "$HOME/.claude/skills/queue/scripts/ack_prompt.py" <prompt_id>
python3 "$HOME/.claude/skills/queue/scripts/ack_prompt.py" <prompt_id> --non-actionable
```

The queue is not finished while any prompt is unacknowledged; the Stop hook
enforces this. Never acknowledge a prompt whose requests you have not yet
added to the queue.

## Live tracker

The user watches progress in a second pane with:

```bash
~/.claude/bin/taskwatch TASKS.md
```

You do not need to run it. Keep `TASKS.md` accurate and the tracker takes
care of itself.

When no actionable tasks remain, provide one concise summary containing:

- completed tasks,
- verification performed,
- remaining blockers, if any.
