"""Exercise the real dialog lifecycle without opening any output device."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from printing import dialog
from printing.backend import CompiledJob
from printing.settings import load_settings, save_profile_settings
from tools.patterns.fvd_pattern import Pattern, VectorPath


@unittest.skipIf(dialog.tk is None, "Python was built without Tk")
class CutWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.workspace = Path(self.directory.name)
        self.root = dialog.tk.Tk()
        self.root.withdraw()
        points = ((0., 0., 0, 0), (72., 0., 4, 0), (72., 72., 4, 0),
                  (0., 72., 4, 0), (0., 0., 4, 0))
        self.pattern = Pattern("fixture.FDU", "a" * 64, 0, 2, (),
                               (VectorPath(0, 0, points, b""),), 1, (1,), (), 23, True, ())
        self.windows = []
        self.patches = []
        # Fail if the test accidentally discovers or opens physical hardware.
        for name in ("transports.send_tcp", "transports.send_windows_raw", "transports.send_windows_serial",
                     "transports.list_windows_queues", "usb_transport.send_usb", "usb_transport.list_usb_devices"):
            self.patches.append(patch("printing.dialog." + name, side_effect=AssertionError("hardware call")))
        self.patches += [patch("printing.dialog.messagebox.showinfo"),
                         patch("printing.dialog.messagebox.showwarning"),
                         patch("printing.dialog.messagebox.showerror")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for window in self.windows:
            if window.winfo_exists():
                window.close()
        self.root.destroy()
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def open_dialog(self):
        window = dialog.CutDialog(self.root, pattern=self.pattern, workspace=self.workspace)
        window.withdraw()
        self.windows.append(window)
        self.root.update()
        return window

    def wait_for_job(self, window):
        deadline = time.monotonic() + 5
        while window.busy and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.root.update()
        self.assertFalse(window.busy, "dialog worker did not finish")

    def test_original_size_prepare_save_reopen_and_no_resize_persistence(self):
        window = self.open_dialog()
        window.variables["runtime"].set(str(self.workspace))
        window.variables["media_width"].set("600")
        window.variables["scale_percent"].set("50")  # Ignored until Resize is enabled.
        output = self.workspace / "new-exports" / "original.plt"
        window.variables["file"].set(str(output))
        compiled = CompiledJob(b"fixture-job", {"native_bounds_mm": [0, 0, 25.4, 25.4]})
        with patch("printing.backend.compile_job", return_value=compiled) as compiler:
            window.prepare()
            self.wait_for_job(window)
        self.assertIsNotNone(window.compiled, window.status.get())
        self.assertEqual(window.variables["unit_scale"].get(), "")
        self.assertAlmostEqual(compiler.call_args.args[0].width_mm, 25.4)
        self.assertEqual(window.compiled.metadata["job_scale"], 1)
        self.assertTrue(window.compiled.metadata["source_units"]["automatic"])
        self.assertIn("25.40", window.size_summary.get())
        window.send()
        self.wait_for_job(window)
        self.assertEqual(output.read_bytes(), b"fixture-job")
        self.assertIsNone(window.compiled)
        self.assertEqual(str(window.send_button["state"]), "disabled")
        window.variables["resize_enabled"].set(True)
        window.variables["copies"].set("3")
        self.assertTrue(window.save_setup(quiet=True))
        window.close()
        reopened = self.open_dialog()
        self.assertEqual(reopened.variables["media_width"].get(), "600.0")
        self.assertEqual(reopened.variables["media_mode"].get(), "roll")
        self.assertFalse(reopened.variables["resize_enabled"].get())
        self.assertEqual(reopened.variables["scale_percent"].get(), "100")
        self.assertEqual(reopened.variables["copies"].get(), "1")
        self.assertIsNone(reopened.compiled)

    def test_saved_device_target_is_restored_without_contacting_it_or_bleeding_profiles(self):
        path = self.workspace / ".runtime" / "cut-settings.json"
        save_profile_settings(path, "graphtec:53", setup={
            "connection": {"type": "tcp", "host": "explicit.invalid", "port": 12345, "timeout": 3},
            "media": {"mode": "roll", "width_mm": 1300, "height_mm": None}}, tools={"force": 20})
        window = self.open_dialog()
        self.assertEqual(window.transport_tabs.tab(window.transport_tabs.select(), "text"), "TCP")
        self.assertEqual(window.variables["host"].get(), "explicit.invalid")
        self.assertEqual(window.variables["port"].get(), "12345")
        self.assertEqual(window.variables["force"].get(), "20")
        self.assertIsNone(window.compiled)
        other = next(label for label, key in window.profiles.items() if key == "graphtec:52")
        window.variables["profile"].set(other)
        self.root.update()
        self.assertEqual(window.variables["host"].get(), "")
        self.assertEqual(window.variables["media_width"].get(), "")
        self.assertEqual(window.variables["force"].get(), "")
        self.assertEqual(window.transport_tabs.tab(window.transport_tabs.select(), "text"), "File")
        self.assertEqual(load_settings(path)["last_profile"], "graphtec:53")

    def test_moved_project_reuses_local_runtime_when_saved_absolute_path_is_missing(self):
        bundled = self.workspace / ".runtime" / "vendor"
        bundled.mkdir(parents=True)
        save_profile_settings(self.workspace / ".runtime" / "cut-settings.json", "graphtec:53",
                              setup={"runtime_directory": str(self.workspace / "old-project" / "vendor")})
        window = self.open_dialog()
        self.assertEqual(Path(window.variables["runtime"].get()), bundled)
        self.assertIsNone(window.compiled)

    def test_completed_file_uses_immutable_result_after_edits_during_send(self):
        actual_delivery = dialog.deliver
        for change_target in (False, True):
            with self.subTest(change_target=change_target):
                window = self.open_dialog()
                window.variables["runtime"].set(str(self.workspace))
                window.variables["media_width"].set("600")
                output = self.workspace / "exports" / f"cut-job-race-{change_target}.plt"
                window.variables["file"].set(str(output))
                compiled = CompiledJob(b"reviewed-snapshot", {"native_bounds_mm": [0, 0, 25.4, 25.4]})
                with patch("printing.backend.compile_job", return_value=compiled):
                    window.prepare()
                    self.wait_for_job(window)
                self.assertIsNotNone(window.compiled, window.status.get())
                started, release = threading.Event(), threading.Event()

                def delayed_delivery(*args, **kwargs):
                    started.set()
                    if not release.wait(3):
                        raise RuntimeError("Test did not release the file worker")
                    return actual_delivery(*args, **kwargs)

                with patch("printing.dialog.deliver", side_effect=delayed_delivery):
                    window.send()
                    try:
                        self.assertTrue(started.wait(3), "file worker did not start")
                        window.variables["copies"].set("2")
                        new_target = self.workspace / "other" / "cut-job-user-selected.plt"
                        if change_target:
                            window.variables["file"].set(str(new_target))
                        # A user edit invalidates the next preview while the
                        # already-reviewed delivery retains its own snapshot.
                        self.assertIsNone(window.review_destination)
                    finally:
                        release.set()
                    self.wait_for_job(window)
                self.assertEqual(output.read_bytes(), b"reviewed-snapshot")
                current_target = Path(window.variables["file"].get())
                if change_target:
                    self.assertEqual(current_target, new_target)
                    self.assertFalse(new_target.exists())
                else:
                    self.assertNotEqual(current_target, output)
                    self.assertEqual(current_target.parent, output.parent)
                    self.assertFalse(current_target.exists())
                self.assertIsNone(window.compiled)
                self.assertEqual(str(window.send_button["state"]), "disabled")
                window.close()


if __name__ == "__main__":
    unittest.main()
