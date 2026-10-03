"""Image pipeline stress: run explicitly with ``pytest -m stress``.

N images through one ImageWorkerClient; records parent RSS and checks that
memory does not grow, workers are recycled and no process is left behind.
"""

from __future__ import annotations

import gc
import os
import time

import psutil
import pytest

from videogen.config.settings import ImageSettings, TimeoutPolicy
from videogen.workers.image_worker import ImageWorkerClient
from tests.fixtures import factory as F

N = int(os.environ.get("VIDEOGEN_STRESS_IMAGES", "300"))


@pytest.mark.stress
@pytest.mark.timeout(1800)
def test_many_images_bounded_memory(tmp_path):
    srcdir = tmp_path / "src"
    makers = [F.jpg, F.png, F.webp]
    srcs = []
    for i in range(N):
        if i % 50 == 17:
            srcs.append(F.corrupted_png(srcdir.mkdir(exist_ok=True) or srcdir / f"{i:05d}.png"))
        else:
            srcs.append(makers[i % 3](srcdir / f"{i:05d}.img", size=(1920, 1080)))
    me = psutil.Process()
    rss = []
    t0 = time.monotonic()
    with ImageWorkerClient(ImageSettings(), TimeoutPolicy(), ffmpeg=F.FFMPEG, recycle_after=100) as w:
        for i, s in enumerate(srcs):
            r = w.normalize(i, str(s), str(tmp_path / "norm" / f"i{i:05d}.jpg"), width=1920, height=1080,
                            overscan=1.15, vertical=False)
            assert r.ok or i % 50 == 17
            if i % 25 == 0:
                gc.collect()
                rss.append(me.memory_info().rss / 1048576)
        pids = list(w.pids)
    elapsed = time.monotonic() - t0
    print(f"\n{N} images in {elapsed:.1f}s ({N / elapsed:.1f}/s); parent RSS MB: "
          f"{[round(x) for x in rss]}; workers: {len(pids)}")
    assert rss[-1] - rss[1] < 30, rss          # no growth in the parent
    assert len(pids) == -(-N // 100)
    for pid in pids:
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
