"""Explicit byte transports for caller-generated, already validated plotter jobs.

Nothing connects, enumerates queues, or writes a device at import time. There is
no network discovery, implicit destination, assumed Graphtec TCP port, retry,
or automatic failover. These transports do not generate printer commands or
establish cutter compatibility. A successful send means local acceptance, not
physical cutting or an interpreted device acknowledgement.

File output is exclusive. TCP reply capture is optional, bounded, and opaque.
Windows RAW queues require an explicitly selected installed queue; USB works
only if an appropriate installed manufacturer queue/port supports these bytes.
Direct manufacturer USB APIs remain unresolved. Serial settings are explicit.
Winspool calls are synchronous and driver-controlled; callers should use a
worker thread. Cancellation is checked between calls, and cannot undo motion
or guarantee that previously delivered bytes are removed from a device.

Optional JSONL logs record job identity, byte counts and final status, never the
payload itself. DeliveryError.result preserves partial progress; never blindly
retry or switch transport after a partial/uncertain delivery.

Win32 contracts consulted:
https://learn.microsoft.com/en-us/windows/win32/printdocs/writeprinter
https://learn.microsoft.com/en-us/windows/win32/printdocs/enumprinters
https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-dcb
https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-commtimeouts
"""
from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import time
from typing import Callable
import uuid


CHUNK_BYTES = 65536
MAX_REPLY_BYTES = 65536
DWORD = ctypes.c_uint32
WORD = ctypes.c_uint16
BYTE = ctypes.c_ubyte
HANDLE = ctypes.c_void_p
BOOL = ctypes.c_int
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass(frozen=True)
class DeliveryResult:
    transport: str
    destination: str
    state: str
    bytes_requested: int
    bytes_sent: int
    payload_sha256: str
    job_token: str
    message: str
    reply: bytes = b""
    reply_status: str = "not_requested"
    job_id: int | None = None
    error_code: int | None = None
    cleanup_errors: tuple[str, ...] = ()
    log_path: str | None = None
    log_error: str | None = None
    physical_completion_verified: bool = False

    def to_dict(self) -> dict:
        result = asdict(self)
        result["reply_hex"] = result.pop("reply").hex()
        return result


class DeliveryError(RuntimeError):
    def __init__(self, result: DeliveryResult):
        self.result = result
        super().__init__(result.message)


@dataclass(frozen=True)
class PrinterQueue:
    name: str
    server: str
    attributes: int


class DOC_INFO_1W(ctypes.Structure):
    _fields_ = [("pDocName", ctypes.c_wchar_p), ("pOutputFile", ctypes.c_wchar_p),
                ("pDatatype", ctypes.c_wchar_p)]


class PRINTER_INFO_4W(ctypes.Structure):
    _fields_ = [("pPrinterName", ctypes.c_wchar_p), ("pServerName", ctypes.c_wchar_p),
                ("Attributes", DWORD)]


class DCB(ctypes.Structure):
    # The DWORD bitfield is represented as one explicit 32-bit flags word.
    _fields_ = [("DCBlength", DWORD), ("BaudRate", DWORD), ("Flags", DWORD),
                ("wReserved", WORD), ("XonLim", WORD), ("XoffLim", WORD),
                ("ByteSize", BYTE), ("Parity", BYTE), ("StopBits", BYTE),
                ("XonChar", ctypes.c_char), ("XoffChar", ctypes.c_char),
                ("ErrorChar", ctypes.c_char), ("EofChar", ctypes.c_char),
                ("EvtChar", ctypes.c_char), ("wReserved1", WORD)]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [("ReadIntervalTimeout", DWORD), ("ReadTotalTimeoutMultiplier", DWORD),
                ("ReadTotalTimeoutConstant", DWORD), ("WriteTotalTimeoutMultiplier", DWORD),
                ("WriteTotalTimeoutConstant", DWORD)]


class COMSTAT(ctypes.Structure):
    _fields_ = [("Flags", DWORD), ("cbInQue", DWORD), ("cbOutQue", DWORD)]


def _payload(value: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("Payload must be bytes, bytearray, or memoryview")
    result = bytes(value)
    if not result:
        raise ValueError("Refusing an empty output job")
    return result


def _timeout(value: float, name: str = "timeout") -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return result


def _name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"An explicit {label} is required")
    return value.strip()


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Output interrupted; previously accepted bytes cannot be recalled")


def _remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("Output deadline expired; do not automatically retry")
    return value


def _error_code(error: BaseException) -> int | None:
    return getattr(error, "winerror", None) or getattr(error, "errno", None)


def _win_error(api, operation: str) -> OSError:
    getter = getattr(api, "last_error", None)
    code = getter() if callable(getter) else (getattr(ctypes, "get_last_error", lambda: 0)())
    error = OSError(int(code), f"{operation} failed (Windows error {code})")
    error.winerror = int(code)
    return error


class _Job:
    def __init__(self, transport: str, destination: str, payload, log_path):
        self.transport, self.destination = transport, destination
        self.payload = _payload(payload)
        self.sent = 0
        self.token = uuid.uuid4().hex
        self.digest = hashlib.sha256(self.payload).hexdigest()
        self.log_path = Path(log_path) if log_path is not None else None
        self.log_error = None
        self.reply = b""
        self.reply_status = "not_requested"
        self.job_id = None
        self.cleanup_errors: list[str] = []

    def log(self, event: dict) -> None:
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"utc": datetime.now(timezone.utc).isoformat(),
                                         "job_token": self.token, **event}, ensure_ascii=True) + "\n")

    def begin(self) -> None:
        self.log({"event": "started", "transport": self.transport,
                  "destination": self.destination, "bytes_requested": len(self.payload),
                  "payload_sha256": self.digest})

    def finish(self, state: str, message: str, error: BaseException | None = None) -> DeliveryResult:
        if error is not None:
            state = ("cancelled" if isinstance(error, (InterruptedError, KeyboardInterrupt))
                     else "partial" if self.sent else "failed")
            message = f"{type(error).__name__}: {error}; {self.sent}/{len(self.payload)} bytes accepted locally. No retry or failover was attempted."
        if self.cleanup_errors:
            message += " Cleanup warnings: " + "; ".join(self.cleanup_errors)
        result = DeliveryResult(self.transport, self.destination, state, len(self.payload), self.sent,
                                self.digest, self.token, message, self.reply, self.reply_status,
                                self.job_id, _error_code(error) if error else None,
                                tuple(self.cleanup_errors), str(self.log_path) if self.log_path else None)
        try:
            self.log({"event": "finished", **result.to_dict()})
        except OSError as log_error:
            self.log_error = str(log_error)
            from dataclasses import replace
            result = replace(result, log_error=self.log_error)
        if error is not None:
            raise DeliveryError(result) from error
        return result


def write_job_file(path: Path | str, payload: bytes, *, log_path: Path | str | None = None,
                   cancel_event=None) -> DeliveryResult:
    """Create a new file only. Existing files and partial failure files are retained."""
    target = Path(path)
    if log_path is not None and Path(log_path).resolve() == target.resolve():
        raise ValueError("Job output and audit log must be different files")
    job = _Job("file", str(target.resolve()), payload, log_path)
    error = None
    try:
        job.begin()
        _check_cancel(cancel_event)
        with target.open("xb") as stream:
            while job.sent < len(job.payload):
                _check_cancel(cancel_event)
                chunk = job.payload[job.sent:job.sent + CHUNK_BYTES]
                count = stream.write(chunk)
                if not isinstance(count, int) or not 0 < count <= len(chunk):
                    raise OSError("File write made no valid progress")
                job.sent += count
            stream.flush()
            os.fsync(stream.fileno())
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
    return job.finish("saved", "New job file saved; no device was contacted.", error)


def send_tcp(payload: bytes, *, host: str, port: int, timeout: float = 10.0,
             reply_limit: int = 0, reply_timeout: float | None = None,
             log_path: Path | str | None = None, cancel_event=None,
             _connector: Callable | None = None) -> DeliveryResult:
    """Send raw bytes to one explicit endpoint; never infer a device port.

    timeout bounds connection/send and the total operation once DNS resolves;
    OS DNS lookup itself may outlast socket timeouts. Optional replies are opaque
    evidence, not interpreted acknowledgements. A reply timeout does not undo a
    completed send and is exposed through reply_status rather than resending.
    """
    host = _name(host, "TCP host")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("An explicit TCP port between 1 and 65535 is required")
    timeout = _timeout(timeout)
    if isinstance(reply_limit, bool) or not isinstance(reply_limit, int) or not 0 <= reply_limit <= MAX_REPLY_BYTES:
        raise ValueError(f"reply_limit must be between 0 and {MAX_REPLY_BYTES}")
    if reply_timeout is not None:
        reply_timeout = _timeout(reply_timeout, "reply_timeout")
    job = _Job("tcp", f"{host}:{port}", payload, log_path)
    error = None
    connection = None
    deadline = time.monotonic() + timeout
    try:
        job.begin()
        _check_cancel(cancel_event)
        connection = (_connector or socket.create_connection)((host, port), timeout=_remaining(deadline))
        while job.sent < len(job.payload):
            _check_cancel(cancel_event)
            connection.settimeout(_remaining(deadline))
            chunk = job.payload[job.sent:job.sent + CHUNK_BYTES]
            count = connection.send(chunk)
            if not isinstance(count, int) or not 0 < count <= len(chunk):
                raise ConnectionError("TCP connection closed or send made no progress")
            job.sent += count
        if reply_limit:
            reply_deadline = min(deadline, time.monotonic() + (reply_timeout or timeout))
            job.reply_status = "timeout"
            while len(job.reply) < reply_limit:
                _check_cancel(cancel_event)
                try:
                    connection.settimeout(_remaining(reply_deadline))
                    chunk = connection.recv(min(4096, reply_limit - len(job.reply)))
                except TimeoutError:
                    job.reply_status = "partial_timeout" if job.reply else "timeout"
                    break
                if not chunk:
                    job.reply_status = "eof"
                    break
                job.reply += chunk
            else:
                job.reply_status = "limit_reached"
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError as cleanup:
                job.cleanup_errors.append(f"TCP close: {cleanup}")
    return job.finish("sent_unconfirmed", "Bytes accepted by the TCP socket; device execution is unverified.", error)


def _bind(library, name: str, restype, argtypes):
    function = getattr(library, name)
    function.restype, function.argtypes = restype, argtypes
    return function


def _winspool():
    if os.name != "nt":
        raise OSError("Windows RAW print queues require Windows")
    library = ctypes.WinDLL("winspool.drv", use_last_error=True)
    _bind(library, "EnumPrintersW", BOOL, [DWORD, ctypes.c_wchar_p, DWORD, ctypes.c_void_p, DWORD,
                                          ctypes.POINTER(DWORD), ctypes.POINTER(DWORD)])
    _bind(library, "OpenPrinterW", BOOL, [ctypes.c_wchar_p, ctypes.POINTER(HANDLE), ctypes.c_void_p])
    _bind(library, "StartDocPrinterW", DWORD, [HANDLE, DWORD, ctypes.c_void_p])
    _bind(library, "StartPagePrinter", BOOL, [HANDLE])
    _bind(library, "WritePrinter", BOOL, [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD)])
    for name in ("EndPagePrinter", "EndDocPrinter", "AbortPrinter", "ClosePrinter"):
        _bind(library, name, BOOL, [HANDLE])
    return library


def list_windows_queues(*, api=None) -> list[PrinterQueue]:
    """List installed/local and already-connected queues; no network discovery."""
    api = api if api is not None else _winspool()
    needed, returned = DWORD(), DWORD()
    ok = api.EnumPrintersW(2 | 4, None, 4, None, 0, ctypes.byref(needed), ctypes.byref(returned))
    if not needed.value:
        if not ok:
            error = _win_error(api, "EnumPrintersW")
            if error.winerror not in (0, 122):
                raise error
        return []
    for _ in range(3):
        if needed.value > 16 * 1024 * 1024:
            raise OSError("Printer queue metadata exceeds the 16 MiB inventory bound")
        size = needed.value
        buffer = ctypes.create_string_buffer(size)
        if api.EnumPrintersW(2 | 4, None, 4, buffer, size, ctypes.byref(needed), ctypes.byref(returned)):
            if returned.value * ctypes.sizeof(PRINTER_INFO_4W) > size:
                raise OSError("Printer queue API returned an invalid item count")
            rows = ctypes.cast(buffer, ctypes.POINTER(PRINTER_INFO_4W))
            return [PrinterQueue(rows[i].pPrinterName or "", rows[i].pServerName or "", int(rows[i].Attributes))
                    for i in range(returned.value)]
        error = _win_error(api, "EnumPrintersW")
        if error.winerror != 122 or needed.value <= size:
            raise error
    raise OSError("Printer queue inventory changed repeatedly; retry the explicit listing")


def send_windows_raw(payload: bytes, *, queue_name: str, document_name: str = "Tint & PPF job",
                     log_path: Path | str | None = None, cancel_event=None, api=None) -> DeliveryResult:
    """Submit caller-ready RAW bytes to one explicit installed Windows queue.

    No format conversion or Windows graphics printing occurs. The selected queue
    and its hardware must understand the bytes. A returned job ID confirms
    spooler acceptance only. AbortPrinter cannot recall data already delivered.
    """
    queue_name, document_name = _name(queue_name, "queue name"), _name(document_name, "document name")
    job = _Job("windows_raw", queue_name, payload, log_path)
    handle = HANDLE()
    document_open = False
    error = None
    try:
        job.begin()
        _check_cancel(cancel_event)
        api = api if api is not None else _winspool()
        if not api.OpenPrinterW(queue_name, ctypes.byref(handle), None):
            raise _win_error(api, "OpenPrinterW")
        doc = DOC_INFO_1W(document_name, None, "RAW")
        job.job_id = int(api.StartDocPrinterW(handle, 1, ctypes.byref(doc)))
        if not job.job_id:
            raise _win_error(api, "StartDocPrinterW")
        document_open = True
        if not api.StartPagePrinter(handle):
            raise _win_error(api, "StartPagePrinter")
        while job.sent < len(job.payload):
            _check_cancel(cancel_event)
            chunk = job.payload[job.sent:job.sent + CHUNK_BYTES]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = DWORD()
            ok = api.WritePrinter(handle, buffer, len(chunk), ctypes.byref(written))
            if written.value > len(chunk):
                raise OSError("WritePrinter returned an invalid byte count")
            job.sent += written.value
            if not ok:
                raise _win_error(api, "WritePrinter")
            if not written.value:
                raise OSError("WritePrinter made no progress")
        _check_cancel(cancel_event)
        if not api.EndPagePrinter(handle):
            raise _win_error(api, "EndPagePrinter")
        if not api.EndDocPrinter(handle):
            raise _win_error(api, "EndDocPrinter")
        document_open = False
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
    finally:
        if handle.value:
            if document_open:
                try:
                    if not api.AbortPrinter(handle):
                        raise _win_error(api, "AbortPrinter")
                except Exception as cleanup:
                    job.cleanup_errors.append(str(cleanup))
            try:
                if not api.ClosePrinter(handle):
                    raise _win_error(api, "ClosePrinter")
            except Exception as cleanup:
                job.cleanup_errors.append(str(cleanup))
    return job.finish("queued", "RAW job accepted by Windows spooler; physical output is unverified.", error)


def _kernel32():
    if os.name != "nt":
        raise OSError("Windows COM serial output requires Windows")
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    _bind(library, "CreateFileW", HANDLE, [ctypes.c_wchar_p, DWORD, DWORD, ctypes.c_void_p, DWORD, DWORD, HANDLE])
    _bind(library, "GetCommState", BOOL, [HANDLE, ctypes.POINTER(DCB)])
    _bind(library, "SetCommState", BOOL, [HANDLE, ctypes.POINTER(DCB)])
    _bind(library, "SetCommTimeouts", BOOL, [HANDLE, ctypes.POINTER(COMMTIMEOUTS)])
    _bind(library, "WriteFile", BOOL, [HANDLE, ctypes.c_void_p, DWORD, ctypes.POINTER(DWORD), ctypes.c_void_p])
    _bind(library, "ClearCommError", BOOL, [HANDLE, ctypes.POINTER(DWORD), ctypes.POINTER(COMSTAT)])
    _bind(library, "PurgeComm", BOOL, [HANDLE, DWORD])
    _bind(library, "CloseHandle", BOOL, [HANDLE])
    return library


def serial_dcb(baud: int, parity: str, stop_bits: float, flow_control: str,
               data_bits: int = 8, previous: DCB | None = None) -> DCB:
    """Build explicit framing/handshake settings while preserving other DCB fields."""
    if isinstance(baud, bool) or not isinstance(baud, int) or not 1 <= baud <= 0xffffffff:
        raise ValueError("baud must be an explicit positive 32-bit integer")
    parity_codes = {"N": 0, "O": 1, "E": 2, "M": 3, "S": 4}
    parity = _name(parity, "serial parity (N/O/E/M/S)").upper()
    if parity not in parity_codes:
        raise ValueError("parity must be N, O, E, M, or S")
    if data_bits not in (5, 6, 7, 8):
        raise ValueError("data_bits must be 5, 6, 7, or 8")
    if stop_bits not in (1, 1.5, 2) or (stop_bits == 1.5 and data_bits != 5) or (stop_bits == 2 and data_bits == 5):
        raise ValueError("Invalid data_bits/stop_bits combination")
    if flow_control not in ("none", "rtscts", "xonxoff", "dsrdtr"):
        raise ValueError("flow_control must be none, rtscts, xonxoff, or dsrdtr")
    dcb = previous if previous is not None else DCB()
    dcb.DCBlength, dcb.BaudRate = ctypes.sizeof(DCB), baud
    dcb.ByteSize, dcb.Parity = data_bits, parity_codes[parity]
    dcb.StopBits = {1: 0, 1.5: 1, 2: 2}[stop_bits]
    # Binary; parity as selected; DTR/RTS enabled when not used for handshake.
    dcb.Flags = (1 | (int(parity != "N") << 1) | (int(flow_control == "rtscts") << 2)
                 | (int(flow_control == "dsrdtr") << 3) | ((2 if flow_control == "dsrdtr" else 1) << 4)
                 | (1 << 7) | (int(flow_control == "xonxoff") << 8)
                 | (int(flow_control == "xonxoff") << 9) | ((2 if flow_control == "rtscts" else 1) << 12))
    dcb.wReserved = dcb.wReserved1 = 0
    if dcb.XonChar == dcb.XoffChar:
        dcb.XonChar, dcb.XoffChar = b"\x11", b"\x13"
    if not dcb.XonLim:
        dcb.XonLim = 128
    if not dcb.XoffLim:
        dcb.XoffLim = 128
    return dcb


def send_windows_serial(payload: bytes, *, port: str, baud: int, parity: str,
                        stop_bits: float, flow_control: str, data_bits: int = 8,
                        timeout: float = 10.0, log_path: Path | str | None = None,
                        cancel_event=None, api=None) -> DeliveryResult:
    """Write one exclusively opened Windows COM port with explicit settings.

    Write deadlines and output-queue draining are bounded. No unbounded
    FlushFileBuffers is used. On interruption/error, only this owned port's
    unsent TX queue is purged; transmitted data remains irreversible.
    """
    port = _name(port, "COM port").upper()
    if port.startswith("\\\\.\\"):
        port = port[4:]
    if not re.fullmatch(r"COM[1-9][0-9]*", port):
        raise ValueError("An explicit Windows COM port such as COM3 is required")
    timeout = _timeout(timeout)
    if timeout * 1000 > 0xffffffff:
        raise ValueError("Serial timeout exceeds Windows DWORD milliseconds")
    # Validate before opening a port or recording an output attempt.
    serial_dcb(baud, parity, stop_bits, flow_control, data_bits)
    job = _Job("windows_serial", f"{port} {baud}/{data_bits}{parity.upper()}/{stop_bits} {flow_control}", payload, log_path)
    handle = None
    error = None
    deadline = time.monotonic() + timeout
    try:
        job.begin()
        _check_cancel(cancel_event)
        api = api if api is not None else _kernel32()
        handle = api.CreateFileW("\\\\.\\" + port, 0xc0000000, 0, None, 3, 0, None)
        handle_value = handle.value if isinstance(handle, HANDLE) else handle
        if handle_value in (None, 0, INVALID_HANDLE_VALUE, -1):
            handle = None
            raise _win_error(api, "CreateFileW(COM)")
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not api.GetCommState(handle, ctypes.byref(dcb)):
            raise _win_error(api, "GetCommState")
        serial_dcb(baud, parity, stop_bits, flow_control, data_bits, dcb)
        if not api.SetCommState(handle, ctypes.byref(dcb)):
            raise _win_error(api, "SetCommState")
        while job.sent < len(job.payload):
            _check_cancel(cancel_event)
            milliseconds = max(1, math.ceil(_remaining(deadline) * 1000))
            timeouts = COMMTIMEOUTS(0xffffffff, 0, 0, 0, milliseconds)
            if not api.SetCommTimeouts(handle, ctypes.byref(timeouts)):
                raise _win_error(api, "SetCommTimeouts")
            chunk = job.payload[job.sent:job.sent + CHUNK_BYTES]
            buffer = ctypes.create_string_buffer(chunk, len(chunk))
            written = DWORD()
            ok = api.WriteFile(handle, buffer, len(chunk), ctypes.byref(written), None)
            if written.value > len(chunk):
                raise OSError("WriteFile(COM) returned an invalid byte count")
            job.sent += written.value
            if not ok:
                raise _win_error(api, "WriteFile(COM)")
            if not written.value:
                raise TimeoutError("Serial write made no progress before its deadline")
        while True:
            _check_cancel(cancel_event)
            _remaining(deadline)
            errors, status = DWORD(), COMSTAT()
            if not api.ClearCommError(handle, ctypes.byref(errors), ctypes.byref(status)):
                raise _win_error(api, "ClearCommError")
            if errors.value:
                raise OSError(f"Serial communications error flags: 0x{errors.value:x}")
            if status.cbOutQue == 0:
                break
            time.sleep(min(.01, _remaining(deadline)))
    except (Exception, KeyboardInterrupt) as caught:
        error = caught
    finally:
        if handle is not None:
            if error is not None:
                try:
                    if not api.PurgeComm(handle, 1 | 4):  # PURGE_TXABORT | PURGE_TXCLEAR
                        raise _win_error(api, "PurgeComm(TX)")
                except Exception as cleanup:
                    job.cleanup_errors.append(str(cleanup))
            try:
                if not api.CloseHandle(handle):
                    raise _win_error(api, "CloseHandle(COM)")
            except Exception as cleanup:
                job.cleanup_errors.append(str(cleanup))
    return job.finish("sent_unconfirmed", "Bytes left the Windows serial output queue; device execution is unverified.", error)
