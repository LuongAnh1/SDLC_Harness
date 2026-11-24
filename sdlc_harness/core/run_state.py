"""Run-state layout: its schema version, the migrations between versions, and the Phase 4 lane pins.

`RuntimeStore.read_state` migrates every state it reads to `STATE_SCHEMA_VERSION`; `write_state` stamps it. A layout
change bumps the version and registers one migration from the previous version, so old runs keep reading correctly.
"""

from __future__ import annotations

from collections.abc import Callable

from .coerce import dict_value
from .errors import HarnessError

STATE_SCHEMA_VERSION_KEY = "state_schema_version"
# A state written before versioning (no key) uses layout 1.
STATE_SCHEMA_VERSION = 1
# from-version -> function returning the state in from-version + 1 layout
MIGRATIONS: dict[int, Callable[[dict[str, object]], dict[str, object]]] = {}


def migrate_state(state: dict[str, object], *, run_id: str | None = None) -> dict[str, object]:
    """Bring a stored state to the current layout in memory (the next write persists it)."""

    version = int(state.get(STATE_SCHEMA_VERSION_KEY) or 1)
    if version > STATE_SCHEMA_VERSION:
        raise HarnessError(
            "state_schema_unsupported",
            f"Run state uses layout v{version}; this Harness reads up to v{STATE_SCHEMA_VERSION}.",
            run_id=run_id,
        )
    while version < STATE_SCHEMA_VERSION:
        state = MIGRATIONS[version](state)
        version += 1
    state[STATE_SCHEMA_VERSION_KEY] = version
    return state


# Phase 4 lane pins: each lane keeps its pinned mode, policies and progress under one state key.
RUNTIME_CLOSURE = "runtime_closure"
BROWSER_STAGES = "browser_stages"
DOCUMENTATION_CLOSURE = "documentation_closure"
FINAL_VERIFICATION = "final_verification"
PUBLICATION = "publication"
# Lanes whose pin records a mode; every lane always runs (2026-11-18).
LANE_PINS = (RUNTIME_CLOSURE, BROWSER_STAGES, DOCUMENTATION_CLOSURE)


def runtime_closure(state: dict[str, object]) -> dict[str, object]:
    return dict_value(state.get(RUNTIME_CLOSURE))


def browser_stages(state: dict[str, object]) -> dict[str, object]:
    return dict_value(state.get(BROWSER_STAGES))


def documentation_closure(state: dict[str, object]) -> dict[str, object]:
    return dict_value(state.get(DOCUMENTATION_CLOSURE))


def final_verification(state: dict[str, object]) -> dict[str, object]:
    return dict_value(state.get(FINAL_VERIFICATION))


def publication(state: dict[str, object]) -> dict[str, object]:
    return dict_value(state.get(PUBLICATION))
