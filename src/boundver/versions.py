"""Version extraction and parsing utilities."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

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
) -> Optional[str]:
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
    full_path = repo_root / component_path / file_rel
    if not full_path.exists():
        return None
    if file_rel.endswith('.json'):
        return _extract_json_field(full_path, field_path)
    if file_rel.endswith('.toml'):
        return _extract_toml_field(full_path, field_path)
    if file_rel.endswith('.yaml') or file_rel.endswith('.yml'):
        return _extract_yaml_field(full_path, field_path)
    return None


def _extract_json_field(path: Path, field_path: str) -> Optional[str]:
    try:
        data = json.loads(path.read_text())
        for key in field_path.split('.'):
            data = data[key]
        return str(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _extract_toml_field(path: Path, field_path: str) -> Optional[str]:
    text = path.read_text()
    keys = field_path.split('.')
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except Exception:
            return None
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        return str(current) if current is not None else None

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
            kv_match = re.match(r'^(\w[\w-]*)\s*=\s*("?)([^"]*)\2', line)
            if kv_match and kv_match.group(1) == target_key:
                return kv_match.group(3)
    return None


def _extract_yaml_field(path: Path, field_path: str) -> Optional[str]:
    text = path.read_text()
    keys = field_path.split('.')
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except Exception:
            data = None
        current: Any = data
        for key in keys:
            if not isinstance(current, dict) or key not in current:
                current = None
                break
            current = current[key]
        if current is not None:
            return str(current)

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
            value = kv_match.group(2).strip().strip("'\"")
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
