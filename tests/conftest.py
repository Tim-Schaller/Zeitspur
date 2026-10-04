import secrets
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Jeder Test bekommt automatisch einen eigenen Datenordner, damit nie %LOCALAPPDATA%\\Zeitspur
    (config.yaml, key.bin, zeitspur.db) angefasst wird."""
    d = tmp_path / "data"
    d.mkdir(exist_ok=True)
    monkeypatch.setenv("ZEITSPUR_DATA_DIR", str(d))
    return d


@pytest.fixture
def data_dir(_isolated_data_dir):
    """Expliziter Zugriff auf den isolierten Datenordner."""
    return _isolated_data_dir


@pytest.fixture
def key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def storage(tmp_path, key):
    from zeitspur.storage import Storage

    s = Storage(tmp_path / "zeitspur.db", key)
    yield s
    s.close()
