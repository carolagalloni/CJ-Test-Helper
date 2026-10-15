# wait_offset_then_relative_split

The shape of the retention journeys of step 3. The journey first waits until "13 days before EndDate" (a wait by
attribute with an offset of -13 days). It then asks "is EndDate on or after Today plus 13 days?". A contact that
arrives in time meets the condition exactly on its bound. A contact whose wait date is already past goes on at
once and fails it. Spec, step 3, "The clock", "Values of a date field read with relative dates or by a wait with an
offset" (its last paragraph describes this very journey) and "Validity of the data".

## Journey

```
WAIT-1 "13 days before EndDate"   wait by attribute on DE_Master.EndDate, waitDuration -13, waitUnit DAYS,
                                  specifiedTime "" -> SPLIT-1
SPLIT-1 "End date still 13 days ahead"
                                  DE_Master.EndDate AtOrAfter ;+;13;days -> EMAIL-1 "Renewal Reminder" -> UPDATE-1
                                  remainder "Late"                       -> UPDATE-2 "Too late"
```

The wait's expression is `{{Contact.Attribute.DE_Master."EndDate"}}`, in the step 3 export shape (attribute set
and field only). Tables (automation entry): `DE_Entry`, `DE_Master` (`EndDate` Date, nullable, plus the usual
fields), `DE_Log` (written only). T0 = 2026-10-11 12:00 (`tests/test_examples.py`), entry time T0, minimum
validity 1 day. "k" stands for T0 + k days at 12:00.

## The rules used

- **Validity** (spec, "Validity of the data": "how many days the activation can slip before any split reads it
  differently"; settled reading): the whole days the entry time can slip before any condition on the field, at any
  reachable delay, reads differently, computed by evaluating the conditions again. A value that no slip up to a
  year changes is **not perishable** (it contributes nothing to the deadline).
- **One value per class** (spec: "the one closest to the cut points among those whose margin ... reaches the
  setting 'minimum validity of the data', so that waits end as soon as the margin allows"; and "no larger than
  needed, so that the test runs as early as the margin allows"). The settled order:
  1. validity >= minimum validity (1 day), or all candidates when none reaches it;
  2. the waits by attribute on the field end soonest (only when a wait with an offset reads the field: here it
     does);
  3. not on a cut point;
  4. longest validity;
  5. earliest date.

  Null is dispreferred ("prefers those whose path rests on no unverified assumption: not null").

## Expected answer, step by step

1. **The wait.** Spec, "The clock": "A wait by attribute holds the contact until the date in its field plus its
   offset (...: '13 days before Entitle_RequestCancellationDate' is an offset of -13 days), at the time of day given
   by `specifiedTime` when it has one, otherwise at the time of day held in the field. When that moment is already
   past, the contact goes on at once (as before). When the field is null, the contact goes on at once
   (assumption)." `specifiedTime` is empty, so WAIT-1 ends at EndDate - 13 days, 12:00, or at once when that is
   past or EndDate is null.
2. **The split.** `AtOrAfter ;+;13;days`: date(EndDate) >= date(clock) + 13, bound included ("Both bounds are
   inclusive for `AtOrAfter` and `AtOrBefore`"). On null it is false.
3. **Cut points.** The relative condition gives `n` + delay = 13 + 0. The delay is 0 because "waits by attribute
   count zero". The wait gives "minus its offset plus the same delays" = 13 + 0. Cut points: **{13}**.
   Candidates: 11 ... 15, and null.
4. **Simulation of each candidate:**

   | EndDate | WAIT-1 ends at | Clock at SPLIT-1 | Today + 13 | Branch | Comparison |
   | --- | --- | --- | --- | --- | --- |
   | 11 (2026-10-22) | -2, past: at once | T0 (10-11) | 10-24 | remainder | below the bound |
   | 12 | -1, past: at once | T0 | 10-24 | remainder | below the bound |
   | 13 (2026-10-24) | T0 | T0 | 10-24 | `path_a` | **on the bound** |
   | 14 (2026-10-25) | +1 (2026-10-12 12:00) | 10-12 | 10-25 | `path_a` | **on the bound** |
   | 15 | +2 | 10-13 | 10-26 | `path_a` | **on the bound** |
   | null | at once (assumption) | T0 | 10-24 | remainder | false on null |

   For every EndDate from 13 on, the wait holds the contact until EndDate - 13 days. The split then compares EndDate
   with that same day + 13: equality, always. **`path_a` is reachable only on the bound.** The spec covers this
   case: "When the only values that take a branch sit on a bound (... true exactly on the bound for a contact that
   arrives in time), the generator uses the bound, and the testbook and the report say that the branch rests on
   the day-level comparison assumption." Under a full date-and-time comparison the branch would need the split to
   run at the very minute the wait ends, so the day-level assumption decides it.
5. **Validity of each candidate** (the condition `AtOrAfter ;+;13;days` at its only delay, 0, with the entry time
   slipped by s days):
   - 13: 13 >= 13 + s holds only for s = 0: **0**.
   - 14: holds for s <= 1: **1**. 15: **2**.
   - 11 and 12: false for every s: **not perishable**. null: not perishable.

   (Evaluating the whole path agrees. With EndDate 14 and an activation one day late, the wait date 2026-10-12 is
   "now", and the split on that day is on the bound. Two days late, the wait date is past, the contact goes on at
   once on 10-13, and 10-25 < 10-26 sends it to the remainder.)
6. **Choice.**
   - `path_a` {13, 14, 15}: rule 1 drops 13 (validity 0). Rule 2: the wait ends at 2026-10-12 12:00 for 14 and at
     2026-10-13 12:00 for 15, so **14 = `2026-10-25 12:00`** wins. The contact waits one day in WAIT-1 and reaches
     the split, on its bound, on 2026-10-12.
   - Remainder {11, 12, null}: null is dispreferred. Rule 1 keeps both (not perishable). Rule 2 is a tie (both
     waits end at once, at T0). Rule 3 keeps both (neither is 13). Rule 4 is a tie. Rule 5, earliest date, gives
     **11 = `2026-10-22 12:00`**. The wait date 2026-10-09 is past, the contact goes on at once, and 10-22 < 10-24.
   - The wait no longer writes the generation time into EndDate: "A wait by attribute with an offset no longer
     writes 'the generation time plus the wait offset' into its field when the field also has conditions: the field
     takes the candidate the case needs, and the testbook says when the wait ends."
7. **Cover.** Two terminal outcomes, one contact-level field: **1 scenario, 2 cases, 6 contacts**, both branches
   **covered**. EMAIL-1 is sent on the `path_a` case only.
8. **Validity and deadline.** The `path_a` value has 1 day. The remainder value is not perishable. Overall
   **1 day**: "import the files and activate the journey by **2026-10-12**" (T0 + 1 day = 2026-10-12 12:00).
9. **Timeline.** `path_a` case: WAIT-1 ends at day 1 (2026-10-12 12:00), and SPLIT-1 and EMAIL-1 are at the same
   moment. Remainder case: WAIT-1 ends at once (day 0, 2026-10-11 12:00), and SPLIT-1 is at day 0.

## What report.md and the testbook must say

- That `path_a` rests on the bound, so on the **day-level comparison assumption**. Both the report and the testbook
  must say it (spec sentence quoted in step 4). `expected.json` accepts several Italian wordings ("confronto sul
  giorno", "parte data", ...). The testbook should also say which case is which. The `path_a` case is the contact
  "in time", reaching the split on its bound after a one-day wait. The remainder case is the contact "late", whose
  wait date is already past.
- The deadline in the opening of report.md and in the testbook: 2026-10-12.
- The testbook says when the wait ends (2026-10-12 12:00 for the `path_a` case) and writes the timeline.
- The time zone of the waits is reported: "the time zone of the waits (`W. Europe Standard Time` in these exports)
  is reported".
- The assumption "A wait-by-attribute whose date is already past ends immediately", since the remainder case
  relies on it (as in `assumptions_wait`).

## History

The first version of this example chose EndDate 15 ("the one farthest from every cut point", the spec's wording at
the time). The team then settled the order of rules above ("no larger than needed, so that the test runs as early
as the margin allows"), which gives 14. The brief's first proposal, 13, has a margin of 0. Its other proposal for
the remainder, 10, is not a candidate.

## Not asserted

- The exact wording of the bound and day-level notes, of the deadline, and of the "in time" / "late" labels.
- The validity of the remainder case (not perishable).
- `Case.not_before` (2026-10-12 12:00 for the `path_a` case, in a format the spec does not fix).

**Update 14 October 2026.** The team confirmed that relative dates are compared on the day, not on the time, so the day-level comparison is a fact, no longer an assumption. The note on the bound in report.md and in the testbook now says that the branch is decided on the bound of a day-level comparison (it no longer says that it "rests on the assumption"), and report.md does not list the comparison among the assumptions. `expected.json` still asserts "confronto sul giorno" in both texts, unchanged.
