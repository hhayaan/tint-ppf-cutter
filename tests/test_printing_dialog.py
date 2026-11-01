"""Headless validation of cut-dialog inputs and explicit transport routing."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from printing.dialog import (Destination, calibration_factor, command_preview, deliver, destination_from_values,
                             geometry_kwargs, number, owner_profile_label, preparation_kwargs, profile_labels, synthetic_rectangle, validate_device_bounds)


class DialogInputTests(unittest.TestCase):
    def values(self):
        return dict(unit_scale="", scale_percent="100", tolerance="0.05", inner_first=True,
                    order="unsorted", rotation="0", mirror_x=False, mirror_y=False, copies="1",
                    columns="1", spacing="0", weed="", passes="1", media_width="", media_height="")

    def test_pattern_scale_cannot_be_assumed_but_synthetic_dimensions_are_mm(self):
        with self.assertRaisesRegex(ValueError, "source scale"):
            geometry_kwargs(self.values(), (0,), False)
        self.assertEqual(geometry_kwargs(self.values(), (0,), True)["mm_per_source_unit"], 1)
        values = self.values()
        values["unit_scale"] = "0.25"
        options = geometry_kwargs(values, (1, 3), False)
        self.assertEqual(options["selected_path_indices"], (1, 3))
        self.assertEqual(options["mm_per_source_unit"], .25)
        with self.assertRaises(ValueError):
            geometry_kwargs(values, (), False)

    def test_calibration_and_nonfinite_dimensions_are_rejected(self):
        self.assertEqual(calibration_factor("200", 800), .25)
        for value in ("nan", "inf", "-1", "0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                number(value, "width")
        with self.assertRaises(ValueError):
            calibration_factor("200", 0)

    def test_synthetic_twenty_mm_rectangle_is_closed_and_independent(self):
        pattern = synthetic_rectangle(20, 20)
        self.assertTrue(pattern.complete_geometry)
        self.assertEqual(pattern.paths[0].points[0][:2], pattern.paths[0].points[-1][:2])
        self.assertEqual(max(point[0] for point in pattern.paths[0].points), 20)
        self.assertEqual(max(point[1] for point in pattern.paths[0].points), 20)

    def test_fractional_copy_count_rejected(self):
        values = self.values()
        values["copies"] = "1.5"
        with self.assertRaisesRegex(ValueError, "whole number"):
            geometry_kwargs(values, (0,), True)

    def test_manual_paneling_defers_media_fit_and_selects_exactly_one_page(self):
        values = self.values()
        values.update(paneling=True, panel_page="3", panel_overlap="2", media_width="100", media_height="80")
        geometry, panels, review = preparation_kwargs(values, (0, 2), True)
        self.assertIsNone(geometry["media_width_mm"])
        self.assertIsNone(geometry["media_height_mm"])
        self.assertEqual(panels, {"width_mm": 100, "height_mm": 80, "overlap_mm": 2, "selected_pages": (2,), "order": "row-major"})
        self.assertEqual((review["media_width_mm"], review["media_height_mm"]), (100, 80))

    def test_manual_paneling_requires_explicit_page_dimensions(self):
        values = self.values()
        values.update(paneling=True, panel_page="1", panel_overlap="0")
        with self.assertRaisesRegex(ValueError, "explicit usable media"):
            preparation_kwargs(values, (0,), True)

    def test_manual_panel_selection_prepares_only_requested_clipped_page(self):
        from printing.geometry import JobOptions, prepare_job
        from printing.paneling import PanelOptions, split_panels

        values = self.values()
        values.update(paneling=True, panel_page="3", panel_overlap="5", media_width="50", media_height="30")
        geometry, panels, review = preparation_kwargs(values, (0,), True)
        whole = prepare_job(synthetic_rectangle(120, 20), JobOptions(**geometry))
        pages = split_panels(whole, PanelOptions(**panels))
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertEqual((page.index, page.page_count, page.origin_mm), (2, 3, (90, 0)))
        self.assertEqual((page.job.width_mm, page.job.height_mm), (30, 20))
        self.assertEqual((page.job.options.media_width_mm, page.job.options.media_height_mm), (50, 30))
        self.assertEqual(whole.width_mm, 120)
        self.assertEqual((review["media_width_mm"], review["media_height_mm"]), (50, 30))

    def test_tcp_port_is_explicit_and_serial_framing_validated(self):
        with self.assertRaises(ValueError):
            destination_from_values("TCP", dict(host="127.0.0.1", port="", timeout="10"))
        destination = destination_from_values("TCP", dict(host="127.0.0.1", port="12345", timeout="2"))
        self.assertEqual(destination.parameters, {"host": "127.0.0.1", "port": 12345, "timeout": 2})
        with self.assertRaises(ValueError):
            destination_from_values("Serial COM", dict(com="COM3", baud="9600", parity="N", stop_bits="1.5", data_bits="8", flow="none", timeout="1"))

    def test_usb_requires_selected_matching_identity_and_no_default_pipe(self):
        path = r"\\?\usb#vid_1234&pid_abcd#selected#{a5dcbf10-6530-11d2-901f-00c04fb951ed}"
        values = dict(usb_path=path, usb_vid="", usb_pid="", usb_pipe="", timeout="2")
        with self.assertRaises(ValueError):
            destination_from_values("Direct USB", values)
        values.update(usb_vid="1234", usb_pid="ABCD")
        destination = destination_from_values("Direct USB", values)
        self.assertEqual(destination.parameters["pipe_number"], None)
        self.assertEqual((destination.parameters["vid"], destination.parameters["pid"]), (0x1234, 0xabcd))

    def test_profile_labels_keep_duplicate_names_unambiguous(self):
        labels = profile_labels([{"name": "Same model", "key": "driver:1"}, {"name": "Same model", "key": "driver:2"}])
        self.assertEqual(set(labels.values()), {"driver:1", "driver:2"})
        self.assertEqual(len(labels), 2)

    def test_capture_label_does_not_claim_hardware_verification(self):
        labels = profile_labels([{"name": "Example", "key": "driver:1"}], {"driver:1": {"status": "verified_capture"}})
        self.assertIn("capture verified", next(iter(labels)))
        self.assertNotIn("hardware", next(iter(labels)))

    def test_owner_confirmed_model_uses_exact_profile_key(self):
        labels = {"Other Graphtec": "graphtec:54", "FC9000-140 · capture verified": "graphtec:53"}
        self.assertEqual(owner_profile_label(labels), "FC9000-140 · capture verified")
        self.assertEqual(owner_profile_label({"Other Graphtec": "graphtec:54"}), "")

    def test_native_emitted_bounds_gate_hardware_not_just_geometry_size(self):
        destination = Destination("tcp", {"host": "explicit.invalid", "port": 12345}, "test")
        with self.assertRaisesRegex(ValueError, "actual usable media"):
            validate_device_bounds(destination, {}, {"native_bounds_mm": [0, 0, 20, 20]})
        media = {"media_width_mm": 30, "media_height_mm": 30}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_device_bounds(destination, media, {})
        with self.assertRaisesRegex(ValueError, "exceed"):
            validate_device_bounds(destination, media, {"native_bounds_mm": [1352, 0, 1372, 20]})
        validate_device_bounds(destination, media, {"native_bounds_mm": [0, 0, 20, 20]})
        validate_device_bounds(Destination("file", {}, "test"), {}, {})

    def test_command_preview_escapes_controls_without_mutating_payload(self):
        payload = b"IN;\x1b\x00\r\n\xff"
        self.assertEqual(command_preview(payload), "IN;\\x1b\\x00\\r\n\\xff")
        self.assertEqual(payload, b"IN;\x1b\x00\r\n\xff")

    def test_file_destination_does_not_overwrite_and_delivery_has_no_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.plt"
            path.write_bytes(b"keep")
            with self.assertRaises(ValueError):
                destination_from_values("File", {"file": str(path)})
            with patch("printing.dialog.transports.send_tcp", side_effect=RuntimeError("offline failure")) as tcp, \
                 patch("printing.dialog.transports.write_job_file") as file_output:
                with self.assertRaises(RuntimeError):
                    deliver(b"job", Destination("tcp", {"host": "explicit.invalid", "port": 12345}, "test"),
                            log_path=Path(directory) / "logs.jsonl", cancel_event=threading.Event())
                tcp.assert_called_once()
                file_output.assert_not_called()
            self.assertEqual(path.read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
