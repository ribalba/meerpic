"""The app around the routes: health, the single-page fallback, static files,
and the search model warming up without holding anything else up.

No database: /healthz must answer without one, the fallback is routing, and
the warm-up is a thread.
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from app import main, security


@pytest.fixture
def open_door(monkeypatch, tmp_path):
    monkeypatch.setattr(security.settings, "server_password", "")
    monkeypatch.setattr(main.settings, "server_password", "")
    monkeypatch.setattr(security.settings, "trusted_proxies", [])
    (tmp_path / "index.html").write_text("<!doctype html><title>shell</title>")
    monkeypatch.setattr(main, "STATIC_DIR", tmp_path)
    return tmp_path


def client() -> TestClient:
    return TestClient(main.app, client=("127.0.0.1", 44444))


def test_healthz_answers_without_the_database(open_door):
    r = client().get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_a_deep_link_gets_the_shell_and_it_is_revalidated(open_door):
    for path in ("/", "/map", "/photo/12"):
        r = client().get(path)
        assert r.status_code == 200, path
        assert "<title>shell</title>" in r.text
        assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/api/nothing", "/media/nothing/1", "/static/js/missing.js"])
def test_api_media_and_static_404s_are_json_not_the_shell(open_door, path):
    # A missing thumbnail answered with the app's HTML is an <img> that
    # decodes a web page; a missing script answered with it is a syntax error
    # in the console that points nowhere.
    r = client().get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


def test_a_missing_ui_is_said_plainly(open_door, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(main, "STATIC_DIR", empty)
    r = client().get("/")
    assert r.status_code == 404
    assert "index.html" in r.json()["detail"]


def test_static_files_must_be_revalidated_and_answer_304(tmp_path):
    (tmp_path / "app.js").write_text("console.log(1)")
    c = TestClient(Starlette(routes=[Mount("/static", main.RevalidatingStatic(directory=tmp_path))]))
    first = c.get("/static/app.js")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    again = c.get("/static/app.js", headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304
    assert not again.content


# --- warming the search model -------------------------------------------------


def test_warming_does_not_hold_up_startup(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(main.embed, "warm_text", lambda *a, **k: release.wait(10))
    started = time.monotonic()
    thread = main._warm_search_model()
    assert time.monotonic() - started < 0.5
    assert thread.is_alive() and thread.daemon
    release.set()
    thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize("error", [RuntimeError("no network"), SystemExit("unknown search.model")])
def test_a_model_that_will_not_load_is_logged_not_raised(monkeypatch, caplog, error):
    def fail(*_a, **_k):
        raise error

    monkeypatch.setattr(main.embed, "warm_text", fail)
    with caplog.at_level(logging.WARNING, logger="uvicorn.error"):
        thread = main._warm_search_model()
        thread.join(5)
    assert not thread.is_alive()
    assert "did not load" in caplog.text


def test_under_the_fake_model_warming_is_a_no_op():
    from core import embed

    assert embed.FAKE
    thread = main._warm_search_model()
    thread.join(5)
    assert embed.text_ready()
