"""Shared child-process import contract used by the real test helpers."""
from __future__ import annotations

import os
from pathlib import Path

import daikibo


def imported_package_file() -> Path:
    """Return the package file selected by this parent test process."""
    return Path(daikibo.__file__).resolve()


def imported_package_root() -> Path:
    return imported_package_file().parent.parent


def child_env(**updates: str) -> dict[str, str]:
    """Build a child environment from the parent's resolved package lane."""
    inherited = os.environ.get("PYTHONPATH")
    paths = [str(imported_package_root())]
    if inherited:
        paths.append(inherited)
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(paths),
        "DAIKIBO_EXPECTED_PACKAGE_FILE": str(imported_package_file()),
    }
    environment.update(updates)
    return environment


def child_import_guard() -> str:
    """Source that rejects a child import before its fault injection runs."""
    return r'''
import os as _child_os
from pathlib import Path as _child_Path
import daikibo

_child_expected_raw = _child_os.environ.get("DAIKIBO_EXPECTED_PACKAGE_FILE")
if not _child_expected_raw:
    raise SystemExit(79)
_child_expected_file = _child_Path(_child_expected_raw).resolve()
_child_actual_file = _child_Path(daikibo.__file__).resolve()
if _child_actual_file != _child_expected_file:
    raise SystemExit(79)
'''
