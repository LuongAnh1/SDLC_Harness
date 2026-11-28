# Design the change

You are the Designer. Read the approved requirements at {{REQ_REF}} and the repository (CodeGraph is available), then
write the design to {{DESIGN_REF}}. Do not change any code.

The requirements are final: the `Answer:` line of each question Q-n is fixed. Write
what a developer needs to implement them in this repository:

1. Every existing endpoint, query, data model, screen and component the use case affects, and how each must change,
   including the ones the requirements do not name but that read or write the same data.
2. The new data fields, migration and backfill, endpoints, UI pieces and seed changes.
3. How each scenario below runs through these units.

Design each unit once. An endpoint, query or screen that several scenarios use is one unit that describes every branch
it takes (status and body for an endpoint), not one copy per scenario. Keep the repository's existing behavior unless a
requirement asks to change it, and say which requirement does.

Choices with no observable difference (columns, route shapes, storage, algorithms) are yours: record each as a design
decision `DD-n` with its reason. If you find an observable decision the requirements leave open (what is accepted or
refused, what a request answers, what is shown, who may act), do not ask anyone: settle it yourself as a requirement
decision (layout below) and mark every unit it decides with "(decided by Q-n, agent decision)". For a design or
requirement decision, prefer the option that keeps the repository's existing behavior; change it only as far as a
requirement asks, and cite the file that shows the existing behavior.

The repository runs on a database that already holds real users' data. Migrations and seed changes must be safe to
run, and to run again, on that database: the seed may still create and restore its own demo records, but it must never
break a rule of the requirements or undo or remove what users have since done (records they changed, and the rows
linked to them).

## Scenarios

{{SCENARIOS}}

## Existing routes of the repository

Every route below must appear, written exactly as listed (for example `GET /api/articles/feed` or `/editor/:slug`),
either in the unit that changes it or under "Unchanged surfaces" with the reason it stays unchanged.

{{ROUTES}}

## Layout

Use these sections and define each ID once, at the start of its own line:

```markdown
## Units
### U-1 <name>
- Kind: endpoint | query | data | migration | seed | screen | component | other
- Status: modify | new
- Files: `path/to/file.ts`, `path/to/planned.ts` (new), ... (mark a file that does not exist yet with (new))
- Serves: AC-1, IMP-3 (every AC and every IMP of the requirements is served by at least one unit)
- Decided by: Q-4, DD-2 (the answered questions and design decisions it applies; every answered question is applied
  by at least one unit)
- Uses: U-7, U-4 (the units this unit calls, reads or writes directly; `-` when none. An endpoint reaches the stored
  data through its Uses, a screen reaches the endpoints it calls)
<behavior; for an endpoint: the request and every branch with its status and body; for a screen: its states, actions
and errors>

## Endpoint branches
| Endpoint | Branch | Status / body | Scenarios |

## Scenario paths
| Scenario | Units in order | ACs |
| SC-1 | U-1, U-4 | AC-1, AC-6 |

## Unchanged surfaces
- `<METHOD> <path>` or `<web route>` (`file`): <why it stays unchanged>

## Design decisions
### DD-1 <name>
- Decision: ...
- Reason: ... (cite the repository file when it rests on existing behavior)
- Alternatives: ...

## Requirement decisions
(only when needed; numbers continue after the last question of the requirements)
### Q-17 <name>
<the decision as a question ending with ?>
- Disposition: agent_decided
- Answer: ...
- Reason: ...
- Status: answered

## Notes
```

Write the design with the write tool and read it once to check it.
