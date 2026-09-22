from cia_brain.auth import hash_api_key, hash_password, verify_password
from cia_brain.ratelimit import check_rate_limit
from cia_brain.settings import Settings


def test_password_roundtrip():
    s = Settings(jwt_secret="test", _env_file=None)
    stored = hash_password("correct-horse-battery", s)
    assert verify_password("correct-horse-battery", stored, s)
    assert not verify_password("wrong-password", stored, s)


def test_api_key_hash_stable():
    assert hash_api_key("cia_abc") == hash_api_key("cia_abc")
    assert hash_api_key("cia_abc") != hash_api_key("cia_xyz")


def test_memory_rate_limit(tmp_path):
    s = Settings(data_dir=tmp_path, database_url="", _env_file=None)
    s.ensure_dirs()
    for _ in range(3):
        ok, headers = check_rate_limit("ip:test:m", limit=3, window_seconds=60, settings=s)
        assert ok
        assert "X-RateLimit-Remaining" in headers
    ok, headers = check_rate_limit("ip:test:m", limit=3, window_seconds=60, settings=s)
    assert not ok
    assert headers["X-RateLimit-Remaining"] == "0"
