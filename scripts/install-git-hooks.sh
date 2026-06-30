#!/usr/bin/env bash
#
# Install the Hey Mr. Postman git safety hooks into a repo's .git/hooks.
# Works for the main clone AND for the local-only deploy repo.
#
# Usage:
#   scripts/install-git-hooks.sh           # install into the current repo
#   scripts/install-git-hooks.sh <gitdir>  # install into another repo's worktree
#
set -euo pipefail

target="${1:-.}"
src="$(cd "$(dirname "$0")/git-hooks" && pwd)"
hooks_dir="$(git -C "$target" rev-parse --git-path hooks)"
hooks_dir="$(cd "$target" && cd "$hooks_dir" && pwd)"

mkdir -p "$hooks_dir"
for hook in pre-commit pre-push; do
  install -m 0755 "$src/$hook" "$hooks_dir/$hook"
  echo "installed $hook -> $hooks_dir/$hook"
done
echo "Done. Guards active for commits/pushes in: $target"