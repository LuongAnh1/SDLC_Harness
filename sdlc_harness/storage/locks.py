"""Khóa phối hợp đa nền tảng, nhỏ gọn cho các action runtime."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

from ..core.errors import HarnessError


class RuntimeActionLock:
    """Non-blocking OS lock used to serialize mutations for one run."""

    def __init__(
        self,
        path: Path,
        *,
        run_id: str | None,
        error_code: str = "run_action_locked",
        locked_message: str | None = None,
    ):
        self.path = path
        self.run_id = run_id
        self.error_code = error_code
        self.locked_message = locked_message
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "RuntimeActionLock":
        handle: BinaryIO | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            self._acquire(handle)
            self._handle = handle
            return self
        except OSError as exc:
            if handle is not None:
                handle.close()
            raise HarnessError(
                self.error_code,
                self.locked_message or f"Another action is already mutating run {self.run_id}.",
                run_id=self.run_id,
            ) from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None

    @staticmethod
    def _acquire(handle: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
