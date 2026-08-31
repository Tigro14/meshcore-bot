#!/usr/bin/env python3
"""
Version resolution utilities for MeshCore Bot.

Centralizes runtime version lookup so bot command output, web viewer, and
services all report consistent version information.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

# A release-looking version: "v" then a digit. Distinguishes a real release from
# the installer's "dev-<sha>" fallback and from tag names like "nightly".
_RELEASE_VERSION_RE = re.compile(r"^v\d")


def _normalize_tag(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    # Only version-looking values get the conventional "v" prefix. install-service.sh
    # writes installer_version = "dev-<sha>" for untagged checkouts, and a repo may
    # carry tags like "nightly" — prefixing those produced "vdev-abc1234"/"vnightly".
    return f"v{value}" if value[0].isdigit() else value


def _is_release_version(value: str | None) -> bool:
    """True when `value` names an actual release, not a dev/branch identifier."""
    return bool(value and _RELEASE_VERSION_RE.match(value))


def _safe_git_run(repo_root: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root)] + args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        out = (result.stdout or "").strip()
        return out or None
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def _read_version_file(repo_root: Path) -> str | None:
    version_file = repo_root / ".version_info"
    if not version_file.is_file():
        return None
    try:
        with open(version_file, encoding="utf-8") as fh:
            data = json.load(fh)
        version = data.get("installer_version") or data.get("tag")
        return _normalize_tag(version)
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _read_pyproject_version(repo_root: Path) -> str | None:
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return None
    try:
        text = pyproject_path.read_text(encoding="utf-8")
    except OSError:
        return None

    in_project_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_project_section = line == "[project]"
            continue
        if not in_project_section:
            continue
        match = re.match(r'version\s*=\s*"([^"]+)"', line)
        if match:
            return _normalize_tag(match.group(1))
    return None


def get_application_root() -> Path:
    """Return the installed/source directory containing the bot application.

    Runtime configuration may live elsewhere (for example, ``/etc/meshcore-bot``
    or ``/data/config``), so version metadata must be resolved relative to this
    module rather than relative to the config file.
    """
    return Path(__file__).resolve().parent.parent


def resolve_application_version() -> dict[str, str | None]:
    """Resolve version metadata for the running bot application."""
    return resolve_runtime_version(get_application_root())


def resolve_runtime_version(repo_root: Path | str) -> dict[str, str | None]:
    """Resolve version metadata and a single runtime display value.

    Returns a dict with:
      - baked: release-like version from env/.version_info/pyproject (v-prefixed)
      - tag: the release this checkout actually *is*, else None. Deliberately not
        ``baked``: a source checkout bakes in the in-progress pyproject version —
        which on a release-prep commit names a tag that does not exist yet — and
        reporting that as the running release is how the viewer footer and the
        version command came to disagree. Only a value git corroborates (or an
        explicit override) becomes a tag.
      - branch, commit, date: git metadata when available (branch is None when
        HEAD is detached)
      - display: final runtime version string; the single value UI should show
    """
    root = Path(repo_root).resolve()

    env_version = _normalize_tag(os.environ.get("MESHCORE_BOT_VERSION", "").strip())
    file_version = _read_version_file(root)
    pyproject_version = _read_pyproject_version(root)
    baked = env_version or file_version or pyproject_version

    branch = _safe_git_run(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    commit = _safe_git_run(root, ["rev-parse", "--short", "HEAD"])
    date_raw = _safe_git_run(root, ["show", "-s", "--format=%ci", "HEAD"])
    date = None
    if date_raw:
        # %ci format is "YYYY-MM-DD HH:MM:SS +TZ"; keep date only.
        date = date_raw.split()[0] if " " in date_raw else date_raw

    # Detached HEAD: rev-parse reports the literal string "HEAD", not a branch name.
    detached = branch == "HEAD"
    if detached:
        branch = None

    # The tag HEAD actually sits on, if any. Checked on every checkout shape rather
    # than only detached ones: `main` two commits past v1.0.0 is not v1.0.0 (and a
    # release-prep commit bumps pyproject *before* the tag exists), while a release
    # branch parked exactly on the tag is.
    exact_tag: str | None = None
    if commit:
        exact_tag = _normalize_tag(
            _safe_git_run(root, ["describe", "--tags", "--exact-match", "HEAD"])
        )

    display: str | None
    tag: str | None
    if env_version:
        # An explicit override is authoritative — pinning the reported version is
        # the only reason to set it.
        display = env_version
    elif exact_tag:
        display = exact_tag
    elif branch and commit:
        display = f"{branch}-{commit}"
    elif detached and commit:
        display = f"detached-{commit}"
    else:
        # No usable git metadata: an installed release, which writes .version_info.
        display = baked or "unknown"
    # `tag` is a release identity, so it only survives when the value looks like one.
    tag = display if _is_release_version(display) else None

    return {
        "baked": baked,
        "tag": tag,
        "branch": branch,
        "commit": commit,
        "date": date,
        "display": display,
    }
