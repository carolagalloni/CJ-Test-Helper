# is_not_on_null_boolean

`IsNot 'true'` on a nullable Boolean, then `IsNull` on the same field on the `IsNot` path. It checks the step 3
reading of `IsNot` (true for `False` and for null) and that null stays in the domain, so the `IsNull` branch behind
it is reachable. Builder: `examples/build_step3_structure_examples.py`, `build_is_not_on_null_boolean`.

## Journey and tables

```
SPLIT-1 "Flag not true"   DE_Master.Flag IsNot 'true' -> SPLIT-2
                          remainder                   -> UPDATE-3 "Flag true"
SPLIT-2 "Flag empty"      DE_Master.Flag IsNull       -> UPDATE-1 "Flag empty"
                          remainder                   -> UPDATE-2 "Flag false"
UPDATE-1..3  write DE_Log -> exit
```

Tables as in `one_split`: `DE_Entry`, `DE_Master` (`Flag` Boolean, nullable, no default), `DE_Log` (written only);
entry query `SELECT M.ContactId, M.Emailaddress FROM [DE_Master] M`.

## Expected answer, step by step

1. **Tables.** `DE_Entry` (entry), `DE_Master` (split and source): **2 CSVs**, both **contact**-level.
2. **Domain.** Data rules and coverage step 4: "Each field takes the values named in conditions, one extra 'other'
   value, and null when the schema allows it." For a Boolean: `True`, `False`, null.
3. **IsNot.** Spec, step 3: "`IsNot` is the negation of `Is` (on a Boolean `IsNot true` is true for `false` and for
   null, assumption: the rule 'any other operator on null is false' does not apply to a negated operator ...)".
4. **Simulation.**
   - `True`: SPLIT-1 `IsNot true` false -> remainder -> UPDATE-3.
   - `False`: SPLIT-1 true -> SPLIT-2 `IsNull` false -> remainder -> UPDATE-2.
   - null: SPLIT-1 true (the assumption) -> SPLIT-2 `IsNull` true -> `path_a` -> UPDATE-1.

   All four branches are reachable and **covered**; `SPLIT-2/path_a` is reachable because null takes the `IsNot`
   branch of SPLIT-1.
5. **The cover is 3 cases, and no fewer.** The terminal outcomes (followed by an exit with no further split) are
   `SPLIT-1/remainder_path`, `SPLIT-2/path_a` and `SPLIT-2/remainder_path`; every path ends after exactly one of
   them and all three are reachable, so at least 3 cases; the three values above give exactly 3. No scenario-level
   field: 1 scenario. **1 / 3 / 9.**
6. **The not-null preference.** "Among values that cover the same branches, the generator prefers those whose path
   rests on no unverified assumption: not null". On SPLIT-1 alone, `False` and null cover the same branch
   (`path_a`), and `False` would be preferred (as in `case_when_identifier`). Here SPLIT-2 separates them: null is
   the only value for `SPLIT-2/path_a`, so both are kept. The preference still shows in what the cover rests on:
   `SPLIT-1/path_a` is crossed by the `False` case, whose path rests on no assumption; only `SPLIT-2/path_a` depends
   on the `IsNot`-on-null assumption. If a real run disproved it, the null contacts would end at UPDATE-3 and
   `SPLIT-2/path_a` would be unreachable.
7. **CSV.** `Flag` is `True`, `False` and empty for 3 contacts each; never a cell holding a space ("A null value is
   an empty CSV cell ... the generator never writes one").

## What report.md must say

The `IsNot`-on-null assumption, because the path of the null case rests on it ("report.md lists only the
assumptions the export triggers"). `report_contains` `IsNot` is a weak check (the operator may appear elsewhere in
the report); the exact wording is not fixed by the spec.

## Format notes

`csv_values` is a proposed key (see `examples/link_table_path/README.md`).
