"""Read Jet 4's stored database password from a workspace snapshot in memory.

Format references: MDB Tools src/libmdb/file.c and Jackcess DatabaseImpl.java /
JetFormat.java. This recovers the header's reversible field; it performs no
password search, file mutation, database opening, or credential logging.
"""
from pathlib import Path
import struct


def snapshot_password(path: Path) -> str:
    path = path.resolve(strict=True)
    snapshots = Path(__file__).resolve().parents[2] / 'analysis' / 'snapshots'
    if not path.is_relative_to(snapshots.resolve()):
        raise ValueError('Only workspace database snapshots are accepted')
    with path.open('rb') as stream:
        header = bytearray(stream.read(4096))
    if len(header) != 4096 or header[4:19] != b'Standard Jet DB' or header[20] != 1:
        raise ValueError('Expected a Jet 4 database header')
    key = bytes.fromhex('c7da396b')
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 255
        state[i], state[j] = state[j], state[i]
    i = j = 0
    for offset in range(24, 152):
        i = (i + 1) & 255
        j = (j + state[i]) & 255
        state[i], state[j] = state[j], state[i]
        header[offset] ^= state[(state[i] + state[j]) & 255]
    date = struct.unpack_from('<d', header, 114)[0]
    mask = struct.pack('<i', int(date))
    password = bytes(value ^ mask[i % 4] for i, value in enumerate(header[66:106]))
    return password.decode('utf-16-le').split('\0', 1)[0]
