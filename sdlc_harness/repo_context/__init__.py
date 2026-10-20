"""Thu thập ngữ cảnh source của target repo cho Developer.

Package dùng CodeGraph khi có và source discovery theo profile để tạo context
giới hạn, có thể truy vết trước khi lập kế hoạch. Nó chỉ đọc repo, không sửa
workspace và không quyết định file nào Developer được phép ghi.
"""

from .codegraph import CodeGraphContextProvider, RepoContext
from .discovery import discover_profile_source_files

__all__ = ["CodeGraphContextProvider", "RepoContext", "discover_profile_source_files"]
