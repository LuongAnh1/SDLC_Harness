"""Entrypoint tương thích cho CLI công khai của SDLC Harness.

Implementation thật nằm ở `sdlc_harness.interfaces.cli`, nhưng wrapper này giữ
ổn định lệnh `python -m sdlc_harness.cli` cho operator và automation thủ công.
"""

from __future__ import annotations

from .interfaces.cli import build_parser, main, run

__all__ = ["build_parser", "main", "run"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
