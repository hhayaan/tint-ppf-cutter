"""Offline cutting job CLI. This command never opens a device transport."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from printing.backend import ToolOptions, compile_job, discover_runtime
from printing.driver_catalog import get_driver, get_profile, search_profiles
from printing.geometry import JobOptions, prepare_job
from printing.transports import write_job_file
from printing.units import resolve_pattern_units
from tools.patterns.fvd_pattern import read_pattern


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-models", action="store_true")
    parser.add_argument("--search", default="")
    parser.add_argument("--profile", default="graphtec:53", help="Exact driver:index key")
    parser.add_argument("--runtime", type=Path, default=discover_runtime())
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pattern", type=Path)
    source.add_argument("--test-rectangle", type=float, nargs=2, metavar=("WIDTH_MM", "HEIGHT_MM"))
    parser.add_argument("--mm-per-source-unit", type=float,
                        help="Advanced custom override; supported FDA/FDU patterns use their native physical units automatically")
    parser.add_argument("--output", type=Path, help="New output file; no existing file is replaced")
    parser.add_argument("--media", type=float, nargs=2, metavar=("WIDTH_MM", "LENGTH_MM"))
    parser.add_argument("--speed", type=int)
    parser.add_argument("--force", type=int)
    args = parser.parse_args()
    if args.list_models:
        for profile in search_profiles(args.search):
            print(f"{profile['key']}\t{profile['name']}")
        return
    if args.output is None or (args.pattern is None and args.test_rectangle is None):
        parser.error("Choose --pattern or --test-rectangle, and a new --output file")
    if args.runtime is None:
        parser.error("Supply the inspected native runtime using --runtime")
    profile = get_profile(args.profile)
    if profile is None:
        parser.error("Unknown exact profile key; use --list-models")
    if args.pattern:
        pattern = read_pattern(args.pattern)
        if args.mm_per_source_unit is None:
            units = resolve_pattern_units(pattern).to_dict()
            factor = units["mm_per_source_unit"]
        else:
            factor = args.mm_per_source_unit
            units = {"automatic": False, "source_unit": "custom override", "mm_per_source_unit": factor}
    else:
        from printing.dialog import synthetic_rectangle
        pattern, factor = synthetic_rectangle(*args.test_rectangle), 1.0
        units = {"automatic": False, "source_unit": "millimeter", "mm_per_source_unit": 1.0}
    media = args.media or (None, None)
    prepared = prepare_job(pattern, JobOptions(mm_per_source_unit=factor, media_width_mm=media[0], media_height_mm=media[1]))
    driver = get_driver(profile["driver_id"])
    options = ToolOptions(speed=args.speed, force=args.force, rotate_axes=driver.get("native_abi_family") != "DMPL")
    compiled = compile_job(prepared, args.profile, options, args.runtime)
    receipt = args.output.with_suffix(args.output.suffix + ".json")
    if args.output.exists() or receipt.exists():
        parser.error("Output or receipt already exists; choose a new filename")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = write_job_file(args.output, compiled.payload)
    metadata = {**compiled.metadata, "delivery": result.to_dict(), "geometry": prepared.preview_data(),
                "source_units": units}
    with receipt.open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, indent=2, allow_nan=False)
        stream.write("\n")
    print(f"Saved {len(compiled.payload):,} bytes to {args.output}")
    print(f"Native bounds (mm): {compiled.metadata['native_bounds_mm']}")
    print(f"Receipt: {receipt}")
    print("No device transport was opened; physical output remains unverified.")


if __name__ == "__main__":
    main()
