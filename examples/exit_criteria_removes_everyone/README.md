# exit_criteria_removes_everyone

The shape of one of the six step 3 exports, a four-week retention journey. Its exit criterion is
`<date> is not null OR ...`. The split the contacts must pass before the first wait requires the same date to be
not null. Every contact that reaches the first wait therefore meets the exit criteria when the wait expires, and
leaves. Everything behind the first wait is dead. The spec asks the report to say so as a finding on the journey:
"One of the six exports has `Entitle_RequestCancellationDate is not null` as an exit criterion, which every contact
its splits accept satisfies: the report must say that every branch behind the first wait is unreachable for this
reason, as a finding on the journey."

## Journey

```
SPLIT-1 "End date known?"  DE_Master.EndDate IsNotNull -> WAIT-1
                           remainder                   -> UPDATE-3 "No end date"
WAIT-1 "2 days"            wait by duration, 2 DAYS     -> SPLIT-2
SPLIT-2 "Opt-in"           DE_Master.Optin Is true      -> EMAIL-1 "Retention Offer" -> UPDATE-1 "Offer sent"
                           remainder                    -> UPDATE-2 "No opt-in"

exit criteria (journey.exits, key EXITCRITERIA):
                           DE_Master.EndDate IsNotNull OR DE_Master.IsDunning Is true
```

Same `journey.exits` shape as `exit_criteria_at_wait_end`. Tables (automation entry): `DE_Entry`, `DE_Master`
(`EndDate` Date, `IsDunning` and `Optin` Boolean, all nullable), `DE_Log` (written only). T0 = 2026-10-11 12:00.

## Expected answer, step by step

1. **SPLIT-1 comes before any wait**, so the exit criteria are not evaluated before it ("At the end of every wait on
   a path the exit criteria are evaluated"). Both outcomes are reachable: EndDate not null -> `path_a` -> WAIT-1.
   EndDate null -> remainder -> UPDATE-3, an exit with no wait on the path. Both **covered**.
2. **The end of WAIT-1** (clock T0 + 2 days = 2026-10-13 12:00). Every contact there came through SPLIT-1 `path_a`,
   so its EndDate is not null. No Update contact writes EndDate, so the value read live is the one SPLIT-1 read. The
   first operand of the OR is true, and **every** contact leaves the journey at the end of WAIT-1.
3. **SPLIT-2.** No contact reaches it. Both outcomes are **not covered**, with the exit-criteria reason: "A branch no
   contact can reach because every contact that gets to the wait before it meets the exit criteria is reported as
   unreachable with that reason." EMAIL-1 is never sent.
4. **Exit target.** Reachable, so **covered**, by the case that takes SPLIT-1 `path_a`. Its expected path is
   SPLIT-1 -> WAIT-1 and ends there (`exit_by_criteria` = `WAIT-1`).
5. **Values and cover.** The `path_a` case has a non-null EndDate. The spec does not fix the date, since the field has
   no relative condition and no wait by attribute (the step 2 rule "Other date fields: Generation time" applies).
   The remainder case has EndDate **null** (an empty cell). IsDunning and Optin play no part. **1 scenario,
   2 cases, 6 contacts.**
6. **Timeline.** `path_a` case: SPLIT-1 at day 0 (2026-10-11 12:00), WAIT-1 ends at day 2 (2026-10-13 12:00), and
   the contact leaves there. Remainder case: SPLIT-1 at day 0.
7. **Validity.** No value relative to the entry time: `validity_days` is null.

## Finding on the journey (what report.md must say)

The exit criterion `EndDate is not null` is met by every contact that the first split lets through. As a result:

- every branch behind WAIT-1 (both outcomes of SPLIT-2) is unreachable;
- EMAIL-1 is never sent;
- the only test the journey allows behind the first split is "the contact leaves at the end of WAIT-1".

The real journey this mirrors most likely meant `is null`: the other exports of the same family have `is null`.
The generator cannot know that and must not guess. report.md must state the fact as a finding on the journey,
naming the exit criteria and the wait. `expected.json` checks that report.md mentions the exit criteria and names
WAIT-1. The testbook's expected result for the `path_a` case says the contacts leave at the end of WAIT-1 because
the exit criteria are met.

## Not asserted

- Where the finding sits in report.md (warnings, exit criteria section, summary) and its wording.
- The EndDate value of the `path_a` case.
