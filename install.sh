#!/usr/bin/env bash
# Symlink every device/venue skill into ~/.claude/skills/.
# Re-runnable: existing symlinks are replaced, but real directories are left alone.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"

mkdir -p "$TARGET_DIR"

link_dir() {
  local src="$1"
  local name
  name=$(basename "$src")
  local dst="$TARGET_DIR/$name"

  if [ -L "$dst" ]; then
    # symlink — refresh
    ln -sfn "$src" "$dst"
    echo "  refreshed: $dst -> $src"
  elif [ -e "$dst" ]; then
    # real file/dir — refuse to clobber
    echo "  SKIPPED (real path exists, not a symlink): $dst" >&2
    echo "  to overwrite: rm -rf '$dst' && rerun this script" >&2
    return 0
  else
    ln -s "$src" "$dst"
    echo "  linked: $dst -> $src"
  fi
}

echo "Linking devices/* skills into $TARGET_DIR/"
for d in "$REPO_ROOT"/devices/*/; do
  [ -d "$d" ] || continue
  [ -f "$d/SKILL.md" ] || { echo "  skip $(basename "$d") (no SKILL.md)"; continue; }
  link_dir "${d%/}"
done

echo "Linking venues/* skills into $TARGET_DIR/"
for d in "$REPO_ROOT"/venues/*/; do
  [ -d "$d" ] || continue
  [ -f "$d/SKILL.md" ] || continue
  link_dir "${d%/}"
done

echo "Linking adapters/* skills into $TARGET_DIR/"
for d in "$REPO_ROOT"/adapters/*/; do
  [ -d "$d" ] || continue
  [ -f "$d/SKILL.md" ] || { echo "  skip $(basename "$d") (no SKILL.md)"; continue; }
  link_dir "${d%/}"
done

echo "Linking adapters/*/mock skills into $TARGET_DIR/"
for d in "$REPO_ROOT"/adapters/*/mock/; do
  [ -d "$d" ] || continue
  [ -f "$d/SKILL.md" ] || continue
  # Use the name: field from SKILL.md as the symlink name (not the directory basename).
  skill_name=$(grep "^name:" "$d/SKILL.md" | head -1 | sed 's/name:[[:space:]]*//')
  [ -n "$skill_name" ] || skill_name=$(basename "$d")
  src="${d%/}"
  dst="$TARGET_DIR/$skill_name"
  if [ -L "$dst" ]; then
    ln -sfn "$src" "$dst" && echo "  refreshed: $dst -> $src"
  elif [ -e "$dst" ]; then
    echo "  SKIPPED (real path exists): $dst" >&2
  else
    ln -s "$src" "$dst" && echo "  linked: $dst -> $src"
  fi
done

echo "Linking agentic-commerce-skills/skills/**/* into $TARGET_DIR/"
for d in "$REPO_ROOT"/agentic-commerce-skills/skills/*/*/; do
  [ -d "$d" ] || continue
  [ -f "$d/SKILL.md" ] || { echo "  skip $(basename "$d") (no SKILL.md)"; continue; }
  link_dir "${d%/}"
done

echo
echo "Done. Verify with:"
echo "  ls -la $TARGET_DIR/ | grep -E 'weimi|wm800|ucp|ap2'"
