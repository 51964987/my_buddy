import pytest

from kbserver.config import Config, check_listen_security


def _cfg(tmp_path, host, token=""):
    c = Config(path=tmp_path / "kbserver.config.json")
    c.data["kb_root"] = str(tmp_path / "kb")
    c.data["host"] = host
    c.data["token"] = token
    return c


def test_loopback_without_token_ok(tmp_path):
    for host in ("127.0.0.1", "localhost", "::1", ""):
        check_listen_security(_cfg(tmp_path, host))


def test_lan_requires_token(tmp_path):
    with pytest.raises(RuntimeError):
        check_listen_security(_cfg(tmp_path, "0.0.0.0"))
    with pytest.raises(RuntimeError):
        check_listen_security(_cfg(tmp_path, "192.168.1.10"))


def test_lan_with_token_ok(tmp_path):
    check_listen_security(_cfg(tmp_path, "0.0.0.0", token="s3cret"))
