"""SDLC Harness - execution workflow cho chuyển giao ngữ cảnh SDLC.

Package này là phần chạy workflow độc lập với Niko. Niko chỉ gọi CLI/API để
khởi tạo run, đọc trạng thái và xin duyệt checkpoint; còn việc load source,
chuẩn hóa requirement, lưu artifact và sync tool thuộc repo này.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
