"""Bounded, typed structural explanations for boundary-provider output.

The range-review host owns the source identities and provider bindings.  A
provider that implements :class:`StructuralDiffProvider` receives the same two
immutable source contexts used for endpoint verification and returns only
structural evidence.  The result deliberately contains paths and JSON types,
not source values and not a compatibility verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, List, Optional, Protocol, Sequence, Tuple, runtime_checkable

from ._structured_data import StrictJSONError, strict_json_loads
from ._utils import GuardrailError, ProviderError


MAX_PROVIDER_DIFF_INPUT_BYTES = 512 * 1024 * 1024
MAX_PROVIDER_DIFF_WORK_STEPS = 250_000
MAX_PROVIDER_DIFF_ROWS = 20_000
MAX_PROVIDER_DIFF_RESULT_BYTES = 16 * 1024 * 1024
MAX_PROVIDER_DIFF_DEPTH = 64
MAX_PROVIDER_DIFF_PATH_BYTES = 16 * 1024
STRUCTURAL_DIFF_INTERFACE = "boundver-structural-diff/v1"

_JSON_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


@dataclass(frozen=True)
class StructuralChange:
    """One value-free structural transition at an RFC 6901 JSON pointer."""

    kind: str
    path: str
    before_type: Optional[str]
    after_type: Optional[str]


@dataclass(frozen=True)
class StructuralDocumentDiff:
    """Complete structural changes for one deterministic provider entry."""

    label: str
    status: str
    changes: Tuple[StructuralChange, ...]


@dataclass(frozen=True)
class StructuralDiffResult:
    """Complete typed result returned by a structural-diff provider."""

    documents: Tuple[StructuralDocumentDiff, ...]


@runtime_checkable
class StructuralDiffProvider(Protocol):
    """Optional provider interface used by historical range review.

    Implementations must spend from the host-supplied budget before retaining
    work or output.  Raising :class:`GuardrailError` means no partial provider
    result is authoritative; the host reports an incomplete explanation with
    an empty document list.
    """

    structural_diff_interface: str

    def structural_diff(
        self,
        before_ctx: Any,
        after_ctx: Any,
        budget: "StructuralDiffBudget",
    ) -> StructuralDiffResult:
        ...  # pragma: no cover


class StructuralDiffBudget:
    """One aggregate input, traversal, and output budget for a review."""

    def __init__(
        self,
        *,
        max_input_bytes: Optional[int] = None,
        max_work_steps: Optional[int] = None,
        max_rows: Optional[int] = None,
        max_result_bytes: Optional[int] = None,
        max_depth: Optional[int] = None,
        max_path_bytes: Optional[int] = None,
    ) -> None:
        self.max_input_bytes = (
            MAX_PROVIDER_DIFF_INPUT_BYTES
            if max_input_bytes is None
            else max_input_bytes
        )
        self.max_work_steps = (
            MAX_PROVIDER_DIFF_WORK_STEPS
            if max_work_steps is None
            else max_work_steps
        )
        self.max_rows = MAX_PROVIDER_DIFF_ROWS if max_rows is None else max_rows
        self.max_result_bytes = (
            MAX_PROVIDER_DIFF_RESULT_BYTES
            if max_result_bytes is None
            else max_result_bytes
        )
        self.max_depth = MAX_PROVIDER_DIFF_DEPTH if max_depth is None else max_depth
        self.max_path_bytes = (
            MAX_PROVIDER_DIFF_PATH_BYTES
            if max_path_bytes is None
            else max_path_bytes
        )
        limits = (
            self.max_input_bytes,
            self.max_work_steps,
            self.max_rows,
            self.max_result_bytes,
            self.max_depth,
            self.max_path_bytes,
        )
        if any(type(value) is not int or value < 0 for value in limits):
            raise ValueError("Structural diff limits must be non-negative integers")
        self.input_bytes = 0
        self.work_steps = 0
        self.result_rows = 0
        self.result_bytes = 0
        self.exhausted = False

    def _limit(self, detail: str) -> GuardrailError:
        self.exhausted = True
        return GuardrailError(
            "Structural boundary explanation exceeds the "
            f"{detail}. No partial structural result was emitted."
        )

    def reserve_input(self, label: str, content: bytes) -> None:
        amount = len(label.encode("utf-8", errors="strict")) + len(content)
        self.input_bytes += amount
        if self.input_bytes > self.max_input_bytes:
            raise self._limit(
                f"{self.max_input_bytes}-byte aggregate input limit"
            )

    def spend(self, *, depth: int) -> None:
        if depth > self.max_depth:
            raise self._limit(f"{self.max_depth}-level nesting limit")
        self.work_steps += 1
        if self.work_steps > self.max_work_steps:
            raise self._limit(
                f"{self.max_work_steps}-step aggregate work limit"
            )

    def pointer_child(self, parent: str, value: object) -> str:
        segment = str(value).replace("~", "~0").replace("/", "~1")
        child = f"{parent}/{segment}"
        if len(child.encode("utf-8", errors="strict")) > self.max_path_bytes:
            raise self._limit(
                f"{self.max_path_bytes}-byte JSON-pointer limit"
            )
        return child

    def change(
        self,
        *,
        kind: str,
        path: str,
        before_type: Optional[str],
        after_type: Optional[str],
    ) -> StructuralChange:
        if kind not in {"added", "removed", "changed"}:
            raise ProviderError(f"Unsupported structural change kind: {kind!r}")
        if before_type is not None and before_type not in _JSON_TYPES:
            raise ProviderError(f"Unsupported before JSON type: {before_type!r}")
        if after_type is not None and after_type not in _JSON_TYPES:
            raise ProviderError(f"Unsupported after JSON type: {after_type!r}")
        path_bytes = len(path.encode("utf-8", errors="strict"))
        if path_bytes > self.max_path_bytes:
            raise self._limit(
                f"{self.max_path_bytes}-byte JSON-pointer limit"
            )
        self.result_rows += 1
        self.result_bytes += path_bytes + 96
        if self.result_rows > self.max_rows:
            raise self._limit(f"{self.max_rows}-row aggregate output limit")
        if self.result_bytes > self.max_result_bytes:
            raise self._limit(
                f"{self.max_result_bytes}-byte aggregate output limit"
            )
        return StructuralChange(kind, path, before_type, after_type)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if value is True or value is False:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    raise ProviderError(
        f"Structural provider output contains unsupported type {type(value).__name__}"
    )


def _is_json_pointer(value: str) -> bool:
    if not value:
        return True
    if not value.startswith("/"):
        return False
    index = 0
    while True:
        index = value.find("~", index)
        if index < 0:
            return True
        if index + 1 >= len(value) or value[index + 1] not in {"0", "1"}:
            return False
        index += 2


def _parse_canonical_entry(content: bytes, label: str) -> Any:
    try:
        text = content.decode("utf-8")
        return strict_json_loads(text)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        StrictJSONError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ProviderError(
            f"Structural provider entry {label!r} is not canonical JSON: "
            f"{type(exc).__name__}"
        ) from exc


def _validate_tree(value: Any, budget: StructuralDiffBudget) -> None:
    pending: List[Tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        budget.spend(depth=depth)
        value_type = _json_type(item)
        if value_type == "object":
            pending.extend((child, depth + 1) for child in item.values())
        elif value_type == "array":
            pending.extend((child, depth + 1) for child in item)


def _diff_json(
    before: Any,
    after: Any,
    *,
    path: str,
    depth: int,
    budget: StructuralDiffBudget,
    changes: List[StructuralChange],
) -> None:
    budget.spend(depth=depth)
    before_type = _json_type(before)
    after_type = _json_type(after)
    if before_type != after_type:
        changes.append(
            budget.change(
                kind="changed",
                path=path,
                before_type=before_type,
                after_type=after_type,
            )
        )
        return

    if before_type == "object":
        before_keys = set(before)
        after_keys = set(after)
        for key in sorted(before_keys | after_keys):
            child_path = budget.pointer_child(path, key)
            if key not in before:
                changes.append(
                    budget.change(
                        kind="added",
                        path=child_path,
                        before_type=None,
                        after_type=_json_type(after[key]),
                    )
                )
            elif key not in after:
                changes.append(
                    budget.change(
                        kind="removed",
                        path=child_path,
                        before_type=_json_type(before[key]),
                        after_type=None,
                    )
                )
            else:
                _diff_json(
                    before[key],
                    after[key],
                    path=child_path,
                    depth=depth + 1,
                    budget=budget,
                    changes=changes,
                )
        return

    if before_type == "array":
        common = min(len(before), len(after))
        for index in range(common):
            _diff_json(
                before[index],
                after[index],
                path=budget.pointer_child(path, index),
                depth=depth + 1,
                budget=budget,
                changes=changes,
            )
        for index in range(common, len(before)):
            changes.append(
                budget.change(
                    kind="removed",
                    path=budget.pointer_child(path, index),
                    before_type=_json_type(before[index]),
                    after_type=None,
                )
            )
        for index in range(common, len(after)):
            changes.append(
                budget.change(
                    kind="added",
                    path=budget.pointer_child(path, index),
                    before_type=None,
                    after_type=_json_type(after[index]),
                )
            )
        return

    if before != after:
        changes.append(
            budget.change(
                kind="changed",
                path=path,
                before_type=before_type,
                after_type=after_type,
            )
        )


def diff_canonical_json_entries(
    before_entries: Sequence[tuple],
    after_entries: Sequence[tuple],
    budget: StructuralDiffBudget,
) -> StructuralDiffResult:
    """Return a complete, deterministic structural diff for canonical JSON.

    Added or removed subtrees produce one row at their root.  Changed trees are
    traversed recursively.  Source values are never retained in the result.
    """

    def entry_map(entries: Sequence[tuple]) -> dict:
        result = {}
        for item in entries:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not bytes
            ):
                raise ProviderError(
                    "Structural provider entries must be (str, bytes) tuples"
                )
            label, content = item
            if label in result:
                raise ProviderError(
                    f"Structural provider returned duplicate entry label {label!r}"
                )
            budget.reserve_input(label, content)
            result[label] = content
        return result

    before_by_label = entry_map(before_entries)
    after_by_label = entry_map(after_entries)
    documents: List[StructuralDocumentDiff] = []
    for label in sorted(set(before_by_label) | set(after_by_label)):
        before_content = before_by_label.get(label)
        after_content = after_by_label.get(label)
        if before_content is not None and before_content == after_content:
            continue
        changes: List[StructuralChange] = []
        if before_content is None:
            after_tree = _parse_canonical_entry(after_content, label)
            _validate_tree(after_tree, budget)
            changes.append(
                budget.change(
                    kind="added",
                    path="",
                    before_type=None,
                    after_type=_json_type(after_tree),
                )
            )
            status = "added"
        elif after_content is None:
            before_tree = _parse_canonical_entry(before_content, label)
            _validate_tree(before_tree, budget)
            changes.append(
                budget.change(
                    kind="removed",
                    path="",
                    before_type=_json_type(before_tree),
                    after_type=None,
                )
            )
            status = "removed"
        else:
            before_tree = _parse_canonical_entry(before_content, label)
            after_tree = _parse_canonical_entry(after_content, label)
            _validate_tree(before_tree, budget)
            _validate_tree(after_tree, budget)
            _diff_json(
                before_tree,
                after_tree,
                path="",
                depth=0,
                budget=budget,
                changes=changes,
            )
            status = "changed"
        if changes:
            documents.append(
                StructuralDocumentDiff(label, status, tuple(changes))
            )
    return StructuralDiffResult(tuple(documents))


def structural_diff_payload(result: StructuralDiffResult) -> dict:
    """Validate and convert one typed provider result to the JSON contract."""
    if type(result) is not StructuralDiffResult:
        raise ProviderError("Structural provider returned an invalid result type")
    if type(result.documents) is not tuple:
        raise ProviderError("Structural provider documents must be a tuple")
    counts = {"added": 0, "removed": 0, "changed": 0}
    documents = []
    row_count = 0
    result_bytes = 0
    previous_label: Optional[str] = None
    for document in result.documents:
        if type(document) is not StructuralDocumentDiff:
            raise ProviderError("Structural provider returned an invalid document type")
        if type(document.label) is not str or not document.label:
            raise ProviderError("Structural provider returned an invalid document label")
        label_bytes = len(document.label.encode("utf-8", errors="strict"))
        if label_bytes > MAX_PROVIDER_DIFF_PATH_BYTES:
            raise GuardrailError(
                "Structural provider result exceeds the bounded label limit. "
                "No partial structural result was emitted."
            )
        if previous_label is not None and document.label <= previous_label:
            raise ProviderError(
                "Structural provider document labels must be unique and sorted"
            )
        previous_label = document.label
        result_bytes += label_bytes + 96
        if document.status not in {"added", "removed", "changed"}:
            raise ProviderError(
                f"Structural provider returned invalid document status {document.status!r}"
            )
        if type(document.changes) is not tuple or not document.changes:
            raise ProviderError(
                "Structural provider document changes must be a non-empty tuple"
            )
        changes = []
        seen_changes = set()
        for change in document.changes:
            if type(change) is not StructuralChange:
                raise ProviderError("Structural provider returned an invalid change type")
            if change.kind not in counts:
                raise ProviderError(
                    f"Structural provider returned invalid change kind {change.kind!r}"
                )
            if type(change.path) is not str or not _is_json_pointer(change.path):
                raise ProviderError("Structural provider returned an invalid JSON pointer")
            path_bytes = len(change.path.encode("utf-8", errors="strict"))
            if path_bytes > MAX_PROVIDER_DIFF_PATH_BYTES:
                raise GuardrailError(
                    "Structural provider result exceeds the bounded JSON-pointer "
                    "limit. No partial structural result was emitted."
                )
            if (
                change.before_type is not None
                and change.before_type not in _JSON_TYPES
            ) or (
                change.after_type is not None
                and change.after_type not in _JSON_TYPES
            ):
                raise ProviderError("Structural provider returned an invalid JSON type")
            if (
                change.kind == "added"
                and (change.before_type is not None or change.after_type is None)
            ) or (
                change.kind == "removed"
                and (change.before_type is None or change.after_type is not None)
            ) or (
                change.kind == "changed"
                and (change.before_type is None or change.after_type is None)
            ):
                raise ProviderError(
                    "Structural provider returned an inconsistent type transition"
                )
            identity = (change.kind, change.path)
            if identity in seen_changes:
                raise ProviderError("Structural provider returned a duplicate change")
            seen_changes.add(identity)
            row_count += 1
            result_bytes += path_bytes + 96
            if row_count > MAX_PROVIDER_DIFF_ROWS:
                raise GuardrailError(
                    "Structural provider result exceeds the bounded row limit. "
                    "No partial structural result was emitted."
                )
            if result_bytes > MAX_PROVIDER_DIFF_RESULT_BYTES:
                raise GuardrailError(
                    "Structural provider result exceeds the bounded byte limit. "
                    "No partial structural result was emitted."
                )
            counts[change.kind] += 1
            changes.append(
                {
                    "kind": change.kind,
                    "path": change.path,
                    "before_type": change.before_type,
                    "after_type": change.after_type,
                }
            )
        if document.status in {"added", "removed"} and (
            len(document.changes) != 1
            or document.changes[0].kind != document.status
            or document.changes[0].path != ""
        ):
            raise ProviderError(
                "Added or removed structural documents require one matching root change"
            )
        documents.append(
            {
                "label": document.label,
                "status": document.status,
                "changes": changes,
            }
        )
    return {"documents": documents, "summary": counts}
