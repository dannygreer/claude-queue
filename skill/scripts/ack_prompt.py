#!/usr/bin/env python3
"""
Acknowledge captured prompts (queue skill v2).

Usage:
  python3 ack_prompt.py <prompt_id> [--non-actionable] [--note "text"]
  python3 ack_prompt.py --list
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import queue_lib as ql  # noqa: E402


def main(argv: list[str]) -> int:
    cwd = Path.cwd()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if argv[0] == "--list":
        st = ql.load_state(cwd)
        rows = ql.unacked_prompts(st)
        if not rows:
            print("No unacknowledged prompts.")
            return 0
        for pid, rec in rows:
            print(f"{pid}  {rec.get('ts', '')}  {rec.get('preview', '')}")
        return 0

    pid = argv[0]
    actionable = "--non-actionable" not in argv
    note = ""
    if "--note" in argv:
        i = argv.index("--note")
        if i + 1 < len(argv):
            note = argv[i + 1]
    if ql.ack_prompt(cwd, pid, actionable=actionable, note=note):
        print(f"acknowledged {pid} ({'actionable' if actionable else 'non-actionable'})")
        return 0
    print(f"unknown prompt id: {pid}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
