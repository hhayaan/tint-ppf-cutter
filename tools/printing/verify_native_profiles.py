"""Capture a fixed rectangle through isolated x86 native plotter helpers.

The helper supplies only NativeCaptureSink, never a hardware transport. This
verifies captured command generation, not device compatibility or a physical cut.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
PENDING_DIRECT_IO = {
    "gunner.dll": "Direct WriteFile import; selected output leaf path needs review",
    "summahpgl.dll": "Direct ReadFile/WriteFile imports; selected output leaf path needs review",
    "summadmpl.dll": "Direct ReadFile/WriteFile imports; selected output leaf path needs review",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def helper_source_sha256() -> str:
    sources = [ROOT / "tools/printing/NativeCaptureSink.cs", ROOT / "tools/printing/NativePlotterBridge.cs"]
    return hashlib.sha256(b"".join(path.read_bytes() for path in sources)).hexdigest()


def request_for(driver: dict, profile: dict, runtime: Path, template: dict | None) -> dict:
    request = json.loads(json.dumps(template)) if template else {
        "schema": "tint-ppf-native-job.v1",
        "width_mm": 20.0,
        "height_mm": 10.0,
        "paths": [[[0.0, 0.0], [20.0, 0.0], [20.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
        "options": {"speed": None, "force": None, "acceleration": None, "tool": 1,
                    "rotate_axes": False, "return_origin": True, "feed": False},
    }
    request.update({"runtime_directory": str(runtime.resolve()), "profile_key": profile["key"],
                    "model_id": profile["model_id"], "driver_filename": driver["module_filename"],
                    "driver_sha256": driver["artifact_sha256"],
                    "variant_index": profile["static_record_index"],
                    "family": driver.get("native_abi_family") or ("DMPL" if "DMPL" in driver["protocol_families"] else "HPGL")})
    request["options"]["rotate_axes"] = request["family"] != "DMPL"
    return request


def validate_receipt(request: dict, receipt: dict, payload: Path) -> dict:
    if receipt.get("schema") != "tint-ppf-native-receipt.v1":
        raise ValueError("Unsupported receipt schema")
    if receipt.get("model_id") != request["model_id"]:
        raise ValueError("Factory model ID differs from catalog profile")
    if receipt.get("hardware_opened") is not False:
        raise ValueError("Receipt does not affirm offline capture")
    if receipt.get("physical_completion_verified") is not False:
        raise ValueError("Receipt incorrectly claims physical completion")
    if receipt.get("native_abi_family") != request.get("family"):
        raise ValueError("Native configuration ancestry differs from requested ABI family")
    coordinate_mode = receipt.get("coordinate_mode")
    if coordinate_mode not in {"native-source-points", "native-inverse-direct-xy"}:
        raise ValueError("Missing or unsupported native coordinate mode")
    size = payload.stat().st_size
    if size <= 0 or receipt.get("bytes") != size:
        raise ValueError("Empty or mismatched command byte count")
    digest = sha256(payload)
    if receipt.get("sha256") != digest:
        raise ValueError("Captured command hash differs from receipt")
    expected_paths = len(request["paths"])
    expected_points = sum(len(path) for path in request["paths"])
    if receipt.get("path_count") != expected_paths or receipt.get("point_count") != expected_points:
        raise ValueError("Native path/point counts differ from the fixture")
    for key in ("native_units_per_mm_x", "native_units_per_mm_y"):
        value = float(receipt[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Invalid native driver scale")
    bounds = receipt.get("native_bounds_mm")
    if not isinstance(bounds, list) or len(bounds) != 4 or any(not math.isfinite(float(v)) for v in bounds):
        raise ValueError("Missing or invalid native coordinate bounds")
    width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
    tolerance_x = max(1e-7, 1.0 / receipt["native_units_per_mm_x"])
    tolerance_y = max(1e-7, 1.0 / receipt["native_units_per_mm_y"])
    points = [point for path in request["paths"] for point in path]
    expected_bounds = [min(p[0] for p in points), min(p[1] for p in points),
                       max(p[0] for p in points), max(p[1] for p in points)]
    match = all(abs(actual - expected) <= tolerance
                for actual, expected, tolerance in zip(bounds, expected_bounds,
                                                       [tolerance_x, tolerance_y] * 2))
    return {"bytes": size, "sha256": digest, "write_calls": receipt.get("write_calls"),
            "native_units_per_mm_x": receipt["native_units_per_mm_x"],
            "native_units_per_mm_y": receipt["native_units_per_mm_y"],
            "native_bounds_mm": bounds, "captured_rectangle_mm": [width, height],
            "expected_bounds_mm": expected_bounds, "coordinate_mode": coordinate_mode,
            "native_abi_family": receipt["native_abi_family"],
            "expected_rectangle_mm": [request["width_mm"], request["height_mm"]],
            "dimensions_match": match,
            "model_id_verified": True, "receipt_verified": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "printing" / "drivers.json")
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True,
                        help="New directory beneath this workspace's analysis folder")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--request-template", type=Path)
    parser.add_argument("--public-manifest", type=Path,
                        help="Optional small metadata-only verification manifest, atomically published")
    parser.add_argument("--only", action="append", default=[], help="Exact profile key; repeatable")
    parser.add_argument("--reviewed-module", action="append", default=[],
                        help="Include a pending direct-I/O module after its selected leaf path was reviewed")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 90:
        parser.error("--timeout must be between 1 and 90 seconds")
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / "analysis").resolve()):
        parser.error("Output must stay beneath this workspace's analysis directory")
    output.mkdir(parents=True, exist_ok=False)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    if catalog.get("schema") != "tint-ppf-driver-catalog.v1":
        raise ValueError("Unsupported catalog schema")
    expected = catalog["counts"]["variant_profiles"]
    pairs = [(driver, profile) for driver in catalog["drivers"] for profile in driver["profiles"]]
    if len(pairs) != expected or len({p["key"] for _, p in pairs}) != expected:
        raise ValueError("Catalog profile count or key uniqueness mismatch")
    if args.only:
        selected = set(args.only)
        unknown = selected - {profile["key"] for _, profile in pairs}
        if unknown:
            raise ValueError("Unknown profile keys: " + ", ".join(sorted(unknown)))
        pairs = [(driver, profile) for driver, profile in pairs if profile["key"] in selected]
    template = json.loads(args.request_template.read_text(encoding="utf-8-sig")) if args.request_template else None
    source_hash = helper_source_sha256()
    source_binding_valid = (args.bridge.resolve().name == "NativePlotterBridge.exe"
                            and args.bridge.resolve().parent.name == source_hash[:20]
                            and args.bridge.resolve().parent.parent == (ROOT / ".runtime/printing").resolve())
    if args.public_manifest and not source_binding_valid:
        raise ValueError("A public manifest requires the current source-bound _helper build")
    bridge = output / "NativePlotterBridge.exe"
    with args.bridge.open("rb") as source, bridge.open("xb") as target:
        shutil.copyfileobj(source, target)
    bridge_hash = sha256(bridge)
    reviewed = {name.casefold() for name in args.reviewed_module}
    report = {"schema": "native-profile-capture-verification.v1",
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "catalog": str(args.catalog.resolve()), "catalog_sha256": sha256(args.catalog),
              "runtime_directory": str(args.runtime.resolve()), "catalog_profiles": expected,
              "selected_profiles": len(pairs), "bridge_snapshot": str(bridge),
              "bridge_sha256": bridge_hash, "per_process_timeout_seconds": args.timeout,
              "helper_source_sha256": source_hash if source_binding_valid else None,
              "helper_source_binding_valid": source_binding_valid,
              "hardware_transport": "NativeCaptureSink only; no configured transport",
              "physical_completion_verified": False, "profiles": []}
    started = time.monotonic()
    try:
        for index, (driver, profile) in enumerate(pairs):
            record = {"key": profile["key"], "model_id": profile["model_id"],
                      "model_name": profile["name"], "driver_filename": driver["module_filename"],
                      "variant_index": profile["static_record_index"],
                      "driver_sha256": driver["artifact_sha256"],
                      "rotate_axes": (driver.get("native_abi_family") or ("DMPL" if "DMPL" in driver["protocol_families"] else "HPGL")) != "DMPL"}
            module = driver["module_filename"].casefold()
            if module in PENDING_DIRECT_IO and module not in reviewed:
                record.update(status="pending_leaf_review", reason=PENDING_DIRECT_IO[module])
                report["profiles"].append(record)
                continue
            stem = re.sub(r"[^a-zA-Z0-9_-]", "_", profile["key"])
            folder = output / stem
            folder.mkdir(exist_ok=False)
            request = request_for(driver, profile, args.runtime, template)
            request_path, payload_path, receipt_path = (folder / "request.json", folder / "commands.bin", folder / "receipt.json")
            request_path.write_text(json.dumps(request, indent=2), encoding="utf-8")
            tick = time.monotonic()
            print(json.dumps({"profile": profile["key"], "index": index + 1, "total": len(pairs)}), flush=True)
            try:
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                process = subprocess.run([str(bridge), str(request_path), str(payload_path), str(receipt_path)],
                                         timeout=args.timeout, capture_output=True, text=True,
                                         errors="replace", creationflags=flags, check=False)
                (folder / "stdout.txt").write_text(process.stdout, encoding="utf-8")
                (folder / "stderr.txt").write_text(process.stderr, encoding="utf-8")
                record["exit_code"] = process.returncode
                if process.returncode != 0:
                    record.update(status="helper_failed", error=process.stderr[-4000:])
                else:
                    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
                    record.update(validate_receipt(request, receipt, payload_path))
                    record.update(status="captured" if record["dimensions_match"] else "geometry_mismatch",
                                  receipt=str(receipt_path), commands=str(payload_path))
            except subprocess.TimeoutExpired:
                # subprocess.run kills and reaps this owned helper before raising.
                record.update(status="timeout", error="Owned native helper exceeded its process deadline")
            except (OSError, ValueError, KeyError) as error:
                record.update(status="validation_failed", error=str(error))
            record["elapsed_seconds"] = round(time.monotonic() - tick, 3)
            report["profiles"].append(record)
    finally:
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["status_counts"] = dict(Counter(p["status"] for p in report["profiles"]))
        report["all_selected_captured"] = len(report["profiles"]) == len(pairs) and all(p["status"] == "captured" for p in report["profiles"])
        report["all_catalog_profiles_captured"] = len(pairs) == expected and report["all_selected_captured"]
        report["helper_source_unchanged_during_verification"] = helper_source_sha256() == source_hash
        if args.public_manifest and report["helper_source_unchanged_during_verification"]:
            public_profiles = {}
            for row in report["profiles"]:
                entry = {"status": "verified_capture" if row["status"] == "captured" else row["status"],
                         "driver_sha256": row["driver_sha256"], "model_id": row["model_id"],
                         "rectangle_mm": row.get("expected_rectangle_mm", [20.0, 10.0]),
                         "rotate_axes": row["rotate_axes"], "hardware_verified": False,
                         "helper_sha256": bridge_hash, "helper_source_sha256": source_hash}
                if row.get("receipt_verified"):
                    entry.update(capture_sha256=row["sha256"], capture_bytes=row["bytes"],
                                 coordinate_mode=row["coordinate_mode"],
                                 native_abi_family=row["native_abi_family"],
                                 native_bounds_mm=row["native_bounds_mm"],
                                 native_units_per_mm_x=row["native_units_per_mm_x"],
                                 native_units_per_mm_y=row["native_units_per_mm_y"],
                                 captured_rectangle_mm=row["captured_rectangle_mm"])
                if row["status"] != "captured":
                    entry["reason"] = row.get("reason", "Native capture or dimension verification did not pass; see private verification report")
                public_profiles[row["key"]] = entry
            public = {"schema": "tint-ppf-native-verification.v1",
                      "scope": "Offline native rectangle command capture only; no hardware output or physical compatibility test",
                      "catalog_sha256": report["catalog_sha256"], "bridge_sha256": bridge_hash,
                      "helper_source_sha256": source_hash,
                      "completed_utc": report["completed_utc"], "catalog_profiles": expected,
                      "selected_profiles": len(pairs), "status_counts": report["status_counts"],
                      "all_catalog_profiles_captured": report["all_catalog_profiles_captured"],
                      "profiles": public_profiles}
            args.public_manifest.parent.mkdir(parents=True, exist_ok=True)
            temporary = args.public_manifest.with_name(args.public_manifest.name + ".tmp")
            with temporary.open("x", encoding="utf-8") as stream:
                json.dump(public, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, args.public_manifest)
        (output / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output / "verification.json"), "status_counts": report["status_counts"],
                      "elapsed_seconds": report["elapsed_seconds"], "all_catalog_profiles_captured": report["all_catalog_profiles_captured"]}), flush=True)
    return 0 if report["all_selected_captured"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
