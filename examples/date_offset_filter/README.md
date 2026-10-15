# date_offset_filter

The entry query keeps only the rows whose date equals the current date moved by a number of days, written in the
two spellings the spec names: `CAST(col AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)` on a joined source
table, and `CAST(col AS DATE) = DATEADD(day, -2, CAST(GETDATE() AS DATE))` on Master. One trivial split. Builder:
`examples/build_step3_structure_examples.py`, `build_date_offset_filter`.

## Journey and tables

```
SPLIT-1 "Permission"   DE_Master.Perm Is true -> UPDATE-1 "Allowed"
                       remainder              -> UPDATE-2 "Not allowed"
```

```sql
SELECT M.ContactId, M.Emailaddress
FROM [DE_Master] M
JOIN [DE_Member] CM ON M.ContactId = CM.ContactId
WHERE CAST(CM.CreatedDate AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)
AND CAST(M.LastVisit AS DATE) = DATEADD(day, -2, CAST(GETDATE() AS DATE))
```

Tables: `DE_Entry` (`ContactId` PK, `Emailaddress`), `DE_Master` (`ContactId` PK, `FirstName`, `Emailaddress`,
`MobilePhone`, `Perm` Boolean nullable, `LastVisit` Date nullable), `DE_Member` (`ContactId` PK, `CreatedDate` Date
not nullable), `DE_Log` (written only). Automation entry. The runner generates at `2026-10-11 12:00`.

## Expected answer, step by step

1. **Tables.** `DE_Member` is in a JOIN of the entry query: source table, CSV. **3 CSVs**, all **contact**-level
   (`DE_Member` joins Master on `ContactId`, the contact key).
2. **The filters are understood.** Spec, step 3, "Columns the entry query derives and columns it copies": "A filter
   comparing the date part of a column with the current date moved by a number of days
   (`CAST(CM.CreatedDate AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)`) is understood: the column takes the
   generation time moved by that number of days. The spelling with the current date cast first
   (`DATEADD(day, -2, CAST(GETDATE() AS DATE))`) is the same filter."
3. **Values.** Data rule 3 ("Field filtered by the entry query | A value that passes the filter") with the rule
   above: **`DE_Member.CreatedDate` = `2026-10-10 12:00`** (generation time - 1 day) and **`DE_Master.LastVisit` =
   `2026-10-09 12:00`** (generation time - 2 days) in every row, in the CSV date format `YYYY-MM-DD HH:MM`. Their date
   parts equal `GETDATE()` - 1 and - 2 days at generation time, so every contact passes the WHERE.
4. **Coverage.** `Perm` (nullable Boolean): `True` -> UPDATE-1; `False` or null -> UPDATE-2, and the "not null"
   preference picks **`False`**. **1 / 2 / 6**, both branches **covered**. The two date columns are not read by a
   split: they are not coverage variables and the data is not perishable in the sense of "Validity of the data"
   (which counts the days "before any split reads it differently").
5. **Entry trust check.** "every Entry row matches what the entry query would return": the filters are understood,
   so the check verifies them on the date part and **passes** (`checks_pass`). Since nothing is "not verified", no
   warning quotes the filter text as not understood (`warnings_not_contain` `DATEADD`).

## What report.md must say

Nothing specific: the filters are understood (no placeholder, no "not understood" warning, no "not verified" note
about them).

## Format notes

`table_roles`, `csv_rows`, `csv_values` are proposed keys (see `examples/link_table_path/README.md`).

## Not asserted

- Whether report.md mentions that the dates are relative to the generation time (useful only if the tester ran the
  automation on another day; the procedure is the direct import of the entry CSV).
