from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_required_ci_results.py"
SHA = "a" * 40
TARGET_URL = "https://github.com/yzm1/boundver/actions/runs/123"
DESCRIPTION = "All merge-critical CI jobs passed under base-controlled policy."


def _load_script():
    spec = importlib.util.spec_from_file_location("check_required_ci_results", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load required CI gate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RequiredCiGateClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = _load_script()
        self.client = self.gate.GitHubClient("token")

    def _response(self, **overrides):
        response = {
            "state": "success",
            "context": self.gate.STATUS_CONTEXT,
            "description": DESCRIPTION,
            "target_url": TARGET_URL,
            "url": (
                f"{self.gate.API_ROOT}/repos/{self.gate.REPOSITORY}/statuses/{SHA}"
            ),
        }
        response.update(overrides)
        return response

    def test_status_accepts_the_documented_response_without_a_sha_field(self) -> None:
        with mock.patch.object(
            self.client, "_request", return_value=self._response()
        ) as request:
            self.client.status(
                SHA,
                state="success",
                description=DESCRIPTION,
                target_url=TARGET_URL,
            )

        request.assert_called_once_with(
            "POST",
            f"/repos/{self.gate.REPOSITORY}/statuses/{SHA}",
            {
                "state": "success",
                "context": self.gate.STATUS_CONTEXT,
                "description": DESCRIPTION,
                "target_url": TARGET_URL,
            },
        )

    def test_status_rejects_a_response_for_another_commit(self) -> None:
        with mock.patch.object(
            self.client,
            "_request",
            return_value=self._response(
                url=(
                    f"{self.gate.API_ROOT}/repos/{self.gate.REPOSITORY}/statuses/"
                    + "b" * 40
                )
            ),
        ):
            with self.assertRaisesRegex(
                self.gate.RequiredCiGateError, "mismatched commit status"
            ):
                self.client.status(
                    SHA,
                    state="success",
                    description=DESCRIPTION,
                    target_url=TARGET_URL,
                )


if __name__ == "__main__":
    unittest.main()
