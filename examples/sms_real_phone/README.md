# sms_real_phone

An SMS send (`SMSSYNC`) on one branch. The journey's default mobile number is the entry column `MobilePhone`, which
the entry query copies from `DE_Master.Phone`. One contact of the case that receives the SMS must get the tester's
real number, in both columns. Spec, step 3, "SMS sends" and "Columns the entry query derives and columns it copies",
and the data rule "Phone".

## Journey

```
SPLIT-1 "SMS allowed?"   DE_Master.SmsOptOut IsNot true -> SMSSYNC-1 "Reminder SMS" -> UPDATE-1 "SMS sent"
                         remainder "Opted out"          -> UPDATE-2 "SMS opted out"
```

- `defaults.mobileNumber` = `{{Event.<entry event key>."MobilePhone"}}`, written like `defaults.email`.
- Entry query: `SELECT M.ContactId, M.Emailaddress, M.Phone AS MobilePhone FROM DE_Master M` (automation entry).
- Tables: `DE_Entry` (`ContactId`, `Emailaddress`, `MobilePhone` Phone), `DE_Master` (`ContactId`, `FirstName`,
  `Emailaddress`, `Phone` Phone, `SmsOptOut` Boolean nullable), `DE_Log` (written only).
- The inner shape of `SMSSYNC` is invented: no real activity was at hand. It has `configurationArguments.assetId`
  (a literal id, no asset entity), a name, and one outcome.
- Settings: real phone number `393401234567`, fake number at its default `333333333333`. The same package without a
  real number is `sms_no_real_phone`.

## Expected answer, step by step

1. **SPLIT-1.** Spec: "`IsNot` is the negation of `Is` (on a Boolean `IsNot true` is true for `false` and for null,
   assumption ...)". `path_a` is taken by `False` and by null. The remainder is taken only by `True`. The `path_a`
   case gets **`False`**: null rests on the `IsNot` assumption, and "the generator prefers those whose path rests on
   no unverified assumption: not null". The remainder case gets **`True`**. **1 scenario, 2 cases, 6 contacts**, both
   branches **covered**.
2. **The SMS.** "`SMSSYNC` is a send to the phone number of the journey's default mobile number
   (`defaults.mobileNumber`, a column of the entry table ...). It has no branching." The `path_a` case sends
   SMSSYNC-1 (`sms_sent` = `["SMSSYNC-1"]`). The remainder case sends nothing.
3. **Who gets the real number.** "the spec rule 'in each case whose path sends a message to a phone, one of the 3
   contacts gets the tester's real number' applies: that contact's row holds the real number in the default mobile
   column (and in every column the entry query copies it from)". In the `path_a` case, one contact has
   `393401234567` in `DE_Entry.MobilePhone` **and** in `DE_Master.Phone`. "A column of the entry table copied as a
   plain column from a source table ... holds the same value as its source: the two fields are one variable ... and
   the CSVs agree." The other two contacts have the fake number in both columns: "All other contacts get a clearly
   fake number". The three contacts of the remainder case have the fake number in both columns.
4. **Trust checks.** Every entry row is what the entry query returns, so `MobilePhone` = `Phone` on every row, and
   the checks pass.

## What the testbook and report.md must say

- The testbook's expected result names the SMS and the contact that receives it: "the testbook's expected result
  names the SMS and the contact that receives it". `expected.json` checks for the SMS name or key. The contact is the
  one with the real number. Which contact key that is depends on the order of the cases, so it is not checked.

## Not asserted

- Which of the three contacts gets the real number. The spec says only "one of the 3 contacts". The runner compares
  `contact_rows` in contact-key order, and the expected rows put the real number on the first contact, as
  `docs/plan.md` (datagen) does. If the generator gives it to another contact, that is not a spec violation: reorder
  the rows rather than change the generator.
- The key of the receiving contact in the testbook.
