from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .settings import ManagerSettings, get_settings


_security = HTTPBearer(auto_error=False)


def require_internal_bearer_token(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    settings: ManagerSettings = Depends(get_settings),
) -> None:
    configured_token = settings.ace_internal_api_token.strip()
    if not configured_token:
        raise HTTPException(status_code=500, detail="ACE_INTERNAL_API_TOKEN is not configured")

    if credentials is None or (credentials.scheme or "").lower() != "bearer":
        raise HTTPException(status_code=401, detail="Missing authorization")

    if not hmac.compare_digest(credentials.credentials or "", configured_token):
        raise HTTPException(status_code=401, detail="Invalid authorization")

