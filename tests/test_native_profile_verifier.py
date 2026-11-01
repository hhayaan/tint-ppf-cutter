import hashlib
from pathlib import Path
import tempfile
import unittest

from tools.printing.verify_native_profiles import request_for, validate_receipt


class NativeProfileVerifierTests(unittest.TestCase):
    def fixture(self, payload):
        request = {"model_id": "test_model", "width_mm": 20.0, "height_mm": 10.0,
                   "family": "HPGL",
                   "paths": [[[0, 0], [20, 0], [20, 10], [0, 10], [0, 0]]]}
        receipt = {"schema": "tint-ppf-native-receipt.v1", "model_id": "test_model",
                   "bytes": payload.stat().st_size,
                   "sha256": hashlib.sha256(payload.read_bytes()).hexdigest(),
                   "path_count": 1, "point_count": 5, "write_calls": 4,
                   "native_units_per_mm_x": 40, "native_units_per_mm_y": 40,
                   "native_bounds_mm": [0, 0, 20, 10], "hardware_opened": False,
                   "native_abi_family": "HPGL", "coordinate_mode": "native-source-points",
                   "physical_completion_verified": False}
        return request, receipt

    def test_capture_checks_payload_and_factory_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "commands.bin"
            payload.write_bytes(b"IN;PU;")
            request, receipt = self.fixture(payload)
            self.assertTrue(validate_receipt(request, receipt, payload)["dimensions_match"])
            receipt["model_id"] = "other_factory_model"
            with self.assertRaises(ValueError):
                validate_receipt(request, receipt, payload)

    def test_changed_bytes_and_hardware_claims_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "commands.bin"
            payload.write_bytes(b"IN;PU;")
            request, receipt = self.fixture(payload)
            payload.write_bytes(b"IN;PD;")
            with self.assertRaises(ValueError):
                validate_receipt(request, receipt, payload)
            request, receipt = self.fixture(payload)
            receipt["hardware_opened"] = True
            with self.assertRaises(ValueError):
                validate_receipt(request, receipt, payload)
            receipt["hardware_opened"] = False
            receipt["physical_completion_verified"] = True
            with self.assertRaises(ValueError):
                validate_receipt(request, receipt, payload)

    def test_axis_swap_is_recorded_as_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "commands.bin"
            payload.write_bytes(b"IN;PU;")
            request, receipt = self.fixture(payload)
            receipt["native_bounds_mm"] = [190, 0, 200, 20]
            result = validate_receipt(request, receipt, payload)
            self.assertFalse(result["dimensions_match"])
            self.assertEqual(result["captured_rectangle_mm"], [10, 20])

    def test_family_rotation_contract_is_explicit(self):
        driver = {"module_filename": "example.dll", "artifact_sha256": "a" * 64,
                  "protocol_families": ["HPGL"]}
        profile = {"key": "example:0", "model_id": "test_model", "static_record_index": 0}
        hpgl = request_for(driver, profile, Path.cwd(), None)
        self.assertTrue(hpgl["options"]["rotate_axes"])
        driver["protocol_families"] = ["DMPL"]
        dmpl = request_for(driver, profile, Path.cwd(), None)
        self.assertFalse(dmpl["options"]["rotate_axes"])

    def test_offset_origin_and_family_mismatch_are_not_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "commands.bin"
            payload.write_bytes(b"IN;PU;")
            request, receipt = self.fixture(payload)
            receipt["native_bounds_mm"] = [100, 0, 120, 10]
            self.assertFalse(validate_receipt(request, receipt, payload)["dimensions_match"])
            receipt["native_abi_family"] = "DMPL"
            with self.assertRaises(ValueError):
                validate_receipt(request, receipt, payload)


if __name__ == "__main__":
    unittest.main()
