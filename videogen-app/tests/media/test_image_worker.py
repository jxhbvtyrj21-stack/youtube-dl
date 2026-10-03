from __future__ import annotations

import threading
import time

import psutil
import pytest

from videogen.config.settings import ImageSettings, TimeoutPolicy
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import JobCancelledError
from videogen.core.models import ImageStatus
from videogen.workers.image_worker import TEST_HOOKS_ENV, ImageWorkerClient
from tests.fixtures import factory as F

FAST = TimeoutPolicy(image_base_s=1.5, image_per_mpx_s=0.1, image_max_s=3.0)


@pytest.fixture()
def hooks(monkeypatch):
    monkeypatch.setenv(TEST_HOOKS_ENV, "1")


def client(**kw):
    kw.setdefault("ffmpeg", F.FFMPEG)
    return ImageWorkerClient(ImageSettings(), kw.pop("tp", FAST), **kw)


def norm(w, i, src, tmp_path, **kw):
    return w.normalize(i, str(src), str(tmp_path / "norm" / f"i{i:05d}.jpg"), width=640, height=360,
                       overscan=1.0, vertical=False, **kw)


def assert_dead(pids):
    for pid in pids:
        if psutil.pid_exists(pid):
            try:
                assert psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                pass


def test_batch_with_several_bad_images_continues(tmp_path):
    srcs = [F.jpg(tmp_path / "1.jpg"), F.corrupted_png(tmp_path / "2.png"), F.png(tmp_path / "3.png"),
            F.zero_byte(tmp_path / "4.jpg"), F.webp(tmp_path / "5.webp"), F.corrupted_jpg(tmp_path / "6.jpg"),
            tmp_path / "missing.jpg", F.png_named_jpg(tmp_path / "8.jpg")]
    with client() as w:
        results = [norm(w, i, s, tmp_path) for i, s in enumerate(srcs)]
        pids = list(w.pids)
    ok = [r.index for r in results if r.ok]
    # #6 (truncated JPEG with zeroed data): Pillow refuses it; a fallback may
    # still decode it, but then it MUST be flagged as recovered, not clean.
    assert [i for i in ok if i != 5] == [0, 2, 4, 7]
    if 5 in ok:
        r5 = results[5]
        assert r5.recovered and r5.decoder != "pillow"
        assert any("пошкоджений" in x for x in r5.warnings)
    for i in (0, 2, 4, 7):
        assert not results[i].recovered
    bad = {r.index: r.reason_code for r in results if not r.ok}
    assert bad[3] == "EMPTY" and bad[6] == "NOT_FOUND"
    assert bad[1]
    assert 5 in bad or results[5].recovered
    for r in results:
        item = r.to_image_item()
        assert item.status is (ImageStatus.NORMALIZED if r.ok else ImageStatus.INVALID)
        if not r.ok:
            assert r.message and not (tmp_path / "norm" / f"i{r.index:05d}.jpg").exists()
    assert_dead(pids)


def test_fallback_decoder_after_python_failure(tmp_path, hooks):
    with client() as w:
        r = norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="fail:pillow")
    assert r.ok and r.decoder == "opencv" and r.decoders_tried == ["pillow", "opencv"]
    assert r.recovered     # the primary decoder failed on the data


def test_hanging_decoder_is_killed_and_next_decoder_used(tmp_path, hooks):
    with client() as w:
        t0 = time.monotonic()
        r = norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="hang:pillow")
        elapsed = time.monotonic() - t0
        assert w.restarts == 1
        pids = list(w.pids)
    assert r.ok and r.decoder == "opencv"
    assert elapsed < 10
    assert any("перевищено час" in x for x in r.warnings)
    assert_dead(pids)


def test_crashing_decoder_is_isolated(tmp_path, hooks):
    with client() as w:
        r = norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="crash:pillow")
        r2 = norm(w, 1, F.jpg(tmp_path / "b.jpg"), tmp_path)        # worker restarted, keeps going
        pids = list(w.pids)
    assert r.ok and r.decoder == "opencv"
    assert r2.ok and r2.decoder == "pillow"
    assert_dead(pids)


def test_image_crashing_every_decoder_is_marked_invalid(tmp_path, hooks):
    with client() as w:
        r = norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="crash")
        r2 = norm(w, 1, F.png(tmp_path / "b.png"), tmp_path)
    assert not r.ok and r.reason_code == "CRASH"
    assert r.decoders_tried == ["pillow", "opencv", "ffmpeg"]
    assert r2.ok


def test_image_hanging_every_decoder_is_bounded(tmp_path, hooks):
    with client() as w:
        t0 = time.monotonic()
        r = norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="hang")
        assert time.monotonic() - t0 < 20
    assert not r.ok and r.reason_code == "TIMEOUT"


def test_cancel_while_decoder_hangs(tmp_path, hooks):
    token = CancellationToken()
    with client() as w:
        threading.Timer(0.5, token.cancel).start()
        t0 = time.monotonic()
        with pytest.raises(JobCancelledError):
            norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="hang", token=token)
        assert time.monotonic() - t0 < 3
        pids = list(w.pids)
    assert_dead(pids)


def test_worker_is_recycled(tmp_path):
    srcs = [F.jpg(tmp_path / f"{i}.jpg") for i in range(5)]
    with client(recycle_after=2) as w:
        for i, s in enumerate(srcs):
            assert norm(w, i, s, tmp_path).ok
        pids = list(w.pids)
    assert len(pids) == 3                  # 2 + 2 + 1
    assert_dead(pids)


def test_hooks_ignored_without_env(tmp_path):
    with client() as w:
        r = norm(w, 0, F.png(tmp_path / "a.png"), tmp_path, debug_action="crash")
    assert r.ok and r.decoder == "pillow"
