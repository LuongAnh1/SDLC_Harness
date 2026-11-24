# Requirement Model V2 Clarification Update

Return exactly one JSON object with this shape:

```json
{
  "resolution": "accepted|insufficient",
  "follow_up_text": null,
  "updates": [
    {"requirement_id": "BR-1", "fields": {"text": "..."}}
  ],
  "surface_updates": [
    {"scenario_id": "SC-1", "surface": "ui", "status": "required", "reason": "..."}
  ],
  "related_updates": [
    {"requirement_id": "AC-2", "fields": {"text": "..."}, "reason": "..."}
  ]
}
```

Rules:

- Judge whether the human answer is sufficient to resolve the current question.
- If insufficient, return a concise follow-up question and empty `updates`, `surface_updates`, and `related_updates` lists.
- If accepted, return null follow_up_text and exactly one update entry for every ID in `affects`.
- For an accepted answer, also check every other requirement in the canonical model against the answer. When the answer makes another requirement false or contradictory (for example a criterion that restates a source clause the answer now narrows), return it once in `related_updates` with the corrected fields and a `reason` that names the conflict. Never put an ID from `affects` there, and never use it for wording-only improvements; return an empty list when nothing else conflicts.
- For an accepted answer, return exactly one `surface_updates` entry for every surface whose current status is `clarification_required` and whose `open_question_refs` contains the current question.
- You may also update a `required` surface linked to the current question when the answer makes its reason more precise. Never update a surface that is not linked to the current question.
- Return an empty list when no linked surface needs an update.
- A surface update may set `required`, `not_applicable`, or keep `clarification_required` only when another open question still controls it. Explain the answer-backed result in `reason`.
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
