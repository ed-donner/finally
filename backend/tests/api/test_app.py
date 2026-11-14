"""App wiring: lifespan, static serving, SSE route registration, chat router."""

from __future__ import annotations


def test_lifespan_starts_and_stops_source(make_client):
    client = make_client()
    src = client.source
    assert src.started
    assert len(src.get_tickers()) == 10
    client.__exit__(None, None, None)
    assert src.stopped


def test_restart_tracks_held_positions(make_client):
    c1 = make_client()
    c1.delete("/api/watchlist/AAPL")
    c1.app.state.price_cache.update("AAPL", 100.0)
    c1.post("/api/portfolio/trade", json={"ticker": "AAPL", "quantity": 1, "side": "buy"})
    c1.__exit__(None, None, None)
    c2 = make_client()
    assert "AAPL" in c2.source.get_tickers()


def test_routes_registered(client):
    paths = {r.path for r in client.app.routes}
    assert {"/api/stream/prices", "/api/health", "/api/portfolio", "/api/watchlist",
            "/api/chat"} <= paths


def test_no_static_dir_means_api_only(client):
    assert client.get("/").status_code == 404


def test_static_and_spa_fallback(make_client, tmp_path):
    static = tmp_path / "static"
    (static / "_next").mkdir(parents=True)
    (static / "index.html").write_text("<html>INDEX</html>")
    (static / "_next" / "app.js").write_text("console.log(1)")
    (static / "about.html").write_text("ABOUT")
    client = make_client(static)

    assert "INDEX" in client.get("/").text
    assert client.get("/_next/app.js").text == "console.log(1)"
    assert client.get("/about").text == "ABOUT"
    assert "INDEX" in client.get("/some/client/route").text
    # API routes still win and unknown API paths are real 404s
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/nope").status_code == 404
    # No path traversal outside the static dir
    (tmp_path / "secret.txt").write_text("SECRET")
    assert "SECRET" not in client.get("/../secret.txt").text
    assert "SECRET" not in client.get("/%2e%2e/secret.txt").text
