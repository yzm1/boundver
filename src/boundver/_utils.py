"""Shared utilities, enums, and exception types for boundver."""

import json
import math
import os
import re
import stat
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, List, Mapping, Optional, Set, Tuple

from ._bounded_io import FileSizeLimitError, read_bounded_file


# Python 3.11+ limits decimal-to-int conversion to 4,300 digits by default,
# while earlier supported interpreters do not.  Enforce one explicit contract
# so parsing the same JSON cannot depend on the runner's Python version.
MAX_JSON_INTEGER_DIGITS = 4300
_MAX_JSON_INTEGER_ABS = 10 ** MAX_JSON_INTEGER_DIGITS
MAX_YAML_INTEGER_CHARACTERS = MAX_JSON_INTEGER_DIGITS + 1

# 640 is the smallest non-zero decimal conversion limit accepted by CPython
# 3.11+. TOML parsers expose no parse-int hook, so reject longer numeric runs
# lexically before parsing. This keeps tomllib/tomli behavior aligned with
# older interpreters that have no process-wide decimal conversion limit.
MAX_TOML_INTEGER_DIGITS = 640

# Parsed config, lock, and canonical-provider trees are byte-bounded at their
# input boundaries, but a compact document can still contain a very deep or
# very wide value graph.  Keep traversal limits independent of parser/runtime
# recursion behavior.  Diagnostic paths are rendered lazily from linked path
# nodes so a chain of long object keys cannot retain every growing prefix.
MAX_JSON_TREE_DEPTH = 128
MAX_JSON_TREE_NODES = 100_000
MAX_JSON_TREE_ISSUES = 100
MAX_JSON_DIAGNOSTIC_PATH_BYTES = 4 * 1024
MAX_DIAGNOSTIC_VALUE_CHARS = 500
_JSON_PATH_TRUNCATION = "...[path truncated]"

# Declared paths and glob matching are reachable through both built-in and
# extension-provider flows.  Keep the primitive itself bounded rather than
# relying on every caller/provider to impose the same limits.
MAX_DECLARED_PATH_BYTES = 16 * 1024
MAX_GLOB_PATH_BYTES = 64 * 1024
MAX_GLOB_SEGMENTS = 1024
MAX_GLOB_MATCH_STEPS = 100_000
MAX_GLOB_PATTERN_SEGMENT_BYTES = 4 * 1024
MAX_GLOB_METACHARACTERS_PER_SEGMENT = 256

# Canonical public vocabularies. Keep ordering where it affects human output
# or severity-independent iteration, and use the frozen sets for membership.
SOURCE_MODES = ("head", "index", "working-tree")
SOURCE_MODE_SET = frozenset(SOURCE_MODES)
FACETS = ("exact", "behavior", "boundary", "compat")
FACET_SET = frozenset(FACETS)


def _bounded_json_int(value: str) -> int:
    """Parse one JSON integer under the cross-version decimal-size limit."""
    if not isinstance(value, str):
        raise TypeError("JSON integer must be text")
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError(
            "Invalid JSON integer: " f"{_bounded_diagnostic_repr(value)}"
        )
    negative = value.startswith("-")
    digits = value[1:] if negative else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    # ``int(<decimal text>)`` observes the process-wide
    # ``sys.set_int_max_str_digits`` setting.  Parse short chunks so accepting
    # a value under our explicit contract does not depend on unrelated runtime
    # configuration (and without temporarily weakening that global setting).
    result = 0
    first = len(digits) % 9 or 9
    for end in range(first, len(digits) + 1, 9):
        chunk = digits[end - (first if end == first else 9):end]
        result = result * (10 ** len(chunk)) + int(chunk)
    return -result if negative else result


def _bounded_yaml_int(value: str) -> int:
    """Parse only the JSON integer subset accepted across config formats."""
    if not isinstance(value, str):
        raise TypeError("YAML integer must be text")
    if not value or len(value) > MAX_YAML_INTEGER_CHARACTERS:
        raise ValueError("YAML integer representation exceeds the safety limit")
    if re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError(
            "YAML integer must use JSON decimal syntax without plus signs, "
            "separators, prefixes, leading zeroes, or sexagesimal notation"
        )
    return _bounded_json_int(value)


def _json_integer_is_bounded(value: int) -> bool:
    """Return whether *value* fits the JSON decimal-size contract."""
    return abs(value) < _MAX_JSON_INTEGER_ABS


def _bounded_int_to_decimal(value: int) -> str:
    """Render a bounded integer without consulting ``int_max_str_digits``."""
    if not _json_integer_is_bounded(value):
        raise ValueError(
            "JSON integer exceeds the "
            f"{MAX_JSON_INTEGER_DIGITS}-decimal-digit limit"
        )
    if value == 0:
        return "0"
    negative = value < 0
    remaining = -value if negative else value
    chunks = []
    while remaining:
        remaining, chunk = divmod(remaining, 1_000_000_000)
        chunks.append(chunk)
    rendered = str(chunks.pop())
    while chunks:
        rendered += f"{chunks.pop():09d}"
    return "-" + rendered if negative else rendered


def _bounded_diagnostic_repr(
    value: Any,
    *,
    max_chars: int = MAX_DIAGNOSTIC_VALUE_CHARS,
) -> str:
    """Render an untrusted value without invoking its arbitrary ``repr``.

    CPython's process-wide decimal conversion limit can make even an error
    message raise for an otherwise accepted JSON integer.  Containers and
    strings can also make diagnostics duplicate a large fraction of a bounded
    input.  This renderer handles exact JSON types itself and caps the result;
    unsupported values are represented by type name only.
    """
    if max_chars < 0:
        raise ValueError("Diagnostic character limit must be non-negative")
    if max_chars == 0:
        return ""

    def truncate(text: str) -> str:
        if len(text) <= max_chars:
            return text
        if max_chars <= 3:
            return "." * max_chars
        return text[: max_chars - 3] + "..."

    active: Set[int] = set()

    def render(item: Any, depth: int) -> str:
        if item is None:
            return "None"
        if item is True:
            return "True"
        if item is False:
            return "False"
        if type(item) is int:
            try:
                return truncate(_bounded_int_to_decimal(item))
            except ValueError:
                return "<integer outside the JSON safety limit>"
        if type(item) is float:
            return truncate(repr(item))
        if type(item) is str:
            # Slice before escaping so a very large source string cannot make
            # a correspondingly large temporary representation.
            prefix = item[: max_chars + 1]
            return truncate(ascii(prefix))
        if type(item) is bytes:
            return truncate(repr(item[: max_chars + 1]))
        if type(item) not in {list, dict, tuple}:
            return truncate(f"<{type(item).__name__}>")
        if depth >= 4:
            return "[...]" if type(item) in {list, tuple} else "{...}"

        marker = id(item)
        if marker in active:
            return "<reference cycle>"
        active.add(marker)
        try:
            if type(item) is dict:
                pieces = []
                for index, (key, child) in enumerate(item.items()):
                    if index >= 8:
                        pieces.append("...")
                        break
                    pieces.append(
                        f"{render(key, depth + 1)}: {render(child, depth + 1)}"
                    )
                return truncate("{" + ", ".join(pieces) + "}")

            pieces = []
            for index, child in enumerate(item):
                if index >= 8:
                    pieces.append("...")
                    break
                pieces.append(render(child, depth + 1))
            opening, closing = ("[", "]") if type(item) is list else ("(", ")")
            return truncate(opening + ", ".join(pieces) + closing)
        finally:
            active.remove(marker)

    return render(value, 0)


def _bounded_diagnostic_text(
    value: Any,
    *,
    max_chars: int = MAX_DIAGNOSTIC_VALUE_CHARS,
) -> str:
    """Render bounded plain text, using safe representation for non-strings."""
    if type(value) is not str:
        return _bounded_diagnostic_repr(value, max_chars=max_chars)
    if max_chars < 0:
        raise ValueError("Diagnostic character limit must be non-negative")
    if max_chars == 0:
        return ""
    prefix = value[: max_chars + 1]
    rendered = prefix.encode("utf-8", errors="backslashreplace").decode("utf-8")
    if len(rendered) <= max_chars:
        return rendered
    if max_chars <= 3:
        return "." * max_chars
    return rendered[: max_chars - 3] + "..."


def _bounded_json_dumps(
    value: Any,
    *,
    skipkeys: bool = False,
    ensure_ascii: bool = True,
    check_circular: bool = True,
    allow_nan: bool = True,
    indent: Any = None,
    separators: Optional[tuple] = None,
    default: Optional[Callable[[Any], Any]] = None,
    sort_keys: bool = False,
) -> str:
    """Serialize JSON without Python's setting-dependent integer rendering.

    This small encoder intentionally uses only the public ``json.dumps`` API
    for individual strings and floats. Container traversal and integer
    rendering stay in boundver, avoiding the private ``json.encoder`` helpers
    whose signatures can change between supported Python releases.
    """
    if indent is None or isinstance(indent, str):
        indent_text = indent
    else:
        indent_text = " " * indent
    if separators is None:
        item_separator = ", " if indent_text is None else ","
        key_separator = ": "
    else:
        item_separator, key_separator = separators

    active: Set[int] = set()

    def quote(text: str) -> str:
        return json.dumps(text, ensure_ascii=ensure_ascii)

    def enter(container: Any) -> Optional[int]:
        if not check_circular:
            return None
        marker = id(container)
        if marker in active:
            raise ValueError("Circular reference detected")
        active.add(marker)
        return marker

    def leave(marker: Optional[int]) -> None:
        if marker is not None:
            active.remove(marker)

    def encode_key(key: Any) -> Optional[str]:
        if isinstance(key, str):
            text = key
        elif key is None:
            text = "null"
        elif key is True:
            text = "true"
        elif key is False:
            text = "false"
        elif isinstance(key, int):
            text = _bounded_int_to_decimal(key)
        elif isinstance(key, float):
            text = json.dumps(key, allow_nan=allow_nan)
        elif skipkeys:
            return None
        else:
            raise TypeError(
                "keys must be str, int, float, bool or None, "
                f"not {type(key).__name__}"
            )
        return quote(text)

    def encode(item: Any, level: int) -> str:
        if item is None:
            return "null"
        if item is True:
            return "true"
        if item is False:
            return "false"
        if isinstance(item, str):
            return quote(item)
        if isinstance(item, int):
            return _bounded_int_to_decimal(item)
        if isinstance(item, float):
            return json.dumps(item, allow_nan=allow_nan)
        if isinstance(item, (list, tuple)):
            marker = enter(item)
            try:
                encoded = [encode(child, level + 1) for child in item]
            finally:
                leave(marker)
            if not encoded:
                return "[]"
            if indent_text is None:
                return "[" + item_separator.join(encoded) + "]"
            child_indent = "\n" + indent_text * (level + 1)
            closing_indent = "\n" + indent_text * level
            return (
                "["
                + child_indent
                + (item_separator + child_indent).join(encoded)
                + closing_indent
                + "]"
            )
        if isinstance(item, dict):
            marker = enter(item)
            try:
                pairs = list(item.items())
                if sort_keys:
                    pairs.sort(key=lambda pair: pair[0])
                encoded_pairs = []
                for key, child in pairs:
                    encoded_key = encode_key(key)
                    if encoded_key is None:
                        continue
                    encoded_pairs.append(
                        encoded_key + key_separator + encode(child, level + 1)
                    )
            finally:
                leave(marker)
            if not encoded_pairs:
                return "{}"
            if indent_text is None:
                return "{" + item_separator.join(encoded_pairs) + "}"
            child_indent = "\n" + indent_text * (level + 1)
            closing_indent = "\n" + indent_text * level
            return (
                "{"
                + child_indent
                + (item_separator + child_indent).join(encoded_pairs)
                + closing_indent
                + "}"
            )
        if default is None:
            raise TypeError(
                f"Object of type {type(item).__name__} is not JSON serializable"
            )
        marker = enter(item)
        try:
            return encode(default(item), level)
        finally:
            leave(marker)

    return encode(value, 0)


class _BoundedJsonPath:
    """One shared link in a lazily rendered JSON diagnostic path."""

    __slots__ = ("parent", "segment", "is_index")

    def __init__(
        self,
        parent: Optional["_BoundedJsonPath"],
        segment: object,
        *,
        is_index: bool = False,
    ) -> None:
        self.parent = parent
        self.segment = segment
        self.is_index = is_index


def _json_path_child(
    parent: _BoundedJsonPath,
    segment: object,
    *,
    is_index: bool = False,
) -> _BoundedJsonPath:
    """Return a child path without copying any ancestor text."""
    return _BoundedJsonPath(parent, segment, is_index=is_index)


def _render_bounded_json_path(path: _BoundedJsonPath) -> str:
    """Render an ASCII diagnostic path under one hard byte ceiling."""
    nodes = []
    current: Optional[_BoundedJsonPath] = path
    while current is not None:
        nodes.append(current)
        current = current.parent
    nodes.reverse()

    path_limit = max(0, MAX_JSON_DIAGNOSTIC_PATH_BYTES)
    suffix = _JSON_PATH_TRUNCATION[:path_limit]
    content_limit = path_limit - len(suffix)
    rendered: List[str] = []
    used = 0
    truncated = False

    def append_ascii(text: str) -> bool:
        nonlocal used, truncated
        for character in text:
            codepoint = ord(character)
            if 0x20 <= codepoint <= 0x7E and character != "\\":
                encoded = character
            elif character == "\\":
                encoded = "\\\\"
            elif codepoint <= 0xFF:
                encoded = f"\\x{codepoint:02x}"
            elif codepoint <= 0xFFFF:
                encoded = f"\\u{codepoint:04x}"
            else:
                encoded = f"\\U{codepoint:08x}"
            if used + len(encoded) > content_limit:
                truncated = True
                return False
            rendered.append(encoded)
            used += len(encoded)
        return True

    for index, node in enumerate(nodes):
        if index == 0:
            piece = str(node.segment)
        elif node.is_index:
            piece = f"[{node.segment}]"
        else:
            piece = "." + str(node.segment)
        if not append_ascii(piece):
            break
    if truncated:
        rendered.append(suffix)
    return "".join(rendered)


def _iter_bounded_json_values(
    value: Any,
    *,
    path: str = "$",
) -> Iterator[Tuple[Any, _BoundedJsonPath]]:
    """Yield a JSON-like tree in depth-first order with bounded live state.

    Container iterators are resumed one child at a time.  A wide array or
    object therefore cannot allocate a second work list before the node limit
    is checked.  Object-key text is retained by reference in linked path nodes
    and rendered only for an actual diagnostic.
    """
    root_path = _BoundedJsonPath(None, path)
    stack: List[tuple] = [("visit", value, root_path, 0)]
    active: Set[int] = set()
    nodes = 0

    while stack:
        frame = stack.pop()
        if frame[0] == "iterate":
            _, iterator, parent_path, depth, marker, is_mapping = frame
            try:
                key, child = next(iterator)
            except StopIteration:
                active.remove(marker)
                continue
            stack.append(frame)
            if is_mapping:
                if type(key) is not str:
                    raise ValueError(
                        f"{_render_bounded_json_path(parent_path)} contains "
                        "non-string mapping key"
                    )
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        f"{_render_bounded_json_path(parent_path)} contains an "
                        "object key that is not valid Unicode/UTF-8"
                    ) from exc
                child_path = _json_path_child(parent_path, key)
            else:
                child_path = _json_path_child(
                    parent_path,
                    key,
                    is_index=True,
                )
            stack.append(("visit", child, child_path, depth + 1))
            continue

        _, item, item_path, depth = frame
        nodes += 1
        rendered_path: Optional[str] = None
        if depth > MAX_JSON_TREE_DEPTH:
            rendered_path = _render_bounded_json_path(item_path)
            raise GuardrailError(
                f"{rendered_path} is nested too deeply (maximum "
                f"{MAX_JSON_TREE_DEPTH} levels)"
            )
        if nodes > MAX_JSON_TREE_NODES:
            rendered_path = _render_bounded_json_path(item_path)
            raise GuardrailError(
                f"{rendered_path} exceeds the "
                f"{MAX_JSON_TREE_NODES}-value JSON tree limit"
            )

        if type(item) in {list, dict}:
            marker = id(item)
            if marker in active:
                raise ValueError(
                    f"{_render_bounded_json_path(item_path)} contains a "
                    "reference cycle"
                )
            active.add(marker)
            if type(item) is dict:
                iterator = iter(item.items())
                is_mapping = True
            else:
                iterator = iter(enumerate(item))
                is_mapping = False
            stack.append(
                (
                    "iterate",
                    iterator,
                    item_path,
                    depth,
                    marker,
                    is_mapping,
                )
            )
        yield item, item_path


def _bounded_json_value_issues(
    value: Any,
    *,
    path: str = "value",
) -> List[str]:
    """Return bounded reasons that *value* is not a safe JSON value tree."""
    issues: List[str] = []
    walker = _iter_bounded_json_values(value, path=path)

    def append_issue(message: str) -> bool:
        if len(issues) >= MAX_JSON_TREE_ISSUES - 1:
            issues.append(
                "JSON value validation stopped after reaching the "
                f"{MAX_JSON_TREE_ISSUES}-issue limit"
            )
            return False
        issues.append(message)
        return True

    try:
        for item, item_path in walker:
            if item is None or type(item) in {str, bool, int}:
                if type(item) is str:
                    try:
                        item.encode("utf-8")
                    except UnicodeEncodeError:
                        if not append_issue(
                            f"{_render_bounded_json_path(item_path)} contains a "
                            "string that is not valid Unicode/UTF-8"
                        ):
                            break
                elif type(item) is int and not _json_integer_is_bounded(item):
                    if not append_issue(
                        f"{_render_bounded_json_path(item_path)} contains an "
                        "oversized integer"
                    ):
                        break
                continue
            if type(item) is float:
                if not math.isfinite(item) and not append_issue(
                    f"{_render_bounded_json_path(item_path)} contains a "
                    "non-finite number"
                ):
                    break
                continue
            if type(item) in {list, dict}:
                continue
            if not append_issue(
                f"{_render_bounded_json_path(item_path)} contains non-JSON "
                f"scalar type {type(item).__name__}"
            ):
                break
    except (GuardrailError, RuntimeError, ValueError) as exc:
        append_issue(str(exc))
    finally:
        walker.close()
    return issues


def _toml_has_oversized_numeric_token(text: str) -> bool:
    """Detect oversized numeric value runs outside strings and comments.

    The scanner understands basic, literal, and multiline strings. It runs
    before the real TOML parser because tomllib/tomli provide ``parse_float``
    but no integer hook, leaving decimal acceptance dependent on Python's
    mutable conversion limit unless boundver applies this lexical ceiling.
    Bare keys are deliberately excluded: TOML permits digits in identifiers,
    and those digits are never converted to Python integers by the parser.
    """
    index = 0
    length = len(text)
    state = "normal"
    root_in_value = False
    table_header_depth = 0
    at_statement_start = True
    # Compact frame codes keep this pre-parser bounded even for maliciously
    # nested input. Arrays are value context; inline tables alternate between
    # a bare/quoted key and its value.
    array_frame = 0
    inline_key_frame = 1
    inline_value_frame = 2
    containers = bytearray()

    def in_key_context() -> bool:
        if table_header_depth:
            return True
        if containers:
            return containers[-1] == inline_key_frame
        return not root_in_value

    def reset_line() -> None:
        nonlocal root_in_value, table_header_depth, at_statement_start
        if not containers:
            root_in_value = False
            table_header_depth = 0
            at_statement_start = True

    while index < length:
        char = text[index]
        if state == "comment":
            if char in "\r\n":
                state = "normal"
                reset_line()
            index += 1
            continue
        if state in {"basic", "multiline-basic"}:
            if char == "\\":
                index += 2
                continue
            if state == "basic" and char == '"':
                state = "normal"
                index += 1
                continue
            if state == "multiline-basic" and char == '"':
                end = index
                while end < length and text[end] == '"':
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
            continue
        if state in {"literal", "multiline-literal"}:
            if state == "literal" and char == "'":
                state = "normal"
                index += 1
                continue
            if state == "multiline-literal" and char == "'":
                end = index
                while end < length and text[end] == "'":
                    end += 1
                if end - index >= 3:
                    state = "normal"
                index = end
                continue
            index += 1
            continue

        if char == "#":
            state = "comment"
            index += 1
            continue
        if char in "\r\n":
            reset_line()
            index += 1
            continue
        if char in " \t":
            index += 1
            continue
        if text.startswith('"""', index):
            state = "multiline-basic"
            at_statement_start = False
            index += 3
            continue
        if text.startswith("'''", index):
            state = "multiline-literal"
            at_statement_start = False
            index += 3
            continue
        if char == '"':
            state = "basic"
            at_statement_start = False
            index += 1
            continue
        if char == "'":
            state = "literal"
            at_statement_start = False
            index += 1
            continue
        if char == "[":
            if table_header_depth:
                table_header_depth += 1
            elif not containers and not root_in_value and at_statement_start:
                table_header_depth = 1
            else:
                containers.append(array_frame)
            at_statement_start = False
            index += 1
            continue
        if char == "]":
            if table_header_depth:
                table_header_depth -= 1
            elif containers and containers[-1] == array_frame:
                containers.pop()
            at_statement_start = False
            index += 1
            continue
        if char == "{":
            containers.append(inline_key_frame)
            at_statement_start = False
            index += 1
            continue
        if char == "}":
            if containers and containers[-1] in {
                inline_key_frame,
                inline_value_frame,
            }:
                containers.pop()
            at_statement_start = False
            index += 1
            continue
        if char == "=":
            if containers and containers[-1] == inline_key_frame:
                containers[-1] = inline_value_frame
            elif not containers and not table_header_depth:
                root_in_value = True
            at_statement_start = False
            index += 1
            continue
        if char == ",":
            if containers and containers[-1] == inline_value_frame:
                containers[-1] = inline_key_frame
            at_statement_start = False
            index += 1
            continue
        if (
            not in_key_context()
            and char == "0"
            and index + 1 < length
            and text[index + 1] in "bBoOxX"
        ):
            prefix = text[index + 1].lower()
            valid_digits = {
                "b": "01",
                "o": "01234567",
                "x": "0123456789abcdefABCDEF",
            }[prefix]
            index += 2
            digits = 0
            while index < length and (
                text[index] in valid_digits or text[index] == "_"
            ):
                if text[index] != "_":
                    digits += 1
                    if digits > MAX_TOML_INTEGER_DIGITS:
                        return True
                index += 1
            continue
        if not in_key_context() and "0" <= char <= "9":
            digits = 0
            while index < length and (
                "0" <= text[index] <= "9" or text[index] == "_"
            ):
                if text[index] != "_":
                    digits += 1
                    if digits > MAX_TOML_INTEGER_DIGITS:
                        return True
                index += 1
            continue
        at_statement_start = False
        index += 1
    return False


# ---------------------------------------------------------------------------
# Source mode enum
# ---------------------------------------------------------------------------

class SourceMode(str, Enum):
    """Which version of the file tree to fingerprint.

    Inherits from ``str`` so instances compare equal to their value string
    and can be passed directly to functions expecting ``str``.
    """

    HEAD = SOURCE_MODES[0]
    INDEX = SOURCE_MODES[1]
    WORKING_TREE = SOURCE_MODES[2]

    def __str__(self) -> str:  # pragma: no cover
        return self.value


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------

class BoundverError(Exception):
    """Base for all boundver-specific errors."""


class ConfigError(BoundverError, ValueError):
    """Configuration file is invalid or missing."""


class LockfileError(BoundverError, ValueError):
    """Lockfile is malformed, missing, or cannot be migrated."""


class ProviderError(BoundverError, ValueError):
    """A boundary provider failed to load or execute."""


class GuardrailError(BoundverError, ValueError):
    """A safety guardrail was triggered (file count, size, etc.)."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _is_windows_reparse_point(identity: os.stat_result) -> bool:
    """Return whether a stat sample redirects path traversal on Windows."""
    attributes = getattr(identity, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _bounded_sorted_paths(
    paths: Iterable[Path],
    *,
    max_paths: int,
    exceeded_message: str,
) -> List[Path]:
    """Materialize and sort paths without allowing an unbounded allocation.

    Callers should filter the iterable to the entries governed by their
    contract before passing it here.  One sentinel entry is inspected but not
    retained when the limit is exceeded, so both successful and failing paths
    use bounded memory.
    """
    if max_paths < 0:
        raise ValueError("Path limit must be non-negative")
    collected: List[Path] = []
    iterator = iter(paths)
    try:
        for path in iterator:
            if len(collected) >= max_paths:
                raise GuardrailError(exceeded_message)
            collected.append(path)
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    return sorted(collected)


def _iter_bounded_filesystem_paths(
    root: Path,
    *,
    recursive: bool,
    max_entries: int,
    exceeded_message: str,
    should_descend: Optional[Callable[[Path], bool]] = None,
) -> Iterator[Path]:
    """Yield directory entries lazily under one aggregate traversal limit.

    ``Path.iterdir``/``Path.rglob`` have used eager directory listings on some
    supported Python versions.  ``os.scandir`` gives us a stable lazy
    primitive. Directory symlinks are yielded but never followed, matching the
    filesystem fallback's historical behavior and avoiding traversal cycles.
    """
    if max_entries < 0:
        raise ValueError("Filesystem traversal limit must be non-negative")
    pending = [root]
    seen = 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if seen >= max_entries:
                    raise GuardrailError(exceeded_message)
                seen += 1
                path = Path(entry.path)
                yield path
                if not recursive or not entry.is_dir(follow_symlinks=False):
                    continue
                if _is_windows_reparse_point(entry.stat(follow_symlinks=False)):
                    continue
                if should_descend is None or should_descend(path):
                    pending.append(path)


def _read_bounded_path_bytes(
    full_path: Path,
    path_label: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read one stable regular file under a hard byte ceiling."""
    try:
        return read_bounded_file(
            full_path,
            max_bytes,
            path_label=path_label,
            operation="hashing",
        )
    except FileSizeLimitError as exc:
        observed = f">{max_bytes}" if exc.grew_during_read else str(exc.size)
        raise GuardrailError(
            "Hash guardrail exceeded: file too large "
            f"({observed} bytes) at {path_label}"
        ) from exc


def _is_glob(pattern: str) -> bool:
    """Return True if the pattern contains glob metacharacters."""
    return any(c in pattern for c in ("*", "?", "["))


def _validate_glob_pattern_complexity(pattern: str) -> None:
    """Reject wildcard segments that exceed the public matching contract.

    Whole declared paths have their own byte and segment limits.  These
    per-segment limits keep a single wildcard expression bounded as well,
    including when a caller reaches the matcher without config validation.
    """
    for segment in pattern.split("/"):
        if not _is_glob(segment):
            continue
        try:
            segment_bytes = len(segment.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("glob segments must contain valid Unicode") from exc
        if segment_bytes > MAX_GLOB_PATTERN_SEGMENT_BYTES:
            raise ValueError(
                "glob segments must not exceed "
                f"{MAX_GLOB_PATTERN_SEGMENT_BYTES} UTF-8 bytes"
            )
        metacharacters = sum(character in "*?[" for character in segment)
        if metacharacters > MAX_GLOB_METACHARACTERS_PER_SEGMENT:
            raise ValueError(
                "glob segments must not contain more than "
                f"{MAX_GLOB_METACHARACTERS_PER_SEGMENT} wildcard metacharacters"
            )


def _normalize_declared_path(path: str) -> str:
    """Return a canonical component/repository-relative declared path.

    The same validation is used before hashing and by config diagnostics so a
    path cannot mean one thing in one source mode and another elsewhere.
    A trailing slash is accepted for directory literals; all other redundant
    or unsafe segments are rejected.
    """
    if not isinstance(path, str):
        raise ValueError("must be a string")
    if not path or not path.strip():
        raise ValueError("must not be empty or whitespace")
    if path != path.strip():
        raise ValueError("must not have leading or trailing whitespace")
    try:
        encoded_path = path.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("must contain valid Unicode") from exc
    if len(encoded_path) > MAX_DECLARED_PATH_BYTES:
        raise ValueError(
            f"must not exceed {MAX_DECLARED_PATH_BYTES} UTF-8 bytes"
        )
    if "\\" in path:
        raise ValueError("must use '/' separators")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise ValueError("must be relative")
    normalized = path.rstrip("/")
    parts = normalized.split("/")
    if len(parts) > MAX_GLOB_SEGMENTS:
        raise ValueError(f"must not exceed {MAX_GLOB_SEGMENTS} path segments")
    if any(part == "" for part in parts):
        raise ValueError("must not contain empty path segments")
    if "." in parts:
        raise ValueError("must not contain '.' path segments")
    if ".." in parts:
        raise ValueError(
            "must not contain '..' path segments; the path escapes its declared root"
        )
    _validate_glob_pattern_complexity(normalized)
    return normalized


_GLOB_LITERAL = 0
_GLOB_ANY = 1
_GLOB_STAR = 2
_GLOB_CLASS = 3


def _normalize_glob_class_chunks(
    content: str,
    spend_step: Callable[[], None],
) -> Tuple[str, ...]:
    """Normalize one character class using stable ``fnmatch`` semantics.

    CPython 3.10+ removes reversed ranges before compiling a shell class.  That
    normalization has observable edge cases: for example, ``[a--c]`` becomes
    ``[c]`` and ``[a--!]`` becomes a single-character wildcard.  Reproduce the
    normalization directly so supported runtimes agree without invoking the
    regex engine.  Hyphens retained inside a chunk are literals; hyphens
    between chunks are ranges.
    """
    chunks: List[str] = []
    chunk_start = 0
    search_index = 2 if content.startswith("!") else 1
    content_length = len(content)

    # A leading hyphen (or one immediately after ``!``) is literal.  After a
    # range separator, skip its two endpoints before looking for another range
    # separator.  This is the bounded equivalent of fnmatch's class splitting.
    while search_index < content_length:
        spend_step()
        if content[search_index] != "-":
            search_index += 1
            continue
        chunks.append(content[chunk_start:search_index])
        chunk_start = search_index + 1
        search_index += 3

    if not chunks:
        return (content,)

    tail = content[chunk_start:]
    if tail:
        chunks.append(tail)
    else:
        # A final hyphen is literal rather than a range separator.
        chunks[-1] += "-"

    # Remove reversed ranges from right to left exactly as fnmatch does.  An
    # invalid range can collapse the complete class to an empty or negated
    # empty class, represented here without a regex.
    for chunk_index in range(len(chunks) - 1, 0, -1):
        spend_step()
        previous = chunks[chunk_index - 1]
        current = chunks[chunk_index]
        if previous[-1] > current[0]:
            chunks[chunk_index - 1] = previous[:-1] + current[1:]
            del chunks[chunk_index]

    return tuple(chunks)


def _compile_glob_class(
    content: str,
    spend_step: Callable[[], None],
) -> Tuple[bool, Tuple[Tuple[int, int], ...]]:
    """Compile a normalized shell class to bounded code-point intervals."""
    chunks = _normalize_glob_class_chunks(content, spend_step)
    # Keep normalized range separators distinct from literal hyphens retained
    # inside chunks.  Removing a leading negation marker can move a separator
    # to the start of the class (for example ``[a-!!-a]`` normalizes to
    # ``[^-a]``); a leading or trailing separator is a literal hyphen rather
    # than a range operator.
    units: List[Tuple[bool, str]] = []
    for chunk_index, chunk in enumerate(chunks):
        for character in chunk:
            spend_step()
            units.append((False, character))
        if chunk_index < len(chunks) - 1:
            spend_step()
            units.append((True, "-"))

    negated = bool(units and not units[0][0] and units[0][1] == "!")
    unit_start = 1 if negated else 0
    range_separators: Set[int] = set()
    for unit_index in range(unit_start, len(units)):
        spend_step()
        is_separator, _character = units[unit_index]
        if (
            is_separator
            and unit_index > unit_start
            and unit_index + 1 < len(units)
            and not units[unit_index - 1][0]
            and not units[unit_index + 1][0]
        ):
            range_separators.add(unit_index)

    intervals: List[Tuple[int, int]] = []
    for unit_index in range(unit_start, len(units)):
        spend_step()
        is_separator, character = units[unit_index]
        if is_separator:
            if unit_index in range_separators:
                intervals.append(
                    (
                        ord(units[unit_index - 1][1]),
                        ord(units[unit_index + 1][1]),
                    )
                )
            else:
                intervals.append((ord("-"), ord("-")))
            continue
        if (
            unit_index - 1 in range_separators
            or unit_index + 1 in range_separators
        ):
            continue
        value = ord(character)
        intervals.append((value, value))

    return negated, tuple(intervals)


def _compile_text_glob(
    pattern: str,
    spend_step: Callable[[], None],
) -> Tuple[tuple, ...]:
    """Compile one shell-style text glob without constructing a regex."""
    tokens: List[tuple] = []
    index = 0
    pattern_length = len(pattern)
    while index < pattern_length:
        spend_step()
        character = pattern[index]
        if character == "*":
            if not tokens or tokens[-1][0] != _GLOB_STAR:
                tokens.append((_GLOB_STAR, None))
            index += 1
            continue
        if character == "?":
            tokens.append((_GLOB_ANY, None))
            index += 1
            continue
        if character != "[":
            tokens.append((_GLOB_LITERAL, character))
            index += 1
            continue

        # Match fnmatch's documented class grammar: ! negates a class, ] is
        # literal in the first class position, and an unclosed [ is literal.
        closing = index + 1
        if closing < pattern_length and pattern[closing] == "!":
            closing += 1
        if closing < pattern_length and pattern[closing] == "]":
            closing += 1
        while closing < pattern_length and pattern[closing] != "]":
            spend_step()
            closing += 1
        if closing >= pattern_length:
            tokens.append((_GLOB_LITERAL, "["))
            index += 1
            continue

        content = pattern[index + 1 : closing]
        tokens.append((_GLOB_CLASS, _compile_glob_class(content, spend_step)))
        index = closing + 1
    return tuple(tokens)


def _glob_token_matches(
    token: tuple,
    character: str,
    spend_step: Callable[[], None],
) -> bool:
    kind, value = token
    if kind == _GLOB_LITERAL:
        return character == value
    if kind == _GLOB_ANY:
        return True
    if kind != _GLOB_CLASS:  # pragma: no cover - internal compiler invariant
        return False
    negated, intervals = value
    codepoint = ord(character)
    matched = False
    for lower, upper in intervals:
        spend_step()
        if lower <= codepoint <= upper:
            matched = True
            break
    return not matched if negated else matched


def _bounded_text_equal(
    candidate: str,
    pattern: str,
    spend_step: Callable[[], None],
) -> bool:
    if len(candidate) != len(pattern):
        spend_step()
        return False
    for candidate_character, pattern_character in zip(candidate, pattern):
        spend_step()
        if candidate_character != pattern_character:
            return False
    return True


def _match_compiled_text_glob(
    candidate: str,
    tokens: Tuple[tuple, ...],
    spend_step: Callable[[], None],
) -> bool:
    """Run a bounded NFA over one already-compiled text glob."""
    token_count = len(tokens)

    def epsilon_closure(states: Set[int]) -> Set[int]:
        closed = set(states)
        pending = list(states)
        while pending:
            token_index = pending.pop()
            spend_step()
            if (
                token_index < token_count
                and tokens[token_index][0] == _GLOB_STAR
                and token_index + 1 not in closed
            ):
                closed.add(token_index + 1)
                pending.append(token_index + 1)
        return closed

    states = epsilon_closure({0})
    for character in candidate:
        next_states: Set[int] = set()
        for token_index in states:
            spend_step()
            if token_index >= token_count:
                continue
            token = tokens[token_index]
            if token[0] == _GLOB_STAR:
                next_states.add(token_index)
            elif _glob_token_matches(token, character, spend_step):
                next_states.add(token_index + 1)
        states = epsilon_closure(next_states)
        if not states:
            return False
    states = epsilon_closure(states)
    return token_count in states


def _match_text_glob(
    candidate: str,
    pattern: str,
    *,
    _step_consumer: Optional[Callable[[int], None]] = None,
) -> bool:
    """Match a bounded shell-style glob against text, including ``/``."""
    if not isinstance(candidate, str) or not isinstance(pattern, str):
        return False
    try:
        candidate_bytes = len(candidate.encode("utf-8", errors="surrogateescape"))
        pattern_bytes = len(pattern.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    if candidate_bytes > MAX_GLOB_PATH_BYTES:
        raise GuardrailError(
            "Glob match guardrail exceeded: candidate path exceeds "
            f"{MAX_GLOB_PATH_BYTES} UTF-8 bytes"
        )
    if pattern_bytes > MAX_DECLARED_PATH_BYTES:
        raise GuardrailError(
            "Glob match guardrail exceeded: pattern exceeds "
            f"{MAX_DECLARED_PATH_BYTES} UTF-8 bytes"
        )
    try:
        _validate_glob_pattern_complexity(pattern)
    except ValueError as exc:
        raise GuardrailError(f"Glob match guardrail exceeded: {exc}") from exc

    steps = 0

    def spend_step() -> None:
        nonlocal steps
        steps += 1
        if steps > MAX_GLOB_MATCH_STEPS:
            raise GuardrailError(
                "Glob match guardrail exceeded: more than "
                f"{MAX_GLOB_MATCH_STEPS} matcher steps"
            )
        if _step_consumer is not None:
            _step_consumer(1)

    if not _is_glob(pattern):
        return _bounded_text_equal(candidate, pattern, spend_step)
    tokens = _compile_text_glob(pattern, spend_step)
    return _match_compiled_text_glob(candidate, tokens, spend_step)


def _match_path_glob(
    path: str,
    pattern: str,
    *,
    _step_consumer: Optional[Callable[[int], None]] = None,
    _allow_descendants: bool = False,
) -> bool:
    """Match a POSIX path with deterministic, segment-aware glob semantics.

    Ordinary wildcard segments use a bounded NFA, so ``*``, ``?``, and
    character classes never cross ``/``.  A segment that is exactly ``**``
    consumes zero or more complete path segments.  Matching is always
    case-sensitive and includes leading-dot names.

    Internal bounded-analysis callers may supply ``_step_consumer`` to charge
    every NFA state/epsilon transition to an aggregate work budget.  The
    ordinary two-argument API and boolean result remain unchanged.
    """
    if not isinstance(path, str) or not isinstance(pattern, str):
        return False
    if path.startswith("/") or pattern.startswith("/"):
        return False
    try:
        path_bytes = len(path.encode("utf-8", errors="surrogateescape"))
        pattern_bytes = len(pattern.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    if path_bytes > MAX_GLOB_PATH_BYTES:
        raise GuardrailError(
            "Glob match guardrail exceeded: candidate path exceeds "
            f"{MAX_GLOB_PATH_BYTES} UTF-8 bytes"
        )
    if pattern_bytes > MAX_DECLARED_PATH_BYTES:
        raise GuardrailError(
            "Glob match guardrail exceeded: pattern exceeds "
            f"{MAX_DECLARED_PATH_BYTES} UTF-8 bytes"
        )
    path_parts = tuple(path.split("/")) if path else tuple()
    pattern_parts = tuple(pattern.split("/")) if pattern else tuple()
    if any(part == "" for part in path_parts + pattern_parts):
        return False
    if len(path_parts) > MAX_GLOB_SEGMENTS:
        raise GuardrailError(
            "Glob match guardrail exceeded: candidate path exceeds "
            f"{MAX_GLOB_SEGMENTS} segments"
        )
    if len(pattern_parts) > MAX_GLOB_SEGMENTS:
        raise GuardrailError(
            "Glob match guardrail exceeded: pattern exceeds "
            f"{MAX_GLOB_SEGMENTS} segments"
        )
    try:
        _validate_glob_pattern_complexity(pattern)
    except ValueError as exc:
        raise GuardrailError(f"Glob match guardrail exceeded: {exc}") from exc

    # Consecutive recursive wildcards are equivalent to one and needlessly
    # multiply the state space.
    collapsed: List[str] = []
    for token in pattern_parts:
        if token != "**" or not collapsed or collapsed[-1] != "**":
            collapsed.append(token)
    pattern_parts = tuple(collapsed)
    steps = 0

    def spend_step() -> None:
        nonlocal steps
        steps += 1
        if steps > MAX_GLOB_MATCH_STEPS:
            raise GuardrailError(
                "Glob match guardrail exceeded: more than "
                f"{MAX_GLOB_MATCH_STEPS} matcher steps"
            )
        if _step_consumer is not None:
            _step_consumer(1)

    compiled_parts = []
    for token in pattern_parts:
        if token == "**":
            compiled_parts.append(("recursive", None))
        elif _is_glob(token):
            compiled_parts.append(("glob", _compile_text_glob(token, spend_step)))
        else:
            compiled_parts.append(("literal", token))
    compiled_parts = tuple(compiled_parts)
    pattern_count = len(compiled_parts)

    def epsilon_closure(states: Set[int]) -> Set[int]:
        closed = set(states)
        pending = list(states)
        while pending:
            index = pending.pop()
            spend_step()
            if (
                index < pattern_count
                and compiled_parts[index][0] == "recursive"
                and index + 1 not in closed
            ):
                closed.add(index + 1)
                pending.append(index + 1)
        return closed

    states = epsilon_closure({0})
    for segment in path_parts:
        next_states: Set[int] = set()
        for index in states:
            spend_step()
            if index >= pattern_count:
                continue
            kind, value = compiled_parts[index]
            if kind == "recursive":
                next_states.add(index)
            elif kind == "literal" and _bounded_text_equal(
                segment, value, spend_step
            ):
                next_states.add(index + 1)
            elif kind == "glob" and _match_compiled_text_glob(
                segment, value, spend_step
            ):
                next_states.add(index + 1)
        states = epsilon_closure(next_states)
        if _allow_descendants and pattern_count in states:
            return True
        if not states:
            return False
    states = epsilon_closure(states)
    return pattern_count in states


_FACET_ISSUE_RE = re.compile(
    r"^(?:MISMATCH|SLICE MISMATCH|UNAVAILABLE FACET) "
    r".+\.(exact|behavior|boundary|compat):",
    re.DOTALL,
)


def _issue_facet(message: str) -> Optional[str]:
    """Return the structured facet encoded by a verification issue."""
    match = _FACET_ISSUE_RE.match(message)
    return match.group(1) if match else None


def _available_component_facets(component: Mapping[str, object]) -> Set[str]:
    """Return facets the declaration can intentionally produce.

    This is the policy-free fallback used by verify and its JSON policy view.
    It describes declared capability, not computation success: provider or
    source failures are reported independently as digest errors.
    """
    available = {"exact"}
    boundary = component.get("boundary")
    if isinstance(boundary, dict):
        provider = boundary_provider_name(boundary)
        paths = boundary.get("paths", [])
        if provider not in {"leaf", "implicit"} or (
            provider == "implicit" and isinstance(paths, list) and bool(paths)
        ):
            available.add("boundary")
    behavior = component.get("behavior")
    if isinstance(behavior, dict):
        paths = behavior.get("paths")
        if isinstance(paths, list) and bool(paths):
            available.add("behavior")
    if isinstance(component.get("version_source"), dict):
        available.add("compat")
    return available


def _effective_component_facets(
    config: Mapping[str, object],
    component_name: str,
    explicit_facets: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Return the facets that gate one component under the effective policy.

    This is shared by verification and diagnostic commands so ``why`` and
    ``status`` cannot classify a configured non-gating observation as a gate
    failure.  An explicit CLI-wide policy wins, followed by a component
    override, configured defaults, and finally all facets the declaration can
    intentionally produce.
    """
    if explicit_facets is not None:
        return {facet for facet in explicit_facets if isinstance(facet, str)}

    components = config.get("components", {})
    component = (
        components.get(component_name, {})
        if isinstance(components, Mapping)
        else {}
    )
    if not isinstance(component, Mapping):
        return set()
    component_facets = component.get("verify_facets")
    if isinstance(component_facets, list) and all(
        isinstance(facet, str) for facet in component_facets
    ):
        return set(component_facets)

    defaults = config.get("defaults", {})
    if isinstance(defaults, Mapping) and "verify_facets" in defaults:
        default_facets = defaults.get("verify_facets")
        if isinstance(default_facets, list) and all(
            isinstance(facet, str) for facet in default_facets
        ):
            return set(default_facets)
        return set()
    return _available_component_facets(component)


def boundary_provider_name(boundary: Mapping[str, object]) -> str:
    """Return boundary provider name from a component's boundary config."""
    provider = boundary.get("provider")
    return provider if isinstance(provider, str) and provider else "unknown"


def _short(h: Optional[str]) -> str:
    """Truncate a hex digest for display."""
    if h is None:
        return "none"
    return h[:12] + "..."


def _is_within(base: Path, candidate: Path) -> bool:
    """Return True if candidate path is within base path."""
    try:
        candidate.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False
