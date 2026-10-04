"""Error taxonomy and retry budgets (ARCHITECTURE.md §11).

Every failure that can end a job is mapped to exactly one ``ErrorClass``.
The class alone decides how many retries are allowed, so retry behaviour is
uniform and finite.
"""

from __future__ import annotations

import traceback
from enum import Enum

from videogen.config.settings import RetryPolicy
from videogen.core.models import ErrorInfo


class ErrorClass(str, Enum):
    INPUT = "INPUT"
    TRANSIENT = "TRANSIENT"
    FFMPEG_CRASH = "FFMPEG_CRASH"
    TIMEOUT = "TIMEOUT"
    VERIFICATION = "VERIFICATION"
    RESOURCE = "RESOURCE"
    FFMPEG_UNAVAILABLE = "FFMPEG_UNAVAILABLE"
    ARCHIVE = "ARCHIVE"
    CANCELLED = "CANCELLED"
    INTERNAL = "INTERNAL"


class VideoGenError(Exception):
    """Base for all expected, classified failures."""

    error_class: ErrorClass = ErrorClass.INTERNAL
    default_code: str = "INTERNAL"

    def __init__(self, user_message: str, *, code: str | None = None, detail: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code or self.default_code
        self.detail = detail

    def to_info(self) -> ErrorInfo:
        return ErrorInfo(self.error_class.value, self.code, self.user_message, self.detail)


class InputError(VideoGenError):
    error_class = ErrorClass.INPUT
    default_code = "INVALID_INPUT"


class TransientError(VideoGenError):
    error_class = ErrorClass.TRANSIENT
    default_code = "TRANSIENT"


class FFmpegCrashError(VideoGenError):
    error_class = ErrorClass.FFMPEG_CRASH
    default_code = "FFMPEG_CRASH"


class OperationTimeoutError(VideoGenError):
    error_class = ErrorClass.TIMEOUT
    default_code = "TIMEOUT"


class VerificationError(VideoGenError):
    error_class = ErrorClass.VERIFICATION
    default_code = "VERIFICATION_FAILED"


class ResourceError(VideoGenError):
    error_class = ErrorClass.RESOURCE
    default_code = "RESOURCE"


class DiskSpaceError(ResourceError):
    default_code = "DISK_SPACE"


class FFmpegUnavailableError(VideoGenError):
    error_class = ErrorClass.FFMPEG_UNAVAILABLE
    default_code = "FFMPEG_UNAVAILABLE"


class ArchiveError(VideoGenError):
    error_class = ErrorClass.ARCHIVE
    default_code = "ARCHIVE_FAILED"


class StateLockedError(VideoGenError):
    """The state database is held by another program (not corrupt!)."""
    error_class = ErrorClass.INTERNAL
    default_code = "STATE_LOCKED"


class AlreadyRunningError(VideoGenError):
    error_class = ErrorClass.INTERNAL
    default_code = "ALREADY_RUNNING"


class JobCancelledError(VideoGenError):
    error_class = ErrorClass.CANCELLED
    default_code = "CANCELLED"

    def __init__(self, user_message: str = "Скасовано користувачем.", **kw: str) -> None:
        super().__init__(user_message, **kw)


def retry_budget(error_class: ErrorClass, policy: RetryPolicy) -> int:
    """Number of *additional* attempts permitted after the first failure."""
    return {
        ErrorClass.TRANSIENT: policy.transient,
        ErrorClass.FFMPEG_CRASH: policy.ffmpeg_crash,
        ErrorClass.TIMEOUT: policy.timeout,
        ErrorClass.VERIFICATION: policy.verification,
    }.get(error_class, 0)


def classify(exc: BaseException) -> ErrorInfo:
    """Map any exception to ErrorInfo. Unknown exceptions are INTERNAL."""
    if isinstance(exc, VideoGenError):
        info = exc.to_info()
        if not info.detail and exc.__cause__ is not None:
            info = ErrorInfo(info.error_class, info.code, info.message, _format_tb(exc.__cause__))
        return info
    if isinstance(exc, MemoryError):
        return ErrorInfo(ErrorClass.RESOURCE.value, "OUT_OF_MEMORY",
                         "Недостатньо оперативної пам'яті для виконання операції.", _format_tb(exc))
    if isinstance(exc, PermissionError):
        return ErrorInfo(ErrorClass.TRANSIENT.value, "FILE_LOCKED",
                         f"Файл заблоковано іншою програмою або немає доступу: {getattr(exc, 'filename', '') or ''}".rstrip(": "),
                         _format_tb(exc))
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:  # ENOSPC
        return ErrorInfo(ErrorClass.RESOURCE.value, "DISK_SPACE",
                         "Недостатньо вільного місця на диску.", _format_tb(exc))
    return ErrorInfo(ErrorClass.INTERNAL.value, "INTERNAL",
                     "Внутрішня помилка програми. Подробиці записано в журнал.", _format_tb(exc))


def _format_tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-8000:]
