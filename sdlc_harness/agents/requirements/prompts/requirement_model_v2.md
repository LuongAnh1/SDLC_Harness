# Requirement Model V2 Structured Output

Return exactly one JSON object. Do not use markdown fences or prose.

Dataset: `{{DATASET}}`
Use case: `{{USE_CASE}}`
Source ref: `{{SOURCE_REF}}`

## Evidence and status rules

- Use only facts supported by an exact quote from the source snapshot.
- Every `explicit` item must reference one or more evidence IDs.
- A broad source statement supports only the behavior it literally states. It does not support any unstated criterion, threshold, field, role, transition, outcome, or implementation choice.
- Use `assumption` only for a low-impact, reversible working default that does not change business semantics, persisted-data meaning, authorization, an external contract, or acceptance-test pass/fail. Keep it visibly unconfirmed and explain the reason.
- Never use an assumption to replace a missing validation criterion, matching or identity rule, limit, authorization rule, state-transition condition, or acceptance-test decision introduced by the source.
- Use `unknown` only for a material product decision that the source does not answer and link it to an open question.
- Never infer confirmed requirements from domain knowledge.
- Never create human decisions. Return `decisions` as an empty list and never use `human_decision` status.
- Keep normal success steps in `flows.main`, optional branches in `flows.alternatives`, and source-mentioned error/cancellation/interruption behavior in `flows.exceptions`.
- Use stable IDs with these prefixes: `UC-`, `GOAL-`, `ACT-`, `PRE-`, `MF-`, `AF-`, `EF-`, `POST-`, `BR-`, `AC-`, `QR-`, `ASM-`, `Q-`, `E-`, `SC-`.
- Each open question must include `category`, boolean `proposed_blocking`, `affects`, `depends_on`, `status="open"`, `answer_guidance`, and `evidence_refs`.
- `suggested_policy_id` is advisory only. Use `null` unless an exact reviewed policy below matches; never invent an ID or provide a policy answer yourself.
- `effective_blocking`, root metadata, provenance ledger, evidence `source_id`, decisions, and validation are controlled by Python. Evidence must cite an `observation_ref` returned by the source tool; Python will verify the quote, stamp `source_id`, and remove the temporary observation reference.

Reviewed safe-default policies:

{{SAFE_DEFAULT_POLICY_CATALOG}}

## Required source coverage pass

Before reducing or merging questions, account for every source clause that introduces an actor, input, action, branch, decision, constraint, state change, success result, or exception result.

- Map each source-supported clause to at least one explicit goal, actor, condition, flow, postcondition, business rule, acceptance criterion, or quality requirement.
- Do not silently omit a source-mentioned decision because its deciding criteria are missing. Preserve the supported high-level behavior as `explicit`, then represent the missing criterion through one linked open question and an unknown acceptance criterion when pass/fail depends on it.
- Copying or paraphrasing a vague source clause does not make it operationally complete.
- Perform question reduction only after source coverage and acceptance coverage are complete.

## Mandatory operational-decision check

Treat a source clause as decision-bearing when it requires the system to validate, verify, check, accept/reject, match, identify, prevent duplication, enforce uniqueness, apply a minimum/maximum/limit, determine eligibility, authorize an actor, or choose a state transition or externally observable outcome. This list describes semantics and is not limited to exact keywords.

For every decision-bearing clause:

1. Identify the exact criterion that determines the branch, pass/fail result, accepted data, identity match, authorization, threshold, or resulting state.
2. If the source states that criterion, model it as explicit evidence-backed content.
3. If the source does not state that criterion, preserve the high-level rule as explicit and create one consolidated open question for the missing product decision.
4. Create or link an `unknown` acceptance criterion when objective pass/fail cannot be written until the question is answered.

Use this counterfactual test: if two implementations can both satisfy the literal source text but disagree about which input is accepted, which records match, who is authorized, whether a limit is exceeded, which state follows, or whether an acceptance test passes, the source contains a material unresolved decision and the model must not return a false-clear result.

## Minimum necessary question and precision rules

- Ask for the missing product decision, not the implementation mechanism.
- Do not ask for exact UI copy, visual layout, widget/control choice, class structure, database layout, framework, internal algorithm, or vendor/service selection unless the source explicitly makes it contractual.
- Do not invent a timeout, failure mode, cancellation path, or integration scenario merely because software could fail. Ask about it only when the source mentions it or an explicit external contract requires its outcome.
- A source-supported result is already an observable outcome. For example, if the source says an object is deleted, do not ask what success message confirms deletion unless a notification is itself required.
- Prefer one question per owner decision. Merge overlapping questions and attach every affected requirement ID to the merged question. Merging must preserve every material decision; it must not remove a source-introduced gap.
- Do not ask separately for form fields, submitted data, and validation criteria when one input-contract question can resolve the same decision.
- Unknown actor identity is blocking only when different actors would change authorization, responsibility, available behavior, or acceptance testing. Do not require a human actor for a purely system-internal step.
- Omit details that the source never makes part of the product contract. Never omit the deciding criterion of a source-mentioned validation, matching, uniqueness, limit, authorization, or state-transition rule.

## Required acceptance coverage

- `acceptance_criteria` must not be empty when the source describes a success outcome, a material business rule, or a source-mentioned alternative/exception outcome.
- Create at least one evidence-backed acceptance criterion for the primary source-supported success outcome.
- Every material business rule must be referenced by at least one acceptance criterion through `verifies`.
- Cover each source-mentioned alternative or exception whose result changes externally observable behavior.
- An explicit acceptance criterion must contain an objective condition and observable expected outcome supported by source evidence. Merely saying “verify”, “check”, “process”, “handle”, or repeating an undefined term such as “valid” is not testable coverage.
- When the outcome is fully specified, create an `explicit` acceptance criterion directly and do not ask a question.
- When pass/fail depends on a missing decision criterion, create an `unknown` acceptance criterion with `text=""`, link its `open_question_refs`, and make its `verifies` references point to the affected rule or flow.

## Required scenario and surface contract

- Return `scenario_contract.schema_version="requirement_scenario_surface_v1"` and at least one scenario.
- Split source behavior into stable `SC-*` main, alternative, and exception scenarios. Every `MF-*`, `AF-*`, `EF-*`, `BR-*`, and `AC-*` must be covered by at least one scenario.
- Every scenario must reference at least one behavior item, one acceptance criterion, and source evidence.
- `scenario.requirement_refs` may contain only scenario behavior IDs (`MF-*`, `AF-*`,
  `EF-*`, or `BR-*`). `scenario.acceptance_refs` may contain only `AC-*` IDs. Never put
  an `AC-*` ID in `scenario.requirement_refs`. Together those two lists form the
  scenario-local allowlist; every surface `requirement_refs` entry must occur in one of them.
- A scenario `open_question_refs` list must contain every open question whose `affects`
  intersects the scenario-local allowlist, and must not contain unrelated questions.
- Every scenario must contain exactly four surfaces: `ui`, `api`, `business_rule`, and `data`.
- Each surface uses exactly one status: `required`, `not_applicable`, or `clarification_required`, plus a non-empty reason.
- `required` means the product behavior must address that surface; it does not mean new code must be created. Reference the scenario-local behavior or acceptance criteria that justify it.
- `not_applicable` requires a reason and empty requirement/question references.
- Use `clarification_required` only when the human answer can change whether that surface participates. It must reference affected behavior and an open question.
- A known required surface may still list open questions about its detailed behavior without changing its status to `clarification_required`.
- Map every scenario open question to at least one surface. Do not create file names, routes, classes, database tables, framework choices, or repository claims.
- Put a question in a surface `open_question_refs` only when that question's `affects`
  intersects that surface's `requirement_refs`. Otherwise map it to the related surface;
  never attach a question merely to satisfy the mapping requirement.

## Exact encoding for unknown content

- Every `unknown` item must reference at least one open question through `open_question_refs`.
- For an unknown goal, precondition, postcondition, business rule, acceptance criterion, or quality requirement, set `text` to the empty string `""`.
- For an unknown actor, set `name` to the empty string `""`.
- Do not put a guessed label or explanation in an unknown item's `text` or `name`. Put the missing decision only in the linked open question.
- Flow items do not have a requirement status. Keep only the source-supported `actor`, `action`, `trigger`, `steps`, `target`, and `outcome`; attach `open_question_refs` for missing material decisions. Do not fill missing flow detail from domain knowledge.
- When a supported item has one missing detail, keep the supported item `explicit` with evidence and link the question to it. Create a separate unknown item only when the missing decision needs its own requirement or acceptance-criterion slot.

## Blocking proposal

- Set `proposed_blocking=true` when the unanswered decision can change business acceptance/rejection, identity or matching, authorization, a required threshold, a state transition, an external contract, an observable branch outcome, or objective acceptance-test pass/fail.
- Do not mark wording, presentation, internal design, implementation preference, or optional contextual information as blocking.
- Use a category that names the product decision. Use `business_rule`, `acceptance_criteria`, `data_identity`, `api_contract`, or `observable_behavior` only when that category is genuinely the unresolved contract.

## Gap routing before clarification

- Do not turn a failed source tool call, unread source, missing quote, or ungrounded claim
  into a human question. Use `source.search`/`source.read` and repair the evidence first.
- Use `category="source_conflict"` only when at least two grounded evidence items state
  incompatible facts that require a human choice. Cite both evidence IDs; source conflict
  is always blocking.
- All other open questions represent a missing business/product decision. Every question
  must cite source evidence showing the supported clause whose deciding detail is absent.
- Never ask a human to repair a snapshot, catalog, fingerprint, tool argument, JSON shape,
  or validation error.

## Required JSON shape

```json
{
  "schema_version": "requirement_model_v2",
  "revision": 1,
  "run_id": "python_owned",
  "source": {"source_id": "SRC-1", "source_ref": "{{SOURCE_REF}}", "artifact_ref": "python_owned", "fingerprint": "python_owned"},
  "provenance_ledger": {
    "schema_version": "requirement_provenance_v1",
    "sources": [{"id": "SRC-1", "type": "python_owned", "source_ref": "{{SOURCE_REF}}", "locator": "python_owned", "revision": null, "artifact_ref": "python_owned", "fingerprint": "python_owned"}]
  },
  "use_case": {
    "id": "UC-1",
    "name": "...",
    "goal": {"id": "GOAL-1", "text": "...", "status": "explicit|assumption|unknown", "evidence_refs": ["E-1"], "open_question_refs": []},
    "actors": [{"id": "ACT-1", "name": "...", "status": "explicit|assumption|unknown", "evidence_refs": ["E-2"], "open_question_refs": []}]
  },
  "scenario_contract": {
    "schema_version": "requirement_scenario_surface_v1",
    "scenarios": [{
      "id": "SC-1",
      "name": "...",
      "kind": "main|alternative|exception",
      "actor_refs": ["ACT-1"],
      "requirement_refs": ["MF-1"],
      "acceptance_refs": ["AC-1"],
      "evidence_refs": ["E-1"],
      "open_question_refs": [],
      "surfaces": {
        "ui": {"status": "required|not_applicable|clarification_required", "reason": "...", "requirement_refs": ["MF-1"], "evidence_refs": ["E-1"], "open_question_refs": []},
        "api": {"status": "required|not_applicable|clarification_required", "reason": "...", "requirement_refs": [], "evidence_refs": [], "open_question_refs": []},
        "business_rule": {"status": "required|not_applicable|clarification_required", "reason": "...", "requirement_refs": [], "evidence_refs": [], "open_question_refs": []},
        "data": {"status": "required|not_applicable|clarification_required", "reason": "...", "requirement_refs": [], "evidence_refs": [], "open_question_refs": []}
      }
    }]
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
  "open_questions": [{"id": "Q-1", "question": "...", "category": "...", "proposed_blocking": false, "effective_blocking": false, "affects": [], "depends_on": [], "status": "open", "answer_guidance": null, "suggested_policy_id": null, "evidence_refs": []}],
  "decisions": [],
  "evidence": [{"id": "E-1", "observation_ref": "OBS-1", "location": "...", "quote": "exact quote from that observation"}],
  "validation": {"status": "invalid", "errors": []}
}
```

## Source snapshot

{{SOURCE_TEXT}}
