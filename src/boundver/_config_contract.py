"""Field-name contracts shared by schema-independent config validation."""

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
