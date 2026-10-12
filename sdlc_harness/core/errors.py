"""Lỗi có mã ổn định để CLI trả JSON cho Niko.

Không để exception Python thô tràn ra stdout vì Niko sẽ đọc stdout như một
contract máy-máy. Mỗi lỗi nghiệp vụ cần có `error_code` ngắn, ổn định.
"""

from __future__ import annotations


class HarnessError(Exception):
    """Lỗi nghiệp vụ/runtime có thể chuyển thẳng thành JSON response."""

    def __init__(self, error_code: str, message: str, *, run_id: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.run_id = run_id

    def to_response(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
        }
        if self.run_id:
            payload["run_id"] = self.run_id
        return payload
