"""Sinh và phân tích thiết kế kỹ thuật trong Phase 2.

Designer tạo design draft từ requirement đã duyệt; các parser PlantUML chuyển
class/sequence diagram thành model để Validator kiểm tra. Package không sở hữu
checkpoint approval hay việc triển khai source code.
"""
from .designer import DesignDraft, DesignerAgent
from .plantuml import (
    ClassDiagramParser,
    ParsedDesign,
    SequenceDiagramParser,
    extract_class_sequence_diagrams,
    parse_design_model,
)

__all__ = [
    "ClassDiagramParser",
    "DesignDraft",
    "DesignerAgent",
    "ParsedDesign",
    "SequenceDiagramParser",
    "extract_class_sequence_diagrams",
    "parse_design_model",
]
