# left_join_source_row

The entry query LEFT JOINs a score table on the contact key and copies its `Score` into the entry table; a Journey
Data split reads the copy with `GreaterThan 10`. Builder: `examples/build_step3_structure_examples.py`,
`build_left_join_source_row`.

## Journey and tables

```
SPLIT-1 "Score above 10"   Event.<key>.Score GreaterThan 10 -> UPDATE-1 "High score"
                           remainder                        -> UPDATE-2 "Low score"
```

```sql
SELECT M.ContactId, M.Emailaddress, S.Score
FROM [DE_Master] M
LEFT JOIN [DE_Score] S ON M.ContactId = S.ContactId
```

Tables: `DE_Entry` (`ContactId` PK, `Emailaddress`, `Score` Number nullable), `DE_Master` (`ContactId` PK,
`FirstName`, `Emailaddress`, `MobilePhone`), `DE_Score` (`ContactId` PK, `Score` Number nullable), `DE_Log` (written
only). Automation entry; `DE_Score` is not in Contact Builder.

## Expected answer, step by step

1. **Tables.** `DE_Score` is in a JOIN of the entry query: source table, CSV. Spec, step 3: "A `LEFT JOIN` source
   still gets one row per contact (read as an inner join, as before); the report says so." **3 CSVs**, all
   **contact**-level (`DE_Score` joins on `ContactId`, the contact key); **one `DE_Score` row per contact** (6 rows,
   `has_row` true for both cases).
2. **One variable.** `S.Score` is copied as a plain column: "the two fields are **one variable** for the coverage
   ..., so a Journey Data condition on the copy and a Contact Data condition on the source read the same value, and
   the CSVs agree." The split reads `DE_Entry.Score`; the value is written in both `DE_Score.Score` and
   `DE_Entry.Score`.
3. **Values.** "Coverage values are chosen strictly inside the ranges the conditions define, never on a bound." For
   `GreaterThan 10`: a number **> 10** (`path_a`), a number **< 10**, and null (nullable). The remainder class holds
   the numbers <= 10 and null.
4. **Null is not needed.** "Among values that cover the same branches, the generator prefers those whose path rests
   on no unverified assumption: not null, row present." A number < 10 and null take the same branch, so the
   remainder case has a number < 10 (never 10, the bound). The "row absent" alternative does not exist here: "No such
   variable for tables the entry query joins".
5. **Cases.** 1 scenario, 2 cases (> 10 -> UPDATE-1, < 10 -> UPDATE-2): **1 / 2 / 6**; both branches **covered**.
6. **Entry trust check.** "The trust check on the entry query reports a column it cannot verify (... a LEFT JOIN read
   as an inner join) as 'not verified', with the reason, and fails only on what it can verify": it **passes**, with a
   "not verified" note naming the LEFT JOIN.

## What report.md must say

- That the LEFT JOIN of `DE_Score` is read as an inner join, one row per contact (`LEFT JOIN`, `DE_Score`).
- The entry trust check passed with the "not verified" note (`non verificat`).

## Format notes

- `contact_constraints` names the variable by its source field `DE_Score.Score`. The spec does not say which member
  names the group; `docs/plan.md` (step 3, coverage.py) says the source table's field. If the generator names it by
  the entry copy, the key changes, not the constraints.
- `table_roles` and `csv_rows` are proposed keys (see `examples/link_table_path/README.md`).

## Not asserted

- The exact numbers (any value strictly above / below 10 is right).
- That `DE_Entry.Score` equals `DE_Score.Score` row by row: covered by `checks_pass` (the entry trust check).
