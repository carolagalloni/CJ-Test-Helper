# relative_date_chain_waits

Two splits read the same Journey Data date with relative bounds, and a 2-day wait sits between them. The clock
moves between the two splits, so the same window ("between 45 and 90 days from today") means different dates at
each one. The journey shortens the upper bound of the second split from 90 to 88 days to make up for the wait,
but keeps the lower bound at 45 days. Spec, step 3, "The clock", "Values of a date field read with relative dates
or by a wait with an offset" (cut points with delays) and "Validity of the data".

## Journey

```
SPLIT-1 "Ends in 45 to 90 days"  Event.EndDate AtOrBefore ;+;90;days AND Event.EndDate AtOrAfter ;+;45;days
                                     -> EMAIL-1 "Renewal Window" -> WAIT-1 "2 days" -> SPLIT-2
                                 remainder -> UPDATE-3 "Outside the window"
SPLIT-2 "Still in window"        Event.EndDate AtOrBefore ;+;88;days AND Event.EndDate AtOrAfter ;+;45;days
                                     -> UPDATE-1 "Still in window"
                                 remainder -> UPDATE-2 "Left the window"
```

`Event.EndDate` is a Journey Data condition (`Event.<eventDefinitionKey>.EndDate`, `IsEphemeralAttribute="true"`)
on the entry table `DE_RC_Entry` (`SubscriberKey`, `EmailAddress`, `EndDate` Date nullable). The entry is a data
extension, Run Once (as `journey_data_split`). `DE_RC_Log` is written only. T0 = 2026-10-11 12:00, entry time T0,
minimum validity 1 day. Below, "k" stands for the value T0 + k days (12:00).

## The rules used

- **Validity** (spec, "Validity of the data"; settled reading): the whole days the entry time can slip before any
  condition on the field, at any reachable delay, reads differently, computed by evaluating the conditions again.
  A value that no slip up to a year changes is **not perishable**.
- **One value per class**, in the settled order:
  1. validity >= minimum validity (1 day), or all candidates when none reaches it;
  2. the waits by attribute on the field end soonest (not applicable: no wait by attribute reads EndDate);
  3. not on a cut point;
  4. longest validity;
  5. earliest date.

  Null is dispreferred ("prefers ... not null").

## Expected answer, step by step

1. **The clock.** SPLIT-1 is the start: clock T0. EMAIL-1 is sent at T0. WAIT-1 "advances the clock by its amount":
   SPLIT-2 runs at T0 + 2 days (2026-10-13 12:00). Journey Data is the snapshot taken at entry, but the comparison
   is with the clock at each split.
2. **The windows in dates.** Day-level comparison, bounds inclusive. SPLIT-1 at T0: 45 <= k <= 90. SPLIT-2 at T0+2:
   2 + 45 <= k <= 2 + 88, that is **47 <= k <= 90**. The upper bounds coincide (T0+90 = 2027-01-09). Only the lower
   bound moves, from 45 to 47.
3. **Cut points.** "for each relative condition, `n` plus every fixed delay with which a contact can reach that
   split (the sum of the waits by duration on each path from the start ...)". SPLIT-1 is reached with delay 0, which
   gives {90, 45}. SPLIT-2 is reached with delay 2 (one path: SPLIT-1 -> EMAIL-1 -> WAIT-1), which gives
   {88 + 2, 45 + 2} = {90, 47}. Cut points: **{45, 47, 90}**.
4. **Candidates**: every cut point and the cut point plus or minus 1 and 2 days, which gives {43, 44, 45, 46, 47, 48,
   49, 88, 89, 90, 91, 92}, plus null.
5. **Classes** (same branches at every split that reads the field) and **validity**. The conditions are
   `AtOrBefore 90` and `AtOrAfter 45` at delay 0, and `AtOrBefore 88` and `AtOrAfter 45` at delay 2. A late
   activation of s days shifts all four bounds by s:

   | Class | SPLIT-1 | SPLIT-2 | Candidates with validity |
   | --- | --- | --- | --- |
   | A | remainder | not reached | 43 and 44 not perishable; 91: 0 (`AtOrBefore 90` turns true at s = 1); 92: 1; null not perishable |
   | B | `path_a` | remainder | 45: 0 (`AtOrAfter 45` fails at s = 1); 46: 1 |
   | C | `path_a` | `path_a` | 47: 0; 48: 1; 49: 2; 88: 41; 89: 42; 90: 43 (`AtOrAfter 45` at delay 2 fails from s = k - 46) |

   (Reading classes as truth vectors per condition and delay splits A into {43, 44}, {91, 92} and {null}. The
   value kept for the branch is the same.)
6. **Choice.**
   - Case A (SPLIT-1 remainder): null is dispreferred. Rule 1 drops 91. Rule 3 drops nothing. Rule 4 keeps 43 and 44
     (not perishable, against 1 day for 92). Rule 5, earliest date, gives **43 = `2026-11-23 12:00`**. However late
     the activation, it never enters the window.
   - Case B (SPLIT-1 `path_a`, SPLIT-2 remainder): rule 1 drops 45, which leaves **46 = `2026-11-26 12:00`**
     (validity 1). It passes the first window (45 <= 46 <= 90) and fails the second (46 < 47).
   - Case C (both `path_a`): rule 1 drops 47. Rule 3 drops 90 (a cut point). Rule 4, longest validity among
     {48, 49, 88, 89}, gives **89 = `2027-01-08 12:00`** (validity 42).
7. **Cover.** Three terminal outcomes (UPDATE-1, UPDATE-2, UPDATE-3), one contact-level field: **1 scenario, 3 cases,
   9 contacts**, all four branches **covered**. EMAIL-1 is sent on cases B and C.
8. **Validity and deadline.** Case A is not perishable, case B has 1 day, case C has 42. Overall **1 day**:
   "import the files and activate the journey by **2026-10-12**" (T0 + 1 day = 2026-10-12 12:00). Case B alone sets
   it. It is the contact that is in the window at entry and *leaves* it during the wait: two days late, it would no
   longer be in the window at entry.
9. **Timeline.** Cases B and C: SPLIT-1 and EMAIL-1 at day 0 (2026-10-11 12:00). WAIT-1 ends and SPLIT-2 runs at
   day 2 (2026-10-13 12:00). Case A: SPLIT-1 at day 0.

## What report.md and the testbook must say

- The deadline 2026-10-12 in the opening of report.md and in the testbook.
- The timeline of cases B and C, with SPLIT-2 two days after entry. The testbook should make clear that case B is
  the contact that *was* in the window at entry and *is no longer* in it after the wait.

## Notes for the reviewer

- 89 passes both splits. The brief said that "a value at T0+89 days passes the first and fails the second", but
  SPLIT-2 runs at T0+2, so its upper bound is T0+2+88 = T0+90, the same date as SPLIT-1's. The second split's
  remainder is reached on the lower side (46), because the journey did not move the 45 days bound to make up for
  the wait. That is also the useful finding for the journey's author.
- The spec sentence on the choice ("the one closest to the cut points among those whose margin ... reaches the
  setting") would favour 90 or 48 in class C. 90 is on a cut point (distance 0, margin 43); 48 and 89 are both at
  distance 1. The settled order (not on a cut point, then longest validity) gives 89. `expected.json` follows the
  settled order.
- The first version of this example accepted two values for cases A and C ("farthest from every cut point" ties).
  The settled order breaks those ties.

## Not asserted

- The validity of case A (not perishable).
- Run Once texts (covered by `run_once_entry`).

**Update 14 October 2026.** The team confirmed that relative dates ("Today +/- N days") are compared on the day, not on the time: the day-level comparison is a fact, no longer an assumption, so report.md does not list it among the assumptions any more (the comparison with hours or minutes, `docs/assumptions.md` row 48, is still one). The expected values above do not change.
