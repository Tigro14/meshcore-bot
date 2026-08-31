#!/usr/bin/env python3
"""Preserve installed-only alternative command files across service syncs."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def backup_installed_only(
    source_dir: Path,
    installed_dir: Path,
    backup_dir: Path,
) -> list[Path]:
    """Back up installed-only files and symlinks without following symlinks."""

    preserved: list[Path] = []
    backup_dir.mkdir(parents=True, exist_ok=True)
    if not installed_dir.is_dir():
        return preserved

    for installed_path in sorted(installed_dir.rglob("*")):
        relative_path = installed_path.relative_to(installed_dir)
        if (
            "__pycache__" in relative_path.parts
            or installed_path.suffix in {".pyc", ".pyo"}
        ):
            continue
        source_path = source_dir / relative_path
        if source_path.exists() or source_path.is_symlink():
            continue
        backup_path = backup_dir / relative_path
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if installed_path.is_symlink():
            backup_path.symlink_to(os.readlink(installed_path))
        elif installed_path.is_file():
            shutil.copy2(installed_path, backup_path)
        else:
            continue
        preserved.append(relative_path)
    return preserved


def restore_backup(backup_dir: Path, installed_dir: Path) -> list[Path]:
    """Restore previously preserved files, then remove the temporary backup."""

    restored: list[Path] = []
    if not backup_dir.is_dir():
        return restored
    for backup_path in sorted(backup_dir.rglob("*")):
        relative_path = backup_path.relative_to(backup_dir)
        installed_path = installed_dir / relative_path
        installed_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.is_symlink():
            installed_path.unlink(missing_ok=True)
            installed_path.symlink_to(os.readlink(backup_path))
        elif backup_path.is_file():
            shutil.copy2(backup_path, installed_path)
        else:
            continue
        restored.append(relative_path)
    shutil.rmtree(backup_dir)
    return restored


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source", required=True, type=Path)
    backup_parser.add_argument("--installed", required=True, type=Path)
    backup_parser.add_argument("--backup", required=True, type=Path)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--installed", required=True, type=Path)
    restore_parser.add_argument("--backup", required=True, type=Path)

    args = parser.parse_args()
    if args.command == "backup":
        changed = backup_installed_only(args.source, args.installed, args.backup)
        print(f"preserved {len(changed)} installed-only alternative file(s)")
    else:
        changed = restore_backup(args.backup, args.installed)
        print(f"restored {len(changed)} installed-only alternative file(s)")


if __name__ == "__main__":
    main()
