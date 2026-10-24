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

- Never provide or mutate `id`, root schema/run/revision, source/provenance, decisions,
  validation, `decision_ref`, `source_id`, or `effective_blocking`.
- For every added entity, use a unique `temp_ref=tmp:<name>` and omit its ID. Reference
  that new entity with the exact temp ref; Python assigns and rewrites the stable ID.
- Added open questions omit `status` and `effective_blocking`; Python sets them.
- Existing entities are targeted only by their current stable ID. Do not update and
  remove the same target, and do not mutate one target or surface twice.
- New evidence, or an evidence quote change, must include an exact quote and an
  `observation_ref=OBS-*` already returned by a source tool.
- Use `entity.add` values with the same non-Python-owned fields as the corresponding
  entity in the base candidate. A new scenario includes all four surfaces.
- After the delta, every reference, scenario coverage rule, surface invariant,
  grounding rule and clarification rule must remain valid.
