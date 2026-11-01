"""Read a workspace Jet4 snapshot with native ACE and export pattern metadata.

Jet header decoding follows primary-source mdbtools and Jackcess algorithms:
https://github.com/mdbtools/mdbtools/blob/dev/src/libmdb/file.c
https://github.com/jahlborn/jackcess/blob/master/src/main/java/com/healthmarketscience/jackcess/impl/DatabaseImpl.java
https://github.com/jahlborn/jackcess/blob/master/src/main/java/com/healthmarketscience/jackcess/impl/JetFormat.java
No password guessing, source editing, vendor execution, or service access occurs.
The stored password is decoded in memory and sent only in the reader environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from jet_header import snapshot_password

WORKSPACE = pathlib.Path(__file__).resolve().parents[2]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def q(name):
    return '"' + name.replace('"', '""') + '"'


def affinity(dotnet_type):
    if dotnet_type in ("System.Boolean", "System.Byte", "System.Int16", "System.Int32", "System.Int64"):
        return "INTEGER"
    if dotnet_type in ("System.Single", "System.Double", "System.Decimal"):
        return "REAL"
    if dotnet_type == "System.Byte[]":
        return "BLOB"
    return "TEXT"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=pathlib.Path, default=WORKSPACE / "analysis/snapshots/fvd.mdb")
    parser.add_argument("--output", type=pathlib.Path, default=WORKSPACE / "analysis/catalog.sqlite")
    parser.add_argument("--report", type=pathlib.Path, default=WORKSPACE / "analysis/catalog-export.json")
    args = parser.parse_args()
    snapshot = args.snapshot.resolve(strict=True)
    if not snapshot.is_relative_to((WORKSPACE / "analysis/snapshots").resolve()):
        parser.error("Only workspace snapshots may be read")
    output = args.output.resolve()
    if not output.is_relative_to((WORKSPACE / "analysis").resolve()):
        parser.error("Output must be inside workspace analysis")
    before = sha256(snapshot)
    started = time.monotonic()
    stage = WORKSPACE / "analysis/catalog-ace-stage"
    env = os.environ.copy()
    env["FVD_CATALOG_PASSWORD"] = snapshot_password(snapshot)
    powershell = pathlib.Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    completed = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-File", str(pathlib.Path(__file__).with_name("Export-AceMetadata.ps1")), "-SnapshotPath", str(snapshot), "-OutputDirectory", str(stage)], env=env, capture_output=True)
    env.pop("FVD_CATALOG_PASSWORD", None)
    if completed.returncode:
        # Native exception messages never include the connection string/password.
        raise RuntimeError("Native ACE metadata export failed: " + completed.stderr.decode("utf-8", errors="replace")[-3000:])
    schema = json.loads((stage / "schema.json").read_text(encoding="utf-8-sig"))
    pending = output.with_name(output.name + ".ace-pending")
    if pending.exists():
        parser.error("Pending output exists; inspect it before rerunning")
    connection = sqlite3.connect(pending)
    try:
        connection.execute("BEGIN")
        for table in schema:
            name = table["name"]
            columns = table["columns"]
            definition = ",".join(q(column["name"]) + " " + affinity(column["dotnet_type"]) for column in columns)
            connection.execute(f"CREATE TABLE {q(name)} ({definition})")
            placeholders = ",".join("?" for _ in columns)
            with (stage / (name + ".jsonl")).open(encoding="utf-8") as stream:
                connection.executemany(f"INSERT INTO {q(name)} VALUES ({placeholders})", (json.loads(line) for line in stream))
            count = connection.execute(f"SELECT COUNT(*) FROM {q(name)}").fetchone()[0]
            if count != table["declared_rows"] or count != table["exported_rows"]:
                raise ValueError(f"{name}: source/export/SQLite counts disagree")
            print(json.dumps({"table": name, "rows": count}), flush=True)
        for table, key in (("CarPattern", "PatternID"), ("VehicleModel", "Id"), ("VehicleTrimLevel", "Id")):
            connection.execute(f"CREATE UNIQUE INDEX {q('id_' + table)} ON {q(table)} ({q(key)})")
        connection.execute("CREATE INDEX ix_pattern_vehicle ON CarPattern (VehicleMakeID, VehicleModelId, VehicleTrimLevelId)")
        connection.execute("CREATE INDEX ix_pattern_names ON CarPattern (MakeName, ModelName, YearFrom, YearTo)")
        connection.execute("CREATE INDEX ix_pattern_asset ON CarPattern (FDImageId COLLATE NOCASE)")
        connection.execute("""CREATE VIEW patterns AS SELECT PatternID AS id, MakeName AS make,
            ModelName AS model, TrimName AS trim, YearFrom AS year_from, YearTo AS year_to,
            Type AS pattern_type, FDImageId AS asset_id, FileExt AS extension, Description AS description,
            Width AS width, Length AS length, SQFT AS square_feet, FileExists AS source_file_exists,
            VehicleMakeID AS make_id, VehicleModelId AS model_id, VehicleTrimLevelId AS trim_id,
            Active AS active, Tips AS tips, KitDetails AS kit_details, Difficulty AS difficulty,
            Price AS price, Rating AS rating, Instructions AS has_instructions, Warning AS warning
            FROM CarPattern""")
        empty_ids = connection.execute("SELECT COUNT(*) FROM patterns WHERE id IS NULL OR id=''").fetchone()[0]
        extension_counts = dict(connection.execute("SELECT extension,COUNT(*) FROM patterns GROUP BY extension"))
        type_counts = dict(connection.execute("SELECT pattern_type,COUNT(*) FROM patterns GROUP BY pattern_type"))
        type_codes = {row[0] for row in connection.execute("SELECT ShortName FROM CarPatternType")}
        if empty_ids or set(type_counts) - type_codes:
            raise ValueError(f"Semantic validation failed: empty identifier count={empty_ids}, type code match={not(set(type_counts)-type_codes)}")
        if sha256(snapshot) != before:
            raise ValueError("Snapshot changed; refusing to publish")
        connection.commit()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError("SQLite integrity check failed")
    finally:
        connection.close()
    os.replace(pending, output)
    report = {"snapshot": str(snapshot), "snapshot_sha256": before, "snapshot_unchanged": True, "reader": "Microsoft.ACE.OLEDB.12.0", "mode": "Read", "tables": schema, "output": str(output), "output_sha256": sha256(output), "integrity_check": integrity, "semantic_checks": {"empty_pattern_ids": empty_ids, "extension_counts": extension_counts, "type_counts": type_counts, "all_type_codes_known": True}, "elapsed_seconds": round(time.monotonic() - started, 2), "created_utc": datetime.now(timezone.utc).isoformat(), "exclusions": "No account, authentication, licensing, system, temporary or duplicated partition table values exported"}
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "integrity_check": integrity, "semantic_checks": report["semantic_checks"], "elapsed_seconds": report["elapsed_seconds"]}), flush=True)


if __name__ == "__main__":
    main()
