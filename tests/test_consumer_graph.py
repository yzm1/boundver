"""Contracts for consumer traversal, closure slices, and component policies."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import boundver
import boundver.core as core
from boundver._config import validate_config
from boundver._consumer_graph import affected_consumers, consumer_closure
from boundver._lockfile import generate_lockfile, semantic_config_digest, verify_lockfile
from boundver._output import why_component
from boundver._utils import ConfigError


def _make_components(root: Path, names: tuple[str, ...]) -> None:
    for name in names:
        component = root / name
        component.mkdir()
        (component / "api.yaml").write_text(
            "openapi: 3.0.0\npaths: {}\n", encoding="utf-8"
        )
        (component / "impl.py").write_text("value = 1\n", encoding="utf-8")


def _component(path: str, **extra: object) -> dict:
    return {
        "path": path,
        "boundary": {"provider": "openapi", "paths": ["api.yaml"]},
        **extra,
    }


def _run_main(root: Path, *arguments: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    result = 0
    with (
        patch.object(sys, "argv", ["boundver", *arguments]),
        patch("boundver.core.git_root", return_value=root),
        redirect_stdout(stdout),
        redirect_stderr(stderr),
    ):
        try:
            core.main()
        except SystemExit as exc:
            result = int(exc.code or 0)
    return result, stdout.getvalue(), stderr.getvalue()


class ConsumerGraphTests(unittest.TestCase):
    def test_unknown_consumer_is_a_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("source",))
            config = {
                "project": "p",
                "components": {
                    "source": _component(
                        "source",
                        consumers=["NO-SUCH-COMPONENT"],
                        external_consumers=["outside-team"],
                    )
                },
                "slices": {},
            }

            errors = validate_config(config, root, source="working-tree")

            self.assertIn(
                "Component 'source' references unknown consumer: NO-SUCH-COMPONENT",
                errors,
            )
            config["components"]["source"]["consumers"] = []
            self.assertEqual(validate_config(config, root), [])

    def test_external_consumer_cannot_alias_a_configured_component(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("source", "client"))
            config = {
                "project": "p",
                "components": {
                    "source": _component(
                        "source", external_consumers=["client"]
                    ),
                    "client": _component("client"),
                },
                "slices": {},
            }

            errors = validate_config(config, root)

            self.assertTrue(
                any("declare it in 'consumers' instead" in error for error in errors),
                errors,
            )

    def test_transitive_closure_is_directional_deterministic_and_cycle_safe(self) -> None:
        components = {
            "layer": {
                "consumers": ["service-defs"],
                "external_consumers": ["layer-audit"],
            },
            "service-defs": {
                "consumers": ["platform-api"],
                "external_consumers": ["schema-audit"],
            },
            "platform-api": {
                "consumers": ["frontend-b", "frontend-a"],
                "external_consumers": ["partner-app"],
            },
            "frontend-a": {"consumers": ["service-defs"]},
            "frontend-b": {},
        }

        self.assertEqual(
            affected_consumers(components, "layer"),
            ["layer-audit", "service-defs"],
        )
        self.assertEqual(
            affected_consumers(components, "layer", transitive=True),
            [
                "frontend-a", "frontend-b", "layer-audit", "partner-app",
                "platform-api", "schema-audit", "service-defs",
            ],
        )
        self.assertEqual(
            consumer_closure(components, ["platform-api"], include_seeds=True),
            ["frontend-a", "frontend-b", "platform-api", "service-defs"],
        )

    def test_closure_slice_persists_resolved_component_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("client", "web", "mobile", "unrelated"))
            config = {
                "project": "p",
                "components": {
                    "client": _component("client", consumers=["web", "mobile"]),
                    "web": _component("web"),
                    "mobile": _component("mobile", consumers=["client"]),
                    "unrelated": _component("unrelated"),
                },
                "slices": {
                    "client-impact": {"mode": "exact", "closure_of": "client"}
                },
            }

            self.assertEqual(validate_config(config, root), [])
            lockfile = generate_lockfile(config, root, source="working-tree")

            self.assertEqual(
                lockfile["slices"]["client-impact"]["components"],
                ["client", "mobile", "web"],
            )
            self.assertNotIn("closure_of", lockfile["slices"]["client-impact"])

    def test_slice_requires_exactly_one_membership_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            base = {
                "project": "p",
                "components": {"svc": _component("svc")},
            }
            for definition in ({"mode": "exact"}, {
                "mode": "exact", "components": ["svc"], "closure_of": "svc"
            }):
                with self.subTest(definition=definition):
                    config = {**base, "slices": {"s": definition}}
                    errors = validate_config(config, root)
                    self.assertTrue(
                        any("exactly one of 'components' or 'closure_of'" in error for error in errors),
                        errors,
                    )

    def test_closure_membership_addition_is_updatable_drift_not_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("a", "b", "c"))
            config = {
                "project": "p",
                "components": {
                    "a": _component("a", consumers=["b"]),
                    "b": _component("b"),
                    "c": _component("c"),
                },
                "slices": {"impact": {"mode": "exact", "closure_of": "a"}},
            }
            lockfile = generate_lockfile(config, root, source="working-tree")
            (root / "boundary.lock.json").write_text(
                json.dumps(lockfile) + "\n", encoding="utf-8"
            )
            config["components"]["a"]["consumers"] = ["b", "c"]
            (root / "boundary.config.json").write_text(
                json.dumps(config) + "\n", encoding="utf-8"
            )

            result, stdout, stderr = _run_main(
                root,
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "exact",
                "--update",
                "--format",
                "json",
            )

            self.assertEqual(result, core.EXIT_OK, stderr)
            payload = json.loads(stdout)
            self.assertTrue(payload["updated"])
            self.assertFalse(
                any(issue.startswith("UNAVAILABLE FACET") for issue in payload["issues"])
            )
            updated = json.loads((root / "boundary.lock.json").read_text())
            self.assertEqual(updated["slices"]["impact"]["components"], ["a", "b", "c"])


class ConsumerImpactTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[dict, dict]:
        _make_components(root, ("api", "client", "frontend"))
        config = {
            "project": "p",
            "components": {
                "api": _component(
                    "api",
                    consumers=["client"],
                    external_consumers=["vendor-sdk"],
                ),
                "client": _component(
                    "client",
                    consumers=["frontend"],
                    external_consumers=["partner-app"],
                ),
                "frontend": _component("frontend"),
            },
            "slices": {},
        }
        lockfile = generate_lockfile(config, root, source="working-tree")
        (root / "api" / "api.yaml").write_text(
            "openapi: 3.0.0\npaths:\n  /changed: {}\n", encoding="utf-8"
        )
        return config, lockfile

    def test_verify_transitive_is_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, lockfile = self._fixture(root)

            direct = verify_lockfile(
                config, lockfile, root, source="working-tree", facets=["boundary"]
            )
            transitive = verify_lockfile(
                config,
                lockfile,
                root,
                source="working-tree",
                facets=["boundary"],
                transitive_consumers=True,
            )

            self.assertIn("AFFECTED CONSUMERS api: client, vendor-sdk", direct)
            self.assertNotIn("frontend", "\n".join(direct))
            self.assertIn(
                "AFFECTED CONSUMERS (TRANSITIVE) api: client, frontend, "
                "partner-app, vendor-sdk",
                transitive,
            )

    def test_why_json_contains_machine_readable_transitive_impact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, lockfile = self._fixture(root)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                result = why_component(
                    config,
                    lockfile,
                    root,
                    "api",
                    source="working-tree",
                    transitive_consumers=True,
                    output_format="json",
                )

            self.assertEqual(result, 1)
            payload = json.loads(stdout.getvalue())
            self.assertTrue(payload["drifted"])
            self.assertEqual(
                payload["affected_consumers"],
                ["client", "frontend", "partner-app", "vendor-sdk"],
            )
            self.assertTrue(payload["transitive_consumers"])

    def test_verify_cli_transitive_reports_full_closure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, lockfile = self._fixture(root)
            (root / "boundary.config.json").write_text(
                json.dumps(config) + "\n", encoding="utf-8"
            )
            (root / "boundary.lock.json").write_text(
                json.dumps(lockfile) + "\n", encoding="utf-8"
            )

            result, stdout, stderr = _run_main(
                root,
                "verify",
                "--source",
                "working-tree",
                "--facets",
                "boundary",
                "--transitive",
                "--format",
                "json",
            )

            self.assertEqual(result, core.EXIT_BOUNDARY, stderr)
            payload = json.loads(stdout)
            self.assertIn(
                "AFFECTED CONSUMERS (TRANSITIVE) api: client, frontend, "
                "partner-app, vendor-sdk",
                payload["issues"],
            )
            self.assertEqual(payload["facets"], ["boundary"])
            self.assertEqual(
                payload["facet_policy"]["components"]["api"], ["boundary"]
            )

    def test_slice_cli_json_has_resolved_members(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("client", "web"))
            config = {
                "project": "p",
                "components": {
                    "client": _component("client", consumers=["web"]),
                    "web": _component("web"),
                },
                "slices": {
                    "impact": {"mode": "exact", "closure_of": "client"}
                },
            }
            lockfile = generate_lockfile(config, root, source="working-tree")
            (root / "boundary.lock.json").write_text(
                json.dumps(lockfile) + "\n", encoding="utf-8"
            )

            result, stdout, stderr = _run_main(
                root, "slice", "impact", "--format", "json"
            )

            self.assertEqual(result, 0, stderr)
            payload = json.loads(stdout)
            self.assertEqual(
                [component["name"] for component in payload["components"]],
                ["client", "web"],
            )


class ComponentFacetPolicyTests(unittest.TestCase):
    def test_strict_slice_validation_contains_unhashable_values(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            config = {
                "project": "p",
                "components": {
                    "svc": {
                        "path": "svc",
                        "boundary": {"provider": []},
                    }
                },
                "slices": {
                    "public": {"mode": [], "components": ["svc"]}
                },
            }

            errors = validate_config(config, root, require_slice_facets=True)

            self.assertTrue(
                any("unknown mode" in error for error in errors), errors
            )
            self.assertTrue(
                any("provider" in error for error in errors), errors
            )

    def test_strict_slice_validation_rejects_unavailable_member_facets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("api", "leaf", "implicit"))
            config = {
                "project": "p",
                "components": {
                    "api": _component("api", consumers=["leaf"]),
                    "leaf": {
                        "path": "leaf",
                        "boundary": {"provider": "leaf"},
                    },
                    "implicit": {
                        "path": "implicit",
                        "boundary": {"provider": "implicit"},
                    },
                },
                "slices": {
                    "leaf-boundary": {
                        "mode": "boundary",
                        "components": ["leaf"],
                    },
                    "implicit-boundary": {
                        "mode": "boundary",
                        "components": ["implicit"],
                    },
                    "missing-behavior": {
                        "mode": "behavior",
                        "components": ["api"],
                    },
                    "missing-compat": {
                        "mode": "compat",
                        "components": ["api"],
                    },
                    "boundary-closure": {
                        "mode": "boundary",
                        "closure_of": "api",
                    },
                    "exact-closure": {
                        "mode": "exact",
                        "closure_of": "api",
                    },
                },
            }

            # The default remains compatible with generate(strict=False).
            self.assertEqual(validate_config(config, root), [])

            errors = validate_config(
                config, root, require_slice_facets=True
            )
            unavailable = [
                error for error in errors if "to supply that facet" in error
            ]

            self.assertEqual(len(unavailable), 5, errors)
            self.assertTrue(
                any(
                    "Slice 'leaf-boundary'" in error
                    and "provider 'leaf'" in error
                    for error in unavailable
                ),
                unavailable,
            )
            self.assertTrue(
                any(
                    "Slice 'implicit-boundary'" in error
                    and "provider 'implicit'" in error
                    for error in unavailable
                ),
                unavailable,
            )
            self.assertTrue(
                any("no non-empty behavior.paths" in error for error in unavailable),
                unavailable,
            )
            self.assertTrue(
                any("no version_source" in error for error in unavailable),
                unavailable,
            )
            self.assertTrue(
                any(
                    "Slice 'boundary-closure'" in error
                    and "component 'leaf'" in error
                    for error in unavailable
                ),
                unavailable,
            )
            self.assertFalse(
                any("Slice 'exact-closure'" in error for error in errors),
                errors,
            )

    def test_component_note_is_validated_as_presentation_text(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            config = {
                "project": "p",
                "components": {
                    "svc": _component(
                        "svc", note="Owned by the checkout platform team"
                    )
                },
                "slices": {},
            }

            self.assertEqual(validate_config(config, root), [])
            config["components"]["svc"]["note"] = ["not", "text"]
            errors = validate_config(config, root)
            self.assertTrue(
                any("field 'note' must be a string" in error for error in errors),
                errors,
            )

    def test_policy_free_leaf_slice_gates_only_available_facets(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("a", "b"))
            config = {
                "project": "p",
                "components": {
                    "a": {"path": "a", "boundary": {"provider": "leaf"}},
                    "b": {"path": "b", "boundary": {"provider": "leaf"}},
                },
                "slices": {
                    "partial-boundary": {
                        "mode": "boundary",
                        "components": ["a"],
                    }
                },
            }
            lockfile = generate_lockfile(
                config, root, source="working-tree", strict=False
            )
            config["slices"]["partial-boundary"]["components"] = ["a", "b"]
            observations: list[str] = []

            issues = verify_lockfile(
                config,
                lockfile,
                root,
                source="working-tree",
                observations=observations,
            )
            policy = core._facet_policy_payload(config, None)

            self.assertIsNone(policy["defaults"])
            self.assertEqual(policy["components"], {"a": ["exact"], "b": ["exact"]})
            self.assertFalse(policy["slices"]["partial-boundary"]["gated"])
            self.assertFalse(any("UNAVAILABLE FACET" in issue for issue in issues))
            self.assertFalse(any("SLICE MISMATCH" in issue for issue in issues))
            self.assertTrue(
                any("SLICE MISMATCH partial-boundary.boundary" in item for item in observations),
                observations,
            )

    def test_explicit_impossible_component_gates_fail_validation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            cases = {
                "compat": _component("svc", verify_facets=["compat"]),
                "behavior": _component("svc", verify_facets=["behavior"]),
                "boundary": {
                    "path": "svc",
                    "boundary": {"provider": "leaf"},
                    "verify_facets": ["boundary"],
                },
            }

            for facet, component in cases.items():
                with self.subTest(facet=facet):
                    config = {
                        "project": "p",
                        "components": {"svc": component},
                        "slices": {},
                    }
                    errors = validate_config(config, root)
                    self.assertTrue(
                        any(
                            f"explicitly gates '{facet}'" in error
                            for error in errors
                        ),
                        errors,
                    )

    def test_component_override_replaces_explicit_impossible_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            config = {
                "project": "p",
                "defaults": {"verify_facets": ["compat"]},
                "components": {
                    "svc": _component("svc", verify_facets=["exact"])
                },
                "slices": {},
            }

            self.assertEqual(validate_config(config, root), [])

    def test_null_facet_slice_is_valid_configuration_for_allow_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            config = {
                "project": "p",
                "components": {
                    "svc": {"path": "svc", "boundary": {"provider": "leaf"}}
                },
                "slices": {
                    "boundary": {
                        "mode": "boundary",
                        "components": ["svc"],
                    }
                },
            }

            self.assertEqual(validate_config(config, root), [])
            strict_errors = validate_config(
                config, root, require_slice_facets=True
            )
            self.assertTrue(
                any(
                    "Slice 'boundary' mode 'boundary'" in error
                    and "provider 'leaf'" in error
                    for error in strict_errors
                ),
                strict_errors,
            )
            with self.assertRaisesRegex(ConfigError, "requires boundary digest"):
                generate_lockfile(config, root, source="working-tree", strict=True)
            partial = generate_lockfile(
                config, root, source="working-tree", strict=False
            )
            self.assertIsNone(
                partial["slices"]["boundary"]["component_digests"]["svc"]
            )

    def test_full_verify_uses_each_component_policy_and_cli_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("boundary-gated", "exact-gated"))
            config = {
                "project": "p",
                "defaults": {"verify_facets": ["boundary"]},
                "components": {
                    "boundary-gated": _component(
                        "boundary-gated", verify_facets=["boundary"]
                    ),
                    "exact-gated": _component(
                        "exact-gated", verify_facets=["exact"]
                    ),
                },
                "slices": {},
            }
            self.assertEqual(validate_config(config, root), [])
            lockfile = generate_lockfile(config, root, source="working-tree")
            for name in config["components"]:
                (root / name / "impl.py").write_text("value = 2\n", encoding="utf-8")

            observations: list[str] = []
            issues = verify_lockfile(
                config,
                lockfile,
                root,
                source="working-tree",
                observations=observations,
            )

            self.assertTrue(any("MISMATCH exact-gated.exact" in issue for issue in issues))
            self.assertFalse(any("boundary-gated.exact" in issue for issue in issues))
            self.assertTrue(
                any("MISMATCH boundary-gated.exact" in item for item in observations)
            )

            override_observations: list[str] = []
            override_issues = verify_lockfile(
                config,
                lockfile,
                root,
                source="working-tree",
                facets=["boundary"],
                observations=override_observations,
            )
            self.assertEqual(override_issues, [])
            self.assertEqual(len(override_observations), 2)

    def test_component_policy_is_order_insensitive_but_changes_config_digest(self) -> None:
        base = {
            "project": "p",
            "components": {
                "svc": _component("svc", verify_facets=["exact", "boundary"])
            },
            "slices": {},
        }
        reordered = json.loads(json.dumps(base))
        reordered["components"]["svc"]["verify_facets"] = ["boundary", "exact"]
        narrowed = json.loads(json.dumps(base))
        narrowed["components"]["svc"]["verify_facets"] = ["boundary"]
        with_external = json.loads(json.dumps(base))
        with_external["components"]["svc"]["external_consumers"] = ["vendor"]

        self.assertEqual(semantic_config_digest(base), semantic_config_digest(reordered))
        self.assertNotEqual(semantic_config_digest(base), semantic_config_digest(narrowed))
        self.assertNotEqual(
            semantic_config_digest(base), semantic_config_digest(with_external)
        )

    def test_implicit_available_policy_is_distinct_from_explicit_all_facets(self) -> None:
        implicit = {
            "project": "p",
            "components": {"svc": _component("svc")},
            "slices": {},
        }
        explicit_all = json.loads(json.dumps(implicit))
        explicit_all["defaults"] = {
            "verify_facets": ["exact", "behavior", "boundary", "compat"]
        }

        self.assertNotEqual(
            semantic_config_digest(implicit),
            semantic_config_digest(explicit_all),
        )

    def test_public_verify_preserves_component_override_when_facets_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_components(root, ("svc",))
            config = {
                "project": "p",
                "defaults": {"verify_facets": ["boundary"]},
                "components": {
                    "svc": _component("svc", verify_facets=["exact"])
                },
                "slices": {},
            }
            (root / "boundary.config.json").write_text(
                json.dumps(config) + "\n", encoding="utf-8"
            )
            with patch("boundver._git.git_root", return_value=root):
                boundver.generate(source="working-tree")
                (root / "svc" / "impl.py").write_text(
                    "value = 2\n", encoding="utf-8"
                )
                issues = boundver.verify(source="working-tree")

            self.assertTrue(
                any("MISMATCH svc.exact" in issue for issue in issues), issues
            )
            result, stdout, stderr = _run_main(
                root,
                "verify",
                "--source",
                "working-tree",
                "--format",
                "json",
            )
            self.assertEqual(result, core.EXIT_DRIFT, stderr)
            payload = json.loads(stdout)
            self.assertIsNone(payload["facets"])
            self.assertEqual(payload["facet_policy"]["components"]["svc"], ["exact"])
            self.assertTrue(
                any("MISMATCH svc.exact" in issue for issue in payload["issues"]),
                payload,
            )

    def test_slice_policy_is_scoped_to_its_resolved_members(self) -> None:
        for member_facets, expected_gated in ((["exact"], True), (["boundary"], False)):
            with self.subTest(member_facets=member_facets), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                _make_components(root, ("member", "unrelated"))
                config = {
                    "project": "p",
                    "defaults": {"verify_facets": ["boundary"]},
                    "components": {
                        "member": _component(
                            "member", verify_facets=member_facets
                        ),
                        # This exact override must not leak into member-slice.
                        "unrelated": _component(
                            "unrelated", verify_facets=["exact"]
                        ),
                    },
                    "slices": {
                        "member-slice": {
                            "mode": "exact",
                            "components": ["member"],
                        }
                    },
                }
                lockfile = generate_lockfile(config, root, source="working-tree")
                (root / "member" / "impl.py").write_text(
                    "value = 2\n", encoding="utf-8"
                )
                observations: list[str] = []

                issues = verify_lockfile(
                    config,
                    lockfile,
                    root,
                    source="working-tree",
                    observations=observations,
                )
                policy = core._facet_policy_payload(config, None)

                self.assertEqual(
                    policy["slices"]["member-slice"]["gated"], expected_gated
                )
                self.assertEqual(
                    any("SLICE MISMATCH member-slice.exact" in issue for issue in issues),
                    expected_gated,
                )
                self.assertEqual(
                    any(
                        "SLICE MISMATCH member-slice.exact" in item
                        for item in observations
                    ),
                    not expected_gated,
                )


if __name__ == "__main__":
    unittest.main()
