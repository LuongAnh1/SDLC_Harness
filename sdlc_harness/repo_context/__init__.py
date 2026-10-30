"""Thu thập ngữ cảnh source của target repo cho Developer.

Package dùng CodeGraph khi có và source discovery theo profile để tạo context
giới hạn, có thể truy vết trước khi lập kế hoạch. Nó chỉ đọc repo, không sửa
workspace và không quyết định file nào Developer được phép ghi.
"""

from .codegraph import CODEGRAPH_SNAPSHOT_VERSION, CodeGraphContextProvider, RepoContext
from .discovery import discover_profile_source_files
from .landscape import REPO_LANDSCAPE_VERSION, build_repo_landscape, validate_repo_landscape

__all__ = [
    "CODEGRAPH_SNAPSHOT_VERSION",
    "CodeGraphContextProvider",
    "REPO_LANDSCAPE_VERSION",
    "RepoContext",
    "build_repo_landscape",
    "discover_profile_source_files",
    "validate_repo_landscape",
]
