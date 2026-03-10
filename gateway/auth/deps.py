from dataclasses import dataclass

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .jwt import JWTValidationError, verify_supabase_jwt


security = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    user_id: str
    email: str | None
    claims: dict


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> CurrentUser:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = credentials.credentials
    try:
        claims = verify_supabase_jwt(token)
    except JWTValidationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    return CurrentUser(
        user_id=claims["sub"],
        email=claims.get("email"),
        claims=claims,
    )

