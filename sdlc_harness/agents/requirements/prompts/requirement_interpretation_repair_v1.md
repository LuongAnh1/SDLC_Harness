# Requirement Model V2 Interpretation Repair

A reviewer compared the Requirement Model with its source. Each finding below names
requirements that settle a point the source leaves open in a way that contradicts the
source or another requirement, and says what must change. Correct those requirements.

Return exactly one JSON object with this shape:

```json
{
  "updates": [
    {"requirement_id": "BR-1", "fields": {"text": "..."}, "reason": "..."}
  ]
}
```

Rules:

- Update only requirement IDs named by a finding, each at most once, and address every
  finding with at least one update.
- Make each updated requirement agree with its quoted source evidence and with the other
  requirements, following the finding's `fix`. Keep everything the source states.
- Do not add facts from domain knowledge, and do not change requirements that no finding names.
- Change only business fields. Never include IDs, status, evidence, decisions, revision, or
  validation fields in `fields`. Use only these editable fields and exact JSON value types:
  - `GOAL-*`, `PRE-*`, `POST-*`, `BR-*`, `QR-*`, `ASM-*`: `text` is a string.
  - `ACT-*`: `name` is a string.
  - `MF-*`: `actor` and `action` are strings; `target` is a string or null.
  - `AF-*`, `EF-*`: `trigger` is a string; `steps` is an array of strings; `outcome` is a string or null.
  - `AC-*`: `text` is a string; `verifies` is an array of requirement-ID strings.
- An `unknown` item keeps empty `text`; do not fill it here.
- `reason` names the finding the update resolves.

## Findings

{{FINDINGS}}

## Canonical model

{{CURRENT_MODEL}}

## Original source snapshot

{{SOURCE_TEXT}}
