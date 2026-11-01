"""Reviewed cut-job preparation UI. Opening this dialog never sends a job."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import threading
from types import SimpleNamespace

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:
    tk = None
    filedialog = messagebox = ttk = None

from tools.patterns.fvd_pattern import VectorPath
from .driver_catalog import get_driver, get_profile, search_profiles
from . import transports, usb_transport


def number(text: str, label: str, *, optional: bool = False, minimum: float = 0,
           allow_minimum: bool = False) -> float | None:
    text = str(text).strip()
    if not text and optional:
        return None
    try:
        value = float(text)
    except ValueError as error:
        raise ValueError(f"Enter {label}") from error
    if not math.isfinite(value) or value < minimum or (value == minimum and not allow_minimum):
        comparison = "at least" if allow_minimum else "greater than"
        raise ValueError(f"{label} must be finite and {comparison} {minimum:g}")
    return value


def integer(text: str, label: str, *, optional: bool = False, minimum: int = 1) -> int | None:
    text = str(text).strip()
    if not text and optional:
        return None
    if not re.fullmatch(r"[0-9]+", text) or int(text) < minimum:
        raise ValueError(f"{label} must be a whole number of at least {minimum}")
    return int(text)


def calibration_factor(known_width_mm: str, source_width: float) -> float:
    known = number(known_width_mm, "the known selected width in millimeters")
    if not math.isfinite(source_width) or source_width <= 0:
        raise ValueError("Selected paths do not have a usable unrotated width")
    return known / source_width


def synthetic_rectangle(width_mm: float, height_mm: float):
    """A standalone closed millimeter rectangle, independent of source patterns."""
    if not all(math.isfinite(value) and value > 0 for value in (width_mm, height_mm)):
        raise ValueError("Test rectangle dimensions must be finite and positive")
    points = ((0., 0., 0, 0), (width_mm, 0., 4, 0), (width_mm, height_mm, 4, 0),
              (0., height_mm, 4, 0), (0., 0., 4, 0))
    return SimpleNamespace(source="Synthetic millimeter rectangle", sha256=None,
                           complete_geometry=True, paths=(VectorPath(0, 0, points, b""),), warnings=())


def geometry_kwargs(values: dict, selected_indices: tuple[int, ...], synthetic: bool) -> dict:
    if not selected_indices:
        raise ValueError("Select at least one vector path")
    return {
        "mm_per_source_unit": 1.0 if synthetic else number(values["unit_scale"], "millimeters per source unit; no source scale is assumed"),
        "scale": number(values["scale_percent"], "scale percent") / 100,
        "flatten_tolerance_mm": number(values["tolerance"], "curve tolerance in millimeters"),
        "selected_path_indices": selected_indices,
        "inner_first": bool(values["inner_first"]),
        "order": values["order"],
        "rotation_degrees": number(values["rotation"], "rotation", allow_minimum=True),
        "mirror_x": bool(values["mirror_x"]), "mirror_y": bool(values["mirror_y"]),
        "copies": integer(values["copies"], "copies"),
        "copy_columns": integer(values["columns"], "copy columns"),
        "copy_spacing_mm": number(values["spacing"], "copy spacing", allow_minimum=True),
        "weed_border_mm": number(values["weed"], "weed border", optional=True),
        "passes": integer(values["passes"], "passes"),
        "media_width_mm": number(values["media_width"], "media width", optional=True),
        "media_height_mm": number(values["media_height"], "media height", optional=True),
    }


def preparation_kwargs(values: dict, selected_indices: tuple[int, ...], synthetic: bool) -> tuple[dict, dict | None, dict]:
    geometry = geometry_kwargs(values, selected_indices, synthetic)
    review = dict(geometry)
    if not values.get("paneling", False):
        return geometry, None, review
    width, height = geometry["media_width_mm"], geometry["media_height_mm"]
    if width is None or height is None:
        raise ValueError("Manual paneling requires explicit usable media width and length as the page size")
    page = integer(values.get("panel_page", ""), "panel page number")
    panel = {"width_mm": width, "height_mm": height,
             "overlap_mm": number(values.get("panel_overlap", "0"), "panel overlap", allow_minimum=True),
             "selected_pages": (page - 1,), "order": "row-major"}
    geometry["media_width_mm"] = geometry["media_height_mm"] = None
    return geometry, panel, review


@dataclass(frozen=True)
class Destination:
    kind: str
    parameters: dict
    description: str


def destination_from_values(kind: str, values: dict) -> Destination:
    if kind == "File":
        text = str(values.get("file", "")).strip()
        if not text:
            raise ValueError("Choose a new job-output file")
        path = Path(text).expanduser()
        if path.exists():
            raise ValueError("The output file already exists; choose a new filename")
        return Destination("file", {"path": path}, str(path))
    if kind == "Windows RAW queue":
        name = str(values.get("queue", "")).strip()
        if not name:
            raise ValueError("Select or enter an installed Windows queue")
        return Destination("windows_raw", {"queue_name": name}, name)
    if kind == "TCP":
        host = str(values.get("host", "")).strip()
        if not host or "\x00" in host:
            raise ValueError("Enter the device host or IP address")
        port = integer(values.get("port", ""), "the explicit TCP port")
        if port > 65535:
            raise ValueError("TCP port must be between 1 and 65535")
        timeout = number(values.get("timeout", ""), "timeout seconds")
        return Destination("tcp", {"host": host, "port": port, "timeout": timeout}, f"{host}:{port}")
    if kind == "Serial COM":
        port = str(values.get("com", "")).strip().upper()
        if not re.fullmatch(r"COM[1-9][0-9]*", port):
            raise ValueError("Enter an explicit COM port such as COM3")
        parameters = {"port": port, "baud": integer(values.get("baud", ""), "baud rate"),
                      "parity": values["parity"], "stop_bits": float(values["stop_bits"]),
                      "flow_control": values["flow"], "data_bits": integer(values["data_bits"], "data bits"),
                      "timeout": number(values.get("timeout", ""), "timeout seconds")}
        transports.serial_dcb(parameters["baud"], parameters["parity"], parameters["stop_bits"],
                              parameters["flow_control"], parameters["data_bits"])
        return Destination("windows_serial", parameters,
                           f"{port} · {parameters['baud']} baud · {parameters['data_bits']}{parameters['parity']}/{parameters['stop_bits']:g} · {parameters['flow_control']}")
    if kind == "Direct USB":
        identity = []
        for key in ("usb_vid", "usb_pid"):
            text = str(values.get(key, "")).strip()
            if text.lower().startswith("0x"):
                text = text[2:]
            if not re.fullmatch(r"[0-9a-fA-F]{1,4}", text):
                raise ValueError("Select the actual USB interface and explicit hexadecimal VID/PID")
            identity.append(int(text, 16))
        pipe_number = integer(values.get("usb_pipe", ""), "USB pipe number", optional=True, minimum=0)
        path = str(values.get("usb_path", "")).strip()
        usb_transport.validated_device_path(path, identity[0], identity[1], pipe_number)
        parameters = {"device_path": path, "vid": identity[0], "pid": identity[1],
                      "pipe_number": pipe_number, "timeout": number(values.get("timeout", ""), "timeout seconds")}
        return Destination("windows_usb", parameters, f"Advanced USB VID {identity[0]:04X} PID {identity[1]:04X} · {path}" + (f" · Pipe {pipe_number}" if pipe_number is not None else ""))
    raise ValueError("Choose an explicit file, queue, TCP, serial, or advanced USB destination")


def deliver(payload: bytes, destination: Destination, *, log_path: Path, cancel_event=None):
    """No automatic selection, fallback, retry, discovery, or payload conversion."""
    arguments = {"log_path": log_path, "cancel_event": cancel_event, **destination.parameters}
    if destination.kind == "file":
        arguments["path"].parent.mkdir(parents=True, exist_ok=True)
        return transports.write_job_file(payload=payload, **arguments)
    functions = {"tcp": transports.send_tcp, "windows_raw": transports.send_windows_raw,
                 "windows_serial": transports.send_windows_serial, "windows_usb": usb_transport.send_usb}
    if destination.kind not in functions:
        raise ValueError("Unsupported output destination")
    return functions[destination.kind](payload, **arguments)


def runtime_candidates(workspace: Path) -> list[Path]:
    candidates = [workspace / ".runtime" / "vendor"]
    program_files = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    candidates.append(Path(program_files) / "FilmVinylDesigns" / "ClientV2")
    return [path for path in candidates if path.is_dir()]


def profile_labels(profiles: list[dict], verification: dict | None = None) -> dict[str, str]:
    verification = verification or {}
    result = {}
    for profile in profiles:
        receipt = verification.get(profile["key"], {})
        marker = "capture verified" if isinstance(receipt, dict) and receipt.get("status") == "verified_capture" else "catalog only"
        result[f"{profile['name']}  [{profile['key']}] · {marker}"] = profile["key"]
    return result


def owner_profile_label(labels: dict[str, str]) -> str:
    """The owner confirmed FC9000-140; retain the exact extracted profile key."""
    return next((label for label, key in labels.items() if key == "graphtec:53"), "")


def command_preview(payload: bytes, limit: int = 8192) -> str:
    result = "".join(chr(value) if 32 <= value <= 126 or value == 10 else
                     "\\r" if value == 13 else "\\t" if value == 9 else f"\\x{value:02x}"
                     for value in payload[:limit])
    return result + ("\n[preview truncated; payload unchanged]" if len(payload) > limit else "")


def native_bounds(metadata: dict) -> tuple[float, float, float, float] | None:
    raw = metadata.get("native_bounds_mm")
    if raw is None:
        return None
    if not isinstance(raw, (tuple, list)) or len(raw) != 4:
        raise ValueError("Compiler native output bounds are invalid")
    bounds = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in bounds) or bounds[2] < bounds[0] or bounds[3] < bounds[1]:
        raise ValueError("Compiler native output bounds are invalid")
    return bounds


def validate_device_bounds(destination: Destination, options: dict, metadata: dict) -> None:
    if destination.kind == "file":
        return
    width, height = options.get("media_width_mm"), options.get("media_height_mm")
    if width is None or height is None or not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in (width, height)):
        raise ValueError("Device output requires the actual usable media width and length in millimeters")
    bounds = native_bounds(metadata)
    if bounds is None:
        raise ValueError("Native emitted-coordinate bounds are unavailable; save to File for inspection instead")
    xmin, ymin, xmax, ymax = bounds
    if xmin < -1e-6 or ymin < -1e-6 or xmax > width + 1e-6 or ymax > height + 1e-6:
        raise ValueError(f"Native output bounds X {xmin:g}…{xmax:g}, Y {ymin:g}…{ymax:g} mm exceed the usable media {width:g} × {height:g} mm")


class CutDialog(tk.Toplevel if tk else object):
    def __init__(self, parent, *, pattern=None, workspace: Path | None = None,
                 source_label: str = "Selected pattern", compatibility_note: str = ""):
        super().__init__(parent)
        self.workspace = Path(workspace) if workspace else Path(__file__).resolve().parents[1]
        self.pattern, self.source_label = pattern, source_label
        self.compatibility_note = compatibility_note
        self.title("Prepare cut / plot job")
        window_width = min(1180, max(700, self.winfo_screenwidth() - 80))
        window_height = min(900, max(450, self.winfo_screenheight() - 100))
        self.geometry(f"{window_width}x{window_height}")
        self.minsize(min(960, window_width), min(600, window_height))
        self.transient(parent)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False
        self.busy = False
        self.prepared = self.compiled = self.review_destination = None
        self.review_fingerprint = None
        self.review_geometry_options = None
        self.events = queue.Queue()
        self.usb_devices = {}
        self.cancel_event = threading.Event()
        self.variables = {}
        defaults = {"source": "pattern" if pattern and getattr(pattern, "complete_geometry", False) else "test",
                    "unit_scale": "", "known_width": "", "test_width": "20", "test_height": "20",
                    "scale_percent": "100", "tolerance": "0.05", "rotation": "0", "copies": "1", "columns": "1",
                    "spacing": "0", "weed": "", "passes": "1", "media_width": "", "media_height": "",
                    "order": "unsorted", "speed": "", "force": "", "acceleration": "", "tool": "1",
                    "runtime": str(next(iter(runtime_candidates(self.workspace)), self.workspace / ".runtime" / "vendor")),
                    "profile": "", "profile_search": "", "file": str(self.workspace / "exports" / f"cut-job-{datetime.now():%Y%m%d-%H%M%S}.plt"),
                    "queue": "", "host": "", "port": "", "timeout": "10", "com": "", "baud": "9600", "parity": "N",
                    "stop_bits": "1", "data_bits": "8", "flow": "none",
                    "usb_path": "", "usb_vid": "", "usb_pid": "", "usb_pipe": ""}
        defaults.update(panel_page="1", panel_overlap="0")
        for key, value in defaults.items():
            self.variables[key] = tk.StringVar(value=value)
        for key, value in {"inner_first": True, "mirror_x": False, "mirror_y": False,
                           "rotate_axes": True, "return_origin": True, "feed": False, "paneling": False}.items():
            self.variables[key] = tk.BooleanVar(value=value)
        self.status = tk.StringVar(value="Choose the exact model, paths, physical scale, and destination; then prepare the output preview.")
        self.review_summary = tk.StringVar(value="No generated job has been reviewed.")
        self.mode_notice = tk.StringVar()
        verification_path = self.workspace / "docs" / "printing-native-verification.json"
        try:
            verification = json.loads(verification_path.read_text(encoding="utf-8")).get("profiles", {})
        except (OSError, ValueError):
            verification = {}
        if not isinstance(verification, dict):
            verification = {}
        self.profiles = profile_labels(search_profiles(), verification)
        self._build()
        for key, variable in self.variables.items():
            if key != "profile_search":
                variable.trace_add("write", self.profile_changed if key == "profile" else self.invalidate)
        self.variables["profile_search"].trace_add("write", self.filter_profiles)
        self.refresh_paths()
        self.variables["profile"].set(owner_profile_label(self.profiles))
        self.after(100, self.poll)

    def _build(self):
        # Keep action/status controls visible while the full form remains
        # reachable on laptop-sized displays and larger Windows text scales.
        footer = ttk.Frame(self, padding=(12, 6, 12, 10))
        footer.pack(side="bottom", fill="x")
        form = ttk.Frame(self)
        form.pack(fill="both", expand=True)
        self.form_canvas = tk.Canvas(form, highlightthickness=0, borderwidth=0)
        vertical = ttk.Scrollbar(form, orient="vertical", command=self.form_canvas.yview)
        horizontal = ttk.Scrollbar(form, orient="horizontal", command=self.form_canvas.xview)
        self.form_canvas.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.form_canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(0, weight=1)
        form.rowconfigure(0, weight=1)
        outer = ttk.Frame(self.form_canvas, padding=12)
        form_window = self.form_canvas.create_window(0, 0, anchor="nw", window=outer)
        def resize_form(_event=None):
            width = max(self.form_canvas.winfo_width(), outer.winfo_reqwidth())
            self.form_canvas.itemconfigure(form_window, width=width)
            self.form_canvas.configure(scrollregion=self.form_canvas.bbox("all"))
        outer.bind("<Configure>", resize_form)
        self.form_canvas.bind("<Configure>", resize_form)
        ttk.Label(outer, text="Prepare a cut / plot job", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Compiler uses original installed driver DLLs. Capture verified means generated bytes were checked; no model is hardware certified.", foreground="#72501e").pack(anchor="w", pady=(4, 8))
        if self.compatibility_note:
            ttk.Label(outer, text="Pattern compatibility: " + self.compatibility_note, foreground="#923d1a", wraplength=1100).pack(anchor="w", pady=(0, 8))
        driver = ttk.LabelFrame(outer, text="Exact model and compiler runtime", padding=8)
        driver.pack(fill="x")
        ttk.Label(driver, text="Filter models").grid(row=0, column=0, sticky="w")
        ttk.Entry(driver, textvariable=self.variables["profile_search"], width=20).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(driver, text="Model / exact profile key").grid(row=0, column=1, sticky="w")
        self.profile_box = ttk.Combobox(driver, textvariable=self.variables["profile"], values=list(self.profiles), state="readonly", width=60)
        self.profile_box.grid(row=1, column=1, columnspan=2, sticky="ew")
        ttk.Label(driver, text="Driver DLL folder").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(driver, textvariable=self.variables["runtime"]).grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Button(driver, text="Browse…", command=self.choose_runtime).grid(row=2, column=2, padx=(8, 0), pady=(6, 0))
        ttk.Label(driver, textvariable=self.mode_notice, foreground="#72501e", wraplength=1080).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        driver.columnconfigure(1, weight=1)
        main = ttk.Frame(outer)
        main.pack(fill="x", pady=8)
        paths = ttk.LabelFrame(main, text="Source and selected vector paths", padding=8)
        paths.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Radiobutton(paths, text=self.source_label, variable=self.variables["source"], value="pattern", command=self.refresh_paths,
                        state="normal" if self.pattern and getattr(self.pattern, "complete_geometry", False) else "disabled").pack(anchor="w")
        test = ttk.Frame(paths)
        test.pack(fill="x", pady=4)
        ttk.Radiobutton(test, text="Synthetic rectangle (mm)", variable=self.variables["source"], value="test", command=self.refresh_paths).pack(side="left")
        ttk.Entry(test, textvariable=self.variables["test_width"], width=6).pack(side="left", padx=4)
        ttk.Label(test, text="×").pack(side="left")
        ttk.Entry(test, textvariable=self.variables["test_height"], width=6).pack(side="left", padx=4)
        path_list = ttk.Frame(paths)
        path_list.pack(fill="both", expand=True)
        self.path_list = tk.Listbox(path_list, selectmode="extended", exportselection=False, height=5)
        self.path_list.pack(side="left", fill="both", expand=True)
        path_scroll = ttk.Scrollbar(path_list, orient="vertical", command=self.path_list.yview)
        path_scroll.pack(side="right", fill="y")
        self.path_list.configure(yscrollcommand=path_scroll.set)
        self.path_list.bind("<<ListboxSelect>>", self.invalidate)
        controls = ttk.Frame(paths)
        controls.pack(fill="x", pady=(5, 0))
        ttk.Button(controls, text="Select all", command=self.select_all).pack(side="left")
        ttk.Button(controls, text="Clear", command=self.clear_paths).pack(side="left", padx=5)
        ttk.Label(controls, text="Ctrl/Shift selects multiple paths").pack(side="left", padx=5)
        geometry = ttk.LabelFrame(main, text="Dimensions and geometry", padding=8)
        geometry.pack(side="right", fill="both", expand=True)
        self._field(geometry, "mm per source unit (required for patterns)", "unit_scale", 0, 0)
        self._field(geometry, "Known unrotated selected width (mm)", "known_width", 1, 0)
        ttk.Button(geometry, text="Use known width", command=self.calibrate_width).grid(row=1, column=2, padx=4)
        self._field(geometry, "Scale (%)", "scale_percent", 2, 0)
        self._field(geometry, "Curve tolerance (mm)", "tolerance", 3, 0)
        self._field(geometry, "Usable media width (mm; required for device)", "media_width", 4, 0)
        self._field(geometry, "Usable media length (mm; required for device)", "media_height", 5, 0)
        geometry.columnconfigure(1, weight=1)
        settings = ttk.Notebook(outer)
        settings.pack(fill="x")
        transforms = ttk.Frame(settings, padding=8)
        tools = ttk.Frame(settings, padding=8)
        panels = ttk.Frame(settings, padding=8)
        settings.add(transforms, text="Layout and path order")
        settings.add(tools, text="Driver tool options")
        settings.add(panels, text="Manual paneling")
        for index, (label, key) in enumerate((("Copies", "copies"), ("Columns", "columns"), ("Spacing (mm)", "spacing"), ("Weed border (mm; optional)", "weed"), ("Passes", "passes"))):
            self._field(transforms, label, key, index // 3, (index % 3) * 2, width=8)
        ttk.Label(transforms, text="Rotation").grid(row=1, column=4, sticky="w")
        ttk.Combobox(transforms, textvariable=self.variables["rotation"], values=("0", "90", "180", "270"), state="readonly", width=8).grid(row=1, column=5, padx=5)
        for column, (text, key) in enumerate((("Mirror X", "mirror_x"), ("Mirror Y", "mirror_y"), ("Inner paths first", "inner_first"))):
            ttk.Checkbutton(transforms, text=text, variable=self.variables[key]).grid(row=2, column=column * 2, columnspan=2, sticky="w", pady=(5, 0))
        ttk.Label(transforms, text="Path order").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(transforms, textvariable=self.variables["order"], values=("unsorted", "start-nearest", "end-nearest"), state="readonly", width=18).grid(row=3, column=1, columnspan=2, sticky="w")
        for index, (label, key) in enumerate((("Speed (native units; optional)", "speed"), ("Force (native units; optional)", "force"), ("Acceleration (native units; optional)", "acceleration"), ("Tool", "tool"))):
            self._field(tools, label, key, index // 2, (index % 2) * 2, width=12)
        for column, (text, key) in enumerate((("Direct X/Y axes (native Rotate90)", "rotate_axes"), ("Return to origin", "return_origin"), ("Feed after job", "feed"))):
            checkbox = ttk.Checkbutton(tools, text="Feed (not validated)" if key == "feed" else text, variable=self.variables[key])
            checkbox.grid(row=2, column=column * 2, columnspan=2, sticky="w", pady=5)
            if key == "feed":
                checkbox.configure(state="disabled")
            elif key == "rotate_axes":
                self.axis_checkbox = checkbox
        ttk.Label(tools, text="Blank values add no host override; model initialization can still emit its native defaults.", foreground="#72501e").grid(row=3, column=0, columnspan=6, sticky="w")
        ttk.Checkbutton(panels, text="Enable manual paneling", variable=self.variables["paneling"]).grid(row=0, column=0, columnspan=2, sticky="w")
        self._field(panels, "Selected page (1 based)", "panel_page", 1, 0, width=8)
        self._field(panels, "Overlap (mm)", "panel_overlap", 1, 2, width=8)
        ttk.Button(panels, text="Previous page", command=lambda: self.change_panel_page(-1)).grid(row=1, column=4, padx=4)
        ttk.Button(panels, text="Next page", command=lambda: self.change_panel_page(1)).grid(row=1, column=5, padx=4)
        ttk.Label(panels, text="Usable media width/length define each page. Prepare, review and send one selected page at a time. Position the media/origin yourself; there is no automatic advance or feed. Use a new file name for each saved page.", foreground="#72501e", wraplength=1080).grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        self.transport_tabs = ttk.Notebook(outer)
        self.transport_tabs.pack(fill="x", pady=8)
        for title in ("File", "Windows RAW queue", "TCP", "Serial COM", "Direct USB"):
            frame = ttk.Frame(self.transport_tabs, padding=8)
            self.transport_tabs.add(frame, text=title)
            if title == "File":
                self._field(frame, "New output file", "file", 0, 0, width=90)
                ttk.Button(frame, text="Browse…", command=self.choose_file).grid(row=0, column=2, padx=6)
                frame.columnconfigure(1, weight=1)
            elif title == "Windows RAW queue":
                ttk.Label(frame, text="Installed queue").grid(row=0, column=0)
                self.queue_box = ttk.Combobox(frame, textvariable=self.variables["queue"], width=70)
                self.queue_box.grid(row=0, column=1, padx=6)
                ttk.Button(frame, text="List installed queues", command=self.list_queues).grid(row=0, column=2)
                ttk.Label(frame, text="Use the actual manufacturer queue when installed. Direct generic USB is a separate advanced, driver-dependent option.", foreground="#72501e").grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 0))
            elif title == "TCP":
                self._field(frame, "Host / IP", "host", 0, 0, width=30)
                self._field(frame, "Port (required)", "port", 0, 2, width=8)
                self._field(frame, "Timeout (s)", "timeout", 0, 4, width=8)
                ttk.Label(frame, text="Use the verified device/driver port; no Graphtec port is assumed.", foreground="#72501e").grid(row=1, column=0, columnspan=6, sticky="w", pady=(5, 0))
            elif title == "Serial COM":
                self._field(frame, "Port", "com", 0, 0, width=10)
                self._field(frame, "Baud", "baud", 0, 2, width=10)
                for column, label, key, choices in ((4, "Data bits", "data_bits", ("5", "6", "7", "8")), (6, "Parity", "parity", ("N", "O", "E", "M", "S"))):
                    ttk.Label(frame, text=label).grid(row=0, column=column)
                    ttk.Combobox(frame, textvariable=self.variables[key], values=choices, state="readonly", width=6).grid(row=0, column=column + 1, padx=5)
                ttk.Label(frame, text="Stop bits").grid(row=1, column=0, pady=5)
                ttk.Combobox(frame, textvariable=self.variables["stop_bits"], values=("1", "1.5", "2"), state="readonly", width=8).grid(row=1, column=1)
                ttk.Label(frame, text="Flow control").grid(row=1, column=2)
                ttk.Combobox(frame, textvariable=self.variables["flow"], values=("none", "rtscts", "xonxoff", "dsrdtr"), state="readonly", width=12).grid(row=1, column=3)
                self._field(frame, "Timeout (s)", "timeout", 1, 4, width=8)
            else:
                ttk.Label(frame, text="Explicit device interface").grid(row=0, column=0, sticky="w")
                self.usb_box = ttk.Combobox(frame, textvariable=self.variables["usb_path"], width=85)
                self.usb_box.grid(row=0, column=1, columnspan=5, sticky="ew", padx=5)
                self.usb_box.bind("<<ComboboxSelected>>", self.usb_selected)
                ttk.Button(frame, text="List USB interfaces", command=self.list_usb).grid(row=0, column=6)
                self._field(frame, "VID (hex)", "usb_vid", 1, 0, width=7)
                self._field(frame, "PID (hex)", "usb_pid", 1, 2, width=7)
                self._field(frame, "Pipe (optional; no guess)", "usb_pipe", 1, 4, width=6)
                self._field(frame, "Timeout (s)", "timeout", 2, 0, width=7)
                ttk.Button(frame, text="Recheck pending USB cleanup", command=self.recheck_usb_cleanup).grid(row=2, column=2, columnspan=3, sticky="w", padx=5)
                ttk.Label(frame, text="Advanced / hardware-unverified. Requires an installed function driver that accepts this raw device path; not universal FC9000 USB support.", foreground="#72501e", wraplength=1000).grid(row=3, column=0, columnspan=7, sticky="w", pady=(5, 0))
        self.transport_tabs.bind("<<NotebookTabChanged>>", self.invalidate)
        preview = ttk.Frame(outer)
        preview.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(preview, height=150, background="#fafbfc", highlightthickness=1, highlightbackground="#ced4da")
        self.canvas.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.output_text = tk.Text(preview, height=7, width=54, wrap="word", state="disabled", font=("Consolas", 9))
        self.output_text.pack(side="right", fill="both", expand=True)
        summary_label = ttk.Label(footer, textvariable=self.review_summary, wraplength=1120, foreground="#2c4a61")
        summary_label.pack(anchor="w", pady=(0, 3))
        buttons = ttk.Frame(footer)
        buttons.pack(fill="x", pady=(4, 0))
        self.prepare_button = ttk.Button(buttons, text="Prepare and review output", command=self.prepare)
        self.prepare_button.pack(side="left")
        self.send_button = ttk.Button(buttons, text="Save reviewed job", command=self.send, state="disabled")
        self.send_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="Cancel operation", command=self.cancel).pack(side="right")
        ttk.Button(buttons, text="Close", command=self.close).pack(side="right", padx=8)
        status_label = ttk.Label(footer, textvariable=self.status, wraplength=1120)
        status_label.pack(anchor="w", pady=(7, 0))
        footer.bind("<Configure>", lambda event: (summary_label.configure(wraplength=max(300, event.width - 24)),
                                                  status_label.configure(wraplength=max(300, event.width - 24))))
        def scroll_form(event):
            self.form_canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
            return "break"
        def bind_scrolling(widget):
            if not isinstance(widget, (tk.Text, tk.Listbox, ttk.Combobox, ttk.Scrollbar)):
                widget.bind("<MouseWheel>", scroll_form, add="+")
            for child in widget.winfo_children():
                bind_scrolling(child)
        bind_scrolling(outer)
        self.form_canvas.bind("<MouseWheel>", scroll_form)

    def _field(self, parent, label, key, row, column, width=15):
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=(0, 5), pady=2)
        widget = ttk.Entry(parent, textvariable=self.variables[key], width=width)
        widget.grid(row=row, column=column + 1, sticky="ew", padx=(0, 8), pady=2)
        return widget

    def values(self):
        return {key: variable.get() for key, variable in self.variables.items()}

    def fingerprint(self):
        return json.dumps({"values": self.values(), "paths": list(self.path_list.curselection()),
                           "transport": self.transport_tabs.tab(self.transport_tabs.select(), "text")}, sort_keys=True)

    def invalidate(self, *_args):
        self.prepared = self.compiled = self.review_destination = None
        self.review_fingerprint = None
        self.review_geometry_options = None
        self.send_button.configure(state="disabled")
        self.review_summary.set("Settings or selection changed. Prepare a new output preview before sending.")

    def profile_changed(self, *_args):
        profile_key = self.profiles.get(self.variables["profile"].get())
        profile = get_profile(profile_key) if profile_key else None
        driver = get_driver(profile["driver_id"]) if profile else None
        is_dmpl = driver is not None and "DMPL" in driver.get("protocol_families", ())
        self.variables["rotate_axes"].set(not is_dmpl)
        self.axis_checkbox.configure(state="disabled" if is_dmpl else "normal")
        self.mode_notice.set("Owner-confirmed FC9000-140. Before a physical trial, verify cutter COMMAND is HP-GL; the current hardware mode is unconfirmed. Profile selection does not contact the cutter."
                             if profile_key == "graphtec:53" else "Before physical output, verify the device command mode matches the generated job.")
        self.invalidate()

    def filter_profiles(self, *_args):
        query = self.variables["profile_search"].get().casefold().strip()
        self.profile_box["values"] = [label for label in self.profiles if query in label.casefold()]

    def refresh_paths(self):
        self.path_list.delete(0, "end")
        synthetic = self.variables["source"].get() == "test"
        if synthetic:
            self.path_list.insert("end", "Path 1 · synthetic closed rectangle")
        elif self.pattern:
            for index, path in enumerate(self.pattern.paths):
                self.path_list.insert("end", f"Path {index + 1} · group {path.group} · {len(path.points):,} points")
        self.select_all()

    def select_all(self):
        self.path_list.selection_set(0, "end")
        self.invalidate()

    def clear_paths(self):
        self.path_list.selection_clear(0, "end")
        self.invalidate()

    def change_panel_page(self, direction):
        try:
            page = integer(self.variables["panel_page"].get(), "panel page number")
            self.variables["panel_page"].set(str(max(1, page + direction)))
        except ValueError as error:
            self.status.set(str(error))

    def choose_runtime(self):
        path = filedialog.askdirectory(parent=self, title="Choose original driver DLL folder", initialdir=self.variables["runtime"].get())
        if path:
            self.variables["runtime"].set(path)

    def choose_file(self):
        path = filedialog.asksaveasfilename(parent=self, title="Save a new generated job", initialdir=self.workspace / "exports", defaultextension=".plt", confirmoverwrite=False,
                                          filetypes=(("Plotter job", "*.plt"), ("All files", "*.*")))
        if path:
            self.variables["file"].set(path)

    def calibrate_width(self):
        try:
            if self.variables["source"].get() != "pattern" or not self.pattern:
                raise ValueError("Synthetic rectangle dimensions are already in millimeters")
            from .geometry import source_bounds
            selected = tuple(self.path_list.curselection())
            if not selected:
                raise ValueError("Select paths before calibrating width")
            xmin, ymin, xmax, ymax = source_bounds(self.pattern, selected)
            factor = calibration_factor(self.variables["known_width"].get(), xmax - xmin)
            self.variables["unit_scale"].set(f"{factor:.12g}")
            self.status.set("Conversion calculated from the supplied unrotated selected width; review the resulting millimeter dimensions.")
        except Exception as error:
            messagebox.showerror("Width calibration", str(error), parent=self)

    def prepare(self):
        if self.busy:
            return
        try:
            values = self.values()
            if values["profile"] not in self.profiles:
                raise ValueError("Choose the exact model/profile; an informal device width is insufficient")
            profile_key = self.profiles[values["profile"]]
            runtime = Path(values["runtime"]).expanduser()
            if not runtime.is_dir():
                raise ValueError("Choose an existing original driver DLL folder; see README.md for runtime setup")
            synthetic = values["source"] == "test"
            pattern = (synthetic_rectangle(number(values["test_width"], "test width"), number(values["test_height"], "test height"))
                       if synthetic else self.pattern)
            if pattern is None or not getattr(pattern, "complete_geometry", False):
                raise ValueError("This pattern has incomplete geometry. Choose a complete pattern or the synthetic test rectangle")
            options, panel_options, review_options = preparation_kwargs(values, tuple(self.path_list.curselection()), synthetic)
            tool_values = {"speed": integer(values["speed"], "speed", optional=True),
                           "force": integer(values["force"], "force", optional=True),
                           "acceleration": number(values["acceleration"], "acceleration", optional=True),
                           "tool": integer(values["tool"], "tool"), "rotate_axes": bool(values["rotate_axes"]),
                           "return_origin": bool(values["return_origin"]), "feed": bool(values["feed"])}
            destination = destination_from_values(self.transport_tabs.tab(self.transport_tabs.select(), "text"), values)
            if destination.kind != "file" and (review_options["media_width_mm"] is None or review_options["media_height_mm"] is None):
                raise ValueError("Device output requires the actual usable media width and length in millimeters")
            fingerprint = self.fingerprint()
            self.invalidate()
            self.busy = True
            self.cancel_event = threading.Event()
            self.prepare_button.configure(state="disabled")
            self.status.set("Preparing geometry and capturing original-driver output. No device is contacted during this step…")
            def work():
                try:
                    from .geometry import JobOptions, prepare_job
                    from .backend import ToolOptions, compile_job
                    prepared = prepare_job(pattern, JobOptions(**options))
                    panel_metadata = None
                    if panel_options is not None:
                        from .paneling import PanelOptions, split_panels
                        panel = split_panels(prepared, PanelOptions(**panel_options))[0]
                        if panel.is_empty:
                            raise ValueError(f"Selected page {panel.index + 1} of {panel.page_count} is blank; nothing was compiled or sent")
                        prepared = panel.job
                        panel_metadata = {"page": panel.index + 1, "page_count": panel.page_count,
                                          "row": panel.row + 1, "column": panel.column + 1,
                                          "grid_rows": panel.grid_rows, "grid_columns": panel.grid_columns,
                                          "source_origin_mm": list(panel.origin_mm),
                                          "usable_page_width_mm": panel_options["width_mm"],
                                          "usable_page_height_mm": panel_options["height_mm"],
                                          "overlap_mm": panel_options["overlap_mm"], "automatic_advance": False}
                    compiled = compile_job(prepared, profile_key, ToolOptions(**tool_values), runtime)
                    if panel_metadata is not None:
                        compiled = replace(compiled, metadata={**compiled.metadata, "manual_panel": panel_metadata})
                    validate_device_bounds(destination, review_options, compiled.metadata)
                    self.events.put(("prepared", prepared, compiled, destination, fingerprint, review_options))
                except Exception as error:
                    self.events.put(("error", str(error)))
            threading.Thread(target=work, daemon=True).start()
        except Exception as error:
            self.status.set(str(error))
            messagebox.showerror("Cannot prepare job", str(error), parent=self)

    def send(self):
        if self.busy or self.compiled is None or self.review_destination is None:
            return
        if self.review_fingerprint != self.fingerprint():
            self.invalidate()
            return
        try:
            validate_device_bounds(self.review_destination, self.review_geometry_options, self.compiled.metadata)
        except ValueError as error:
            self.invalidate()
            messagebox.showerror("Device bounds", str(error), parent=self)
            return
        compiled, destination = self.compiled, self.review_destination
        self.busy = True
        self.cancel_event = threading.Event()
        self.prepare_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.compiled = None  # Another send always requires a new explicit preparation.
        self.status.set("Saving output…" if destination.kind == "file" else "Sending the reviewed job once; no automatic retries or transport fallback…")
        def work():
            try:
                result = deliver(compiled.payload, destination, log_path=self.workspace / "output" / "jobs" / "transports.jsonl", cancel_event=self.cancel_event)
                self.events.put(("sent", result))
            except transports.DeliveryError as error:
                self.events.put(("sent", error.result))
            except Exception as error:
                self.events.put(("error", str(error)))
        threading.Thread(target=work, daemon=True).start()

    def list_queues(self):
        if self.busy:
            return
        self.busy = True
        self.prepare_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.status.set("Reading installed/local and previously connected Windows queues…")
        def work():
            try:
                self.events.put(("queues", transports.list_windows_queues()))
            except Exception as error:
                self.events.put(("error", str(error)))
        threading.Thread(target=work, daemon=True).start()

    def usb_selected(self, _event=None):
        device = self.usb_devices.get(self.variables["usb_path"].get())
        if device is not None:
            self.variables["usb_vid"].set(f"{device.vid:04X}" if device.vid is not None else "")
            self.variables["usb_pid"].set(f"{device.pid:04X}" if device.pid is not None else "")
            self.status.set(f"Selected metadata: {device.description}. Verify the actual interface and driver; no output device was opened.")

    def list_usb(self):
        if self.busy:
            return
        self.busy = True
        self.prepare_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.status.set("Listing present generic USB interface metadata only; no device is opened for output…")
        def work():
            try:
                self.events.put(("usb_devices", usb_transport.list_usb_devices()))
            except Exception as error:
                self.events.put(("error", str(error)))
        threading.Thread(target=work, daemon=True).start()

    def recheck_usb_cleanup(self):
        if self.busy:
            return
        self.busy = True
        self.prepare_button.configure(state="disabled")
        self.send_button.configure(state="disabled")
        self.status.set("Rechecking previously owned USB operation cleanup only; no device is opened or written…")
        def work():
            try:
                self.events.put(("usb_cleanup", usb_transport.reap_pending_usb_io()))
            except Exception as error:
                self.events.put(("error", str(error)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                event = self.events.get_nowait()
                self.busy = False
                self.prepare_button.configure(state="normal")
                if event[0] == "prepared":
                    _, prepared, compiled, destination, fingerprint, options = event
                    if self.cancel_event.is_set() or fingerprint != self.fingerprint():
                        self.invalidate()
                        self.status.set("Preparation discarded because settings changed or cancellation was requested.")
                        continue
                    self.prepared, self.compiled, self.review_destination = prepared, compiled, destination
                    self.review_fingerprint = fingerprint
                    self.review_geometry_options = options
                    self.draw_prepared(prepared)
                    metadata = getattr(compiled, "metadata", {})
                    commands = command_preview(compiled.payload)
                    bounds = native_bounds(metadata)
                    bounds_text = (f"Native emitted bounds: X {bounds[0]:.3f}…{bounds[2]:.3f}, Y {bounds[1]:.3f}…{bounds[3]:.3f} mm"
                                   if bounds is not None else "Native emitted-coordinate bounds: unavailable; device output blocked")
                    text = (f"Model: {self.variables['profile'].get()}\nDestination: {destination.description}\nPrepared geometry: {prepared.width_mm:.3f} × {prepared.height_mm:.3f} mm\n"
                            f"{bounds_text}\n"
                            f"{len(prepared.polylines):,} output paths; {prepared.point_count:,} points\nPayload: {len(compiled.payload):,} bytes\n"
                            f"SHA-256: {hashlib.sha256(compiled.payload).hexdigest()}\n\nGenerated command preview (first 8192 bytes):\n{commands}")
                    if metadata:
                        text += "\n\nCompiler metadata:\n" + json.dumps(metadata, indent=2, default=str)
                    self.output_text.configure(state="normal")
                    self.output_text.delete("1.0", "end")
                    self.output_text.insert("1.0", text)
                    self.output_text.configure(state="disabled")
                    panel_metadata = metadata.get("manual_panel")
                    panel_text = (f"Manual page {panel_metadata['page']} of {panel_metadata['page_count']} (row {panel_metadata['row']}, column {panel_metadata['column']}) · "
                                  if panel_metadata else "")
                    self.review_summary.set(f"Review: {panel_text}{prepared.width_mm:.3f} × {prepared.height_mm:.3f} mm · {len(prepared.polylines):,} paths · {bounds_text} · {destination.kind}: {destination.description}")
                    self.send_button.configure(state="normal", text="Save reviewed job" if destination.kind == "file" else "Send reviewed job once")
                    self.status.set("Review dimensions, selected paths, exact model and destination above. Sending confirms this reviewed job; physical cutter parity remains unverified.")
                elif event[0] == "sent":
                    result = event[1]
                    details = f"{result.state}: {result.bytes_sent:,}/{result.bytes_requested:,} bytes. {result.message}"
                    if result.log_error:
                        details += " Audit log error: " + result.log_error
                    if result.job_id is not None:
                        details += f" Windows job ID: {result.job_id}."
                    self.status.set(details)
                    self.review_summary.set("Output attempt finished. Prepare a new reviewed job for another attempt; no automatic retry occurred.")
                    if result.state in ("failed", "partial", "cancelled") or result.cleanup_errors or result.log_error:
                        messagebox.showwarning("Output status", details, parent=self)
                    else:
                        messagebox.showinfo("Output status", details, parent=self)
                elif event[0] == "queues":
                    self.queue_box["values"] = [item.name for item in event[1]]
                    self.invalidate()
                    self.status.set(f"{len(event[1])} installed/connected queues listed. Choose the actual cutter queue explicitly.")
                elif event[0] == "usb_devices":
                    self.usb_devices = {device.path: device for device in event[1]}
                    self.usb_box["values"] = list(self.usb_devices)
                    self.invalidate()
                    self.status.set(f"{len(event[1])} USB interfaces listed. Explicitly select the cutter interface; none was selected or opened automatically.")
                elif event[0] == "usb_cleanup":
                    self.invalidate()
                    self.status.set("USB cleanup: " + ("; ".join(f"{token}: {status}" for token, status in event[1].items())
                                                       if event[1] else "no previously owned operations remain."))
                else:
                    self.invalidate()
                    self.status.set(event[1])
                    messagebox.showerror("Output preparation", event[1], parent=self)
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def draw_prepared(self, prepared):
        self.canvas.delete("all")
        lines = prepared.polylines
        points = [point for line in lines for point in line.points_mm]
        if not points:
            return
        xmin, xmax = min(point[0] for point in points), max(point[0] for point in points)
        ymin, ymax = min(point[1] for point in points), max(point[1] for point in points)
        width, height = max(200, self.canvas.winfo_width()), max(120, self.canvas.winfo_height())
        scale = min((width - 30) / max(xmax - xmin, .01), (height - 30) / max(ymax - ymin, .01))
        for line in lines:
            flat = [coordinate for x, y in line.points_mm for coordinate in ((x - xmin) * scale + 15, (ymax - y) * scale + 15)]
            if len(flat) >= 4:
                self.canvas.create_line(*flat, fill="#b66a21" if line.role == "weed" else "#234c6a", width=1.4)

    def cancel(self):
        if self.busy:
            self.cancel_event.set()
            self.status.set("Cancellation requested. Waiting for the current driver/API call; bytes already accepted cannot be recalled.")

    def close(self):
        if self.busy:
            self.cancel()
            return
        self.closed = True
        self.grab_release()
        self.destroy()
