#!/usr/bin/env python3
"""Move legacy relative runtime paths into service-owned directories safely."""

from __future__ import annotations

import argparse
import configparser
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# This script is invoked from an installed source tree before the virtualenv is
# ready, so make the adjacent application package importable explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modules.ini_writer import update_ini_values  # noqa: E402


def _copy_sqlite(source: Path, target: Path) -> None:
    """Atomically create a verified SQLite copy, including committed WAL data."""
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".migrating",
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        target_conn = sqlite3.connect(temporary)
        try:
            source_conn.backup(target_conn)
            result = target_conn.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise sqlite3.DatabaseError(f"migrated database failed integrity_check: {result!r}")
        finally:
            target_conn.close()
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as migrated_file:
            os.fsync(migrated_file.fileno())
        os.replace(temporary, target)
        # Persist the rename itself where directory fsync is supported.
        try:
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        source_conn.close()
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _backup_path(path: Path, label: str) -> Path:
    """Return a collision-safe sibling path for a pre-migration backup."""

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if path.suffix and path.is_file():
        base_name = f"{path.stem}.{label}-{timestamp}"
        candidate = path.with_name(f"{base_name}{path.suffix}")
    else:
        base_name = f"{path.name}.{label}-{timestamp}"
        candidate = path.with_name(base_name)
    counter = 1
    while candidate.exists():
        if path.suffix and path.is_file():
            candidate = path.with_name(f"{base_name}-{counter}{path.suffix}")
        else:
            candidate = path.with_name(f"{base_name}-{counter}")
        counter += 1
    return candidate


def _migrate_sqlite(source: Path, target: Path) -> None:
    """Make the config-selected legacy DB authoritative without losing target data."""

    if source == target or not source.is_file():
        return
    if target.exists():
        # A relative config value proves the legacy source is still the active
        # database.  Preserve any stale/partial prior target before replacing it
        # instead of silently switching the service to that target.
        _copy_sqlite(target, _backup_path(target, "pre-layout"))
    _copy_sqlite(source, target)


def _migrate_runtime_database_paths(
    parser: configparser.ConfigParser,
    legacy_base: Path,
    state_dir: Path,
    updates: dict[str, dict[str, str]],
) -> None:
    """Migrate Bot and optional split Web_Viewer DB paths without collisions."""

    configured: list[tuple[str, str, str]] = [
        ("Bot", "db_path", parser.get("Bot", "db_path", fallback="meshcore_bot.db").strip()),
    ]
    viewer_raw = parser.get("Web_Viewer", "db_path", fallback="").strip()
    if viewer_raw:
        configured.append(("Web_Viewer", "db_path", viewer_raw))

    source_targets: dict[Path, Path] = {}
    claimed_targets: dict[Path, Path] = {}
    for section, key, raw in configured:
        if not raw or Path(raw).is_absolute() or raw == ":memory:":
            continue
        source = (legacy_base / raw).resolve()
        if source in source_targets:
            target = source_targets[source]
        else:
            filename = Path(raw).name or "meshcore_bot.db"
            target = (state_dir / filename).resolve()
            if target in claimed_targets and claimed_targets[target] != source:
                prefix = "viewer-" if section == "Web_Viewer" else "bot-"
                target = (state_dir / f"{prefix}{filename}").resolve()
            suffix = 1
            original_target = target
            while target in claimed_targets and claimed_targets[target] != source:
                target = original_target.with_name(
                    f"{original_target.stem}-{suffix}{original_target.suffix}"
                )
                suffix += 1
            source_targets[source] = target
            claimed_targets[target] = source
            _migrate_sqlite(source, target)
        updates.setdefault(section, {})[key] = str(target)


def migrate_service_layout(
    config_path: Path,
    legacy_base: Path,
    state_dir: Path,
    log_dir: Path,
) -> dict[str, dict[str, str]]:
    """Migrate relative DB/local/log settings and return applied INI updates."""
    parser = configparser.ConfigParser()
    loaded = parser.read(config_path, encoding="utf-8")
    if not loaded:
        raise FileNotFoundError(config_path)

    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    updates: dict[str, dict[str, str]] = {}

    _migrate_runtime_database_paths(parser, legacy_base, state_dir, updates)

    local_raw = parser.get("Bot", "local_dir_path", fallback="local").strip() or "local"
    if not Path(local_raw).is_absolute():
        source = (legacy_base / local_raw).resolve()
        target = (state_dir / "local").resolve()
        if source.is_dir() and source != target:
            if target.exists() and any(target.iterdir()):
                os.replace(target, _backup_path(target, "pre-layout"))
            elif target.exists():
                target.rmdir()
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
        updates.setdefault("Bot", {})["local_dir_path"] = str(target)

    log_raw = parser.get("Logging", "log_file", fallback="").strip()
    if log_raw and not Path(log_raw).is_absolute():
        source = (legacy_base / log_raw).resolve()
        target = (log_dir / Path(log_raw).name).resolve()
        if source.is_file() and source != target:
            if target.exists():
                shutil.copy2(target, _backup_path(target, "pre-layout"))
            shutil.copy2(source, target)
            os.chmod(target, 0o600)
        updates.setdefault("Logging", {})["log_file"] = str(target)

    if updates:
        update_ini_values(str(config_path), updates)
    return updates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--legacy-base", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--log-dir", required=True, type=Path)
    args = parser.parse_args()
    updates = migrate_service_layout(
        args.config.resolve(),
        args.legacy_base.resolve(),
        args.state_dir.resolve(),
        args.log_dir.resolve(),
    )
    changed = sum(len(values) for values in updates.values())
    print(f"service layout migration complete ({changed} path setting(s) updated)")


if __name__ == "__main__":
    main()
