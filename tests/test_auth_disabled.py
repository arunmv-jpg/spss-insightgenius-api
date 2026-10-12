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


@pytest.mark.parametrize("header", ["Bearer demo", "Bearer sk_test_wrong_key_999", "Bearer "])
def test_placeholder_or_invalid_key_falls_back_when_disabled(client, auth_disabled, test_sav_bytes, header):
    """Frontends send 'Bearer demo' — must not 401 when auth is disabled."""
    response = client.post(
        "/v1/metadata",
        headers={"Authorization": header},
        files={"file": ("test.sav", test_sav_bytes, "application/octet-stream")},
    )
    assert response.status_code == 200


def test_valid_key_still_honored_when_disabled(auth_disabled):
    from mcp_server.auth import _auth
    assert _auth("sk_test_quantipro_test_key_abc123").name == "Test Key"


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
    assert _auth("demo") is ANONYMOUS_KEY
    assert asyncio.run(_auth_async("")) is ANONYMOUS_KEY


def test_no_header_rejected_by_default(client):
    """Default (flag off) behaviour unchanged."""
    response = client.post(
        "/v1/metadata",
        files={"file": ("test.sav", b"dummy", "application/octet-stream")},
    )
    assert response.status_code == 401
