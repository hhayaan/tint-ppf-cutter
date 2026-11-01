"""Mock Win32-only direct USB checks. No real USB enumeration or I/O occurs."""
import ctypes
import threading
import unittest
import uuid

from printing import usb_transport as usb
from printing.transports import DeliveryError, DWORD

DEVICE_PATH = r"\\?\usb#vid_1234&pid_abcd#owner-selected-instance#{a5dcbf10-6530-11d2-901f-00c04fb951ed}"


def set_dword(pointer, value):
    ctypes.cast(pointer, ctypes.POINTER(DWORD)).contents.value = value


class FakeSetup:
    def __init__(self):
        self.calls = []
        self.error = 0

    def last_error(self):
        return self.error

    def SetupDiGetClassDevsW(self, guid, enumerator, parent, flags):
        self.calls.append(("class", ctypes.string_at(guid, 16), flags))
        return 100

    def SetupDiEnumDeviceInterfaces(self, handle, info, guid, index, interface):
        self.calls.append(("enum", index))
        self.error = 259 if index else 0
        return index == 0

    def SetupDiGetDeviceInterfaceDetailW(self, handle, interface, detail, size, needed, info):
        text = ctypes.create_unicode_buffer(DEVICE_PATH)
        length = 4 + ctypes.sizeof(text)
        set_dword(needed, length)
        if detail is None:
            self.error = 122
            return False
        expected = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
        assert ctypes.cast(detail, ctypes.POINTER(DWORD)).contents.value == expected
        ctypes.memmove(ctypes.addressof(detail) + 4, text, ctypes.sizeof(text))
        self.calls.append(("detail", size, expected))
        return True

    def SetupDiGetDeviceRegistryPropertyW(self, handle, info, property_id, kind, buffer, size, needed):
        text = (r"USB\VID_1234&PID_ABCD" + "\x00\x00" if property_id == 1 else "Owner-selected fake device\x00")
        raw = text.encode("utf-16-le")
        set_dword(kind, 7 if property_id == 1 else 1)
        set_dword(needed, len(raw))
        if buffer is None:
            self.error = 122
            return False
        ctypes.memmove(buffer, raw, len(raw))
        return True

    def SetupDiDestroyDeviceInfoList(self, handle):
        self.calls.append(("destroy", handle))
        return True


class FakeKernel:
    def __init__(self, mode="immediate", cancel_event=None):
        self.calls = []
        self.data = bytearray()
        self.error = 0
        self.mode = mode
        self.cancel_event = cancel_event
        self.cancelled = False
        self.released = False
        self.count = 0

    def last_error(self):
        return self.error

    def CreateFileW(self, path, access, share, security, disposition, flags, template):
        self.calls.append(("open", path, access, share, disposition, flags))
        return 200

    def CreateEventW(self, security, manual, initial, name):
        self.calls.append(("event", bool(manual), bool(initial)))
        return 201

    def ResetEvent(self, event):
        self.calls.append(("reset",))
        return True

    def WriteFile(self, handle, buffer, size, written, overlapped):
        self.count = min(2, size)
        self.data.extend(ctypes.string_at(buffer, self.count))
        self.calls.append(("write", size, bool(overlapped)))
        if self.cancel_event:
            self.cancel_event.set()
        if self.mode == "immediate":
            set_dword(written, self.count)
            return True
        self.error = 997
        return False

    def WaitForSingleObject(self, event, milliseconds):
        self.calls.append(("wait", milliseconds))
        if self.mode == "pending" or self.released or (self.cancelled and self.mode != "stuck"):
            return 0
        return 258

    def GetOverlappedResult(self, handle, overlapped, written, wait):
        self.calls.append(("result", bool(wait)))
        if self.cancelled and not self.released:
            self.error = 995
            return False
        set_dword(written, self.count)
        return True

    def CancelIoEx(self, handle, overlapped):
        self.calls.append(("cancel",))
        self.cancelled = True
        return True

    def CloseHandle(self, handle):
        self.calls.append(("close", handle))
        return True


class USBTransportTests(unittest.TestCase):
    def test_setupapi_metadata_listing_has_exact_guid_and_never_opens_device(self):
        fake = FakeSetup()
        devices = usb.list_usb_devices(api=fake)
        self.assertEqual(len(devices), 1)
        self.assertEqual((devices[0].path, devices[0].vid, devices[0].pid), (DEVICE_PATH, 0x1234, 0xabcd))
        self.assertEqual(fake.calls[0], ("class", uuid.UUID(usb.USB_DEVICE_GUID).bytes_le, 0x12))
        self.assertEqual(fake.calls[-1], ("destroy", 100))
        self.assertFalse(any(call[0] == "open" for call in fake.calls))

    def test_explicit_identity_and_path_reject_files_disks_or_mismatched_vid(self):
        for path, vid, pid in ((r"C:\job.plt", 0x1234, 0xabcd), (r"\\.\PhysicalDrive0", 0x1234, 0xabcd),
                               (DEVICE_PATH, 0x9999, 0xabcd), (DEVICE_PATH.replace("#owner", "#..\\owner"), 0x1234, 0xabcd)):
            fake = FakeKernel()
            with self.subTest(path=path), self.assertRaises(ValueError):
                usb.send_usb(b"job", device_path=path, vid=vid, pid=pid, api=fake)
            self.assertFalse(fake.calls)

    def test_raw_bytes_partial_completions_and_owned_handle_cleanup(self):
        fake = FakeKernel()
        result = usb.send_usb(b"IN;\x00ab", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd, api=fake)
        self.assertEqual(fake.data, b"IN;\x00ab")
        self.assertEqual(fake.calls[0], ("open", DEVICE_PATH, 0x40000000, 7, 3, 0x40000000))
        self.assertEqual(fake.calls[-2:], [("close", 200), ("close", 201)])
        self.assertEqual((result.state, result.bytes_sent), ("sent_unconfirmed", 6))
        self.assertFalse(result.physical_completion_verified)

    def test_overlapped_pending_waits_and_gets_reported_completion_count(self):
        fake = FakeKernel(mode="pending")
        result = usb.send_usb(b"abcd", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd, api=fake)
        self.assertEqual(result.bytes_sent, 4)
        self.assertTrue(any(call[0] == "wait" for call in fake.calls))
        self.assertFalse(any(call[0] == "cancel" for call in fake.calls))

    def test_timeout_cancels_owned_overlapped_and_closes_after_completion(self):
        fake = FakeKernel(mode="timeout")
        with self.assertRaises(DeliveryError) as context:
            usb.send_usb(b"abcdef", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd,
                         timeout=.002, cleanup_timeout=.01, api=fake)
        self.assertEqual(context.exception.result.state, "partial")
        self.assertEqual(context.exception.result.bytes_sent, 0)
        self.assertIn(("cancel",), fake.calls)
        self.assertEqual(fake.calls[-2:], [("close", 200), ("close", 201)])
        self.assertEqual(sum(call[0] == "write" for call in fake.calls), 1)

    def test_cancellation_preserves_uncertainty_and_never_retries(self):
        event = threading.Event()
        fake = FakeKernel(mode="timeout", cancel_event=event)
        with self.assertRaises(DeliveryError) as context:
            usb.send_usb(b"abcdef", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd,
                         cancel_event=event, api=fake)
        self.assertEqual(context.exception.result.state, "cancelled")
        self.assertIn("in-flight", context.exception.result.message)
        self.assertEqual(sum(call[0] == "write" for call in fake.calls), 1)

    def test_stuck_cancellation_retains_live_buffer_until_later_reaping(self):
        fake = FakeKernel(mode="stuck")
        with self.assertRaises(DeliveryError) as context:
            usb.send_usb(b"abcdef", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd,
                         timeout=.001, cleanup_timeout=.001, api=fake)
        self.assertTrue(context.exception.result.cleanup_errors)
        self.assertIn("Recheck pending USB cleanup", context.exception.result.message)
        self.assertFalse(any(call[0] == "close" for call in fake.calls))
        pending = usb.reap_pending_usb_io()
        self.assertTrue(any("still pending" in status for status in pending.values()))
        second = FakeKernel()
        with self.assertRaisesRegex(ValueError, "Previous USB cleanup"):
            usb.send_usb(b"another job", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd, api=second)
        self.assertFalse(second.calls)
        fake.released = True
        released = usb.reap_pending_usb_io()
        self.assertTrue(any(status == "released" for status in released.values()))
        self.assertEqual(fake.calls[-2:], [("close", 200), ("close", 201)])

    def test_completed_io_with_close_failure_retains_handle_for_explicit_reaping(self):
        class CloseOnceFails(FakeKernel):
            def __init__(self):
                super().__init__()
                self.failed = False

            def CloseHandle(self, handle):
                self.calls.append(("close", handle))
                if handle == 200 and not self.failed:
                    self.failed = True
                    self.error = 6
                    return False
                return True

        fake = CloseOnceFails()
        result = usb.send_usb(b"ab", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd, api=fake)
        self.assertEqual((result.state, result.bytes_sent), ("sent_unconfirmed", 2))
        self.assertIn("Cleanup warnings", result.message)
        before_writes = sum(call[0] == "write" for call in fake.calls)
        self.assertTrue(any(status == "released" for status in usb.reap_pending_usb_io().values()))
        self.assertEqual(sum(call[0] == "write" for call in fake.calls), before_writes)

    def test_race_completion_during_cancel_preserves_reported_byte_count(self):
        class FinishesDuringCancel(FakeKernel):
            def GetOverlappedResult(self, handle, overlapped, written, wait):
                set_dword(written, self.count)
                return True

        fake = FinishesDuringCancel(mode="timeout")
        with self.assertRaises(DeliveryError) as context:
            usb.send_usb(b"abcdef", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd, timeout=.001, api=fake)
        self.assertEqual(context.exception.result.bytes_sent, 2)

    def test_pipe_suffix_is_only_added_when_explicitly_requested(self):
        self.assertEqual(usb.validated_device_path(DEVICE_PATH, 0x1234, 0xabcd), DEVICE_PATH)
        self.assertEqual(usb.validated_device_path(DEVICE_PATH, 0x1234, 0xabcd, 2), DEVICE_PATH + r"\Pipe02")
        with self.assertRaises(ValueError):
            usb.validated_device_path(DEVICE_PATH, 0x1234, 0xabcd, -1)

    def test_precancelled_usb_job_never_opens_any_handle(self):
        fake = FakeKernel()
        event = threading.Event()
        event.set()
        with self.assertRaises(DeliveryError) as context:
            usb.send_usb(b"job", device_path=DEVICE_PATH, vid=0x1234, pid=0xabcd, cancel_event=event, api=fake)
        self.assertEqual(context.exception.result.state, "cancelled")
        self.assertFalse(fake.calls)


if __name__ == "__main__":
    unittest.main()
