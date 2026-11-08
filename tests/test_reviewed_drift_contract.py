"""Pin v1 category debt honestly; this is not a candidate v2 implementation."""

import copy

import pytest

from boundver._baseline import (
    BaselineError, apply_baseline, baseline_context, baseline_change_ids,
    create_baseline, validate_baseline, violation_identity,
)


@pytest.fixture
def context():
    return baseline_context(
        config={"project": "reviewed-debt"},
        lockfile={"schema": "boundary-lock/v4", "config_contract": "boundver-semantic-config/v3"},
        source="head", components_filter=[], facets=["boundary"], transitive=False,
        facet_policy={"svc": ["boundary"]},
    )


DRIFT = "MISMATCH svc.boundary: locked=old current=first"


def test_v1_is_category_debt_not_exact_digest_acceptance(context):
    baseline = create_baseline(context, [DRIFT])
    second = "MISMATCH svc.boundary: locked=old current=second"
    assert violation_identity(DRIFT) == violation_identity(second)
    new, acknowledged, stale = apply_baseline(baseline, context, [second])
    assert new == [] and acknowledged == [second] and stale == []


@pytest.mark.parametrize("issue", [
    "METADATA MISMATCH config_digest: locked=old current=new",
    "METADATA MISMATCH svc.consumers: locked=['sdk'] current=[]",
    "METADATA MISMATCH svc.provider: locked=old current=new",
    "LOCKED DIGEST ERROR svc.boundary: malformed digest",
    "CONFIG ERROR: unknown consumer",
    "UNAVAILABLE svc.boundary: no digest",
    "ERROR: unsupported lock schema",
    "ERROR: provider failed",
    "ERROR: analysis budget exceeded",
])
def test_integrity_and_unclassified_issues_cannot_be_acknowledged(context, issue):
    assert violation_identity(issue) is None
    with pytest.raises(BaselineError, match="cannot baseline"):
        create_baseline(context, [DRIFT, issue])
    baseline = create_baseline(context, [DRIFT])
    new, acknowledged, _ = apply_baseline(baseline, context, [DRIFT, issue])
    assert new == [issue] and acknowledged == [DRIFT]


def test_new_subject_and_facet_remain_new_while_repaired_debt_is_stale(context):
    baseline = create_baseline(context, [DRIFT])
    new_issues = ["MISMATCH other.boundary: changed", "MISMATCH svc.compat: changed"]
    new, acknowledged, stale = apply_baseline(baseline, context, new_issues)
    assert new == new_issues and acknowledged == []
    assert stale == [baseline["violations"][0]["id"]]
    replacement = create_baseline(context, [])
    added, removed = baseline_change_ids(baseline, replacement)
    assert added == [] and removed == stale


@pytest.mark.parametrize("field,value", [
    ("source", "index"), ("lock_digest", "a" * 64),
    ("policy_digest", "b" * 64), ("transitive", True),
    ("components_filter", ["svc"]), ("facets", ["exact"]),
])
def test_v1_context_changes_do_not_inherit_debt(context, field, value):
    baseline = create_baseline(context, [DRIFT])
    changed = {**context, field: value}
    with pytest.raises(BaselineError, match="does not match"):
        apply_baseline(baseline, changed, [DRIFT])


@pytest.mark.parametrize("mutation", ["kind", "id", "duplicate", "extra"])
def test_corrupt_or_unknown_baseline_identity_is_rejected(context, mutation):
    baseline = copy.deepcopy(create_baseline(context, [DRIFT]))
    entry = baseline["violations"][0]
    if mutation == "kind":
        entry["kind"] = "config-digest-waiver"
    elif mutation == "id":
        entry["id"] = "0" * 64
    elif mutation == "duplicate":
        baseline["violations"].append(copy.deepcopy(entry))
    else:
        entry["new_digest"] = "f" * 64
    with pytest.raises(BaselineError):
        validate_baseline(baseline)
