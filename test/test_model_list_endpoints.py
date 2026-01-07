import asyncio

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Ensure tests are hermetic and don't touch real storages.
    for key in ("VALKEY_URL", "REDIS_URL", "POSTGRES_DSN", "MONGODB_URI"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setenv("API_PASSWORD", "pwd")
    monkeypatch.setenv("PANEL_PASSWORD", "pwd")
    monkeypatch.setenv("CREDENTIALS_DIR", str(tmp_path))

    # Reset global singletons so env changes apply.
    import src.storage_adapter as storage_adapter
    import src.credential_manager as credential_manager

    asyncio.run(storage_adapter.close_storage_adapter())
    credential_manager._credential_manager = None

    from web import app

    with TestClient(app) as test_client:
        yield test_client


def test_v1_models_gemini_format_with_key_param(client: TestClient):
    r = client.get("/v1/models?key=pwd")
    assert r.status_code == 200
    body = r.json()
    assert "models" in body
    assert isinstance(body["models"], list)
    assert "data" not in body


def test_v1_models_openai_format_with_bearer(client: TestClient):
    r = client.get("/v1/models", headers={"Authorization": "Bearer pwd"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("object") == "list"
    assert "data" in body
    assert isinstance(body["data"], list)
    assert "models" not in body


def test_v1_models_requires_auth_when_no_key(client: TestClient):
    r = client.get("/v1/models")
    assert r.status_code == 401


def test_antigravity_v1_models_gemini_format_with_key_param(client: TestClient):
    r = client.get("/antigravity/v1/models?key=pwd")
    assert r.status_code == 200
    body = r.json()
    assert "models" in body
    assert isinstance(body["models"], list)
    assert "data" not in body


def test_antigravity_v1_models_openai_format_without_auth(client: TestClient):
    r = client.get("/antigravity/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body.get("object") == "list"
    assert "data" in body
    assert isinstance(body["data"], list)
    assert "models" not in body

