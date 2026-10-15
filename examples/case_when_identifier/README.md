# case_when_identifier

The entry query computes an entry column with `CASE WHEN ... THEN 'True' ELSE 'False' END` from the timestamps of a
LEFT JOINed backup table (the "already emailed" flag of the step 3 exports), and a split reads that column with
`IsNot 'true'`. Builder: `examples/build_step3_structure_examples.py`, `build_case_when_identifier`.

## Journey and tables

```
SPLIT-1 "Emailed this week?"   Event.<key>.Email_Identifier IsNot 'true'  -> UPDATE-1 "Send reminder"   (label N)
                               remainder                                  -> UPDATE-2 "Already emailed" (label Y)
```

```sql
SELECT M.ContactId, M.Emailaddress,
    CASE WHEN B.Email_Timestamp IS NOT NULL AND B.Email_Timestamp >= DATEADD(day, -7, GETDATE())
         THEN 'True' ELSE 'False' END AS Email_Identifier
FROM [DE_Master] M
LEFT JOIN [DE_Backup] B ON M.ContactId = B.ContactId
```

Tables: `DE_Entry` (`ContactId` PK, `Emailaddress`, `Email_Identifier` Boolean, nullable), `DE_Master` (`ContactId`
PK, `FirstName`, `Emailaddress`, `MobilePhone`), `DE_Backup` (`ContactId` PK, `Email_Timestamp` Date, nullable),
`DE_Log` (written only). Automation entry; Master -> Entry on `ContactId` in Contact Builder; `DE_Backup` is not in
Contact Builder. The runner generates at `2026-10-11 12:00`.

## Expected answer, step by step

1. **Tables.** `DE_Backup` is in a JOIN of the entry query: "Source table | Any table in the FROM or JOIN of a query
   that fills the entry table ... | Yes". Spec, step 3: "A `LEFT JOIN` source still gets one row per contact (read as
   an inner join, as before); the report says so." **3 CSVs**, all **contact**-level (`DE_Backup` is joined on
   `ContactId`, the contact key). `DE_Log`: no CSV.
2. **The computed column.** The CASE is not one of the "simple select expressions (column, literal, `CONCAT`,
   current date)". Spec, step 3: "A column of the entry table that a split reads and that the entry query computes
   with an expression the generator does not understand (a `CASE WHEN ... THEN 'True' ELSE 'False' END` on the
   timestamps of an earlier run) takes the value the case needs (data rule 2), not a placeholder." So
   `Email_Identifier` is a coverage variable of the entry table, written as the case needs it.
3. **IsNot.** Spec, step 3, "Relative dates in conditions": "`IsNot` is the negation of `Is` (on a Boolean `IsNot
   true` is true for `false` and for null, assumption ...)". Domain of `Email_Identifier` (Boolean, nullable):
   `True`, `False`, null.
   - `True`: `IsNot true` false -> remainder (Y) -> UPDATE-2.
   - `False`: true -> `path_a` (N) -> UPDATE-1.
   - null: true (the assumption) -> `path_a` (N) -> UPDATE-1.
4. **Which value for N: `False`.** `False` and null cover the same branch. Spec, "Operators, types and values":
   "Among values that cover the same branches, the generator prefers those whose path rests on no unverified
   assumption: not null, row present." The null path rests on the `IsNot`-on-null assumption, the `False` path on
   none: the N case has **`Email_Identifier` = `False`**. (`True` and `False` are also the only values the CASE can
   produce.)
5. **Cases.** 1 scenario (no scenario-level field), 2 cases (`False` -> UPDATE-1, `True` -> UPDATE-2), **1 / 2 / 6**;
   both branches **covered**.
6. **The backup rows.** One `DE_Backup` row per contact (step 1), `ContactId` = the contact key (data rule 1).
   `Email_Timestamp` is not a key, not read by a split and not in the WHERE (the CASE is in the SELECT), so data rule
   4 applies: "Other date fields | None of the above | Generation time": **`2026-10-11 12:00` in all 6 rows**. (Its
   name starts with `Email` but its type is Date: the email rule does not apply, and "Every value respects the type"
   of its schema; likewise `Email_Identifier` is a Boolean read by a split, data rule 2.) The generator does not
   reproduce the rows that would make the CASE give each case's value (spec: "the automation would compute it from
   rows the generator does not reproduce"); with these rows the automation would compute `True` for all six contacts
   (timestamp present and within 7 days), which is exactly why the direct import is the only valid procedure here.
7. **Entry trust check.** Spec, step 3, "Honest checks": "The trust check on the entry query reports a column it
   cannot verify (an expression the generator does not understand, ..., a LEFT JOIN read as an inner join) as 'not
   verified', with the reason, and fails only on what it can verify." `ContactId` and `Emailaddress` are verified;
   `Email_Identifier` (CASE) and the LEFT JOIN are "not verified": the check **passes** with that note
   (`checks_pass`).

## What report.md must say

- That `Email_Identifier` is computed by an expression the generator does not evaluate, so the automation would
  compute it from rows the generator does not reproduce, and the **direct import** of the CSVs is the only procedure
  for which the expected results hold (`report_contains`: `Email_Identifier`, `import diretto`, the glossary's term).
- That the LEFT JOIN of `DE_Backup` is read as an inner join, one row per contact (`LEFT JOIN`).
- The entry trust check passed, with the "not verified" note and its reason (`non verificat`).

## Format notes

`table_roles`, `csv_rows`, `csv_values` are proposed keys (see `examples/link_table_path/README.md`).

## Not asserted

- The wording of the report sentences beyond the substrings above.
- Whether the report lists the `IsNot`-on-null assumption: no case's path rests on it (the N case uses `False`), and
  the spec does not say whether an assumption counts as "triggered" by the operator alone.
