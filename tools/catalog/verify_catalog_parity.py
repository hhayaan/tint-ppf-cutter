"""Verify the current local MDB lineage and every migrated metadata value.

Reads the original MDB, its captured snapshot, native ACE staging records, and
our SQLite database. Only the verification report is written.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

WORKSPACE = Path(__file__).resolve().parents[2]


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def main() -> None:
    analysis = WORKSPACE / 'analysis'
    receipt = json.loads((analysis / 'snapshots/fvd-snapshot.json').read_text(encoding='utf-8-sig'))
    original = Path(receipt['Source'])
    snapshot = Path(receipt['Snapshot'])
    original_before = original.stat()
    original_digest = digest(original)
    snapshot_digest = digest(snapshot)
    expected_digest = receipt['SnapshotSHA256'].lower()
    original_after = original.stat()
    schema = json.loads((analysis / 'catalog-ace-stage/schema.json').read_text(encoding='utf-8-sig'))
    catalog = analysis / 'catalog.sqlite'
    connection = sqlite3.connect(catalog.as_uri() + '?mode=ro', uri=True)
    connection.execute('PRAGMA query_only=ON')
    tables = []
    for table in schema:
        name = table['name']
        columns = [column['name'] for column in table['columns']]
        actual_columns = [row[1] for row in connection.execute('PRAGMA table_info(' + quote(name) + ')')]
        cursor = connection.execute('SELECT ' + ','.join(map(quote, columns)) + ' FROM ' + quote(name) + ' ORDER BY rowid')
        row_count = 0
        compared_values = 0
        mismatch_count = 0
        mismatch_locations = []
        with (analysis / 'catalog-ace-stage' / (name + '.jsonl')).open(encoding='utf-8-sig') as stream:
            for row_number, line in enumerate(stream, 1):
                source = json.loads(line)
                ours = cursor.fetchone()
                if ours is None:
                    mismatch_count += 1
                    if len(mismatch_locations) < 10:
                        mismatch_locations.append({'row': row_number, 'reason': 'missing SQLite row'})
                    continue
                row_count += 1
                compared_values += len(source)
                for index, (expected, actual) in enumerate(zip(source, ours)):
                    if expected != actual or (expected is None) != (actual is None):
                        mismatch_count += 1
                        if len(mismatch_locations) < 10:
                            mismatch_locations.append({'row': row_number, 'column': columns[index]})
                if len(source) != len(ours):
                    mismatch_count += 1
        extra_rows = sum(1 for _ in cursor)
        matched = (actual_columns == columns and mismatch_count == 0 and extra_rows == 0
                   and row_count == table['declared_rows'] == table['exported_rows'])
        tables.append({'table': name, 'source_rows': table['exported_rows'], 'compared_rows': row_count,
                       'compared_values': compared_values, 'columns_match': actual_columns == columns,
                       'mismatch_count': mismatch_count, 'extra_rows': extra_rows,
                       'mismatch_locations': mismatch_locations, 'exact_logical_match': matched})
    count = connection.execute('SELECT COUNT(*) FROM patterns').fetchone()[0]
    assets = connection.execute('SELECT COUNT(DISTINCT lower(asset_id)) FROM patterns').fetchone()[0]
    linked = connection.execute('SELECT COUNT(*) FROM local_patterns').fetchone()[0]
    integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
    connection.close()
    lineage = original_digest == snapshot_digest == expected_digest
    stable = (original_before.st_size, original_before.st_mtime_ns) == (original_after.st_size, original_after.st_mtime_ns)
    result = {
        'verified_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'Current installed local database; does not enumerate a remote vendor library',
        'source': str(original), 'snapshot': str(snapshot), 'catalog': str(catalog),
        'source_sha256': original_digest, 'snapshot_sha256': snapshot_digest,
        'source_matches_captured_snapshot': lineage, 'source_metadata_stable_during_hash': stable,
        'source_native_ace_rows': 'analysis/catalog-ace-stage/*.jsonl',
        'source_tables_compared': tables, 'all_migrated_values_match_native_export': all(t['exact_logical_match'] for t in tables),
        'catalog_listings': count, 'referenced_geometry_assets': assets, 'local_linked_listings': linked,
        'sqlite_integrity': integrity,
        'complete': lineage and stable and all(t['exact_logical_match'] for t in tables) and linked == count and integrity == 'ok',
        'limit': 'The complete original MDB is retained as a byte-identical snapshot; SQLite migrates the 13 allowlisted pattern/vehicle tables, not account or licensing workflows.',
    }
    (analysis / 'catalog-parity-verification.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'source_tables_compared'}))
    if not result['complete']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
