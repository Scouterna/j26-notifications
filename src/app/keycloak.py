import logging
from datetime import datetime, timezone

import httpx

from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Cached service-account token: (access_token, expires_at)
_kc_token_cache: tuple[str, datetime] | None = None


async def get_kc_token() -> str:
    global _kc_token_cache
    now = datetime.now(timezone.utc)
    if _kc_token_cache and _kc_token_cache[1] > now:
        return _kc_token_cache[0]

    url = f"{settings.KC_ADMIN_API}/realms/{settings.KC_REALM}/protocol/openid-connect/token"
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(url, data={
            "grant_type": "client_credentials",
            "client_id": settings.KC_SA_ACCOUNT,
            "client_secret": settings.KC_SA_ACCOUNT_KEY,
        })
        response.raise_for_status()

    data = response.json()
    expires_at = datetime.fromtimestamp(now.timestamp() + data["expires_in"] - 30, tz=timezone.utc)
    _kc_token_cache = (data["access_token"], expires_at)
    logger.debug("Fetched new Keycloak service-account token, expires_in=%s", data["expires_in"])
    return data["access_token"]


async def get_group_members(group_path: str) -> list[str]:
    """Return usernames of all members of the group identified by path (e.g. '/scoutnet/784')."""
    token = await get_kc_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{settings.KC_ADMIN_API}/admin/realms/{settings.KC_REALM}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Search for group by path — API may return parent groups with the match nested inside
        name = group_path.rstrip("/").split("/")[-1]
        r = await client.get(f"{base}/groups", headers=headers, params={"search": name, "exact": "true"})
        r.raise_for_status()

        def _find(groups: list, path: str) -> dict | None:
            for g in groups:
                if g["path"] == path:
                    return g
                found = _find(g.get("subGroups", []), path)
                if found:
                    return found
            return None

        group = _find(r.json(), group_path)
        if group is None:
            logger.warning("Group not found: %s", group_path)
            return []

        group_id = group["id"]
        usernames: list[str] = []
        first = 0
        page_size = 100
        while True:
            r = await client.get(
                f"{base}/groups/{group_id}/members",
                headers=headers,
                params={"first": first, "max": page_size},
            )
            r.raise_for_status()
            page = r.json()
            usernames.extend(m["username"] for m in page)
            if len(page) < page_size:
                break
            first += page_size

    logger.debug("Group %s has %d members", group_path, len(usernames))
    return usernames


async def get_user_groups(username: str) -> list[str]:
    """Return group paths for the given username (e.g. 'scoutnet|3073781')."""
    token = await get_kc_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{settings.KC_ADMIN_API}/admin/realms/{settings.KC_REALM}"

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Resolve username to Keycloak user ID
        r = await client.get(f"{base}/users", headers=headers, params={"username": username, "exact": "true"})
        r.raise_for_status()
        users = r.json()
        if not users:
            logger.warning("User not found in Keycloak: %s", username)
            return []

        user_id = users[0]["id"]

        r = await client.get(f"{base}/users/{user_id}/groups", headers=headers)
        r.raise_for_status()
        groups = r.json()

    paths = [g["path"] for g in groups]
    logger.debug("User %s is member of groups: %s", username, paths)
    return paths
