from kbserver.idgen import canonicalize_url, text_entry_id, url_to_id


def test_canonicalize_strips_tracking_and_fragment():
    assert (
        canonicalize_url("https://Docs.Example.com/a/b?utm_source=x&id=3&utm_medium=y#frag")
        == "https://docs.example.com/a/b?id=3"
    )


def test_canonicalize_param_order_and_defaults():
    a = canonicalize_url("https://example.com/p?b=2&a=1")
    b = canonicalize_url("https://EXAMPLE.com/p?a=1&b=2")
    assert a == b == "https://example.com/p?a=1&b=2"
    assert canonicalize_url("https://example.com:443/x") == canonicalize_url("https://example.com/x")
    assert canonicalize_url("http://example.com:80/x") == canonicalize_url("http://example.com/x")


def test_id_stable_and_distinct():
    u1 = "https://example.com/post/1?utm_campaign=z"
    u2 = "https://example.com/post/1"
    assert url_to_id(u1) == url_to_id(u2)
    assert url_to_id("https://example.com/post/2") != url_to_id(u2)
    assert len(url_to_id(u1)) == 12


def test_share_tracking_keys():
    assert (
        canonicalize_url("https://example.com/v?share_token=abc&k=1&share_medium=wx")
        == "https://example.com/v?k=1"
    )


def test_vtm_fingerprint_param_stripped():
    # docs.volcengine.com 的 _vtm_ 每次访问变化，若不清洗则同页每次剪藏都会生成新 id
    a = canonicalize_url("https://docs.example.com/p?_vtm_=a.b.13&lang=zh")
    b = canonicalize_url("https://docs.example.com/p?_vtm_=a.b.15&lang=zh")
    assert a == b == "https://docs.example.com/p?lang=zh"
    assert url_to_id("https://docs.example.com/p?_vtm_=x") == url_to_id("https://docs.example.com/p")


def test_text_entry_id():
    eid = text_entry_id()
    assert eid.startswith("t")
    assert "-" in eid
