import threading
import time
from typing import Any

import requests
from jose import JWTError, jwt

from .. import config


class JWTValidationError(Exception):
    """Raised when bearer token validation fails."""


_jwks_cache_lock = threading.Lock()
_jwks_cache: dict[str, Any] = {"fetched_at": 0.0, "keys": []}


def _fetch_jwks() -> list[dict[str, Any]]:
    if not config.SUPABASE_JWKS_URL:
        raise JWTValidationError("SUPABASE_URL is not configured")
    try:
        response = requests.get(config.SUPABASE_JWKS_URL, timeout=5)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise JWTValidationError("Unable to fetch Supabase JWKS") from exc

    keys = payload.get("keys")
    if not isinstance(keys, list) or not keys:
        raise JWTValidationError("Supabase JWKS payload is invalid")
    return keys


def _get_jwks_keys() -> list[dict[str, Any]]:
    now = time.time()
    with _jwks_cache_lock:
        fetched_at = float(_jwks_cache.get("fetched_at", 0.0))
        cached_keys = _jwks_cache.get("keys", [])
        if cached_keys and (now - fetched_at) < config.SUPABASE_JWKS_CACHE_SECONDS:
            return cached_keys
        keys = _fetch_jwks()
        _jwks_cache["fetched_at"] = now
        _jwks_cache["keys"] = keys
        return keys


def _pick_key_for_token(token: str) -> tuple[Any, str]:
    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise JWTValidationError("Invalid token header") from exc

    kid = header.get("kid")
    alg = header.get("alg")
    if alg in {"HS256", "HS384", "HS512"}:
        if not config.SUPABASE_JWT_SECRET:
            raise JWTValidationError(
                "Token uses symmetric JWT signing but SUPABASE_JWT_SECRET is not configured"
            )
        return config.SUPABASE_JWT_SECRET, alg
    if alg not in {"RS256", "ES256"}:
        raise JWTValidationError(f"Unsupported JWT algorithm: {alg}")

    keys = _get_jwks_keys()
    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return key, alg
        raise JWTValidationError("No matching JWK found for token")

    # Fallback for tokens without kid: accept single-key projects.
    if len(keys) == 1:
        return keys[0], alg
    raise JWTValidationError("Token does not include key id (kid)")


def verify_supabase_jwt(token: str) -> dict[str, Any]:
    if not token:
        raise JWTValidationError("Missing bearer token")

    signing_key, alg = _pick_key_for_token(token)

    decode_kwargs: dict[str, Any] = {
        "algorithms": [alg],
        "options": {
            "verify_signature": True,
            "verify_exp": True,
            "verify_aud": config.SUPABASE_VERIFY_AUDIENCE,
            "verify_iss": config.SUPABASE_VERIFY_ISSUER,
        },
    }

    if config.SUPABASE_VERIFY_AUDIENCE:
        decode_kwargs["audience"] = config.SUPABASE_JWT_AUDIENCE
    if config.SUPABASE_VERIFY_ISSUER and config.SUPABASE_AUTH_ISSUER:
        decode_kwargs["issuer"] = config.SUPABASE_AUTH_ISSUER

    try:
        claims = jwt.decode(token, signing_key, **decode_kwargs)
    except JWTError as exc:
        raise JWTValidationError("Invalid or expired bearer token") from exc

    sub = claims.get("sub")
    if not sub:
        raise JWTValidationError("Token missing subject (sub)")
    return claims
