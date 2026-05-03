"""Version extraction and parsing utilities."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

_FALLBACK_WARNED: set = set()  # Track which fallback warnings we've already emitted


def _warn_fallback(parser_type: str) -> None:
    """Emit a one-time stderr warning when a regex fallback parser is used."""
    if parser_type not in _FALLBACK_WARNED:
        _FALLBACK_WARNED.add(parser_type)
        lib = "tomli (or Python 3.11+)" if parser_type == "toml" else "PyYAML"
        print(
            f"warning: using regex fallback for {parser_type} parsing "
            f"(install {lib} for reliable results)",
            file=sys.stderr,
        )

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

try:
    import yaml  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    yaml = None


def extract_version(
    repo_root: Path,
    component_path: str,
    version_source: Optional[dict],
    git_latest_tag_fn: Optional[Callable[[Path, str], Optional[str]]] = None,
    read_file_fn: Optional[Callable[[str], bytes]] = None,
) -> Optional[str]:
    """Extract version from a version_source config.

    If *read_file_fn* is provided, it is used to read file content (supporting
    head/index/working-tree sources). Otherwise falls back to disk reads.
    """
    if version_source is None:
        return None
    if "git_tag_prefix" in version_source:
        if git_latest_tag_fn is None:
            return None
        return git_latest_tag_fn(repo_root, version_source["git_tag_prefix"])
    file_rel = version_source.get("file")
    field_path = version_source.get("field")
    if not file_rel or not field_path:
        return None
    # Reject path traversal in version_source.file
    if ".." in file_rel.replace("\\", "/").split("/"):
        return None
    repo_rel = f"{component_path}/{file_rel}"
    if read_file_fn is not None:
        try:
            raw = read_file_fn(repo_rel)
        except (OSError, subprocess.CalledProcessError, FileNotFoundError):
            return None
        return _extract_field_from_bytes(raw, file_rel, field_path)
    # Fallback: read from disk
    full_path = repo_root / component_path / file_rel
    # Verify resolved path stays within repository
    try:
        full_path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None
    if not full_path.exists():
        return None
    # Reject oversized version-source files (10 MiB cap).
    _MAX_VERSION_FILE = 10 * 1024 * 1024
    try:
        if full_path.stat().st_size > _MAX_VERSION_FILE:
            return None
    except OSError:
        return None
    if file_rel.endswith('.json'):
        return _extract_json_field(full_path, field_path)
    if file_rel.endswith('.toml'):
        return _extract_toml_field(full_path, field_path)
    if file_rel.endswith('.yaml') or file_rel.endswith('.yml'):
        return _extract_yaml_field(full_path, field_path)
    return None


def _extract_field_from_bytes(raw: bytes, file_rel: str, field_path: str) -> Optional[str]:
    """Extract a field from raw file bytes based on file extension."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if file_rel.endswith('.json'):
        return _extract_json_from_text(text, field_path)
    if file_rel.endswith('.toml'):
        return _extract_toml_from_text(text, field_path)
    if file_rel.endswith('.yaml') or file_rel.endswith('.yml'):
        return _extract_yaml_from_text(text, field_path)
    return None


def _extract_json_from_text(text: str, field_path: str) -> Optional[str]:
    try:
        data = json.loads(text)
        for key in field_path.split('.'):
            data = data[key]
        return str(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _extract_toml_from_text(text: str, field_path: str) -> Optional[str]:
    keys = field_path.split('.')
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except (MemoryError, RecursionError):
            raise
        except Exception:
            return None
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return str(current) if current is not None else None
    # Fallback regex parser — tomllib unavailable
    _warn_fallback("toml")
    current_section = ''
    if len(keys) >= 2:
        target_section = '.'.join(keys[:-1])
        target_key = keys[-1]
    else:
        target_section = ''
        target_key = keys[0]
    for line in text.splitlines():
        line = line.strip()
        section_match = re.match(r"^\[([^\]]+)\]", line)
        if section_match:
            current_section = section_match.group(1).strip()
            continue
        if current_section == target_section:
            kv_match = re.match(r'^(\w[\w-]*)\s*=\s*(?:"([^"]*)"|' "'([^']*)'" r'|(\S+))', line)
            if kv_match and kv_match.group(1) == target_key:
                # Check groups explicitly — empty string is a valid value
                if kv_match.group(2) is not None:
                    return kv_match.group(2)
                if kv_match.group(3) is not None:
                    return kv_match.group(3)
                return kv_match.group(4)
    return None


def _extract_yaml_from_text(text: str, field_path: str) -> Optional[str]:
    keys = field_path.split('.')
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except (MemoryError, RecursionError):
            raise
        except Exception:
            return None  # Real parser failed — don't fall back to regex; be authoritative
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None  # Field not found — authoritative answer from real parser
            current = current[key]
        return str(current) if current is not None else None
    # Fallback regex parser — PyYAML unavailable
    _warn_fallback("yaml")
    indent_stack: list = []
    current_path: list = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        while indent_stack and indent <= indent_stack[-1]:
            indent_stack.pop()
            if current_path:
                current_path.pop()
        kv_match = re.match(r"^([\w.-]+)\s*:\s*(.+)$", stripped)
        if kv_match:
            key = kv_match.group(1)
            raw_value = kv_match.group(2).strip()
            # Strip inline comments (but not from quoted values)
            if raw_value.startswith("'") or raw_value.startswith('"'):
                quote = raw_value[0]
                end = raw_value.find(quote, 1)
                value = raw_value[1:end] if end > 0 else raw_value.strip("'\"")
            else:
                # Unquoted: strip inline comment
                comment_idx = raw_value.find(' #')
                value = raw_value[:comment_idx].rstrip() if comment_idx >= 0 else raw_value
            test_path = current_path + [key]
            if test_path == keys:
                return value
        else:
            section_match = re.match(r"^([\w.-]+)\s*:\s*$", stripped)
            if section_match:
                current_path.append(section_match.group(1))
                indent_stack.append(indent)
    return None


def _extract_json_field(path: Path, field_path: str) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in field_path.split('.'):
            data = data[key]
        return str(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _extract_toml_field(path: Path, field_path: str) -> Optional[str]:
    text = path.read_text(encoding="utf-8")
    keys = field_path.split('.')
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except (MemoryError, RecursionError):
            raise
        except Exception:
            return None
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return str(current) if current is not None else None

    _warn_fallback("toml")
    current_section = ''
    if len(keys) >= 2:
        target_section = '.'.join(keys[:-1])
        target_key = keys[-1]
    else:
        target_section = ''
        target_key = keys[0]

    for line in text.splitlines():
        line = line.strip()
        section_match = re.match(r"^\[([^\]]+)\]", line)
        if section_match:
            current_section = section_match.group(1).strip()
            continue
        if current_section == target_section:
            kv_match = re.match(r'^(\w[\w-]*)\s*=\s*(?:"([^"]*)"|' "'([^']*)'" r'|(\S+))', line)
            if kv_match and kv_match.group(1) == target_key:
                # Check groups explicitly — empty string is a valid value
                if kv_match.group(2) is not None:
                    return kv_match.group(2)
                if kv_match.group(3) is not None:
                    return kv_match.group(3)
                return kv_match.group(4)
    return None


def _extract_yaml_field(path: Path, field_path: str) -> Optional[str]:
    text = path.read_text(encoding="utf-8")
    keys = field_path.split('.')
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except (MemoryError, RecursionError):
            raise
        except Exception:
            return None  # Real parser failed — don't fall back to regex; be authoritative
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None  # Field not found — authoritative answer from real parser
            current = current[key]
        return str(current) if current is not None else None

    _warn_fallback("yaml")
    indent_stack = []
    current_path = []
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith('#'):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        while indent_stack and indent <= indent_stack[-1]:
            indent_stack.pop()
            if current_path:
                current_path.pop()
        kv_match = re.match(r"^([\w.-]+)\s*:\s*(.+)$", stripped)
        if kv_match:
            key = kv_match.group(1)
            raw_value = kv_match.group(2).strip()
            if raw_value.startswith("'") or raw_value.startswith('"'):
                quote = raw_value[0]
                end = raw_value.find(quote, 1)
                value = raw_value[1:end] if end > 0 else raw_value.strip("'\"")
            else:
                comment_idx = raw_value.find(' #')
                value = raw_value[:comment_idx].rstrip() if comment_idx >= 0 else raw_value
            test_path = current_path + [key]
            if test_path == keys:
                return value
        else:
            section_match = re.match(r"^([\w.-]+)\s*:\s*$", stripped)
            if section_match:
                current_path.append(section_match.group(1))
                indent_stack.append(indent)
    return None


def parse_semver(version: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not version:
        return (None, None, None)
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", version)
    if not match:
        return (None, None, version)
    major = match.group(1)
    minor = match.group(2)
    patch = match.group(3) or '0'
    return (major, f"{major}.{minor}", f"{major}.{minor}.{patch}")
