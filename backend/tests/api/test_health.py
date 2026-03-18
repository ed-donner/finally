"""Tests for health endpoint."""


async def test_health(app_client):
    resp = await app_client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
