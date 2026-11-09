"""Execute the published historical demo, including its pinned Git identities."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_public_range_review_demo_preserves_its_fixture_and_documented_provenance():
    fixture = ROOT / "examples/range-review/fixture/boundary.config.json"
    before = fixture.read_bytes()
    try:
        result = subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts/demo_range_review.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    finally:
        assert fixture.read_bytes() == before
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Demo passed: current drift and reconciled historical review agree." in result.stdout
