import logging
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import HTTPException, Request, status
from joserfc import jwt
from joserfc.jwk import KeySet
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Cache the verification key set per origin (base_url) it was discovered from.
_jwks_keyset_cache: dict[str, KeySet] = {}


class AuthUser(BaseModel):
    subject: str
    name: str | None = None
    preferred_username: str
    email: str | None = None
    roles: list[str] = Field(default_factory=list)
    # claims: Dict[str, Any]

    def __str__(self) -> str:
        uid = self.preferred_username or self.subject
        return f"{self.name or uid} ({uid})"


async def _fetch_json(url: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(timeout=5.0) as http_client:
            response = await http_client.get(url)
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


async def _get_jwks_keyset(request: Request) -> KeySet | None:
    """
    Discover the JWKS via the OIDC metadata served by the '/auth' app in front of
    us (the framework's auth-app in prod, the local auth router in dev), so we never
    hardcode the IdP realm/certs URL. Cached per origin.
    """
    base_url = str(request.base_url)
    if base_url in _jwks_keyset_cache:
        return _jwks_keyset_cache[base_url]

    metadata = await _fetch_json(urljoin(base_url, "auth/.well-known/openid-configuration"))
    jwks_uri = metadata.get("jwks_uri") if metadata else None
    if not jwks_uri:
        logger.warning("OIDC metadata missing jwks_uri")
        return None

    jwks_dict = await _fetch_json(jwks_uri)
    if not jwks_dict:
        return None

    try:
        keyset = KeySet.import_key_set(jwks_dict)
    except Exception as exc:
        logger.warning("Failed to parse JWKS: %s", exc)
        return None

    _jwks_keyset_cache[base_url] = keyset
    return keyset


async def _decode_access_token(token: str, request: Request) -> dict[str, Any]:
    keyset = await _get_jwks_keyset(request)
    if keyset is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Token validation unavailable")

    try:
        token_obj = jwt.decode(token, keyset)
        registry = jwt.JWTClaimsRegistry(leeway=30)
        registry.validate(token_obj.claims)
        return dict(token_obj.claims)
    except Exception as exc:
        logger.warning("Failed to validate JWT: %s", exc)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized") from exc


def _extract_roles(claims: dict[str, Any]) -> list[str]:
    roles = set()
    realm_access = claims.get("realm_access") or {}
    realm_roles = realm_access.get("roles") or []
    roles.update(role for role in realm_roles if isinstance(role, str))

    resource_access = claims.get("resource_access") or {}
    for client_id, resource in resource_access.items():
        resource_roles = resource.get("roles") if isinstance(resource, dict) else []
        roles.update(f"{client_id}:{role}" for role in (resource_roles or []) if isinstance(role, str))

    return sorted(roles)


async def _build_auth_user(token: str, request: Request) -> AuthUser:
    claims = await _decode_access_token(token, request)
    return AuthUser(
        subject=claims.get("sub", ""),
        name=claims.get("name"),
        preferred_username=claims.get("preferred_username"),
        email=claims.get("email"),
        roles=_extract_roles(claims),
    )


def _extract_token(request: Request) -> str | None:
    token = request.cookies.get("j26-auth_access-token")
    if not token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[len("Bearer ") :]
    return token or None


# --- Authentication dependeny functions ---
async def optional_auth_user(request: Request) -> AuthUser | None:
    token = _extract_token(request)
    if not token:
        return None
    try:
        return await _build_auth_user(token, request)
    except HTTPException:
        return None


async def require_auth_user(request: Request) -> AuthUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    return await _build_auth_user(token, request)
