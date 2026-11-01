"""Read-only, complete pattern-cache parity verification.

Compare current source, independent workspace copies, and the snapshot receipts.
Hash every regular cache file on both sides, including ancillary metadata;
never repair or rewrite either side.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from snapshot_patterns import check_ancestry, check_no_reparse, identity

WORKSPACE = Path(__file__).resolve().parents[2]


def inventory(folder):
    check_ancestry(folder)
    check_no_reparse(folder, directory=True)
    entries = {}
    other = []
    for path in folder.iterdir():
        if path.is_dir():
            check_no_reparse(path, directory=True)
            other.append(path.name + "/")
            continue
        info = check_no_reparse(path)
        key = path.name.casefold()
        if key in entries:
            raise ValueError(f"Case-insensitive filename collision: {path.name}")
        entries[key] = (path, info)
    return entries, sorted(other, key=str.casefold)


def read_digest(path, expected):
    before = check_no_reparse(path)
    if identity(before) != identity(expected):
        raise ValueError(f"File changed since inventory: {path}")
    with path.open("rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError(f"File changed during open: {path}")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError(f"File changed while hashing: {path}")
    if identity(check_no_reparse(path)) != identity(before):
        raise ValueError(f"File changed before hash verification finished: {path}")
    return digest


def verify_one(key, source_entry, copied_entry, recorded):
    source_path, source_info = source_entry
    copied_path, copied_info = copied_entry
    source_hash = read_digest(source_path, source_info)
    copied_hash = read_digest(copied_path, copied_info)
    return {
        "basename": source_path.name,
        "source_bytes": source_info.st_size,
        "copied_bytes": copied_info.st_size,
        "manifest_bytes": recorded["byte_size"] if recorded else None,
        "source_sha256": source_hash,
        "copied_sha256": copied_hash,
        "manifest_sha256": recorded["sha256"] if recorded else None,
        "source_copy_equal": source_hash == copied_hash and source_info.st_size == copied_info.st_size,
        "manifest_equal": bool(recorded) and source_hash == copied_hash == recorded["sha256"] and source_info.st_size == copied_info.st_size == recorded["byte_size"],
        "source_metadata_unchanged_since_snapshot": bool(recorded) and source_info.st_size == recorded["byte_size"] and source_info.st_mtime_ns == recorded["source_mtime_ns"] and str(source_info.st_ino) == recorded["source_file_id"],
        "source_and_copy_stable_during_hash": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(r"C:\FilmVinylDesigns\ClientV2\Files"))
    parser.add_argument("--copies", type=Path, default=WORKSPACE / "data/patterns")
    parser.add_argument("--manifest", type=Path, default=WORKSPACE / "analysis/pattern-cache-snapshot.json")
    parser.add_argument("--auxiliary-manifest", type=Path, default=WORKSPACE / "analysis/pattern-auxiliary-snapshot.json")
    parser.add_argument("--report", type=Path, default=WORKSPACE / "analysis/pattern-parity-verification.json")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 17))
    args = parser.parse_args()
    started = time.monotonic()
    started_utc = datetime.now(timezone.utc).isoformat()
    source = Path(os.path.abspath(args.source))
    copies = Path(os.path.abspath(args.copies))
    report = Path(os.path.abspath(args.report))
    if not report.is_relative_to(WORKSPACE / "analysis"):
        parser.error("Verification report must stay inside workspace analysis")
    source_entries, other_source = inventory(source)
    copied_entries, other_copies = inventory(copies)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    manifest_entries = {}
    for record in manifest["files"]:
        key = record["basename"].casefold()
        if key in manifest_entries:
            raise ValueError("Duplicate manifest filename")
        manifest_entries[key] = record
    geometry_manifest_files = len(manifest_entries)
    auxiliary_manifest_files = 0
    if args.auxiliary_manifest.exists():
        auxiliary = json.loads(args.auxiliary_manifest.read_text(encoding="utf-8"))
        for record in auxiliary["files"]:
            key = record["basename"].casefold()
            if key in manifest_entries:
                raise ValueError("Auxiliary receipt overlaps the geometry manifest")
            manifest_entries[key] = record
            auxiliary_manifest_files += 1
    source_names, copied_names, manifest_names = set(source_entries), set(copied_entries), set(manifest_entries)
    results = []
    errors = []
    last_progress = started
    common = source_names & copied_names
    print(json.dumps({"status": "hashing", "source_files": len(source_names), "copied_files": len(copied_names), "manifest_files": len(manifest_names), "matched_filenames": len(common), "missing": len(source_names-copied_names), "extra": len(copied_names-source_names)}), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(verify_one, key, source_entries[key], copied_entries[key], manifest_entries.get(key)): key for key in sorted(common)}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as error:
                errors.append({"basename": futures[future], "error": str(error)})
            completed = len(results) + len(errors)
            now = time.monotonic()
            if completed % 4000 == 0 or now-last_progress >= 10:
                print(json.dumps({"verified": len(results), "total": len(common), "errors": len(errors), "elapsed_seconds": round(now-started,1)}), flush=True)
                last_progress = now
    source_after, ignored = inventory(source)
    copied_after, ignored = inventory(copies)
    source_changes = sorted(key for key in source_names | set(source_after) if key not in source_entries or key not in source_after or identity(source_entries[key][1]) != identity(source_after[key][1]))
    copy_changes = sorted(key for key in copied_names | set(copied_after) if key not in copied_entries or key not in copied_after or identity(copied_entries[key][1]) != identity(copied_after[key][1]))
    source_copy_mismatches = [record["basename"] for record in results if not record["source_copy_equal"]]
    manifest_mismatches = [record["basename"] for record in results if not record["manifest_equal"]]
    metadata_changes = [record["basename"] for record in results if not record["source_metadata_unchanged_since_snapshot"]]
    exact = source_names == copied_names == manifest_names
    result = {
        "schema": "pattern-parity-verification.v1",
        "scope": "All regular files currently present in the cache folder, including FDU/FDA geometry and associated metadata",
        "started_utc": started_utc,
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source),
        "copied_root": str(copies),
        "snapshot_manifest": str(args.manifest.resolve()),
        "auxiliary_manifest": str(args.auxiliary_manifest.resolve()) if args.auxiliary_manifest.exists() else None,
        "source_files": len(source_entries),
        "copied_files": len(copied_entries),
        "manifest_files": len(manifest_entries),
        "geometry_manifest_files": geometry_manifest_files,
        "auxiliary_manifest_files": auxiliary_manifest_files,
        "source_extensions": dict(Counter(path.suffix.lower() for path, info in source_entries.values())),
        "copied_extensions": dict(Counter(path.suffix.lower() for path, info in copied_entries.values())),
        "source_bytes": sum(info.st_size for path, info in source_entries.values()),
        "copied_bytes": sum(info.st_size for path, info in copied_entries.values()),
        "verified_files": len(results),
        "exact_case_insensitive_filename_sets": exact,
        "missing_from_copies": sorted(source_names-copied_names),
        "extra_in_copies": sorted(copied_names-source_names),
        "source_not_in_manifest": sorted(source_names-manifest_names),
        "manifest_not_in_source": sorted(manifest_names-source_names),
        "source_copy_mismatches": source_copy_mismatches,
        "manifest_mismatches": manifest_mismatches,
        "source_metadata_changes_since_snapshot": metadata_changes,
        "source_changes_during_verification": source_changes,
        "copy_changes_during_verification": copy_changes,
        "other_source_entries": other_source,
        "other_copy_entries": other_copies,
        "errors": errors,
        "complete_parity": exact and len(results)==len(source_names) and not errors and not source_copy_mismatches and not manifest_mismatches and not source_changes and not copy_changes and not other_source and not other_copies,
        "elapsed_seconds": round(time.monotonic()-started,2),
        "files": sorted(results,key=lambda record:record["basename"].casefold()),
    }
    check_ancestry(report.parent)
    if report.exists():
        check_no_reparse(report)
    report.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({key:value for key,value in result.items() if key!="files"}),flush=True)
    if not result["complete_parity"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
