"""Offline validation for self-contained output contracts; no schema fetching."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache
def _validator(name):
    try:
        import jsonschema
        from referencing import Registry
    except ImportError:
        return None
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "spec" / name).read_text()
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    # Explicitly avoid jsonschema's legacy remote-retrieval default. Local
    # fragment refs still resolve; a missing external resource fails closed.
    return jsonschema.Draft202012Validator(schema, registry=Registry())


def assert_output_schema(name, result):
    validator = _validator(name)
    if validator is not None:
        validator.validate(result)
