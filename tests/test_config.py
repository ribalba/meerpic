"""The [sync] section with more than one remote: what it resolves to, and the
configurations it refuses before a sync could copy anything."""

from __future__ import annotations

import pytest

from core.config import Settings, remote_parent

SHARED = "iclouddrive:SharedSync-3535C497-14F6-44A1-BF2A-E9A5752153B9/All Photos"


def settings(tmp_path, **kw) -> Settings:
    roots = {"icloud": str(tmp_path / "iCloud"), "shared": str(tmp_path / "iCloud-Shared")}
    return Settings(**({"library_roots": roots} | kw))


def test_one_remote_is_the_default(tmp_path):
    s = settings(tmp_path)
    assert s.sync_sources == {"icloud": "iclouddrive:PrimarySync/All Photos"}
    assert s.shared_roots == []
    assert s.sync_keep_free == 10


def test_more_remotes_come_after_the_main_one(tmp_path):
    s = settings(tmp_path, sync_remotes={"shared": SHARED})
    assert list(s.sync_sources.items()) == [
        ("icloud", "iclouddrive:PrimarySync/All Photos"), ("shared", SHARED),
    ]
    assert s.shared_roots == ["shared"]


def test_only_a_sharedsync_zone_is_the_shared_library(tmp_path):
    s = settings(tmp_path, sync_remotes={"shared": "nas:Photos/SharedSync-ish"})
    assert s.shared_roots == []


def test_remote_parent():
    assert remote_parent("iclouddrive:PrimarySync/All Photos") == "iclouddrive:PrimarySync"
    assert remote_parent(SHARED) == "iclouddrive:SharedSync-3535C497-14F6-44A1-BF2A-E9A5752153B9"
    assert remote_parent("photos:All") == "photos:"
    assert remote_parent("/tmp/remote/All Photos/") == "/tmp/remote"


def test_a_remote_needs_a_root_of_its_own(tmp_path):
    with pytest.raises(ValueError, match="not one of library.roots"):
        settings(tmp_path, sync_remotes={"camera": SHARED})
    with pytest.raises(ValueError, match="sync.root's already"):
        settings(tmp_path, sync_remotes={"icloud": SHARED})
    with pytest.raises(ValueError, match="empty remote"):
        settings(tmp_path, sync_remotes={"shared": " "})


def test_a_remote_is_copied_once(tmp_path):
    with pytest.raises(ValueError, match="twice"):
        settings(tmp_path, sync_remotes={"shared": "iclouddrive:PrimarySync/All Photos/"})


@pytest.mark.parametrize("shared", ["{root}/iCloud", "{root}/iCloud/shared"])
def test_two_remotes_never_share_a_folder(tmp_path, shared):
    """Names repeat across iCloud libraries: one folder for both would have
    one library's IMG_1234.HEIC overwrite the other's."""
    roots = {"icloud": str(tmp_path / "iCloud"), "shared": shared.format(root=tmp_path)}
    with pytest.raises(ValueError, match="needs a folder of its own"):
        Settings(library_roots=roots, sync_remotes={"shared": SHARED})


def test_the_folder_rule_is_for_synced_roots_only(tmp_path):
    roots = {"icloud": str(tmp_path / "iCloud"), "inside": str(tmp_path / "iCloud" / "scans")}
    assert Settings(library_roots=roots).sync_sources == {"icloud": "iclouddrive:PrimarySync/All Photos"}


def test_nothing_is_checked_with_sync_off(tmp_path):
    s = settings(tmp_path, sync_enabled=False, sync_remotes={"camera": SHARED})
    assert s.sync_remotes == {"camera": SHARED}


def test_keep_free_is_not_negative(tmp_path):
    with pytest.raises(ValueError, match="keep_free"):
        settings(tmp_path, sync_keep_free=-1)
