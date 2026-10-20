"""Transport adapter đưa SDLC Harness ra HTTP API và CLI.

HTTP là contract tích hợp cho Niko/dashboard; CLI dành cho operator và debug.
Các adapter chỉ parse request, gọi application service và định dạng response,
không sở hữu workflow hay nghiệp vụ riêng.
"""
