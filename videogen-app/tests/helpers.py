"""Shared test helpers: job factory and a scriptable fake executor."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from videogen.core.errors import VideoGenError
from videogen.core.job_manager import JobContext, JobOutcome
from videogen.core.models import ErrorInfo, JobConfig, JobState, JobStatus, Mode, Orientation, Stage
from videogen.core.state_manager import StateManager


def make_config(name: str = "job", batch_id: str = "b1", job_id: str | None = None) -> JobConfig:
    return JobConfig(
        job_id=job_id or uuid.uuid4().hex[:10], batch_id=batch_id, name=name,
        mode=Mode.AUDIO_IMAGES, orientation=Orientation.HORIZONTAL,
        input_dir=f"/in/{name}", output_dir="/out", width=1920, height=1080, fps=30,
        image_files=("a.jpg", "b.png"), audio_file="a.mp3")


def add_jobs(state: StateManager, n: int, batch_id: str = "b1") -> list[str]:
    ids = []
    for i in range(n):
        cfg = make_config(f"job{i:03d}", batch_id)
        state.add_job(cfg, i, f"/ws/j{i:04d}")
        ids.append(cfg.job_id)
    return ids


Action = Callable[[JobContext], JobOutcome] | str | BaseException | type


@dataclass
class FakeExecutor:
    """``script[job_name]`` is a list of actions, one per attempt.

    Actions: "ok", "partial", "block" (loops on checkpoints until cancelled,
    bounded to 30 s), "slow:<seconds>" (checkpoints while sleeping),
    an exception instance/class to raise, or a callable.
    """

    script: dict[str, list[Action]] = field(default_factory=dict)
    default: Action = "ok"
    calls: list[tuple[str, int]] = field(default_factory=list)
    retries: list[tuple[str, str]] = field(default_factory=list)
    finalized: list[tuple[str, JobStatus]] = field(default_factory=list)
    started: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def execute(self, ctx: JobContext) -> JobOutcome:
        name = ctx.job.name
        with self.lock:
            self.calls.append((name, ctx.attempt))
            n = sum(1 for c in self.calls if c[0] == name)
        actions = self.script.get(name, [])
        action = actions[n - 1] if n - 1 < len(actions) else self.default
        ctx.stage(Stage.VALIDATING)
        self.started.set()
        if callable(action) and not isinstance(action, type):
            return action(ctx)
        if isinstance(action, BaseException):
            raise action
        if isinstance(action, type) and issubclass(action, BaseException):
            raise action("boom") if not issubclass(action, VideoGenError) else action("boom")
        if action == "ok":
            ctx.checkpoint()
            return JobOutcome(JobStatus.SUCCESS, f"/out/{name}.mp4")
        if action == "partial":
            return JobOutcome(JobStatus.PARTIAL, f"/out/{name} [PARTIAL].mp4", skipped_images=2)
        if action == "block":
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                ctx.checkpoint()
                time.sleep(0.02)
            raise AssertionError("block action was never cancelled")
        if isinstance(action, str) and action.startswith("slow:"):
            end = time.monotonic() + float(action.split(":")[1])
            while time.monotonic() < end:
                ctx.checkpoint()
                time.sleep(0.02)
            return JobOutcome(JobStatus.SUCCESS, f"/out/{name}.mp4")
        raise AssertionError(f"unknown action {action!r}")

    def prepare_retry(self, ctx: JobContext, error: ErrorInfo) -> None:
        self.retries.append((ctx.job.name, error.error_class))

    def finalize(self, ctx: JobContext, final: JobState) -> None:
        self.finalized.append((ctx.job.name, final.status))


def wait_until(pred: Callable[[], bool], timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()
