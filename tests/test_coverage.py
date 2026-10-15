from datetime import datetime

from cjhelper import coverage
from cjhelper.model import (
    LEVEL_CONTACT, LEVEL_SCENARIO, STATUS_COVERED, STATUS_MANUAL, STATUS_NOT_COVERED, Settings,
)
from tests.helpers import (
    AND, OR, cond, decision, email, email_asset, engagement, field, journey, package, random_split, table,
    table_model, unsupported, update, wait_attr,
)

NOW = datetime(2026, 10, 11, 12, 0)

ENTRY = table("DE_Entry", [field("ContactId", pk=True, nullable=False), field("CampaignName", pk=True, nullable=False)],
              sendable_field="ContactId")
MASTER = table("DE_Master", [field("ContactId", pk=True, nullable=False), field("Perm", "Boolean"),
                             field("Dunning", "Boolean"), field("Consent", "Boolean"), field("Email", "EmailAddress")],
               sendable_field="ContactId")
CONFIG = table("DE_Config", [field("CampaignName", pk=True, nullable=False), field("Channel", "Text", nullable=False),
                             field("Channel2", "Text"), field("CG", "Boolean", nullable=False), field("WAIT1", "Date", nullable=False)])


def model_for(tables, scenario=False):
    levels = {"DE_Entry": LEVEL_CONTACT, "DE_Master": LEVEL_CONTACT}
    groups = [("contact", [("DE_Entry", "ContactId"), ("DE_Master", "ContactId")])]
    if scenario:
        levels["DE_Config"] = LEVEL_SCENARIO
        groups.append(("scenario", [("DE_Entry", "CampaignName"), ("DE_Config", "CampaignName")]))
    return table_model("DE_Entry", "ContactId", levels, groups, tables)


def run(acts, tables, scenario=False, settings=None, emails=None, unsupported_keys=()):
    j = journey(acts)
    pkg = package(j, tables, emails, unsupported_keys)
    tm = model_for(tables, scenario)
    return coverage.compute_coverage(pkg, tm, settings or Settings(), NOW)


def status(result, activity, outcome):
    for b in result.branches:
        if b.activity_key == activity and b.outcome_key == outcome:
            return b
    raise AssertionError(f"no branch {activity}/{outcome}")


def test_one_boolean_split_gives_two_cases_in_one_scenario():
    acts = [decision("SPLIT-1", "perm", [("yes", "true", "UPD-Y", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-N"),
            update("UPD-Y", "Y", [], None), update("UPD-N", "N", [], None)]
    r = run(acts, [ENTRY, MASTER])
    assert r.stats["scenarios"] == 1 and r.stats["cases"] == 2 and r.stats["contacts"] == 6
    assert status(r, "SPLIT-1", "yes").status == STATUS_COVERED
    assert status(r, "SPLIT-1", "remainder_path").status == STATUS_COVERED
    values = sorted(c.contact_values[("DE_Master", "Perm")] for c in r.cases)
    assert values == ["False", "True"]
    assert r.scenario_fields == [] and r.contact_fields == [("DE_Master", "Perm")]
    assert all(c.id.startswith("S01-C") for c in r.cases)


def test_contact_classes_group_combinations_by_predicate_truth():
    f = AND(cond("DE_Master", "Perm", "Is", "true"), OR(cond("DE_Master", "Dunning", "Is", "false"), cond("DE_Master", "Dunning", "IsNull")),
            cond("DE_Master", "Consent", "Is", "true"))
    acts = [decision("SPLIT-1", "phone", [("yes", "ok", None, f)], remainder_next=None)]
    r = run(acts, [ENTRY, MASTER])
    # 3 nullable booleans = 27 combinations, one predicate = 2 classes
    assert len(r.classes) == 2
    assert sum(c.member_count for c in r.classes) == 27
    assert r.stats["cases"] == 2
    # the representative of the true class prefers the condition values and non-null
    true_case = next(c for c in r.cases if c.end_activity_key == "SPLIT-1" and c.steps[-1].outcome_key == "yes")
    assert true_case.contact_values == {("DE_Master", "Perm"): "True", ("DE_Master", "Dunning"): "False", ("DE_Master", "Consent"): "True"}


def test_scenario_level_and_contact_level_fields():
    acts = [
        decision("SPLIT-1", "Channel", [("email", "Email", "SPLIT-2", cond("DE_Config", "Channel", "Equal", "Email")),
                                        ("dm", "DM", "UPD-DM", cond("DE_Config", "Channel", "Equal", "DM"))], remainder_next=None),
        decision("SPLIT-2", "perm", [("ok", "ok", "UPD-E", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-X"),
        update("UPD-DM", "DM", [], None), update("UPD-E", "E", [], None), update("UPD-X", "X", [], None),
    ]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    assert r.scenario_fields == [("DE_Config", "Channel")]
    assert r.domains[("DE_Config", "Channel")] == ["Email", "DM", "OTHER"]   # not nullable: no null
    assert r.stats["scenarios"] == 3 and r.stats["cases"] == 4
    assert all(b.status == STATUS_COVERED for b in r.branches)
    chans = sorted(s.values[("DE_Config", "Channel")] for s in r.scenarios)
    assert chans == ["DM", "Email", "OTHER"]
    assert [s.run_order for s in r.scenarios] == [1, 2, 3]


def test_repeated_split_on_same_field_marks_contradiction_unreachable():
    acts = [
        decision("SPLIT-1", "c1", [("a", "A", "SPLIT-2", cond("DE_Config", "Channel", "Equal", "A"))], remainder_next="UPD-R"),
        decision("SPLIT-2", "c2", [("b", "B", "UPD-B", cond("DE_Config", "Channel", "Equal", "B"))], remainder_next="UPD-R2"),
        update("UPD-R", "R", [], None), update("UPD-B", "B", [], None), update("UPD-R2", "R2", [], None),
    ]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    b = status(r, "SPLIT-2", "b")
    assert b.status == STATUS_NOT_COVERED
    assert "non è mai vera" in b.reason and "'A'" in b.reason
    assert status(r, "SPLIT-2", "remainder_path").status == STATUS_COVERED
    assert r.stats["scenarios"] == 2


def test_case_mismatch_is_flagged_and_setting_changes_reachability():
    acts = [
        decision("SPLIT-1", "c1", [("a", "Email", "SPLIT-2", cond("DE_Config", "Channel", "Equal", "Email"))], remainder_next=None),
        decision("SPLIT-2", "c2", [("b", "EMAIL", "UPD-B", cond("DE_Config", "Channel", "Equal", "EMAIL"))], remainder_next="UPD-R"),
        update("UPD-B", "B", [], None), update("UPD-R", "R", [], None),
    ]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    assert status(r, "SPLIT-2", "b").status == STATUS_NOT_COVERED
    assert any("'Email' e 'EMAIL'" in w and "perché i confronti di testo distinguono maiuscole e minuscole" in w
               for w in r.warnings)
    r2 = run(acts, [ENTRY, MASTER, CONFIG], scenario=True, settings=Settings(case_insensitive_text=True))
    assert status(r2, "SPLIT-2", "b").status == STATUS_COVERED
    assert status(r2, "SPLIT-2", "remainder_path").status == STATUS_NOT_COVERED
    assert r2.domains[("DE_Config", "Channel")] == ["Email", "OTHER"]


def engagement_journey(email_always_sent: bool, has_link=False):
    emails = {"A1": email_asset("A1", "Demo", has_link=has_link)}
    if email_always_sent:
        first = email("EMAIL-1", "Demo", "A1", "WAIT-1")
    else:
        first = decision("SPLIT-0", "send?", [("s", "send", "EMAIL-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="WAIT-1")
    acts = [first,
            email("EMAIL-1", "Demo", "A1", "UPD-Z") if not email_always_sent else wait_attr("WAIT-1", "DE_Config", "WAIT1", "ENG-1"),
            wait_attr("WAIT-1", "DE_Config", "WAIT1", "ENG-1") if not email_always_sent else None,
            engagement("ENG-1", "Open Demo", "open", "EMAIL-1", "ENG-2", "UPD-N"),
            engagement("ENG-2", "Click Demo", "click", "EMAIL-1", "UPD-C", "UPD-NC"),
            update("UPD-N", "N", [], None), update("UPD-C", "C", [], None), update("UPD-NC", "NC", [], None),
            update("UPD-Z", "Z", [], None)]
    acts = [a for a in acts if a is not None]
    return [a for a in acts if a is not None], emails


def test_engagement_split_with_email_sent():
    acts, emails = engagement_journey(True)
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True, emails=emails, settings=Settings(real_inbox="t@example.com"))
    assert status(r, "ENG-1", "branchResult-1").status == STATUS_MANUAL
    assert status(r, "ENG-1", "branchResult-2").status == STATUS_COVERED
    click_yes = status(r, "ENG-2", "branchResult-1")
    assert click_yes.status == STATUS_NOT_COVERED and "non contiene link" in click_yes.reason
    assert status(r, "ENG-2", "branchResult-2").status == STATUS_MANUAL
    manual_cases = [c for c in r.cases if c.manual_actions]
    assert len(manual_cases) == 1 and manual_cases[0].manual_actions[0].kind == "open"
    assert manual_cases[0].waits == ["WAIT-1"]
    assert r.scenarios[0].has_manual_action   # manual scenarios come first
    # without an inbox the same cases exist but the branches are not covered
    r2 = run(acts, [ENTRY, MASTER, CONFIG], scenario=True, emails=emails)
    assert status(r2, "ENG-1", "branchResult-1").status == STATUS_NOT_COVERED
    assert "inbox" in status(r2, "ENG-1", "branchResult-1").reason
    assert len([c for c in r2.cases if c.manual_actions]) == 1


def test_engagement_split_with_email_never_sent_on_its_paths():
    acts, emails = engagement_journey(False)
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True, emails=emails, settings=Settings(real_inbox="t@example.com"))
    yes = status(r, "ENG-1", "branchResult-1")
    assert yes.status == STATUS_NOT_COVERED and "non viene inviata" in yes.reason and "Demo" in yes.reason
    assert status(r, "ENG-1", "branchResult-2").status == STATUS_COVERED
    assert status(r, "ENG-2", "branchResult-1").status == STATUS_NOT_COVERED
    assert status(r, "ENG-2", "branchResult-2").status == STATUS_NOT_COVERED
    assert "non raggiungibile" in status(r, "ENG-2", "branchResult-2").reason


def test_random_split_gets_a_statistical_case():
    acts = [
        decision("SPLIT-1", "cg", [("cg", "true", "RANDOM-1", cond("DE_Config", "CG", "Is", "true"))], remainder_next="SPLIT-2"),
        random_split("RANDOM-1", [("branchResult-1", 90, "SPLIT-2"), ("branchResult-2", 10, "UPD-CG")]),
        decision("SPLIT-2", "perm", [("ok", "ok", "UPD-Y", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-N"),
        update("UPD-CG", "CG", [], None), update("UPD-Y", "Y", [], None), update("UPD-N", "N", [], None),
    ]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    stat = [s for s in r.scenarios if s.is_statistical]
    assert len(stat) == 1 and stat[0].values[("DE_Config", "CG")] == "True"
    case = stat[0].cases[0]
    assert case.contact_count == 50 and case.random is not None
    assert case.random.expected_low == 1 and case.random.expected_high == 9
    assert [b.outcome_key for b in case.random.branches] == ["branchResult-1", "branchResult-2"]
    assert case.steps[-1].activity_key == "RANDOM-1"
    assert stat[0].run_order == len(r.scenarios)   # last in the run order
    for key in ("branchResult-1", "branchResult-2"):
        b = status(r, "RANDOM-1", key)
        assert b.status == STATUS_COVERED and b.statistical
    # the branch before the random split is crossed by all 50 contacts: covered, not statistical
    cg = status(r, "SPLIT-1", "cg")
    assert cg.status == STATUS_COVERED and not cg.statistical and cg.covered_by == [stat[0].cases[0].id]
    assert not status(r, "SPLIT-2", "ok").statistical
    det = [s for s in r.scenarios if not s.is_statistical]
    assert len(det) == 1 and det[0].values[("DE_Config", "CG")] == "False"
    assert r.stats["contacts"] == 6 + 50


def test_unsupported_activity_blocks_what_is_behind_it():
    acts = [
        decision("SPLIT-1", "perm", [("ok", "ok", "X-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-N"),
        unsupported("X-1", "SMSSYNC", "SPLIT-2"),
        decision("SPLIT-2", "later", [("a", "A", None, cond("DE_Master", "Consent", "Is", "true"))], remainder_next=None),
        update("UPD-N", "N", [], None),
    ]
    r = run(acts, [ENTRY, MASTER], unsupported_keys=["X-1"])
    assert status(r, "SPLIT-1", "ok").status == STATUS_COVERED
    b = status(r, "SPLIT-2", "a")
    assert b.status == STATUS_NOT_COVERED and "non supportat" in b.reason and "X-1" in b.reason
    assert r.unsupported == ["X-1"]


def test_update_before_split_is_tracked():
    acts = [
        update("UPD-1", "set", [("DE_Master", "Perm", "True")], "SPLIT-1"),
        decision("SPLIT-1", "perm", [("ok", "ok", "UPD-Y", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-N"),
        update("UPD-Y", "Y", [], None), update("UPD-N", "N", [], None),
    ]
    r = run(acts, [ENTRY, MASTER])
    assert status(r, "SPLIT-1", "ok").status == STATUS_COVERED
    b = status(r, "SPLIT-1", "remainder_path")
    assert b.status == STATUS_NOT_COVERED and "mai" in b.reason.lower() or "sempre" in b.reason
    assert r.stats["cases"] == 1 and r.cases[0].writes[0].value == "True"


def test_same_input_gives_same_result():
    acts = [
        decision("SPLIT-1", "Channel", [("email", "Email", "SPLIT-2", cond("DE_Config", "Channel", "Equal", "Email")),
                                        ("dm", "DM", "UPD-DM", cond("DE_Config", "Channel", "Equal", "DM"))], remainder_next=None),
        decision("SPLIT-2", "perm", [("ok", "ok", "UPD-E", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-X"),
        update("UPD-DM", "DM", [], None), update("UPD-E", "E", [], None), update("UPD-X", "X", [], None),
    ]
    a = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    b = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    assert [(s.id, s.values, [(c.id, c.contact_values, c.steps) for c in s.cases]) for s in a.scenarios] == \
           [(s.id, s.values, [(c.id, c.contact_values, c.steps) for c in s.cases]) for s in b.scenarios]


def test_expected_range():
    assert coverage.expected_range(50, 0.10) == (1, 9)
    assert coverage.expected_range(50, 0.50) == (18, 32)
    assert coverage.expected_range(0, 0.1) == (0, 0)


def test_build_domain_rules():
    fmt = "%Y-%m-%d %H:%M"
    c = lambda op, v, t="DE_Master", f="F": cond(t, f, op, v)
    assert coverage.build_domain("Boolean", [c("Is", "false")], True, False, fmt)[0] == ["False", "True", None]
    assert coverage.build_domain("Text", [c("Equal", "A"), c("Equal", "a"), c("Equal", "B")], False, False, fmt)[0] == ["A", "a", "B", "OTHER"]
    assert coverage.build_domain("Text", [c("Equal", "A"), c("Equal", "a"), c("Equal", "B")], True, True, fmt)[0] == ["A", "B", "OTHER", None]
    assert coverage.build_domain("Text", [c("Equal", "OTHER")], False, False, fmt)[0] == ["OTHER", "X"]
    assert coverage.build_domain("Number", [c("Equal", "3")], False, False, fmt)[0] == ["3", "4"]
    assert coverage.build_domain("Date", [c("Equal", "2026-01-01 00:00")], False, False, fmt)[0] == ["2026-01-01 00:00", "2026-01-02 00:00"]
    # the 'other' value fits the field length; a condition value that does not fit is left out with a warning
    domain, other, warnings = coverage.build_domain("Text", [c("Equal", "IT"), c("Equal", "ITA")], True, False, fmt, length=2)
    assert domain == ["IT", "X", None] and other == "X" and any("'ITA'" in w for w in warnings)
    domain, other, _ = coverage.build_domain("Text", [c("Equal", "Y")], False, False, fmt, length=1)
    assert domain == ["Y", "X"]
    # ordering operators get one value inside each range, never the bound itself (spec, "Extensions beyond
    # the pilot": "Coverage values are chosen strictly inside the ranges the conditions define, never on a
    # bound"; this replaces the earlier expectation that the bound 50 was tried)
    assert coverage.build_domain("Number", [c("LessThan", "50")], False, False, fmt)[0] == ["49", "51"]
    assert coverage.build_domain("Date", [c("GreaterThan", "2026-01-01 00:00")], False, False, fmt)[0] == ["2025-12-31 00:00", "2026-01-02 00:00"]
    # email and phone fields get an 'other' value of their own kind
    assert coverage.build_domain("EmailAddress", [c("IsNull", None)], True, False, fmt)[0] == ["other@example.invalid", None]
    assert coverage.build_domain("Phone", [c("IsNull", None)], True, False, fmt)[0] == ["000000000000", None]
    # a Contains condition: the other value must not contain the text
    domain, other, _ = coverage.build_domain("Text", [c("Contains", "OTH")], False, False, fmt)
    assert other == "X"


def test_unsupported_outcome_keeps_the_earlier_outcomes_coverable():
    acts = [decision("SPLIT-1", "s", [("a", "A", "UPD-A", cond("DE_Master", "Perm", "Is", "true")),
                                      ("b", "B", "UPD-B", cond("DE_Master", "Consent", "Between", "x"))], remainder_next="UPD-R"),
            update("UPD-A", "A", [], None), update("UPD-B", "B", [], None), update("UPD-R", "R", [], None)]
    acts[0].outcomes[1].unsupported_reason = "outcome 'B' uses the operator 'Between', which the PoC does not support"
    r = run(acts, [ENTRY, MASTER])
    assert status(r, "SPLIT-1", "a").status == STATUS_COVERED
    b = status(r, "SPLIT-1", "b")
    assert b.status == STATUS_NOT_COVERED and b.reason.startswith("branch non supportato") and "Between" in b.reason
    rem = status(r, "SPLIT-1", "remainder_path")
    assert rem.status == STATUS_NOT_COVERED and rem.reason.startswith("a valle di un elemento non supportato")
    assert "SPLIT-1/b" in r.unsupported
    assert r.stats["cases"] == 1   # only the A case: the path stopped at the unsupported outcome covers nothing


def test_branches_behind_an_unsupported_activity_name_it():
    acts = [
        decision("SPLIT-1", "perm", [("ok", "ok", "X-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="X-2"),
        unsupported("X-1", "SMSSYNC", "SPLIT-2"), unsupported("X-2", "WHATSAPP", "SPLIT-3"),
        decision("SPLIT-2", "later", [("a", "A", None, cond("DE_Master", "Consent", "Is", "true"))], remainder_next=None),
        decision("SPLIT-3", "later2", [("a", "A", None, cond("DE_Master", "Consent", "Is", "true"))], remainder_next=None),
    ]
    r = run(acts, [ENTRY, MASTER], unsupported_keys=["X-1", "X-2"])
    b2 = status(r, "SPLIT-2", "a")
    assert b2.status == STATUS_NOT_COVERED and b2.reason.startswith("a valle di un elemento non supportato") and "X-1" in b2.reason and "X-2" not in b2.reason
    b3 = status(r, "SPLIT-3", "a")
    assert "X-2" in b3.reason and "X-1" not in b3.reason


def test_manual_action_cannot_be_done_when_a_split_fixes_the_address():
    """Data rule 2 (split value) wins over rule 4 (real inbox): the open contacts must carry the
    address the split requires, which the tester does not own, so the Yes branch is not covered."""
    emails = {"A1": email_asset("A1", "Demo")}
    acts = [decision("SPLIT-1", "vip?", [("vip", "vip", "EMAIL-1", cond("DE_Master", "Email", "Equal", "vip@example.com"))], remainder_next="UPD-R"),
            email("EMAIL-1", "Demo", "A1", "WAIT-1"), wait_attr("WAIT-1", "DE_Config", "WAIT1", "ENG-1"),
            engagement("ENG-1", "Open Demo", "open", "EMAIL-1", "UPD-Y", "UPD-N"),
            update("UPD-Y", "Y", [], None), update("UPD-N", "N", [], None), update("UPD-R", "R", [], None)]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True, emails=emails, settings=Settings(real_inbox="t@example.com"))
    yes = status(r, "ENG-1", "branchResult-1")
    assert yes.status == STATUS_NOT_COVERED and "vip@example.com" in yes.reason and yes.covered_by
    assert status(r, "ENG-1", "branchResult-2").status == STATUS_COVERED
    blocked = [c for c in r.cases if c.manual_actions]
    assert len(blocked) == 1 and blocked[0].inbox_blocked_reason and "vip@example.com" in blocked[0].inbox_blocked_reason


def test_engagement_split_of_unknown_kind_reports_both_branches_not_covered():
    """Triage RB-5: a statsTypeId that is neither an open nor a click measures something unknown, so
    neither branch can be claimed, as the loader warning says; the split before it stays covered."""
    emails = {"A1": email_asset("A1", "Demo", has_link=True)}
    acts = [decision("SPLIT-1", "perm", [("ok", "ok", "EMAIL-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-R"),
            email("EMAIL-1", "Demo", "A1", "ENG-1"),
            engagement("ENG-1", "Bounce Demo", None, "EMAIL-1", "UPD-Y", "UPD-N"),
            update("UPD-Y", "Y", [], None), update("UPD-N", "N", [], None), update("UPD-R", "R", [], None)]
    r = run(acts, [ENTRY, MASTER], emails=emails, settings=Settings(real_inbox="t@example.com"))
    for key in ("branchResult-1", "branchResult-2"):
        b = status(r, "ENG-1", key)
        assert b.status == STATUS_NOT_COVERED and b.covered_by == []
        assert b.reason.startswith("branch non supportato") and "né un open né un click" in b.reason
    assert status(r, "SPLIT-1", "ok").status == STATUS_COVERED
    assert status(r, "SPLIT-1", "remainder_path").status == STATUS_COVERED
    assert r.stats["not_covered"] == 2


# ---------------------------------------------------------------------------
# Spec, "Extensions beyond the pilot"
# ---------------------------------------------------------------------------

from cjhelper.model import GENERATED_KEY, SOURCE_JOURNEY, Condition  # noqa: E402
from tests.helpers import wait_dur  # noqa: E402

SCORES = table("DE_Master", [field("ContactId", pk=True, nullable=False), field("Score", "Number"),
                             field("A", "Text"), field("B", "Text"), field("Tier", "Text")], sendable_field="ContactId")
FORM = table("DE_Form", [field("ContactId", pk=True, nullable=False), field("Consent", "Boolean"),
                         field("Source", "Text", length=20)])
RANGES = table("DE_Config", [field("CampaignName", pk=True, nullable=False), field("Score", "Number", nullable=False)])


def row_run(acts, settings=None, form=FORM):
    """DE_Form is a contact-level table whose existence a split tests (TableModel.existence_tested)."""
    tables = [ENTRY, MASTER, form]
    levels = {"DE_Entry": LEVEL_CONTACT, "DE_Master": LEVEL_CONTACT, "DE_Form": LEVEL_CONTACT}
    groups = [("contact", [("DE_Entry", "ContactId"), ("DE_Master", "ContactId"), ("DE_Form", "ContactId")])]
    tm = table_model("DE_Entry", "ContactId", levels, groups, tables)
    tm.existence_tested = {"DE_Form": ("DE_Form", "ContactId")}
    return coverage.compute_coverage(package(journey(acts), tables), tm, settings or Settings(), NOW)


def test_range_values_are_strictly_inside_the_ranges():
    """Spec: "Coverage values are chosen strictly inside the ranges the conditions define, never on a bound";
    "Between takes the value low|high and includes both bounds"."""
    fmt = "%Y-%m-%d %H:%M"
    c = lambda op, v: cond("DE_Master", "F", op, v)
    # Between 10|20, GreaterThan 20, Equal 20: below 10, inside, above 20, then the named 20
    domain, other, _ = coverage.build_domain("Number", [c("GreaterThan", "20"), c("Between", "10|20"), c("Equal", "20")],
                                             True, False, fmt)
    assert domain == ["9", "15", "21", "20", None] and other == "21"
    # inclusive bounds no longer move inward only: both sides of >= and <= get a value
    assert coverage.build_domain("Number", [c("GreaterThanOrEqual", "5")], False, False, fmt)[0] == ["4", "6"]
    assert coverage.build_domain("Number", [c("LessThanOrEqual", "5")], False, False, fmt)[0] == ["4", "6"]
    # a Decimal keeps a value between neighbouring integers at its scale; a Number cannot
    assert coverage.build_domain("Decimal", [c("GreaterThan", "10"), c("LessThan", "11")], False, False, fmt,
                                 scale=2)[0] == ["9", "10.5", "12"]
    assert coverage.build_domain("Number", [c("GreaterThan", "10"), c("LessThan", "11")], False, False, fmt)[0] == ["9", "12"]
    assert coverage.build_domain("Decimal", [c("Between", "150|350"), c("GreaterThan", "350")], True, False, fmt,
                                 scale=4)[0] == ["149", "250", "351", None]
    # dates written without a time are compared on the date, so the values fall on other days
    assert coverage.build_domain("Date", [c("Between", "2026-01-01|2026-01-05")], False, False, fmt)[0] == \
        ["2025-12-31 00:00", "2026-01-03 00:00", "2026-01-06 00:00"]
    assert coverage.build_domain("Date", [c("Between", "2026-01-01|2026-01-02")], False, False, fmt)[0] == \
        ["2025-12-31 00:00", "2026-01-03 00:00"]
    # a text range gets no value from its bounds, and a warning; "low|high" is never a candidate value
    domain, _, warnings = coverage.build_domain("Text", [c("Between", "a|c"), c("Equal", "b")], True, False, fmt)
    assert domain == ["b", "OTHER", None] and "a|c" not in domain and any("intervalli di testo" in w for w in warnings)


def test_branch_needing_a_value_inside_a_text_range_is_reported_as_unsupported():
    """A text field gets no value inside a range, so a branch that needs one is not covered because of an
    element the generator does not support (spec, "Honest outcomes"), not reported as unreachable."""
    acts = [decision("SPLIT-1", "region", [("low", "low", "UPD-1", cond("DE_Config", "Channel", "LessThan", "A")),
                                           ("sms", "sms", "UPD-2", cond("DE_Config", "Channel", "Equal", "SMS"))],
                     remainder_next="UPD-3"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None)]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True)
    assert not any("non è negli schemi" in w for w in r.warnings)
    low = status(r, "SPLIT-1", "low")
    assert low.status == STATUS_NOT_COVERED and low.reason.startswith("branch non supportato")
    assert "LessThan" in low.reason and "non raggiungibile" not in low.reason
    assert r.unsupported == ["SPLIT-1/low"] and "Number, Decimal e Date" in r.unsupported_reasons["SPLIT-1/low"]
    # the other branches of the split are still evaluated and covered
    assert status(r, "SPLIT-1", "sms").status == STATUS_COVERED
    assert status(r, "SPLIT-1", "remainder_path").status == STATUS_COVERED


def test_between_cases_use_values_inside_the_ranges_and_not_null():
    """examples/between_inclusive: the Between case takes a value strictly between the bounds, the remainder
    case a number below the range rather than null."""
    acts = [decision("SPLIT-1", "band", [("a", "high", "UPD-1", cond("DE_Master", "Score", "GreaterThan", "20")),
                                         ("b", "mid", "UPD-2", cond("DE_Master", "Score", "Between", "10|20")),
                                         ("c", "twenty", "UPD-3", cond("DE_Master", "Score", "Equal", "20"))],
                     remainder_next="UPD-4"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None),
            update("UPD-4", "4", [], None)]
    r = run(acts, [ENTRY, SCORES])
    by_end = {c.end_activity_key: c.contact_values[("DE_Master", "Score")] for c in r.cases}
    assert by_end == {"UPD-1": "21", "UPD-2": "15", "UPD-4": "9"}
    c = status(r, "SPLIT-1", "c")
    assert c.status == STATUS_NOT_COVERED and "non raggiungibile" in c.reason


def test_class_representative_prefers_values_without_null():
    """Spec: "Among values that cover the same branches, the generator prefers those whose path rests on no
    unverified assumption: not null". (x, null) comes first in the enumeration, (z, y) has no null."""
    f = OR(AND(cond("DE_Master", "A", "Equal", "x"), cond("DE_Master", "B", "IsNull")),
           AND(cond("DE_Master", "A", "Equal", "z"), cond("DE_Master", "B", "Equal", "y")))
    acts = [decision("SPLIT-1", "s", [("t", "true", "UPD-T", f)], remainder_next="UPD-F"),
            update("UPD-T", "T", [], None), update("UPD-F", "F", [], None)]
    r = run(acts, [ENTRY, SCORES])
    true_case = next(c for c in r.cases if c.end_activity_key == "UPD-T")
    assert true_case.contact_values == {("DE_Master", "A"): "z", ("DE_Master", "B"): "y"}


def test_scenarios_crossing_the_same_branches_prefer_values_inside_the_ranges():
    """A scenario-level Score of 15 and of 20 both take the Between branch; 15 is inside the range."""
    acts = [decision("SPLIT-1", "band", [("b", "mid", "UPD-2", cond("DE_Config", "Score", "Between", "10|20")),
                                         ("c", "twenty", "UPD-3", cond("DE_Config", "Score", "Equal", "20"))],
                     remainder_next="UPD-4"),
            update("UPD-2", "2", [], None), update("UPD-3", "3", [], None), update("UPD-4", "4", [], None)]
    tables = [ENTRY, MASTER, RANGES]
    levels = {"DE_Entry": LEVEL_CONTACT, "DE_Master": LEVEL_CONTACT, "DE_Config": LEVEL_SCENARIO}
    groups = [("contact", [("DE_Entry", "ContactId"), ("DE_Master", "ContactId")]),
              ("scenario", [("DE_Entry", "CampaignName"), ("DE_Config", "CampaignName")])]
    tm = table_model("DE_Entry", "ContactId", levels, groups, tables)
    for seed in range(5):
        r = coverage.compute_coverage(package(journey(acts), tables), tm, Settings(seed=seed), NOW)
        mid = next(s for s in r.scenarios if s.cases[0].end_activity_key == "UPD-2")
        assert mid.values[("DE_Config", "Score")] == "15"


def test_scenarios_crossing_the_same_branches_prefer_values_without_null():
    """Spec: "Among values that cover the same branches, the generator prefers those whose path rests on no
    unverified assumption: not null". Team decision of 13 October 2026 (docs/progress.md): this holds between
    different scenarios too. A scenario-level Channel2 of OTHER and of null both take the remainder; OTHER wins
    whatever the seed (on the pilot, S18 gets CHANNEL2 = OTHER)."""
    acts = [decision("SPLIT-1", "channel 2", [("e", "email", "UPD-1", cond("DE_Config", "Channel2", "Equal", "Email"))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    tables = [ENTRY, MASTER, CONFIG]
    levels = {"DE_Entry": LEVEL_CONTACT, "DE_Master": LEVEL_CONTACT, "DE_Config": LEVEL_SCENARIO}
    groups = [("contact", [("DE_Entry", "ContactId"), ("DE_Master", "ContactId")]),
              ("scenario", [("DE_Entry", "CampaignName"), ("DE_Config", "CampaignName")])]
    tm = table_model("DE_Entry", "ContactId", levels, groups, tables)
    for seed in range(8):
        r = coverage.compute_coverage(package(journey(acts), tables), tm, Settings(seed=seed), NOW)
        assert None in r.domains[("DE_Config", "Channel2")]
        other = next(s for s in r.scenarios if s.cases[0].end_activity_key == "UPD-2")
        assert other.values[("DE_Config", "Channel2")] == "OTHER"


def test_row_present_or_absent_variable():
    """Spec, "Row present or absent": the linked key takes "row present" or "row absent"; on an absent row
    IsNull is true on any field and every other operator false; the absent row has no other value."""
    acts = [decision("SPLIT-1", "form?", [("none", "no form", "UPD-1", cond("DE_Form", "ContactId", "IsNull"))],
                     remainder_next="SPLIT-2"),
            decision("SPLIT-2", "consent", [("yes", "yes", "UPD-2", cond("DE_Form", "Consent", "Is", "true"))],
                     remainder_next="UPD-3"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None)]
    r = row_run(acts)
    assert r.domains[("DE_Form", "ContactId")] == [GENERATED_KEY, None]
    assert r.row_fields == {"DE_Form": ("DE_Form", "ContactId")}
    assert all(b.status == STATUS_COVERED for b in r.branches)
    values = {c.end_activity_key: c.contact_values for c in r.cases}
    assert values["UPD-1"] == {("DE_Form", "ContactId"): None, ("DE_Form", "Consent"): None}
    assert values["UPD-2"] == {("DE_Form", "ContactId"): GENERATED_KEY, ("DE_Form", "Consent"): "True"}
    assert values["UPD-3"] == {("DE_Form", "ContactId"): GENERATED_KEY, ("DE_Form", "Consent"): "False"}
    assert [r.row_absent(c, "DE_Form") for c in r.cases] == [c.end_activity_key == "UPD-1" for c in r.cases]
    assert not any("campo chiave" in w for w in r.warnings)


def test_absent_row_fails_every_operator_but_isnull():
    acts = [decision("SPLIT-1", "has form?", [("yes", "yes", "UPD-1", cond("DE_Form", "ContactId", "IsNotNull"))],
                     remainder_next="SPLIT-2"),
            decision("SPLIT-2", "source", [("null", "empty", "UPD-2", cond("DE_Form", "Source", "IsNull")),
                                           ("web", "web", "UPD-3", cond("DE_Form", "Source", "Equal", "Web"))],
                     remainder_next="UPD-4"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None),
            update("UPD-4", "4", [], None)]
    r = row_run(acts)
    assert status(r, "SPLIT-2", "null").status == STATUS_COVERED
    for key in ("web", "remainder_path"):
        b = status(r, "SPLIT-2", key)
        assert b.status == STATUS_NOT_COVERED and "non raggiungibile" in b.reason
    assert "DE_Form.Source è sempre null, perché il contatto non ha una riga in DE_Form" in status(r, "SPLIT-2", "web").reason
    assert r.stats["cases"] == 2


def test_update_contact_inserts_the_row_in_coverage():
    acts = [decision("SPLIT-1", "visit?", [("none", "no visit", "UPD-1", cond("DE_Form", "ContactId", "IsNull"))],
                     remainder_next="UPD-3"),
            update("UPD-1", "record", [("DE_Form", "Source", "Journey")], "SPLIT-2"),
            decision("SPLIT-2", "recorded?", [("none", "still none", "UPD-2", cond("DE_Form", "ContactId", "IsNull"))],
                     remainder_next="UPD-4"),
            update("UPD-2", "2", [], None), update("UPD-3", "3", [], None), update("UPD-4", "4", [], None)]
    r = row_run(acts)
    assert status(r, "SPLIT-1", "none").status == STATUS_COVERED
    assert status(r, "SPLIT-2", "remainder_path").status == STATUS_COVERED
    b = status(r, "SPLIT-2", "none")
    assert b.status == STATUS_NOT_COVERED and "ha sempre una riga in DE_Form" in b.reason
    inserted = next(c for c in r.cases if c.end_activity_key == "UPD-4")
    assert r.row_absent(inserted, "DE_Form")   # the CSV is the state before the run: no row yet


def test_insert_that_may_fail_leaves_the_path_unknown_and_is_reported():
    """docs/assumptions.md, rows 19 and 46: an insert that leaves a non-nullable field without a default empty
    may fail. Spec, "Honest outcomes": past an element the walk cannot predict, the path is unknown, and the
    branches only those contacts reach are not covered because of an unsupported element."""
    strict = table("DE_Form", [field("ContactId", pk=True, nullable=False), field("Consent", "Boolean"),
                               field("Source", "Text", length=20), field("Channel", "Text", nullable=False)])
    acts = [decision("SPLIT-1", "visit?", [("none", "no visit", "UPD-1", cond("DE_Form", "ContactId", "IsNull"))],
                     remainder_next="UPD-3"),
            update("UPD-1", "record", [("DE_Form", "Source", "Journey")], "SPLIT-2"),
            decision("SPLIT-2", "recorded?", [("none", "still none", "UPD-2", cond("DE_Form", "ContactId", "IsNull"))],
                     remainder_next="UPD-4"),
            update("UPD-2", "2", [], None), update("UPD-3", "3", [], None), update("UPD-4", "4", [], None)]
    r = row_run(acts, form=strict)
    stopped = next(c for c in r.cases if c.unknown_from is not None)
    assert stopped.unknown_from == "UPD-1" and "Channel" in stopped.unknown_reason
    assert "UPD-1" in r.unsupported and "può fallire" in r.unsupported_reasons["UPD-1"]
    for outcome in ("none", "remainder_path"):
        b = status(r, "SPLIT-2", outcome)
        assert b.status == STATUS_NOT_COVERED and b.reason.startswith("a valle di un elemento non supportato")
    assert status(r, "SPLIT-1", "none").status == STATUS_COVERED   # before the update


def test_journey_data_is_not_changed_by_update_contact():
    """examples/journey_data_snapshot: Journey Data reads the entry value, Contact Data the written one."""
    journey_gold = Condition("DE_Master", "Tier", "Equal", "Gold", source=SOURCE_JOURNEY)
    acts = [decision("SPLIT-1", "at entry", [("gold", "gold", "UPD-1", journey_gold)], remainder_next="UPD-4"),
            update("UPD-1", "downgrade", [("DE_Master", "Tier", "Silver")], "SPLIT-2"),
            decision("SPLIT-2", "journey data", [("gold", "gold", "SPLIT-3", journey_gold)], remainder_next=None),
            decision("SPLIT-3", "contact data", [("silver", "silver", "UPD-2", cond("DE_Master", "Tier", "Equal", "Silver"))],
                     remainder_next="UPD-3"),
            update("UPD-2", "2", [], None), update("UPD-3", "3", [], None), update("UPD-4", "4", [], None)]
    r = run(acts, [ENTRY, SCORES])
    for key in (("SPLIT-1", "gold"), ("SPLIT-1", "remainder_path"), ("SPLIT-2", "gold"), ("SPLIT-3", "silver")):
        assert status(r, *key).status == STATUS_COVERED
    for key in (("SPLIT-2", "remainder_path"), ("SPLIT-3", "remainder_path")):
        assert status(r, *key).status == STATUS_NOT_COVERED
    other = next(c for c in r.cases if c.end_activity_key == "UPD-4")
    assert other.contact_values[("DE_Master", "Tier")] not in (None, "Gold")


def test_key_field_compared_with_a_value_is_flagged_but_an_existence_test_on_it_is_not():
    acts = [decision("SPLIT-1", "s", [("a", "A", None, cond("DE_Master", "ContactId", "IsNull"))], remainder_next=None)]
    r = run(acts, [ENTRY, MASTER])
    assert r.domains[("DE_Master", "ContactId")] == [GENERATED_KEY]
    assert not any("campo chiave" in w for w in r.warnings)
    a = status(r, "SPLIT-1", "a")
    assert a.status == STATUS_NOT_COVERED and "contiene sempre la chiave generata" in a.reason
    acts = [decision("SPLIT-1", "s", [("a", "A", None, cond("DE_Master", "ContactId", "Equal", "C1"))], remainder_next=None)]
    r = run(acts, [ENTRY, MASTER])
    assert any("campo chiave" in w for w in r.warnings)


def test_path_past_an_unsupported_element_is_marked_unknown():
    """Spec, "Honest outcomes": "Past an unsupported element the expected result says the path is unknown from
    that activity on"."""
    acts = [decision("SPLIT-1", "perm", [("ok", "ok", "X-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-N"),
            unsupported("X-1", "SMSSYNC", "UPD-Y"), update("UPD-Y", "Y", [], None), update("UPD-N", "N", [], None)]
    r = run(acts, [ENTRY, MASTER], unsupported_keys=["X-1"])
    stopped = next(c for c in r.cases if c.contact_values[("DE_Master", "Perm")] == "True")
    assert stopped.unknown_from == "X-1" and "SMSSYNC" in stopped.unknown_reason
    plain = next(c for c in r.cases if c is not stopped)
    assert plain.unknown_from is None and plain.unknown_reason == ""


def test_action_windows_and_minimum_duration():
    """Spec, "Honest outcomes": the window of a manual open comes from the waits between the email and the
    engagement split; each case has a minimum duration."""
    emails = {"A1": email_asset("A1", "Flash"), "A2": email_asset("A2", "Slow")}
    acts = [decision("SPLIT-1", "perm", [("ok", "ok", "EMAIL-1", cond("DE_Master", "Perm", "Is", "true"))],
                     remainder_next="EMAIL-2"),
            email("EMAIL-1", "Flash", "A1", "WAIT-1"), wait_dur("WAIT-1", 5, "MINUTES", "ENG-1"),
            engagement("ENG-1", "Open Flash", "open", "EMAIL-1", None, None),
            email("EMAIL-2", "Slow", "A2", "WAIT-2"), wait_dur("WAIT-2", 2, "DAYS", "WAIT-3"),
            wait_attr("WAIT-3", "DE_Config", "WAIT1", "WAIT-4"), wait_dur("WAIT-4", 1, "HOURS", "ENG-2"),
            engagement("ENG-2", "Open Slow", "open", "EMAIL-2", None, None)]
    r = run(acts, [ENTRY, MASTER, CONFIG], scenario=True, emails=emails, settings=Settings(real_inbox="t@example.com"))
    flash = next(c for c in r.cases if c.manual_actions and c.end_activity_key == "ENG-1")
    [window] = flash.action_windows
    assert (window.activity_key, window.email_activity_key, window.wait_keys, window.minutes) == \
        ("ENG-1", "EMAIL-1", ["WAIT-1"], 5)
    assert window.attribute_wait_key is None and flash.min_duration_minutes == 5
    slow = next(c for c in r.cases if c.manual_actions and c.end_activity_key == "ENG-2")
    [window] = slow.action_windows
    assert window.wait_keys == ["WAIT-2", "WAIT-3", "WAIT-4"] and window.minutes == 2 * 24 * 60 + 60
    assert window.attribute_wait_key == "WAIT-3" and window.minutes_after_attribute_wait == 60
    assert slow.min_duration_minutes == 2 * 24 * 60 + 60
    assert all(not c.action_windows for c in r.cases if not c.manual_actions)


# ---------------------------------------------------------------------------
# Spec, "Extensions for time-dependent journeys (step 3, 14 October 2026)"
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402
from pathlib import Path  # noqa: E402

from cjhelper import criteria, loader, tables  # noqa: E402
from cjhelper.model import (  # noqa: E402
    KIND_SMS, SOURCE_CONTACT, Activity, JourneyFilter, Outcome, Step, TimelinePoint,
)

T0 = NOW
FMT = "%Y-%m-%d %H:%M"
TIMED = table("DE_Master", [field("ContactId", pk=True, nullable=False), field("StartDate", "Date"),
                            field("EndDate", "Date"), field("Status", "Text", nullable=False),
                            field("IsDunning", "Boolean"), field("Perm", "Boolean")], sendable_field="ContactId")
START = ("DE_Master", "StartDate")
END = ("DE_Master", "EndDate")


def day(k, hours=0):
    """T0 moved by k days (and hours), in the CSV date format."""
    return (T0 + timedelta(days=k, hours=hours)).strftime(FMT)


def rel(table_name, field_name, operator, value, source=SOURCE_CONTACT):
    """A condition with a relative date value, as criteria.py parses it."""
    return Condition(table_name, field_name, operator, value, source=source, relative=criteria.relative_date(value))


def wait_offset(key, table_name, field_name, amount, unit, next_key, time_of_day=None):
    w = wait_attr(key, table_name, field_name, next_key)
    w.wait_offset_amount, w.wait_offset_unit, w.wait_time_of_day = amount, unit, time_of_day
    return w


def sms(key, name, next_key):
    return Activity(key=key, raw_type="SMSSYNC", kind=KIND_SMS, name=name,
                    outcomes=[Outcome(key=key + "-out", label="", next=next_key)], sms_name=name)


def exit_filter(key, formula, description=""):
    return JourneyFilter(key=key, name=key, kind="exit", formula=formula, description=description)


def engine_run(acts, tables_=(ENTRY, TIMED), exits=(), settings=None, scenario=False, aliases=()):
    """The engine after run(), to read its validity and cut points, and the result."""
    j = journey(acts)
    j.exits = list(exits)
    tm = model_for(list(tables_), scenario)
    tm.aliases = [list(group) for group in aliases]
    engine = coverage.CoverageEngine(package(j, list(tables_)), tm, settings or Settings(), NOW)
    return engine, engine.run()


def case_ending(result, key):
    return next(c for c in result.cases if c.end_activity_key == key)


def brute_validity(engine, ref, value):
    """Spec, "Validity of the data": re-evaluate with the entry time moved by one day at a time, up to a year."""
    intern = {}
    base = engine._behaviour(ref, value, 0, intern)
    k = 0
    for slip in range(1, coverage.MAX_SLIP_DAYS + 1):
        if engine._behaviour(ref, value, slip, intern) != base:
            return k
        k = slip
    return None


def assert_validity_matches_simulation(engine, ref):
    points = engine._clock_points(ref)
    cuts = engine._cut_points(ref, points, engine.conditions_by_field[ref])
    for value in engine._clock_candidates(cuts, engine.conditions_by_field[ref], True):
        assert engine._clock_validity(ref, value)[0] == brute_validity(engine, ref, value), value


AFTER_TODAY = [decision("SPLIT-1", "Start date", [("path_a", "Future", "UPD-1", rel("DE_Master", "StartDate", "After", ";+;0;days")),
                                                  ("path_b", "Started", "UPD-2", rel("DE_Master", "StartDate", "AtOrBefore", ";+;0;days"))],
                        remainder_next="UPD-3"),
               update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None)]


def test_relative_date_domain_classes_validity_and_choice():
    """examples/relative_date_after_today: cut point {0}; one value per class; T0+2 (validity 1: After excludes the
    bound), T0-2 (not perishable, rule 3 drops T0, rule 5 the earliest), null alone."""
    engine, r = engine_run(AFTER_TODAY)
    assert engine.cut_points[START] == [0.0]
    assert r.domains[START] == [day(-2), day(2), None]
    assert engine.validity[START] == {day(-2): None, day(2): 1, None: None}
    assert all(b.status == STATUS_COVERED for b in r.branches)
    assert {c.end_activity_key: (c.contact_values[START], c.validity_days) for c in r.cases} == {
        "UPD-1": (day(2), 1), "UPD-2": (day(-2), None), "UPD-3": (None, None)}
    assert r.validity_days == 1
    [sentence] = r.perishable
    assert sentence.startswith(f"DE_Master.StartDate = {day(2)}: resta valido 1 giorno dall'ora di ingresso, poi ")
    assert "After" in sentence and "SPLIT-1" in sentence
    assert_validity_matches_simulation(engine, START)


def test_relative_date_in_the_past_with_a_strict_operator_and_null_never_representing():
    """examples/relative_date_before_minus_days: Before ;-;7;days. Null takes the same branch as T0-7..T0-5, so it
    never represents that class: the domain has no null; the remainder value is T0-5 (rule 4, validity 2)."""
    acts = [decision("SPLIT-1", "No order", [("path_a", "old", "UPD-1", rel("DE_Master", "StartDate", "Before", ";-;7;days"))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    engine, r = engine_run(acts)
    assert engine.cut_points[START] == [-7.0]
    assert r.domains[START] == [day(-9), day(-5)]
    assert engine.validity[START] == {day(-9): None, day(-5): 2}
    assert r.validity_days == 2 and r.stats["cases"] == 2
    assert_validity_matches_simulation(engine, START)


def test_cut_points_follow_the_fixed_delays_of_each_split():
    """examples/relative_date_chain_waits: a 2-day wait between two windows. Cut points {45, 47, 90}; classes by
    the branches taken on each path; rule 3 drops the cut point 90, rule 4 picks 89 (validity 42)."""
    window = lambda upper: AND(rel("DE_Master", "EndDate", "AtOrBefore", f";+;{upper};days"),
                               rel("DE_Master", "EndDate", "AtOrAfter", ";+;45;days"))
    acts = [decision("SPLIT-1", "Window", [("path_a", "In", "WAIT-1", window(90))], remainder_next="UPD-3"),
            wait_dur("WAIT-1", 2, "DAYS", "SPLIT-2"),
            decision("SPLIT-2", "Still", [("path_a", "In", "UPD-1", window(88))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None)]
    engine, r = engine_run(acts)
    assert engine.cut_points[END] == [45.0, 47.0, 90.0]
    # class A of the README (SPLIT-1 remainder: T0+43, T0+44, T0+91, T0+92 and null take the same branch) is one
    # class: 43, not perishable, is kept
    assert r.domains[END] == [day(43), day(46), day(89)]
    assert engine.validity[END] == {day(43): None, day(46): 1, day(89): 42}
    assert {c.end_activity_key: c.contact_values[END] for c in r.cases} == {"UPD-3": day(43), "UPD-2": day(46),
                                                                             "UPD-1": day(89)}
    assert all(b.status == STATUS_COVERED for b in r.branches)
    assert r.validity_days == 1
    late = case_ending(r, "UPD-1")
    assert [(p.activity_key, p.at) for p in late.timeline][:3] == [("SPLIT-1", T0), ("WAIT-1", T0 + timedelta(days=2)),
                                                                   ("SPLIT-2", T0 + timedelta(days=2))]
    assert_validity_matches_simulation(engine, END)


WAIT_THEN_BOUND = [wait_offset("WAIT-1", "DE_Master", "EndDate", -13, "DAYS", "SPLIT-1"),
                   decision("SPLIT-1", "Still ahead", [("path_a", "In time", "UPD-1", rel("DE_Master", "EndDate", "AtOrAfter", ";+;13;days"))],
                            remainder_next="UPD-2"),
                   update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]


def test_wait_with_an_offset_makes_the_class_compare_on_the_bound():
    """examples/wait_offset_then_relative_split: the wait "13 days before EndDate" holds the contact until the split
    compares on its bound. Rule 1 drops T0+13 (validity 0), rule 2 prefers T0+14 (the wait ends after one day); the
    late contact gets T0+11 (rule 5); the branch on the bound is noted (decided on the bound of a day-level comparison)."""
    engine, r = engine_run(WAIT_THEN_BOUND)
    assert engine.cut_points[END] == [13.0]
    assert r.domains[END] == [day(11), day(14)]
    in_time = case_ending(r, "UPD-1")
    assert in_time.contact_values[END] == day(14) and in_time.validity_days == 1
    assert [(p.activity_key, p.at) for p in in_time.timeline][:2] == [("WAIT-1", T0 + timedelta(days=1)),
                                                                      ("SPLIT-1", T0 + timedelta(days=1))]
    late = case_ending(r, "UPD-2")
    assert late.contact_values[END] == day(11) and late.timeline[0].at == T0
    notes = [w for w in r.warnings if "confronto sul giorno" in w]
    assert len(notes) == 1 and "S01-C" in notes[0] and "SPLIT-1" in notes[0] and day(14) in notes[0]
    assert_validity_matches_simulation(engine, END)


def test_minimum_validity_setting_drives_the_choice_with_the_wait_end():
    """Rule (1) then rule (2): with a minimum of 0 the value on which the wait ends at once wins; 2 days asks for
    T0+15; a minimum no candidate reaches keeps them all (then rule 2 again)."""
    picks = {}
    for minimum in (0, 1, 2, 30):
        _, r = engine_run(WAIT_THEN_BOUND, settings=Settings(min_validity_days=minimum))
        picks[minimum] = case_ending(r, "UPD-1").contact_values[END]
    assert picks == {0: day(13), 1: day(14), 2: day(15), 30: day(13)}


def test_time_level_relative_dates_cut_at_the_hour():
    """A relative date in hours compares the date and time ("Now"): the cut point is a fraction of a day."""
    acts = [decision("SPLIT-1", "soon", [("path_a", "soon", "UPD-1", rel("DE_Master", "StartDate", "After", ";+;3;hours"))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    engine, r = engine_run(acts)
    assert engine.cut_points[START] == [0.125]
    assert r.domains[START] == [day(-2, 3), day(2, 3)]
    assert engine.validity[START] == {day(-2, 3): None, day(2, 3): 1}
    assert_validity_matches_simulation(engine, START)


def test_entry_time_setting_moves_the_clock_and_the_values():
    """Spec, step 3: "The clock starts at the entry time, a setting whose default is the generation time"."""
    entry = datetime(2026, 10, 20, 9, 0)
    _, r = engine_run(AFTER_TODAY, settings=Settings(entry_time=entry))
    assert r.domains[START] == [(entry - timedelta(days=2)).strftime(FMT), (entry + timedelta(days=2)).strftime(FMT), None]
    assert all(c.timeline[0].at == entry for c in r.cases)


def test_relative_predicates_are_evaluated_at_every_delay_of_their_split():
    """A split reached with the delays 0 and 3 days: T0+3 is "after tomorrow" for the contact that comes at once but
    not for the one that waited, so it is a class of its own, and the classes evaluate the predicate twice."""
    acts = [decision("SPLIT-0", "perm", [("yes", "yes", "WAIT-1", cond("DE_Master", "Perm", "Is", "true"))],
                     remainder_next="SPLIT-R"),
            wait_dur("WAIT-1", 3, "DAYS", "SPLIT-R"),
            decision("SPLIT-R", "later", [("path_a", "later", "UPD-1", rel("DE_Master", "StartDate", "After", ";+;1;days"))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    engine, r = engine_run(acts)
    assert engine.delays["SPLIT-R"] == {0.0, 3.0}
    assert engine.cut_points[START] == [1.0, 4.0]
    assert r.domains[START] == [day(-1), day(3), day(6)]
    moments = dict((formula_text_of(f), m) for f, m in engine._contact_predicate_points())
    assert moments["DE_Master.StartDate After ';+;1;days'"] == ((0.0, ()), (3.0, ()))
    assert moments["DE_Master.Perm Is 'true'"] == ((0.0, ()),)
    assert all(len(c.predicate_truth) == 3 for c in r.classes)
    assert all(b.status == STATUS_COVERED for b in r.branches)
    # the class of T0+3 takes path_a without the wait and the remainder after it
    perm = ("DE_Master", "Perm")
    assert [c for c in r.classes if c.values[START] == day(3) and c.values[perm] != "True"][0].predicate_truth[1:] == (True, False)


def formula_text_of(formula):
    from cjhelper.model import formula_text
    return formula_text(formula)


def test_range_operators_take_absolute_date_values_but_never_relative_ones():
    """Spec, step 3: "The same operators with an absolute date value keep the earlier reading"; a relative value is
    never a bound nor a named value of build_domain."""
    c = lambda op, v: cond("DE_Master", "F", op, v)
    assert coverage.build_domain("Date", [c("After", "2026-01-01")], False, False, FMT)[0] == \
        ["2025-12-31 00:00", "2026-01-02 00:00"]
    domain = coverage.build_domain("Date", [rel("DE_Master", "F", "After", ";+;0;days")], True, False, FMT)[0]
    assert ";+;0;days" not in domain and all(v is None or v.startswith("2000-") for v in domain)
    assert {"After", "AtOrAfter", "Before", "AtOrBefore"} <= set(coverage.RANGE_OPERATORS)


# -- aliases --------------------------------------------------------------------

ENTRY_TIER = table("DE_Entry", [field("ContactId", pk=True, nullable=False), field("CampaignName", pk=True, nullable=False),
                                field("Tier", length=20), field("Channel", length=20)], sendable_field="ContactId")
MASTER_TIER = table("DE_Master", [field("ContactId", pk=True, nullable=False), field("Tier", length=20)],
                    sendable_field="ContactId")


def test_an_entry_copy_and_its_source_are_one_variable():
    """examples/entry_copy_alias: a Journey Data split on the entry copy, then Contact Data splits on the source.
    One variable, named by the source (the member outside the entry table): 2 classes, 2 cases, the contradictory
    branches unreachable."""
    journey_gold = Condition("DE_Entry", "Tier", "Equal", "Gold", source=SOURCE_JOURNEY)
    acts = [decision("SPLIT-1", "entry", [("path_a", "gold", "SPLIT-2", journey_gold)], remainder_next="SPLIT-3"),
            decision("SPLIT-2", "now", [("path_a", "gold", "UPD-1", cond("DE_Master", "Tier", "Equal", "Gold"))], remainder_next="UPD-2"),
            decision("SPLIT-3", "now", [("path_a", "gold", "UPD-3", cond("DE_Master", "Tier", "Equal", "Gold"))], remainder_next="UPD-4"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None),
            update("UPD-4", "4", [], None)]
    engine, r = engine_run(acts, (ENTRY_TIER, MASTER_TIER), aliases=[[("DE_Entry", "Tier"), ("DE_Master", "Tier")]])
    assert engine.alias_of == {("DE_Entry", "Tier"): ("DE_Master", "Tier")}
    assert r.contact_fields == [("DE_Master", "Tier")] and list(r.domains) == [("DE_Master", "Tier")]
    assert len(r.classes) == 2 and r.stats["cases"] == 2
    assert all(set(c.contact_values) == {("DE_Master", "Tier")} for c in r.cases)
    assert case_ending(r, "UPD-1").contact_values[("DE_Master", "Tier")] == "Gold"
    for key in (("SPLIT-2", "remainder_path"), ("SPLIT-3", "path_a")):
        assert status(r, *key).status == STATUS_NOT_COVERED and "non raggiungibile" in status(r, *key).reason


def test_alias_representative_is_the_first_member_outside_the_entry_table():
    j = journey([decision("SPLIT-1", "s", [("a", "a", None, cond("DE_Entry", "Tier", "Equal", "Gold"))], remainder_next=None)])
    tables_ = [ENTRY_TIER, MASTER_TIER, table("DE_Stage", [field("ContactId", pk=True, nullable=False), field("Tier")])]
    tm = model_for(tables_)
    tm.aliases = [[("DE_Entry", "Tier"), ("DE_Stage", "Tier"), ("DE_Master", "Tier")], [("DE_Entry", "Channel"), ("de_entry", "x")]]
    engine = coverage.CoverageEngine(package(j, tables_), tm, Settings(), NOW)
    assert engine.alias_of[("DE_Entry", "Tier")] == ("DE_Stage", "Tier")
    assert engine.alias_of[("DE_Master", "Tier")] == ("DE_Stage", "Tier")
    assert ("DE_Entry", "Channel") not in engine.alias_of      # no member outside the entry table: the first one


def test_a_scenario_level_source_read_through_its_entry_copy_is_branched_on():
    """The walker stops on the entry copy (Journey Data); the exploration branches on the source's values."""
    acts = [decision("SPLIT-1", "channel", [("e", "email", "UPD-1", Condition("DE_Entry", "Channel", "Equal", "Email",
                                                                             source=SOURCE_JOURNEY))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    _, r = engine_run(acts, (ENTRY_TIER, MASTER_TIER, CONFIG), scenario=True,
                      aliases=[[("DE_Entry", "Channel"), ("DE_Config", "Channel")]])
    assert r.scenario_fields == [("DE_Config", "Channel")] and r.contact_fields == []
    assert all(b.status == STATUS_COVERED for b in r.branches)
    assert sorted(s.values[("DE_Config", "Channel")] for s in r.scenarios) == ["Email", "OTHER"]


def test_aliases_from_the_real_table_model():
    """examples/entry_copy_alias through loader.py and tables.py: TableModel.aliases groups the copy and its source."""
    path = Path(__file__).resolve().parent.parent / "examples" / "entry_copy_alias" / "package.json"
    pkg = loader.load_package(str(path))
    tm = tables.build_table_model(pkg)
    assert [("DE_Entry", "Tier"), ("DE_Master", "Tier")] in tm.aliases
    r = coverage.compute_coverage(pkg, tm, Settings(generation_time=NOW), NOW)
    assert r.contact_fields == [("DE_Master", "Tier")] and r.stats["cases"] == 2


# -- exit criteria ------------------------------------------------------------------

def exit_at_wait_end():
    acts = [wait_dur("WAIT-1", 1, "DAYS", "SPLIT-1"),
            decision("SPLIT-1", "Active?", [("path_a", "Active", "EMAIL-1", cond("DE_Master", "Status", "Equal", "Active"))],
                     remainder_next="UPD-2"),
            email("EMAIL-1", "Offer", "A1", "UPD-1"), update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    exits = [exit_filter("EXITCRITERIA", OR(cond("DE_Master", "Status", "NotEqual", "Active"),
                                            cond("DE_Master", "IsDunning", "Is", "true")), "Not active or dunning")]
    return acts, exits


def test_exit_criteria_target_case_and_unreachable_branch():
    """examples/exit_criteria_at_wait_end: the exit field is collected, the exit target is covered by a case that
    ends at the wait, and the remainder is unreachable because those contacts leave at the end of WAIT-1."""
    acts, exits = exit_at_wait_end()
    _, r = engine_run(acts, exits=exits)
    assert ("DE_Master", "IsDunning") in r.contact_fields
    [target] = r.exit_targets
    assert (target.activity_key, target.activity_name, target.activity_kind, target.outcome_key, target.outcome_label) == \
        ("EXITCRITERIA", "Not active or dunning", "exit", "met", "criteri di uscita soddisfatti")
    leaving = next(c for c in r.cases if c.exit_by_criteria)
    assert target.status == STATUS_COVERED and target.covered_by == [leaving.id]
    assert leaving.exit_by_criteria == "WAIT-1" and leaving.end_activity_key == "WAIT-1" and leaving.unknown_from is None
    assert leaving.steps == [Step("WAIT-1", None)]
    assert leaving.timeline == [TimelinePoint("WAIT-1", None, T0 + timedelta(days=1))]
    assert "criteri di uscita" in leaving.summary
    staying = case_ending(r, "UPD-1")
    assert staying.contact_values == {("DE_Master", "Status"): "Active", ("DE_Master", "IsDunning"): "False"}
    assert staying.exit_by_criteria is None
    remainder = status(r, "SPLIT-1", "remainder_path")
    assert remainder.status == STATUS_NOT_COVERED
    assert remainder.reason.startswith("non raggiungibile: lo split è raggiunto, ma i contatti che prenderebbero questo "
                                       "branch escono prima dal journey alla fine del wait '1 DAYS' (WAIT-1)")
    assert "Not active or dunning" in remainder.reason
    assert r.stats["branches"] == 2 and r.stats["exit_targets"] == 1 and r.stats["exit_targets_covered"] == 1
    assert r.validity_days is None


def test_exit_criteria_that_take_everyone_out_make_the_wait_the_root_cause():
    """examples/exit_criteria_removes_everyone: every contact reaching WAIT-1 leaves there; the branches behind it
    are unreachable for that reason, grouped under the wait, and one warning states the finding."""
    acts = [decision("SPLIT-1", "End date known?", [("path_a", "Known", "WAIT-1", cond("DE_Master", "EndDate", "IsNotNull"))],
                     remainder_next="UPD-3"),
            wait_dur("WAIT-1", 2, "DAYS", "SPLIT-2"),
            decision("SPLIT-2", "Opt-in", [("path_a", "yes", "UPD-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None), update("UPD-3", "3", [], None)]
    exits = [exit_filter("EXITCRITERIA", OR(cond("DE_Master", "EndDate", "IsNotNull"), cond("DE_Master", "IsDunning", "Is", "true")))]
    engine, r = engine_run(acts, exits=exits)
    assert engine.exited_at.get("WAIT-1", 0) > 0 and engine.passed_at.get("WAIT-1", 0) == 0
    for outcome in ("path_a", "remainder_path"):
        b = status(r, "SPLIT-2", outcome)
        assert b.status == STATUS_NOT_COVERED and b.behind == ("WAIT-1", "WAIT-1-out")
        assert b.reason.startswith("non raggiungibile: si trova a valle del branch '2 DAYS: fine del wait' (WAIT-1)")
        assert "tutti i contatti che arrivano alla fine del wait '2 DAYS' (WAIT-1) escono dal journey per i criteri di uscita" in b.reason
    findings = [w for w in r.warnings if w.startswith("rilievo sul journey")]
    assert len(findings) == 1 and "WAIT-1" in findings[0]
    assert r.exit_targets[0].status == STATUS_COVERED
    assert status(r, "SPLIT-1", "path_a").status == STATUS_COVERED and r.stats["cases"] == 2


def test_exit_target_not_covered_with_its_reason():
    """No contact that reaches the wait meets the criterion; and a journey with no wait never evaluates it."""
    acts = [decision("SPLIT-0", "a?", [("a", "a", "WAIT-1", cond("DE_Master", "Status", "Equal", "A"))], remainder_next="UPD-2"),
            wait_dur("WAIT-1", 1, "DAYS", "UPD-1"), update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    _, r = engine_run(acts, exits=[exit_filter("E1", cond("DE_Master", "Status", "Equal", "B"))])
    [target] = r.exit_targets
    assert target.status == STATUS_NOT_COVERED and target.covered_by == []
    assert target.reason.startswith("non raggiungibile: nessun contatto che arriva alla fine di un wait soddisfa i criteri")
    assert r.stats["exit_targets_covered"] == 0
    no_wait = [decision("SPLIT-0", "perm", [("a", "a", "UPD-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-2"),
               update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    _, r = engine_run(no_wait, exits=[exit_filter("E1", cond("DE_Master", "Status", "Equal", "B"))])
    assert ("DE_Master", "Status") not in r.contact_fields      # never evaluated: it reads nothing
    assert r.exit_targets[0].status == STATUS_NOT_COVERED and "nessun percorso del journey passa per un wait" in r.exit_targets[0].reason


def test_several_exit_criteria_each_get_a_target():
    acts = [wait_dur("WAIT-1", 1, "DAYS", "SPLIT-1"),
            decision("SPLIT-1", "perm", [("a", "a", "UPD-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    exits = [exit_filter("E1", cond("DE_Master", "Status", "Equal", "Out")), exit_filter("E2", cond("DE_Master", "IsDunning", "Is", "true"))]
    engine, r = engine_run(acts, exits=exits)
    assert [t.activity_key for t in r.exit_targets] == ["E1", "E2"]
    assert all(t.status == STATUS_COVERED for t in r.exit_targets)
    assert engine.exit_met_at["WAIT-1"] == {"E1", "E2"}
    for target in r.exit_targets:
        cases = {c.id: c for c in r.cases}
        assert all(cases[cid].exit_by_criteria == "WAIT-1" for cid in target.covered_by)
    assert all(b.status == STATUS_COVERED for b in r.branches)


def test_exit_criteria_with_a_relative_date_are_evaluated_at_every_wait_end():
    """The exit criteria are a clock site: their relative condition is evaluated at the end of each wait."""
    acts = [wait_dur("WAIT-1", 2, "DAYS", "SPLIT-1"),
            decision("SPLIT-1", "perm", [("a", "a", "UPD-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    exits = [exit_filter("E1", rel("DE_Master", "EndDate", "Before", ";+;0;days"))]
    engine, r = engine_run(acts, exits=exits)
    assert engine.cut_points[END] == [2.0]
    moments = dict((formula_text_of(f), m) for f, m in engine._contact_predicate_points())
    assert moments["DE_Master.EndDate Before ';+;0;days'"] == ((2.0, ()),)   # the end of WAIT-1, 2 days in
    assert r.exit_targets[0].status == STATUS_COVERED
    leaving = next(c for c in r.cases if c.exit_by_criteria)
    assert leaving.contact_values[END] is not None and leaving.contact_values[END] < day(2)
    assert all(b.status == STATUS_COVERED for b in r.branches)


# -- timeline, SMS, statistical cases --------------------------------------------------

def test_cases_carry_timeline_and_sms():
    acts = [decision("SPLIT-1", "perm", [("a", "a", "SMS-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-2"),
            sms("SMS-1", "Reminder", "WAIT-1"), wait_dur("WAIT-1", 2, "HOURS", "UPD-1"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    _, r = engine_run(acts)
    texted = case_ending(r, "UPD-1")
    assert texted.sms_sent == ["SMS-1"] and case_ending(r, "UPD-2").sms_sent == []
    assert [(p.activity_key, p.at) for p in texted.timeline] == [
        ("SPLIT-1", T0), ("SMS-1", T0), ("WAIT-1", T0 + timedelta(hours=2)), ("UPD-1", T0 + timedelta(hours=2))]
    assert len(texted.timeline) == len(texted.steps)


def test_statistical_case_timeline_and_branch_exits():
    acts = [wait_dur("WAIT-0", 1, "DAYS", "RANDOM-1"),
            random_split("RANDOM-1", [("branchResult-1", 90, "WAIT-1"), ("branchResult-2", 10, "UPD-2")]),
            wait_dur("WAIT-1", 1, "DAYS", "UPD-1"), update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    exits = [exit_filter("E1", cond("DE_Master", "IsDunning", "Is", "true"))]
    _, r = engine_run(acts, exits=exits)
    stat = next(c for c in r.cases if c.random is not None)
    assert stat.timeline[-1] == TimelinePoint("RANDOM-1", None, T0 + timedelta(days=1))
    assert [p.activity_key for p in stat.timeline] == [s.activity_key for s in stat.steps]
    for branch in stat.random.branches:
        assert len(branch.timeline) == len(branch.steps)
        assert branch.exit_by_criteria is None
    assert r.exit_targets[0].status == STATUS_COVERED


def test_exit_criteria_on_a_scenario_level_field_branch_at_the_wait():
    """The walker stops at the end of the wait on the undecided scenario field; the exploration branches on it."""
    acts = [wait_dur("WAIT-1", 1, "DAYS", "SPLIT-1"),
            decision("SPLIT-1", "perm", [("a", "a", "UPD-1", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    _, r = engine_run(acts, (ENTRY, TIMED, CONFIG), exits=[exit_filter("E1", cond("DE_Config", "CG", "Is", "true"))],
                      scenario=True)
    assert r.scenario_fields == [("DE_Config", "CG")]
    assert r.exit_targets[0].status == STATUS_COVERED and all(b.status == STATUS_COVERED for b in r.branches)
    leaving = next(s for s in r.scenarios if any(c.exit_by_criteria for c in s.cases))
    assert leaving.values[("DE_Config", "CG")] == "True"
    assert all(s.values[("DE_Config", "CG")] == "False" for s in r.scenarios if s is not leaving)


def test_classes_are_built_per_component_of_the_contact_fields():
    """14 nullable Booleans in two independent components of 7 (each read by its own splits): 3**14 combinations,
    more than the old cap on the whole product, which never let the first field leave its first value, so the
    remainder of a split on A1 alone was lost. Per component, each product has 3**7 combinations: every class is
    found and the member counts add up to the whole product."""
    wide = table("DE_Master", [field("ContactId", pk=True, nullable=False)]
                 + [field(f"{side}{i}", "Boolean") for side in "AB" for i in range(1, 8)], sendable_field="ContactId")
    all_true = lambda side: AND(*[cond("DE_Master", f"{side}{i}", "Is", "true") for i in range(1, 8)])
    acts = [decision("SPLIT-1", "all A", [("yes", "yes", "SPLIT-2", all_true("A"))], remainder_next="SPLIT-2"),
            decision("SPLIT-2", "all B", [("yes", "yes", "SPLIT-3", all_true("B"))], remainder_next="SPLIT-3"),
            decision("SPLIT-3", "A1", [("yes", "yes", "UPD-1", cond("DE_Master", "A1", "Is", "true"))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    assert 3 ** 14 > coverage.MAX_CONTACT_COMBINATIONS
    _, r = engine_run(acts, (ENTRY, wide))
    assert r.stats["classes_incomplete"] == 0 and not any("combinazioni" in w for w in r.warnings)
    assert len(r.classes) == 3 * 2 and sum(c.member_count for c in r.classes) == 3 ** 14
    assert [c.id for c in r.classes] == list(range(1, 7))
    assert all(b.status == STATUS_COVERED for b in r.branches)
    # representatives keep the preference (no null) and every class holds every contact-level field, in order
    assert all(list(c.values) == r.contact_fields and None not in c.values.values() for c in r.classes)
    assert r.classes[0].values == {ref: "True" for ref in r.contact_fields}


def _class_rows(result):
    return [(c.id, c.values, c.predicate_truth, c.member_count) for c in result.classes]


def test_large_components_are_enumerated_exactly_field_by_field(monkeypatch):
    """A component over the cap is enumerated by prefixes that merge when their predicates have the same residual;
    it gives the same classes (ids, representatives, truths, member counts) and cases as the full product."""
    many = table("DE_Master", [field("ContactId", pk=True, nullable=False), field("StartDate", "Date")]
                 + [field(f"F{i}", "Boolean") for i in range(1, 7)], sendable_field="ContactId")
    f = lambda i, v="true": cond("DE_Master", f"F{i}", "Is", v)
    acts = [decision("SPLIT-1", "mix", [("a", "a", "WAIT-1", AND(f(1), OR(f(2), f(3, "false")), rel("DE_Master", "StartDate", "After", ";+;0;days"))),
                                        ("b", "b", "WAIT-1", OR(AND(f(4), f(5)), cond("DE_Master", "F6", "IsNull")))],
                     remainder_next="WAIT-1"),
            wait_dur("WAIT-1", 2, "DAYS", "SPLIT-2"),
            decision("SPLIT-2", "later", [("a", "a", "UPD-1", AND(f(2), rel("DE_Master", "StartDate", "After", ";+;1;days")))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    _, plain = engine_run(acts, (ENTRY, many))
    monkeypatch.setattr(coverage, "MAX_CONTACT_COMBINATIONS", 10)
    _, exact = engine_run(acts, (ENTRY, many))
    assert exact.stats["classes_incomplete"] == 0 and not any("combinazioni" in w for w in exact.warnings)
    assert _class_rows(exact) == _class_rows(plain)
    assert [(c.id, c.contact_values, c.steps) for c in exact.cases] == [(c.id, c.contact_values, c.steps) for c in plain.cases]
    assert sum(c.member_count for c in exact.classes) == 3 ** 6 * len(exact.domains[START])


def test_a_component_over_the_cap_is_complete():
    """13 Booleans read by one formula: 3**13 combinations in one component, more than the cap; the first and the
    last field still take every value."""
    wide = table("DE_Master", [field("ContactId", pk=True, nullable=False)]
                 + [field(f"B{i}", "Boolean") for i in range(1, 14)], sendable_field="ContactId")
    acts = [decision("SPLIT-1", "all", [("yes", "yes", "SPLIT-2", AND(*[cond("DE_Master", f"B{i}", "Is", "true") for i in range(1, 14)]))],
                     remainder_next="SPLIT-2"),
            decision("SPLIT-2", "first", [("yes", "yes", "SPLIT-3", cond("DE_Master", "B1", "Is", "true"))], remainder_next="SPLIT-3"),
            decision("SPLIT-3", "last", [("yes", "yes", "UPD-1", cond("DE_Master", "B13", "Is", "true"))], remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    assert 3 ** 13 > coverage.MAX_CONTACT_COMBINATIONS
    _, r = engine_run(acts, (ENTRY, wide))
    assert r.stats["classes_incomplete"] == 0
    assert sum(c.member_count for c in r.classes) == 3 ** 13
    assert len(r.classes) == 5      # all true; then B1 and B13 each true or not, with some other field not true
    assert all(b.status == STATUS_COVERED for b in r.branches)


def test_a_delay_only_other_values_reach_does_not_separate_a_class():
    """ret_99_6w: SPLIT-2 is reached at once by the dates the window of SPLIT-1 lets through, and after a 3-day wait
    by the others. Evaluated at every fixed delay, T0+30 and T0+31 differ at the 3-day delay ("on or after Today +
    28" three days later), a delay no contact holding them reaches; on their own paths they take the same branches,
    so they are one class, and the value kept is not on a cut point and lasts longest (T0+33, 3 days)."""
    acts = [decision("SPLIT-1", "window", [("path_a", "in", "SPLIT-2", rel("DE_Master", "EndDate", "AtOrAfter", ";+;30;days"))],
                     remainder_next="WAIT-1"),
            wait_dur("WAIT-1", 3, "DAYS", "SPLIT-2"),
            decision("SPLIT-2", "still", [("path_a", "in", "UPD-1", rel("DE_Master", "EndDate", "AtOrAfter", ";+;28;days"))],
                     remainder_next="UPD-2"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    engine, r = engine_run(acts)
    assert engine.cut_points[END] == [28.0, 30.0, 31.0]
    points = engine._clock_points(END)
    assert engine._clock_vector(points, day(30)) != engine._clock_vector(points, day(31))
    intern = {}
    assert engine._behaviour(END, day(30), 0, intern) == engine._behaviour(END, day(31), 0, intern)
    assert r.domains[END] == [day(26), day(33)]
    assert engine.validity[END] == {day(26): None, day(33): 3}
    assert all(b.status == STATUS_COVERED for b in r.branches)       # the remainder of SPLIT-2 by T0+26, after the wait
    assert r.validity_days == 3
    assert_validity_matches_simulation(engine, END)


def _perishable_exit_journey(with_flag: bool):
    """SPLIT-1 at entry: "EndDate on or after Today + 13"; both branches then wait one day. The exit criteria take
    out the contacts whose EndDate is exactly 7 days after the wait's end (T0+8, valid 0 days); with the flag, also
    the flagged contacts of path_a (a value that lasts)."""
    on_day_8 = AND(rel("DE_Master", "EndDate", "After", ";+;6;days"), rel("DE_Master", "EndDate", "Before", ";+;8;days"))
    formula = OR(on_day_8, AND(cond("DE_Master", "Perm", "Is", "true"), rel("DE_Master", "EndDate", "AtOrAfter", ";+;13;days"))) \
        if with_flag else on_day_8
    acts = [decision("SPLIT-1", "far", [("path_a", "far", "WAIT-2", rel("DE_Master", "EndDate", "AtOrAfter", ";+;13;days"))],
                     remainder_next="WAIT-1"),
            wait_dur("WAIT-1", 1, "DAYS", "UPD-2"), wait_dur("WAIT-2", 1, "DAYS", "UPD-1"),
            update("UPD-1", "1", [], None), update("UPD-2", "2", [], None)]
    return acts, [exit_filter("E1", formula)]


def test_perishable_leaves_do_not_displace_robust_ones():
    """A leaf holding a value valid for fewer days than the minimum counts in the cover only for the targets no
    leaf without such a value reaches: here the remainder and the exit are reached by values that last, so the
    T0+8 contacts are left out and the data stays valid at least one day."""
    acts, exits = _perishable_exit_journey(with_flag=True)
    engine, r = engine_run(acts, exits=exits)
    assert engine.validity[END][day(8)] == 0
    det = [leaf for leaf in engine.leaves if leaf.deterministic]
    masks = engine._effective_masks(det)
    short = [leaf for leaf in det if engine.classes[leaf.class_id - 1].values[END] == day(8)]
    assert short and all(engine._leaf_is_short(leaf) and masks[leaf.id] == 0 for leaf in short)
    assert all(c.contact_values[END] != day(8) for c in r.cases)
    assert all(b.status == STATUS_COVERED for b in r.branches) and r.exit_targets[0].status == STATUS_COVERED
    assert r.validity_days is not None and r.validity_days >= 1


def test_a_perishable_leaf_is_kept_for_a_target_only_it_reaches():
    acts, exits = _perishable_exit_journey(with_flag=False)
    _, r = engine_run(acts, exits=exits)
    leaving = next(c for c in r.cases if c.exit_by_criteria)
    assert leaving.contact_values[END] == day(8) and leaving.validity_days == 0
    assert r.exit_targets[0].status == STATUS_COVERED and r.validity_days == 0
    assert any("resta valido solo 0 giorni" in w for w in r.warnings)
