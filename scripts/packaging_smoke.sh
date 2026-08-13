#!/usr/bin/env bash
set -euo pipefail

# The release build runs on Python 3.12. Pin the frontend and backend so a
# later workflow rerun cannot silently change artifact contents.
python -m pip install \
  'build==1.5.0' 'setuptools==84.0.0' 'wheel==0.48.0'
# Generated backend state must not shadow the `build` frontend or leak stale
# package metadata into a repeat smoke run.
rm -rf -- dist build src/boundver.egg-info
python scripts/build_release_artifacts.py --output-dir dist

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

expected_version=$(python - <<'PY'
import pathlib
try:
    import tomllib
except ImportError:
    import tomli as tomllib

print(tomllib.loads(pathlib.Path("pyproject.toml").read_text())["project"]["version"])
PY
)

smoke_root=$(mktemp -d)
trap 'rm -rf "$smoke_root"' EXIT
pyz_path="$PWD/dist/boundver-$expected_version.pyz"
if [[ ! -f "$pyz_path" ]]; then
  echo "Expected reproducible standalone archive at $pyz_path" >&2
  exit 1
fi

python - "$wheel_path" "$sdist_path" "$pyz_path" "$expected_version" "$PWD/LICENSE" <<'PY'
import email.parser
import sys
import tarfile
from pathlib import Path
from zipfile import ZipFile

wheel = Path(sys.argv[1])
sdist = Path(sys.argv[2])
pyz = Path(sys.argv[3])
expected_version = sys.argv[4]
license_bytes = Path(sys.argv[5]).read_bytes()

with ZipFile(wheel) as archive:
    wheel_members = set(archive.namelist())
    wheel_metadata_name = next(
        name for name in wheel_members if name.endswith(".dist-info/METADATA")
    )
    wheel_metadata = email.parser.BytesParser().parsebytes(
        archive.read(wheel_metadata_name)
    )
required_wheel = {
    "boundver/__main__.py",
    "boundver/boundary.config.schema.json",
    "boundver/py.typed",
}
missing_wheel = required_wheel - wheel_members
if missing_wheel:
    raise SystemExit(f"wheel missing: {sorted(missing_wheel)}")
if wheel_metadata["Version"] != expected_version:
    raise SystemExit(
        f"wheel version {wheel_metadata['Version']!r} != {expected_version!r}"
    )
if not any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_members):
    raise SystemExit("wheel missing license file")

with tarfile.open(sdist) as archive:
    sdist_members = set(archive.getnames())
prefix = next(name.split("/", 1)[0] for name in sdist_members if "/" in name)
required_sdist = {
    f"{prefix}/CODE_OF_CONDUCT.md",
    f"{prefix}/SECURITY.md",
    f"{prefix}/SUPPORT.md",
    f"{prefix}/docs/getting-started.md",
    f"{prefix}/spec/HASHING.md",
    f"{prefix}/spec/cli-output.slice.schema.json",
    f"{prefix}/spec/cli-output.why.schema.json",
}
missing_sdist = required_sdist - sdist_members
if missing_sdist:
    raise SystemExit(f"sdist missing: {sorted(missing_sdist)}")
for internal in (
    f"{prefix}/docs/PROJECT_REVIEW.md",
    f"{prefix}/docs/RELEASING.md",
    f"{prefix}/tests",
    f"{prefix}/scripts",
    f"{prefix}/.github",
    f"{prefix}/.pre-commit-hooks.yaml",
    f"{prefix}/Dockerfile",
    f"{prefix}/action.yml",
):
    if internal in sdist_members or any(
        name.startswith(internal + "/") for name in sdist_members
    ):
        raise SystemExit(f"sdist contains repository-only material: {internal}")

with ZipFile(pyz) as archive:
    pyz_members = set(archive.namelist())
    metadata_name = next(
        (name for name in pyz_members if name.endswith(".dist-info/METADATA")),
        None,
    )
    if metadata_name is None:
        raise SystemExit("standalone archive missing distribution metadata")
    metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_name))
    if metadata["Name"] != "boundver" or metadata["Version"] != expected_version:
        raise SystemExit(
            "standalone metadata mismatch: "
            f"name={metadata['Name']!r} version={metadata['Version']!r}"
        )
    if archive.read("LICENSE") != license_bytes:
        raise SystemExit("standalone archive license does not match repository LICENSE")
    if not any(name.endswith(".dist-info/licenses/LICENSE") for name in pyz_members):
        raise SystemExit("standalone archive missing distribution license")
PY

wheel_venv="$smoke_root/wheel-venv"
sdist_venv="$smoke_root/sdist-venv"
standalone_venv="$smoke_root/standalone-venv"
python -m venv "$wheel_venv"
python -m venv "$sdist_venv"
python -m venv "$standalone_venv"
wheel_python="$wheel_venv/bin/python"
sdist_python="$sdist_venv/bin/python"
standalone_python="$standalone_venv/bin/python"
"$wheel_python" -m pip install "$wheel_path"
"$sdist_python" -m pip install "$sdist_path"

repo="$smoke_root/repo"
mkdir "$repo"
git -C "$repo" init -q
git -C "$repo" config user.email smoke@example.com
git -C "$repo" config user.name "Packaging Smoke"
mkdir "$repo/svc"
printf '{"contract": 1}\n' > "$repo/svc/api.json"
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
git -C "$repo" add svc/api.json boundary.config.json
git -C "$repo" commit -qm baseline

run_installed_smoke() {
  local runtime=$1
  "$runtime" -m boundver --version | grep -F " $expected_version" >/dev/null
  "$runtime" -m boundver validate-config
  "$runtime" -m boundver generate --source head --format json >/dev/null
  "$runtime" -m boundver verify --source head --format json >/dev/null
  "$runtime" -m boundver status --source head --format json >/dev/null
}

cd "$repo"
# Bootstrap and commit the lock once. Head/index verification intentionally
# reads its lock from that selected Git snapshot, not from an unstaged file
# just written by generate.
"$wheel_python" -m boundver validate-config
"$wheel_python" -m boundver generate --source head --format json >/dev/null
git add boundary.lock.json
git commit -qm "record lock"
run_installed_smoke "$wheel_python"
run_installed_smoke "$sdist_python"
"$standalone_python" "$pyz_path" --version | grep -F " $expected_version" >/dev/null
"$standalone_python" "$pyz_path" validate-config
"$standalone_python" "$pyz_path" generate --source head --format json >/dev/null
"$standalone_python" "$pyz_path" verify --source head --format json >/dev/null
"$standalone_python" - "$pyz_path" "$expected_version" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import boundver

if boundver.__version__ != sys.argv[2]:
    raise SystemExit(
        f"standalone package version {boundver.__version__!r} != {sys.argv[2]!r}"
    )
PY
