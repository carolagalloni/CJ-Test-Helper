# exit_criteria_at_wait_end

Exit criteria evaluated at the end of a wait. The journey waits one day, then a split asks `Status Equal 'Active'`
and sends an email on the yes branch. The journey's exit criteria are `Status NotEqual 'Active' OR IsDunning Is true`.
A contact that is not Active (or is in dunning) leaves the journey when the wait expires, before the split. So the
split's remainder is unreachable *because of the exit criteria*, and the exit criteria are a coverage target of
their own. Spec, step 3, "Exit criteria and goals".

## Journey

```
WAIT-1 "1 day"     wait by duration, 1 DAYS -> SPLIT-1
SPLIT-1 "Active?"  DE_Master.Status Equal 'Active' -> EMAIL-1 "Active Offer" -> UPDATE-1 "Offer sent"
                   remainder                       -> UPDATE-2 "Not active"

exit criteria (journey.exits, key EXITCRITERIA):
                   DE_Master.Status NotEqual 'Active' OR DE_Master.IsDunning Is true
```

`journey.exits` has the shape of the real exports: `key` `EXITCRITERIA`, `type` `ExitCriteria`, `outcomes` [],
`configurationArguments.schemaVersionId` 257 and `criteria` (a `FilterDefinition` with a top-level `OR` group),
`metaData.criteriaDescription` and `chainType` `none`. The `filterResult` text is invented. Tables (automation
entry): `DE_Entry`, `DE_Master` with `Status` Text(20) **not nullable** and `IsDunning` Boolean nullable, `DE_Log`
(written only). T0 = 2026-10-11 12:00.

## Expected answer, step by step

1. **When the exit criteria are evaluated.** Spec: "Journey Builder evaluates the exit criteria of a journey for
   each contact when the contact's time in a wait activity expires (documented)" and "At the end of every wait on a
   path the exit criteria are evaluated with the clock; when they are true the contact leaves the journey there."
   The only wait is WAIT-1, at the start. The clock at its end is T0 + 1 day = **2026-10-12 12:00** ("A wait by
   duration advances the clock by its amount").
2. **Who leaves.** The criteria are an OR. `Status NotEqual 'Active'` is true for every non-null Status other than
   `Active`. `IsDunning Is true` is true only for `True`, false for `False` and for null. Status is not nullable, so
   a contact either has `Active` or another value:
   - Status other than `Active`: leaves at the end of WAIT-1.
   - Status `Active`, IsDunning `True`: leaves at the end of WAIT-1.
   - Status `Active`, IsDunning `False` or null: stays, reaches SPLIT-1, takes `path_a`.
3. **SPLIT-1.** Every contact that reaches it has Status `Active`, so `path_a` is **covered** and the remainder
   (Status not `Active`) is **not covered**. Spec: "A branch no contact can reach because every contact that gets to
   the wait before it meets the exit criteria is reported as unreachable with that reason." Every contact with the
   values of the remainder meets the exit criteria at the end of WAIT-1 and leaves the journey there.
   `reason_contains` accepts "criteri di uscita", "criterio di uscita" or "exit criteria".
4. **Exit target.** "Every exit criterion is one extra coverage target, 'exit criteria met at the end of a wait', so
   that the cover includes at least one case that leaves the journey this way when it is reachable". It is reachable
   (any contact of the first two kinds), so it is **covered** by a case whose expected path ends at WAIT-1:
   `exit_by_criteria` = `WAIT-1`, last activity `WAIT-1`, no email. Spec: "The expected path of such a case ends with
   'leaves the journey at the end of <wait>, because the exit criteria are met'".
5. **Values.** The `path_a` case has Status **`Active`**, written exactly as in the condition, and IsDunning
   **`False`**: `False` and null take the same branches, and null is avoided ("prefers those whose path rests on no
   unverified assumption: not null"). The exiting case may be Status = another value, or `Active` with IsDunning
   `True`. The spec leaves the choice open, so `expected.json` does not list its values.
6. **Cover.** The `path_a` branch and the exit target need contacts with different values, and nothing else is
   reachable: **1 scenario, 2 cases, 6 contacts**.
7. **Timeline.** `path_a` case: WAIT-1 ends at 2026-10-12 12:00 (day 1), and SPLIT-1 and EMAIL-1 are at the same
   moment. Exiting case: WAIT-1 ends at 2026-10-12 12:00, and the contact leaves there.
8. **Validity.** No value is chosen relative to the entry time (no relative condition, no wait by attribute):
   `validity_days` is null and there is no activation deadline.

Why Status is declared not nullable. With a nullable Status, a null would meet neither `NotEqual 'Active'` nor
`Equal 'Active'`. `NotEqual` "keeps its earlier reading": false on null, like every operator other than `IsNull`.
Such a contact would stay in the journey and reach the remainder, which would then be covered by a null Status. The
example keeps that case out, so that the remainder is unreachable only because of the exit criteria.

## What report.md and the testbook must say

- report.md says that the exit criteria are modelled (evaluated at the end of every wait). It lists the remainder
  of SPLIT-1 as unreachable because of them. The summary counts the exit target apart from the split branches
  ("the summary counts it apart from the branches of the splits").
- The testbook's expected result for the exiting case says the contacts leave the journey at the end of WAIT-1
  because the exit criteria are met. Both texts must name the exit criteria. `expected.json` checks "criteri di
  uscita" or alternatives.

## Not asserted

- The values of the exiting case, and the wording of the exit section.
- That report.md does not call these exit criteria "not modelled" (a phrase that may still appear for goals or other
  constructs).
