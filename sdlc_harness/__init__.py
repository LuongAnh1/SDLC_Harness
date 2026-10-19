"""SDLC Harness - execution workflow cho chuyển giao ngữ cảnh SDLC.

Package này chạy workflow độc lập với Niko. Niko gọi HTTP API để khởi tạo run,
đọc trạng thái và gửi action; CLI được giữ riêng cho operator/debug. Việc load
source, chuẩn hóa requirement, lưu artifact và sync tool thuộc repo này.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
