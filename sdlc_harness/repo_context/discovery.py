"""Khám phá source/test trong target workspace theo immutable run profile."""

from __future__ import annotations

from pathlib import Path

from ..core.errors import HarnessError
from ..profiles import ProfileSnapshotV1


def discover_profile_source_files(
    *,
    target_path: Path,
    snapshot: ProfileSnapshotV1,
    run_id: str,
) -> list[str]:
    """Trả source/test path đã duyệt theo component roots và language suffixes."""

    if not snapshot.is_workspace_bound or not snapshot.resolved_component_roots:
        raise HarnessError(
            "profile_snapshot_mismatch",
            "Phase 3 source discovery requires a workspace-bound profile snapshot.",
            run_id=run_id,
        )
    _, language = snapshot.resolve_profiles(run_id=run_id)
    suffixes = tuple(dict.fromkeys((*language.source_suffixes, *language.test_suffixes)))
    workspace_root = target_path.resolve()
    discovered: set[str] = set()

    try:
        for _, editable_roots in snapshot.resolved_component_roots:
            for editable_root in editable_roots:
                root = (workspace_root / editable_root).resolve()
                if not _is_within(root, workspace_root) or not root.is_dir():
                    raise HarnessError(
                        "target_source_path_invalid",
                        f"Profile source root is missing or outside the run workspace: {editable_root}",
                        run_id=run_id,
                    )
                for candidate in root.rglob("*"):
                    if not candidate.is_file() or not candidate.name.endswith(suffixes):
                        continue
                    resolved = candidate.resolve()
                    if not _is_within(resolved, root) or not _is_within(resolved, workspace_root):
                        raise HarnessError(
                            "target_source_path_invalid",
                            f"Profile source path escapes its approved root: {candidate}",
                            run_id=run_id,
                        )
                    discovered.add(resolved.relative_to(workspace_root).as_posix())
    except HarnessError:
        raise
    except OSError as exc:
        raise HarnessError(
            "target_source_discovery_failed",
            f"Could not discover source files in the run workspace: {exc}",
            run_id=run_id,
        ) from exc

    return sorted(discovered)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
