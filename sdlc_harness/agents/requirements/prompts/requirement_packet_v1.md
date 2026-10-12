Convert the following use case into Markdown with exactly these sections:
# Requirement Packet V1
## Goal
## Actors
## Preconditions
## Main Flow
## Alternative / Exception Flows
## Postconditions
## Acceptance Criteria
## Quality / Non-functional Requirements
## Open Questions
## Source Evidence

Rules:
- Keep the output grounded in the source.
- Prefer the original source as the evidence of truth. Use the formal use case only as a normalization aid.
- Do not invent facts outside the original source.
- Use numbered steps in Main Flow.
- Main Flow must contain only the successful happy path.
- Put cancel, interruption, connection failure, error, and other non-happy-path behavior under Alternative / Exception Flows.
- Put successful final state under Postconditions.
- Put concrete reviewable checks under Acceptance Criteria.
- Put duplicate submission blocking, latency, reliability, authorization quality, or other non-functional constraints under Quality / Non-functional Requirements.
- Source Evidence must cite short phrases from the original source only, not the generated formal use case and not external knowledge.
- If a section has no evidence, write "- None from source." and add an Open Question when the missing information blocks review.
- Write in Vietnamese.

Dataset: {dataset}
Use case: {use_case}

Original source:
{source_text}

Formal use case:
{formal_use_case}
