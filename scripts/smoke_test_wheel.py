#!/usr/bin/env python3
"""Smoke-test an installed MeshCore Bot wheel from outside its source tree."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
from importlib import resources


def _module_names(distribution: importlib.metadata.Distribution) -> list[str]:
    names: set[str] = set()
    for file in distribution.files or ():
        parts = file.parts
        if not parts or parts[0] != "modules" or file.suffix != ".py":
            continue
        if parts[-1] == "__init__.py":
            names.add(".".join(parts[:-1]))
        else:
            names.add(".".join((*parts[:-1], file.stem)))
    return sorted(names)


def main() -> None:
    distribution = importlib.metadata.distribution("meshcore-bot")
    files = {str(file) for file in distribution.files or ()}

    required_files = {
        "translations/en.json",
        "data/randomlines/funfacts.txt",
        "modules/clients/espn_client.py",
    }
    missing = sorted(required_files - files)
    if missing:
        raise SystemExit(f"wheel is missing required files: {', '.join(missing)}")

    translations = resources.files("translations")
    english = json.loads(translations.joinpath("en.json").read_text(encoding="utf-8"))
    if not english:
        raise SystemExit("bundled English translation catalog is empty")

    randomlines = resources.files("data.randomlines")
    if not randomlines.joinpath("funfacts.txt").read_text(encoding="utf-8").strip():
        raise SystemExit("bundled RandomLine data is empty")

    failures: list[str] = []
    for module_name in _module_names(distribution):
        try:
            importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - aggregate all artifact failures
            failures.append(f"{module_name}: {type(exc).__name__}: {exc}")
    if failures:
        raise SystemExit("wheel module import failures:\n  " + "\n  ".join(failures))

    for script in ("meshcore-bot", "meshcore-viewer"):
        matches = [ep for ep in distribution.entry_points if ep.group == "console_scripts" and ep.name == script]
        if len(matches) != 1:
            raise SystemExit(f"expected one {script!r} console entry point, found {len(matches)}")
        matches[0].load()

    print(f"wheel smoke test passed ({len(_module_names(distribution))} modules imported)")


if __name__ == "__main__":
    main()
