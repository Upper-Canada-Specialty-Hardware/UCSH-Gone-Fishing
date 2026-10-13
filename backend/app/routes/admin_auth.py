"""Signing in to the admin dashboard, and changing its password (#228).

* POST /api/admin-auth/sign-in    - public: the password in, a session out.
* PUT  /api/dashboard/admin/password - needs a session like every other
                                     /admin route: change the password.

The session check on every /admin route is services.admin_auth.admin_guard,
attached in main.py.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.routes.intake import _client_ip
from app.services import admin_auth

public_router = APIRouter()      # mounted at /api/admin-auth, no session needed
router = APIRouter()             # mounted at /api/dashboard, behind admin_guard


class SignInBody(BaseModel):
    """Body of POST /sign-in."""
    password: str


class ChangePasswordBody(BaseModel):
    """Body of PUT /admin/password."""
    current: str
    new: str


@public_router.post("/sign-in")
async def sign_in(body: SignInBody, request: Request):
    """Check the admin password and start a 30-day session on this device.

    Args:
        body: The password typed.
        request: Used for the caller's IP (wrong-password limit).

    Returns:
        {"token", "exp"}; the page sends the token back as X-Admin-Session.

    Raises:
        HTTPException: 401 wrong password, 403 no password set, 429 too many tries.
    """
    try:
        return await admin_auth.sign_in(body.password, _client_ip(request))
    except admin_auth.AdminAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@router.put("/admin/password")
async def change_password(body: ChangePasswordBody):
    """Change the admin password; every other signed-in device is signed out.

    Args:
        body: The current password and the new one.

    Returns:
        A fresh {"token", "exp"} so this device stays signed in.

    Raises:
        HTTPException: 400 with a readable reason, 403 no password set.
    """
    try:
        return await admin_auth.change_password(body.current, body.new)
    except admin_auth.AdminAuthError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
