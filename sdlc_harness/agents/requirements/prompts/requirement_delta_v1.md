# Requirement Repair Delta V1

Return one `requirement.propose_delta` action that fixes only the validator findings in
`repair_context`. Preserve every valid entity and reference from the base candidate.

## Root contract

`arguments.requirement_delta` contains exactly:

```json
{
  "schema_version": "requirement_delta_v1",
  "base_candidate_sha256": "copy the exact repair_context value",
  "operations": []
}
```

Use at most 64 operations. Do not call `complete` while repair context exists.

## Operations

- Update: `{"op":"entity.update","target_id":"SC-1","fields":{...}}`
- Add: `{"op":"entity.add","collection":"acceptance_criteria","temp_ref":"tmp:new_ac","value":{...}}`
- Remove: `{"op":"entity.remove","target_id":"AC-1"}`
- Surface: `{"op":"surface.update","scenario_id":"SC-1","surface":"ui","fields":{...}}`

Add collections are `use_case.actors`, `preconditions`, `flows.main`,
`flows.alternatives`, `flows.exceptions`, `postconditions`, `business_rules`,
`acceptance_criteria`, `quality_requirements`, `assumptions`, `open_questions`,
`evidence`, and `scenario_contract.scenarios`.

## Authority rules

Preserve `proposed_disposition` metadata. Do not create policy IDs. Agent defaults
must remain low-impact, low-uncertainty, reversible and carry an explicit proposed
answer plus `why_it_is_safe`; otherwise use `human_required`.

`repair_context.repair_targets` is the authoritative issue-by-issue repair scope.
When a repair needs to link a question to a requirement or flow, reuse only a question
ID listed in that target's `eligible_existing_question_ids`. If that list is empty and
the base candidate's evidence cannot supply the missing fields, add a new question for
that target instead of reusing an unrelated question. This allowlist does not prevent
updating a question that is itself the validator target.

For `answer_mode="short"`, use `options=[]` and `recommended_option=null`. For
`answer_mode="multiple_choice"`, use 2-5 objects shaped as
`{"key":"stable_key","answer":"User-facing answer","implication":"Optional impact"}`;
keys are unique and `recommended_option` is null or one of those keys. Never use bare
strings as question options.

- Never provide or mutate `id`, root schema/run/revision, source/provenance, decisions,
  validation, `decision_ref`, `source_id`, or `effective_blocking`.
- For every added entity, use a unique `temp_ref=tmp:<name>` and omit its ID. Reference
  that new entity with the exact temp ref; Python assigns and rewrites the stable ID.
- Added open questions omit `status` and `effective_blocking`; Python sets them.
- Every added open question must be bound in the same delta with its exact `temp_ref`:
  - add the temp ref to `open_question_refs` on every affected requirement or flow entity;
  - add the temp ref to `scenario.open_question_refs` for every scenario whose local
    `requirement_refs` or `acceptance_refs` intersects the question's `affects` list;
  - add the temp ref to `surface.open_question_refs` for every related surface whose
    `requirement_refs` intersects the question's `affects` list;
  - use `entity.update` for the affected entity and scenario, and `surface.update` for
    each related surface. Preserve existing question references instead of replacing them.
- The target requirement or flow ID must appear in the question's `affects` list.
- Do not attach a question to unrelated requirements, flows, scenarios or surfaces. A known participating surface
  remains `required` while carrying a question about behavior details. Change it to
  `clarification_required` only when the answer can change whether that surface participates.
- When a missing branch outcome makes an acceptance criterion objectively undecidable, include
  that acceptance ID in `affects`, update it to the exact unknown encoding, and bind the question
  through the same scenario and surface rules.
- Existing entities are targeted only by their current stable ID. Do not update and
  remove the same target, and do not mutate one target or surface twice.
- New evidence, or an evidence quote change, must include an exact quote and an
  `observation_ref=OBS-*` already returned by a source tool.
- Use `entity.add` values with the same non-Python-owned fields as the corresponding
  entity in the base candidate. A new scenario includes all four surfaces.
- After the delta, every reference, scenario coverage rule, surface invariant,
  grounding rule and clarification rule must remain valid.
