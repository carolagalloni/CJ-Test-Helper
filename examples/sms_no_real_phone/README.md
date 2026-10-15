# sms_no_real_phone

Same `package.json` as `sms_real_phone` (built by the same function), run **without** a real phone number. Spec,
step 3, "SMS sends": "Without a real number the SMS cannot be checked and the report says so; the send still happens
on the path."

## Expected answer, step by step

1. **Coverage** as in `sms_real_phone`: `SmsOptOut` `False` -> SMSSYNC-1 -> UPDATE-1, `True` -> UPDATE-2.
   **1 scenario, 2 cases, 6 contacts**, both branches **covered**. The `path_a` case still has
   `sms_sent` = `["SMSSYNC-1"]`: "the send still happens on the path".
2. **Phone numbers.** No real number was given, so all six contacts have the fake number `333333333333` in
   `DE_Entry.MobilePhone` and in `DE_Master.Phone` (one variable, copied by the entry query).
3. **Branch status does not change.** The section "Behaviour at the edges" says for the inbox that "those branches
   are reported as not covered", and adds "The same applies to the real phone number". Those branches are the ones
   that need what the real contact detail makes possible (an open or click in the inbox). The SMS has no branching,
   and SPLIT-1 `path_a` needs no manual action. The step 3 rule, which wins over earlier sections, asks only that
   "the report says so". So `path_a` stays **covered**.
4. **report.md** says that the SMS cannot be checked. `expected.json` checks that "SMS" appears, and one of several
   Italian wordings of "cannot be checked".

## Not asserted

- The wording of the sentence, and whether it is also a warning of the generator.
- Whether the testbook repeats it (the spec says "the report says so").
