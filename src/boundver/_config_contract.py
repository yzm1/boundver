"""Field-name contracts shared by schema-independent config validation."""

import re
from typing import Optional

MAX_CONSUMER_GRAPH_ITEMS = 10_000
MAX_CONSUMER_IDENTIFIER_CHARS = 16_384
MAX_GIT_TAG_PREFIX_CHARS = 4_096

COMPONENT_FILTER_DELIMITER = ","
# Python's ``str.strip`` whitespace set, encoded explicitly so the public JSON
# Schema and the dependency-free validator reject the same edge characters
# across regex engines. Internal whitespace remains valid and addressable.
COMPONENT_IDENTIFIER_PATTERN = (
    r"^(?![\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680"
    r"\u2000-\u200a\u2028\u2029\u202f\u205f\u3000])"
    r"(?![\s\S]*[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680"
    r"\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$)[^,]+$"
)

# This is the Git check-ref-format grammar applied to ``prefix + "0.0.0"``.
# The final prefix component may therefore end in ``/``, ``.``, or ``.lock``
# when appending a SemVer turns it into a valid component. Earlier components
# must already be complete and valid. Keep the public schema pattern identical.
GIT_TAG_PREFIX_PATTERN = (
    r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)"
    r"(?!.*[\u0000-\u0020\u007f~^:?*\[\\])"
    r"(?!\.)(?!.*/\.)(?![^/]*\.lock/)(?!.*/[^/]*\.lock/).+$"
)
_GIT_TAG_PREFIX_RE = re.compile(GIT_TAG_PREFIX_PATTERN)


def git_tag_prefix_error(value: object) -> Optional[str]:
    """Explain why *value* cannot prefix a literal, valid Git tag."""
    if type(value) is not str or not value:
        return "must be a non-empty string"
    if len(value) > MAX_GIT_TAG_PREFIX_CHARS:
        return f"must not exceed {MAX_GIT_TAG_PREFIX_CHARS} characters"
    if _GIT_TAG_PREFIX_RE.fullmatch(value) is None:
        return (
            "must be a literal prefix that can form a valid Git tag; forbidden "
            "forms include whitespace or controls, wildcards, backslashes, "
            "'..', '@{', empty or dot-prefixed path components, and completed "
            "components ending in '.lock'"
        )
    return None


def component_identifier_problem(
    value: object,
    *,
    max_chars: int = MAX_CONSUMER_IDENTIFIER_CHARS,
) -> Optional[str]:
    """Explain why a configured component name cannot be addressed safely.

    CLI, GitHub Action, and GitLab Catalog component subsets share a
    comma-separated transport.  Keeping that transport intentionally simple
    requires configured names (and every internal reference to them) to avoid
    the delimiter and the whitespace stripped around each filter token.
    """
    if not isinstance(value, str) or not value:
        return "must be a non-empty string"
    if len(value) > max_chars:
        return f"exceeds the {max_chars}-character limit"
    if value != value.strip():
        return "must not have leading or trailing whitespace"
    if COMPONENT_FILTER_DELIMITER in value:
        return (
            "must not contain ',' because CLI, GitHub Action, and GitLab "
            "component filters are comma-separated"
        )
    return None


ROOT_FIELDS = frozenset(
    {"$schema", "project", "providers", "defaults", "components", "slices"}
)
DEFAULT_FIELDS = frozenset({"compat_mode", "verify_facets"})
PROVIDER_FIELDS = frozenset({"module", "class", "name"})
COMPONENT_FIELDS = frozenset(
    {
        "path",
        "ecosystem",
        "note",
        "version_source",
        "boundary",
        "behavior",
        "vendored_copies",
        "consumers",
        "external_consumers",
        "verify_facets",
    }
)
BOUNDARY_FIELDS = frozenset({"provider", "paths", "options", "note"})
BEHAVIOR_FIELDS = frozenset({"paths"})
VERSION_FILE_FIELDS = frozenset({"file", "field"})
VERSION_TAG_FIELDS = frozenset({"git_tag_prefix"})
VERSION_SOURCE_FIELDS = VERSION_FILE_FIELDS | VERSION_TAG_FIELDS
SLICE_FIELDS = frozenset({"description", "mode", "components", "closure_of"})
