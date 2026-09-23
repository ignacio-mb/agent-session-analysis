#!/usr/bin/env bash
# Install the session-export skill for Claude Code by symlinking it into the user skills directory.
# The symlink keeps this checkout the single source of truth: `git pull` updates the skill.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
skills_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
dest="$skills_dir/session-export"
src="$repo/skills/session-export"

if [ "${1:-}" = "--uninstall" ]; then
  if [ -L "$dest" ]; then rm "$dest"; echo "removed $dest"; else echo "nothing to remove at $dest"; fi
  exit 0
fi

mkdir -p "$skills_dir"
if [ -e "$dest" ] && [ ! -L "$dest" ]; then
  echo "refusing to replace $dest: it exists and is not a symlink (move it aside first)" >&2
  exit 1
fi
ln -sfn "$src" "$dest"
chmod +x "$src/scripts/session_export.py"
echo "installed: $dest -> $src"
python3 "$dest/scripts/session_export.py" --version
echo "In Claude Code, run /session-export (a new session picks the skill up)."
