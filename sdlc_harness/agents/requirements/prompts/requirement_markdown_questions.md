# Write the open questions file

Your requirements at `{{DRAFT_REF}}` have passed the independent review. The owner answers their open questions from
a JSON file, so write every `Q-n` of the document as one entry of that file, with the same ID. Keep each question's
decision as the document states it; do not add or drop a question. Do not edit the document.

Keep the text short so the owner can answer at a glance:

- `question`: one sentence ending with `?`.
- `context`: at most one sentence: the use-case clause or the repository fact behind the question.
- Each option `answer`: one short sentence. `implication`: at most one short sentence, or null.
- When a question bundles several independent decisions, use `"answer_mode": "short"`, set `options` to `[]`, and
  state in `answer_guidance` which parts the answer must cover, instead of listing their combinations as options.

Rules:

- `source` is `use_case` or `repository_survey`, as the document says.
- `affects` holds only `UC-`, `AC-` and `IMP-` IDs that the document defines.
- `multiple_choice`: 2 to 5 options with unique keys, and `recommended_option` is one of the keys or null.
  `short`: `options` is `[]`, `recommended_option` is null, and `suggested_answer` holds your proposed answer.
- `proposed_disposition` is `human_required`, `agent_default` or `deferred_to_design`, as the document says. An
  `agent_default` question sets `suggested_answer` (or `recommended_option`), `why_it_is_safe` and
  `"reversible": true`. `proposed_blocking` is true for `human_required`.

Shape:

```json
{"questions": [{"id": "Q-1", "question": "...?", "source": "repository_survey", "context": "...", "category": "api_contract", "proposed_disposition": "human_required", "proposed_blocking": true, "affects": ["AC-3", "UC-5"], "depends_on": [], "answer_guidance": null, "answer_mode": "multiple_choice", "options": [{"key": "a", "answer": "...", "implication": "..."}, {"key": "b", "answer": "...", "implication": null}], "recommended_option": "a", "suggested_answer": null, "why_it_is_safe": null, "impact": "high", "uncertainty": "medium", "reversible": false}]}
```
