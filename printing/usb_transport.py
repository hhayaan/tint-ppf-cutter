"""Advanced, driver-dependent USB device-path transport; hardware unverified.

The original TDispIOUSB uses generic USB-device interfaces and CreateFile /
overlapped WriteFile, not a universal manufacturer USB library. This module
reproduces that primitive only. Enumeration occurs solely on an explicit call;
send requires a selected path and matching explicit VID/PID. No Graphtec IDs,
pipe number, network scan, automatic probe, fallback, or retry is supplied.

CancelIoEx is asynchronous. An operation whose cancellation cannot be drained
within the cleanup timeout retains its buffer/OVERLAPPED/handles under a token.
reap_pending_usb_io can later release only those already-owned operations.
Device-driver calls themselves can still be driver-dependent; no success here
certifies physical cutting or overall FC9000 compatibility.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .transports import (BOOL, BYTE, CHUNK_BYTES, DWORD, HANDLE, INVALID_HANDLE_VALUE, WORD,
                         DeliveryError, DeliveryResult, _Job, _bind, _check_cancel,
                         _error_code, _remaining, _timeout, _win_error)

USB_DEVICE_GUID = "a5dcbf10-6530-11d2-901f-00c04fb951ed"
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_NO_MORE_ITEMS = 259
ERROR_IO_PENDING = 997
WAIT_TIMEOUT = 258


class GUID(ctypes.Structure):
    _fields_ = [("Data1", DWORD), ("Data2", WORD), ("Data3", WORD), ("Data4", BYTE * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("InterfaceClassGuid", GUID), ("Flags", DWORD), ("Reserved", ctypes.c_size_t)]


class SP_DEVINFO_DATA(ctypes.Structure):
    _fields_ = [("cbSize", DWORD), ("ClassGuid", GUID), ("DevInst", DWORD), ("Reserved", ctypes.c_size_t)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", DWORD), ("OffsetHigh", DWORD), ("hEvent", HANDLE)]


@dataclass(frozen=True)
class USBDevice:
    path: str
    description: str
    hardware_ids: tuple[str, ...]
    vid: int | None
    pid: int | None


def _identity(text: str) -> tuple[int, int] | None:
    match = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", text, re.IGNORECASE)
    return (int(match[1], 16), int(match[2], 16)) if match else None


def validated_device_path(device_path: str, vid: int, pid: int, pipe_number: int | None = None) -> str:
    if any(type(value) is not int or not 0 <= value <= 65535 for value in (vid, pid)):
        raise ValueError("Explicit VID and PID must be 16-bit integers")
    if not isinstance(device_path, str) or "\x00" in device_path:
        raise ValueError("Select an explicit generic USB interface path")
    path = device_path.strip()
    if not path.casefold().startswith("\\\\?\\usb#") or not path.casefold().endswith("#{" + USB_DEVICE_GUID + "}"):
        raise ValueError("Expected the selected generic USB device-interface path, not a file, queue, disk, or guessed endpoint")
    if any(character in path[4:] for character in "\\/\r\n\t") or not re.match(r"usb#vid_[0-9a-f]{4}&pid_[0-9a-f]{4}[&#]", path[4:], re.IGNORECASE):
        raise ValueError("USB interface path must have its canonical selected VID/PID identity")
    if _identity(path) != (vid, pid):
        raise ValueError("Selected USB path does not match the explicitly selected VID/PID")
    if pipe_number is not None:
        if type(pipe_number) is not int or not 0 <= pipe_number <= 65535:
            raise ValueError("An explicit pipe number must be between 0 and 65535")
        path += f"\\Pipe{pipe_number:02d}"
    return path


def _setupapi():
    if os.name != "nt":
        raise OSError("USB interface enumeration requires Windows")
    api = ctypes.WinDLL("setupapi", use_last_error=True)
    _bind(api, "SetupDiGetClassDevsW", HANDLE, [ctypes.POINTER(GUID), ctypes.c_wchar_p, HANDLE, DWORD])
    _bind(api, "SetupDiEnumDeviceInterfaces", BOOL, [HANDLE, ctypes.c_void_p, ctypes.POINTER(GUID), DWORD, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA)])
    _bind(api, "SetupDiGetDeviceInterfaceDetailW", BOOL, [HANDLE, ctypes.POINTER(SP_DEVICE_INTERFACE_DATA), ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(SP_DEVINFO_DATA)])
    _bind(api, "SetupDiGetDeviceRegistryPropertyW", BOOL, [HANDLE, ctypes.POINTER(SP_DEVINFO_DATA), DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)])
    _bind(api, "SetupDiDestroyDeviceInfoList", BOOL, [HANDLE])
    return api


def _property(api, handle, info, property_id: int) -> tuple[str, ...]:
    needed, kind = DWORD(), DWORD()
    ok = api.SetupDiGetDeviceRegistryPropertyW(handle, ctypes.byref(info), property_id,
                                               ctypes.byref(kind), None, 0, ctypes.byref(needed))
    if not ok and _win_error(api, "SetupDiGetDeviceRegistryPropertyW").winerror != ERROR_INSUFFICIENT_BUFFER:
        return ()  # Friendly name and description are optional properties.
    if not 0 < needed.value <= 65536:
        return ()
    buffer = ctypes.create_string_buffer(needed.value)
    if not api.SetupDiGetDeviceRegistryPropertyW(handle, ctypes.byref(info), property_id,
                                                 ctypes.byref(kind), buffer, needed.value, ctypes.byref(needed)):
        return ()
    if kind.value not in (1, 2, 7):
        return ()
    return tuple(value for value in buffer.raw[:needed.value].decode("utf-16-le", errors="replace").split("\x00") if value)


def list_usb_devices(*, api=None, max_devices: int = 4096) -> list[USBDevice]:
    """Explicit metadata listing only. This does not open a device for I/O."""
    if type(max_devices) is not int or not 1 <= max_devices <= 65536:
        raise ValueError("max_devices must be between 1 and 65536")
    api = api if api is not None else _setupapi()
    guid = GUID.from_buffer_copy(uuid.UUID(USB_DEVICE_GUID).bytes_le)
    handle = api.SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)
    value = handle.value if isinstance(handle, HANDLE) else handle
    if value in (None, 0, -1, INVALID_HANDLE_VALUE):
        raise _win_error(api, "SetupDiGetClassDevsW")
    result = []
    try:
        for index in range(max_devices + 1):
            interface = SP_DEVICE_INTERFACE_DATA()
            interface.cbSize = ctypes.sizeof(interface)
            if not api.SetupDiEnumDeviceInterfaces(handle, None, ctypes.byref(guid), index, ctypes.byref(interface)):
                error = _win_error(api, "SetupDiEnumDeviceInterfaces")
                if error.winerror == ERROR_NO_MORE_ITEMS:
                    break
                raise error
            if index == max_devices:
                raise OSError("USB inventory exceeds the requested metadata bound")
            needed = DWORD()
            api.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(interface), None, 0, ctypes.byref(needed), None)
            if not 8 <= needed.value <= 1024 * 1024:
                raise OSError("USB interface detail length is invalid")
            detail = ctypes.create_string_buffer(needed.value)
            ctypes.cast(detail, ctypes.POINTER(DWORD)).contents.value = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            info = SP_DEVINFO_DATA()
            info.cbSize = ctypes.sizeof(info)
            if not api.SetupDiGetDeviceInterfaceDetailW(handle, ctypes.byref(interface), detail, needed.value, ctypes.byref(needed), ctypes.byref(info)):
                raise _win_error(api, "SetupDiGetDeviceInterfaceDetailW")
            path = ctypes.wstring_at(ctypes.addressof(detail) + ctypes.sizeof(DWORD),
                                    (len(detail) - ctypes.sizeof(DWORD)) // ctypes.sizeof(ctypes.c_wchar)).split("\x00", 1)[0]
            hardware_ids = _property(api, handle, info, 1)
            description = _property(api, handle, info, 12) or _property(api, handle, info, 0)
            identity = _identity(path) or next((identity for value in hardware_ids if (identity := _identity(value))), None)
            result.append(USBDevice(path, description[0] if description else "USB device", hardware_ids,
                                    identity[0] if identity else None, identity[1] if identity else None))
    finally:
        if not api.SetupDiDestroyDeviceInfoList(handle):
            raise _win_error(api, "SetupDiDestroyDeviceInfoList")
    return result


def _kernel32():
    if os.name != "nt":
        raise OSError("Direct USB output requires Windows")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    _bind(api, "CreateFileW", HANDLE, [ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE])
    _bind(api, "CreateEventW", HANDLE, [ctypes.c_void_p, BOOL, BOOL, ctypes.c_wchar_p])
    _bind(api, "ResetEvent", BOOL, [HANDLE])
    _bind(api, "WriteFile", BOOL, [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.POINTER(OVERLAPPED)])
    _bind(api, "GetOverlappedResult", BOOL, [HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(DWORD), BOOL])
    _bind(api, "WaitForSingleObject", DWORD, [HANDLE, DWORD])
    _bind(api, "CancelIoEx", BOOL, [HANDLE, ctypes.POINTER(OVERLAPPED)])
    _bind(api, "CloseHandle", BOOL, [HANDLE])
    return api


@dataclass
class _OwnedIO:
    api: object
    handle: object
    event: object
    buffer: object = None
    overlapped: OVERLAPPED | None = None
    completed: bool = True
    completed_bytes: int = 0
    destination: str = ""


_pending: dict[str, _OwnedIO] = {}
_pending_lock = threading.Lock()


def _require_no_owned_pending(path: str) -> None:
    base_path = path.casefold().split("\\pipe", 1)[0]
    with _pending_lock:
        if any(owned.destination.casefold().split("\\pipe", 1)[0] == base_path for owned in _pending.values()):
            raise ValueError("Previous USB cleanup is still owned for this device; recheck pending cleanup before another output attempt")


def _close_owned(owned: _OwnedIO) -> list[str]:
    errors = []
    for attribute in ("handle", "event"):
        handle = getattr(owned, attribute)
        if handle is not None:
            try:
                if owned.api.CloseHandle(handle):
                    setattr(owned, attribute, None)
                else:
                    errors.append(str(_win_error(owned.api, "CloseHandle(USB)")))
            except Exception as error:
                errors.append(f"CloseHandle(USB): {error}")
    return errors


def _retain(owned: _OwnedIO) -> str:
    token = uuid.uuid4().hex
    with _pending_lock:
        _pending[token] = owned
    return token


def reap_pending_usb_io() -> dict[str, str]:
    """Non-waiting cleanup of previously owned I/O only; never opens a device."""
    results = {}
    with _pending_lock:
        for token, owned in list(_pending.items()):
            if not owned.completed and owned.api.WaitForSingleObject(owned.event, 0) == 0:
                written = DWORD()
                ok = owned.api.GetOverlappedResult(owned.handle, ctypes.byref(owned.overlapped), ctypes.byref(written), False)
                owned.completed = bool(ok) or _win_error(owned.api, "GetOverlappedResult(USB cleanup)").winerror != 996
            if owned.completed:
                errors = _close_owned(owned)
                if not errors:
                    del _pending[token]
                    results[token] = "released"
                else:
                    results[token] = "; ".join(errors)
            else:
                results[token] = "still pending; buffers and handles retained"
    return results


def _cancel_and_drain(owned: _OwnedIO, timeout: float) -> bool:
    owned.api.CancelIoEx(owned.handle, ctypes.byref(owned.overlapped))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wait = owned.api.WaitForSingleObject(owned.event, min(100, max(1, math.ceil((deadline - time.monotonic()) * 1000))))
        if wait == 0:
            written = DWORD()
            ok = owned.api.GetOverlappedResult(owned.handle, ctypes.byref(owned.overlapped), ctypes.byref(written), False)
            if ok or _win_error(owned.api, "GetOverlappedResult(USB cleanup)").winerror != 996:
                owned.completed = True
                owned.completed_bytes = written.value if ok else 0
                return True
            time.sleep(min(.005, max(0, deadline - time.monotonic())))
            continue
        if wait != WAIT_TIMEOUT:
            break
    return False


def _finish(job: _Job, error, attempted: bool) -> DeliveryResult:
    state = "sent_unconfirmed"
    message = "USB bytes accepted through the selected driver path; physical output and FC9000 compatibility are unverified."
    if error is not None:
        state = "cancelled" if isinstance(error, (InterruptedError, KeyboardInterrupt)) else "partial" if attempted else "failed"
        message = (f"{type(error).__name__}: {error}. {job.sent}/{len(job.payload)} bytes reported completed; "
                   "an interrupted in-flight write may have delivered additional bytes. No retry or fallback occurred.")
    if job.cleanup_errors:
        message += " Cleanup warnings: " + "; ".join(job.cleanup_errors)
    result = DeliveryResult("windows_usb", job.destination, state, len(job.payload), job.sent,
                            job.digest, job.token, message, error_code=_error_code(error) if error else None,
                            cleanup_errors=tuple(job.cleanup_errors), log_path=str(job.log_path) if job.log_path else None)
    try:
        job.log({"event": "finished", **result.to_dict()})
    except OSError as log_error:
        result = replace(result, log_error=str(log_error))
    if error is not None:
        raise DeliveryError(result) from error
    return result


def send_usb(payload: bytes, *, device_path: str, vid: int, pid: int, pipe_number: int | None = None,
             timeout: float = 10.0, cleanup_timeout: float = 2.0, log_path: Path | str | None = None,
             cancel_event=None, api=None) -> DeliveryResult:
    """One explicit raw USB-path write attempt, with bounded overlapped waits."""
    path = validated_device_path(device_path, vid, pid, pipe_number)
    _require_no_owned_pending(path)
    timeout, cleanup_timeout = _timeout(timeout), _timeout(cleanup_timeout, "cleanup_timeout")
    job = _Job("windows_usb", path, payload, log_path)
    owned = None
    error = None
    attempted = False
    deadline = time.monotonic() + timeout
    try:
        job.begin()
        _check_cancel(cancel_event)
        api = api if api is not None else _kernel32()
        handle = api.CreateFileW(path, 0x40000000, 7, None, 3, 0x40000000, None)
        value = handle.value if isinstance(handle, HANDLE) else handle
        if value in (None, 0, -1, INVALID_HANDLE_VALUE):
            raise _win_error(api, "CreateFileW(USB)")
        owned = _OwnedIO(api, handle, None, destination=path)
        event = api.CreateEventW(None, True, False, None)
        if not event:
            raise _win_error(api, "CreateEventW(USB)")
        owned.event = event
        while job.sent < len(job.payload):
            _check_cancel(cancel_event)
            _remaining(deadline)
            chunk = job.payload[job.sent:job.sent + CHUNK_BYTES]
            owned.buffer = ctypes.create_string_buffer(chunk, len(chunk))
            owned.overlapped = OVERLAPPED()
            owned.overlapped.hEvent = event
            if not api.ResetEvent(event):
                raise _win_error(api, "ResetEvent(USB)")
            written = DWORD()
            owned.completed = False
            attempted = True
            ok = api.WriteFile(handle, owned.buffer, len(chunk), ctypes.byref(written), ctypes.byref(owned.overlapped))
            if not ok:
                write_error = _win_error(api, "WriteFile(USB)")
                if write_error.winerror != ERROR_IO_PENDING:
                    owned.completed = True
                    raise write_error
                while True:
                    _check_cancel(cancel_event)
                    remaining = _remaining(deadline)
                    wait = api.WaitForSingleObject(event, min(100, max(1, math.ceil(remaining * 1000))))
                    if wait == 0:
                        break
                    if wait != WAIT_TIMEOUT:
                        raise _win_error(api, "WaitForSingleObject(USB)")
            if not api.GetOverlappedResult(handle, ctypes.byref(owned.overlapped), ctypes.byref(written), False):
                completion_error = _win_error(api, "GetOverlappedResult(USB)")
                owned.completed = completion_error.winerror != 996
                raise completion_error
            owned.completed = True
            if not 0 < written.value <= len(chunk):
                raise OSError("USB write made no valid progress")
            job.sent += written.value
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
    finally:
        if owned is not None:
            if not owned.completed:
                try:
                    _cancel_and_drain(owned, cleanup_timeout)
                    if owned.completed_bytes:
                        if owned.completed_bytes <= len(owned.buffer):
                            job.sent += owned.completed_bytes
                        else:
                            job.cleanup_errors.append("USB cancellation returned an invalid completed-byte count")
                except Exception as cleanup:
                    job.cleanup_errors.append(str(cleanup))
            if owned.completed:
                job.cleanup_errors.extend(_close_owned(owned))
            if not owned.completed or owned.handle is not None or owned.event is not None:
                token = _retain(owned)
                job.cleanup_errors.append("USB cleanup remains owned under token " + token + "; use Recheck pending USB cleanup or reap_pending_usb_io after completion")
    return _finish(job, error, attempted)
