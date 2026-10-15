# managed_source_table

A source table of the entry query is a data extension synchronized from the CRM, which Package Manager marks as
managed (`NO_DEPLOY`, `ManagedDataExtension`). Its CSV is imported like every other one (see "Team procedure"
below). Builder: `examples/build_step3_structure_examples.py`, `build_managed_source_table`.

## Journey and tables

```
SPLIT-1 "Permission"   DE_Master.Perm Is true -> UPDATE-1 "Allowed"
                       remainder              -> UPDATE-2 "Not allowed"
```

```sql
SELECT M.ContactId, M.Emailaddress
FROM [DE_Master] M
JOIN [CampaignMember_Salesforce_3] CM ON M.ContactId = CM.ContactId
WHERE CM.Status = 'Responded'
```

Tables: `DE_Entry` (`ContactId` PK, `Emailaddress`), `DE_Master` (`ContactId` PK, `FirstName`, `Emailaddress`,
`MobilePhone`, `Perm` Boolean nullable), `CampaignMember_Salesforce_3` (`Id` Text(18) PK, `ContactId` Text(18),
`Status` Text(40)), `DE_Log` (written only). Automation entry. The managed table's entity carries

```json
"issues": [{"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "sourceJobType": "PACKAGE",
            "functionality": "NO_DEPLOY", "objectType": "ManagedDataExtension"}]
```

## Expected answer, step by step

1. **Selected, with a CSV.** Spec, step 3, "Synchronized data extensions": a source table that Package Manager
   marks as a managed data extension (`NO_DEPLOY`, `ManagedDataExtension` ...) "is imported like every other CSV: its
   CSV is produced and is among the files to import, in the report and in the testbook. The report notes that the
   table is synchronized from the CRM ..., so the tester knows the rows will also arrive from the sync." **3 CSVs**;
   the managed table has the role source and is **contact**-level (joined on `ContactId`, the contact key).
2. **Its rows.** The rows the entry query would need: one per contact, `ContactId` = the contact key (data rule 1),
   `Status` = **`Responded`** (data rule 3, "A value that passes the filter"). `Id` is its primary key, unique in
   the table ("Primary keys are unique within each table"; value not asserted). 6 rows.
3. **The issue is listed.** Spec, "Data sources of a condition": "Package Manager `issues` of level warning or error
   are listed in the report." The issue is level `WARNING`.
4. **Coverage.** `Perm` `True` -> UPDATE-1; `False` (preferred over null) -> UPDATE-2. **1 / 2 / 6**, both branches
   **covered**. The managed table changes nothing in the coverage; the trust checks run on all three CSVs as usual
   and pass.

## What report.md must say

- The table, in the import list of "Come eseguire i test" like the other CSVs, with the note that it is a data
  extension synchronized from the CRM and that its rows will also arrive from the sync (`report_contains`: the note,
  and the "Importa prima ..." sentence with the managed CSV in it; `report_not_contains`: "non importabile",
  "Non importare", "non si può importare").
- The Package Manager issue (`NO_DEPLOY`, `ManagedDataExtension` or its type `UNSUPPORTED_FUNCTIONALITY`, as accepted
  in `cloudpages_entry`).
- The same in the testbook's Pre conditions: the managed CSV in "Importa questi file CSV: ..." and the note
  (`testbook_contains`).

## Format notes

`table_roles`, `csv_rows`, `csv_values` are proposed keys (see `examples/link_table_path/README.md`).

## Not asserted

- The wording of the loader warning in "Lettura dell'export" (now: "... le sue righe arrivano anche dalla sincronizzazione.").
- Whether the report lists the managed table among the assumptions: it does not, the procedure is confirmed.

## Team procedure (proof for the change of 14 October 2026)

The first version of this example expected the managed table's CSV to be left out of the files to import (spec
assumption: "the team confirms the procedure"). The team answered on 14 October 2026: "se serve all'interno del
customer journey deve anche avere un import". A synchronized data extension that the journey uses is therefore
imported like every other CSV. The generator keeps producing the CSV, puts it in the import list of report.md and of
the testbook's Pre conditions, and only notes that the table is synchronized from the CRM, so the tester knows its
rows will also arrive from the sync. `docs/assumptions.md` row 57 is now a confirmed fact, not an assumption.
