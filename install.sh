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

# Tracker (queue) + launcher (qw)
mkdir -p "$bin_dst"
cp "$here/bin/queue" "$bin_dst/queue"
cp "$here/bin/qw" "$bin_dst/qw"
chmod +x "$bin_dst/queue" "$bin_dst/qw"

echo "  ✓ skill  -> $skill_dst"
echo "  ✓ queue  -> $bin_dst/queue"
echo "  ✓ qw     -> $bin_dst/qw"

# PATH hint
case ":$PATH:" in
  *":$bin_dst:"*) : ;;
  *)
    echo
    echo "Add ~/.claude/bin to your PATH so 'queue' and 'qw' work anywhere:"
    echo "    echo 'export PATH=\"\$HOME/.claude/bin:\$PATH\"' >> ~/.zshrc && source ~/.zshrc"
    ;;
esac

echo
echo "Done. Verify with:  $bin_dst/queue --doctor"
echo "Then, in any Claude Code session, run:  /queue"
echo "And in a second pane for that project, run:  qw"
