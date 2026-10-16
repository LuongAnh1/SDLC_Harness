# Requirement Model V2 Structured Output

Return exactly one JSON object. Do not use markdown fences or prose.

Dataset: `{{DATASET}}`
Use case: `{{USE_CASE}}`
Source ref: `{{SOURCE_REF}}`

## Rules

- Use only facts supported by an exact quote from the source snapshot.
- Every `explicit` item must reference one or more evidence IDs.
- Put missing or ambiguous behavior in `open_questions`; represent affected content as `unknown` with an empty text value and `open_question_refs`.
- Never infer confirmed requirements from domain knowledge.
- Never create human decisions. Return `decisions` as an empty list and never use `human_decision` status.
- Keep normal success steps in `flows.main`, optional branches in `flows.alternatives`, and error/cancellation/interruption behavior in `flows.exceptions`.
- Use stable IDs with these prefixes: `UC-`, `GOAL-`, `ACT-`, `PRE-`, `MF-`, `AF-`, `EF-`, `POST-`, `BR-`, `AC-`, `QR-`, `ASM-`, `Q-`, `E-`.
- Each open question must include `category`, boolean `proposed_blocking`, `affects`, `depends_on`, `status="open"`, `answer_guidance`, and `evidence_refs`.
- `effective_blocking`, root metadata, decisions, and validation are controlled by Python. Include placeholders for them; Python will overwrite their values.

## Required JSON shape

```json
{
  "schema_version": "requirement_model_v2",
  "revision": 1,
  "run_id": "python_owned",
  "source": {"source_ref": "{{SOURCE_REF}}", "artifact_ref": "python_owned", "fingerprint": "python_owned"},
  "use_case": {
    "id": "UC-1",
    "name": "...",
    "goal": {"id": "GOAL-1", "text": "...", "status": "explicit|assumption|unknown", "evidence_refs": ["E-1"], "open_question_refs": []},
    "actors": [{"id": "ACT-1", "name": "...", "status": "explicit|assumption|unknown", "evidence_refs": ["E-2"], "open_question_refs": []}]
  },
  "preconditions": [{"id": "PRE-1", "text": "...", "status": "explicit|assumption|unknown", "evidence_refs": [], "open_question_refs": []}],
  "flows": {
    "main": [{"id": "MF-1", "actor": "ACT-1|System", "action": "...", "target": null, "evidence_refs": [], "open_question_refs": []}],
    "alternatives": [{"id": "AF-1", "trigger": "...", "steps": [], "outcome": null, "evidence_refs": [], "open_question_refs": []}],
    "exceptions": [{"id": "EF-1", "trigger": "...", "steps": [], "outcome": null, "evidence_refs": [], "open_question_refs": []}]
  },
  "postconditions": [],
  "business_rules": [],
  "acceptance_criteria": [{"id": "AC-1", "text": "", "status": "unknown", "evidence_refs": [], "open_question_refs": ["Q-1"], "verifies": ["BR-1"]}],
  "quality_requirements": [],
  "assumptions": [{"id": "ASM-1", "text": "...", "status": "assumption", "evidence_refs": [], "open_question_refs": [], "reason": "...", "affects": []}],
  "open_questions": [{"id": "Q-1", "question": "...", "category": "...", "proposed_blocking": false, "effective_blocking": false, "affects": [], "depends_on": [], "status": "open", "answer_guidance": null, "evidence_refs": []}],
  "decisions": [],
  "evidence": [{"id": "E-1", "source_ref": "{{SOURCE_REF}}", "location": "...", "quote": "exact source quote"}],
  "validation": {"status": "invalid", "errors": []}
}
```

## Source snapshot

{{SOURCE_TEXT}}
