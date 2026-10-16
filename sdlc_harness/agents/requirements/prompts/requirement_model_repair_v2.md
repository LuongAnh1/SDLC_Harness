# Requirement Model V2 Bounded Repair

Return exactly one complete JSON object. Do not use markdown fences or prose.

Repair only the schema, ID/reference, dependency, evidence-grounding, or exception-placement violations listed in the validation report. Preserve valid IDs, valid questions, links, source-clause coverage, acceptance coverage, and source-supported business content. Do not add confirmed content from domain knowledge or create human decisions. Do not add a new product question unless a listed violation cannot be repaired without one or an existing source-supported question must be restored after repairing its links.

## Unknown-content repair

When the report contains `unknown_contains_inferred_content`:

- Keep the item's ID, `status="unknown"`, and valid `open_question_refs`.
- For an unknown goal, precondition, postcondition, business rule, acceptance criterion, or quality requirement, set `text` to `""`.
- For an unknown actor, set `name` to `""`.
- Remove guessed content instead of rewriting it into another unsupported statement.
- Preserve the linked open question. If the item has no linked question and the report also contains `unknown_without_question`, create one minimum necessary question and link both sides.
- If part of the rejected item is explicitly stated in the source, represent that supported part as a separate `explicit` item with exact evidence. Do not keep it inside the unknown item.

When the report contains `unknown_without_question`, add the smallest question required to resolve that unknown item, add its ID to `open_question_refs`, and include the unknown item's ID in the question's `affects` list.

## Reference repair without semantic deletion

When the report contains `dangling_reference`:

1. Identify the existing valid requirement, question, decision, or evidence ID that represents the same source-supported clause or unresolved product decision.
2. Replace the dangling reference with that valid ID and repair both directions where the contract requires reciprocal links.
3. Preserve the question when it represents a source-mentioned unresolved validation criterion, matching or identity rule, uniqueness rule, limit, authorization decision, state transition, external contract, or acceptance-test decision.
4. If no suitable requirement target exists, create the smallest correctly typed `unknown` requirement or acceptance criterion with an empty content field and link it to the existing question.

Never delete a question solely to make a dangling reference disappear. Remove a question only when its subject is absent from the source, is purely an internal/UI design choice, and is not required for source or acceptance coverage.

## Acceptance and decision-gap preservation

- After repair, keep at least one evidence-backed acceptance criterion for every source-supported primary success outcome.
- Keep every material business rule linked from at least one acceptance criterion through `verifies`.
- A source clause that requires validation, verification, matching, uniqueness, a minimum/maximum/limit, authorization, or a state transition must retain a linked open question when the source does not define its deciding criterion.
- Do not convert an unresolved decision into an explicit acceptance criterion that merely repeats “verify”, “check”, “valid”, “invalid”, “sufficient”, or another undefined term.

## Evidence and placement repair

- Never fabricate an evidence quote or use a broad quote as proof of an unstated detail.
- When the report contains `exception_in_main_flow` or `exception_in_postcondition`, keep the source-supported behavior and place it under `flows.exceptions`. Do not leave exception, cancellation, or interruption behavior in the main flow or postconditions.
- Do not introduce exact UI wording, presentation details, internal algorithms, storage design, speculative failure paths, or duplicate questions while repairing.

Source ref: `{{SOURCE_REF}}`

## Source snapshot

{{SOURCE_TEXT}}

## Rejected JSON

{{REJECTED_JSON}}

## Validation report

{{VALIDATION_REPORT}}
