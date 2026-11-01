"""Deterministic manual panel planning in calibrated millimeters.

This clips tool paths, not filled polygons. Overlap deliberately repeats the
geometry in the shared area. Page advancement, alignment and delivery remain
operator actions; this module performs no I/O and does not move a plotter.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Sequence
import math
from numbers import Real

from printing.geometry import Bounds, GeometryError, Point, Polyline, PreparedJob

_MAX_PAGES = 100_000
_MAX_WORK = 5_000_000
_MAX_OUTPUT_POINTS = 2_000_000
_MAX_OUTPUT_LINES = 200_000


class PanelingError(GeometryError):
    """The entire requested panel plan cannot be represented safely."""


@dataclass(frozen=True)
class PanelOptions:
    width_mm: float
    height_mm: float
    overlap_mm: float = 0.0
    selected_pages: tuple[int, ...] | None = None
    order: str = "row-major"
    max_pages: int = 10_000


@dataclass(frozen=True)
class Panel:
    index: int
    row: int
    column: int
    origin_mm: Point
    job: PreparedJob
    page_count: int
    grid_rows: int
    grid_columns: int

    @property
    def is_empty(self) -> bool:
        return not self.job.polylines

    @property
    def page_width_mm(self) -> float:
        return self.job.options.media_width_mm

    @property
    def page_height_mm(self) -> float:
        return self.job.options.media_height_mm

    def preview_data(self) -> dict:
        return {"index": self.index, "row": self.row, "column": self.column,
                "origin_mm": list(self.origin_mm), "page_count": self.page_count,
                "grid_rows": self.grid_rows, "grid_columns": self.grid_columns,
                "page_width_mm": self.page_width_mm, "page_height_mm": self.page_height_mm,
                "is_empty": self.is_empty, "job": self.job.preview_data()}


def _number(value, name: str, *, positive=False) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PanelingError(f"{name} must be a finite number")
    try:
        value = float(value)
    except (OverflowError, ValueError) as error:
        raise PanelingError(f"{name} must be a finite number") from error
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise PanelingError(f"{name} must be {'positive' if positive else 'nonnegative'} and finite")
    return value


def _inside(point: Point, bounds: Bounds) -> bool:
    return bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]


def _bounds(points) -> Bounds:
    iterator = iter(points)
    try:
        x0, y0 = next(iterator)
    except StopIteration:
        return (0.0, 0.0, 0.0, 0.0)
    x1, y1 = x0, y0
    for x, y in iterator:
        x0, y0, x1, y1 = min(x0, x), min(y0, y), max(x1, x), max(y1, y)
    return x0, y0, x1, y1


class _Budget:
    def __init__(self):
        self.work = self.points = self.lines = 0

    def step(self, count=1):
        self.work += count
        if self.work > _MAX_WORK:
            raise PanelingError("Panel clipping work budget exceeded")

    def output(self, count):
        self.points += count
        self.lines += 1
        if self.points > _MAX_OUTPUT_POINTS or self.lines > _MAX_OUTPUT_LINES:
            raise PanelingError("Panel output geometry budget exceeded")


def _clip_segment(a: Point, b: Point, box: Bounds):
    """Liang-Barsky interval; point-only boundary contacts are not cut paths."""
    dx, dy = b[0]-a[0], b[1]-a[1]
    lower, upper = 0.0, 1.0
    for p, q in ((-dx, a[0]-box[0]), (dx, box[2]-a[0]),
                 (-dy, a[1]-box[1]), (dy, box[3]-a[1])):
        if p == 0:
            if q < 0:
                return None
            continue
        ratio = q/p
        if p < 0:
            lower = max(lower, ratio)
        else:
            upper = min(upper, ratio)
        if lower > upper:
            return None
    if lower == upper or a == b:
        return None
    # Keep original endpoints whenever unclipped, avoiding numerical drift in
    # paths which meet exactly at a source knot. Clamp computed intersections.
    def at(t):
        if t == 0:
            return a
        if t == 1:
            return b
        return (min(box[2], max(box[0], a[0]+t*dx)),
                min(box[3], max(box[1], a[1]+t*dy)))
    start, end = at(lower), at(upper)
    return None if start == end else (start, end, lower, upper)


def _clip_line(line: Polyline, source_bounds: Bounds, box: Bounds, budget: _Budget) -> tuple[Polyline, ...]:
    origin = box[:2]
    def output(points, closed):
        budget.output(len(points))
        translated = tuple((x-origin[0], y-origin[1]) for x, y in points)
        return replace(line, points_mm=translated, closed=closed)

    if (source_bounds[2] < box[0] or source_bounds[0] > box[2]
            or source_bounds[3] < box[1] or source_bounds[1] > box[3]):
        return ()
    if (box[0] <= source_bounds[0] <= source_bounds[2] <= box[2]
            and box[1] <= source_bounds[1] <= source_bounds[3] <= box[3]):
        budget.step(len(line.points_mm))
        return (output(line.points_mm, line.closed),)

    fragments, current = [], []
    previous_reached_knot = False
    def finish():
        nonlocal current
        if len(current) >= 2:
            fragments.append(tuple(current))
        current = []

    for a, b in zip(line.points_mm, line.points_mm[1:]):
        budget.step()
        # A repeated source knot does not introduce a tool-up discontinuity.
        if a == b and _inside(a, box):
            continue
        clipped = _clip_segment(a, b, box)
        if clipped is None:
            finish()
            previous_reached_knot = False
            continue
        start, end, lower, upper = clipped
        if current and previous_reached_knot and lower == 0 and current[-1] == start:
            current.append(end)
        else:
            finish()
            current = [start, end]
        previous_reached_knot = upper == 1
        if not previous_reached_knot:
            finish()
    finish()
    # A closed source's arbitrary start knot must not split an otherwise
    # continuous clipped fragment. This does not add any closing segment.
    if (line.closed and len(fragments) > 1 and _inside(line.points_mm[0], box)
            and fragments[-1][-1] == fragments[0][0] == line.points_mm[0]):
        fragments = [fragments[-1][:-1]+fragments[0], *fragments[1:-1]]
    return tuple(output(fragment, False) for fragment in fragments)


def _grid_count(extent, span, step, maximum):
    if extent <= span:
        return 1
    ratio = (extent-span)/step
    if not math.isfinite(ratio) or ratio > maximum:
        raise PanelingError("Panel count exceeds the requested page budget")
    count = math.ceil(ratio)+1
    # Correct any division rounding which would leave an uncovered sliver.
    if (count-1)*step+span < extent:
        count += 1
    return count


def _validate_prepared(prepared):
    if not isinstance(prepared, PreparedJob) or not prepared.polylines:
        raise PanelingError("A nonempty complete PreparedJob is required")
    if len(prepared.polylines) > _MAX_OUTPUT_LINES:
        raise PanelingError("Input polyline budget exceeded")
    try:
        box = tuple(prepared.bounds_mm)
    except TypeError as error:
        raise PanelingError("Prepared artwork bounds are invalid") from error
    if (len(box) != 4 or any(not isinstance(v, Real) or isinstance(v, bool) or not math.isfinite(v) for v in box)
            or box[2] < box[0] or box[3] < box[1]
            or not math.isfinite(box[2]-box[0]) or not math.isfinite(box[3]-box[1])):
        raise PanelingError("Prepared artwork bounds are invalid")
    for dimension, extent in ((prepared.width_mm, box[2]-box[0]), (prepared.height_mm, box[3]-box[1])):
        if (not isinstance(dimension, Real) or isinstance(dimension, bool) or not math.isfinite(dimension)
                or not math.isclose(dimension, extent, rel_tol=1e-12, abs_tol=1e-8)):
            raise PanelingError("Prepared dimensions disagree with the artwork bounds")
    total = 0
    line_bounds = []
    for line in prepared.polylines:
        if not isinstance(line, Polyline) or len(line.points_mm) < 2:
            raise PanelingError("Every input polyline needs at least two points")
        total += len(line.points_mm)
        if total > _MAX_OUTPUT_POINTS:
            raise PanelingError("Input point budget exceeded")
        for point in line.points_mm:
            if len(point) != 2 or any(not isinstance(v, Real) or isinstance(v, bool) or not math.isfinite(v) for v in point):
                raise PanelingError("Prepared point must contain finite millimeter coordinates")
            if not _inside(point, box):
                raise PanelingError("Prepared point lies outside the artwork bounds")
        if not any(a != b for a, b in zip(line.points_mm, line.points_mm[1:])):
            raise PanelingError("A point-only polyline cannot become a panel cut")
        if line.closed is not (line.points_mm[0] == line.points_mm[-1]):
            raise PanelingError("Input closure must agree with endpoint equality")
        line_bounds.append(line.bounds_mm)
    return box, line_bounds


def _page_job(prepared, lines, width, height):
    bounds = _bounds(point for line in lines for point in line.points_mm)
    length = math.fsum(line.length_mm for line in lines)
    cursor, travel = (0.0, 0.0), []
    for line in lines:
        travel.append(math.hypot(line.points_mm[0][0]-cursor[0], line.points_mm[0][1]-cursor[1]))
        cursor = line.points_mm[-1]
    travel_length = math.fsum(travel)
    if not math.isfinite(length) or not math.isfinite(travel_length):
        raise PanelingError("Panel path length is nonfinite")
    return replace(prepared, polylines=tuple(lines), width_mm=bounds[2]-bounds[0],
                   height_mm=bounds[3]-bounds[1], bounds_mm=bounds, polyline_bounds_mm=bounds,
                   point_count=sum(len(line.points_mm) for line in lines), total_length_mm=length,
                   travel_length_mm=travel_length,
                   options=replace(prepared.options, media_width_mm=width, media_height_mm=height))


def split_panels(prepared: PreparedJob, options: PanelOptions) -> tuple[Panel, ...]:
    """Return all selected pages, including explicit blank-page records.

    IDs, rows and columns are zero-based; IDs always follow row-major grid
    positions. Selection filters that fixed grid in the requested traversal
    order. Origins cover the full artwork bounds at a fixed page-minus-overlap
    pitch, so the last page can extend beyond the artwork. Clipped contours
    remain open; only a fully contained original contour retains its closure.
    A blank page has an empty job and must not be sent to a cut compiler.
    """
    if not isinstance(options, PanelOptions):
        raise PanelingError("PanelOptions is required")
    width = _number(options.width_mm, "Panel width", positive=True)
    height = _number(options.height_mm, "Panel height", positive=True)
    overlap = _number(options.overlap_mm, "Panel overlap")
    if overlap >= min(width, height):
        raise PanelingError("Overlap must be smaller than both page dimensions")
    if type(options.max_pages) is not int or options.max_pages < 1:
        raise PanelingError("max_pages must be a positive integer")
    if options.order not in ("row-major", "column-major"):
        raise PanelingError("Panel order must be row-major or column-major")
    box, line_bounds = _validate_prepared(prepared)
    maximum = min(options.max_pages, _MAX_PAGES)
    step_x, step_y = width-overlap, height-overlap
    columns = _grid_count(box[2]-box[0], width, step_x, maximum)
    rows = _grid_count(box[3]-box[1], height, step_y, maximum)
    count = columns*rows
    if count > maximum:
        raise PanelingError("Panel count exceeds the requested page budget")
    selected = None
    if options.selected_pages is not None:
        if (not isinstance(options.selected_pages, Sequence) or isinstance(options.selected_pages, (str, bytes))
                or len(options.selected_pages) > count):
            raise PanelingError("selected_pages must be a bounded sequence of page indices")
        values = tuple(options.selected_pages)
        if (not values or any(type(index) is not int or not 0 <= index < count for index in values)
                or len(set(values)) != len(values)):
            raise PanelingError("Selected page indices must be unique valid indices and nonempty")
        selected = set(values)
    positions = ((row, column) for row in range(rows) for column in range(columns)) if options.order == "row-major" else (
        (row, column) for column in range(columns) for row in range(rows))
    budget, result = _Budget(), []
    for row, column in positions:
        index = row*columns+column
        if selected is not None and index not in selected:
            continue
        x, y = box[0]+column*step_x, box[1]+row*step_y
        page_box = (x, y, x+width, y+height)
        if not all(math.isfinite(v) for v in page_box):
            raise PanelingError("Panel grid coordinates are nonfinite")
        lines = []
        for line, source_bounds in zip(prepared.polylines, line_bounds):
            budget.step()
            lines.extend(_clip_line(line, source_bounds, page_box, budget))
        result.append(Panel(index, row, column, (x, y), _page_job(prepared, lines, width, height), count, rows, columns))
    return tuple(result)
