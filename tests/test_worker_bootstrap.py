"""Worker imports must follow the running artifact, including zip archives."""

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from boundver import _worker_bootstrap as bootstrap


@pytest.mark.parametrize("layout", ["directory", "zip"])
def test_bootstrap_runs_its_own_artifact_not_cwd_or_ambient_install(tmp_path, monkeypatch, layout):
    sources = {
        "boundver/__init__.py": "",
        "boundver/_generation_worker.py": (
            "import json,pathlib,sys\n"
            "root=pathlib.Path(sys.argv[1])\n"
            "(root/'worker.json').write_text(json.dumps({'origin':__file__,'argv':sys.argv}),encoding='utf-8')\n"
        ),
    }
    artifact = tmp_path / ("runtime archive \u03bb.pyz" if layout == "zip" else "runtime directory")
    if layout == "zip":
        with zipfile.ZipFile(artifact, "w") as archive:
            for name, text in sources.items():
                archive.writestr(name, text)
    else:
        for name, text in sources.items():
            path = artifact / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(bootstrap, "__file__", str(artifact / "boundver" / "_worker_bootstrap.py"))
    unrelated = tmp_path / "untrusted checkout"
    (unrelated / "boundver").mkdir(parents=True)
    (unrelated / "boundver" / "__init__.py").write_text("raise RuntimeError('cwd shadow imported')", encoding="utf-8")
    transfer = tmp_path / "private transfer"
    transfer.mkdir()
    completed = subprocess.run(
        [sys.executable, *bootstrap.worker_arguments(transfer)],
        cwd=unrelated, env={**os.environ, "PYTHONPATH": str(unrelated)},
        capture_output=True, timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    report = json.loads((transfer / "worker.json").read_text(encoding="utf-8"))
    assert report["origin"].startswith(str(artifact))
    assert report["argv"] == ["boundver._generation_worker", str(transfer)]
    assert not completed.stdout and not completed.stderr


def test_bootstrap_rejects_relative_transfer_directory():
    with pytest.raises(ValueError, match="absolute"):
        bootstrap.worker_arguments(Path("relative"))
