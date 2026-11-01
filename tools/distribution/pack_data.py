"""Build or verify the private, portable Tint/PPF pattern-data bundle.

Builds only from this workspace's SQLite catalog and private pattern copies.
Never reads installed vendor folders, original MDBs, keys, logs, or accounts.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile
import time
import zipfile

WORKSPACE = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "bundle-manifest.json"
SCHEMA = "tint-ppf-private-data.v1"
ALLOWED_TABLES = {
    "AuthorsForPatterns", "CarMake", "CarModel", "CarPattern", "CarPatternType",
    "CarYear", "ImportRegionsForPatterns", "Instructions", "Links",
    "LocalPatternFiles", "Regions", "RegionsStates", "VehicleModel", "VehicleTrimLevel",
}
ALLOWED_VIEWS = {"patterns", "local_patterns"}
REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def progress(event):
    print(json.dumps(event), flush=True)


def inspect_path(path, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE:
        raise ValueError(f"Symlink/reparse point refused: {path}")
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError(f"Unexpected filesystem object: {path}")
    return info


def check_parents(path):
    for parent in reversed((path, *path.parents)):
        if parent.exists():
            inspect_path(parent, directory=True)


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def file_hash(path):
    before = inspect_path(path)
    with path.open("rb") as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError(f"File changed while opening: {path}")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError(f"File changed while hashing: {path}")
    if identity(inspect_path(path)) != identity(before):
        raise ValueError(f"File changed while hashing: {path}")
    return digest


def safe_relative(value):
    relative = PurePosixPath(value)
    if not value or relative.is_absolute() or ".." in relative.parts or "\\" in value or ":" in value:
        raise ValueError(f"Unsafe bundle path: {value}")
    return relative


def read_only_connection(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def catalog_summary(path):
    inspect_path(path)
    with closing(read_only_connection(path)) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("SQLite integrity check failed")
        objects = connection.execute("SELECT name,type FROM sqlite_master WHERE type IN ('table','view')").fetchall()
        tables = {name for name, kind in objects if kind == "table"}
        views = {name for name, kind in objects if kind == "view"}
        if tables - ALLOWED_TABLES or views - ALLOWED_VIEWS:
            raise ValueError("Catalog contains tables/views outside the pattern-metadata allowlist")
        if "CarPattern" not in tables or "patterns" not in views:
            raise ValueError("Catalog lacks the expected pattern metadata")
        records, assets = connection.execute("SELECT COUNT(*),COUNT(DISTINCT FDImageId) FROM CarPattern").fetchone()
        if connection.execute("SELECT COUNT(*) FROM patterns").fetchone()[0] != records:
            raise ValueError("Normalized pattern count differs from raw catalog")
        rows = {name: connection.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in sorted(tables)}
        return {"pattern_records": records, "distinct_pattern_assets": assets, "table_rows": rows, "views": sorted(views), "integrity_check": integrity}


def backup_catalog(source, destination):
    before = file_hash(source)
    source_summary = catalog_summary(source)
    old_directories = set()
    with closing(read_only_connection(source)) as original, closing(sqlite3.connect(destination)) as backup:
        original.backup(backup)
        if "LocalPatternFiles" in source_summary["table_rows"]:
            rows = backup.execute("SELECT rowid,file_path FROM LocalPatternFiles").fetchall()
            portable = []
            for rowid, value in rows:
                normalized = str(value).replace("\\", "/")
                basename = normalized.rsplit("/", 1)[-1]
                if not basename or "/" in basename or "\\" in basename:
                    raise ValueError("Invalid derived file association")
                if len(normalized) > 2 and normalized[1] == ":":
                    old_directories.add(normalized.rsplit("/", 1)[0])
                portable.append(("data/patterns/" + basename, rowid))
            backup.executemany("UPDATE LocalPatternFiles SET file_path=? WHERE rowid=?", portable)
            backup.commit()
        # UPDATE alone leaves old path bytes in SQLite free blocks. Compact
        # only this derivative backup so historical machine paths are removed.
        backup.execute("VACUUM")
    with destination.open("rb") as stream:
        compacted = stream.read()
    for directory in old_directories:
        for spelling in (directory, directory.replace("/", "\\")):
            if spelling.encode("utf-8") in compacted or spelling.encode("utf-16-le") in compacted:
                raise ValueError("Compacted backup retains an original machine directory")
    if file_hash(source) != before or catalog_summary(destination) != source_summary:
        raise ValueError("Canonical catalog changed or backup metadata differs")
    return source_summary, before


def load_receipts(receipts):
    expected = {}
    for receipt_path in receipts:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not receipt.get("complete"):
            raise ValueError(f"Incomplete source copy receipt: {receipt_path.name}")
        for record in receipt["files"]:
            name = record["basename"]
            if Path(name).name != name or "/" in name or "\\" in name or name.casefold() in expected:
                raise ValueError("Unsafe or duplicate receipt filename")
            if Path(name).suffix.lower() not in (".fdu", ".fda") and name != "warnings.json":
                raise ValueError("Receipt includes a file outside the private pattern-data allowlist")
            expected[name.casefold()] = record
    return expected


def add_file(archive, source, archive_name, expected=None):
    safe_relative(archive_name)
    before = inspect_path(source)
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as original, archive.open(archive_name, "w") as packaged:
        if identity(os.fstat(original.fileno())) != identity(before):
            raise ValueError(f"Input changed while opening: {source.name}")
        while chunk := original.read(1024 * 1024):
            packaged.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
        if identity(os.fstat(original.fileno())) != identity(before):
            raise ValueError(f"Input changed while packaging: {source.name}")
    if identity(inspect_path(source)) != identity(before) or copied != before.st_size:
        raise ValueError(f"Input changed while packaging: {source.name}")
    actual_hash = digest.hexdigest()
    if expected and (actual_hash != expected["sha256"] or copied != expected["byte_size"]):
        raise ValueError(f"Private copy differs from its snapshot receipt: {source.name}")
    return {"path": archive_name.removeprefix("data/"), "byte_size": copied, "sha256": actual_hash}


def verify_data(data_root, emit=progress):
    data_root = Path(os.path.abspath(data_root))
    check_parents(data_root)
    inspect_path(data_root, directory=True)
    manifest_path = data_root / MANIFEST_NAME
    inspect_path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != SCHEMA:
        raise ValueError("Unsupported private data manifest")
    expected = {}
    for record in manifest["files"]:
        relative = safe_relative(record["path"])
        key = relative.as_posix().casefold()
        if key in expected or key == MANIFEST_NAME.casefold():
            raise ValueError("Duplicate/self-referencing manifest entry")
        if key != "catalog.sqlite" and not (relative.parent == PurePosixPath("patterns") and (relative.suffix.lower() in (".fda", ".fdu") or relative.name == "warnings.json")):
            raise ValueError("Manifest includes a file outside the private data allowlist")
        expected[key] = record
    actual = {}
    for path in data_root.rglob("*"):
        if path.is_dir():
            inspect_path(path, directory=True)
            continue
        inspect_path(path)
        relative = path.relative_to(data_root).as_posix()
        key = relative.casefold()
        if key in actual:
            raise ValueError("Case-insensitive duplicate extracted filename")
        actual[key] = path
    actual.pop(MANIFEST_NAME.casefold(), None)
    if set(expected) != set(actual):
        missing, extra = sorted(set(expected)-set(actual)), sorted(set(actual)-set(expected))
        raise ValueError(f"Extracted data file set differs: missing={missing[:5]}, extra={extra[:5]}")
    started = time.monotonic()
    checked = 0
    last_progress = started
    def check_file(key):
        path = actual[key]
        record = expected[key]
        if inspect_path(path).st_size != record["byte_size"] or file_hash(path) != record["sha256"]:
            raise ValueError(f"Extracted file size/SHA256 mismatch: {record['path']}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(check_file, key) for key in sorted(expected)]
        for future in as_completed(futures):
            future.result()
            checked += 1
            now = time.monotonic()
            if checked % 4000 == 0 or now-last_progress >= 10:
                emit({"phase": "verify-data", "verified_files": checked, "total": len(expected)})
                last_progress = now
    summary = catalog_summary(data_root / "catalog.sqlite")
    if summary != manifest["catalog"]:
        raise ValueError("Extracted SQLite catalog counts/schema differ from manifest")
    if "LocalPatternFiles" in summary["table_rows"]:
        with closing(read_only_connection(data_root / "catalog.sqlite")) as catalog:
            if catalog.execute("SELECT COUNT(*) FROM LocalPatternFiles WHERE file_path NOT LIKE 'data/patterns/%'").fetchone()[0]:
                raise ValueError("Packaged catalog contains nonportable derived file paths")
    geometry_by_stem = {}
    geometry_count = 0
    for record in expected.values():
        relative = PurePosixPath(record["path"])
        if relative.parent == PurePosixPath("patterns") and relative.suffix.lower() in (".fda", ".fdu"):
            geometry_by_stem.setdefault(relative.stem.casefold(), []).append(relative.as_posix())
            geometry_count += 1
    referenced = set()
    associated_records = 0
    with closing(read_only_connection(data_root / "catalog.sqlite")) as catalog:
        for asset_id, records in catalog.execute("SELECT FDImageId,COUNT(*) FROM CarPattern GROUP BY FDImageId"):
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise ValueError("Catalog contains an empty/null pattern asset identifier")
            stem = asset_id.casefold()
            candidates = geometry_by_stem.get(stem, [])
            if not candidates:
                raise ValueError(f"Catalog references a missing pattern asset: {asset_id}")
            if len(candidates) != 1:
                raise ValueError(f"Catalog pattern asset has ambiguous cached extensions: {asset_id}")
            referenced.add(stem)
            associated_records += records
    associations = {"verified": True, "associated_catalog_records": associated_records, "referenced_geometry_files": len(referenced), "unreferenced_geometry_files": geometry_count-len(referenced), "null_or_missing_assets": 0, "ambiguous_assets": 0}
    counts = Counter(PurePosixPath(record["path"]).suffix.lower() for record in expected.values() if PurePosixPath(record["path"]).parent == PurePosixPath("patterns"))
    result = {"verified": True, "data_root": str(data_root), "verified_files": checked, "pattern_cache_files": sum(counts.values()), "pattern_extensions": dict(counts), "catalog": summary, "associations": associations, "elapsed_seconds": round(time.monotonic()-started, 2)}
    emit({key: value for key, value in result.items() if key != "catalog"})
    return result


def extract_and_verify(archive_path, extraction_root, emit=progress):
    extraction_root.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    last_progress = started
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = archive.namelist()
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError("Duplicate ZIP member names")
        if "data/" + MANIFEST_NAME not in names:
            raise ValueError("Bundle lacks its manifest")
        for index, info in enumerate(archive.infolist(), 1):
            relative = safe_relative(info.filename)
            if relative.parts[0] != "data" or info.is_dir():
                raise ValueError("ZIP does not have the required flat data/ layout")
            target = extraction_root.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            # Reading through EOF makes zipfile verify each entry's recorded CRC.
            with archive.open(info) as packaged, target.open("xb") as extracted:
                while chunk := packaged.read(1024 * 1024):
                    extracted.write(chunk)
            now = time.monotonic()
            if index % 4000 == 0 or now-last_progress >= 10:
                emit({"phase": "extract-crc", "files": index, "total": len(names)})
                last_progress = now
    verified = verify_data(extraction_root / "data", emit)
    verified["zip_crc_verified_entries"] = len(names)
    return verified


def build_bundle(root, catalog, pattern_root, receipts, output_directory, emit=progress):
    root, catalog, pattern_root, output_directory = map(lambda path: Path(os.path.abspath(path)), (root, catalog, pattern_root, output_directory))
    if not catalog.is_relative_to(root) or not pattern_root.is_relative_to(root / "data") or not output_directory.is_relative_to(root / "distribution/private"):
        raise ValueError("Builder only reads workspace copies and writes private distribution outputs")
    check_parents(catalog.parent)
    check_parents(pattern_root)
    check_parents(output_directory)
    inspect_path(catalog)
    inspect_path(pattern_root, directory=True)
    expected = load_receipts(receipts)
    paths = {}
    for path in pattern_root.iterdir():
        inspect_path(path)
        key = path.name.casefold()
        if key in paths:
            raise ValueError("Case-insensitive duplicate pattern filename")
        paths[key] = path
    if set(paths) != set(expected):
        raise ValueError("Private cache file set differs from snapshot receipts")
    output_directory.mkdir(parents=True, exist_ok=True)
    check_parents(output_directory)
    version = 1
    while True:
        stem = f"TintPPF-PatternData-v{version}"
        archive_path = output_directory / (stem + ".zip")
        pending = output_directory / (stem + ".zip.partial")
        receipt_path = output_directory / (stem + ".receipt.json")
        checksum_path = output_directory / (stem + ".sha256")
        extraction_root = output_directory / "verification" / stem
        if not any(path.exists() for path in (archive_path, pending, receipt_path, checksum_path, extraction_root)):
            break
        version += 1
    started = time.monotonic()
    created_utc = datetime.now(timezone.utc).isoformat()
    file_records = []
    with tempfile.TemporaryDirectory(prefix=".bundle-build-", dir=output_directory) as temporary:
        backup = Path(temporary) / "catalog.sqlite"
        summary, canonical_hash = backup_catalog(catalog, backup)
        emit({"phase": "pack", "pattern_files": len(paths), "catalog_records": summary["pattern_records"], "output": str(archive_path)})
        with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            file_records.append(add_file(archive, backup, "data/catalog.sqlite"))
            last_progress = time.monotonic()
            for index, key in enumerate(sorted(paths), 1):
                path = paths[key]
                file_records.append(add_file(archive, path, "data/patterns/" + path.name, expected[key]))
                now = time.monotonic()
                if index % 4000 == 0 or now-last_progress >= 10:
                    emit({"phase": "pack", "pattern_files": index, "total": len(paths)})
                    last_progress = now
            manifest = {"schema": SCHEMA, "layout": "Extract beside the application; the ZIP root contains data/", "catalog": summary, "catalog_derivation": {"method": "sqlite3.Connection.backup from a read-only canonical connection", "canonical_source_sha256": canonical_hash, "derived_path_rewrite": "Only LocalPatternFiles.file_path is normalized to data/patterns/<filename>; native table values are unchanged", "backup_compaction": "VACUUM of the derivative backup removes historical path bytes from freed pages", "old_derived_directory_bytes_absent": True}, "files": file_records}
            archive.writestr("data/" + MANIFEST_NAME, json.dumps(manifest, sort_keys=True, separators=(",", ":")), compress_type=zipfile.ZIP_DEFLATED, compresslevel=3)
        if file_hash(catalog) != canonical_hash:
            raise ValueError("Canonical catalog changed during bundling")
    verification = extract_and_verify(pending, extraction_root, emit)
    pending.rename(archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        uncompressed = sum(info.file_size for info in archive.infolist())
        compressed_payload = sum(info.compress_size for info in archive.infolist())
        entry_count = len(archive.infolist())
    archive_size = archive_path.stat().st_size
    result = {"schema": "tint-ppf-private-bundle-receipt.v1", "created_utc": created_utc, "archive": str(archive_path), "archive_sha256": file_hash(archive_path), "checksum_file": str(checksum_path), "archive_bytes": archive_size, "archive_mb": round(archive_size/1_000_000, 3), "uncompressed_bytes": uncompressed, "uncompressed_mb": round(uncompressed/1_000_000, 3), "compressed_payload_bytes": compressed_payload, "compression": "ZIP_DEFLATED level 3", "zip_entries": entry_count, "manifest": "data/" + MANIFEST_NAME, "data_payload_files": len(file_records), "pattern_cache_files": len(paths), "catalog": summary, "verification": verification, "elapsed_seconds": round(time.monotonic()-started, 2)}
    with checksum_path.open("x", encoding="ascii", newline="\n") as stream:
        stream.write(result["archive_sha256"] + "  " + archive_path.name + "\n")
    with receipt_path.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    emit({key: value for key, value in result.items() if key not in ("catalog", "verification")})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-data", type=Path, help="Verify an extracted data directory; needs only its bundled manifest")
    default_catalog = WORKSPACE / "data/catalog.sqlite"
    if not default_catalog.exists():
        default_catalog = WORKSPACE / "analysis/catalog.sqlite"
    parser.add_argument("--catalog", type=Path, default=default_catalog)
    parser.add_argument("--patterns", type=Path, default=WORKSPACE / "data/patterns")
    parser.add_argument("--geometry-receipt", type=Path, default=WORKSPACE / "analysis/pattern-cache-snapshot.json")
    parser.add_argument("--auxiliary-receipt", type=Path, default=WORKSPACE / "analysis/pattern-auxiliary-snapshot.json")
    parser.add_argument("--output-directory", type=Path, default=WORKSPACE / "distribution/private")
    args = parser.parse_args()
    try:
        if args.verify_data:
            verify_data(args.verify_data)
        else:
            build_bundle(WORKSPACE, args.catalog, args.patterns, [args.geometry_receipt, args.auxiliary_receipt], args.output_directory)
    except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile) as error:
        parser.exit(1, f"Data bundle failed: {error}\n")


if __name__ == "__main__":
    main()
