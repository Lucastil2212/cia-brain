"""Security hardening unit tests."""

import pytest

from cia_brain.paths import normalized_cas_path, path_under, require_sha256
from cia_brain.settings import Settings, assert_secure_settings
from cia_brain.sources import archive_url_allowed


def test_require_sha256_rejects_traversal():
    with pytest.raises(ValueError):
        require_sha256("../../../tmp/evil")
    with pytest.raises(ValueError):
        require_sha256("not-a-hash")
    digest = "a" * 64
    assert require_sha256(digest) == digest


def test_path_under_rejects_escape(tmp_path):
    root = tmp_path / "raw"
    root.mkdir()
    inside = root / "ab" / "cd" / "file.bin"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"x")
    assert path_under(inside, root) == inside.resolve()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope")
    with pytest.raises(ValueError):
        path_under(outside, root)
    with pytest.raises(ValueError):
        path_under(root / ".." / "secret.txt", root)


def test_normalized_cas_path_bound(tmp_path):
    s = Settings(data_dir=tmp_path, jwt_secret="test-secret-not-default-xx", _env_file=None)
    s.ensure_dirs()
    digest = "ab" + ("c" * 62)
    out = normalized_cas_path(s, digest)
    assert out.name == f"{digest}.json.gz"
    assert out.parent.name == "ab"
    assert out.is_relative_to(s.normalized_dir.resolve())


def test_assert_secure_settings_rejects_default_jwt(tmp_path):
    s = Settings(
        data_dir=tmp_path,
        database_url="postgresql://u:p@localhost/db",
        jwt_secret="dev-only-change-me",
        _env_file=None,
    )
    with pytest.raises(RuntimeError):
        assert_secure_settings(s)


def test_assert_secure_settings_ok_with_strong_secret(tmp_path):
    s = Settings(
        data_dir=tmp_path,
        database_url="postgresql://u:p@localhost/db",
        jwt_secret="local-dev-jwt-secret-change-me",
        _env_file=None,
    )
    assert_secure_settings(s)


def test_archive_url_rejects_dotdot_escape():
    s = Settings(archive_sources="state", allowed_hosts="www.cia.gov", _env_file=None)
    assert not archive_url_allowed(
        "https://history.state.gov/historicaldocuments/../secret",
        s,
    )
