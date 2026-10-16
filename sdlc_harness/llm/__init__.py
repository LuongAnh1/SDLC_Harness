"""LLM provider adapters."""

from .client import (
    LlmClient,
    fake_completion,
    fake_refined_requirement,
    fake_requirement_model_v2,
    strip_code_fence,
)

__all__ = [
    "LlmClient",
    "fake_completion",
    "fake_refined_requirement",
    "fake_requirement_model_v2",
    "strip_code_fence",
]
