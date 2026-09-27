"""Check that subprocesses inherit the package selected by the parent test lane."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

import daikibo
import pytest

from _child_import import child_import_guard, imported_package_file


@pytest.mark.parametrize(
    "module_name",
    [
        "test_traceability_unit_a",
        "test_traceability_unit_a_repair",
        "test_backup_recipe_acceptance",
    ],
)
def test_actual_child_helpers_check_parent_imported_package(module_name, monkeypatch):
    module = importlib.import_module(module_name)
    expected_file = imported_package_file()
    monkeypatch.setenv("PYTHONPATH", "/not-the-package")
    monkeypatch.setenv("VIRTUAL_ENV", "/does-not-exist-child-import-contract")
    environment = module._child_env()
    assert environment["DAIKIBO_EXPECTED_PACKAGE_FILE"] == str(expected_file)
    assert environment["PYTHONPATH"].split(os.pathsep)[:2] == [
        str(expected_file.parent.parent),
        "/not-the-package",
    ]
    script = child_import_guard() + r'''
from pathlib import Path
print(Path(daikibo.__file__).resolve())
'''
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert Path(completed.stdout.strip()).resolve() == expected_file


def test_actual_child_helpers_reject_wrong_expected_package():
    script = child_import_guard()
    for module_name in (
        "test_traceability_unit_a",
        "test_traceability_unit_a_repair",
        "test_backup_recipe_acceptance",
    ):
        module = importlib.import_module(module_name)
        environment = module._child_env(
            DAIKIBO_EXPECTED_PACKAGE_FILE="/not-the-package/daikibo/__init__.py"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 79, (module_name, completed.stderr)
