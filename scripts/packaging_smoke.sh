#!/usr/bin/env bash
set -euo pipefail

python -m pip install build
python -m build
python -m venv /tmp/boundver-smoke
/tmp/boundver-smoke/bin/pip install dist/*.whl
/tmp/boundver-smoke/bin/boundver --help
/tmp/boundver-smoke/bin/boundver init --out /tmp/boundary.config.json --force
