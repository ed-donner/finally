"""Tests for API routes using FastAPI TestClient."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app.db.database as db_module
from app.main import create_app
from app.market import PriceCache


@pytest.fixture
def client():
    """Create a test client with a temporary database."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = Path(tmp.name)
    tmp.close()
    db_path.unlink()

    os.environ["LLM_MOCK"] = "true"

    # Patch DB_PATH to use temp file
    with patch.object(db_module, "DB_PATH", db_path):
        # Reset the global connection
        db_module._connection = None
        application = create_app()
        with TestClient(application) as c:
            yield c

    os.environ.pop("LLM_MOCK", None)


class TestHealthEndpoint:
    def test_health(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}
