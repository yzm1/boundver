#!/usr/bin/env bash
set -euo pipefail

python -m pip install build
python -m build

set -- dist/*.whl
if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Expected exactly one wheel in dist/" >&2
  exit 1
fi
wheel_path="$PWD/$1"

set -- dist/*.tar.gz
if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "Expected exactly one source distribution in dist/" >&2
  exit 1
fi
sdist_path="$PWD/$1"

smoke_root=$(mktemp -d)
trap 'rm -rf "$smoke_root"' EXIT
python -m venv "$smoke_root/venv"
venv_python="$smoke_root/venv/bin/python"
"$venv_python" -m pip install "$wheel_path"

"$venv_python" - "$wheel_path" "$sdist_path" <<'PY'
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
with ZipFile(wheel) as archive:
    wheel_members = set(archive.namelist())
required_wheel = {
    "boundver/__main__.py",
    "boundver/boundary.config.schema.json",
    "boundver/py.typed",
}
missing_wheel = required_wheel - wheel_members
if missing_wheel:
    raise SystemExit(f"wheel missing: {sorted(missing_wheel)}")

with tarfile.open(sdist) as archive:
    sdist_members = set(archive.getnames())
prefix = next(name.split("/", 1)[0] for name in sdist_members if "/" in name)
required_sdist = {
    f"{prefix}/CODE_OF_CONDUCT.md",
    f"{prefix}/SECURITY.md",
    f"{prefix}/SUPPORT.md",
    f"{prefix}/docs/PROJECT_REVIEW.md",
    f"{prefix}/spec/HASHING.md",
    f"{prefix}/tests/conftest.py",
}
missing_sdist = required_sdist - sdist_members
if missing_sdist:
    raise SystemExit(f"sdist missing: {sorted(missing_sdist)}")
PY

repo="$smoke_root/repo"
mkdir "$repo"
git -C "$repo" init -q
git -C "$repo" config user.email smoke@example.com
git -C "$repo" config user.name "Packaging Smoke"
mkdir "$repo/svc"
printf '{"contract": 1}\n' > "$repo/svc/api.json"
git -C "$repo" add svc/api.json
git -C "$repo" commit -qm baseline
printf '%s\n' \
  '{' \
  '  "project": "packaging-smoke",' \
  '  "components": {' \
  '    "svc": {' \
  '      "path": "svc",' \
  '      "version_source": null,' \
  '      "boundary": {"provider": "json-file", "paths": ["api.json"]}' \
  '    }' \
  '  },' \
  '  "slices": {}' \
  '}' > "$repo/boundary.config.json"

cd "$repo"
"$venv_python" -m boundver --version
"$venv_python" -m boundver validate-config
"$venv_python" -m boundver generate --source head --format json >/dev/null
"$venv_python" -m boundver verify --source head --format json >/dev/null
"$venv_python" -m boundver status --source head --format json >/dev/null
