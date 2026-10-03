from __future__ import annotations

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from videogen.core.models import ImageItem, ImageStatus, JobStatus, Stage
from videogen.storage import atomic, cleanup
from videogen.storage.manifest import JobManifest, SegmentRecord, read_manifest, write_manifest
from videogen.storage.workspace import MARKER_NAME, WorkspaceRoot, is_managed_path
from videogen.utils.hashing import sha256_file
from videogen.utils.paths import is_within, natural_sort_key, safe_filename, unique_output_path
from tests.helpers import make_config


# ---------------------------------------------------------------- atomic writes

def test_atomic_write_replaces_and_leaves_no_temp(tmp_path):
    p = tmp_path / "f.json"
    atomic.atomic_write_text(p, "one")
    atomic.atomic_write_text(p, "two")
    assert p.read_text() == "two"
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]


def test_crash_between_write_and_replace_keeps_old_file(tmp_path, monkeypatch):
    p = tmp_path / "f.json"
    atomic.atomic_write_text(p, "old-complete")

    def boom(src, dst):
        raise OSError("simulated power loss before rename")

    monkeypatch.setattr(atomic.os, "replace", boom)
    with pytest.raises(OSError):
        atomic.atomic_write_text(p, "new")
    monkeypatch.undo()
    assert p.read_text() == "old-complete"
    assert [x.name for x in tmp_path.iterdir()] == ["f.json"]  # temp removed


def test_replace_retries_transient_lock(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_text("x")
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("locked by antivirus")
        return real(a, b)

    monkeypatch.setattr(atomic.os, "replace", flaky)
    atomic.replace_with_retry(src, dst, delays=(0.01, 0.01, 0.01))
    assert dst.read_text() == "x" and calls["n"] == 3


def test_replace_gives_up_after_bounded_retries(tmp_path, monkeypatch):
    src = tmp_path / "a"
    src.write_text("x")
    monkeypatch.setattr(atomic.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("locked")))
    t0 = time.monotonic()
    with pytest.raises(PermissionError):
        atomic.replace_with_retry(src, tmp_path / "b", delays=(0.01, 0.01))
    assert time.monotonic() - t0 < 2


# ---------------------------------------------------------------- manifest

def test_manifest_roundtrip(tmp_path):
    m = JobManifest(config=make_config("Відео №1 & #%+"))
    m.status = JobStatus.PARTIAL
    m.stage = Stage.FINALIZING
    m.images = [ImageItem(0, "/in/a.jpg", ImageStatus.NORMALIZED, decoder="pillow"),
                ImageItem(1, "/in/b.png", ImageStatus.INVALID, reason_code="CORRUPT",
                          message="пошкоджений PNG", decoders_tried=["pillow", "opencv", "ffmpeg"])]
    m.segments = [SegmentRecord(0, 90, "DONE", 1234)]
    p = tmp_path / "manifest.json"
    write_manifest(p, m)
    back = read_manifest(p)
    assert back is not None
    assert back.to_dict() == m.to_dict()
    assert back.to_dict()["skipped_count"] == 1
    assert "Відео №1" in p.read_text(encoding="utf-8")  # readable UTF-8


@pytest.mark.parametrize("content", [b"", b"{", b'{"schema": 99}', b'{"schema": 1}'])
def test_corrupt_manifest_returns_none(tmp_path, content):
    p = tmp_path / "manifest.json"
    p.write_bytes(content)
    assert read_manifest(p) is None


# ---------------------------------------------------------------- workspace + cleanup

def _batch(tmp_path):
    root = WorkspaceRoot(tmp_path / "ws")
    root.ensure()
    return root.batch("abc123")


def test_workspace_layout_is_ascii_and_marked(tmp_path):
    b = _batch(tmp_path)
    j = b.job(7).create()
    assert (b.root / MARKER_NAME).is_file()
    for p in (j.norm_dir, j.audio_dir, j.seg_dir, j.out_dir, j.normalized_image(3), j.segment(12)):
        assert str(p.relative_to(tmp_path)).isascii()
    assert is_managed_path(j.root)
    assert not is_managed_path(tmp_path)


def test_unsafe_batch_id_rejected(tmp_path):
    root = WorkspaceRoot(tmp_path / "ws")
    with pytest.raises(ValueError):
        root.batch("../../etc")


def test_remove_tree_removes_everything(tmp_path):
    j = _batch(tmp_path).job(1).create()
    for i in range(50):
        (j.norm_dir / f"i{i:05d}.jpg").write_bytes(b"x" * 100)
    ro = j.seg_dir / "readonly.mp4"
    ro.write_bytes(b"x")
    os.chmod(ro, stat.S_IREAD)
    r = cleanup.remove_tree(j.root, deadline_s=10, retry_delays=(0.01,))
    assert r.ok, r
    assert not j.root.exists()
    assert r.removed_files == 51


def test_remove_tree_refuses_paths_outside_workspace(tmp_path):
    user_dir = tmp_path / "Мої фото"
    user_dir.mkdir()
    (user_dir / "precious.jpg").write_bytes(b"x")
    r = cleanup.remove_tree(user_dir, deadline_s=5)
    assert r.refused and not r.ok
    assert (user_dir / "precious.jpg").exists()


def test_remove_tree_reports_locked_file_without_hanging(tmp_path, monkeypatch):
    j = _batch(tmp_path).job(1).create()
    locked = j.out_dir / "locked.mp4"
    locked.write_bytes(b"x")
    (j.out_dir / "free.mp4").write_bytes(b"x")
    real_unlink = os.unlink

    def unlink(p, *a, **kw):
        if str(p).endswith("locked.mp4"):
            raise PermissionError("in use")
        return real_unlink(p, *a, **kw)

    monkeypatch.setattr(cleanup.os, "unlink", unlink)
    t0 = time.monotonic()
    r = cleanup.remove_tree(j.root, deadline_s=5, retry_delays=(0.01, 0.01))
    assert time.monotonic() - t0 < 3
    assert not r.ok
    assert any(f.endswith("locked.mp4") for f in r.failed)
    assert not (j.out_dir / "free.mp4").exists()


def test_remove_tree_honours_deadline_when_filesystem_hangs(tmp_path, monkeypatch):
    j = _batch(tmp_path).job(1).create()
    for i in range(5):
        (j.norm_dir / f"{i}.jpg").write_bytes(b"x")
    release = threading.Event()

    def hanging_unlink(p, *a, **kw):
        release.wait(30)  # simulates a dead network share
        raise OSError("dead share")

    monkeypatch.setattr(cleanup.os, "unlink", hanging_unlink)
    t0 = time.monotonic()
    r = cleanup.remove_tree(j.root, deadline_s=0.5, retry_delays=())
    elapsed = time.monotonic() - t0
    release.set()
    assert elapsed < 3
    assert r.timed_out and not r.ok


def test_remove_stale_parts_only_touches_our_files(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / ".video.mp4.part").write_bytes(b"x")
    (out / ".keep.mp4.part").write_bytes(b"x")
    (out / "video.mp4").write_bytes(b"x")
    (out / "user.part").write_bytes(b"x")
    removed = cleanup.remove_stale_parts(out, known_good={str(out / ".keep.mp4.part")})
    assert removed == [str(out / ".video.mp4.part")]
    assert sorted(p.name for p in out.iterdir()) == [".keep.mp4.part", "user.part", "video.mp4"]


# ---------------------------------------------------------------- paths / hashing

@pytest.mark.parametrize("raw,expected", [
    ("Відео (копія) & #%+'", "Відео (копія) & #%+'"),
    ('a<b>c:"d/e\\f|g?h*', "a_b_c__d_e_f_g_h_"),
    ("CON", "_CON"),
    ("nul.txt", "_nul.txt"),
    ("name. . ", "name"),
    ("", "video"),
    ("\x00\x1f", "__"),
])
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_truncates_long_names():
    assert len(safe_filename("д" * 400)) == 150


def test_natural_sort():
    names = ["img10.jpg", "IMG2.jpg", "img1.jpg", "img2b.jpg", "a.jpg", "Зображення 3.png"]
    assert sorted(names, key=natural_sort_key) == [
        "a.jpg", "img1.jpg", "IMG2.jpg", "img2b.jpg", "img10.jpg", "Зображення 3.png"]


def test_unique_output_path_never_overwrites(tmp_path):
    (tmp_path / "Відео.mp4").write_bytes(b"x")
    (tmp_path / ".Відео (2).mp4.part").write_bytes(b"x")
    assert unique_output_path(tmp_path, "Відео", ".mp4").name == "Відео (3).mp4"


def test_is_within(tmp_path):
    assert is_within(tmp_path / "a" / "b", tmp_path)
    assert not is_within(tmp_path.parent, tmp_path)
    assert not is_within(tmp_path / ".." / "x", tmp_path)


def test_sha256_full_and_partial(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    digest, mode = sha256_file(p)
    assert mode == "full"
    assert digest == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(3 * 1024 * 1024))
    d1, m1 = sha256_file(big, partial_threshold=1024)
    assert m1 == "partial" and len(d1) == 64


def test_unicode_and_long_paths_roundtrip(tmp_path):
    # 100 Cyrillic letters = ~220 UTF-8 bytes: close to the 255-byte limit
    name = "Довга назва " + "ї" * 100 + " (1) & #%+'.png"
    d = tmp_path / ("каталог " * 8).strip()
    d.mkdir()
    p = d / name
    atomic.atomic_write_bytes(p, b"data")
    assert Path(p).read_bytes() == b"data"
