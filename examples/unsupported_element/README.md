# unsupported_element

An activity whose type is not in the spec's supported list (`PUSHNOTIFICATIONACTIVITY`, a plausible name for a
push notification step) in the middle of a path, followed by a split. The generator must not guess what happens
behind it. (Until step 3 this example used `SMSSYNC`; the spec section "Extensions for time-dependent journeys"
now supports it as an SMS send, so a type the spec does not name took its place.)

## Journey

```
SPLIT-1 "Mail offers"                   (DE_Master.IsMailOffers, Boolean, nullable)
  path_a          IsMailOffers Is true -> PUSH-1 "Send push notification" (type PUSHNOTIFICATIONACTIVITY) -> SPLIT-2
  remainder_path                       -> UPDATE-1 "No offers" -> exit

SPLIT-2 "Phone consent"                 (DE_Master.PhoneConsent, Boolean, nullable)
  path_a          PhoneConsent Is true -> UPDATE-2 "SMS consent"    -> exit
  remainder_path                       -> UPDATE-3 "No SMS consent" -> exit
```

## Expected answer, step by step

1. **`PUSH-1` is unsupported.** Spec, "Supported in the PoC": "Activity types | Decision split,
   engagement split, random split, wait by duration, wait by attribute, send-time wait, email, update
   contact", and the step 3 section adds `SMSSYNC` ("SMS sends"). `PUSHNOTIFICATIONACTIVITY` is none of these.
   Spec: "Anything else is not an error: it is listed in the report, and the branches behind it are marked as
   not covered." -> `unsupported: ["PUSH-1"]`.
2. **Behind it: SPLIT-2, both outcomes not covered**, with a reason mentioning the unsupported element:
   spec "not covered (unreachable, or behind an unsupported element), each with its reason" ->
   `reason_contains: "unsupported"`.
3. **Before it: SPLIT-1 `path_a` covered.** The branch itself is reachable (IsMailOffers `True`); the
   contact crosses it and then stops at `PUSH-1`. Spec, trust checks: "Every reachable branch is crossed
   by at least one case". `SPLIT-1/remainder_path` covered as usual.
4. **Cases**: IsMailOffers `True` (path stops at the unsupported activity) and not `True` (UPDATE-1).
   **1 scenario, 2 cases, 6 contacts.** `PhoneConsent` is still a field of a split table and gets a value,
   but no case depends on it.
5. **Tables**: `DE_Master` is read by the splits (both, including SPLIT-2 behind the unsupported element),
   so `DE_Entry` and `DE_Master` get a CSV as usual.

## Not asserted

- The first case's `end_activity`: the last activity is `PUSH-1` if the simulation enters the
  unsupported activity before stopping, or `SPLIT-1` if it stops before it. The spec does not say which.
