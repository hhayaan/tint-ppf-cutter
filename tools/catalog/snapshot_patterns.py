"""Copy FDU/FDA cache files to a new local directory, verifying every SHA256.

The source is opened read-only. Existing destination directories/files are
refused. Reparse points and symlinks are refused in both path ancestry and file
entries. A completed manifest records provenance and per-file integrity.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time

WORKSPACE = Path(__file__).resolve().parents[2]
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def check_no_reparse(path: Path, *, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE:
        raise ValueError(f"Symlink/reparse point refused: {path}")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise ValueError(f"Expected {'directory' if directory else 'regular file'}: {path}")
    return info


def check_ancestry(path: Path):
    for parent in reversed((path, *path.parents)):
        if parent.exists():
            check_no_reparse(parent, directory=True)


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def copy_verified(source: Path, destination: Path, expected):
    before = check_no_reparse(source)
    if identity(before) != identity(expected):
        raise ValueError(f"Source changed since inventory: {source.name}")
    target = destination / source.name
    copied = 0
    source_hash = hashlib.sha256()
    with source.open("rb") as original:
        opened = os.fstat(original.fileno())
        if identity(opened) != identity(before):
            raise ValueError(f"Source changed while opening: {source.name}")
        # Exclusive creation refuses pre-existing files and symlinks.
        with target.open("xb") as output:
            while chunk := original.read(1024 * 1024):
                source_hash.update(chunk)
                output.write(chunk)
                copied += len(chunk)
        if identity(os.fstat(original.fileno())) != identity(before):
            raise ValueError(f"Source changed during copy: {source.name}")
    output_info = check_no_reparse(target)
    with target.open("rb") as output:
        target_hash = hashlib.file_digest(output, "sha256").hexdigest()
    after = check_no_reparse(source)
    if identity(after) != identity(before):
        raise ValueError(f"Source changed before verification completed: {source.name}")
    source_digest = source_hash.hexdigest()
    if copied != before.st_size or output_info.st_size != copied or source_digest != target_hash:
        raise ValueError(f"Source/destination size or SHA256 mismatch: {source.name}")
    return {
        "basename": source.name,
        "byte_size": copied,
        "sha256": source_digest,
        "source_mtime_ns": before.st_mtime_ns,
        "source_file_id": str(before.st_ino),
        "source_size_mtime_identity_unchanged": True,
        "destination_hash_verified": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(r"C:\FilmVinylDesigns\ClientV2\Files"))
    parser.add_argument("--destination", type=Path, default=WORKSPACE / "data/patterns")
    parser.add_argument("--manifest", type=Path, default=WORKSPACE / "analysis/pattern-cache-snapshot.json")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 17))
    args = parser.parse_args()
    # Use absolute lexical paths before inspecting links; resolve() would hide them.
    source = Path(os.path.abspath(args.source))
    destination = Path(os.path.abspath(args.destination))
    manifest = Path(os.path.abspath(args.manifest))
    if not destination.is_relative_to(WORKSPACE / "data"):
        parser.error("Destination must be within workspace data")
    if not manifest.is_relative_to(WORKSPACE / "analysis"):
        parser.error("Manifest must be within workspace analysis")
    check_ancestry(source)
    check_no_reparse(source, directory=True)
    check_ancestry(destination.parent)
    check_ancestry(manifest.parent)
    if destination.exists() or manifest.exists():
        parser.error("Destination or manifest already exists; inspect it and choose a new path")
    entries = []
    for path in source.iterdir():
        if path.suffix.lower() in (".fdu", ".fda"):
            entries.append((path, check_no_reparse(path)))
    entries.sort(key=lambda item: item[0].name.lower())
    if len({path.name.lower() for path, _ in entries}) != len(entries):
        raise ValueError("Case-insensitive duplicate source filenames refused")
    total_bytes = sum(info.st_size for _, info in entries)
    destination.parent.mkdir(parents=True, exist_ok=True)
    check_ancestry(destination.parent)
    destination.mkdir(exist_ok=False)
    check_no_reparse(destination, directory=True)
    started = time.monotonic()
    started_utc = utc_now()
    files = []
    errors = []
    copied_bytes = 0
    last_progress = started
    print(json.dumps({"status": "copying", "files": len(entries), "byte_size": total_bytes, "destination": str(destination)}), flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(copy_verified, path, destination, info): path for path, info in entries}
        for future in as_completed(futures):
            try:
                record = future.result()
                files.append(record)
                copied_bytes += record["byte_size"]
            except Exception as error:
                errors.append({"basename": futures[future].name, "error": str(error)})
            completed = len(files) + len(errors)
            now = time.monotonic()
            if completed % 2000 == 0 or now - last_progress >= 10:
                print(json.dumps({"completed": completed, "total": len(entries), "verified": len(files), "errors": len(errors), "verified_bytes": copied_bytes, "elapsed_seconds": round(now - started, 1)}), flush=True)
                last_progress = now
    source_after = {path.name.lower(): identity(check_no_reparse(path)) for path in source.iterdir() if path.suffix.lower() in (".fdu", ".fda")}
    expected = {path.name.lower(): identity(info) for path, info in entries}
    source_changes = sorted(name for name in set(expected) | set(source_after) if expected.get(name) != source_after.get(name))
    result = {
        "schema": "pattern-cache-snapshot.v1",
        "source_root": str(source),
        "destination_root": str(destination),
        "started_utc": started_utc,
        "completed_utc": utc_now(),
        "source_open_mode": "rb",
        "extensions": [".fdu", ".fda"],
        "expected_files": len(entries),
        "expected_bytes": total_bytes,
        "verified_files": len(files),
        "verified_bytes": copied_bytes,
        "source_changes": source_changes,
        "errors": errors,
        "complete": not errors and not source_changes and len(files) == len(entries) and copied_bytes == total_bytes,
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "files": sorted(files, key=lambda record: record["basename"].lower()),
    }
    with manifest.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key != "files"}), flush=True)
    if not result["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
