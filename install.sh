#!/usr/bin/env bash
# Claude Queue installer — copies the skill + tracker into ~/.claude.
# Safe to re-run: it overwrites the installed copies with this checkout.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
claude_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
skill_dst="$claude_dir/skills/queue"
bin_dst="$claude_dir/bin"

echo "Installing Claude Queue into $claude_dir"

# Python check
if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found on PATH. Install Python 3.9+ and re-run." >&2
  exit 1
fi

# Skill
mkdir -p "$skill_dst/scripts" "$skill_dst/tests"
cp "$here/skill/SKILL.md" "$skill_dst/SKILL.md"
cp "$here/skill/scripts/"*.py "$skill_dst/scripts/"
cp "$here/skill/tests/"*.py "$skill_dst/tests/"

# Tracker
mkdir -p "$bin_dst"
cp "$here/bin/taskwatch" "$bin_dst/taskwatch"
chmod +x "$bin_dst/taskwatch"

echo "  ✓ skill    -> $skill_dst"
echo "  ✓ taskwatch -> $bin_dst/taskwatch"

# PATH hint
case ":$PATH:" in
  *":$bin_dst:"*) : ;;
  *)
    echo
    echo "Add ~/.claude/bin to your PATH so 'taskwatch' works anywhere:"
    echo "    echo 'export PATH=\"\$HOME/.claude/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    ;;
esac

echo
echo "Optional: add a 'qw' shortcut so a second terminal window just needs 'qw'"
echo "to open the tracker for the project you're in:"
echo "    echo \"alias qw='taskwatch TASKS.md'\" >> ~/.zshrc && source ~/.zshrc"
echo
echo "Done. Verify with:  $bin_dst/taskwatch --doctor"
echo "Then, in any Claude Code session, run:  /queue"
