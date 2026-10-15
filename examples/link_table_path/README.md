# link_table_path

A split and a wait by attribute read a table that the contact model reaches only through another table, like the
holiday check of the step 3 exports (`PublicHoliday_DE` reached through `PublicHoliday_Entry`). The split compares
the holiday start with "Today"; the wait holds until the holiday end. Builder:
`examples/build_step3_structure_examples.py`, `build_link_table_path`.

## Journey and tables

```
SPLIT-1 "Holiday period"   Holiday_DE.HStart After ;+;0;days       ("after Today")        -> EMAIL-1 "Before You Leave"
                           Holiday_DE.HStart AtOrBefore ;+;0;days  ("at or before Today") -> WAIT-1
                           remainder                                                      -> EMAIL-3 "Regular News"
WAIT-1  wait by attribute on Holiday_DE.HEnd, no offset (waitDuration 0 DAYS, no specifiedTime) -> EMAIL-2 "Welcome Back"
EMAIL-1, EMAIL-2, EMAIL-3  -> exit
```

| Table | Fields | Contact model |
| --- | --- | --- |
| `DE_HL_Entry` | `SubscriberKey` (PK), `EmailAddress` | entry data extension, sendable on `SubscriberKey`, Run Once entry, not in Contact Builder |
| `Holiday_Entry` | `SubscriberKey` (PK), `HKey` Text(20), not nullable, default `A` | group-to-set relationship on `SubscriberKey` |
| `Holiday_DE` | `HKey` Text(20), `HStart` Date, `HEnd` Date (both nullable); no primary key | set-to-set relationship `Holiday_Entry.HKey -> Holiday_DE.HKey` |

Raw shapes (as in the real exports): both conditions have `Key="<Holiday_DE set>.HStart"`,
`UiMetaData="{&quot;groupToSetRelationshipId&quot;:&quot;<contact -> Holiday_Entry relationship>&quot;}"` and the value
inside `<AttributePath RelationshipID="<Holiday_Entry -> Holiday_DE relationship>">`; the wait has
`waitEndDateAttributeExpression` `{{Contact.Attribute.Contacts.Holiday_Entry.Holiday_DE."HEnd"}}`.

The test runner generates at `2026-10-11 12:00` (`tests/test_examples.py`, `NOW`); the entry time is not set, so it
is the generation time (spec, step 3, "The clock": "a setting whose default is the generation time"). Call it T0.
Dates below are in the default CSV format `YYYY-MM-DD HH:MM`.

## Expected answer, step by step

1. **Tables and roles.** Spec, step 3, "Tables reached through other tables": "Every table on the path from the
   contact to a read table is a **link table**: it gets a CSV, with the role 'link', because without its rows the
   journey cannot read the table at the end of the path." The path of both readers is `Holiday_Entry` (the
   condition's `AttributePath`, the wait's segment `Contacts.Holiday_Entry.Holiday_DE`). So: `DE_HL_Entry` entry,
   `Holiday_Entry` **link**, `Holiday_DE` split (Table selection: "Split table | Any table named in a split
   condition or in a wait-by-attribute | Yes"). **3 CSVs.** No chain has to be guessed (the export gives the path),
   so the "shortest chain ... with a warning" rule does not apply.
2. **Levels.** Same section: "the link table joined on the contact key is contact-level, the table it points to
   through another field is scenario-level (one holiday row shared by the contacts of a scenario), and the scenario
   key is written into the linking field of the link table." `Holiday_Entry` is joined on `SubscriberKey` (the
   group-to-set relationship's `rightAttributeName`, the contact key): **contact**. `Holiday_DE` is joined on `HKey`:
   **scenario**. The entry table: **contact**.
3. **Keys.** Data rule 1 ("Key or join field | Generated key, identical in every table that joins on it") and the
   sentence above: `Holiday_Entry.HKey` = `Holiday_DE.HKey` = the scenario key (`TST_R01_S01` ...), never the schema
   default `A` (priority 1 beats every other source; a shared `A` would also make the three scenarios read the same
   holiday). `Holiday_Entry.SubscriberKey` = the contact key.
4. **Values of `HStart`.** Spec, step 3, "Values of a date field read with relative dates": cut points = "for each
   relative condition, `n` plus every fixed delay with which a contact can reach that split". SPLIT-1 is the start,
   reached with delay 0; both conditions have `n` = 0: cut points {0}. Candidates: T0 moved by 0, +-1, +-2 days, plus
   null (nullable). Comparison level (assumption): unit `days` compares the date parts. Truth on (After Today,
   AtOrBefore Today):
   - T0-2d, T0-1d, T0: (false, true) -> `path_b`;
   - T0+1d, T0+2d: (true, false) -> `path_a`;
   - null: (false, false) -> remainder ("any other operator on null is false"; After / AtOrBefore are not negated).

   One value per class, "the one farthest from every cut point": **T0+2d = `2026-10-13 12:00`**,
   **T0-2d = `2026-10-09 12:00`**, **null**.
5. **`HEnd` (the wait date rule).** `HEnd` has no condition and WAIT-1 has no offset, so "A wait by attribute on a
   field with no condition keeps the earlier rule": "Wait date | Field used by a wait-by-attribute | Generation time,
   so the wait ends at once" plus the wait date offset (default 0 hours). No engagement split follows, so the
   +24 hours rule does not apply. **`HEnd` = `2026-10-11 12:00` in every `Holiday_DE` row.** On the clock: WAIT-1
   holds "until the date in its field plus its offset ... When that moment is already past, the contact goes on at
   once": T0 is not later than the clock when the contact arrives (T0 or later, after import and activation), so the
   wait ends at once and EMAIL-2 goes out at the arrival time. (Business-wise the future-start row has an end before
   its start; nothing reads `HEnd` on that path.)
6. **Scenarios and cases.** SPLIT-1's three outcomes depend only on the scenario-level `HStart`, one scenario
   crosses one outcome: at least **3 scenarios**, and the three classes give exactly 3. No contact-level field is
   read (the entry columns and `Holiday_Entry` are keys only; no split tests `Holiday_Entry` for existence, so no
   "row present / row absent" variable): one case per scenario. **3 / 3 / 9.**
   - `HStart` = `2026-10-13 12:00` -> `path_a` -> EMAIL-1;
   - `HStart` = `2026-10-09 12:00` -> `path_b` -> WAIT-1 (ends at once) -> EMAIL-2;
   - `HStart` null -> remainder -> EMAIL-3.

   All three branches **covered**.
7. **CSV rows.** `DE_HL_Entry` 9, `Holiday_Entry` 9 (one per contact: "each contact row finds ... its rows in the
   linked tables"; every contact has its link row, `has_row`), `Holiday_DE` 3 (one per scenario).
8. **Validity of the data.** Spec, step 3, "Validity of the data": `2026-10-13 12:00` reads "after Today" while the
   clock's date is before 2026-10-13: an activation 1 day late still works, 2 days late does not, so the margin is
   **1 day** (`validity_days` 1 for that case and for the whole result), which meets the default "minimum validity
   of the data" of 1 day. `2026-10-09` stays "at or before Today" however late the start; null and `HEnd` never
   change a branch. Deadline = T0 + 1 day: "import the files and activate the journey by **2026-10-12**", which
   "The report opens with ... when the data is perishable" and the testbook gives too.

## What report.md must say

- The opening: the activation deadline 2026-10-12 (`report_head_contains`; the date format of the text is not fixed
  by the spec, so three spellings are accepted).
- The three tables with their CSVs, `Holiday_Entry` as a link table (`report_contains` checks the names only).
- The triggered assumptions, among them the past wait date (`già passata`, as in `assumptions_wait`) and the
  day-level comparison of relative dates (wording not fixed by the spec: not asserted).

## Proposed expected.json keys used here

Not read by `tests/test_examples.py` yet (examples/README.md, "Extensions to the expected.json format"):

- `table_roles`: `{table: [roles]}`, every listed role is in that table's `TableRole.roles` (`entry`, `split`,
  `source`, `written`, `link`, as in `cjhelper/model.py`).
- `csv_rows`: `{table: n}`, the number of data rows in that table's CSV.
- `csv_values`: `{"Table.Field": [values]}`, the set of distinct values of that column over all rows of the table's
  CSV (`null` = empty cell), compared as a set.
- `validity_days` (top level, `CoverageResult.validity_days`) and, per case, `validity_days` and `timeline`
  (`{activity key: clock when the contact reaches it, the end for a wait}`), in the same form as the time examples
  of `examples/build_step3_time_examples.py`. `emails_sent` is an existing key.

## Not asserted

- Which scenario is S01, S02 or S03 (the run order); `csv_values` compares sets for that reason.
- How the testbook writes the timeline (the case timeline itself is in `timeline`: every step at T0, WAIT-1 included).
- The exact wording of the link-table reason and of the deadline sentence.

**Update 14 October 2026.** The team confirmed that relative dates ("Today +/- N days") are compared on the day, not on the time: the day-level comparison is a fact, no longer an assumption, so report.md does not list it among the assumptions any more (the comparison with hours or minutes, `docs/assumptions.md` row 48, is still one). The expected values above do not change.
