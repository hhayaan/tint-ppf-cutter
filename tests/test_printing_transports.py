"""Offline transport checks: temporary files, loopback TCP and fake Win32 APIs."""
from __future__ import annotations

import ctypes
import errno
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from printing import transports as t


def output_value(pointer, value, kind=t.DWORD):
    ctypes.cast(pointer, ctypes.POINTER(kind)).contents.value = value


class FakeSpooler:
    def __init__(self, fail_after=None, cancel_event=None):
        self.calls = []
        self.data = bytearray()
        self.fail_after = fail_after
        self.cancel_event = cancel_event
        self.references = []

    def last_error(self):
        return 123

    def EnumPrintersW(self, flags, name, level, buffer, size, needed, returned):
        self.calls.append(("enum", flags, name, level))
        output_value(needed, ctypes.sizeof(t.PRINTER_INFO_4W))
        if buffer is None:
            output_value(returned, 0)
            return False
        printer = ctypes.create_unicode_buffer("Explicit test queue")
        self.references.append(printer)
        item = ctypes.cast(buffer, ctypes.POINTER(t.PRINTER_INFO_4W)).contents
        item.pPrinterName = ctypes.cast(printer, ctypes.c_wchar_p)
        item.pServerName = None
        item.Attributes = 64
        output_value(returned, 1)
        return True

    def OpenPrinterW(self, name, handle, defaults):
        self.calls.append(("open", name, defaults))
        output_value(handle, 100, t.HANDLE)
        return True

    def StartDocPrinterW(self, handle, level, info):
        doc = ctypes.cast(info, ctypes.POINTER(t.DOC_INFO_1W)).contents
        self.calls.append(("start_doc", level, doc.pDocName, doc.pDatatype, doc.pOutputFile))
        return 42

    def StartPagePrinter(self, handle):
        self.calls.append(("start_page",))
        return True

    def WritePrinter(self, handle, buffer, size, written):
        count = min(2, size)
        self.data.extend(ctypes.string_at(buffer, count))
        output_value(written, count)
        self.calls.append(("write", count))
        if self.cancel_event:
            self.cancel_event.set()
        return self.fail_after is None or len(self.data) < self.fail_after

    def EndPagePrinter(self, handle):
        self.calls.append(("end_page",))
        return True

    def EndDocPrinter(self, handle):
        self.calls.append(("end_doc",))
        return True

    def AbortPrinter(self, handle):
        self.calls.append(("abort",))
        return True

    def ClosePrinter(self, handle):
        self.calls.append(("close",))
        return True


class FakeSerial:
    def __init__(self, fail_after=None, cancel_event=None):
        self.calls = []
        self.data = bytearray()
        self.fail_after = fail_after
        self.cancel_event = cancel_event
        self.dcb = None
        self.timeouts = []
        self.pending = [2, 0]

    def last_error(self):
        return 1117

    def CreateFileW(self, port, access, sharing, security, disposition, flags, template):
        self.calls.append(("open", port, access, sharing, disposition, flags))
        return 200

    def GetCommState(self, handle, pointer):
        self.calls.append(("get_state",))
        state = ctypes.cast(pointer, ctypes.POINTER(t.DCB)).contents
        state.XonChar, state.XoffChar = b"\x11", b"\x13"
        return True

    def SetCommState(self, handle, pointer):
        self.dcb = t.DCB.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(t.DCB)))
        self.calls.append(("set_state",))
        return True

    def SetCommTimeouts(self, handle, pointer):
        self.timeouts.append(t.COMMTIMEOUTS.from_buffer_copy(ctypes.string_at(pointer, ctypes.sizeof(t.COMMTIMEOUTS))))
        self.calls.append(("timeouts",))
        return True

    def WriteFile(self, handle, buffer, size, written, overlapped):
        count = min(2, size)
        self.data.extend(ctypes.string_at(buffer, count))
        output_value(written, count)
        self.calls.append(("write", count, overlapped))
        if self.cancel_event:
            self.cancel_event.set()
        return self.fail_after is None or len(self.data) < self.fail_after

    def ClearCommError(self, handle, errors, status):
        output_value(errors, 0)
        item = ctypes.cast(status, ctypes.POINTER(t.COMSTAT)).contents
        item.cbOutQue = self.pending.pop(0) if self.pending else 0
        self.calls.append(("drain", item.cbOutQue))
        return True

    def PurgeComm(self, handle, flags):
        self.calls.append(("purge", flags))
        return True

    def CloseHandle(self, handle):
        self.calls.append(("close",))
        return True


class FakeSocket:
    def __init__(self, fail=False, cancel_event=None):
        self.data = bytearray()
        self.closed = False
        self.fail = fail
        self.cancel_event = cancel_event

    def settimeout(self, value):
        self.timeout = value

    def send(self, chunk):
        if self.data and self.fail:
            raise BrokenPipeError(errno.EPIPE, "offline fake connection failed")
        count = min(2, len(chunk))
        self.data.extend(chunk[:count])
        if self.cancel_event:
            self.cancel_event.set()
        return count

    def close(self):
        self.closed = True


class FileTransportTests(unittest.TestCase):
    def test_new_binary_file_and_jsonl_status_without_payload_logging(self):
        with tempfile.TemporaryDirectory() as directory:
            path, log = Path(directory) / "job.plt", Path(directory) / "jobs.jsonl"
            result = t.write_job_file(path, b"IN;PU0,0;\x00", log_path=log)
            self.assertEqual(path.read_bytes(), b"IN;PU0,0;\x00")
            self.assertEqual((result.state, result.bytes_sent), ("saved", 10))
            events = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual([item["event"] for item in events], ["started", "finished"])
            self.assertEqual(events[-1]["state"], "saved")
            self.assertEqual(events[-1]["payload_sha256"], result.payload_sha256)
            self.assertNotIn("IN;PU", log.read_text())

    def test_existing_file_preserved_and_no_alternate_output_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.plt"
            path.write_bytes(b"original bytes")
            with self.assertRaises(t.DeliveryError) as context:
                t.write_job_file(path, b"replacement")
            self.assertEqual(path.read_bytes(), b"original bytes")
            self.assertEqual(context.exception.result.state, "failed")
            self.assertEqual(context.exception.result.bytes_sent, 0)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            with self.assertRaises(ValueError):
                t.write_job_file(path, b"job", log_path=path)

    def test_partial_failed_file_remains_for_review(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.plt"
            original_open = Path.open

            class PartialFile:
                def __enter__(self):
                    self.stream = original_open(path, "xb")
                    self.calls = 0
                    return self

                def write(self, chunk):
                    self.calls += 1
                    if self.calls > 1:
                        raise OSError(errno.ENOSPC, "offline disk-full test")
                    return self.stream.write(chunk[:2])

                def __exit__(self, *args):
                    self.stream.close()

            with patch.object(Path, "open", lambda *args, **kwargs: PartialFile()):
                with self.assertRaises(t.DeliveryError) as context:
                    t.write_job_file(path, b"abcdef")
            self.assertEqual(path.read_bytes(), b"ab")
            self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("partial", 2))


class TcpTransportTests(unittest.TestCase):
    def test_loopback_ephemeral_server_receives_exact_bytes_and_reply_is_bounded(self):
        payload = b"IN;PU0,0;PD100,100;PU;\x00" * 5000
        captured = bytearray()
        errors = []
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        port = listener.getsockname()[1]

        def serve():
            try:
                with listener:
                    connection, _ = listener.accept()
                    with connection:
                        connection.settimeout(3)
                        while len(captured) < len(payload):
                            part = connection.recv(min(8192, len(payload) - len(captured)))
                            if not part:
                                break
                            captured.extend(part)
                        connection.sendall(b"ACK:opaque-device-reply")
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=serve)
        worker.start()
        result = t.send_tcp(payload, host="127.0.0.1", port=port, timeout=3, reply_limit=4)
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertEqual(bytes(captured), payload)
        self.assertEqual(result.reply, b"ACK:")
        self.assertEqual(result.reply_status, "limit_reached")
        self.assertEqual(result.state, "sent_unconfirmed")
        self.assertFalse(result.physical_completion_verified)

    def test_partial_write_failure_is_reported_without_retry_or_failover(self):
        fake = FakeSocket(fail=True)
        calls = []

        def connect(endpoint, timeout):
            calls.append(endpoint)
            return fake

        with self.assertRaises(t.DeliveryError) as context:
            t.send_tcp(b"abcdef", host="explicit.invalid", port=12345, _connector=connect)
        self.assertEqual(calls, [("explicit.invalid", 12345)])
        self.assertEqual(fake.data, b"ab")
        self.assertTrue(fake.closed)
        self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("partial", 2))

    def test_reply_timeout_is_bounded_and_does_not_repeat_completed_job(self):
        class ReplyTimeout(FakeSocket):
            def recv(self, count):
                raise TimeoutError("offline reply timeout")

        fake = ReplyTimeout()
        result = t.send_tcp(b"abc", host="explicit.invalid", port=12345, reply_limit=20,
                            reply_timeout=.01, _connector=lambda *args, **kwargs: fake)
        self.assertEqual(fake.data, b"abc")
        self.assertEqual(result.reply_status, "timeout")
        self.assertEqual(result.state, "sent_unconfirmed")

    def test_cancellation_retains_partial_progress_and_closes_connection(self):
        event = threading.Event()
        fake = FakeSocket(cancel_event=event)
        with self.assertRaises(t.DeliveryError) as context:
            t.send_tcp(b"abcdef", host="explicit.invalid", port=12345, cancel_event=event,
                       _connector=lambda *args, **kwargs: fake)
        self.assertEqual(context.exception.result.state, "cancelled")
        self.assertEqual(context.exception.result.bytes_sent, 2)
        self.assertTrue(fake.closed)

    def test_destination_and_reply_bound_required_before_connection(self):
        with patch.object(socket, "create_connection") as connector:
            for parameters in ({"host": "", "port": 1}, {"host": "host", "port": 0},
                               {"host": "host", "port": 1, "reply_limit": t.MAX_REPLY_BYTES + 1}):
                with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                    t.send_tcp(b"job", **parameters)
            with self.assertRaises(TypeError):
                t.send_tcp(b"job", host="host")
            connector.assert_not_called()

    def test_preexisting_cancellation_never_opens_connection(self):
        event = threading.Event()
        event.set()
        with patch.object(socket, "create_connection") as connector:
            with self.assertRaises(t.DeliveryError) as context:
                t.send_tcp(b"job", host="explicit.invalid", port=12345, cancel_event=event)
            connector.assert_not_called()
        self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("cancelled", 0))


class WindowsRawTests(unittest.TestCase):
    def test_queue_enumeration_only_uses_local_and_existing_connections(self):
        fake = FakeSpooler()
        queues = t.list_windows_queues(api=fake)
        self.assertEqual(queues, [t.PrinterQueue("Explicit test queue", "", 64)])
        self.assertEqual(fake.calls, [("enum", 6, None, 4), ("enum", 6, None, 4)])

    def test_raw_job_sequence_loops_partial_writes_and_returns_spool_id(self):
        fake = FakeSpooler()
        result = t.send_windows_raw(b"abcdef", queue_name="Explicit test queue", api=fake)
        self.assertEqual(fake.data, b"abcdef")
        self.assertEqual((result.state, result.job_id, result.bytes_sent), ("queued", 42, 6))
        self.assertIn(("start_doc", 1, "Tint & PPF job", "RAW", None), fake.calls)
        self.assertEqual([row[0] for row in fake.calls], ["open", "start_doc", "start_page", "write", "write", "write", "end_page", "end_doc", "close"])

    def test_raw_partial_failure_aborts_owned_job_then_closes(self):
        fake = FakeSpooler(fail_after=4)
        with self.assertRaises(t.DeliveryError) as context:
            t.send_windows_raw(b"abcdef", queue_name="Explicit test queue", api=fake)
        self.assertEqual(context.exception.result.bytes_sent, 4)
        self.assertEqual(context.exception.result.error_code, 123)
        self.assertEqual([row[0] for row in fake.calls][-2:], ["abort", "close"])
        self.assertNotIn(("end_doc",), fake.calls)

    def test_empty_queue_rejected_before_any_windows_call(self):
        fake = FakeSpooler()
        with self.assertRaises(ValueError):
            t.send_windows_raw(b"job", queue_name="", api=fake)
        self.assertFalse(fake.calls)

    def test_spooled_success_and_cleanup_problem_are_reported_separately(self):
        class CloseFailure(FakeSpooler):
            def ClosePrinter(self, handle):
                self.calls.append(("close",))
                return False

        result = t.send_windows_raw(b"job", queue_name="Explicit test queue", api=CloseFailure())
        self.assertEqual((result.state, result.bytes_sent, result.job_id), ("queued", 3, 42))
        self.assertTrue(result.cleanup_errors)
        self.assertIn("ClosePrinter", result.cleanup_errors[0])


class WindowsSerialTests(unittest.TestCase):
    def send(self, fake, **kwargs):
        return t.send_windows_serial(b"abcdef", port="COM12", baud=9600, parity="E", stop_bits=1,
                                     flow_control="rtscts", timeout=1, api=fake, **kwargs)

    def test_explicit_dcb_timeouts_exclusive_port_and_output_drain(self):
        fake = FakeSerial()
        result = self.send(fake)
        self.assertEqual(ctypes.sizeof(t.DCB), 28)
        self.assertEqual(fake.calls[0], ("open", r"\\.\COM12", 0xc0000000, 0, 3, 0))
        self.assertEqual((fake.dcb.BaudRate, fake.dcb.ByteSize, fake.dcb.Parity, fake.dcb.StopBits), (9600, 8, 2, 0))
        self.assertTrue(fake.dcb.Flags & (1 << 2))
        self.assertEqual((fake.dcb.Flags >> 12) & 3, 2)
        self.assertTrue(all(row.WriteTotalTimeoutConstant > 0 for row in fake.timeouts))
        self.assertEqual(fake.data, b"abcdef")
        self.assertEqual(result.state, "sent_unconfirmed")
        self.assertEqual([row[0] for row in fake.calls][-3:], ["drain", "drain", "close"])

    def test_partial_serial_failure_purges_unsent_bytes_and_closes_owned_port(self):
        fake = FakeSerial(fail_after=4)
        with self.assertRaises(t.DeliveryError) as context:
            self.send(fake)
        self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("partial", 4))
        self.assertEqual(fake.calls[-2:], [("purge", 5), ("close",)])

    def test_serial_cancel_is_reported_with_partial_byte_count(self):
        event = threading.Event()
        fake = FakeSerial(cancel_event=event)
        with self.assertRaises(t.DeliveryError) as context:
            self.send(fake, cancel_event=event)
        self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("cancelled", 2))
        self.assertEqual(fake.calls[-2:], [("purge", 5), ("close",)])

    def test_invalid_serial_framing_is_rejected_before_opening_any_port(self):
        fake = FakeSerial()
        for values in ((9600, "N", 1.5, "none", 8), (9600, "N", 2, "none", 5),
                       (0, "N", 1, "none", 8), (9600, "N", 1, "guess", 8)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                t.serial_dcb(*values)
        with self.assertRaises(ValueError):
            t.send_windows_serial(b"job", port=r"\\.\PhysicalDrive0", baud=9600, parity="N",
                                  stop_bits=1, flow_control="none", api=fake)
        self.assertFalse(fake.calls)

    def test_software_flow_control_and_no_flow_control_are_distinct(self):
        software = t.serial_dcb(19200, "N", 1, "xonxoff")
        none = t.serial_dcb(19200, "N", 1, "none")
        self.assertEqual((software.Flags >> 8) & 3, 3)
        self.assertEqual((none.Flags >> 8) & 3, 0)
        self.assertNotEqual(software.XonChar, software.XoffChar)

    def test_zero_progress_does_not_loop_and_unsent_queue_is_purged(self):
        class NoProgress(FakeSerial):
            def WriteFile(self, handle, buffer, size, written, overlapped):
                self.calls.append(("write", 0, overlapped))
                output_value(written, 0)
                return True

        fake = NoProgress()
        with self.assertRaises(t.DeliveryError) as context:
            self.send(fake)
        self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("failed", 0))
        self.assertEqual(fake.calls[-2:], [("purge", 5), ("close",)])

    def test_stalled_output_queue_obeys_deadline_without_flushing(self):
        class StalledQueue(FakeSerial):
            def ClearCommError(self, handle, errors, status):
                output_value(errors, 0)
                ctypes.cast(status, ctypes.POINTER(t.COMSTAT)).contents.cbOutQue = 1
                self.calls.append(("drain", 1))
                return True

            def FlushFileBuffers(self, handle):
                raise AssertionError("An unbounded serial flush must not be called")

        fake = StalledQueue()
        ticks = iter(index / 10 for index in range(100))
        with patch.object(t.time, "monotonic", side_effect=lambda: next(ticks)), \
                patch.object(t.time, "sleep") as sleep, \
                self.assertRaises(t.DeliveryError) as context:
            self.send(fake)
        self.assertEqual(context.exception.result.bytes_sent, 6)
        self.assertEqual(context.exception.result.state, "partial")
        self.assertIn("deadline", context.exception.result.message)
        self.assertFalse(context.exception.result.physical_completion_verified)
        self.assertTrue(sleep.called)
        self.assertEqual(fake.calls[-2:], [("purge", 5), ("close",)])
        self.assertLess(len([row for row in fake.calls if row[0] == "drain"]), 10)

    def test_cancel_during_output_queue_wait_preserves_progress_and_closes(self):
        event = threading.Event()

        class CancelDuringDrain(FakeSerial):
            def ClearCommError(self, handle, errors, status):
                output_value(errors, 0)
                ctypes.cast(status, ctypes.POINTER(t.COMSTAT)).contents.cbOutQue = 1
                self.calls.append(("drain", 1))
                event.set()
                return True

        fake = CancelDuringDrain()
        with patch.object(t.time, "sleep"), self.assertRaises(t.DeliveryError) as context:
            self.send(fake, cancel_event=event)
        self.assertEqual((context.exception.result.state, context.exception.result.bytes_sent), ("cancelled", 6))
        self.assertEqual(fake.calls[-3:], [("drain", 1), ("purge", 5), ("close",)])

    def test_output_queue_status_failure_or_error_flags_never_claim_success(self):
        for mode in ("api_failure", "communication_error"):
            class QueueFailure(FakeSerial):
                def ClearCommError(self, handle, errors, status):
                    self.calls.append(("drain_error", mode))
                    output_value(errors, 0x100 if mode == "communication_error" else 0)
                    return mode != "api_failure"

            fake = QueueFailure()
            with self.subTest(mode=mode), self.assertRaises(t.DeliveryError) as context:
                self.send(fake)
            self.assertEqual(context.exception.result.bytes_sent, 6)
            self.assertEqual(context.exception.result.state, "partial")
            self.assertFalse(context.exception.result.physical_completion_verified)
            self.assertEqual(fake.calls[-2:], [("purge", 5), ("close",)])


if __name__ == "__main__":
    unittest.main()
