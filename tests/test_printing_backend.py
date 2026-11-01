"""Portable compiler boundaries; native DLL execution is an explicit offline probe."""
from pathlib import Path
from types import SimpleNamespace
import hashlib
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from printing import backend
from printing.driver_catalog import get_profile, profiles_by_model_id, search_profiles
from printing.dialog import synthetic_rectangle
from printing.geometry import JobOptions, prepare_job


class NativeCompilerTests(unittest.TestCase):
    def setUp(self):
        self.job = prepare_job(synthetic_rectangle(20, 10), JobOptions(mm_per_source_unit=1))
        self.options = backend.ToolOptions(rotate_axes=True)

    def test_catalog_preserves_every_variant_and_ambiguous_source_ids(self):
        profiles = search_profiles()
        self.assertEqual(len(profiles), 344)
        self.assertEqual(len({p["key"] for p in profiles}), 344)
        self.assertEqual(len(profiles_by_model_id("anaexp", "fp_60")), 4)
        self.assertEqual(get_profile("graphtec:53")["name"], "Graphtec FC9000-140")

    def test_tool_fields_do_not_accept_nonfinite_fractional_or_unvalidated_feed(self):
        for options in (backend.ToolOptions(speed=1.5), backend.ToolOptions(force=-1),
                        backend.ToolOptions(tool=True), backend.ToolOptions(acceleration=float("nan")),
                        backend.ToolOptions(feed=True)):
            with self.subTest(options=options), self.assertRaises(ValueError):
                options.validate()

    def test_native_request_keeps_exact_variant_and_calibrated_coordinates(self):
        with patch.object(backend, "validate_runtime", return_value=Path("C:/private/runtime")):
            request = backend.native_request(self.job, "graphtec:53", self.options, Path("unused"))
        self.assertEqual(request["model_id"], "gr_fc_9600140")
        self.assertEqual(request["variant_index"], 53)
        self.assertEqual(request["paths"][0], [[0, 0], [20, 0], [20, 10], [0, 10], [0, 0]])
        self.assertEqual(request["width_mm"], 20)
        self.assertEqual(request["height_mm"], 10)

    def test_missing_or_changed_runtime_is_refused_before_vendor_execution(self):
        from printing.driver_catalog import get_driver
        driver = get_driver("graphtec")
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            with self.assertRaises(ValueError):
                backend.validate_runtime(runtime, driver)
            (runtime / "gsfwkcut62.dll").write_bytes(b"different build")
            with self.assertRaisesRegex(ValueError, "differs"):
                backend.validate_runtime(runtime, driver)

    def test_out_of_bounds_job_is_rejected_before_starting_a_helper(self):
        invalid = SimpleNamespace(width_mm=20, height_mm=10,
                                  polylines=[SimpleNamespace(points=((0, 0), (21, 10)))])
        with patch.object(backend, "validate_runtime", return_value=Path("unused")):
            with self.assertRaisesRegex(ValueError, "outside"):
                backend.native_request(invalid, "graphtec:53", self.options, Path("unused"))

    def test_model_width_limit_and_straight_contour_page(self):
        with patch.object(backend, "validate_runtime", return_value=Path("unused")):
            oversized = prepare_job(synthetic_rectangle(2000, 10), JobOptions(mm_per_source_unit=1))
            with self.assertRaisesRegex(ValueError, "1372"):
                backend.native_request(oversized, "graphtec:53", self.options, Path("unused"))
            line = SimpleNamespace(width_mm=20, height_mm=0,
                                   polylines=[SimpleNamespace(points=((0, 0), (20, 0)))])
            request = backend.native_request(line, "graphtec:53", self.options, Path("unused"))
            self.assertEqual(request["height_mm"], 1)
            self.assertEqual(request["paths"][0], [[0, 0], [20, 0]])

    def test_catalog_only_profile_and_unverified_axes_never_start_helper(self):
        with patch.object(backend, "native_request", return_value={"driver_sha256": "expected"}), \
                patch.object(backend, "_helper") as helper:
            with patch.object(backend, "capture_status", return_value={"status": "not_verified"}):
                with self.assertRaisesRegex(ValueError, "not yet verified"):
                    backend.compile_job(self.job, "graphtec:53", self.options, Path("unused"))
            status = {"status": "verified_capture", "driver_sha256": "expected", "rotate_axes": False}
            with patch.object(backend, "capture_status", return_value=status):
                with self.assertRaisesRegex(ValueError, "axis mode"):
                    backend.compile_job(self.job, "graphtec:53", self.options, Path("unused"))
            helper.assert_not_called()

    def test_tampered_native_receipt_is_not_delivered(self):
        request = {"driver_sha256": "expected", "width_mm": 20, "height_mm": 10}
        status = {"status": "verified_capture", "driver_sha256": "expected", "rotate_axes": True, "helper_source_sha256": backend.helper_source_sha256()}

        def simulate(command, **_kwargs):
            Path(command[2]).write_bytes(b"IN;PU;")
            Path(command[3]).write_text(json.dumps({"schema": "tint-ppf-native-receipt.v1",
                                                  "hardware_opened": False, "bytes": 6,
                                                  "sha256": "wrong digest"}))
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(backend, "ROOT", Path(temporary)), \
                patch.object(backend, "native_request", return_value=request), \
                patch.object(backend, "capture_status", return_value=status), \
                patch.object(backend, "helper_source_sha256", return_value=status["helper_source_sha256"]), \
                patch.object(backend, "_helper", return_value=Path("stub.exe")), \
                patch.object(backend.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(backend.subprocess, "run", side_effect=simulate):
            with self.assertRaisesRegex(RuntimeError, "receipt"):
                backend.compile_job(self.job, "graphtec:53", self.options, Path("unused"))

    def test_compile_timeout_reports_capture_without_claiming_device_output(self):
        status = {"status": "verified_capture", "driver_sha256": "expected", "rotate_axes": True, "helper_source_sha256": backend.helper_source_sha256()}
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(backend, "ROOT", Path(temporary)), \
                patch.object(backend, "native_request", return_value={"driver_sha256": "expected"}), \
                patch.object(backend, "capture_status", return_value=status), \
                patch.object(backend, "helper_source_sha256", return_value=status["helper_source_sha256"]), \
                patch.object(backend, "_helper", return_value=Path("stub.exe")), \
                patch.object(backend.subprocess, "CREATE_NO_WINDOW", 0, create=True), \
                patch.object(backend.subprocess, "run", side_effect=subprocess.TimeoutExpired("stub.exe", 90)):
            with self.assertRaisesRegex(RuntimeError, "no hardware transport"):
                backend.compile_job(self.job, "graphtec:53", self.options, Path("unused"))


if __name__ == "__main__":
    unittest.main()
