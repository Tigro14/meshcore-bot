"""Run dependency-free browser-side regression tests for the contacts manager."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_contacts_manager_frontend_behaviors() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for contacts frontend behavior tests")

    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, "--test", "tests/js/contacts_manager.test.mjs"],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
