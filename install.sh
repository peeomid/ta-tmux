#!/usr/bin/env bash
# Install ta: link the command, load the tmux config, copy the agent skill.
# Usage: ./install.sh [--bin-dir DIR] [--no-skill] [--uninstall]
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="$HOME/.local/bin"; SKILL=1; UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --bin-dir) BIN_DIR="$2"; shift ;;
    --no-skill) SKILL=0 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) sed -n 2,3p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac; shift
done
MARK="# ta-tmux (managed by install.sh)"
TMUX_CONF="$HOME/.tmux.conf"
SKILL_DIR="$HOME/.claude/skills/ta"

if [ "$UNINSTALL" = 1 ]; then
  rm -f "$BIN_DIR/ta"
  if [ -f "$TMUX_CONF" ]; then
    grep -v -F -e "$MARK" -e "source-file $HERE/tmux.conf" "$TMUX_CONF" > "$TMUX_CONF.ta-tmp" || true
    mv "$TMUX_CONF.ta-tmp" "$TMUX_CONF"
  fi
  rm -rf "$SKILL_DIR"
  echo "removed ta. State in ~/.local/state/ta was kept (delete it by hand if you want)."
  exit 0
fi

command -v tmux >/dev/null 2>&1 || { echo "tmux not found. macOS: brew install tmux" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found" >&2; exit 1; }
chmod +x "$HERE/ta"

mkdir -p "$BIN_DIR"
ln -sf "$HERE/ta" "$BIN_DIR/ta"
echo "linked  $BIN_DIR/ta -> $HERE/ta"

touch "$TMUX_CONF"
if grep -q -F "source-file $HERE/tmux.conf" "$TMUX_CONF"; then
  echo "kept    ~/.tmux.conf already loads tmux.conf"
else
  cp "$TMUX_CONF" "$TMUX_CONF.ta-backup"
  printf '\n%s\nsource-file %s/tmux.conf\n' "$MARK" "$HERE" >> "$TMUX_CONF"
  echo "added   source-file line to ~/.tmux.conf (backup: ~/.tmux.conf.ta-backup)"
fi
tmux source-file "$TMUX_CONF" 2>/dev/null || true

if [ "$SKILL" = 1 ]; then
  mkdir -p "$SKILL_DIR"
  cp "$HERE/SKILL.md" "$SKILL_DIR/SKILL.md"
  echo "copied  agent skill -> $SKILL_DIR/SKILL.md (Claude Code)"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "note    $BIN_DIR is not in your PATH. Add: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac
echo
"$BIN_DIR/ta" doctor || true
echo
echo "Done. Start with: ta"
