"""
Minimal OAuth 2.0 client_credentials flow for the UCP adapter.

Issues HMAC-SHA256 signed JWTs. No external OAuth server required.

Environment variables:
    UCP_CLIENT_ID      — client id (default: ucp-agent)
    UCP_CLIENT_SECRET  — shared secret  (default: change-me)
    UCP_JWT_SECRET     — HMAC signing key for JWTs (default: change-me-jwt)
    UCP_TOKEN_TTL      — token lifetime seconds (default: 3600)
"""
from __future__ import annotations

import os
import time
from typing import Optional

import jwt  # PyJWT
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer

_CLIENT_ID = os.environ.get("UCP_CLIENT_ID", "ucp-agent")
_CLIENT_SECRET = os.environ.get("UCP_CLIENT_SECRET", "change-me")
_JWT_SECRET = os.environ.get("UCP_JWT_SECRET", "change-me-jwt")
_TTL = int(os.environ.get("UCP_TOKEN_TTL", "3600"))
_ALGO = "HS256"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/oauth/token", auto_error=False)


def issue_token(client_id: str) -> dict:
    now = int(time.time())
    token = jwt.encode(
        {"sub": client_id, "iat": now, "exp": now + _TTL},
        _JWT_SECRET,
        algorithm=_ALGO,
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": _TTL}


def handle_token_request(client_id: str, client_secret: str, grant_type: str) -> dict:
    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    if client_id != _CLIENT_ID or client_secret != _CLIENT_SECRET:
        raise HTTPException(401, "invalid_client")
    return issue_token(client_id)


def require_auth(token: Optional[str] = Depends(oauth2_scheme)) -> dict:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token")
    try:
        return jwt.decode(token, _JWT_SECRET, algorithms=[_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_token")
