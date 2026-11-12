"""Compile calibrated jobs through the inspected native model drivers.

The vendor code runs in an isolated x86 process with a memory-only NULL sink.
Delivery is a separate operation in transports.py. No vendor binaries are shipped
in this repository, and native success does not establish hardware compatibility.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile

from printing.driver_catalog import get_driver, get_profile

ROOT = Path(__file__).resolve().parents[1]
SHARED_SHA256 = "227203a783a8b4774d5bdc55ce090488db8b76b7f96abe3c9aa0475b0be5c868"
IO_SHA256 = "3b5c48581fd748886fe49145d1a4805e712e495ede45670c0d0703298ee52076"
VERIFICATION_PATH = ROOT / "docs" / "printing-native-verification.json"


@dataclass(frozen=True)
class ToolOptions:
    speed: int | None = None
    force: int | None = None
    acceleration: float | None = None
    tool: int = 1
    rotate_axes: bool = False
    return_origin: bool = True
    feed: bool = False

    def validate(self) -> None:
        for name in ("speed", "force", "tool"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= 32767):
                raise ValueError(f"{name} must be a positive integer in native driver units")
        if self.acceleration is not None and (not math.isfinite(self.acceleration) or not 0 < self.acceleration <= 32767):
            raise ValueError("Acceleration must be finite and positive in native driver units")
        for name in ("rotate_axes", "return_origin", "feed"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if self.feed:
            raise ValueError("An explicit page-feed operation has not yet been validated")


@dataclass(frozen=True)
class CompiledJob:
    payload: bytes
    metadata: dict


def capture_status(profile_key: str) -> dict:
    """Capture verification is an engineering gate, never a hardware certificate."""
    try:
        report = json.loads(VERIFICATION_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "not_verified"}
    if report.get("schema") != "tint-ppf-native-verification.v1":
        return {"status": "not_verified"}
    return report.get("profiles", {}).get(profile_key, {"status": "not_verified"})


def _sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def helper_source_sha256() -> str:
    sources = (ROOT / "tools/printing/NativeCaptureSink.cs", ROOT / "tools/printing/NativePlotterBridge.cs")
    return hashlib.sha256(b"".join(path.read_bytes() for path in sources)).hexdigest()


def discover_runtime() -> Path | None:
    candidates = [ROOT / ".runtime" / "vendor",
                  Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "FilmVinylDesigns" / "ClientV2"]
    return next((p for p in candidates if (p / "gsfwkcut62.dll").is_file() and (p / "drvcut").is_dir()), None)


def validate_runtime(runtime_directory: Path, driver: dict) -> Path:
    runtime = Path(runtime_directory).resolve(strict=True)
    lock = json.loads((Path(__file__).with_name("runtime-lock.json")).read_text(encoding="utf-8"))
    if lock.get("schema") != "tint-ppf-native-runtime-lock.v1":
        raise ValueError("Unsupported native runtime version lock")
    for item in lock["files"]:
        filename, digest = item["path"], item["sha256"]
        path = runtime / filename
        if not path.is_file() or _sha(path) != digest:
            raise ValueError(f"Native runtime file is missing or differs from the inspected version: {filename}")
    return runtime


def _helper() -> Path:
    if os.name != "nt":
        raise OSError("The recovered native driver compiler requires Windows")
    sources = [ROOT / "tools/printing/NativeCaptureSink.cs", ROOT / "tools/printing/NativePlotterBridge.cs"]
    digest = helper_source_sha256()[:20]
    folder = ROOT / ".runtime" / "printing" / digest
    executable = folder / "NativePlotterBridge.exe"
    if executable.is_file():
        return executable
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise OSError("The Windows .NET Framework 4 C# compiler is required for the native driver adapter")
    folder.mkdir(parents=True, exist_ok=True)
    # A unique temporary filename prevents concurrent compile requests from
    # running a half-written executable.
    with tempfile.TemporaryDirectory(prefix="build-", dir=folder) as temporary:
        target = Path(temporary) / executable.name
        result = subprocess.run([str(compiler), "/nologo", "/platform:x86", "/r:System.Web.Extensions.dll",
                                 f"/out:{target}", *(str(p) for p in sources)], capture_output=True, text=True,
                                timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError("Native adapter build failed:\n" + result.stdout + result.stderr)
        if not executable.exists():
            target.replace(executable)
    return executable


def native_request(prepared, profile_key: str, options: ToolOptions, runtime_directory: Path) -> dict:
    options.validate()
    profile = get_profile(profile_key)
    if profile is None:
        raise ValueError("Choose a model profile from the recovered catalog")
    driver = get_driver(profile["driver_id"])
    runtime = validate_runtime(runtime_directory, driver)
    paths = [[[float(x), float(y)] for x, y in line.points] for line in prepared.polylines]
    artwork_width, artwork_height = float(prepared.width_mm), float(prepared.height_mm)
    if not (math.isfinite(artwork_width) and math.isfinite(artwork_height)
            and 0 <= artwork_width <= 100000 and 0 <= artwork_height <= 100000
            and max(artwork_width, artwork_height) > 0):
        raise ValueError("Prepared page dimensions are invalid")
    layout = getattr(prepared, "options", None)
    bounds = getattr(prepared, "bounds_mm", (0.0, 0.0, artwork_width, artwork_height))
    if (not isinstance(bounds, (tuple, list)) or len(bounds) != 4
            or any(not math.isfinite(float(value)) for value in bounds)
            or bounds[0] < 0 or bounds[1] < 0 or bounds[2] < bounds[0] or bounds[3] < bounds[1]):
        raise ValueError("Prepared placement bounds are invalid")
    # A straight horizontal/vertical contour still needs a nondegenerate native
    # page. The actual emitted bounds are measured from points, not page corners.
    width = getattr(layout, "media_width_mm", None) or max(bounds[2], 1.0)
    height = getattr(layout, "media_height_mm", None) or max(bounds[3], 1.0)
    if not (math.isfinite(width) and math.isfinite(height) and 0 < width <= 100000 and 0 < height <= 100000):
        raise ValueError("Loaded media/page dimensions are invalid")
    if driver["id"] == "graphtec":
        # Proven graphtec.dll constructor: table double+8 -> TDriver+0x38,
        # physical page width in PostScript points; confirmed FC9000-140 1372mm.
        maximum = profile["raw_parameters"]["double_at8"] * 25.4 / 72
        if options.rotate_axes and width > maximum + 1e-6:
            raise ValueError(f"Page/media width exceeds this Graphtec model's {maximum:g} mm maximum")
    count = sum(len(path) for path in paths)
    if not paths or count > 2_000_000 or any(len(path) < 2 for path in paths):
        raise ValueError("Prepared job is empty or exceeds the native point limit")
    for path in paths:
        for x, y in path:
            if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= width + 1e-6 and 0 <= y <= height + 1e-6):
                raise ValueError("Prepared point lies outside its page")
    family = driver.get("native_abi_family") or ("DMPL" if "DMPL" in driver["protocol_families"] else "HPGL")
    if family == "DMPL" and options.rotate_axes:
        raise ValueError("Native HPGL axis rotation is unavailable for a DMPL profile; use geometry rotation")
    return {"schema": "tint-ppf-native-job.v1", "runtime_directory": str(runtime),
            "profile_key": profile["key"], "model_id": profile["model_id"],
            "driver_filename": driver["module_filename"], "driver_sha256": driver["artifact_sha256"],
            "variant_index": profile["static_record_index"], "family": family,
            "width_mm": width, "height_mm": height, "paths": paths, "options": asdict(options)}


def compile_job(prepared, profile_key: str, options: ToolOptions, runtime_directory: Path) -> CompiledJob:
    request = native_request(prepared, profile_key, options, runtime_directory)
    status = capture_status(profile_key)
    if status.get("status") != "verified_capture" or status.get("driver_sha256") != request["driver_sha256"]:
        raise ValueError("This profile is listed in the original catalog, but its native command capture is not yet verified")
    if status.get("rotate_axes") is not options.rotate_axes:
        raise ValueError("This model's selected native axis mode has not yet passed command-capture verification")
    if status.get("helper_source_sha256") != helper_source_sha256():
        raise ValueError("The native adapter changed after this profile was verified; repeat offline capture verification")
    helper = _helper()
    scratch = ROOT / ".runtime" / "printing" / "jobs"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compile-", dir=scratch) as temporary:
        folder = Path(temporary)
        input_path, output_path, receipt_path = (folder / name for name in ("request.json", "job.bin", "receipt.json"))
        input_path.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
        try:
            result = subprocess.run([str(helper), str(input_path), str(output_path), str(receipt_path)],
                                    capture_output=True, text=True, timeout=90,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("Native command capture timed out; no hardware transport was opened") from error
        if result.returncode != 0:
            raise RuntimeError("Native command capture failed:\n" + (result.stderr or result.stdout)[-6000:])
        payload = output_path.read_bytes()
        receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
        if (receipt.get("schema") != "tint-ppf-native-receipt.v1" or receipt.get("hardware_opened") is not False
                or receipt.get("bytes") != len(payload) or receipt.get("sha256") != hashlib.sha256(payload).hexdigest()
                or not payload or len(payload) > 64 * 1024 * 1024):
            raise RuntimeError("Native capture receipt or command payload is invalid")
    receipt.update(profile_key=profile_key, profile_name=get_profile(profile_key)["name"],
                   width_mm=prepared.width_mm, height_mm=prepared.height_mm,
                   page_width_mm=request["width_mm"], page_height_mm=request["height_mm"], tool_options=asdict(options),
                   backend="original-native-driver-memory-capture", hardware_verified=False,
                   source_sha256=getattr(prepared, "source_sha256", None),
                   geometry_options=asdict(prepared.options) if hasattr(prepared, "options") else None)
    return CompiledJob(payload, receipt)
