"""Shared deterministic structured-data parsing primitives."""

import json
from typing import Any, List

from ._utils import (
    MAX_JSON_TREE_DEPTH,
    MAX_JSON_TREE_NODES,
    _bounded_diagnostic_repr,
    _bounded_json_float,
    _bounded_json_int,
)


class StrictJSONError(ValueError):
    """JSON contains a lossy or cross-version-unsafe value."""


def _reject_excessive_json_tokens(text: str) -> None:
    """Reject a provably over-wide JSON tree before ``json.loads`` allocates it.

    The scanner counts every value token plus object-key strings. A valid JSON
    object has at most one key token per non-root value, so an accepted tree
    can never cross twice the public value-node ceiling. The exact iterative
    tree validator still runs at each untrusted input boundary.
    """
    token_limit = 2 * MAX_JSON_TREE_NODES
    tokens = 0
    in_string = False
    escaped = False
    in_atom = False
    depth = 0
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            tokens += 1
            in_string = True
            in_atom = False
        elif character in "[{":
            tokens += 1
            depth += 1
            if depth > MAX_JSON_TREE_DEPTH + 1:
                raise StrictJSONError(
                    "JSON document exceeds the pre-parse structural depth limit"
                )
            in_atom = False
        elif character in "]}":
            depth = max(0, depth - 1)
            in_atom = False
        elif character in " \t\r\n,:":
            in_atom = False
        elif not in_atom:
            tokens += 1
            in_atom = True
        if tokens > token_limit:
            raise StrictJSONError(
                "JSON document exceeds the pre-parse structural token limit"
            )


def _unique_json_object(pairs: List[tuple]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise StrictJSONError(
                "duplicate JSON object key " f"{_bounded_diagnostic_repr(key)}"
            )
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> Any:
    raise StrictJSONError(
        "non-finite JSON number "
        f"{_bounded_diagnostic_repr(value)} is not supported"
    )


def strict_json_loads(text: str) -> Any:
    """Load JSON without duplicate keys, non-finite values, or huge numbers."""
    _reject_excessive_json_tokens(text)
    return json.loads(
        text,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonfinite_json_constant,
        parse_float=_bounded_json_float,
        parse_int=_bounded_json_int,
    )
