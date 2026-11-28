# Convert the requirements to the trace JSON

Your requirements at `{{DRAFT_REF}}` are reviewed and include the owner's answers. Convert their use-case clauses,
acceptance criteria and implied obligations into the trace JSON below. This is a conversion, not a new analysis:
every `UC-n`, `AC-n` and `IMP-n` of the document becomes exactly one entry with the same ID, and the JSON has no
entry the document does not define. Do not edit the document. The use case is at `{{SOURCE_REF}}`; copy each clause
quote from it character for character. Questions and decisions are not part of this file; the Harness adds them.

- `uc_clauses`: `quote` is the verbatim clause; a clause the document quotes in several parts uses `quotes`, a list,
  instead. A clause the document marks "no observable behavior" has `"observable": false` and its `reason`; every
  other clause has `"observable": true` and `"reason": null`.
- `kind` places each clause in the use case: `main_step` (a step of the main flow), `alternative` (an alternative
  flow), `exception` (an error or exception flow), `exit` (an exit condition), `quality` (a quality requirement), or
  `context` (description, actors, entry condition).
- `scenario` groups clauses into the situations the work must cover: all `main_step` clauses share `SC-1`; each
  alternative or exception flow is its own scenario, shared by its sentences; each exit condition and each quality
  requirement is its own scenario. Every scenario holds clauses of one kind. A `context` clause has
  `"scenario": null`.
- `acceptance_criteria`: `text` is the condition and result, `api` and `web` are the document's API and Web parts
  (null when the document has none), `covers` lists the clauses from its Covers line.
- `implied_obligations`: `serves` lists the clauses it serves, `repo_evidence` lists the repository files it cites.
- `decided_by` lists the questions whose answer the item states (the document says "decided by Q-n"), else `[]`.

```json
{
  "schema_version": "requirement_trace_v1",
  "uc_clauses": [{"id": "UC-5", "quote": "...", "observable": true, "reason": null, "kind": "main_step", "scenario": "SC-1"}],
  "acceptance_criteria": [{"id": "AC-1", "title": "...", "text": "...", "api": "...", "web": "...", "covers": ["UC-2"], "decided_by": []}],
  "implied_obligations": [{"id": "IMP-1", "text": "...", "serves": ["UC-4"], "repo_evidence": ["path/to/file.ts"], "decided_by": []}]
}
```
