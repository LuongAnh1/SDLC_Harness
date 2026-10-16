# Requirement Model V2 Bounded Repair

Return exactly one complete JSON object. Do not use markdown fences or prose.

Repair only the schema, ID/reference, dependency, or evidence-grounding violations listed in the validation report. Preserve valid IDs and supported business content. Do not add confirmed content from domain knowledge, create human decisions, or move exception behavior into the main flow or postconditions.

Source ref: `{{SOURCE_REF}}`

## Source snapshot

{{SOURCE_TEXT}}

## Rejected JSON

{{REJECTED_JSON}}

## Validation report

{{VALIDATION_REPORT}}
