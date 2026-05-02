#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${1:-boundary.config.json}"
LOCK_PATH="${2:-boundary.lock.json}"

if ! command -v jq >/dev/null 2>&1; then
  echo "ERROR: jq is required" >&2
  exit 2
fi
if ! command -v sha256sum >/dev/null 2>&1; then
  echo "ERROR: sha256sum is required" >&2
  exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

hash_from_paths() {
  local -a paths=("$@")
  if [ ${#paths[@]} -eq 0 ]; then
    printf ""
    return 0
  fi
  local tmp
  tmp="$(mktemp)"
  : > "$tmp"
  for rel in "${paths[@]}"; do
    local content
    content="$(git show "HEAD:${rel}" 2>/dev/null || true)"
    if [ -z "$content" ] && ! git cat-file -e "HEAD:${rel}" 2>/dev/null; then
      continue
    fi
    printf "file:%s\n" "${rel}" >> "$tmp"
    git show "HEAD:${rel}" >> "$tmp"
  done
  if [ ! -s "$tmp" ]; then
    rm -f "$tmp"
    printf ""
    return 0
  fi
  sha256sum "$tmp" | awk '{print $1}'
  rm -f "$tmp"
}

status=0

mapfile -t component_names < <(jq -r '.components | keys[]' "$CONFIG_PATH")

for name in "${component_names[@]}"; do
  comp_path="$(jq -r --arg n "$name" '.components[$n].path' "$CONFIG_PATH")"
  mapfile -t exact_files < <(git ls-tree -r --name-only HEAD "$comp_path" | sed '/^$/d')
  exact_hash="$(hash_from_paths "${exact_files[@]}")"

  mapfile -t boundary_rel < <(jq -r --arg n "$name" '.components[$n].boundary.paths[]? // empty' "$CONFIG_PATH")
  boundary_files=()
  for rel in "${boundary_rel[@]}"; do
    joined="${comp_path%/}/${rel}"
    while IFS= read -r f; do
      [ -n "$f" ] && boundary_files+=("$f")
    done < <(git ls-tree -r --name-only HEAD "$joined")
    if [ ${#boundary_files[@]} -eq 0 ] && git cat-file -e "HEAD:${joined}" 2>/dev/null; then
      boundary_files+=("$joined")
    fi
  done
  boundary_hash="$(hash_from_paths "${boundary_files[@]}")"

  lock_exact="$(jq -r --arg n "$name" '.components[$n].fingerprints.exact // ""' "$LOCK_PATH")"
  lock_boundary="$(jq -r --arg n "$name" '.components[$n].fingerprints.boundary // ""' "$LOCK_PATH")"

  if [ "$exact_hash" != "$lock_exact" ]; then
    echo "MISMATCH ${name}.exact"
    status=1
  fi
  if [ "$boundary_hash" != "$lock_boundary" ]; then
    echo "MISMATCH ${name}.boundary"
    status=1
  fi

done

if [ $status -eq 0 ]; then
  echo "OK: lockfile matches HEAD for exact/boundary"
fi
exit $status
