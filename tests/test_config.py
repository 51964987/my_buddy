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


def test_partial_patch_merges(tmp_path):
    # 逐项即改即存（§11.5 v0.19）：部分补丁只改目标字段，其余保持原值
    c = _cfg(tmp_path, "127.0.0.1")
    c.apply_update({"ai": {"timeout": 120}})
    assert c.data["ai"]["timeout"] == 120
    assert c.data["ai"]["providers"]["ollama"]["model"] == "qwen2.5:1.5b"  # 未触及字段不变
    assert c.data["pipeline"]["worker_enabled"] is True  # 未触及顶层分区不变
    assert c.apply_update({"ai": {"timeout": 120}}) is False  # host/port 未变 → 无需重启


def test_patch_null_deletes_provider_and_persists(tmp_path):
    # 回归：provider 删除以 null 表达；深合并无法删键曾致删除永不持久化
    path = tmp_path / "kbserver.config.json"
    c = Config(path=path)
    c.apply_update({"ai": {"providers": {"myprov": {"base_url": "http://x", "model": "m", "api_key_env": "", "api": "openai"}}}})
    c.save()
    c2 = Config(path=path)
    assert "myprov" in c2.data["ai"]["providers"]
    c2.apply_update({"ai": {"providers": {"myprov": None}}})
    c2.save()
    c3 = Config(path=path)
    assert "myprov" not in c3.data["ai"]["providers"]


def test_patch_masked_token_skipped(tmp_path):
    # 掩码值（******）回传视为保持原值，不覆盖真值
    c = _cfg(tmp_path, "127.0.0.1", token="real-token")
    c.apply_update({"token": "******"})
    assert c.data["token"] == "real-token"
