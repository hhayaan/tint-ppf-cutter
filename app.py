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

    def choices(self, field: str, make: str = "") -> list[str]:
        if field not in ("make", "model", "pattern_type"):
            raise ValueError("Unsupported filter field")
        sql = f'SELECT DISTINCT "{field}" FROM patterns WHERE "{field}" IS NOT NULL AND CAST("{field}" AS TEXT) <> \'\''
        parameters: list[str] = []
        if field == "model" and make:
            sql += " AND make = ? COLLATE NOCASE"
            parameters.append(make)
        sql += f' ORDER BY "{field}" COLLATE NOCASE'
        return [str(row[0]) for row in self.connection.execute(sql, parameters)]

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
        for field, value in (("make", filters.make), ("model", filters.model), ("pattern_type", filters.pattern_type)):
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
        order = " ORDER BY make COLLATE NOCASE, model COLLATE NOCASE, year_from, trim COLLATE NOCASE, id"
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
        self.year_var = tk.StringVar()
        self.type_var = tk.StringVar()
        self.local_var = tk.BooleanVar(value=True)
        self.catalog_var = tk.StringVar(value=str(catalog_path))
        self.root_var = tk.StringVar(value=str(pattern_root))
        self.status_var = tk.StringVar(value="Choose a recovered catalog and local pattern folder.")
        self.page_var = tk.StringVar()
        self.preview_notice_var = tk.StringVar()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
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
        filters = ttk.Frame(outer, padding=(0, 10))
        filters.pack(fill="x")
        ttk.Label(filters, text="Search").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(filters, textvariable=self.query_var, width=34)
        entry.grid(row=1, column=0, sticky="ew", padx=(0, 10))
        entry.bind("<KeyRelease>", self.debounce_search)
        self.make_box = self._combo(filters, "Make", self.make_var, 1, 20)
        self.model_box = self._combo(filters, "Model", self.model_var, 2, 22)
        ttk.Label(filters, text="Year in catalog range").grid(row=0, column=3, sticky="w")
        year = ttk.Entry(filters, textvariable=self.year_var, width=10)
        year.grid(row=1, column=3, sticky="ew", padx=(0, 10))
        year.bind("<Return>", lambda _: self.search())
        self.type_box = self._combo(filters, "Type", self.type_var, 4, 22)
        ttk.Button(filters, text="Search", command=self.search).grid(row=1, column=5, padx=4)
        ttk.Button(filters, text="Reset", command=self.reset).grid(row=1, column=6, padx=4)
        ttk.Checkbutton(filters, text="Local files only", variable=self.local_var, command=self.search).grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        filters.columnconfigure(0, weight=2)
        filters.columnconfigure(2, weight=1)
        self.make_box.bind("<<ComboboxSelected>>", self.make_changed)
        self.model_box.bind("<<ComboboxSelected>>", lambda _: self.search())
        self.type_box.bind("<<ComboboxSelected>>", lambda _: self.search())
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes, padding=(12, 0, 0, 0))
        panes.add(left, weight=3)
        panes.add(right, weight=4)
        columns = ("make", "model", "trim", "years", "type", "local")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for column, title, width in zip(columns, ("Make", "Model", "Trim / description", "Years", "Type", "File"), (90, 120, 170, 100, 70, 50)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=45, stretch=column in ("model", "trim"))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        page = ttk.Frame(left, padding=(0, 8))
        page.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.prev_button = ttk.Button(page, text="‹ Previous", command=lambda: self.change_page(-1))
        self.prev_button.pack(side="left")
        self.next_button = ttk.Button(page, text="Next ›", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        ttk.Label(page, textvariable=self.page_var).pack()
        preview_header = ttk.Frame(right)
        preview_header.pack(fill="x", pady=(0, 6))
        ttk.Label(preview_header, text="Vector preview", font=("Segoe UI", 12, "bold")).pack(side="left")
        self.export_button = ttk.Button(preview_header, text="Export SVG preview", command=self.export_current, state="disabled")
        self.export_button.pack(side="right")
        ttk.Button(preview_header, text="Cut / test…", command=self.open_cut_dialog).pack(side="right", padx=6)
        ttk.Button(preview_header, text="Fit", command=self.fit_preview).pack(side="right", padx=8)
        ttk.Label(right, textvariable=self.preview_notice_var, foreground="#8a4b13", wraplength=620).pack(anchor="w", pady=(0, 5))
        self.canvas = tk.Canvas(right, background="#fafbfc", highlightbackground="#ced4da", highlightthickness=1)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.resize_preview)
        self.canvas.bind("<MouseWheel>", self.zoom_preview)
        self.canvas.bind("<ButtonPress-1>", lambda event: self.canvas.scan_mark(event.x, event.y))
        self.canvas.bind("<B1-Motion>", lambda event: self.canvas.scan_dragto(event.x, event.y, gain=1))
        ttk.Label(right, text="Scroll to zoom · Drag to pan · Fit restores the view", foreground="#59626c").pack(anchor="w", pady=(6, 0))
        self.details = tk.Text(right, height=7, wrap="word", background=self.cget("background"), relief="flat", font=("Segoe UI", 9))
        self.details.pack(fill="x", pady=(8, 0))
        self.details.configure(state="disabled")
        ttk.Label(outer, text="Inspection preview only — coordinate units, orientation and physical scale are not verified.", foreground="#8a4b13").pack(anchor="w", pady=(8, 4))
        ttk.Label(outer, textvariable=self.status_var, anchor="w").pack(fill="x")

    def _combo(self, parent: ttk.Frame, label: str, variable: tk.StringVar, column: int, width: int) -> ttk.Combobox:
        ttk.Label(parent, text=label).grid(row=0, column=column, sticky="w")
        box = ttk.Combobox(parent, textvariable=variable, width=width, state="readonly")
        box.grid(row=1, column=column, sticky="ew", padx=(0, 10))
        return box

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
            self.model_box["values"] = [""] + self.catalog.choices("model", self.make_var.get())
            self.type_names = self.catalog.type_names()
            self.type_codes = {f"{code} · {self.type_names[code]}" if code in self.type_names else code: code
                               for code in self.catalog.choices("pattern_type")}
            self.type_var.set("")
            self.type_box["values"] = [""] + list(self.type_codes)
            self.search()
        except (OSError, sqlite3.Error, ValueError) as error:
            if self.catalog:
                self.catalog.close()
                self.catalog = None
            self.asset_warnings = {}
            self.tree.delete(*self.tree.get_children())
            if isinstance(error, MissingDataError):
                self.status_var.set("Pattern data missing — extract the separate data ZIP; see README.md.")
                self.clear_preview(DATA_SETUP_HELP)
                self.set_details(str(error))
            else:
                self.status_var.set(f"Catalog unavailable: {error}")
                self.clear_preview("Choose a valid recovered catalog to begin.")

    def make_changed(self, _event: object = None) -> None:
        self.model_var.set("")
        if self.catalog:
            self.model_box["values"] = [""] + self.catalog.choices("model", self.make_var.get())
        self.search()

    def debounce_search(self, event: tk.Event) -> None:
        if event.keysym in ("Tab", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        if self.search_after:
            self.after_cancel(self.search_after)
        self.search_after = self.after(350, self.search)

    def get_filters(self) -> Filters:
        text = self.year_var.get().strip()
        if text and (not text.isdigit() or not 1800 <= int(text) <= 2200):
            raise ValueError("Enter a year between 1800 and 2200, or leave Year empty")
        return Filters(self.query_var.get(), self.make_var.get(), self.model_var.get(), int(text) if text else None, self.type_codes.get(self.type_var.get(), self.type_var.get()), self.local_var.get())

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
                self.tree.insert("", "end", iid=key, values=(row["make"] or "", row["model"] or "", row["trim"] or row["description"] or "", year_label(row), row["pattern_type"] or "", "Yes" if has_local_file(row, self.local_names) else "—"))
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
        for variable in (self.query_var, self.make_var, self.model_var, self.year_var, self.type_var):
            variable.set("")
        self.make_changed()

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
        self.set_details("")

    def set_details(self, text: str) -> None:
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def selection_changed(self, _event: object = None) -> None:
        selected = self.tree.selection()
        if not selected or selected[0] not in self.rows:
            return
        row = self.rows[selected[0]]
        self.clear_preview("Loading local pattern…")
        description = f"{row['make'] or ''} {row['model'] or ''} · {row['trim'] or ''} · {year_label(row)}\nID: {row['id']}\n{row['description'] or ''}"
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
            self.preview_notice_var.set("All geometry records decoded; physical scale remains unverified." if complete else "PARTIAL PREVIEW — unsupported geometry may be omitted. SVG export is disabled.")
            detail = f"{description}\n{len(pattern.paths):,} paths · {sum(len(v.points) for v in pattern.paths):,} stored points · {len(pattern.group_path_counts)} groups\nFile: {path.name} · SHA-256: {pattern.sha256[:20]}…"
            if pattern.warnings:
                detail += "\nParser notes: " + "; ".join(pattern.warnings)
            self.set_details(detail)
            self.fit_preview()
        except (OSError, PatternError, ValueError) as error:
            self.clear_preview(str(error))
            self.set_details(description)
        warning = warning_for_asset(self.asset_warnings, row["asset_id"])
        if warning:
            geometry_notice = self.preview_notice_var.get()
            self.preview_notice_var.set("COMPATIBILITY NOTE: " + warning + ("\n" + geometry_notice if geometry_notice else ""))

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
            messagebox.showinfo("SVG preview saved", f"Saved a new file:\n{path}\n\nPhysical scale and cutting suitability remain unverified.")
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
            messagebox.showerror("Cut / test setup", str(error), parent=self)

    def close(self) -> None:
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
