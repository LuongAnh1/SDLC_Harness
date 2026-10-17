# Requirement Model V2 Clarification Update

Return exactly one JSON object with this shape:

```json
{
  "resolution": "accepted|insufficient",
  "follow_up_text": null,
  "updates": [
    {"requirement_id": "BR-1", "fields": {"text": "..."}}
  ]
}
```

Rules:

- Judge whether the human answer is sufficient to resolve the current question.
- If insufficient, return a concise follow-up question and an empty updates list.
- If accepted, return null follow_up_text and exactly one update entry for every ID in `affects`.
- Change only business fields needed to apply the answer. Never include IDs, status, decision, revision, evidence, question status, or validation fields in `fields`.
- Use only the following editable fields and exact JSON value types:
  - `GOAL-*`, `PRE-*`, `POST-*`, `BR-*`, `QR-*`, `ASM-*`: `text` is a string.
  - `ACT-*`: `name` is a string.
  - `MF-*`: `actor` and `action` are strings; `target` is a string or null.
  - `AF-*`, `EF-*`: `trigger` is a string; `steps` is an array of strings; `outcome` is a string or null.
  - `AC-*`: `text` is a string; `verifies` is an array of requirement-ID strings.
- Never put objects inside `steps` or `verifies`. For example, use `"steps": ["Discard the unsaved values.", "Return to the list."]`.
- Preserve the meaning of source-backed content. Do not infer unrelated facts from domain knowledge.
- Do not add, delete, reorder, or rename requirements.

## Current question

{{QUESTION}}

## Human answer

{{ANSWER}}

## Canonical model

{{CURRENT_MODEL}}

## Original source snapshot

{{SOURCE_TEXT}}
