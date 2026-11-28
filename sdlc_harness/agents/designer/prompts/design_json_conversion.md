# Convert the design to the trace JSON

Your design at `{{DESIGN_REF}}` is final. Convert it into the trace JSON below. This is a conversion, not a new
analysis: every `U-n` and `DD-n` of the document becomes exactly one entry with the same ID, every row of the Scenario
paths table becomes one `scenario_paths` entry and every row of the Endpoint branches table one `endpoint_branches`
entry, and the JSON has nothing the document does not state. Do not edit the document.

- `units`: `kind` and `status` are the first word of the unit's Kind and Status lines; `files` lists every path of
  its Files line (without the `(new)` mark); `serves` lists the AC and IMP IDs of its Serves line; `decided_by` lists
  the `Q-n` and `DD-n` IDs of its Decided by line (`[]` for `-`); `uses` lists the `U-n` IDs of its Uses line (`[]` for
  `-`).
- `scenario_paths`: `units` keeps the order of the table.
- `endpoint_branches`: one entry per table row, cells copied as written.
- `design_decisions`: `decision`, `reason` and `alternatives` copied from each `DD-n` block.
- Requirement decisions (`Q-n`) are not part of this file; the Harness adds them as decisions.

```json
{
  "schema_version": "design_trace_v1",
  "units": [{"id": "U-1", "title": "...", "kind": "data", "status": "modify", "files": ["path/a.prisma"], "serves": ["AC-1", "IMP-2"], "decided_by": ["Q-4", "DD-1"], "uses": []}],
  "scenario_paths": [{"scenario": "SC-1", "units": ["U-4", "U-2", "U-1"], "acs": ["AC-1"]}],
  "endpoint_branches": [{"endpoint": "GET /api/items", "branch": "...", "result": "...", "scenarios": ["SC-1"]}],
  "design_decisions": [{"id": "DD-1", "title": "...", "decision": "...", "reason": "...", "alternatives": "..."}]
}
```
