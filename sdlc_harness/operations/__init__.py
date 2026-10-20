"""Quản lý operation bền vững cho các HTTP action có side effect.

Operation store lưu trạng thái, idempotency key và kết quả để client có thể
poll sau timeout mà không chạy workflow lần hai. Package điều phối vòng đời job,
không thay thế workflow state của từng run.
"""

from .manager import OperationManager, canonical_json_hash, sha256_text
from .store import OperationStore

__all__ = ["OperationManager", "OperationStore", "canonical_json_hash", "sha256_text"]
