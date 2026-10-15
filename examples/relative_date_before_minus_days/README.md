# relative_date_before_minus_days

A relative date in the past with a strict operator: `LastOrder Before ;-;7;days` ("before Today Minus 7 days").
It is the mirror image of `relative_date_after_today`. Here a late activation makes the yes branch *easier* to
meet, so the yes value never expires. The remainder value is the one that expires. Spec, step 3, "Relative dates in
conditions", "Values of a date field read with relative dates or by a wait with an offset" and "Validity of the
data". The value choice uses the order the team settled on 14 October 2026 (below).

## Journey

```
SPLIT-1 "No order this week"   DE_Master.LastOrder Before ;-;7;days -> EMAIL-1 "We Miss You" -> UPDATE-1 "Win-back sent"
                               remainder                            -> UPDATE-2 "Recent order"
```

Tables (automation entry, as `one_split`): `DE_Entry`, `DE_Master` (`LastOrder` Date, nullable, plus the usual
fields), `DE_Log` (written only). T0 = 2026-10-11 12:00 (`tests/test_examples.py`), entry time T0, minimum
validity 1 day. Below, "k" stands for T0 + k days at 12:00.

## The rules used

- **Validity** (spec, "Validity of the data": "how many days the activation can slip before any split reads it
  differently"; settled reading): the whole days the entry time can slip before any condition on the field, at
  any reachable delay, reads differently. It is computed by evaluating the conditions again. A value that no slip
  up to a year changes is **not perishable** and contributes nothing to the deadline.
- **One value per class** (spec: "the one closest to the cut points among those whose margin ... reaches the
  setting 'minimum validity of the data', so that waits end as soon as the margin allows, or the farthest from
  every cut point when none reaches it"; the settled order of the tie-breaks):
  1. validity >= minimum validity (1 day), or all candidates when none reaches it;
  2. the waits by attribute on the field end soonest (only when a wait with an offset reads the field);
  3. not on a cut point;
  4. longest validity;
  5. earliest date.

  Null is dispreferred (step 2: "prefers those whose path rests on no unverified assumption: not null").

## Expected answer, step by step

1. **Reading.** `Before ;-;7;days` with the unit days: date(LastOrder) < date(clock) - 7 (day-level comparison,
   assumption). The operator is strict: a date exactly 7 days ago is *not* before. On null it is false, so null
   takes the remainder.
2. **Clock and cut point.** SPLIT-1 is the start, so the delay is 0 and the cut point is n + delay = **-7**.
   Candidates: -9, -8, -7, -6, -5 and null.
3. **Classes at entry** (clock T0, so the bound is 2026-10-04):

   | Candidate | Date | Before 10-04? | Branch |
   | --- | --- | --- | --- |
   | -9, -8 | 10-02, 10-03 | yes | `path_a` |
   | -7, -6, -5 | 10-04, 10-05, 10-06 | no | remainder |
   | null | (empty) | false | remainder |

4. **Validity of each candidate.** A slip of s days moves the bound to 10-04 + s.
   - -9 and -8 stay before the bound for every s >= 0: **not perishable**.
   - -7 (10-04): at s = 1 the bound is 10-05 and 10-04 < 10-05, so it becomes yes. Validity **0**.
   - -6 (10-05): s = 1 gives a bound of 10-05, not strictly before, so no change. s = 2 gives 10-06, which turns it
     into yes. Validity **1**.
   - -5 (10-06): it changes at s = 3. Validity **2**.
   - null: not perishable.
5. **Choice.**
   - `path_a` {-9, -8}: rule 1 keeps both (not perishable), rule 2 does not apply (no wait), rule 3 keeps both,
     rule 4 is a tie. Rule 5, earliest date, gives **-9 = `2026-10-02 12:00`**.
   - Remainder {-7, -6, -5, null}: null is dispreferred. Rule 1 drops -7 (validity 0). Rule 3 changes nothing (-7 is
     already out). Rule 4, longest validity, gives **-5 = `2026-10-06 12:00`** (2 days, against 1 for -6).
6. **Cover.** Two terminal outcomes, one contact-level field: **1 scenario, 2 cases, 6 contacts**, both branches
   **covered**. EMAIL-1 is sent on the `path_a` case only.
7. **Validity and deadline.** The `path_a` value is not perishable. The remainder value has 2 days. Overall
   **2 days**: "import the files and activate the journey by **2026-10-13**" (T0 + 2 days = 2026-10-13 12:00).
8. **Timeline.** No wait: SPLIT-1 and EMAIL-1 at day 0 (2026-10-11 12:00).

## What report.md and the testbook must say

- The deadline 2026-10-13 in the opening of report.md and in the testbook. It comes from the remainder case (the
  contacts with a recent order), not from the win-back case.

## Not asserted

- The validity of the `path_a` case (not perishable; whether `Case.validity_days` is then null is not fixed).
- The wording of the deadline sentence.

**Update 14 October 2026.** The team confirmed that relative dates ("Today +/- N days") are compared on the day, not on the time: the day-level comparison is a fact, no longer an assumption, so report.md does not list it among the assumptions any more (the comparison with hours or minutes, `docs/assumptions.md` row 48, is still one). The expected values above do not change.
