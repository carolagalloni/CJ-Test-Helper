# entry_copy_alias

The entry query copies `M.Tier AS Tier` from Master into the entry table. A first split reads the copy as Journey
Data (`Event.<key>.Tier`), and on each of its two paths a later split reads the source as Contact Data
(`DE_Master.Tier`), with nothing in between that writes `Tier`. The two fields must be one variable. Builder:
`examples/build_step3_structure_examples.py`, `build_entry_copy_alias`.

## Journey and tables

```
SPLIT-1 "Tier at entry"             Event.<key>.Tier Equal 'Gold'  (Journey Data)  -> SPLIT-2
                                    remainder                                      -> SPLIT-3
SPLIT-2 "Tier now, after Gold"      DE_Master.Tier Equal 'Gold'    (Contact Data)  -> UPDATE-1 "Gold confirmed"
                                    remainder                                      -> UPDATE-2 "Gold lost"
SPLIT-3 "Tier now, after not Gold"  DE_Master.Tier Equal 'Gold'    (Contact Data)  -> UPDATE-3 "Gold gained"
                                    remainder                                      -> UPDATE-4 "Never gold"
UPDATE-1..4  write DE_Log (LastAction, ActionDate) -> exit
```

```sql
SELECT M.ContactId, M.Emailaddress, M.Tier AS Tier
FROM [DE_Master] M
```

Tables: `DE_Entry` (`ContactId` PK, `Emailaddress`, `Tier` Text(20)), `DE_Master` (`ContactId` PK, `FirstName`,
`Emailaddress`, `MobilePhone`, `Tier` Text(20), nullable), `DE_Log` (written only). Contact model as in the pilot:
group -> `DE_Master` (`ContactId`) -> `DE_Entry` (`ContactId`). Automation entry.

## Expected answer, step by step

1. **Tables.** `DE_Entry` entry; `DE_Master` split (SPLIT-2, SPLIT-3) and source (FROM of the entry query);
   `DE_Log` written only, no CSV. **2 CSVs**, both **contact**-level (joined on the contact key).
2. **One variable.** Spec, step 3, "Columns the entry query derives and columns it copies": "A column of the entry
   table copied as a plain column from a source table ... holds the same value as its source: the two fields are
   **one variable** for the coverage ..., so a Journey Data condition on the copy and a Contact Data condition on the
   source read the same value, and the CSVs agree." `M.Tier AS Tier` is a plain column (the alias only names the
   entry column).
3. **No change between the reads.** Journey Data is "the snapshot taken at entry"; Contact Data is "read live when
   the contact reaches the activity", and only an Update contact changes it ("The rule 'a later split that reads the
   same field sees the updated value' applies to Contact Data only"). UPDATE-1..4 write `DE_Log` only and come after
   the splits, so SPLIT-2 and SPLIT-3 read the value written in the CSV, which is the entry value.
4. **Values.** `Tier` (one variable): `Gold` (named), one "other" value, null (nullable).
5. **Simulation.**
   - `Gold`: SPLIT-1 `path_a` -> SPLIT-2 `path_a` -> UPDATE-1.
   - other or null: SPLIT-1 remainder -> SPLIT-3 remainder -> UPDATE-4.
6. **The four branches of the second-level splits.**
   - `SPLIT-2/path_a`: **covered** (the `Gold` case).
   - `SPLIT-2/remainder_path`: only `Gold` contacts reach SPLIT-2 and they still read `Gold`: **not covered,
     unreachable**.
   - `SPLIT-3/path_a`: only non-`Gold` contacts reach SPLIT-3 and they still read non-`Gold`: **not covered,
     unreachable**.
   - `SPLIT-3/remainder_path`: **covered** (the non-`Gold` case).

   Spec, "Journey model and coverage": "Every branch ends in one of three states in the report: covered, covered with
   manual action, or not covered (unreachable, or behind an unsupported element), each with its reason." Both
   `SPLIT-1` branches are covered.
7. **Cases.** No scenario-level field: **1 scenario**. The reachable terminal outcomes are `SPLIT-2/path_a` and
   `SPLIT-3/remainder_path`, so at least 2 cases, and the two classes give exactly 2. **1 / 2 / 6.** For the second
   case the extension's preference applies ("Among values that cover the same branches, the generator prefers those
   whose path rests on no unverified assumption: not null"): `Tier` is not null and not `Gold`.
8. **CSVs agree.** `DE_Entry.Tier` = `DE_Master.Tier` on every contact ("the CSVs agree"; the entry trust check:
   "every Entry row matches what the entry query would return"), covered by `checks_pass`.

What a generator with two independent variables would do instead: four classes, `SPLIT-2/remainder_path` and
`SPLIT-3/path_a` "covered" by contacts whose entry `Tier` differs from their Master `Tier` (4 cases), and an entry CSV
that does not match the entry query: the counts, the two statuses and `checks_pass` all fail.

## What report.md must say

The two unreachable branches with their reason (spec, Outputs: "unreachable branches"); nothing specific to the
copy is required by the spec.

## Format notes

- `contact_values` and `contact_constraints` name the variable by its source field `DE_Master.Tier`. The spec does
  not say which member names the group; `docs/plan.md` (step 3, coverage.py) says the group is represented by the
  source table's field. If the generator names it by the entry copy, the key of these two cases has to change, not
  the values.
- `csv_rows` is a proposed key (see `examples/link_table_path/README.md`).

## Not asserted

- The "other" value of `Tier`.
