"""Biên gọi LLM dùng chung cho các agent của Harness.

Package chuẩn hóa completion, structured output và fake response dùng trong
test, đồng thời cô lập giao thức Chat Completions khỏi agent. Nó không chọn
workflow, profile dự án hay tự diễn giải artifact nghiệp vụ.
"""

from .client import (
    BudgetedLlmClient,
    LlmClient,
    strip_code_fence,
)
from .fake import (
    fake_completion,
    fake_refined_requirement,
    fake_requirement_model_v2,
)

__all__ = [
    "BudgetedLlmClient",
    "LlmClient",
    "fake_completion",
    "fake_refined_requirement",
    "fake_requirement_model_v2",
    "strip_code_fence",
]
