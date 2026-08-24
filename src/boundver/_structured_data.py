"""Shared deterministic structured-data parsing primitives."""

import json
from typing import Any, List

from ._utils import _bounded_diagnostic_repr, _bounded_json_int


class StrictJSONError(ValueError):
    """JSON contains a lossy or cross-version-unsafe value."""


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
    """Load JSON without duplicate keys, non-finite values, or huge integers."""
    return json.loads(
        text,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_nonfinite_json_constant,
        parse_int=_bounded_json_int,
    )
