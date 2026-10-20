"""Tài nguyên giao diện quan sát và điều khiển dành cho operator.

Package phục vụ asset dashboard; dữ liệu và action vẫn đi qua application/HTTP
contract hiện có. Ops không được trở thành một đường nghiệp vụ riêng hoặc đọc
ghi trực tiếp state để bỏ qua các guard của Harness.
"""

from .frontend import read_dashboard_asset

__all__ = ["read_dashboard_asset"]
