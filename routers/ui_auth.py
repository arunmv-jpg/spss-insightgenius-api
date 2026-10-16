"""Web UI sign-in: one configured account and a signed session cookie.

This gates the web pages only. API endpoints keep using API keys (see auth.py).
Credentials come from UI_LOGIN_EMAIL / UI_LOGIN_PASSWORD; with either unset,
sign-in is refused.
"""

import base64
import hashlib
import hmac
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from config import get_settings

router = APIRouter(tags=["Web UI"], include_in_schema=False)

COOKIE_NAME = "ic_session"


class LoginBody(BaseModel):
    email: str = ""
    password: str = ""


def _configured() -> bool:
    s = get_settings()
    return bool(s.ui_login_email and s.ui_login_password)


def _secret() -> bytes:
    s = get_settings()
    if s.ui_session_secret:
        return s.ui_session_secret.encode()
    # Stable across workers and replicas; changing the password signs everyone out.
    return hashlib.sha256(f"ic-ui-session:{s.ui_login_email}:{s.ui_login_password}".encode()).digest()


def _sign(payload: bytes) -> str:
    return hmac.new(_secret(), payload, hashlib.sha256).hexdigest()


def make_session(email: str) -> str:
    exp = int(time.time()) + get_settings().ui_session_hours * 3600
    payload = base64.urlsafe_b64encode(json.dumps({"e": email, "x": exp}).encode())
    return f"{payload.decode()}.{_sign(payload)}"


def session_email(request: Request) -> str | None:
    """Email of the signed-in user, or None."""
    if not _configured():
        return None
    raw = request.cookies.get(COOKIE_NAME, "")
    payload, _, sig = raw.partition(".")
    if not payload or not hmac.compare_digest(_sign(payload.encode()), sig):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("x", 0) < time.time() or data.get("e") != get_settings().ui_login_email:
        return None
    return data["e"]


def _user(email: str) -> dict:
    return {"email": email, "name": get_settings().ui_login_name or email}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"success": False, "error": {"code": code, "message": message}})


@router.post("/auth/login")
async def login(body: LoginBody):
    s = get_settings()
    if not _configured():
        return _error(503, "LOGIN_NOT_CONFIGURED", "Sign-in isn't set up on this server.")
    email_ok = hmac.compare_digest(body.email.strip().lower().encode(), s.ui_login_email.strip().lower().encode())
    password_ok = hmac.compare_digest(body.password.encode(), s.ui_login_password.encode())
    if not (email_ok and password_ok):
        return _error(401, "INVALID_CREDENTIALS", "That email and password don't match.")
    resp = JSONResponse({"success": True, "data": _user(s.ui_login_email)})
    resp.set_cookie(
        COOKIE_NAME, make_session(s.ui_login_email),
        max_age=s.ui_session_hours * 3600, httponly=True, samesite="lax",
        secure=s.is_production, path="/",
    )
    return resp


@router.post("/auth/logout")
async def logout():
    resp = JSONResponse({"success": True, "data": {}})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/auth/me")
async def me(request: Request):
    email = session_email(request)
    if not email:
        return _error(401, "NOT_SIGNED_IN", "Not signed in.")
    return {"success": True, "data": _user(email)}
