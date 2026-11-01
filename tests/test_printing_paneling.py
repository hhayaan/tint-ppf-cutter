from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from printing.geometry import JobOptions, prepare_job
from printing.paneling import PanelingError, PanelOptions, split_panels


def job(*paths, **options):
    vectors = tuple(SimpleNamespace(points=tuple((x, y, 0 if i == 0 else 4, 0)
                    for i, (x, y) in enumerate(points)), group=index+10)
                    for index, points in enumerate(paths))
    pattern = SimpleNamespace(paths=vectors, complete_geometry=True, sha256="a"*64)
    return prepare_job(pattern, JobOptions(1, inner_first=False, **options))


class PrintingPanelingTests(unittest.TestCase):
    def test_positioned_input_is_rejected_instead_of_silently_renormalized(self):
        source=job(((0,0),(15,0)),position_mode="manual",offset_x_mm=2,offset_y_mm=3)
        with self.assertRaisesRegex(PanelingError,"zero position offsets"):
            split_panels(source,PanelOptions(10,5))
        # Valid saved manual settings do not move an origin-positioned job.
        origin=job(((0,0),(15,0)),position_mode="origin",offset_x_mm=2,offset_y_mm=3)
        self.assertEqual(len(split_panels(origin,PanelOptions(10,5))),2)

    def test_contained_path_is_exact_and_page_size_is_distinct_from_artwork(self):
        source = job(((0, 0), (4, 0), (4, 3), (0, 0)))
        panel, = split_panels(source, PanelOptions(10, 8))
        self.assertEqual(panel.job.polylines, source.polylines)
        self.assertEqual((panel.job.width_mm, panel.job.height_mm), (4, 3))
        self.assertEqual((panel.page_width_mm, panel.page_height_mm), (10, 8))
        self.assertEqual(panel.origin_mm, (0, 0))
        self.assertEqual((panel.grid_rows, panel.grid_columns, panel.page_count), (1, 1, 1))
        self.assertEqual(panel.job.source_sha256, source.source_sha256)
        self.assertIsNone(source.options.media_width_mm)
        self.assertEqual(panel.preview_data(), split_panels(source, PanelOptions(10, 8))[0].preview_data())
        json.dumps(panel.preview_data(), allow_nan=False)

    def test_long_segment_covers_every_page_and_has_no_zero_length_fragments(self):
        source = job(((0, 0), (25, 0)))
        panels = split_panels(source, PanelOptions(10, 5))
        self.assertEqual([page.origin_mm for page in panels], [(0, 0), (10, 0), (20, 0)])
        self.assertEqual([page.job.polylines[0].points_mm for page in panels],
                         [((0, 0), (10, 0)), ((0, 0), (10, 0)), ((0, 0), (5, 0))])
        self.assertEqual(sum(page.job.total_length_mm for page in panels), source.total_length_mm)
        self.assertTrue(all(not line.closed for page in panels for line in page.job.polylines))

    def test_overlap_repeats_only_shared_geometry_and_covers_last_partial_page(self):
        panels = split_panels(job(((0, 0), (25, 0))), PanelOptions(10, 5, overlap_mm=2))
        self.assertEqual([p.origin_mm[0] for p in panels], [0, 8, 16])
        self.assertEqual([p.job.width_mm for p in panels], [10, 10, 9])
        self.assertEqual(sum(p.job.total_length_mm for p in panels), 29)

    def test_partly_clipped_closed_contour_stays_open_without_artificial_edge(self):
        panels = split_panels(job(((0, 0), (20, 0), (20, 5), (0, 5), (0, 0))), PanelOptions(10, 10))
        first, second = [page.job.polylines for page in panels]
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].points_mm, ((10, 5), (0, 5), (0, 0), (10, 0)))
        self.assertFalse(first[0].closed)
        self.assertFalse(second[0].closed)
        self.assertEqual(sum(p.job.total_length_mm for p in panels), 50)

    def test_outside_excursion_keeps_tool_up_discontinuity_even_if_intersections_coincide(self):
        source = job(((0, 0), (5, 5), (15, 5), (5, 5), (0, 10)))
        first = split_panels(source, PanelOptions(10, 10))[0]
        self.assertEqual(len(first.job.polylines), 2)
        a, b = first.job.polylines
        self.assertEqual(a.points_mm[-1], b.points_mm[0])
        self.assertEqual((a.group, b.group), (10, 10))
        self.assertEqual((a.path_index, a.subpath_index), (0, 0))

    def test_corner_only_touches_are_explicit_blank_pages(self):
        panels = split_panels(job(((0, 2), (2, 0))), PanelOptions(1, 1))
        self.assertEqual(len(panels), 4)
        self.assertEqual([p.index for p in panels if p.is_empty], [0, 3])
        for page in (panels[0], panels[3]):
            self.assertEqual(page.job.point_count, 0)
            self.assertEqual(page.job.total_length_mm, 0)
            self.assertEqual(page.job.bounds_mm, (0, 0, 0, 0))
        self.assertTrue(all(len(line.points_mm) >= 2 and line.length_mm > 0
                            for p in panels for line in p.job.polylines))

    def test_page_ids_are_stable_under_traversal_and_selection(self):
        source = job(((0, 0), (20, 20)))
        self.assertEqual([p.index for p in split_panels(source, PanelOptions(10, 10, order="column-major"))], [0, 2, 1, 3])
        selected = split_panels(source, PanelOptions(10, 10, selected_pages=(3, 0)))
        self.assertEqual([p.index for p in selected], [0, 3])
        self.assertTrue(all(p.page_count == 4 for p in selected))
        for value in ((), (-1,), (4,), (0, 0), (True,), "0", iter((0,))):
            with self.subTest(value=value), self.assertRaises(PanelingError):
                split_panels(source, PanelOptions(10, 10, selected_pages=value))

    def test_all_source_metadata_and_order_survive_clipping(self):
        source = job(((0, 0), (20, 1)), ((0, 2), (20, 3)), copies=2, copy_columns=1,
                     copy_spacing_mm=1, passes=2, weed_border_mm=1)
        panels = split_panels(source, PanelOptions(10, 20))
        source_keys = {(p.path_index, p.subpath_index, p.group, p.copy_index, p.pass_index, p.role)
                       for p in source.polylines}
        output_keys = {(p.path_index, p.subpath_index, p.group, p.copy_index, p.pass_index, p.role)
                       for page in panels for p in page.job.polylines}
        self.assertEqual(source_keys, output_keys)
        self.assertEqual(panels[0].job.selected_path_indices, source.selected_path_indices)
        self.assertEqual((panels[0].job.pass_count, panels[0].job.copy_count), (2, 2))
        self.assertEqual([p.pass_index for p in panels[0].job.polylines],
                         sorted(p.pass_index for p in panels[0].job.polylines))

    def test_repeated_contained_knots_are_preserved_exactly(self):
        source = job(((0, 0), (1, 0), (2, 0)))
        line = source.polylines[0]
        repeated = replace(line, points_mm=((0, 0), (1, 0), (1, 0), (2, 0)))
        source = replace(source, polylines=(repeated,), point_count=4)
        page, = split_panels(source, PanelOptions(10, 10))
        self.assertEqual(page.job.polylines[0].points_mm, repeated.points_mm)

    def test_existing_media_does_not_change_original_or_new_page_size(self):
        source = job(((0, 0), (10, 0)), media_width_mm=30, media_height_mm=20)
        page = split_panels(source, PanelOptions(5, 4))[0]
        self.assertEqual((page.job.options.media_width_mm, page.job.options.media_height_mm), (5, 4))
        self.assertEqual((source.options.media_width_mm, source.options.media_height_mm), (30, 20))

    def test_vertical_lines_and_nonzero_artwork_origins_remain_valid(self):
        source = job(((0, 0), (0, 25)))
        line = replace(source.polylines[0], points_mm=((5, 7), (5, 32)))
        source = replace(source, polylines=(line,), bounds_mm=(5, 7, 5, 32), polyline_bounds_mm=(5, 7, 5, 32))
        panels = split_panels(source, PanelOptions(5, 10))
        self.assertEqual([p.origin_mm for p in panels], [(5, 7), (5, 17), (5, 27)])
        self.assertEqual([p.job.height_mm for p in panels], [10, 10, 5])

    def test_invalid_options_and_page_count_raise_instead_of_partial_plan(self):
        source = job(((0, 0), (20, 20)))
        for kwargs in ({"width_mm":0}, {"width_mm":True}, {"height_mm":float("inf")},
                       {"overlap_mm":float("nan")}, {"overlap_mm":-1}, {"overlap_mm":10},
                       {"max_pages":0}, {"max_pages":True}, {"order":"unknown"}, {"max_pages":3}):
            values = dict(width_mm=10, height_mm=10)
            values.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(PanelingError):
                split_panels(source, PanelOptions(**values))
        with self.assertRaises(PanelingError):
            split_panels(source, PanelOptions(1e-300, 1e-300))

    def test_invalid_geometry_is_not_silently_dropped(self):
        source = job(((0, 0), (2, 2)))
        line = source.polylines[0]
        invalid_jobs = (replace(source, polylines=()), replace(source, width_mm=float("nan")),
                        replace(source, bounds_mm=(0, 0, 1, 1)),
                        replace(source, polylines=(replace(line, points_mm=((0, 0), (float("nan"), 1))),)),
                        replace(source, polylines=(replace(line, closed=True),)),
                        replace(source, polylines=(replace(line, points_mm=((0, 0), (0, 0))),)))
        for invalid in invalid_jobs:
            with self.subTest(invalid=invalid), self.assertRaises(PanelingError):
                split_panels(invalid, PanelOptions(10, 10))

    def test_resource_budgets_fail_complete_request(self):
        source = job(((0, 0), (25, 0)))
        with patch("printing.paneling._MAX_OUTPUT_POINTS", 3), self.assertRaisesRegex(PanelingError, "output geometry"):
            split_panels(source, PanelOptions(10, 5))
        with patch("printing.paneling._MAX_WORK", 1), self.assertRaisesRegex(PanelingError, "work budget"):
            split_panels(source, PanelOptions(10, 5))


if __name__ == "__main__":
    unittest.main()
