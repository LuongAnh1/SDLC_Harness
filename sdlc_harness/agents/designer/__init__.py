"""Sinh và phân tích thiết kế kỹ thuật trong Phase 2.

Designer tạo design draft từ requirement đã duyệt; các parser PlantUML chuyển
class/sequence diagram thành model để Validator kiểm tra. Package không sở hữu
checkpoint approval hay việc triển khai source code.
"""
from .designer import DesignDraft, DesignerAgent
from .execution import DesignerExecutionError, DesignerExecutionResult, DesignerToolLoopExecutor
from .graph import (
    DELTA_VERSION,
    DESIGN_BUNDLE_STATE_KEY,
    DESIGN_BUNDLE_VERSION,
    DESIGNER_CONTRACT_STATE_KEY,
    GRAPH_VERSION,
    SURFACE_CONTRACT_VERSION,
    apply_designer_delta,
    completion_findings,
    design_bundle_findings,
    new_vertical_design_graph,
    validate_vertical_design_graph,
)
from .plantuml import (
    ClassDiagramParser,
    ParsedDesign,
    SequenceDiagramParser,
    extract_class_sequence_diagrams,
    parse_design_model,
)
from .projections import build_deterministic_trace, projection_manifest, render_design_projections

__all__ = [
    "ClassDiagramParser",
    "DesignDraft",
    "DesignerAgent",
    "DesignerExecutionError",
    "DesignerExecutionResult",
    "DesignerToolLoopExecutor",
    "DELTA_VERSION",
    "DESIGN_BUNDLE_STATE_KEY",
    "DESIGN_BUNDLE_VERSION",
    "DESIGNER_CONTRACT_STATE_KEY",
    "GRAPH_VERSION",
    "SURFACE_CONTRACT_VERSION",
    "ParsedDesign",
    "SequenceDiagramParser",
    "apply_designer_delta",
    "build_deterministic_trace",
    "completion_findings",
    "design_bundle_findings",
    "extract_class_sequence_diagrams",
    "new_vertical_design_graph",
    "parse_design_model",
    "projection_manifest",
    "render_design_projections",
    "validate_vertical_design_graph",
]
