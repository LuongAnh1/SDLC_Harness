## Reliance
| Edge | Decision | Reason |
| --- | --- | --- |
| U-4 -> U-26 | drop | U-4 only adds `draft: article.published === false` to the returned contract object and keeps every other field; its Design promises no `P2002`/title-error response, so it merely runs in front of the handler. |
| U-6 -> U-26 | keep | U-6 states "a `publishedTitle` conflict is re-thrown for U-26 to map to a 422 title error (AC-16)", and its published-create branch is decided by DD-10's narrowed `P2002` mapping (AC-16, IMP-20). |
| U-7 -> U-26 | keep | U-7 states it "lets a `publishedTitle` conflict surface as P2002 for U-26" (U-7; AC-16, DD-10), so the promised title-conflict outcome of a published update only exists through U-26. |
| U-8 -> U-26 | drop | U-8 itself returns `title-taken` before writing content when another published row already uses the title (U-8 step 5, AC-8), so its stated outcome does not depend on the handler's `P2002` mapping; U-20 turns `title-taken` into the 422. |
| U-9 -> U-26 | drop | U-9 promises `null`/`not-found`/`published-conflict` outcomes for the draft delete and keeps the current published delete (Q-9, Q-22, DD-7); no unique-conflict response is stated. |
| U-10 -> U-26 | drop | U-10 is a read/resolve query that returns a row or `null` (Q-3, DD-2, DD-12); it promises no error response. |
| U-11 -> U-26 | drop | U-11 adds `published: true` to the list/feed `where` clauses (DD-13); it produces pages and counts only. |
| U-12 -> U-26 | drop | U-12 reads the author's drafts and their count (Q-10, Q-11, DD-5); no conflict or error response is stated. |
| U-13 -> U-26 | drop | U-13 returns `null` or the existing comment list/relation change after a pre-read (Q-6, Q-18, DD-16); the published-article comment path keeps the existing `P2025` handling, which U-26 does not change. |
| U-14 -> U-26 | drop | U-14 pre-reads the article and then takes the current `favoritedBy` connect/disconnect or returns `null` (Q-6); no unique conflict is stated. |
| U-15 -> U-26 | drop | U-15 is a `tag.findMany` filtered by `published: true` (DD-1); read-only, no error response. |
| U-16 -> U-26 | drop | U-16 promises 200 `{ articles, articlesCount }` and the historical 200 query-error body, with "Database/other errors go to `next`" (DD-14); no `P2002`/`P2025` mapping is stated. |
| U-17 -> U-26 | drop | U-17 promises 401/200 (and the unchanged 200 feed-query body) with U-11's published-only filter (DD-13); no unique-conflict response is stated. |
| U-18 -> U-26 | drop | U-18 promises only 200 `{ article }` or `res.sendStatus(404)` (AC-3, DD-12); no Prisma error mapping is stated. |
| U-19 -> U-26 | keep | U-19 states "A published title already used by another published article: 422 `{ errors: { title: ["title is already taken"] } }` through U-26" (U-19; AC-16, IMP-20), a response only U-26's narrowed mapping produces. |
| U-20 -> U-26 | keep | U-20 states that on the published-row edit path "A changed title that another published article uses answers the same 422 title error" (U-20), and U-26 states "A published-title collision from any write path (create, publish, published-title edit) is therefore a 422 title field error" (U-26, AC-8, DD-10). |
| U-21 -> U-26 | drop | U-21 promises 401/404/200 delete responses and the current published-delete behaviour (Q-9, Q-22, Q-23, DD-7); no title/unique-conflict response is stated. |
| U-22 -> U-26 | drop | U-22 promises 401/404/200/201 comment responses and keeps the published-article `P2025` path (Q-6, Q-18, DD-16); U-26 does not change `P2025`, so nothing it does is needed. |
| U-23 -> U-26 | drop | U-23 promises 401/404/200 favorite responses and keeps its 500 guard (Q-6); no unique-conflict response is stated. |
| U-24 -> U-26 | drop | U-24 promises 404 for non-authors and 400 `{ errors: { query: [...] } }` from its own validator, plus 200 `{ articles, articlesCount }` (Q-3, DD-17); no Prisma error mapping is stated. |
| U-25 -> U-26 | drop | U-25 promises 200 `{ tags: string[] }` computed from U-15 (DD-1); read-only, no error response. |

## Base
Option: 0
Reason: Options 1 and 2 estimate T 34.0 against Option 0's 35.0, a 2.9 % reduction that is far below the 15 % threshold, so the extra split and its stub/integration risk are not justified. U-1, U-3, U-4 and U-26 also cover the schema, the `draft` contract/viewer, and the title-error mapping that the four kept reliance edges run into.

## Stubs
none

## Notes
- Only U-6, U-7, U-19 and U-20 rely on U-26; every other dropped unit runs in front of `prismaErrorHandler` but its Design promises no response the handler produces. Keeping U-26 in the base means no unit is ordered before a dependency by these drops.
- U-8's publish-time title conflict is produced by U-8/U-20 as `title-taken`, not by U-26; the Design does not promise a `P2002` on `publishedTitle` in that path, so a concurrent-collision fallback through the handler is unspecified rather than relied on.
- No reliance edge was added: no unit's Uses omits a unit whose stated behavior its Design depends on (U-30's and U-32's mention of 422 title responses is satisfied through U-19/U-20, whose own edges are kept).
