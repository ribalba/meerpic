"""The password gate: the plaintext refusal, what lifts it, and the session.

meercal's tests for the same gate, adapted. ``server.password`` turns on a gate
whose first rule is that nothing is served over a plaintext connection to
anywhere but loopback: the browser that gets the page gets the login form, and
the password has crossed the network by then whatever the server says
afterwards. Behind a proxy that terminates TLS the connection the server sees
*is* plaintext, so the refusal has to be lifted by ``server.trusted_proxies``,
and the shape of that setting is most of this file.

None of this touches the database: the gate says no before any query runs.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from app import security


@pytest.fixture
def shell(tmp_path, monkeypatch):
    """A stand-in index.html: the real one belongs to the UI, and what is
    checked here is that the gate lets it through, not what is in it."""
    import app.main

    (tmp_path / "index.html").write_text('<!doctype html><div id="login-overlay"></div>')
    monkeypatch.setattr(app.main, "STATIC_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def gated(monkeypatch, shell):
    """A configured password, and no trusted proxies until a test says so."""
    monkeypatch.setattr(security.settings, "server_password", "hunter2")
    monkeypatch.setattr(security.settings, "trusted_proxies", [])
    return security.settings


@pytest.fixture
def open_door(monkeypatch, shell):
    monkeypatch.setattr(security.settings, "server_password", "")
    monkeypatch.setattr(security.settings, "trusted_proxies", [])
    return security.settings


def request_from(host: str, scheme: str = "http", **headers) -> Request:
    raw = [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "scheme": scheme,
            "headers": raw,
            "client": (host, 44444),
            "server": ("meerpic", 8000),
        }
    )


def test_loopback_needs_no_tls(gated):
    assert security.is_secure_request(request_from("127.0.0.1"))
    assert security.is_secure_request(request_from("::1"))


def test_plain_http_from_anywhere_else_is_refused(gated):
    assert not security.is_secure_request(request_from("203.0.113.9"))


def test_a_forwarded_header_alone_proves_nothing(gated):
    # Anything that can reach the port can send this. Without a trusted proxy
    # it is a claim from a stranger, not a fact about the connection.
    assert not security.is_secure_request(request_from("203.0.113.9", x_forwarded_proto="https"))


@pytest.mark.parametrize(
    "trusted",
    [
        ["10.0.1.7"],           # the literal address
        ["10.0.0.0/8"],         # the range it is in, which is what survives a redeploy
        ["*"],                  # the port is the boundary
        ["proxy.internal", "10.0.0.0/8"],  # a name it cannot parse, and a range it can
    ],
)
def test_a_trusted_proxy_is_believed(gated, monkeypatch, trusted):
    monkeypatch.setattr(security.settings, "trusted_proxies", trusted)
    assert security.is_secure_request(request_from("10.0.1.7", x_forwarded_proto="https"))


def test_a_chain_of_proxies_is_read_from_the_left(gated, monkeypatch):
    monkeypatch.setattr(security.settings, "trusted_proxies", ["10.0.0.0/8"])
    assert security.is_secure_request(request_from("10.0.1.7", x_forwarded_proto="https,https"))
    assert not security.is_secure_request(request_from("10.0.1.7", x_forwarded_proto="http,https"))


def test_trust_does_not_leak_across_ranges(gated, monkeypatch):
    monkeypatch.setattr(security.settings, "trusted_proxies", ["10.0.0.0/8"])
    assert not security.is_secure_request(request_from("172.17.0.4", x_forwarded_proto="https"))
    # An IPv4 address is not in an IPv6 network, and asking must not raise.
    monkeypatch.setattr(security.settings, "trusted_proxies", ["fd00::/8"])
    assert not security.is_secure_request(request_from("10.0.1.7", x_forwarded_proto="https"))


def test_https_needs_no_proxy_at_all(gated):
    assert security.is_secure_request(request_from("203.0.113.9", scheme="https"))


# --- tokens -------------------------------------------------------------------


def test_a_token_is_good_for_a_month_and_no_longer(gated):
    token = security.issue_token(now=1_000_000)
    assert security.token_valid(token, now=1_000_000 + security.MAX_AGE - 1)
    assert not security.token_valid(token, now=1_000_000 + security.MAX_AGE + 1)


def test_a_forged_or_mangled_token_is_refused(gated):
    token = security.issue_token()
    issued, _, sig = token.partition(".")
    assert not security.token_valid(f"{int(issued) + 1}.{sig}")
    assert not security.token_valid("garbage")
    assert not security.token_valid("")


def test_the_password_is_compared_exactly(gated):
    assert security.password_ok("hunter2")
    assert not security.password_ok("hunter")
    assert not security.password_ok("")


# --- the front door ---------------------------------------------------------


def client(scheme: str = "http", host: str = "10.0.1.7") -> TestClient:
    # No context manager: entering one runs the lifespan, which opens the
    # database, and nothing here gets as far as a query.
    from app.main import app

    return TestClient(app, base_url=f"{scheme}://photos.example.com", client=(host, 44444))


def test_the_shell_is_served_over_https(gated):
    r = client("https").get("/")
    assert r.status_code == 200
    assert 'id="login-overlay"' in r.text


def test_the_shell_is_refused_over_plaintext(gated):
    r = client().get("/")
    assert r.status_code == 403
    assert "trusted_proxies" in r.json()["detail"]
    assert "meerpic" in r.json()["detail"]


def test_a_trusted_proxy_opens_the_front_door(gated, monkeypatch):
    monkeypatch.setattr(security.settings, "trusted_proxies", ["10.0.0.0/8"])
    r = client().get("/", headers={"x-forwarded-proto": "https"})
    assert r.status_code == 200


def test_a_deep_link_is_the_shell_and_is_refused_the_same_way(gated):
    assert client("https").get("/map").status_code == 200
    assert client().get("/map").status_code == 403


def test_the_photos_themselves_still_need_the_session(gated):
    # The point of the split: the page is public, the photos are not. The
    # gate answers before any database is asked anything.
    c = client("https")
    assert c.get("/api/version").status_code == 401
    assert c.get("/api/photos").status_code == 401
    assert c.get("/media/thumb/1").status_code == 401
    assert c.get("/media/original/1?download=1").status_code == 401
    assert c.get("/media/story/1").status_code == 401
    assert c.get("/api/albums").status_code == 401
    assert c.post("/api/albums/refresh").status_code == 401
    assert c.post("/api/photos/delete/plan", json={"ids": [1]}).status_code == 401
    assert c.post("/api/photos/delete", json={"ids": [1], "commands": []}).status_code == 401


def test_the_auth_endpoints_are_outside_the_gate(gated):
    r = client("https").get("/api/auth/state")
    assert r.status_code == 200
    assert r.json() == {"required": True, "secure": True}


def test_signing_in_sets_a_cookie_that_opens_api_and_media(gated):
    c = client("https")
    assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"password": "hunter2"})
    assert r.status_code == 200
    cookie = r.cookies.get(security.COOKIE)
    assert cookie and security.token_valid(cookie)
    set_cookie = r.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "secure" in set_cookie and "samesite=lax" in set_cookie
    # The cookie is what an <img> sends; the bearer header is for scripts.
    assert c.get("/api/version").status_code == 200
    c.post("/api/auth/logout")
    assert c.get("/api/version").status_code == 401


def test_a_bearer_password_works_for_scripts(gated):
    r = client("https").get("/api/version", headers={"authorization": "Bearer hunter2"})
    assert r.status_code == 200
    assert r.json()["version"]


def test_a_password_is_never_taken_over_plaintext(gated):
    r = client().post("/api/auth/login", json={"password": "hunter2"})
    assert r.status_code == 403


def test_without_a_password_nothing_is_refused(open_door):
    assert client().get("/").status_code == 200
    assert client().get("/api/version").status_code == 200
    assert client().get("/api/auth/state").json()["required"] is False
