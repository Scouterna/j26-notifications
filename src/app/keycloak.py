import logging
from datetime import datetime, timezone

import httpx

from .config import get_settings
from .groups import group_path_id_to_name

logger = logging.getLogger(__name__)
settings = get_settings()

# Cached service-account token: (access_token, expires_at)
_kc_token_cache: tuple[str, datetime] | None = None

# Cached group path → Keycloak group ID (permanent, group IDs never change)
_group_id_cache: dict[str, str] = {}

# Only subgroups under this Keycloak path are considered valid channels. The
# prefix is stripped from group paths everywhere in the application, e.g. the
# Keycloak group "/j26-scoutid-sync/j26-leader/j26-rover" is treated as
# "/j26-leader/j26-rover".
GROUP_PREFIX = "/j26-scoutid-sync"

# Main groups (first path segment) that can only have members in their
# subgroups, never directly. Their bare main-group path is not a selectable
# channel (excluded from get_all_groups), and membership in one of their
# subgroups does NOT synthesize the parent main-group channel (unlike e.g.
# "/leader/rover", which also makes the user a member of "/leader").
SUBGROUP_ONLY_MAIN_GROUPS = {"/district", "/group", "/village"}


def _main_group(path: str) -> str:
    """Return the top-level main-group path of a channel, e.g. '/leader/rover'
    and '/leader' both map to '/leader'."""
    return "/" + path.lstrip("/").split("/", 1)[0]


def _find_group(groups: list, path: str) -> dict | None:
    """Recursively search a list of Keycloak group dicts (and their subGroups)
    for the one whose 'path' matches."""
    for g in groups:
        if g["path"] == path:
            return g
        found = _find_group(g.get("subGroups", []), path)
        if found:
            return found
    return None


async def get_kc_token() -> str:
    global _kc_token_cache
    now = datetime.now(timezone.utc)
    if _kc_token_cache and _kc_token_cache[1] > now:
        return _kc_token_cache[0]

    url = f"{settings.KC_API}/realms/{settings.KC_REALM}/protocol/openid-connect/token"
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


async def get_all_groups() -> list[str]:
    """Return all valid channel paths: every subgroup under GROUP_PREFIX, with
    that prefix stripped (e.g. '/j26-leader/j26-rover'), sorted.

    Fetched fresh from Keycloak on every call. TODO: once the group list is
    stable, cache the result (e.g. populate once at pod startup) instead of
    hitting Keycloak on every request."""
    token = await get_kc_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{settings.KC_API}/admin/realms/{settings.KC_REALM}"
    page_size = 100

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Resolve the GROUP_PREFIX root group.
        name = GROUP_PREFIX.rstrip("/").split("/")[-1]
        r = await client.get(f"{base}/groups", headers=headers, params={"search": name, "exact": "true"})
        r.raise_for_status()

        root = _find_group(r.json(), GROUP_PREFIX)
        if root is None:
            logger.warning("Group prefix not found: %s", GROUP_PREFIX)
            return []

        async def _children(group_id: str) -> list[dict]:
            out: list[dict] = []
            first = 0
            while True:
                r = await client.get(
                    f"{base}/groups/{group_id}/children",
                    headers=headers,
                    params={"first": first, "max": page_size},
                )
                r.raise_for_status()
                page = r.json()
                out.extend(page)
                if len(page) < page_size:
                    break
                first += page_size
            return out

        # Keycloak returns subgroups via the /children endpoint (inline
        # subGroups is empty under briefRepresentation); recurse using
        # subGroupCount to decide when to descend.
        paths: list[str] = []

        async def _walk(group: dict) -> None:
            paths.append(group["path"])
            if group.get("subGroupCount", 0):
                for child in await _children(group["id"]):
                    await _walk(child)

        for child in await _children(root["id"]):
            await _walk(child)

    # Drop bare subgroup-only main groups (e.g. "/group", "/village"): they are
    # not selectable channels, only their subgroups are.
    stripped = set(p[len(GROUP_PREFIX):] for p in paths) - SUBGROUP_ONLY_MAIN_GROUPS
    logger.debug("Fetched %d groups under %s", len(stripped), GROUP_PREFIX)
    # Translate "/group/<id>" -> "/group/<name>" so clients see human-readable
    # group names; all other channels pass through unchanged.
    return sorted(group_path_id_to_name(g) for g in stripped)


async def get_group_members(group_path: str) -> list[str]:
    """Return usernames of all members of the group identified by its stripped
    path (e.g. '/j26-leader/j26-rover'). The GROUP_PREFIX is prepended to
    resolve the actual Keycloak group."""
    token = await get_kc_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{settings.KC_API}/admin/realms/{settings.KC_REALM}"
    kc_path = GROUP_PREFIX + group_path

    async with httpx.AsyncClient(timeout=10.0) as client:
        if group_path in _group_id_cache:
            group_id = _group_id_cache[group_path]
        else:
            name = kc_path.rstrip("/").split("/")[-1]
            r = await client.get(f"{base}/groups", headers=headers, params={"search": name, "exact": "true"})
            r.raise_for_status()

            group = _find_group(r.json(), kc_path)
            if group is None:
                logger.warning("Group not found: %s", kc_path)
                return []

            group_id = group["id"]
            _group_id_cache[group_path] = group_id
            logger.debug("Cached group ID for %s: %s", group_path, group_id)
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
    """Return the user's valid channel paths: groups that are subgroups of
    GROUP_PREFIX, with that prefix stripped (e.g. "/j26-scoutid-sync/leader" ->
    "/leader").

    A member of a subgroup is also made a member of its main group, so that a
    notification to the main group reaches the subgroup's members too — except
    for SUBGROUP_ONLY_MAIN_GROUPS, whose main group is never a target. E.g.
    "/leader/rover" yields ["/leader/rover", "/leader"], but "/group/784"
    yields just ["/group/784"]."""
    token = await get_kc_token()
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{settings.KC_API}/admin/realms/{settings.KC_REALM}"

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

    prefix = GROUP_PREFIX + "/"
    paths = {g["path"][len(GROUP_PREFIX):] for g in groups if g["path"].startswith(prefix)}
    # Add the main group for each subgroup membership so main-group
    # notifications reach subgroup members too (skip subgroup-only main groups).
    for path in list(paths):
        main = _main_group(path)
        if main != path and main not in SUBGROUP_ONLY_MAIN_GROUPS:
            paths.add(main)
    result = sorted(paths)
    logger.debug("User %s is member of groups: %s", username, result)
    return result
