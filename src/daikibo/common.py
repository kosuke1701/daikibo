"""Small shared types; no workflow or authority decisions live here."""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_JSON_BYTES = 8 * 1024 * 1024
DEFAULT_STRING_ITEM_MAXIMUM = 4096

class Fault(Exception):
    """An expected rejection, returned to clients without a traceback."""
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details
    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}

def need(condition: Any, code: str, message: str, details: Any = None) -> None:
    if not condition:
        raise Fault(code, message, details)

def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")

def _unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, "invalid_json", f"Duplicate key: {key}")
        result[key] = value
    return result

def parse_json(value: str | bytes, limit: int = MAX_JSON_BYTES) -> Any:
    need(len(value.encode("utf-8") if isinstance(value,str) else value) <= limit, "too_large", "JSON exceeds UTF-8 byte input limit")
    try:
        return json.loads(value, object_pairs_hook=_unique, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Fault("invalid_json", "Invalid or excessively nested JSON") from exc

def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()

def uid(prefix: str) -> str:
    return prefix + "-" + secrets.token_hex(12)

def timestamp() -> float:
    return time.time()

def text(value: Any, name: str = "text", maximum: int = 1_000_000, empty: bool = False) -> str:
    need(isinstance(value, str) and (empty or bool(value.strip())) and len(value) <= maximum,
         "invalid_input", f"{name} must be a {'possibly empty ' if empty else 'nonempty '}string of at most {maximum} characters")
    need("\x00" not in value, "invalid_input", f"{name} contains NUL")
    return value

def number(value: Any, name: str, minimum: float, maximum: float, integer: bool = False):
    need(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum and (not integer or type(value) is int),
         "invalid_input", f"{name} must be in [{minimum}, {maximum}]")
    return value

def finite_duration(value: Any, name: str = "duration") -> float:
    """Validate a positive duration without imposing a calendar-day ceiling.

    The collector still uses short polling intervals; this validator only checks
    that the requested deadline can be represented by Python's monotonic clock.
    ``bool`` is rejected explicitly because it is an ``int`` subclass.
    """
    need(type(value) in (int, float), "invalid_timeout", f"{name} must be a finite positive number")
    try:
        seconds = float(value)
    except (OverflowError, ValueError) as exc:
        raise Fault("invalid_timeout", f"{name} is not representable as a duration") from exc
    need(math.isfinite(seconds) and seconds > 0, "invalid_timeout", f"{name} must be a finite positive number")
    # A huge but finite value can overflow when added to the monotonic clock.
    # Reject that representation failure explicitly, without selecting an
    # arbitrary 24h/7d policy ceiling.
    now = time.monotonic()
    need(math.isfinite(now + seconds) and now + seconds > now,
         "invalid_timeout", f"{name} cannot be represented by the monotonic clock")
    return seconds

def obj(value: Any, required=(), optional=(), name="object") -> dict:
    need(isinstance(value, dict), "invalid_input", f"{name} must be an object")
    need(set(required) <= set(value), "invalid_input", f"{name} is missing fields", sorted(set(required) - set(value)))
    need(set(value) <= set(required) | set(optional), "invalid_input", f"{name} has unknown fields", sorted(set(value) - set(required) - set(optional)))
    return value

def strings(value: Any, name="list", maximum=10000, nonempty=False, item_maximum=DEFAULT_STRING_ITEM_MAXIMUM) -> list[str]:
    need(isinstance(value, list) and len(value) <= maximum and (not nonempty or bool(value)), "invalid_input", f"Invalid {name}")
    for x in value:
        text(x, name, item_maximum)
    need(len(set(value)) == len(value), "invalid_input", f"Duplicate items in {name}")
    return value

def relative_path(value: str) -> str:
    text(value, "path", 4096)
    p = PurePosixPath(value)
    need(not p.is_absolute() and ".." not in p.parts and "\\" not in value and bool(p.parts), "unsafe_path", "Path must be a relative POSIX path")
    need(".git" not in p.parts and not any(x.startswith(".daikibo-control") for x in p.parts), "unsafe_path", "Protected path")
    return str(p)

def inside(root: Path, path: str, allow_missing=True) -> Path:
    p = root / relative_path(path)
    need(p.resolve().is_relative_to(root.resolve()), "unsafe_path", "Path escapes workspace")
    if not allow_missing:
        need(p.is_file() and not p.is_symlink(), "unsafe_path", "Expected regular file")
    return p

def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

@dataclass(frozen=True)
class Actor:
    id: str
    role: str
    project: str | None = None
    task: str | None = None
    token_id: str | None = None

    def require(self, *roles: str, project: str | None = None, task: str | None = None) -> None:
        need(self.role in roles, "forbidden", "This authenticated role is not authorized")
        need(self.project is None or self.project == project, "forbidden", "Capability belongs to another project")
        need(self.task is None or self.task == task, "forbidden", "Capability belongs to another task")
