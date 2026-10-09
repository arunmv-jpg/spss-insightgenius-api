"""Test AUTH_DISABLED bypass (testing-only mode)."""

import pytest

from auth import ANONYMOUS_KEY
from config import get_settings


@pytest.fixture
def auth_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_disabled", True)


def test_no_header_allowed_when_disabled(client, auth_disabled, test_sav_bytes):
    """No Authorization header → served as anonymous."""
    response = client.post(
        "/v1/metadata",
        files={"file": ("test.sav", test_sav_bytes, "application/octet-stream")},
    )
    assert response.status_code == 200


def test_invalid_key_still_rejected_when_disabled(client, auth_disabled):
    """A key that IS sent is still validated."""
    response = client.post(
        "/v1/metadata",
        headers={"Authorization": "Bearer sk_test_wrong_key_999"},
        files={"file": ("test.sav", b"dummy", "application/octet-stream")},
    )
    assert response.status_code == 401


def test_upload_reaches_handler_when_disabled(client, auth_disabled, test_sav_bytes):
    """Upload passes auth; without Redis it fails later with 503, not 401."""
    response = client.post(
        "/v1/files/upload",
        files={"file": ("test.sav", test_sav_bytes, "application/octet-stream")},
    )
    assert response.status_code != 401


def test_keys_routes_still_require_auth(client, auth_disabled):
    """Key management never accepts anonymous."""
    response = client.get("/v1/keys")
    assert response.status_code == 401


def test_mcp_anonymous_when_disabled(auth_disabled):
    import asyncio
    from mcp_server.auth import _auth, _auth_async
    assert _auth("") is ANONYMOUS_KEY
    assert asyncio.run(_auth_async("")) is ANONYMOUS_KEY


def test_no_header_rejected_by_default(client):
    """Default (flag off) behaviour unchanged."""
    response = client.post(
        "/v1/metadata",
        files={"file": ("test.sav", b"dummy", "application/octet-stream")},
    )
    assert response.status_code == 401
