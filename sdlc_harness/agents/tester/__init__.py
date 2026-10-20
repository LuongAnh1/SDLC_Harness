"""Vai trò Tester/QA sinh test và đánh giá hành vi cần nghiệm thu.

Package dùng requirement, design và code context để tạo test tham chiếu hoặc
phản hồi sửa lỗi. Nó không chạy bộ command build/test của target repo; phần đó
thuộc ``verification``.
"""

from .qa import QaGeneratedTestSuite, QaTestResult, QaTesterAgent, QaTesterLlmError

__all__ = [
    "QaGeneratedTestSuite",
    "QaTestResult",
    "QaTesterAgent",
    "QaTesterLlmError",
]
