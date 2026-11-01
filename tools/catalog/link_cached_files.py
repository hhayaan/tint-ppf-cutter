"""Link catalog asset identifiers to existing cache filenames using metadata only.

CarPattern.FDImageId supplies the exact full filename stem. Multiple vehicle
records may intentionally reference the same geometry asset.
No file content is opened and no source cache file is changed or copied.
"""
import argparse
import collections
import hashlib
from datetime import datetime, timezone
import json
import pathlib
import sqlite3

WORKSPACE = pathlib.Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=pathlib.Path, default=WORKSPACE / "analysis/catalog.sqlite")
    parser.add_argument("--files", type=pathlib.Path, default=pathlib.Path(r"C:\FilmVinylDesigns\ClientV2\Files"))
    parser.add_argument("--report", type=pathlib.Path, default=WORKSPACE / "analysis/catalog-file-coverage.json")
    args = parser.parse_args()
    catalog = args.catalog.resolve(strict=True)
    if not catalog.is_relative_to((WORKSPACE / "analysis").resolve()):
        parser.error("Only workspace-derived catalogs may be updated")
    connection = sqlite3.connect(catalog)
    patterns = list(connection.execute("SELECT id,asset_id,source_file_exists FROM patterns"))
    by_asset = collections.defaultdict(list)
    for pattern_id, asset_id, exists in patterns:
        if asset_id:
            by_asset[asset_id.lower()].append(pattern_id)
    files = []
    method_counts = collections.Counter()
    matched_patterns = set()
    for path in sorted(args.files.iterdir(), key=lambda item: item.name.lower()):
        if not path.is_file() or path.suffix.lower() not in (".fdu", ".fda"):
            continue
        stem = path.stem.lower()
        candidates = by_asset.get(stem, [])
        if candidates:
            method = "exact_fdimageid"
            matched_patterns.update(candidates)
        else:
            method = "no_asset_identifier_match"
        method_counts[method] += 1
        stat = path.stat()
        files.append((stem, path.suffix.lower(), str(path), stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), len(candidates), method))
    try:
        connection.execute("BEGIN")
        connection.execute("DROP VIEW IF EXISTS local_patterns")
        connection.execute("DROP TABLE IF EXISTS LocalPatternFiles")
        connection.execute("CREATE TABLE LocalPatternFiles (file_id TEXT NOT NULL COLLATE NOCASE, extension TEXT NOT NULL, file_path TEXT NOT NULL, byte_size INTEGER NOT NULL, modified_utc TEXT NOT NULL, catalog_row_count INTEGER NOT NULL, match_method TEXT NOT NULL, PRIMARY KEY(file_id,extension))")
        connection.executemany("INSERT INTO LocalPatternFiles VALUES (?,?,?,?,?,?,?)", files)
        connection.execute("CREATE VIEW local_patterns AS SELECT p.*,f.file_id AS local_file_id,f.extension AS local_extension,f.file_path AS local_file_path,f.byte_size AS local_byte_size,f.match_method AS local_match_method FROM patterns p JOIN LocalPatternFiles f ON f.file_id=p.asset_id COLLATE NOCASE")
        connection.commit()
        report = {
            "catalog": str(catalog),
            "source_cache": str(args.files),
            "source_content_read": False,
            "cache_files": len(files),
            "cache_bytes": sum(row[3] for row in files),
            "catalog_patterns": len(patterns),
            "patterns_with_candidate_cache_file": len(matched_patterns),
            "patterns_without_candidate_cache_file": len(patterns) - len(matched_patterns),
            "match_counts": dict(method_counts),
            "distinct_catalog_assets": len(by_asset),
            "cache_files_shared_by_multiple_catalog_rows": sum(row[5] > 1 for row in files),
            "patterns_with_multiple_cached_extensions": connection.execute("SELECT COUNT(*) FROM (SELECT p.id FROM patterns p JOIN LocalPatternFiles f ON f.file_id=p.asset_id COLLATE NOCASE GROUP BY p.id HAVING COUNT(*)>1)").fetchone()[0],
            "relationship": "Exact case-insensitive CarPattern.FDImageId = filename stem; multiple vehicle rows may share one geometry asset. Runtime geometry decode and plotter behavior are separate validation.",
            "integrity_check": connection.execute("PRAGMA integrity_check").fetchone()[0],
        }
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        export_report = catalog.parent / "catalog-export.json"
        if export_report.exists():
            export = json.loads(export_report.read_text(encoding="utf-8"))
            if pathlib.Path(export.get("output", "")).resolve() == catalog:
                export.setdefault("metadata_only_sha256_before_cache_link", export.get("output_sha256"))
                with catalog.open("rb") as stream:
                    export["output_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
                export["cache_link_report"] = str(args.report.resolve())
                export_report.write_text(json.dumps(export, indent=2), encoding="utf-8")
        print(json.dumps(report))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
