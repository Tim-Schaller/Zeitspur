import secrets
import sys

import pytest
import sqlcipher3

from zeitspur.crypto import (KEY_BYTES, KeyProtectionError, WrongKeyError, is_encrypted_file,
                                load_or_create_key, open_database, protect_key, unprotect_key)
from zeitspur.storage import Storage

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI nur unter Windows")


def test_key_roundtrip_creates_protected_file(tmp_path):
    kp = tmp_path / "key.bin"
    key = load_or_create_key(kp)
    assert len(key) == KEY_BYTES
    assert kp.exists() and not (tmp_path / "key.bin.tmp").exists()
    blob = kp.read_bytes()
    assert key not in blob  # Rohschluessel liegt nicht im Klartext in der Datei
    assert load_or_create_key(kp) == key


def test_protect_unprotect():
    key = secrets.token_bytes(32)
    blob = protect_key(key)
    assert blob != key and len(blob) > len(key)
    assert unprotect_key(blob) == key


def test_tampered_blob_fails(tmp_path):
    kp = tmp_path / "key.bin"
    load_or_create_key(kp)
    blob = bytearray(kp.read_bytes())
    blob[-1] ^= 0xFF
    kp.write_bytes(bytes(blob))
    with pytest.raises(KeyProtectionError):
        load_or_create_key(kp)


def test_empty_key_file_fails(tmp_path):
    kp = tmp_path / "key.bin"
    kp.write_bytes(b"")
    with pytest.raises(KeyProtectionError):
        load_or_create_key(kp)


def test_wrong_entropy_fails():
    import win32crypt

    blob = protect_key(secrets.token_bytes(32))
    with pytest.raises(Exception):
        win32crypt.CryptUnprotectData(blob, b"andere-entropie", None, None, 0x01)


def test_database_unreadable_with_wrong_or_missing_key(tmp_path):
    db = tmp_path / "zeitspur.db"
    key = secrets.token_bytes(32)
    s = Storage(db, key)
    s.insert_entry(ts_start=1, ts_end=2, monitor_id=1, process_name="a", window_title="geheim",
                   exe_path=None, width=1, height=1, webp=b"x")
    s.close()
    assert is_encrypted_file(db)
    with open(db, "rb") as f:
        assert not f.read(16).startswith(b"SQLite format 3")
    with pytest.raises(WrongKeyError):
        open_database(db, secrets.token_bytes(32))
    plain = sqlcipher3.connect(str(db))
    with pytest.raises(sqlcipher3.DatabaseError):
        plain.execute("SELECT count(*) FROM sqlite_master").fetchall()
    plain.close()
    # richtiger Schluessel funktioniert weiterhin
    con = open_database(db, key)
    assert con.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    con.close()


def test_writer_pragmas(tmp_path):
    con = open_database(tmp_path / "w.db", secrets.token_bytes(32))
    p = lambda name: con.execute(f"PRAGMA {name}").fetchone()[0]  # noqa: E731
    assert p("temp_store") == 2          # MEMORY: keine Klartext-Sortierdateien
    assert p("journal_mode") == "wal"
    assert p("auto_vacuum") == 2         # INCREMENTAL
    assert p("foreign_keys") == 1
    assert p("secure_delete") == 1  # ON: freigegebene Seiten werden genullt
    assert p("busy_timeout") == 5000
    con.close()


def test_read_only_connection(tmp_path):
    db = tmp_path / "ro.db"
    key = secrets.token_bytes(32)
    s = Storage(db, key)
    s.insert_entry(ts_start=1, ts_end=2, monitor_id=1, process_name="a", window_title="t",
                   exe_path=None, width=1, height=1, webp=b"x")
    # parallel zum Writer
    ro = open_database(db, key, read_only=True, create=False)
    assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
    assert ro.execute("PRAGMA temp_store").fetchone()[0] == 2
    assert ro.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    with pytest.raises(sqlcipher3.OperationalError):
        ro.execute("DELETE FROM entries")
    ro.close()
    s.close()
    # ohne laufenden Writer (WAL-Datei ggf. vorhanden)
    ro2 = open_database(db, key, read_only=True, create=False)
    assert ro2.execute("SELECT count(*) FROM entries").fetchone()[0] == 1
    ro2.close()


def test_read_only_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        open_database(tmp_path / "missing.db", secrets.token_bytes(32), read_only=True, create=False)
