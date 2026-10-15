# relative_date_after_today

A decision split compares a Date field with "Today" through relative values, in the shape of the real exports:
`StartDate After ;+;0;days`, then `StartDate AtOrBefore ;+;0;days`, then the remainder. The values must be chosen
relative to the entry time and away from the bound, and the report must give the activation deadline, because
the data stops working when the test starts late. Spec, "Extensions for time-dependent journeys (step 3, 14 October
2026)", sections "Relative dates in conditions", "Values of a date field read with relative dates or by a wait with
an offset" and "Validity of the data".

## Journey

```
SPLIT-1 "Start date"   DE_Master.StartDate After      ;+;0;days -> EMAIL-1 "Starting Soon" -> UPDATE-1 "Future start"
                       DE_Master.StartDate AtOrBefore ;+;0;days -> UPDATE-2 "Already started"
                       remainder                                -> UPDATE-3 "No start date"
```

Tables (automation entry, as `one_split`): `DE_Entry` (`ContactId`, `Emailaddress`), `DE_Master` (`ContactId`,
`FirstName`, `Emailaddress`, `MobilePhone`, `StartDate` Date, nullable), `DE_Log` (written only). Entry query
`SELECT M.ContactId, M.Emailaddress FROM DE_Master M`. Builder: `examples/build_step3_time_examples.py`.

Time: the tests fix the generation time at **T0 = 2026-10-11 12:00** (`NOW` in `tests/test_examples.py`). The entry
time is left at its default ("The clock starts at the entry time, a setting whose default is the generation time"),
and so is the minimum validity of the data (1 day). Every date below is T0 moved by whole days, at 12:00.

## Expected answer, step by step

1. **Reading the conditions.** Spec: "A condition value written `;<sign>;<n>;<unit>` ... compares the field with the
   clock's date moved by `n` units", and "with the unit `days` ... the field's date part is compared with the clock's
   date part" (an assumption). So `path_a` is "date(StartDate) > date(clock)", `path_b` is
   "date(StartDate) <= date(clock)". A non-null value satisfies exactly one of the two; the remainder is taken only by
   null, since on null every operator other than `IsNull` (and the negated `IsNot`) is false.
2. **The clock at SPLIT-1.** SPLIT-1 is the start activity and no wait comes before it: the clock is T0.
3. **Cut points.** "for each relative condition, `n` plus every fixed delay with which a contact can reach that
   split": `n` = 0 for both conditions, delay 0. Cut points: **{0}**.
4. **Candidates.** "the entry time moved by every cut point and by every cut point plus or minus one and two days, ...
   plus null when the schema allows it": T0-2, T0-1, T0, T0+1, T0+2, null.
5. **Classes** ("Candidates that take the same branches at every split that reads the field ... are one class"):

   | Candidate | date compared with 2026-10-11 | Branch |
   | --- | --- | --- |
   | T0+1, T0+2 | later | `path_a` |
   | T0, T0-1, T0-2 | same day or earlier | `path_b` |
   | null | (no value) | remainder |

6. **Validity of each candidate.** Spec, "Validity of the data": "The generator computes for every such value how
   many days the activation can slip before any split reads it differently, and the report and the testbook give
   the earliest of these as the deadline". The settled reading: the whole days the entry time can slip before any
   condition on the field, at any reachable delay, reads differently, computed by evaluating the conditions again.
   A value that no slip up to a year changes is not perishable and contributes nothing to the deadline. For
   StartDate = T0+2 (2026-10-13 12:00), activation slipping by s whole days:

   | s | Clock at SPLIT-1 | 2026-10-13 after that day? | Branch |
   | --- | --- | --- | --- |
   | 0 | 2026-10-11 | yes | `path_a` |
   | 1 | 2026-10-12 | yes | `path_a` |
   | 2 | 2026-10-13 | **no** (same day) | `path_b` |

   So T0+2 has **1 day**, not 2. The distance from the cut point is 2 days, but `After` excludes the cut point
   itself: the value stops being "after today" on the day it *is* today. For an inclusive operator (`AtOrAfter`,
   `AtOrBefore`) the two figures coincide. T0+1 has 0. T0, T0-1 and T0-2 read with `AtOrBefore` (and `After`) the
   same way for any late activation: **not perishable**. Null is not compared with the clock.
7. **One value per class.** Spec: "the one closest to the cut points among those whose margin ... reaches the
   setting 'minimum validity of the data' ..., or the farthest from every cut point when none reaches it". The
   settled order is:
   1. validity >= minimum validity (1 day), or all candidates when none reaches it;
   2. the waits by attribute on the field end soonest (only with a wait with an offset: none here);
   3. not on a cut point;
   4. longest validity;
   5. earliest date.

   Null is dispreferred.
   - `path_a` {T0+1, T0+2}: rule 1 drops T0+1 (validity 0), which leaves **T0+2 = `2026-10-13 12:00`**.
   - `path_b` {T0, T0-1, T0-2}: all are not perishable. Rule 3 drops T0 (the cut point). Rule 4 is a tie. Rule 5,
     earliest date, gives **T0-2 = `2026-10-09 12:00`**.
   - Remainder: only **null** (an empty cell).
8. **Cover, validity, deadline.** Each outcome is terminal (EMAIL-1 and the updates lead to exits), and there is no
   scenario-level table: **1 scenario, 3 cases, 9 contacts**, all three branches **covered**. EMAIL-1 is sent only
   on the `path_a` case. Case validity: 1 for `path_a`; `path_b` not perishable (not asserted); none for the null
   case (nothing in it depends on the clock). Overall **1 day**, which meets the minimum validity of 1 day.
   Deadline: **import the files and activate the journey by 2026-10-12** (T0 + 1 day = 2026-10-12 12:00; any time
   that day still works under the day-level comparison).
9. **Timeline.** There is no wait, so every step is at day 0 (2026-10-11 12:00).

## What report.md and the testbook must say

- The opening of report.md gives the deadline ("The report opens with the deadline for activation when the data is
  perishable"): the date 2026-10-12. The testbook gives it too ("the report and the testbook give the earliest of
  these as the deadline"). `expected.json` accepts `2026-10-12`, `12/10/2026` or `12 ottobre 2026`.
- The testbook writes the timeline as days from the entry time with the absolute date and time: day 0,
  2026-10-11 12:00, for every step.

## Not asserted

- The wording of the deadline sentence and its time of day (end of day or 12:00).
- The validity of the `path_b` case: not perishable (whether `Case.validity_days` is then null is not fixed).
- Whether report.md lists the day-level comparison assumption. The export triggers it (a relative condition in
  days), but no value here rests on it: every value is two days away from the bound, and null is not compared.

Proposed keys used here (`cases[i].timeline`, `cases[i].validity_days`, `cases[i].exit_by_criteria`, top-level
`validity_days`) are described in `examples/README.md`, "Extensions to the expected.json format (proposed)".

**Update 14 October 2026.** The team confirmed that relative dates ("Today +/- N days") are compared on the day, not on the time: the day-level comparison is a fact, no longer an assumption, so report.md does not list it among the assumptions any more (the comparison with hours or minutes, `docs/assumptions.md` row 48, is still one). The expected values above do not change.
