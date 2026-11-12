"""Offline pattern catalog and inspection viewer. Original files are read only."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
import uuid

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    # Catalog tools and their tests remain usable on Python builds without Tk.
    tk = None
    filedialog = messagebox = ttk = None

from tools.patterns.fvd_pattern import Pattern, PatternError, VectorPath, export_svg, read_pattern


WORKSPACE = Path(__file__).resolve().parent
PAGE_SIZE = 100
DATA_SETUP_HELP = ("Pattern data is not installed.\n"
                   "Extract the separate data ZIP into this project folder.\n"
                   "Expected: data/catalog.sqlite and data/patterns/\n"
                   "See README.md for the handoff and setup steps.")


class MissingDataError(FileNotFoundError):
    pass


def default_data_paths(workspace: Path) -> tuple[Path, Path]:
    """Portable handoff layout, with an existing development catalog fallback."""
    portable_catalog = workspace / "data" / "catalog.sqlite"
    development_catalog = workspace / "analysis" / "catalog.sqlite"
    catalog = (portable_catalog if portable_catalog.is_file() or not development_catalog.is_file()
               else development_catalog)
    return catalog, workspace / "data" / "patterns"


def require_data_sources(catalog: Path, pattern_root: Path) -> None:
    missing = []
    if not catalog.is_file():
        missing.append(f"Catalog not found: {catalog}")
    if not pattern_root.is_dir():
        missing.append(f"Pattern folder not found: {pattern_root}")
    if missing:
        raise MissingDataError("\n".join(missing))


DEFAULT_CATALOG, DEFAULT_PATTERN_ROOT = default_data_paths(WORKSPACE)
EXPLORER_SETTINGS = WORKSPACE / "explorer_settings.json"

TYPE_COLORS: dict[str, tuple[str, str]] = {
    # code: (background, description)
    "S": ("#dbeafe", "Short  (front + rear windshield)"),
    "L": ("#ede9fe", "Long   (+ sunroof)"),
    "F": ("#dcfce7", "Full   (all glass incl. sides)"),
    "U": ("#fed7aa", "Urethane / PPF pieces"),
    "H": ("#fef9c3", "H — specialty"),
    "T": ("#fce7f3", "T — specialty"),
    "G": ("#ccfbf1", "G — specialty"),
}


def _load_explorer_settings() -> dict:
    try:
        return json.loads(EXPLORER_SETTINGS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save_explorer_settings(data: dict) -> None:
    try:
        EXPLORER_SETTINGS.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_asset_warnings(root: Path) -> dict[str, str]:
    """Read optional compatibility notes as plain text, keyed by geometry asset ID."""
    path = root / "warnings.json"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid warnings.json: {error.msg} at line {error.lineno}, column {error.colno}") from error
    if not isinstance(data, dict) or any(not isinstance(value, str) for value in data.values()):
        raise ValueError("Invalid warnings.json: expected asset IDs mapped to warning text")
    return {key.casefold(): value for key, value in data.items()}


def warning_for_asset(warnings: dict[str, str], asset_id: object) -> str:
    return warnings.get(str(asset_id or "").casefold(), "")


@dataclass(frozen=True)
class Filters:
    query: str = ""
    make: str = ""
    model: str = ""
    trim: str = ""
    year: int | None = None
    pattern_type: str = ""
    local_only: bool = True


def pattern_filename(pattern_id: object, extension: object) -> str:
    """Accept flat FDA/FDU filenames; never interpret catalog data as a path."""
    stem = str(pattern_id or "").strip()
    suffix = str(extension or "").strip()
    if suffix and not suffix.startswith("."):
        suffix = "." + suffix
    if not re.fullmatch(r"[A-Za-z0-9_-]+", stem):
        raise ValueError("Invalid pattern identifier")
    if suffix.upper() not in (".FDA", ".FDU"):
        raise ValueError("This viewer supports FDA and FDU pattern files")
    return stem + suffix.upper()


def pattern_filenames(pattern_id: object, extension: object) -> tuple[str, ...]:
    if extension is None or not str(extension).strip():
        return (pattern_filename(pattern_id, "FDU"), pattern_filename(pattern_id, "FDA"))
    return (pattern_filename(pattern_id, extension),)


class PatternFiles:
    """Filename index keyed by the catalog's full FDImageId geometry asset ID."""

    def __init__(self, names):
        self.names = tuple(sorted(set(names), key=str.casefold))
        self.lookup = {name.casefold(): name for name in self.names}

    def __len__(self) -> int:
        return len(self.names)

    def matches(self, pattern_id: object, extension: object) -> tuple[str, ...]:
        expected = pattern_filenames(pattern_id, extension)
        return tuple(self.lookup[name.casefold()] for name in expected if name.casefold() in self.lookup)


def safe_pattern_path(root: Path, pattern_id: object, extension: object,
                      local_files: PatternFiles | None = None) -> Path:
    folder = root.resolve()
    index = local_files if local_files is not None else local_pattern_names(root)
    names = index.matches(pattern_id, extension)
    candidates = [(folder / filename).resolve() for filename in names]
    if any(candidate.parent != folder for candidate in candidates):
        raise ValueError("Pattern path escapes the selected source folder")
    if len(candidates) > 1:
        raise ValueError("Ambiguous geometry asset: both FDA and FDU files match")
    if not candidates:
        raise FileNotFoundError("The catalog entry has no matching local FDA/FDU file")
    return candidates[0]


def local_pattern_names(root: Path) -> PatternFiles:
    """Inventory only filenames. Pattern contents are read after selection."""
    if not root.is_dir():
        return PatternFiles(())
    return PatternFiles(p.name for p in root.iterdir()
                        if p.suffix.upper() in (".FDA", ".FDU") and p.is_file())


def has_local_file(row: sqlite3.Row | dict, local_names: PatternFiles | set[str]) -> bool:
    try:
        index = local_names if isinstance(local_names, PatternFiles) else PatternFiles(local_names)
        return len(index.matches(row["asset_id"], row["extension"])) == 1
    except ValueError:
        return False


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Catalog:
    """A read-only connection to the recovered catalog, never the vendor MDB."""

    def __init__(self, path: Path):
        self.path = path.resolve(strict=True)
        if not self.path.is_file():
            raise ValueError("Catalog must be a SQLite file")
        self.connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only=ON")
        self._local_index: PatternFiles | None = None
        self._all_assets_local = False
        required = {"id", "asset_id", "make", "model", "trim", "year_from", "year_to", "pattern_type", "extension", "description"}
        actual = {row[1] for row in self.connection.execute("PRAGMA table_info(patterns)")}
        if not required.issubset(actual):
            self.connection.close()
            raise ValueError("Catalog lacks the recovered patterns view; run the catalog export first")

    def close(self) -> None:
        self.connection.close()

    def choices(self, field: str, make: str = "", model: str = "", pattern_type: str = "") -> list[str]:
        if field not in ("make", "model", "trim", "pattern_type"):
            raise ValueError("Unsupported filter field")
        sql = f'SELECT DISTINCT "{field}" FROM patterns WHERE "{field}" IS NOT NULL AND CAST("{field}" AS TEXT) <> \'\''
        parameters: list[str] = []
        if field == "model" and make:
            sql += " AND make = ? COLLATE NOCASE"
            parameters.append(make)
        if field == "trim":
            if make:
                sql += " AND make = ? COLLATE NOCASE"
                parameters.append(make)
            if model:
                sql += " AND model = ? COLLATE NOCASE"
                parameters.append(model)
        if pattern_type:
            sql += " AND pattern_type = ? COLLATE NOCASE"
            parameters.append(pattern_type)
        sql += f' ORDER BY "{field}" COLLATE NOCASE'
        return [str(row[0]) for row in self.connection.execute(sql, parameters)]

    def year_choices(self, make: str = "", model: str = "", trim: str = "", pattern_type: str = "") -> list[int]:
        sql = "SELECT DISTINCT year_from, year_to FROM patterns WHERE year_from IS NOT NULL AND year_to IS NOT NULL"
        parameters: list[str] = []
        if make:
            sql += " AND make = ? COLLATE NOCASE"
            parameters.append(make)
        if model:
            sql += " AND model = ? COLLATE NOCASE"
            parameters.append(model)
        if trim:
            sql += " AND trim = ? COLLATE NOCASE"
            parameters.append(trim)
        if pattern_type:
            sql += " AND pattern_type = ? COLLATE NOCASE"
            parameters.append(pattern_type)
        years: set[int] = set()
        for row in self.connection.execute(sql, parameters):
            for y in range(int(row[0]), int(row[1]) + 1):
                years.add(y)
        return sorted(years)

    def type_names(self) -> dict[str, str]:
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(CarPatternType)")}
        if not {"ShortName", "FullName"}.issubset(columns):
            return {}
        return {str(row[0]).strip(): str(row[1]).strip()
                for row in self.connection.execute("SELECT ShortName, FullName FROM CarPatternType")
                if row[0] is not None and row[1] is not None}

    @staticmethod
    def where(filters: Filters) -> tuple[str, list[object]]:
        clauses: list[str] = []
        values: list[object] = []
        if filters.query.strip():
            fields = ("id", "make", "model", "trim", "description")
            clauses.append("(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")")
            values.extend([_like(filters.query.strip())] * len(fields))
        for field, value in (("make", filters.make), ("model", filters.model), ("trim", filters.trim), ("pattern_type", filters.pattern_type)):
            if value:
                clauses.append(f"CAST({field} AS TEXT) = ? COLLATE NOCASE")
                values.append(value)
        if filters.year is not None:
            # Null endpoints are unknown, not unbounded applicability claims.
            clauses.append("year_from <= ? AND year_to >= ?")
            values.extend([filters.year, filters.year])
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), values

    def search(self, filters: Filters, local_names: PatternFiles | set[str], offset: int = 0,
               limit: int = PAGE_SIZE) -> tuple[list[sqlite3.Row], int]:
        if offset < 0 or not 1 <= limit <= 500:
            raise ValueError("Invalid catalog page")
        where, values = self.where(filters)
        if not isinstance(local_names, PatternFiles):
            local_names = PatternFiles(local_names)
        if filters.local_only and local_names is not self._local_index:
            self._local_index = local_names
            self._all_assets_local = all(has_local_file(row, local_names) for row in self.connection.execute(
                "SELECT DISTINCT asset_id, extension FROM patterns"))
        order = " ORDER BY make COLLATE NOCASE, model COLLATE NOCASE, year_from DESC, trim COLLATE NOCASE, id"
        if not filters.local_only or self._all_assets_local:
            total = self.connection.execute("SELECT COUNT(*) FROM patterns" + where, values).fetchone()[0]
            rows = self.connection.execute("SELECT * FROM patterns" + where + order + " LIMIT ? OFFSET ?", values + [limit, offset]).fetchall()
            return rows, total
        # Catalog FileExists can be stale. Compare with current local filenames.
        rows: list[sqlite3.Row] = []
        total = 0
        for row in self.connection.execute("SELECT * FROM patterns" + where + order, values):
            if has_local_file(row, local_names):
                if offset <= total < offset + limit:
                    rows.append(row)
                total += 1
        return rows, total


def sample_path(path: VectorPath, steps: int = 12) -> list[list[tuple[float, float]]]:
    """Sample inferred cubic segments for screen display only; exports retain curves."""
    if steps < 1:
        raise ValueError("Curve sampling requires positive steps")
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    i = 0
    while i < len(path.points):
        x, y, kind, _ = path.points[i]
        if kind == 0:
            if current:
                segments.append(current)
            current = [(x, y)]
            i += 1
        elif not current:
            raise PatternError("Vector path does not begin with a move")
        elif kind == 4:
            current.append((x, y))
            i += 1
        elif kind == 1 and i + 2 < len(path.points) and [p[2] for p in path.points[i:i + 3]] == [1, 2, 3]:
            start = current[-1]
            a, b, end = path.points[i:i + 3]
            for n in range(1, steps + 1):
                t = n / steps
                u = 1 - t
                current.append((u**3 * start[0] + 3*u*u*t*a[0] + 3*u*t*t*b[0] + t**3*end[0],
                                u**3 * start[1] + 3*u*u*t*a[1] + 3*u*t*t*b[1] + t**3*end[1]))
            i += 3
        else:
            raise PatternError(f"Unsupported vector point sequence: {kind}")
    if current:
        segments.append(current)
    return segments


def export_preview(pattern: Pattern) -> Path:
    """Write a new preview beneath this workspace, never to a pattern folder."""
    if not getattr(pattern, "complete_geometry", False):
        raise PatternError("Partial geometry cannot be exported as a complete SVG preview")
    folder = WORKSPACE / "exports"
    folder.mkdir(exist_ok=True)
    if folder.resolve().parent != WORKSPACE.resolve():
        raise ValueError("Export folder must remain in the workspace")
    target = folder / f"{Path(pattern.source).stem}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.svg"
    if target.exists():
        raise FileExistsError(target)
    export_svg(pattern, target, y_axis="up")
    return target


def year_label(row: sqlite3.Row | dict) -> str:
    low, high = row["year_from"], row["year_to"]
    if low is None and high is None:
        return "Unknown"
    return str(low) if low == high else f"{low if low is not None else '?'}–{high if high is not None else '?'}"


class PatternExplorer(tk.Tk if tk else object):
    def __init__(self, catalog_path: Path, pattern_root: Path):
        super().__init__()
        self.title("Tint & PPF Pattern Explorer")
        self.geometry("1360x850")
        self.minsize(1020, 650)
        self.catalog: Catalog | None = None
        self.local_names = PatternFiles(())
        self.asset_warnings: dict[str, str] = {}
        self.offset = 0
        self.total = 0
        self.rows: dict[str, sqlite3.Row] = {}
        self.type_names: dict[str, str] = {}
        self.type_codes: dict[str, str] = {}
        self.pattern: Pattern | None = None
        self.preview_lines: list[tuple[int, list[tuple[float, float]]]] = []
        self.search_after: str | None = None
        self.resize_after: str | None = None
        self.query_var = tk.StringVar()
        self.make_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.trim_var = tk.StringVar()
        self.year_var = tk.StringVar()
        self.type_var = tk.StringVar()
        self.local_var = tk.BooleanVar(value=True)
        self.catalog_var = tk.StringVar(value=str(catalog_path))
        self.root_var = tk.StringVar(value=str(pattern_root))
        self.status_var = tk.StringVar(value="Choose a recovered catalog and local pattern folder.")
        self.page_var = tk.StringVar()
        self.preview_notice_var = tk.StringVar()
        self._nav_items: dict[str, tuple] = {}
        self._nav_counter = 0
        self._nav_key_char = ""
        self._nav_key_index = 0
        self._meta_vars: dict[str, tk.StringVar] = {}
        self.detail_title_var = tk.StringVar(value="")
        self._build()
        self.bind("<KeyPress>", self._global_keypress)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._apply_saved_settings()
        self.after(50, self.load_catalog)

    def _build(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=27)
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Pattern Explorer", font=("Segoe UI", 19, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Browse the recovered catalog and preview local vector patterns.").pack(anchor="w", pady=(2, 10))
        sources = ttk.LabelFrame(outer, text="Local sources", padding=8)
        sources.pack(fill="x")
        for index, (label, var, action) in enumerate((("Catalog", self.catalog_var, self.choose_catalog), ("Pattern folder", self.root_var, self.choose_root))):
            ttk.Label(sources, text=label, width=14).grid(row=index, column=0, sticky="w", pady=3)
            ttk.Entry(sources, textvariable=var).grid(row=index, column=1, sticky="ew", padx=7, pady=3)
            ttk.Button(sources, text="Browse…", command=action).grid(row=index, column=2, padx=4)
        ttk.Button(sources, text="Reload sources", command=self.load_catalog).grid(row=0, column=3, rowspan=2, padx=6)
        sources.columnconfigure(1, weight=1)

        # 3-pane layout: left (tree) | center (details + results) | right (preview)
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True, pady=(8, 0))
        left = ttk.Frame(panes)
        center = ttk.Frame(panes)
        right = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(left, weight=2)
        panes.add(center, weight=5)
        panes.add(right, weight=3)

        # === LEFT: tree navigation ===
        ttk.Checkbutton(left, text="Local files only", variable=self.local_var, command=self.search).pack(anchor="w", pady=(0, 3))
        search_row = ttk.Frame(left)
        search_row.pack(fill="x", pady=(0, 2))
        self.query_entry = ttk.Entry(search_row, textvariable=self.query_var)
        self.query_entry.pack(side="left", fill="x", expand=True, padx=(0, 2))
        self.query_entry.bind("<KeyRelease>", self.debounce_search)
        ttk.Button(search_row, text="✕", width=2, command=self.reset).pack(side="right")
        type_row = ttk.Frame(left)
        type_row.pack(fill="x", pady=(0, 2))
        ttk.Label(type_row, text="Type:", width=5).pack(side="left")
        self.type_box = ttk.Combobox(type_row, textvariable=self.type_var, state="readonly")
        self.type_box.pack(side="left", fill="x", expand=True)
        self.type_box.bind("<<ComboboxSelected>>", lambda _: self._nav_refresh_tree())
        browse_toolbar = ttk.Frame(left)
        browse_toolbar.pack(fill="x", pady=(0, 2))
        ttk.Button(browse_toolbar, text="Collapse All", command=self.collapse_all).pack(side="right")
        browse_tree_frame = ttk.Frame(left)
        browse_tree_frame.pack(fill="both", expand=True)
        self.nav_tree = ttk.Treeview(browse_tree_frame, show="tree", selectmode="browse")
        nav_scroll = ttk.Scrollbar(browse_tree_frame, orient="vertical", command=self.nav_tree.yview)
        self.nav_tree.configure(yscrollcommand=nav_scroll.set)
        self.nav_tree.pack(side="left", fill="both", expand=True)
        nav_scroll.pack(side="right", fill="y")
        self.nav_tree.bind("<<TreeviewOpen>>", self._nav_open)
        self.nav_tree.bind("<<TreeviewSelect>>", self._nav_select)
        self.nav_tree.bind("<KeyPress>", self._nav_keypress)
        self.nav_tree.bind("<Button-1>", self._nav_click)
        self.nav_tree.bind("<Shift-KeyPress-c>", lambda _e: self.collapse_all())
        self.bind("<Shift-KeyPress-c>", lambda _e: self.collapse_all())
        # Hidden filter combos — still functional, drive search() via _nav_select
        _hidden = ttk.Frame(left)
        self.make_box = self._combo(_hidden, "Make", self.make_var, 1, 14)
        self.model_box = self._combo(_hidden, "Model", self.model_var, 2, 16)
        self.trim_box = self._combo(_hidden, "Trim", self.trim_var, 3, 16)
        self.year_box = self._combo(_hidden, "Year", self.year_var, 4, 7)
        self.model_box["state"] = "disabled"
        self.trim_box["state"] = "disabled"
        self.year_box["state"] = "disabled"
        self.make_box.bind("<<ComboboxSelected>>", self.make_changed)
        self.model_box.bind("<<ComboboxSelected>>", self.model_changed)
        self.trim_box.bind("<<ComboboxSelected>>", self.trim_changed)
        self.year_box.bind("<<ComboboxSelected>>", lambda _: self.search())

        # === CENTER: detail header + results table ===
        detail_header = ttk.Frame(center, padding=(0, 0, 0, 6))
        detail_header.pack(fill="x")
        ttk.Label(detail_header, textvariable=self.detail_title_var,
                  font=("Segoe UI", 12, "bold"), wraplength=560).pack(anchor="w")
        meta_frame = ttk.Frame(detail_header)
        meta_frame.pack(fill="x", pady=(5, 0))
        for col, (key, label) in enumerate([("type", "Type"), ("years", "Year range"), ("dims", "Dims W×L")]):
            ttk.Label(meta_frame, text=label + ":", foreground="#657383",
                      font=("Segoe UI", 9)).grid(row=0, column=col * 2, sticky="w", padx=(0, 4), pady=1)
            v = tk.StringVar(value="—")
            self._meta_vars[key] = v
            ttk.Label(meta_frame, textvariable=v,
                      font=("Segoe UI", 9, "bold")).grid(row=0, column=col * 2 + 1, sticky="w", padx=(0, 28), pady=1)
        for col, (key, label) in enumerate([("paths", "Paths"), ("points", "Points"), ("file", "File")]):
            ttk.Label(meta_frame, text=label + ":", foreground="#657383",
                      font=("Segoe UI", 9)).grid(row=1, column=col * 2, sticky="w", padx=(0, 4), pady=1)
            v = tk.StringVar(value="—")
            self._meta_vars[key] = v
            ttk.Label(meta_frame, textvariable=v,
                      font=("Segoe UI", 9, "bold")).grid(row=1, column=col * 2 + 1, sticky="w", padx=(0, 28), pady=1)
        ttk.Label(detail_header, textvariable=self.preview_notice_var,
                  foreground="#8a4b13", wraplength=560, font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))
        ttk.Separator(center, orient="horizontal").pack(fill="x", pady=(0, 4))
        columns = ("make", "model", "trim", "years", "type", "local")
        results_frame = ttk.Frame(center)
        results_frame.pack(fill="both", expand=True)
        results_frame.columnconfigure(0, weight=1)
        results_frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(results_frame, columns=columns, show="headings", selectmode="browse")
        for column, title, width in zip(columns, ("Make", "Model", "Trim / description", "Years", "Type", "File"), (90, 120, 170, 100, 70, 50)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=45, stretch=column in ("model", "trim"))
        scroll = ttk.Scrollbar(results_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        for code, (bg, _) in TYPE_COLORS.items():
            self.tree.tag_configure(f"type_{code}", background=bg)
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        # Legend
        legend_frame = ttk.Frame(center)
        legend_frame.pack(fill="x", pady=(3, 0))
        self.legend_inner = ttk.Frame(legend_frame)
        self.legend_inner.pack(anchor="w")
        page = ttk.Frame(center, padding=(0, 4))
        page.pack(fill="x")
        self.prev_button = ttk.Button(page, text="‹ Previous", command=lambda: self.change_page(-1))
        self.prev_button.pack(side="left")
        self.next_button = ttk.Button(page, text="Next ›", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        ttk.Label(page, textvariable=self.page_var).pack()

        # === RIGHT: vector preview ===
        preview_header = ttk.Frame(right)
        preview_header.pack(fill="x", pady=(0, 4))
        ttk.Label(preview_header, text="Vector preview", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.export_button = ttk.Button(preview_header, text="Export SVG", command=self.export_current, state="disabled")
        self.export_button.pack(side="right")
        btn_row = ttk.Frame(right)
        btn_row.pack(fill="x", pady=(0, 6))
        tk.Button(btn_row, text="Select Pattern", command=self.open_cut_dialog,
                  bg="#1a6fc4", fg="white", activebackground="#155a9e", activeforeground="white",
                  font=("Segoe UI", 10, "bold"), relief="flat", padx=12, pady=4,
                  cursor="hand2").pack(side="left", padx=(0, 8))
        ttk.Button(btn_row, text="Fit", command=self.fit_preview).pack(side="left")
        self.canvas = tk.Canvas(right, background="#fafbfc", highlightbackground="#ced4da", highlightthickness=1)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.resize_preview)
        self.canvas.bind("<MouseWheel>", self.zoom_preview)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        ttk.Label(right, text="Scroll to zoom · Drag to pan · Fit restores the view",
                  foreground="#59626c", font=("Segoe UI", 8)).pack(anchor="w", pady=(4, 0))

        ttk.Label(outer, text="Source units: PostScript points · Physical cutter testing is pending.",
                  foreground="#8a4b13").pack(anchor="w", pady=(6, 2))
        ttk.Label(outer, textvariable=self.status_var, anchor="w").pack(fill="x")

    def _combo(self, parent: ttk.Frame, label: str, variable: tk.StringVar, column: int, width: int) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w")
        box = ttk.Combobox(parent, textvariable=variable, width=width, state="readonly")
        box.grid(row=1, column=column, sticky="ew", padx=(0, 10))
        return box

    def _nav_iid(self) -> str:
        self._nav_counter += 1
        return f"n{self._nav_counter}"

    def _active_type_code(self) -> str:
        return self.type_codes.get(self.type_var.get(), "")

    def _nav_populate_makes(self) -> None:
        self.nav_tree.delete(*self.nav_tree.get_children())
        self._nav_items.clear()
        if not self.catalog:
            return
        pt = self._active_type_code()
        for make in self.catalog.choices("make", pattern_type=pt):
            iid = self._nav_iid()
            self._nav_items[iid] = (make, "", "", "")
            self.nav_tree.insert("", "end", iid=iid, text=make)
            self.nav_tree.insert(iid, "end", iid=f"{iid}_ph", text="")

    def _nav_open(self, _event: object = None) -> None:
        iid = self.nav_tree.focus()
        if not iid or not self.catalog or iid not in self._nav_items:
            return
        children = self.nav_tree.get_children(iid)
        if len(children) != 1 or not children[0].endswith("_ph"):
            return
        self.nav_tree.delete(children[0])
        make, model, trim, _ = self._nav_items[iid]
        pt = self._active_type_code()
        if not model:
            for m in self.catalog.choices("model", make, pattern_type=pt):
                child = self._nav_iid()
                self._nav_items[child] = (make, m, "", "")
                self.nav_tree.insert(iid, "end", iid=child, text=m)
                self.nav_tree.insert(child, "end", iid=f"{child}_ph", text="")
        elif not trim:
            for t in self.catalog.choices("trim", make, model, pattern_type=pt):
                child = self._nav_iid()
                self._nav_items[child] = (make, model, t, "")
                self.nav_tree.insert(iid, "end", iid=child, text=t)
                self.nav_tree.insert(child, "end", iid=f"{child}_ph", text="")
        else:
            for y in reversed(self.catalog.year_choices(make, model, trim, pattern_type=pt)):
                child = self._nav_iid()
                self._nav_items[child] = (make, model, trim, str(y))
                self.nav_tree.insert(iid, "end", iid=child, text=str(y))

    def _nav_click(self, event: tk.Event) -> None:
        if self.nav_tree.identify_element(event.x, event.y) == "indicator":
            return  # arrow click — default behavior already handles it
        item = self.nav_tree.identify_row(event.y)
        if not item or item not in self._nav_items:
            return
        if self.nav_tree.item(item, "open"):
            self.nav_tree.item(item, open=False)
        else:
            self.nav_tree.focus(item)
            self.nav_tree.item(item, open=True)
            self._nav_open()  # trigger lazy load in case <<TreeviewOpen>> doesn't fire

    def _nav_keypress(self, event: tk.Event) -> str | None:
        if event.state & 0x1:  # Shift held — let root-level shortcuts fire
            return None
        char = event.char.upper()
        if not char or not char.isalnum():
            return None
        focused = self.nav_tree.focus()
        # If the focused node is expanded, cycle within its children (next level)
        if focused and self.nav_tree.item(focused, "open") and self.nav_tree.get_children(focused):
            candidates = [iid for iid in self.nav_tree.get_children(focused) if not iid.endswith("_ph")]
        else:
            parent = self.nav_tree.parent(focused) if focused else ""
            candidates = list(self.nav_tree.get_children(parent))
        siblings = candidates
        matches = [iid for iid in siblings
                   if self.nav_tree.item(iid, "text").upper().startswith(char)
                   and iid in self._nav_items]
        if not matches:
            return "break"
        if char == self._nav_key_char:
            self._nav_key_index = (self._nav_key_index + 1) % len(matches)
        else:
            self._nav_key_char = char
            self._nav_key_index = 0
        target = matches[self._nav_key_index]
        self.nav_tree.focus(target)
        self.nav_tree.selection_set(target)
        self.nav_tree.see(target)
        return "break"

    def _nav_select(self, _event: object = None) -> None:
        iid = self.nav_tree.focus()
        if not iid or iid not in self._nav_items:
            return
        make, model, trim, year = self._nav_items[iid]
        self.make_var.set(make)
        self.model_var.set(model)
        self.trim_var.set(trim)
        self.year_var.set(year)
        if self.catalog:
            if make:
                self.model_box["values"] = [""] + self.catalog.choices("model", make)
                self.model_box["state"] = "readonly"
            else:
                self.model_box["values"] = [""]
                self.model_box["state"] = "disabled"
            if model:
                self.trim_box["values"] = [""] + self.catalog.choices("trim", make, model)
                self.trim_box["state"] = "readonly"
            else:
                self.trim_box["values"] = [""]
                self.trim_box["state"] = "disabled"
            if trim:
                years = self.catalog.year_choices(make, model, trim)
                self.year_box["values"] = [""] + [str(y) for y in years]
                self.year_box["state"] = "readonly"
            else:
                self.year_box["values"] = [""]
                self.year_box["state"] = "disabled"
        if year:
            self.search()
        else:
            self.tree.delete(*self.tree.get_children())
            self.rows.clear()
            self.page_var.set("")
            self.prev_button.configure(state="disabled")
            self.next_button.configure(state="disabled")
            self.clear_preview("Select a year from the tree to view patterns.")

    def _rebuild_legend(self) -> None:
        for w in self.legend_inner.winfo_children():
            w.destroy()
        for code, (bg, fallback_desc) in TYPE_COLORS.items():
            if code not in {c for c in self.type_codes.values()}:
                continue
            full_name = self.type_names.get(code, fallback_desc)
            chip = tk.Frame(self.legend_inner, bg=bg, bd=1, relief="solid", width=14, height=14)
            chip.pack_propagate(False)
            chip.pack(side="left", padx=(0, 3), pady=2)
            ttk.Label(self.legend_inner, text=f"{code} · {full_name}",
                      font=("Segoe UI", 8)).pack(side="left", padx=(0, 12))

    def _nav_refresh_tree(self) -> None:
        self._nav_populate_makes()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.page_var.set("")
        self.prev_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        self.clear_preview("Select a year from the tree to view patterns.")

    def collapse_all(self) -> None:
        yview = self.nav_tree.yview()
        def _collapse(iid: str) -> None:
            for child in self.nav_tree.get_children(iid):
                _collapse(child)
            self.nav_tree.item(iid, open=False)
        for root_iid in self.nav_tree.get_children():
            _collapse(root_iid)
        self.nav_tree.yview_moveto(yview[0])

    def choose_catalog(self) -> None:
        chosen = filedialog.askopenfilename(title="Open recovered catalog", initialdir=WORKSPACE / "analysis", filetypes=[("SQLite catalog", "*.sqlite *.sqlite3 *.db"), ("All files", "*.*")])
        if chosen:
            self.catalog_var.set(chosen)
            self.load_catalog()

    def choose_root(self) -> None:
        chosen = filedialog.askdirectory(title="Choose local pattern folder", initialdir=self.root_var.get())
        if chosen:
            self.root_var.set(chosen)
            self.load_catalog()

    def load_catalog(self) -> None:
        if self.catalog:
            self.catalog.close()
            self.catalog = None
        try:
            catalog_path, pattern_root = Path(self.catalog_var.get()), Path(self.root_var.get())
            require_data_sources(catalog_path, pattern_root)
            self.catalog = Catalog(catalog_path)
            self.local_names = local_pattern_names(pattern_root)
            self.asset_warnings = load_asset_warnings(pattern_root)
            self.make_box["values"] = [""] + self.catalog.choices("make")
            self._nav_populate_makes()
            self.type_names = self.catalog.type_names()
            self.type_codes = {f"{code} · {self.type_names[code]}" if code in self.type_names else code: code
                               for code in self.catalog.choices("pattern_type")}
            pending_code = getattr(self, "_pending_type_filter", "")
            self.type_var.set("")
            self.type_box["values"] = [""] + list(self.type_codes)
            if pending_code:
                for display_label, code in self.type_codes.items():
                    if code == pending_code:
                        self.type_var.set(display_label)
                        break
            self._pending_type_filter = ""
            for var in (self.make_var, self.model_var, self.trim_var, self.year_var):
                var.set("")
            for box in (self.model_box, self.trim_box, self.year_box):
                box["values"] = [""]
                box["state"] = "disabled"
            self.tree.delete(*self.tree.get_children())
            self.rows.clear()
            self.page_var.set("")
            self.prev_button.configure(state="disabled")
            self.next_button.configure(state="disabled")
            self.status_var.set(f"{len(self.local_names):,} local FDA/FDU files · select a make from the tree to browse")
            self.clear_preview("Select a make from the tree to begin.")
            self._rebuild_legend()
        except (OSError, sqlite3.Error, ValueError) as error:
            if self.catalog:
                self.catalog.close()
                self.catalog = None
            self.asset_warnings = {}
            self.tree.delete(*self.tree.get_children())
            if isinstance(error, MissingDataError):
                self.status_var.set("Pattern data missing — extract the separate data ZIP; see README.md.")
                self.clear_preview(DATA_SETUP_HELP)
            else:
                self.status_var.set(f"Catalog unavailable: {error}")
                self.clear_preview("Choose a valid recovered catalog to begin.")

    def make_changed(self, _event: object = None) -> None:
        self.model_var.set("")
        self.trim_var.set("")
        self.year_var.set("")
        make = self.make_var.get()
        if self.catalog and make:
            self.model_box["values"] = [""] + self.catalog.choices("model", make)
            self.model_box["state"] = "readonly"
        else:
            self.model_box["values"] = [""]
            self.model_box["state"] = "disabled"
        self.trim_box["values"] = [""]
        self.trim_box["state"] = "disabled"
        self.year_box["values"] = [""]
        self.year_box["state"] = "disabled"
        self.search()

    def model_changed(self, _event: object = None) -> None:
        self.trim_var.set("")
        self.year_var.set("")
        model = self.model_var.get()
        if self.catalog and model:
            self.trim_box["values"] = [""] + self.catalog.choices("trim", self.make_var.get(), model)
            self.trim_box["state"] = "readonly"
        else:
            self.trim_box["values"] = [""]
            self.trim_box["state"] = "disabled"
        self.year_box["values"] = [""]
        self.year_box["state"] = "disabled"
        self.search()

    def trim_changed(self, _event: object = None) -> None:
        self.year_var.set("")
        trim = self.trim_var.get()
        if self.catalog and trim:
            years = self.catalog.year_choices(self.make_var.get(), self.model_var.get(), trim)
            self.year_box["values"] = [""] + [str(y) for y in years]
            self.year_box["state"] = "readonly"
        else:
            self.year_box["values"] = [""]
            self.year_box["state"] = "disabled"
        self.search()

    def debounce_search(self, event: tk.Event) -> None:
        if event.keysym in ("Tab", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        if self.search_after:
            self.after_cancel(self.search_after)
        self.search_after = self.after(350, self.search)

    def _global_keypress(self, event: tk.Event) -> None:
        char = event.char
        if not char or not char.isalnum() or event.state & (0x1 | 0x4):  # ignore Shift/Ctrl combos
            return
        focused = self.focus_get()
        if focused is self.nav_tree or isinstance(focused, (ttk.Entry, tk.Entry, ttk.Combobox)):
            return
        self.nav_tree.focus_set()
        self._nav_keypress(event)

    def get_filters(self) -> Filters:
        text = self.year_var.get().strip()
        year = int(text) if text.isdigit() else None
        return Filters(self.query_var.get(), self.make_var.get(), self.model_var.get(), self.trim_var.get(), year, self.type_codes.get(self.type_var.get(), self.type_var.get()), self.local_var.get())

    def search(self, reset_page: bool = True) -> None:
        self.search_after = None
        if not self.catalog:
            return
        try:
            if reset_page:
                self.offset = 0
            rows, self.total = self.catalog.search(self.get_filters(), self.local_names, self.offset)
            self.tree.delete(*self.tree.get_children())
            self.rows.clear()
            for index, row in enumerate(rows):
                key = str(index)
                self.rows[key] = row
                pt = row["pattern_type"] or ""
                tag = (f"type_{pt}",) if pt in TYPE_COLORS else ()
                self.tree.insert("", "end", iid=key, values=(row["make"] or "", row["model"] or "", row["trim"] or row["description"] or "", year_label(row), pt, "Yes" if has_local_file(row, self.local_names) else "—"), tags=tag)
            end = min(self.offset + len(rows), self.total)
            self.page_var.set(f"{self.offset + 1 if rows else 0:,}–{end:,} of {self.total:,}")
            self.prev_button.configure(state="normal" if self.offset else "disabled")
            self.next_button.configure(state="normal" if end < self.total else "disabled")
            self.status_var.set(f"{self.total:,} matching catalog entries · {len(self.local_names):,} local FDA/FDU files · sources opened read only")
            self.clear_preview("Select a pattern to preview its local vectors.")
            if rows:
                self.tree.selection_set("0")
        except (ValueError, sqlite3.Error) as error:
            self.status_var.set(str(error))

    def reset(self) -> None:
        for variable in (self.query_var, self.make_var, self.model_var, self.trim_var, self.year_var, self.type_var):
            variable.set("")
        self.model_box["values"] = [""]
        self.model_box["state"] = "disabled"
        self.trim_box["values"] = [""]
        self.trim_box["state"] = "disabled"
        self.year_box["values"] = [""]
        self.year_box["state"] = "disabled"
        self._nav_refresh_tree()

    def change_page(self, direction: int) -> None:
        self.offset = max(0, self.offset + direction * PAGE_SIZE)
        self.search(reset_page=False)

    def clear_preview(self, text: str) -> None:
        self.pattern = None
        self.preview_lines.clear()
        self.canvas.delete("all")
        self.canvas.create_text(20, 25, anchor="nw", text=text, fill="#657383", tags="message")
        self.export_button.configure(state="disabled")
        self.preview_notice_var.set("")
        self.detail_title_var.set("Select a pattern to view details")
        for v in self._meta_vars.values():
            v.set("—")

    def selection_changed(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected or selected[0] not in self.rows:
            return
        row = self.rows[selected[0]]
        self.clear_preview("Loading local pattern…")
        title = f"{row['make'] or ''} {row['model'] or ''} · {row['trim'] or row['description'] or ''}"
        self.detail_title_var.set(title)
        try:
            path = safe_pattern_path(Path(self.root_var.get()), row["asset_id"], row["extension"], self.local_names)
            if not path.is_file():
                raise FileNotFoundError("The catalog entry has no matching local file")
            pattern = read_pattern(path)
            lines = [(vector.group, segment) for vector in pattern.paths for segment in sample_path(vector)]
            self.pattern = pattern
            self.preview_lines = lines
            complete = getattr(pattern, "complete_geometry", False)
            self.export_button.configure(state="normal" if complete else "disabled")
            self.preview_notice_var.set("" if complete else "Some geometry records unrecognized — SVG export disabled.")
            coords = [(x, y) for _, pts in lines for x, y in pts]
            if coords:
                w_in = (max(x for x, y in coords) - min(x for x, y in coords)) / 72.0
                h_in = (max(y for x, y in coords) - min(y for x, y in coords)) / 72.0
                dims = f"{w_in:.1f} × {h_in:.1f} in"
            else:
                dims = "—"
            self._meta_vars["type"].set(row["pattern_type"] or "—")
            self._meta_vars["years"].set(year_label(row))
            self._meta_vars["dims"].set(dims)
            self._meta_vars["paths"].set(f"{len(pattern.paths):,}")
            self._meta_vars["points"].set(f"{sum(len(v.points) for v in pattern.paths):,}")
            self._meta_vars["file"].set(f"{path.name}  ·  {pattern.sha256[:14]}…")
            if pattern.warnings:
                notice = self.preview_notice_var.get()
                self.preview_notice_var.set((notice + "  " if notice else "") + "; ".join(pattern.warnings))
            self.fit_preview()
        except (OSError, PatternError, ValueError) as error:
            self.clear_preview(str(error))
            self.detail_title_var.set(title)
        warning = warning_for_asset(self.asset_warnings, row["asset_id"])
        if warning:
            geometry_notice = self.preview_notice_var.get()
            self.preview_notice_var.set("COMPATIBILITY: " + warning + ("  —  " + geometry_notice if geometry_notice else ""))

    def resize_preview(self, _event: object = None) -> None:
        if self.resize_after:
            self.after_cancel(self.resize_after)
        self.resize_after = self.after(100, self.fit_preview)

    def fit_preview(self) -> None:
        self.resize_after = None
        if not self.preview_lines:
            return
        coordinates = [point for _, points in self.preview_lines for point in points]
        if not coordinates:
            return
        xmin, xmax = min(x for x, _ in coordinates), max(x for x, _ in coordinates)
        ymin, ymax = min(y for _, y in coordinates), max(y for _, y in coordinates)
        width, height = max(self.canvas.winfo_width(), 100), max(self.canvas.winfo_height(), 100)
        scale = min((width - 50) / max(xmax - xmin, 1), (height - 50) / max(ymax - ymin, 1))
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        colors = ("#1e4264", "#008578", "#bd641d", "#7455a3", "#bd4054", "#4c7d2e")
        self.canvas.delete("all")
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        for group, points in self.preview_lines:
            if len(points) < 2:
                continue
            flat = [v for x, y in points for v in ((x - cx) * scale + width / 2, (cy - y) * scale + height / 2)]
            self.canvas.create_line(*flat, fill=colors[group % len(colors)], width=1.4, tags="geometry")

    def zoom_preview(self, event: tk.Event) -> None:
        if self.preview_lines:
            factor = 1.15 if event.delta > 0 else 1 / 1.15
            self.canvas.scale("geometry", self.canvas.canvasx(event.x), self.canvas.canvasy(event.y), factor, factor)

    def export_current(self) -> None:
        if not self.pattern:
            return
        try:
            path = export_preview(self.pattern)
            self.status_var.set(f"SVG preview saved: {path}")
            messagebox.showinfo("SVG preview saved", f"Saved a new file:\n{path}\n\nSVG retains raw source coordinates without a physical width or height. Physical cutter testing is pending.")
        except (OSError, PatternError, ValueError) as error:
            messagebox.showerror("Export failed", str(error))

    def open_cut_dialog(self) -> None:
        try:
            from printing.dialog import CutDialog
            selected = self.tree.selection()
            row = self.rows.get(selected[0]) if selected else None
            label = (f"{row['make'] or ''} {row['model'] or ''} · {year_label(row)}" if row is not None else "Selected pattern")
            note = warning_for_asset(self.asset_warnings, row["asset_id"]) if row is not None else ""
            CutDialog(self, pattern=self.pattern, workspace=WORKSPACE, source_label=label, compatibility_note=note)
        except Exception as error:
            messagebox.showerror("Select Pattern", str(error), parent=self)

    def _apply_saved_settings(self) -> None:
        s = _load_explorer_settings()
        if "geometry" in s:
            try:
                self.geometry(s["geometry"])
            except tk.TclError:
                pass
        if "catalog" in s:
            self.catalog_var.set(s["catalog"])
        if "pattern_root" in s:
            self.root_var.set(s["pattern_root"])
        if "local_only" in s:
            self.local_var.set(bool(s["local_only"]))
        self._pending_type_filter = s.get("type_code", "")

    def _persist_settings(self) -> None:
        _save_explorer_settings({
            "geometry": self.geometry(),
            "catalog": self.catalog_var.get(),
            "pattern_root": self.root_var.get(),
            "local_only": self.local_var.get(),
            "type_code": self.type_codes.get(self.type_var.get(), ""),
        })

    def close(self) -> None:
        self._persist_settings()
        if self.catalog:
            self.catalog.close()
        self.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--pattern-root", type=Path, default=DEFAULT_PATTERN_ROOT)
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 or newer with Tcl/Tk is required; see README.md.")
    if tk is None:
        parser.error("This Python build lacks Tkinter. Install Python 3.11+ with Tcl/Tk enabled, then relaunch start.cmd; see README.md.")
    PatternExplorer(args.catalog, args.pattern_root).mainloop()


if __name__ == "__main__":
    main()
