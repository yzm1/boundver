"""Compare recorded/resolved version values without claiming compatibility."""

from .versions import parse_semver


def resolved_version_change(previous: dict, current: dict) -> dict:
    """Unavailable values are not equal versions or deployment evidence."""
    old, new = previous.get("version"), current.get("version")
    available = (
        isinstance(old, str)
        and isinstance(new, str)
        and parse_semver(old)[0] is not None
        and parse_semver(new)[0] is not None
        and not previous.get("version_errors")
        and not current.get("version_errors")
    )
    return {
        "previous": old if isinstance(old, str) else None,
        "current": new if isinstance(new, str) else None,
        "status": ("changed" if old != new else "unchanged")
        if available
        else "unavailable",
    }
