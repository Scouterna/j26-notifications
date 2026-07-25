"""Unit tests for main-group / subgroup channel rules in keycloak.py:
subgroup membership synthesizes the main-group channel (except subgroup-only)
"""

import os

# keycloak.py calls get_settings() at import; provide dummy required settings.
os.environ.setdefault("POSTGRES_DSN", "postgresql://x")
os.environ.setdefault("FCM_PROJECT_ID", "x")
os.environ.setdefault("FCM_CREDENTIALS_JSON", "{}")
os.environ.setdefault("KC_SA_ACCOUNT_KEY", "x")

import pytest

from app.keycloak import SUBGROUP_ONLY_MAIN_GROUPS, _main_group


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/leader", "/leader"),
        ("/leader/rover", "/leader"),
        ("/staff/funktion-infra", "/staff"),
        ("/group/784", "/group"),
        ("/village/001", "/village"),
    ],
)
def test_main_group(path, expected):
    assert _main_group(path) == expected


# --- Parent-synthesis transformation (the logic applied inside get_user_groups) ---
# Tested as a pure function over the stripped path set, mirroring the code, so we
# don't need a live Keycloak.


def _synthesize(paths: set[str]) -> list[str]:
    paths = set(paths)
    for path in list(paths):
        main = _main_group(path)
        if main != path and main not in SUBGROUP_ONLY_MAIN_GROUPS:
            paths.add(main)
    return sorted(paths)


def test_subgroup_adds_main_group():
    assert _synthesize({"/leader/rover"}) == ["/leader", "/leader/rover"]


def test_deep_subgroup_adds_top_level_main_only():
    assert _synthesize({"/staff/funktion-infra"}) == ["/staff", "/staff/funktion-infra"]


def test_direct_main_group_is_noop():
    assert _synthesize({"/leader"}) == ["/leader"]


def test_subgroup_only_main_not_synthesized():
    assert _synthesize({"/group/784"}) == ["/group/784"]
    assert _synthesize({"/village/001"}) == ["/village/001"]
    assert _synthesize({"/district/amazonas"}) == ["/district/amazonas"]


def test_mixed_membership():
    assert _synthesize({"/leader/rover", "/group/784", "/staff"}) == [
        "/group/784",
        "/leader",
        "/leader/rover",
        "/staff",
    ]
