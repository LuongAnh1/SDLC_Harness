# Repair Requirement Packet v1

You previously produced a Requirement Packet for:

- Dataset: `{dataset}`
- Use case: `{use_case}`

The packet failed validation. Repair the packet only. Return the full corrected
Markdown packet and nothing else.

## Rules

- Preserve all factual content supported by the original source.
- Do not invent new behavior.
- Keep the same section schema:
  - `## Goal`
  - `## Actors`
  - `## Preconditions`
  - `## Main Flow`
  - `## Alternative / Exception Flows`
  - `## Postconditions`
  - `## Acceptance Criteria`
  - `## Quality / Non-functional Requirements`
  - `## Open Questions`
  - `## Source Evidence`
- `Main Flow` must contain the happy path only.
- Put cancel, interruption, connection failure, error, and other non-happy-path
  behavior under `Alternative / Exception Flows`.
- `Postconditions` must describe only the successful final state after the happy
  path completes.
- `Acceptance Criteria` must describe reviewable success checks only. Do not
  hide cancel/error/interruption handling there.
- Put duplicate submission, input control, reliability, security, performance,
  and other quality constraints under `Quality / Non-functional Requirements`.
- `Source Evidence` must cite the original source text only. Do not cite
  generated formal use case lines such as `UsecaseName:`.

## Requirement Validation Report

```json
{RequirementValidationReport}
```

## Original Source

```text
{source_text}
```

## Formal Use Case

This formal use case is a normalization aid, not the source of truth:

```text
{formal_use_case}
```

## Current Requirement Packet

```markdown
{RequirementPacket}
```
