"""Strict JSON/YAML parsing and canonical OpenAPI normalization."""

import json as _json_mod
import re
from typing import Any, Optional

from ._structured_data import StrictJSONError, strict_json_loads
from ._utils import (
    GuardrailError,
    ProviderError,
    _bounded_diagnostic_repr,
    _bounded_diagnostic_text,
    _bounded_int_to_decimal,
    _bounded_json_value_issues,
    _bounded_yaml_int,
    _iter_bounded_json_values,
    _json_path_child,
    _render_bounded_json_path,
)

MAX_CANONICAL_JSON_BYTES = 50 * 1024 * 1024


class CanonicalJsonLimitError(ProviderError):
    """Canonical JSON could not fit within its caller-provided byte budget."""


def _bounded_exception(exc: Exception) -> str:
    """Return a useful, bounded exception description for user-facing errors."""
    try:
        detail = str(exc).strip()
    except Exception:
        detail = ""
    detail = detail or exc.__class__.__name__
    return _bounded_diagnostic_text(detail)


# Keys stripped at every level of an OpenAPI document (non-contract fields).
_OPENAPI_STRIP_KEYS = frozenset(
    {"description", "summary", "externalDocs", "example", "examples"}
)
# Top-level OpenAPI keys that are metadata only, not API contract.
_OPENAPI_DROP_TOP = frozenset({"info", "servers", "tags"})

# Maps whose direct keys are user-defined identifiers rather than OpenAPI
# annotation fields.  Preserve those keys, then resume normal annotation
# stripping inside each referenced object.
_OPENAPI_COMPONENT_MAPS = frozenset({
    "schemas", "responses", "parameters", "examples", "requestBodies",
    "headers", "securitySchemes", "links", "callbacks", "pathItems",
})
_OPENAPI_NAMED_MAP_KEYS = frozenset({
    "paths", "webhooks", "scopes", "patternProperties", "dependentSchemas",
    "dependentRequired", "parameters", "headers", "encoding", "mapping",
    "callbacks", "links", "variables",
})


def _is_openapi_named_schema_map(path: tuple, key: Any) -> bool:
    """Return whether ``key`` introduces a map of user-defined schema names.

    Annotation-looking names are legal property/schema identifiers.  For
    example, ``properties.description`` describes a property named
    ``description``; it is not the Schema Object's documentation field.
    """
    if key in {"properties", "definitions", "$defs"} | _OPENAPI_NAMED_MAP_KEYS:
        return True
    return path == ("components",) and key in _OPENAPI_COMPONENT_MAPS


def _strip_openapi(
    obj: Any,
    *,
    path: tuple = (),
    preserve_keys: bool = False,
) -> Any:
    """Recursively remove non-contract fields from an OpenAPI object.

    Drops:
    - ``description``, ``summary``, ``externalDocs``, ``example``, ``examples``
      at any nesting level (documentation-only fields).

    OpenAPI explicitly permits ``x-*`` specification extensions.  They can
    affect routing, code generation, authentication, and deployment behavior,
    so they remain part of the canonical contract by default.

    Keys inside Schema Object ``properties``/``definitions``/``$defs`` maps
    and ``components.schemas`` are user-defined identifiers.  Those map keys
    are retained even when their spelling looks like an annotation; annotation
    fields within the schema value are still removed.
    """
    if isinstance(obj, dict):
        stripped = {}
        for key, value in obj.items():
            is_annotation = key in _OPENAPI_STRIP_KEYS
            if is_annotation and not preserve_keys:
                continue

            # A named-map entry's value is a schema object.  Its arbitrary key
            # must not make that schema object itself behave like a named map.
            child_preserves_keys = (
                not preserve_keys and _is_openapi_named_schema_map(path, key)
            )
            stripped[key] = _strip_openapi(
                value,
                path=path + (key,),
                preserve_keys=child_preserves_keys,
            )
        return stripped
    if isinstance(obj, list):
        return [
            _strip_openapi(
                item,
                path=path,
                # Security Requirement Object keys are arbitrary scheme names.
                preserve_keys=(path and path[-1] == "security"),
            )
            for item in obj
        ]
    return obj


def _parse_json_strict(text: str, path_label: str) -> Any:
    """Parse standards-compliant JSON without lossy duplicate-key handling."""
    try:
        return strict_json_loads(text)
    except (
        _json_mod.JSONDecodeError,
        StrictJSONError,
        RecursionError,
        ValueError,
    ) as exc:
        raise ProviderError(
            f"JSON parse failed for {path_label}: {_bounded_exception(exc)}"
        ) from exc


def _parse_yaml_strict(text: str, path_label: str) -> Any:
    """Parse a conservative YAML 1.2-compatible subset for OpenAPI."""
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise ProviderError(
            f"Cannot parse {path_label}: file is not valid JSON and PyYAML is not installed. "
            "Install PyYAML: pip install PyYAML"
        ) from exc

    class StrictOpenApiLoader(yaml.SafeLoader):
        def compose_node(self, parent: Any, index: Any) -> Any:
            if self.check_event(yaml.AliasEvent):
                raise ProviderError(
                    "YAML aliases are not supported in canonical OpenAPI inputs"
                )
            return super().compose_node(parent, index)

    # PyYAML's default resolver follows YAML 1.1 and treats yes/no/on/off as
    # booleans.  OpenAPI uses YAML 1.2 semantics, where only true/false are
    # implicit booleans.  Copy before editing so the process-global SafeLoader
    # remains untouched.  Timestamps are not in the YAML 1.2 core schema and
    # remain strings rather than becoming non-JSON datetime/date objects.
    StrictOpenApiLoader.yaml_implicit_resolvers = {
        key: list(resolvers)
        for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    for key, resolvers in StrictOpenApiLoader.yaml_implicit_resolvers.items():
        StrictOpenApiLoader.yaml_implicit_resolvers[key] = [
            resolver
            for resolver in resolvers
            if resolver[0]
            not in {
                "tag:yaml.org,2002:bool",
                "tag:yaml.org,2002:float",
                "tag:yaml.org,2002:int",
                "tag:yaml.org,2002:timestamp",
            }
        ]
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:bool",
        re.compile(r"^(?:true|false)$", re.IGNORECASE),
        list("tTfF"),
    )
    # Accept the JSON numeric grammar, which is a conservative unambiguous
    # subset of YAML 1.2 core numbers. In particular, YAML 1.1's ``012`` octal
    # and sexagesimal forms remain strings instead of colliding with decimals.
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:int",
        re.compile(r"^-?(?:0|[1-9][0-9]*)$"),
        list("-0123456789"),
    )
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(
            r"^-?(?:(?:0|[1-9][0-9]*)\.[0-9]+(?:[eE][-+]?[0-9]+)?"
            r"|(?:0|[1-9][0-9]*)[eE][-+]?[0-9]+)$"
        ),
        list("-0123456789"),
    )
    # YAML 1.2 still recognizes non-finite spellings. Resolve them as floats
    # so the JSON-tree guard rejects them, instead of accidentally preserving
    # them as benign strings after removing PyYAML's broader YAML 1.1 rules.
    StrictOpenApiLoader.add_implicit_resolver(
        "tag:yaml.org,2002:float",
        re.compile(r"^(?:[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$"),
        list("-+."),
    )

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> dict:
        if not isinstance(node, yaml.MappingNode):
            raise ProviderError("expected a YAML mapping")
        mapping: dict = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if type(key) is not str:
                raise ProviderError(
                    "YAML/OpenAPI mapping keys must be strings; quote numeric "
                    "response keys (for example, '200')"
                )
            if key in mapping:
                raise ProviderError(
                    "duplicate mapping key " f"{_bounded_diagnostic_repr(key)}"
                )
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    def construct_integer(loader: Any, node: Any) -> int:
        try:
            scalar = loader.construct_scalar(node)
            return _bounded_yaml_int(scalar)
        except (TypeError, ValueError) as exc:
            raise ProviderError(f"invalid YAML integer: {exc}") from exc

    StrictOpenApiLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
        construct_mapping,
    )
    StrictOpenApiLoader.add_constructor(
        "tag:yaml.org,2002:int",
        construct_integer,
    )
    try:
        return yaml.load(text, Loader=StrictOpenApiLoader)
    except ProviderError:
        raise
    except yaml.MarkedYAMLError as exc:
        # PyYAML's string form embeds the offending source line.  Contract
        # contents must not be copied into CI logs or Action outputs merely
        # because parsing failed; retain only the error kind and coordinates.
        mark = exc.context_mark or exc.problem_mark
        location = ""
        if mark is not None:
            location = f" at line {mark.line + 1}, column {mark.column + 1}"
        raise ProviderError(
            f"YAML parse failed for {path_label}: "
            f"{exc.__class__.__name__}{location}"
        ) from exc
    except Exception as exc:
        raise ProviderError(
            f"YAML parse failed for {path_label}: {_bounded_exception(exc)}"
        ) from exc


def _parse_yaml_or_json(raw: bytes, path_label: str) -> Any:
    """Parse raw OpenAPI bytes without accepting ambiguous structures."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProviderError(
            f"OpenAPI file {path_label} is not valid UTF-8: {_bounded_exception(exc)}"
        ) from exc

    # A .json declaration must actually be JSON; silently reinterpreting a
    # malformed JSON contract as YAML would hide mistakes.  YAML files still
    # take the JSON fast path because JSON is a YAML subset.
    if path_label.lower().endswith(".json"):
        return _parse_json_strict(text, path_label)
    try:
        return strict_json_loads(text)
    except _json_mod.JSONDecodeError:
        return _parse_yaml_strict(text, path_label)
    except (StrictJSONError, ValueError) as exc:
        raise ProviderError(
            f"JSON parse failed for {path_label}: {_bounded_exception(exc)}"
        ) from exc


def _json_tree_error(value: Any, *, path: str = "$") -> Optional[str]:
    """Reject values that cannot be represented safely and portably as JSON."""
    issues = _bounded_json_value_issues(value, path=path)
    if not issues:
        return None
    error = issues[0]
    if "non-string mapping key" in error:
        return (
            f"{error}; quote numeric response keys (for example, '200')"
        )
    return error


def _openapi_document_error(document: Any) -> Optional[str]:
    """Validate the minimum root contract and reference safety policy."""
    tree_error = _json_tree_error(document)
    if tree_error:
        return tree_error
    if type(document) is not dict:
        return "OpenAPI document root must be an object"

    has_openapi_version = "openapi" in document
    has_swagger_version = "swagger" in document
    openapi_version = document.get("openapi")
    swagger_version = document.get("swagger")
    if has_openapi_version and has_swagger_version:
        return "OpenAPI document must not declare both 'openapi' and 'swagger'"
    if has_openapi_version:
        if type(openapi_version) is not str or not re.fullmatch(
            r"3\.(?:0|1)\.[0-9]+", openapi_version
        ):
            return (
                "unsupported or invalid 'openapi' version; expected an OpenAPI "
                "3.0.x or 3.1.x string"
            )
    elif not has_swagger_version or swagger_version != "2.0":
        return (
            "OpenAPI document must declare 'openapi' 3.0.x/3.1.x or "
            "'swagger' 2.0"
        )

    try:
        for value, value_path in _iter_bounded_json_values(document, path="$"):
            if type(value) is not dict or "$ref" not in value:
                continue
            reference = value["$ref"]
            if type(reference) is str and reference.startswith("#"):
                continue
            reference_path = _render_bounded_json_path(
                _json_path_child(value_path, "$ref")
            )
            return (
                f"{reference_path} uses an external or local-file reference; "
                "openapi-canonical accepts only same-document fragment "
                "references beginning with '#'"
            )
    except (GuardrailError, RuntimeError, ValueError) as exc:
        # The generic tree check above normally catches these first. Keep the
        # reference walk independently fail-closed if a mutable direct caller
        # changes the document between validation passes.
        return str(exc)
    return None


def _canonical_json_bytes(
    value: Any,
    path_label: str,
    *,
    max_bytes: int = MAX_CANONICAL_JSON_BYTES,
) -> bytes:
    """Serialize deterministic JSON without crossing the remaining budget.

    Building a complete string and checking its size afterwards can allocate
    several times the source size when control characters need JSON escaping.
    Emit into a bounded byte buffer instead.  Large strings are sized before
    quoting, so even a single scalar cannot create an over-budget temporary.
    """
    effective_limit = min(max_bytes, MAX_CANONICAL_JSON_BYTES)
    if effective_limit < 0:
        raise ProviderError("provider aggregate byte budget was exhausted")

    output = bytearray()
    active: set[int] = set()

    def over_limit() -> CanonicalJsonLimitError:
        return CanonicalJsonLimitError(
            f"Canonical JSON for {path_label} exceeds the "
            f"{effective_limit}-byte remaining provider limit"
        )

    def emit_bytes(chunk: bytes) -> None:
        if len(chunk) > effective_limit - len(output):
            raise over_limit()
        output.extend(chunk)

    def emit_ascii(chunk: str) -> None:
        emit_bytes(chunk.encode("ascii"))

    def quoted_utf8_size(text: str) -> int:
        size = 2  # surrounding quotes
        if size > effective_limit - len(output):
            raise over_limit()
        for character in text:
            codepoint = ord(character)
            if character in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
                size += 2
            elif codepoint < 0x20:
                size += 6
            else:
                size += len(character.encode("utf-8"))
            if size > effective_limit - len(output):
                raise over_limit()
        return size

    def emit_string(text: str) -> None:
        expected_size = quoted_utf8_size(text)
        rendered = _json_mod.dumps(text, ensure_ascii=False).encode("utf-8")
        if len(rendered) != expected_size:  # pragma: no cover - stdlib contract
            raise ProviderError("JSON string encoding produced an unexpected size")
        emit_bytes(rendered)

    def encode(item: Any) -> None:
        if item is None:
            emit_ascii("null")
            return
        if item is True:
            emit_ascii("true")
            return
        if item is False:
            emit_ascii("false")
            return
        if type(item) is str:
            emit_string(item)
            return
        if type(item) is int:
            emit_ascii(_bounded_int_to_decimal(item))
            return
        if type(item) is float:
            emit_ascii(_json_mod.dumps(item, allow_nan=False))
            return
        if type(item) not in {list, dict}:
            raise TypeError(
                f"Object of type {type(item).__name__} is not JSON serializable"
            )

        marker = id(item)
        if marker in active:
            raise ValueError("Circular reference detected")
        active.add(marker)
        try:
            if type(item) is list:
                emit_ascii("[")
                for index, child in enumerate(item):
                    if index:
                        emit_ascii(",")
                    encode(child)
                emit_ascii("]")
                return

            emit_ascii("{")
            for index, key in enumerate(sorted(item)):
                if index:
                    emit_ascii(",")
                if type(key) is not str:
                    raise TypeError("JSON object keys must be strings")
                emit_string(key)
                emit_ascii(":")
                encode(item[key])
            emit_ascii("}")
        finally:
            active.remove(marker)

    try:
        encode(value)
    except ProviderError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProviderError(
            f"Canonical JSON serialization failed for {path_label}: "
            f"{_bounded_exception(exc)}"
        ) from exc
    return bytes(output)
