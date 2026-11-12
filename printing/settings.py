"""Whitelisted, machine-local cutter preferences; no device or vendor calls.

Use ``default_settings_path(workspace)`` for the ignored ``.runtime`` location.
Setup and tools are stored separately for each exact driver profile. Source
selection, geometry calibration/resizing and individual jobs are never stored.
Loading preferences does not validate that a device is connected or that media
is still loaded. Callers must review those facts before sending a new job.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import tempfile
import threading

SCHEMA = "tint-ppf-cut-settings.v1"
MAX_BYTES = 1_048_576
MAX_PROFILES = 1024
_PROFILE_KEY = re.compile(r"[a-z][a-z0-9_-]*:[0-9]+", re.ASCII)
_LOCK = threading.RLock()


class SettingsError(ValueError):
    """Preferences could not be loaded or saved; existing data is preserved."""


def default_settings_path(workspace: Path) -> Path:
    return Path(workspace) / ".runtime" / "cut-settings.json"


def empty_settings() -> dict:
    return {"schema": SCHEMA, "last_profile": None, "profiles": {}}


def _object(value, allowed, label):
    if not isinstance(value, dict):
        raise SettingsError(f"{label} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise SettingsError(f"{label} contains a non-text key")
    unknown = set(value) - set(allowed)
    if unknown:
        raise SettingsError(f"{label} contains unsupported fields: {', '.join(sorted(unknown))}")


def _profile_key(value):
    if not isinstance(value, str) or len(value) > 100 or not _PROFILE_KEY.fullmatch(value):
        raise SettingsError("Use an exact lowercase driver:index profile key")
    return value


def _text(value, label, *, empty=False, maximum=32768):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (not empty and not value.strip()):
        raise SettingsError(f"{label} must be {'optional ' if empty else 'nonempty '}text without NUL characters")
    return value


def _integer(value, label, minimum=1, maximum=2_147_483_647, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SettingsError(f"{label} must be a whole number from {minimum} to {maximum}")
    return value


def _number(value, label, *, optional=False):
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise SettingsError(f"{label} must be a positive finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite or value <= 0:
        raise SettingsError(f"{label} must be a positive finite number")
    return value


def _choice(value, choices, label):
    if value not in choices or not isinstance(value, str):
        raise SettingsError(f"{label} must be one of {', '.join(choices)}")
    return value


def _boolean(value, label):
    if not isinstance(value, bool):
        raise SettingsError(f"{label} must be true or false")
    return value


def _connection(value):
    if not isinstance(value, dict):
        raise SettingsError("connection must be an object")
    kind = value.get("type")
    kinds = {
        "file": {"file_directory"},
        "windows_raw": {"queue_name"},
        "tcp": {"host", "port", "timeout"},
        "windows_serial": {"port", "baud", "parity", "stop_bits", "flow_control", "data_bits", "timeout"},
        "windows_usb": {"device_path", "vid", "pid", "pipe_number", "timeout"},
    }
    _choice(kind, tuple(kinds), "connection.type")
    _object(value, {"type", *kinds[kind]}, "connection")
    result = {"type": kind}
    for key, item in value.items():
        label = "connection." + key
        if key == "type":
            continue
        if key in ("file_directory", "queue_name", "host", "device_path"):
            result[key] = _text(item, label)
        elif key == "port" and kind == "windows_serial":
            if not isinstance(item, str) or not re.fullmatch(r"COM[1-9][0-9]*", item, re.ASCII):
                raise SettingsError("connection.port must be an explicit uppercase COM port")
            result[key] = item
        elif key == "port":
            result[key] = _integer(item, label, maximum=65535)
        elif key == "timeout":
            result[key] = _number(item, label)
        elif key == "baud":
            result[key] = _integer(item, label)
        elif key == "data_bits":
            result[key] = _integer(item, label, minimum=5, maximum=8)
        elif key in ("vid", "pid"):
            result[key] = _integer(item, label, minimum=0, maximum=65535)
        elif key == "pipe_number":
            result[key] = _integer(item, label, minimum=0, maximum=99, optional=True)
        elif key == "parity":
            result[key] = _choice(item, ("N", "E", "O", "M", "S"), label)
        elif key == "flow_control":
            result[key] = _choice(item, ("none", "rtscts", "xonxoff", "dsrdtr"), label)
        elif key == "stop_bits":
            if isinstance(item, bool) or not isinstance(item, Real) or item not in (1, 1.5, 2):
                raise SettingsError("connection.stop_bits must be 1, 1.5 or 2")
            result[key] = item
    if kind == "windows_serial" and result.get("stop_bits") == 1.5 and result.get("data_bits") not in (None, 5):
        raise SettingsError("1.5 serial stop bits require 5 data bits")
    if kind == "windows_serial" and result.get("stop_bits") == 2 and result.get("data_bits") == 5:
        raise SettingsError("5 serial data bits require 1 or 1.5 stop bits")
    return result


def _media(value):
    _object(value, ("mode", "width_mm", "height_mm"), "media")
    result = {}
    for key, item in value.items():
        result[key] = (_choice(item, ("sheet", "roll"), "media.mode") if key == "mode" else
                       _number(item, "media." + key, optional=True))
    return result


def _setup(value):
    _object(value, ("runtime_directory", "connection", "media"), "setup")
    result = {}
    for key, item in value.items():
        result[key] = (_text(item, "runtime_directory") if key == "runtime_directory" else
                       _connection(item) if key == "connection" else _media(item))
    return result


def _tools(value):
    _object(value, ("speed", "force", "acceleration", "tool", "rotate_axes", "return_origin", "feed_offset_mm"), "tools")
    result = {}
    for key, item in value.items():
        result[key] = (_boolean(item, "tools." + key) if key in ("rotate_axes", "return_origin") else
                       _number(item, "tools.acceleration", optional=True) if key == "acceleration" else
                       _number(item, "tools.feed_offset_mm", optional=True) if key == "feed_offset_mm" else
                       _integer(item, "tools." + key, optional=key != "tool"))
    return result


def validate_settings(settings: dict) -> dict:
    """Return a validated fresh document; reject unknown or job-specific keys."""
    _object(settings, ("schema", "last_profile", "profiles"), "settings")
    if settings.get("schema") != SCHEMA:
        raise SettingsError("Unsupported cut-settings schema")
    profiles = settings.get("profiles")
    if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
        raise SettingsError(f"profiles must be an object with at most {MAX_PROFILES} entries")
    cleaned = {}
    for key, entry in profiles.items():
        _profile_key(key)
        _object(entry, ("setup", "tools"), f"profile {key}")
        cleaned[key] = {"setup": _setup(entry.get("setup", {})), "tools": _tools(entry.get("tools", {}))}
    last = settings.get("last_profile")
    if last is not None and (_profile_key(last) not in cleaned):
        raise SettingsError("last_profile does not name a saved profile")
    return {"schema": SCHEMA, "last_profile": last, "profiles": cleaned}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SettingsError(f"Duplicate settings key: {key}")
        result[key] = value
    return result


def load_settings(path: Path) -> dict:
    """Missing file is normal. Invalid/unreadable content is a visible error."""
    path = Path(path)
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return empty_settings()
    except OSError as error:
        raise SettingsError(f"Cannot read cut settings {path}: {error}") from error
    if len(raw) > MAX_BYTES:
        raise SettingsError(f"Cut settings exceed {MAX_BYTES} bytes: {path}")
    try:
        return validate_settings(json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object))
    except (ValueError, UnicodeError, RecursionError) as error:
        raise SettingsError(f"Invalid cut settings {path}: {error}") from error


def profile_settings(settings: dict, profile_key: str) -> dict:
    """Return isolated setup/tools dictionaries, or empty ones for a new model."""
    key = _profile_key(profile_key)
    return deepcopy(validate_settings(settings)["profiles"].get(key, {"setup": {}, "tools": {}}))


def _write_atomic(path: Path, settings: dict):
    try:
        data = (json.dumps(settings, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise SettingsError(f"Cannot encode cut settings: {error}") from error
    if len(data) > MAX_BYTES:
        raise SettingsError(f"Cut settings exceed {MAX_BYTES} bytes")
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as error:
        raise SettingsError(f"Cannot save cut settings {path}: {error}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def save_profile_settings(path: Path, profile_key: str, *, setup: dict | None = None,
                          tools: dict | None = None, set_last: bool = True) -> dict:
    """Merge only supplied whitelisted preferences and atomically replace JSON.

    ``setup`` merges at its top level; supplied connection/media objects replace
    their previous object so switching transports cannot retain stale details.
    ``tools`` merges individual fields, where None clears an optional override.
    ``set_last=False`` saves a preset without changing the selected model.
    Malformed existing settings are never replaced with defaults.
    """
    key = _profile_key(profile_key)
    _boolean(set_last, "set_last")
    setup = None if setup is None else _setup(setup)
    tools = None if tools is None else _tools(tools)
    path = Path(path)
    with _LOCK:
        settings = load_settings(path)
        entry = settings["profiles"].setdefault(key, {"setup": {}, "tools": {}})
        if setup is not None:
            entry["setup"].update(setup)
        if tools is not None:
            entry["tools"].update(tools)
        if set_last:
            settings["last_profile"] = key
        settings = validate_settings(settings)
        _write_atomic(path, settings)
        return deepcopy(settings)
