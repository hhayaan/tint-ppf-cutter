"""Persistent device preferences stay separate from source geometry and jobs."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from printing.settings import (SettingsError, default_settings_path, load_settings,
                               profile_settings, save_profile_settings)


class CutSettingsTests(unittest.TestCase):
    def test_missing_settings_do_not_create_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = default_settings_path(Path(directory))
            self.assertEqual(path, Path(directory) / ".runtime" / "cut-settings.json")
            self.assertEqual(load_settings(path)["profiles"], {})
            self.assertFalse(path.parent.exists())

    def test_per_profile_setup_tools_and_last_model_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = default_settings_path(Path(directory))
            save_profile_settings(path, "graphtec:53", setup={
                "runtime_directory": "private-runtime",
                "connection": {"type": "tcp", "host": "explicit.example", "port": 12345, "timeout": 5},
                "media": {"mode": "roll", "width_mm": 1372, "height_mm": None}},
                tools={"force": 20, "speed": None, "acceleration": 1.5, "rotate_axes": True, "return_origin": True, "tool": 1})
            save_profile_settings(path, "roland:0", setup={"connection": {"type": "windows_raw", "queue_name": "Named queue"}})
            loaded = load_settings(path)
            self.assertEqual(loaded["last_profile"], "roland:0")
            graphtec = profile_settings(loaded, "graphtec:53")
            self.assertEqual(graphtec["setup"]["media"], {"mode": "roll", "width_mm": 1372, "height_mm": None})
            self.assertEqual(graphtec["tools"]["force"], 20)
            self.assertIsNone(graphtec["tools"]["speed"])
            self.assertEqual(profile_settings(loaded, "roland:0")["tools"], {})

    def test_switching_transport_replaces_old_details_without_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_profile_settings(path, "graphtec:53", setup={"connection": {"type": "tcp", "host": "explicit.example", "port": 12345}}, tools={"force": 20})
            loaded = save_profile_settings(path, "graphtec:53", setup={"connection": {"type": "file", "file_directory": "exports"}}, tools={"force": None})
            entry = profile_settings(loaded, "graphtec:53")
            self.assertEqual(entry["setup"]["connection"], {"type": "file", "file_directory": "exports"})
            self.assertIsNone(entry["tools"]["force"])

    def test_job_specific_and_unknown_fields_are_rejected_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_profile_settings(path, "graphtec:53", tools={"tool": 1})
            original = path.read_bytes()
            for scope, field in (("setup", "source"), ("setup", "scale"), ("setup", "selected_path_indices"), ("setup", "copies"), ("tools", "panel_page"), ("tools", "feed")):
                with self.subTest(scope=scope, field=field), self.assertRaises(SettingsError):
                    save_profile_settings(path, "graphtec:53", **{scope: {field: 1}})
                self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(SettingsError):
                save_profile_settings(path, "graphtec:53", setup={"connection": {"type": "file", "host": "stale.example"}})
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_or_duplicate_json_is_visible_and_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            for raw in (b'{broken', b'{"schema":"x","schema":"y"}', b'\xff'):
                path.write_bytes(raw)
                with self.subTest(raw=raw), self.assertRaises(SettingsError):
                    load_settings(path)
                with self.assertRaises(SettingsError):
                    save_profile_settings(path, "graphtec:53", tools={"tool": 1})
                self.assertEqual(path.read_bytes(), raw)

    def test_atomic_replace_failure_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_profile_settings(path, "graphtec:53", tools={"tool": 1})
            original = path.read_bytes()
            with patch("printing.settings.os.replace", side_effect=OSError("injected replacement failure")):
                with self.assertRaisesRegex(SettingsError, "Cannot save"):
                    save_profile_settings(path, "graphtec:53", tools={"force": 20})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_numeric_types_ranges_and_nonfinite_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            cases = [
                {"setup": {"connection": {"type": "tcp", "port": True}}},
                {"setup": {"connection": {"type": "tcp", "port": 65536}}},
                {"setup": {"media": {"width_mm": float("nan")}}},
                {"setup": {"media": {"height_mm": 0}}},
                {"tools": {"speed": 1.5}},
                {"tools": {"force": "20"}},
                {"tools": {"rotate_axes": 1}},
                {"tools": {"acceleration": float("inf")}},
            ]
            for kwargs in cases:
                with self.subTest(kwargs=kwargs), self.assertRaises(SettingsError):
                    save_profile_settings(path, "graphtec:53", **kwargs)
            self.assertFalse(path.exists())

    def test_returned_preferences_are_defensive_copies_and_preset_keeps_last_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_profile_settings(path, "graphtec:53", tools={"tool": 1})
            loaded = save_profile_settings(path, "roland:0", tools={"force": 10}, set_last=False)
            self.assertEqual(loaded["last_profile"], "graphtec:53")
            entry = profile_settings(loaded, "roland:0")
            entry["tools"]["force"] = 999
            self.assertEqual(profile_settings(loaded, "roland:0")["tools"]["force"], 10)
            self.assertEqual(profile_settings(loaded, "graphtec:1"), {"setup": {}, "tools": {}})

    def test_serial_and_usb_preferences_preserve_explicit_details_as_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            serial = {"type": "windows_serial", "port": "COM7", "baud": 9600, "parity": "E",
                      "data_bits": 7, "stop_bits": 1, "flow_control": "rtscts", "timeout": 5}
            save_profile_settings(path, "graphtec:53", setup={"connection": serial})
            self.assertEqual(profile_settings(load_settings(path), "graphtec:53")["setup"]["connection"], serial)
            usb = {"type": "windows_usb", "device_path": r"\\?\usb#vid_1234&pid_abcd#chosen#{a5dcbf10-6530-11d2-901f-00c04fb951ed}",
                   "vid": 0x1234, "pid": 0xabcd, "pipe_number": None, "timeout": 5}
            save_profile_settings(path, "graphtec:53", setup={"connection": usb})
            self.assertEqual(profile_settings(load_settings(path), "graphtec:53")["setup"]["connection"], usb)
            for bits, stops in ((8, 1.5), (5, 2)):
                with self.subTest(bits=bits, stops=stops), self.assertRaises(SettingsError):
                    save_profile_settings(path, "graphtec:53", setup={"connection": {**serial, "data_bits": bits, "stop_bits": stops}})


if __name__ == "__main__":
    unittest.main()
