"""Nạp và chuẩn hóa nguồn đầu vào thành use-case source của Harness.

Adapter hiện hỗ trợ fixture/dataset và Jira, đồng thời tạo ``source_ref`` ổn
định cho idempotency. Package không tinh chỉnh requirement và không đồng bộ kết
quả ngược về công cụ cộng tác.
"""

from .fixtures import UseCaseSource, load_use_case, source_ref_for
from .jira import jira_source_ref_for, load_jira_issue_source

__all__ = ["UseCaseSource", "load_use_case", "source_ref_for", "jira_source_ref_for", "load_jira_issue_source"]
