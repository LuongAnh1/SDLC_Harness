"""Sinh và phân tích thiết kế kỹ thuật trong Phase 2.

Designer tạo design draft từ requirement đã duyệt; các parser PlantUML chuyển
class/sequence diagram thành model để Validator kiểm tra. Package không sở hữu
checkpoint approval hay việc triển khai source code.
"""
from .execution import DesignerExecutionError, DesignerExecutionResult, DesignerToolLoopExecutor
from .graph import (
    UI_LAYERED_LAYOUT_V1,
    BOUNDARY_CONTRACT_VERSION,
    DELTA_VERSION,
    DESIGN_BUNDLE_STATE_KEY,
    DESIGN_BUNDLE_VERSION,
    DESIGNER_CONTRACT_STATE_KEY,
    GRAPH_VERSION,
    RUNTIME_BINDING_VERSION,
    SURFACE_CONTRACT_VERSION,
    UI_LAYOUT_CONTRACT_KEY,
    apply_designer_delta,
    canonicalize_composition_wiring,
    completion_findings,
    design_bundle_findings,
    new_vertical_design_graph,
    validate_vertical_design_graph,
)
from .plantuml import (
    extract_class_sequence_diagrams,
)
from .projections import (
    build_deterministic_trace,
    projection_manifest,
    render_design_projections,
    render_ui_feature_experience,
)
from .uiux import (
    UIUX_CONTRACT_STATE_KEY,
    UIUX_CONTRACT_VERSION,
    UIUX_MODE_STATE_KEY,
    UI_CONTRACT_STATE_KEY,
    UI_EXPERIENCE_CONTRACT_V2,
    UiUxGuidanceProvider,
)

__all__ = [
    "DesignerExecutionError",
    "DesignerExecutionResult",
    "DesignerToolLoopExecutor",
    "BOUNDARY_CONTRACT_VERSION",
    "DELTA_VERSION",
    "DESIGN_BUNDLE_STATE_KEY",
    "DESIGN_BUNDLE_VERSION",
    "DESIGNER_CONTRACT_STATE_KEY",
    "GRAPH_VERSION",
    "RUNTIME_BINDING_VERSION",
    "SURFACE_CONTRACT_VERSION",
    "UIUX_CONTRACT_STATE_KEY",
    "UIUX_CONTRACT_VERSION",
    "UIUX_MODE_STATE_KEY",
    "UI_CONTRACT_STATE_KEY",
    "UI_EXPERIENCE_CONTRACT_V2",
    "UI_LAYOUT_CONTRACT_KEY",
    "UI_LAYERED_LAYOUT_V1",
    "UiUxGuidanceProvider",
    "apply_designer_delta",
    "canonicalize_composition_wiring",
    "build_deterministic_trace",
    "completion_findings",
    "design_bundle_findings",
    "extract_class_sequence_diagrams",
    "new_vertical_design_graph",
    "projection_manifest",
    "render_design_projections",
    "render_ui_feature_experience",
    "validate_vertical_design_graph",
]
