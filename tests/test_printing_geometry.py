from dataclasses import replace
import json
import math
from types import SimpleNamespace
import unittest

from printing.geometry import GeometryError, JobOptions, prepare_job, source_bounds


def path(points, group=0):
    return SimpleNamespace(points=tuple(points), group=group, record_offset=10)


def pattern(*paths, complete=True):
    return SimpleNamespace(paths=tuple(paths), complete_geometry=complete, sha256=None)


def polygon(points, group=0):
    return path([(x, y, 0 if index == 0 else 4, 0) for index, (x, y) in enumerate(points)], group)


def square(x=0, y=0, size=10, group=0):
    return polygon(((x,y),(x+size,y),(x+size,y+size),(x,y+size),(x,y)), group)


def segment_distance(point, start, end):
    dx, dy = end[0]-start[0], end[1]-start[1]
    factor = max(0,min(1,((point[0]-start[0])*dx+(point[1]-start[1])*dy)/(dx*dx+dy*dy))) if dx or dy else 0
    return math.hypot(point[0]-start[0]-factor*dx,point[1]-start[1]-factor*dy)


class PrintingGeometryTests(unittest.TestCase):
    def test_explicit_calibration_and_complete_geometry_are_required(self):
        with self.assertRaises(TypeError):
            JobOptions()
        for value in (0,-1,float('nan'),float('inf'),True):
            with self.subTest(value=value), self.assertRaises(GeometryError):
                prepare_job(pattern(square()),JobOptions(value))
        with self.assertRaisesRegex(GeometryError,'Complete geometry'):
            prepare_job(pattern(square(),complete=False),JobOptions(1))

    def test_subpaths_closure_and_opaque_flags_are_preserved_without_inference(self):
        source=pattern(path(((0,0,0,65535),(2,0,4,65535),(2,2,4,65535),
                             (5,5,0,0),(6,5,4,0),(5,5,4,0)),group=7))
        job=prepare_job(source,JobOptions(1,inner_first=False))
        self.assertEqual(len(job.polylines),2)
        self.assertFalse(job.polylines[0].closed)
        self.assertTrue(job.polylines[1].closed)
        self.assertEqual([(line.path_index,line.subpath_index,line.group) for line in job.polylines],[(0,0,7),(0,1,7)])
        self.assertEqual(job.polylines[0].points,job.polylines[0].points_mm)

    def test_cubic_flattening_has_bounded_physical_error_after_scale(self):
        source=pattern(path(((0,0,0,0),(0,10,1,0),(10,10,2,0),(10,0,3,0))))
        job=prepare_job(source,JobOptions(2,scale=3,flatten_tolerance_mm=.025))
        points=job.polylines[0].points_mm
        self.assertGreater(len(points),8)
        self.assertAlmostEqual(job.width_mm,60)
        self.assertAlmostEqual(job.height_mm,45)
        for index in range(501):
            t=index/500
            actual=(6*(30*t*t-20*t*t*t),6*(30*t-30*t*t))
            distance=min(segment_distance(actual,a,b) for a,b in zip(points,points[1:]))
            self.assertLessEqual(distance,.025000001)
        self.assertEqual(source_bounds(source),(0.0,0.0,10.0,7.5))

    def test_collinear_cubic_overshoot_is_not_lost(self):
        source=pattern(path(((0,0,0,0),(100,0,1,0),(100,0,2,0),(1,0,3,0))))
        job=prepare_job(source,JobOptions(1,flatten_tolerance_mm=.001))
        self.assertGreater(job.width_mm,75)
        self.assertGreater(max(x for x,y in job.polylines[0].points_mm),75)

    def test_closed_cubic_loop_preserves_endpoint_closure(self):
        source=pattern(path(((0,0,0,0),(10,20,1,0),(-10,20,2,0),(0,0,3,0))))
        job=prepare_job(source,JobOptions(1,flatten_tolerance_mm=.01))
        line=job.polylines[0]
        self.assertTrue(line.closed)
        self.assertEqual(line.points_mm[0],line.points_mm[-1])
        self.assertGreater(len(line.points_mm),8)

    def test_rotation_mirrors_scale_and_origin_are_deterministic(self):
        source=pattern(polygon(((5,10),(8,10),(8,12),(5,10))))
        job=prepare_job(source,JobOptions(2,scale=.5,rotation_degrees=90,mirror_x=True))
        self.assertEqual((job.width_mm,job.height_mm),(2.0,3.0))
        self.assertEqual(job.polylines[0].points_mm,((2.0,3.0),(2.0,0.0),(0.0,0.0),(2.0,3.0)))
        self.assertEqual(job.preview_data(),prepare_job(source,job.options).preview_data())
        json.dumps(job.preview_data(),allow_nan=False)

    def test_selecting_paths_preserves_all_subpaths_and_rejects_bad_selections(self):
        source=pattern(square(),square(20,0,2,group=4))
        job=prepare_job(source,JobOptions(1,selected_path_indices=(1,)))
        self.assertEqual(job.selected_path_indices,(1,))
        self.assertEqual(job.width_mm,2)
        self.assertEqual(job.polylines[0].group,4)
        for selected in ((),(2,),(-1,),(0,0),(True,)):
            with self.subTest(selected=selected), self.assertRaises(GeometryError):
                prepare_job(source,JobOptions(1,selected_path_indices=selected))

    def test_inner_first_uses_containment_not_only_bounding_box_area(self):
        source=pattern(square(0,0,20),square(5,5,10),square(7,7,2),square(30,0,1))
        job=prepare_job(source,JobOptions(1))
        self.assertEqual([line.path_index for line in job.polylines],[2,1,0,3])
        unsorted=prepare_job(source,JobOptions(1,inner_first=False))
        self.assertEqual([line.path_index for line in unsorted.polylines],[0,1,2,3])
        # A diagonal across the missing part of a concave U is not contained,
        # although both endpoints and its bounding box are inside outer bounds.
        outer=polygon(((0,0),(10,0),(10,10),(7,10),(7,3),(3,3),(3,10),(0,10),(0,0)))
        diagonal=polygon(((1,8),(9,8)))
        job=prepare_job(pattern(outer,diagonal),JobOptions(1))
        self.assertEqual([line.path_index for line in job.polylines],[0,1])

    def test_nearest_start_and_nearest_endpoint_have_explicit_directions(self):
        source=pattern(polygon(((20,0),(21,0))),polygon(((10,0),(0,0))))
        start=prepare_job(source,JobOptions(1,order='start-nearest',inner_first=False))
        self.assertEqual(start.polylines[0].path_index,1)
        self.assertEqual(start.polylines[0].points_mm[0],(10,0))
        endpoint=prepare_job(source,JobOptions(1,order='end-nearest',inner_first=False))
        self.assertEqual(endpoint.polylines[0].path_index,1)
        self.assertEqual(endpoint.polylines[0].points_mm[0],(0,0))

    def test_copy_grid_spacing_global_weed_border_and_passes(self):
        source=pattern(square())
        job=prepare_job(source,JobOptions(1,copies=3,copy_columns=2,copy_spacing_mm=2,weed_border_mm=1,passes=2))
        self.assertEqual((job.width_mm,job.height_mm),(24,24))
        self.assertEqual(job.copy_count,3)
        self.assertEqual(job.pass_count,2)
        self.assertEqual(job.polyline_count,8)
        self.assertEqual(job.point_count,40)
        self.assertEqual([line.role for line in job.polylines],['cut','cut','cut','weed']*2)
        self.assertEqual([line.pass_index for line in job.polylines],[0]*4+[1]*4)
        self.assertEqual(job.polylines[2].points_mm[0],(1,13))
        self.assertEqual(job.total_length_mm,2*(3*40+96))

    def test_media_limits_cover_curves_copies_and_weed_margin(self):
        source=pattern(square())
        prepare_job(source,JobOptions(1,media_width_mm=10,media_height_mm=10))
        for options in (JobOptions(1,media_width_mm=9.9),JobOptions(1,media_height_mm=9.9),
                        JobOptions(1,copies=2,copy_columns=2,media_width_mm=19),
                        JobOptions(1,weed_border_mm=1,media_width_mm=11)):
            with self.subTest(options=options), self.assertRaisesRegex(GeometryError,'exceeds media'):
                prepare_job(source,options)
        bulge=pattern(path(((0,0,0,0),(0,10,1,0),(10,5,2,0),(10,0,3,0))))
        # A loose flatten tolerance cannot disguise the exact source-curve size.
        with self.assertRaisesRegex(GeometryError,'exceeds media'):
            prepare_job(bulge,JobOptions(1,flatten_tolerance_mm=20,media_height_mm=5))

    def test_invalid_and_empty_geometry_is_rejected_without_partial_output(self):
        invalid=(path(()),path(((0,0,0,0),)),path(((0,0,4,0),(1,0,4,0))),
                 path(((0,0,0,0),(1,1,1,0),(2,2,3,0))),
                 path(((0,0,0,0),(float('nan'),1,4,0))),
                 path(((0,0,0,0),(0,0,4,0))),
                 path(((0,0,0,0),(1,0,4,0),(3,3,0,0))),path((None,)))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(GeometryError):
                prepare_job(pattern(square(),value),JobOptions(1))
        with self.assertRaises(GeometryError):
            prepare_job(pattern(),JobOptions(1))

    def test_resource_limits_fail_instead_of_weakening_tolerance(self):
        curve=pattern(path(((0,0,0,0),(0,100,1,0),(100,100,2,0),(100,0,3,0))))
        options=(JobOptions(1,max_points=4),JobOptions(1,max_subdivisions=1),JobOptions(1,max_subdivision_depth=1))
        for value in options:
            with self.subTest(value=value), self.assertRaisesRegex(GeometryError,'budget'):
                prepare_job(curve,value)
        with self.assertRaisesRegex(GeometryError,'polyline budget'):
            prepare_job(pattern(square()),JobOptions(1,copies=5,max_polylines=4))
        with self.assertRaisesRegex(GeometryError,'final point budget'):
            prepare_job(pattern(square()),JobOptions(1,copies=5,max_points=10))
        with self.assertRaisesRegex(GeometryError,'work budget'):
            prepare_job(pattern(square(),square(2,2,1)),JobOptions(1,max_ordering_work=1))

    def test_invalid_options_are_rejected(self):
        base=JobOptions(1)
        values={'scale':0,'flatten_tolerance_mm':0,'rotation_degrees':45,'order':'random',
                'copy_spacing_mm':-1,'copies':0,'copy_columns':0,'passes':0,
                'weed_border_mm':-1,'media_width_mm':float('inf'),'mirror_x':'yes'}
        for name,value in values.items():
            with self.subTest(name=name), self.assertRaises(GeometryError):
                prepare_job(pattern(square()),replace(base,**{name:value}))

    def test_nonfinite_combined_scale_and_huge_layout_fail_cleanly(self):
        for options in (JobOptions(1e300,scale=1e300),JobOptions(1e-300,scale=1e-300),
                        JobOptions(10**400),JobOptions(1,copies=10**400)):
            with self.subTest(options=options),self.assertRaises(GeometryError):
                prepare_job(pattern(square()),options)


if __name__=='__main__':
    unittest.main()
