"""Unit tests for the group name <-> ID channel-path translation."""

import json

import pytest

from app import groups
from app.groups import group_path_id_to_name, group_path_name_to_id


def test_known_id_to_name():
    assert group_path_id_to_name("/groups/784") == "/groups/Trollbäckens Scoutkår"


def test_known_name_to_id():
    assert group_path_name_to_id("/groups/Trollbäckens Scoutkår") == "/groups/784"


def test_name_with_slash_round_trips():
    # id 15750 -> "Equmenia/SMU Trelleborg": the '/' in the name must not be split.
    name_path = group_path_id_to_name("/groups/15750")
    assert name_path == "/groups/Equmenia/SMU Trelleborg"
    assert group_path_name_to_id(name_path) == "/groups/15750"


def test_name_with_trailing_space_round_trips():
    # id 15247 -> "Åby NSF Scoutkår " (trailing space): must be preserved verbatim.
    name_path = group_path_id_to_name("/groups/15247")
    assert name_path == "/groups/Åby NSF Scoutkår "
    assert group_path_name_to_id(name_path) == "/groups/15247"


@pytest.mark.parametrize("path", ["@all", "/leader/rover", "/villages/001", "/districts/amazonas", "username123"])
def test_non_group_paths_pass_through(path):
    assert group_path_id_to_name(path) == path
    assert group_path_name_to_id(path) == path


def test_unknown_id_passes_through():
    assert group_path_id_to_name("/groups/999999") == "/groups/999999"


def test_unknown_name_passes_through():
    assert group_path_name_to_id("/groups/Does Not Exist") == "/groups/Does Not Exist"


def test_round_trip_identity_over_all_ids():
    for gid in groups._ID_TO_NAME:
        path = f"/groups/{gid}"
        assert group_path_name_to_id(group_path_id_to_name(path)) == path


def test_duplicate_name_raises(tmp_path, monkeypatch):
    dup = tmp_path / "groups.json"
    dup.write_text(json.dumps({"1": "Same Name", "2": "Same Name"}), encoding="utf-8")
    monkeypatch.setattr(groups, "_GROUPS_FILE", dup)
    with pytest.raises(ValueError, match="Duplicate group name"):
        groups._load()
