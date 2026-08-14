"""Local archive tests (FR-9, design doc 5.6)."""

from __future__ import annotations

import os
from datetime import datetime

import pytest

from screenrecon import storage


def test_new_stem_uses_the_timestamp_format(tmp_path):
    stem = storage.new_stem(tmp_path, now=datetime(2026, 8, 12, 14, 30, 52))
    assert stem == "20260812_143052"


def test_new_stem_backs_off_from_collisions(tmp_path):
    when = datetime(2026, 8, 12, 14, 30, 52)
    (tmp_path / "20260812_143052.jpg").write_bytes(b"")
    assert storage.new_stem(tmp_path, now=when) == "20260812_143052_1"

    (tmp_path / "20260812_143052_1.txt").write_text("", encoding="utf-8")
    assert storage.new_stem(tmp_path, now=when) == "20260812_143052_2"


def test_new_stem_still_backs_off_from_legacy_png_files(tmp_path):
    """Users with archives from before 0.1.5 still have .png files; a new .jpg
    must not shadow the same-second .png sitting next to it."""
    when = datetime(2026, 8, 12, 14, 30, 52)
    (tmp_path / "20260812_143052.png").write_bytes(b"")
    assert storage.new_stem(tmp_path, now=when) == "20260812_143052_1"


def test_new_stem_treats_image_and_txt_as_a_pair(tmp_path):
    """A .txt alone still blocks the stem, so the pair never splits across names."""
    when = datetime(2026, 8, 12, 14, 30, 52)
    (tmp_path / "20260812_143052.txt").write_text("", encoding="utf-8")
    assert storage.new_stem(tmp_path, now=when) == "20260812_143052_1"


def test_save_jpeg_and_txt_round_trip(tmp_path):
    directory = storage.resolve_dir(tmp_path / "archive")
    image = storage.save_jpeg(directory, "shot", b"\xff\xd8\xff-jpeg-data")
    txt = storage.save_txt(directory, "shot", "recognised text")

    assert image is not None and image.read_bytes() == b"\xff\xd8\xff-jpeg-data"
    assert txt is not None and txt.read_text(encoding="utf-8") == "recognised text"


def test_save_leaves_no_temporary_files_behind(tmp_path):
    directory = storage.resolve_dir(tmp_path / "archive")
    storage.save_jpeg(directory, "shot", b"data")
    assert sorted(p.name for p in directory.iterdir()) == ["shot.jpg"]


def test_unicode_text_is_written_as_utf8(tmp_path):
    directory = storage.resolve_dir(tmp_path / "archive")
    target = storage.save_txt(directory, "shot", "naïve café 数据")
    assert target is not None
    assert target.read_text(encoding="utf-8") == "naïve café 数据"


def test_resolve_dir_creates_missing_parents(tmp_path):
    directory = storage.resolve_dir(tmp_path / "a" / "b" / "c")
    assert directory.is_dir()


def test_resolve_dir_expands_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert storage.resolve_dir("~/Shots") == (tmp_path / "Shots").resolve()


def test_normalise_dir_strips_trailing_whitespace(tmp_path):
    """Windows drops a trailing space when creating the directory but keeps it in
    the path, which would make every later write fail."""
    assert storage.normalise_dir(f"{tmp_path}{os.sep}shots ") == (tmp_path / "shots").resolve()


def test_normalise_dir_expands_environment_variables(monkeypatch, tmp_path):
    monkeypatch.setenv("SCREENRECON_TEST_BASE", str(tmp_path))
    resolved = storage.normalise_dir(f"%SCREENRECON_TEST_BASE%{os.sep}shots")
    if os.name == "nt":
        assert resolved == (tmp_path / "shots").resolve()
    else:
        resolved = storage.normalise_dir(f"$SCREENRECON_TEST_BASE{os.sep}shots")
        assert resolved == (tmp_path / "shots").resolve()


def test_save_failure_warns_and_returns_none(tmp_path, capsys, monkeypatch):
    """Design doc 5.6: a write failure warns; it never interrupts the watch loop."""
    directory = storage.resolve_dir(tmp_path / "archive")

    def explode(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(storage, "write_private_bytes", explode)
    monkeypatch.setattr(storage, "write_private_text", explode)

    assert storage.save_jpeg(directory, "shot", b"data") is None
    assert storage.save_txt(directory, "shot", "text") is None
    output = capsys.readouterr().out
    assert output.count("[warn]") == 2
    assert "No space left" in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_existing_directories_are_never_chmodded(tmp_path):
    """save_dir and a --config parent can be $HOME, a shared folder, or /etc.

    Tightening a directory we did not create would break everything else that
    reads it.
    """
    import stat

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)

    storage.make_private_dir(shared)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_archive_is_owner_only(tmp_path):
    """Captures can contain anything that was on screen."""
    import stat

    directory = storage.resolve_dir(tmp_path / "archive")
    image = storage.save_jpeg(directory, "shot", b"data")

    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert image is not None
    assert stat.S_IMODE(image.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits do not apply on Windows")
def test_private_write_is_never_briefly_world_readable(tmp_path):
    """The credentials must not exist at the umask default even for an instant."""
    import stat

    target = tmp_path / "secret.json"
    storage.write_private_text(target, '{"key": "value"}')
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_private_write_replaces_a_symlinked_destination(tmp_path):
    """A symlink planted at the destination must be replaced, not followed."""
    outside = tmp_path / "attacker.json"
    outside.write_text("", encoding="utf-8")
    target = tmp_path / "config.json"
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available for this user")

    storage.write_private_text(target, "secret")
    assert not target.is_symlink()
    assert target.read_text(encoding="utf-8") == "secret"
    assert outside.read_text(encoding="utf-8") == ""
