"""Translation between scout-group numeric IDs and human-readable names for
`/group/<token>` channel paths.

Channels are path strings such as "/group/784", "/leader/rover", "/village/001"
or "@all". Only "/group/<numeric-id>" paths refer to scout groups in the
external membership system; their IDs are meaningless to humans. groups.json
maps id -> name (e.g. "784" -> "Trollbäckens Scoutkår"). We expose names at the
API boundary (outgoing /groups, incoming POST /notifications) while the rest of
the backend keeps using IDs for Keycloak member resolution and DB storage.

Only the "/group/" prefix is translated; every other channel passes through
unchanged. The token after "/group/" is taken as-is (never split further), so
names containing spaces or slashes round-trip correctly.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_GROUPS_FILE = Path(__file__).with_name("groups.json")
_GROUP_PREFIX = "/group/"


def _load() -> tuple[dict[str, str], dict[str, str]]:
    """Load groups.json into (id->name, name->id) dicts. Raises ValueError on
    duplicate names: names must be unique for the reverse lookup to be
    unambiguous, so a duplicate is a data bug that must surface loudly."""
    raw: dict[str, str] = json.loads(_GROUPS_FILE.read_text(encoding="utf-8"))
    id_to_name: dict[str, str] = {}
    name_to_id: dict[str, str] = {}
    for gid, name in raw.items():
        if name in name_to_id:
            raise ValueError(
                f"Duplicate group name in {_GROUPS_FILE.name!r}: {name!r} maps to "
                f"both id {name_to_id[name]!r} and {gid!r}"
            )
        id_to_name[gid] = name
        name_to_id[name] = gid
    logger.info("Loaded %d group name mappings from %s", len(id_to_name), _GROUPS_FILE.name)
    return id_to_name, name_to_id


_ID_TO_NAME, _NAME_TO_ID = _load()


def group_path_id_to_name(path: str) -> str:
    """'/group/784' -> '/group/Trollbäckens Scoutkår'. Non-'/group/' paths and
    unknown IDs are returned unchanged (unknown IDs are logged)."""
    if not path.startswith(_GROUP_PREFIX):
        return path
    gid = path[len(_GROUP_PREFIX):]
    name = _ID_TO_NAME.get(gid)
    if name is None:
        logger.warning("Unmapped group id passed through untranslated: %r", path)
        return path
    return _GROUP_PREFIX + name


def group_path_name_to_id(path: str) -> str:
    """'/group/Trollbäckens Scoutkår' -> '/group/784'. Non-'/group/' paths and
    unknown names are returned unchanged (unknown names are logged)."""
    if not path.startswith(_GROUP_PREFIX):
        return path
    name = path[len(_GROUP_PREFIX):]
    gid = _NAME_TO_ID.get(name)
    if gid is None:
        logger.warning("Unmapped group name passed through untranslated: %r", path)
        return path
    return _GROUP_PREFIX + gid
