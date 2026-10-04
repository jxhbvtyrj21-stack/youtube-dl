"""Gap K: deterministic visual sanity of the produced video (not computer
vision). Every input image is a field of its own bright colour with a white
block in the middle, so each decoded frame can be attributed to an image:

* the images appear in the input order, each exactly once;
* no black / empty frame anywhere (also not inside cross-fades);
* while an image is on screen its content is there (colour field + white
  centre) despite Ken Burns zoom/pan and, for 9:16, the blurred background;
* cross-fade frames are a blend of the two neighbouring images, nothing else;
* the frame count equals what was decoded and what verification promised.
"""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from videogen.config.settings import EffectsSettings, TimeoutPolicy
from videogen.core import events as ev
from videogen.core.engine import Engine
from videogen.core.models import JobStatus
from videogen.media.media_validator import probe_media
from tests.fixtures import factory as F
from tests.pipeline_support import Events, small_settings

PALETTE = [(220, 40, 40), (40, 200, 60), (40, 80, 230), (230, 210, 40), (210, 50, 200), (40, 200, 210)]
SECONDS_PER_IMAGE = 2.0
FPS = 24
TRANSITION_S = 0.5


def _images(folder: Path) -> None:
    folder.mkdir(parents=True)
    for i, c in enumerate(PALETTE):
        im = Image.new("RGB", (1280, 720), c)
        ImageDraw.Draw(im).rectangle((1280 * 35 // 100, 720 * 35 // 100, 1280 * 65 // 100, 720 * 65 // 100),
                                     fill=(255, 255, 255))
        im.save(folder / f"{i + 1:02d}.png")
    F.tone(folder / "voice.mp3", SECONDS_PER_IMAGE * len(PALETTE))


def _decode(path: Path, w: int, h: int) -> list[bytes]:
    raw = subprocess.run([F.FFMPEG, "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h}:flags=area",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    n = w * h * 3
    assert len(raw) % n == 0
    return [raw[i:i + n] for i in range(0, len(raw), n)]


def _mean(px: list[tuple[int, int, int]]) -> tuple[float, float, float]:
    return tuple(sum(p[k] for p in px) / len(px) for k in range(3))  # type: ignore[return-value]


def _dist(a, b) -> float:
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5


def _analyse(frame: bytes, w: int, h: int):
    px = [tuple(frame[i:i + 3]) for i in range(0, len(frame), 3)]
    at = lambda x, y: px[y * w + x]          # noqa: E731
    if w > h:      # 16:9: the image fills the frame -> its colour field is the outer ring
        border = [at(x, y) for y in range(h) for x in range(w) if x < 2 or y < 2 or x >= w - 2 or y >= h - 2]
        bg_luma = 255.0
    else:          # 9:16: the landscape image sits in the middle band over a darkened blurred background
        border = [at(x, y) for y in range(h // 2 - 2, h // 2 + 2) for x in (0, 1, w - 2, w - 1)]
        bg = _mean([at(x, y) for y in (0, 1, h - 2, h - 1) for x in range(w)])
        bg_luma = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    cw, ch = max(2, w // 8), max(2, h // 8)
    ys, xs = range(h // 2 - ch // 2, h // 2 + ch // 2), range(w // 2 - cw // 2, w // 2 + cw // 2)
    centre = [at(x, y) for y in ys for x in xs]
    whole = _mean(px)
    luma = 0.299 * whole[0] + 0.587 * whole[1] + 0.114 * whole[2]
    b = _mean(border)
    dists = [_dist(b, c) for c in PALETTE]
    label = dists.index(min(dists)) if min(dists) < 60 else None
    return label, b, min(_mean(centre)), luma, bg_luma


def _produce(tmp_path: Path, orientation: str) -> tuple[list, int]:
    inp = tmp_path / "in"
    _images(inp / "кольори")
    s = small_settings(effects=EffectsSettings(ken_burns=True, transitions=True, transition_s=TRANSITION_S))
    s = dataclasses.replace(s, video=dataclasses.replace(s.video, fps=FPS))
    eng = Engine(tmp_path / "appdata", s, Events())
    eng.startup()
    try:
        eng.start_batch(ev.StartBatch("A", orientation, str(inp), str(tmp_path / "out"), str(tmp_path / "ws")))
        assert eng.wait_idle(300)
        [job] = eng.state.list_jobs()
    finally:
        eng.shutdown()
    assert job.status is JobStatus.SUCCESS, job.error
    out = Path(job.output_file)
    w, h = (32, 18) if orientation == "16:9" else (18, 32)
    frames = _decode(out, w, h)
    probed = probe_media(F.FFPROBE, out, TimeoutPolicy()).video_frames
    expected = round(SECONDS_PER_IMAGE * len(PALETTE) * FPS)
    assert len(frames) == probed and abs(probed - expected) <= 1, (len(frames), probed, expected)
    return [_analyse(f, w, h) for f in frames], len(frames)


def _problems(rows: list, n_frames: int) -> list[str]:
    problems: list[str] = []
    # 1. no black / empty frame anywhere
    dark = [i for i, r in enumerate(rows) if r[3] < 40 or r[4] < 15]
    if dark:
        problems.append(f"dark frames: {[(i, round(rows[i][3]), round(rows[i][4])) for i in dark[:10]]}")
    # 2. the images appear in input order, each exactly once
    seq: list[int] = []
    for label, *_ in rows:
        if label is not None and (not seq or seq[-1] != label):
            seq.append(label)
    if seq != list(range(len(PALETTE))):
        problems.append(f"order: {seq}")
    # 3. while an image is on screen its content is there (colour field + white centre)
    weak = [i for i, r in enumerate(rows) if r[0] is not None and r[2] < 170]
    if weak:
        problems.append(f"white centre missing (bad crop / empty content): {weak[:10]}")
    runs: dict[int, int] = {}
    for label, *_ in rows:
        if label is not None:
            runs[label] = runs.get(label, 0) + 1
    share = n_frames / len(PALETTE)
    if len(runs) != len(PALETTE) or not all(0.6 * share <= v <= 1.2 * share for v in runs.values()):
        problems.append(f"screen time per image: {runs} (share {share:.0f})")
    # 4. unlabelled frames are cross-fades: short, only between neighbours, a blend of the two
    blends = [i for i, r in enumerate(rows) if r[0] is None]
    max_tr = int(TRANSITION_S * FPS) + 2
    groups: list[list[int]] = []
    for i in blends:
        if groups and i == groups[-1][-1] + 1:
            groups[-1].append(i)
        else:
            groups.append([i])
    if len(groups) > len(PALETTE) - 1 or any(len(g) > max_tr for g in groups):
        problems.append(f"unexplained frame runs: {[(g[0], len(g)) for g in groups]}")
    for g in groups:
        before = rows[g[0] - 1][0] if g[0] > 0 else None
        after = rows[g[-1] + 1][0] if g[-1] + 1 < len(rows) else None
        if before is None or after != before + 1:
            problems.append(f"transition {g[0]}..{g[-1]} not between neighbours ({before} -> {after})")
            continue
        ca, cb = PALETTE[before], PALETTE[after]
        for i in g:
            col = rows[i][1]
            seg = [cb[k] - ca[k] for k in range(3)]
            tt = max(0.0, min(1.0, sum((col[k] - ca[k]) * seg[k] for k in range(3)) / sum(x * x for x in seg)))
            if _dist(col, [ca[k] + tt * seg[k] for k in range(3)]) >= 60:
                problems.append(f"frame {i} is not a blend of images {before} and {after}: {col}")
    return problems


@pytest.mark.timeout(600)
@pytest.mark.parametrize("orientation", ["16:9", "9:16"])
def test_frames_show_the_right_images_in_order_without_black_frames(tmp_path, orientation):
    rows, n = _produce(tmp_path, orientation)
    assert _problems(rows, n) == []


def _tamper_normalized(monkeypatch, action):
    from videogen.core import pipeline as pl
    real = pl.MediaPipeline._normalize

    def normalize(self, run, sources):
        items = real(self, run, sources)
        action(run)
        return items
    monkeypatch.setattr(pl.MediaPipeline, "_normalize", normalize)


@pytest.mark.timeout(600)
def test_negative_control_wrong_order_is_detected(tmp_path, monkeypatch):
    """The checker must catch a real ordering bug, not just pass."""
    def swap(run):
        a, b = run.ws.normalized_image(1), run.ws.normalized_image(3)
        tmp = a.with_name("swap.tmp")
        a.rename(tmp)
        b.rename(a)
        tmp.rename(b)
    _tamper_normalized(monkeypatch, swap)
    rows, n = _produce(tmp_path, "16:9")
    assert any(p.startswith("order") for p in _problems(rows, n))


@pytest.mark.timeout(600)
def test_negative_control_black_image_is_detected(tmp_path, monkeypatch):
    def blacken(run):
        p = run.ws.normalized_image(2)
        with Image.open(p) as im:
            size, fmt = im.size, im.format
        Image.new("RGB", size, (0, 0, 0)).save(p, fmt)
    _tamper_normalized(monkeypatch, blacken)
    rows, n = _produce(tmp_path, "16:9")
    assert any(p.startswith("dark frames") for p in _problems(rows, n))
