from datetime import datetime

import pytest

from cjhelper import criteria, graph
from cjhelper.model import (
    KIND_SMS, KIND_WAIT_STO, SOURCE_JOURNEY, Activity, Condition, JourneyFilter, Outcome, Step,
    TimelinePoint,
)
from tests.helpers import (
    AND, OR, cond, decision, email, email_asset, engagement, field, journey, package, table, unsupported, update,
    wait_attr, wait_dur,
)

MASTER = table("DE_Master", [field("ContactId", pk=True, nullable=False), field("Perm", "Boolean"),
                             field("Flag", "Boolean"), field("Channel", "Text", length=10)], sendable_field="ContactId")
ENTRY = table("DE_Entry", [field("ContactId", pk=True, nullable=False)], sendable_field="ContactId")


def schema():
    return graph.Schema({t.name: t for t in (MASTER, ENTRY)})


def lookup_from(values):
    def lookup(ref):
        return values.get(ref, graph.UNKNOWN)
    return lookup


def test_evaluate_three_valued_logic():
    s = schema()
    f = AND(cond("DE_Master", "Perm", "Is", "true"), OR(cond("DE_Master", "Flag", "Is", "false"), cond("DE_Master", "Flag", "IsNull")))
    assert graph.evaluate(f, lookup_from({("DE_Master", "Perm"): "False"}), s, False) is False  # AND short-circuits
    assert graph.evaluate(f, lookup_from({("DE_Master", "Perm"): "True"}), s, False) is graph.UNKNOWN
    assert graph.evaluate(f, lookup_from({("DE_Master", "Perm"): "True", ("DE_Master", "Flag"): None}), s, False) is True
    assert graph.evaluate(f, lookup_from({("DE_Master", "Perm"): "True", ("DE_Master", "Flag"): "True"}), s, False) is False
    assert graph.first_unknown_field(f, lookup_from({("DE_Master", "Perm"): "True"}), s) == ("DE_Master", "Flag")


def test_canonical_spelling_is_case_insensitive():
    s = schema()
    assert s.canonical(("de_master", "PERM")) == ("DE_Master", "Perm")
    assert s.field_type(("DE_MASTER", "perm")) == "Boolean"


def build_walker(acts, emails=None, case_insensitive=False):
    j = journey(acts)
    pkg = package(j, [MASTER, ENTRY], emails)
    return graph.Walker(j, schema(), pkg.emails, case_insensitive, datetime(2026, 10, 11, 12, 0)), j


def test_decision_first_match_wins_and_remainder():
    acts = [
        decision("SPLIT-1", "s", [("a", "A", "UPD-A", cond("DE_Master", "Channel", "Equal", "X")),
                                  ("b", "B", "UPD-B", cond("DE_Master", "Channel", "Equal", "X"))], remainder_next="UPD-R"),
        update("UPD-A", "A", [], None), update("UPD-B", "B", [], None), update("UPD-R", "R", [], None),
    ]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "X"}))
    assert st.stop == graph.STOP_EXIT
    assert st.steps == [Step("SPLIT-1", "a"), Step("UPD-A", "UPD-A-out")]
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "Y"}))
    assert st.steps[0] == Step("SPLIT-1", "remainder_path")
    assert st.branches() == [("SPLIT-1", "remainder_path"), ("UPD-R", "UPD-R-out")]


def test_undetermined_value_stops_the_walk_and_names_the_field():
    acts = [decision("SPLIT-1", "s", [("a", "A", None, cond("DE_Master", "Channel", "Equal", "X"))], remainder_next=None)]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_UNDETERMINED
    assert st.pending_field == ("DE_Master", "Channel")
    assert st.current == "SPLIT-1" and "SPLIT-1" not in st.visited


def test_update_write_is_seen_by_a_later_split():
    acts = [
        update("UPD-1", "set", [("DE_Master", "Channel", "X")], "SPLIT-1"),
        decision("SPLIT-1", "s", [("a", "A", None, cond("DE_Master", "Channel", "Equal", "X"))], remainder_next=None),
    ]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "Y"}))
    assert st.steps == [Step("UPD-1", "UPD-1-out"), Step("SPLIT-1", "a")]
    assert st.writes[0].value == "X"


def test_datetime_now_write_uses_generation_time():
    acts = [update("UPD-1", "set", [("DE_Master", "Channel", "{{DateTime.Now}}")], None)]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({}))
    assert st.overrides[("DE_Master", "Channel")] == "2026-10-11 12:00"


def test_engagement_verdicts():
    emails = {"A1": email_asset("A1", "Demo", has_link=False)}
    acts = [email("EMAIL-1", "Demo", "A1", "ENG-1"),
            engagement("ENG-1", "open", "open", "EMAIL-1", None, None),
            engagement("ENG-2", "click", "click", "EMAIL-1", None, None),
            engagement("ENG-3", "open", "open", "EMAIL-9", None, None)]
    w, j = build_walker(acts, emails)
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_CHOICE and st.current == "ENG-1"
    assert graph.engagement_verdict(j.activities["ENG-1"], st, emails, j).possible
    v = graph.engagement_verdict(j.activities["ENG-2"], st, emails, j)
    assert not v.possible and "non contiene link" in v.reason
    v = graph.engagement_verdict(j.activities["ENG-3"], st, emails, j)
    assert not v.possible and "non viene inviata" in v.reason
    # taking an outcome after a choice continues the walk
    w.take(st, j.activities["ENG-1"], j.activities["ENG-1"].outcomes[1])
    assert st.stop == graph.STOP_EXIT and st.steps[-1] == Step("ENG-1", "branchResult-2")


def test_loop_and_dangling_are_reported():
    acts = [update("U-1", "a", [], "U-2"), update("U-2", "b", [], "U-1")]
    j = journey(acts)
    j.start_key = "U-1"
    w = graph.Walker(j, schema(), {}, False)
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_LOOP
    acts = [update("U-1", "a", [], "MISSING")]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_DANGLING
    assert any("attività mancante" in p for p in graph.structural_problems(j))


def test_unsupported_activity_stops_the_path():
    acts = [unsupported("X-1", "SALESCLOUDACTIVITY", "U-1"), update("U-1", "a", [], None)]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_UNSUPPORTED
    assert graph.activities_behind(j, {"X-1"}) == {"U-1"}


def test_observer_receives_values_of_the_split_fields():
    seen = []
    acts = [decision("SPLIT-1", "s", [("a", "A", None, cond("DE_Master", "Channel", "Equal", "X"))], remainder_next=None)]
    w, j = build_walker(acts)
    w.on_decision = lambda activity, values: seen.append((activity.key, values))
    w.run(w.start(), lookup_from({("DE_Master", "Channel"): "Y"}))
    assert seen == [("SPLIT-1", {("DE_Master", "Channel"): "Y"})]


def test_remainder_is_evaluated_last_whatever_its_position():
    from cjhelper.model import Outcome
    remainder = Outcome(key="remainder_path", label="Remainder", next="UPD-R", formula=None, is_remainder=True)
    yes = Outcome(key="yes", label="yes", next="UPD-Y", formula=cond("DE_Master", "Perm", "Is", "true"))
    split = decision("SPLIT-1", "s", [], remainder_next=None)
    split.outcomes = [remainder, yes]
    acts = [split, update("UPD-Y", "Y", [], None), update("UPD-R", "R", [], None)]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Master", "Perm"): "True"}))
    assert st.steps[0] == Step("SPLIT-1", "yes")
    st = w.run(w.start(), lookup_from({("DE_Master", "Perm"): "False"}))
    assert st.steps[0] == Step("SPLIT-1", "remainder_path")


def test_unsupported_outcome_stops_the_walk_only_when_it_is_reached():
    acts = [decision("SPLIT-1", "s", [("a", "A", "UPD-A", cond("DE_Master", "Perm", "Is", "true")),
                                      ("b", "B", "UPD-B", cond("DE_Master", "Channel", "Between", "x"))], remainder_next="UPD-R"),
            update("UPD-A", "A", [], None), update("UPD-B", "B", [], None), update("UPD-R", "R", [], None)]
    acts[0].outcomes[1].unsupported_reason = "uses the operator 'Between'"
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Master", "Perm"): "True"}))
    assert st.steps == [Step("SPLIT-1", "a"), Step("UPD-A", "UPD-A-out")]
    st = w.run(w.start(), lookup_from({("DE_Master", "Perm"): "False"}))
    assert st.stop == graph.STOP_UNSUPPORTED and st.steps == [Step("SPLIT-1", None)]
    assert "Between" in st.stop_detail


def test_between_and_row_present_conditions_are_evaluated():
    """Spec, "Extensions beyond the pilot": Between includes both bounds; the supported attribute comparison
    reads "the contact has a row", true when the linked key field holds a value."""
    from cjhelper.model import Condition

    row_present = Condition("DE_Master", "ContactId", "Equal", None, value_attribute="Event.EV.ContactId")
    acts = [decision("SPLIT-1", "s", [("a", "A", "UPD-A", cond("DE_Master", "Channel", "Between", "b|d")),
                                      ("b", "B", "UPD-B", row_present)], remainder_next="UPD-R"),
            update("UPD-A", "A", [], None), update("UPD-B", "B", [], None), update("UPD-R", "R", [], None)]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "d", ("DE_Master", "ContactId"): "TST_C1"}))
    assert st.steps[0] == Step("SPLIT-1", "a")
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "e", ("DE_Master", "ContactId"): "TST_C1"}))
    assert st.steps[0] == Step("SPLIT-1", "b")
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "a", ("DE_Master", "ContactId"): None}))
    assert st.steps[0] == Step("SPLIT-1", "remainder_path")
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "e"}))
    assert st.stop == graph.STOP_UNDETERMINED and st.pending_field == ("DE_Master", "ContactId")


# ---------------------------------------------------------------------------
# Spec, "Extensions beyond the pilot": Journey Data snapshot, row present or absent, waits
# ---------------------------------------------------------------------------

FORM = table("DE_Form", [field("ContactId", pk=True, nullable=False), field("Consent", "Boolean"),
                         field("Source", "Text", length=20)], sendable_field="ContactId")


def row_walker(acts):
    """A walker that knows DE_Form has the "row present / row absent" variable on ContactId."""
    j = journey(acts)
    tables = {t.name: t for t in (MASTER, ENTRY, FORM)}
    return graph.Walker(j, graph.Schema(tables), {}, False, datetime(2026, 10, 11, 12, 0),
                        row_fields={"DE_Form": ("DE_Form", "ContactId")}), j


def test_journey_data_reads_the_snapshot_taken_at_entry_and_contact_data_the_written_value():
    """Spec: Journey Data "is the snapshot taken at entry: later Update contact writes do not change it";
    "a later split that reads the same field sees the updated value" applies to Contact Data only."""
    from cjhelper.model import SOURCE_JOURNEY, Condition

    journey_tier = Condition("DE_Master", "Channel", "Equal", "Gold", source=SOURCE_JOURNEY)
    acts = [
        update("UPD-1", "set", [("DE_Master", "Channel", "Silver")], "SPLIT-1"),
        decision("SPLIT-1", "journey data", [("a", "A", "SPLIT-2", journey_tier)], remainder_next=None),
        decision("SPLIT-2", "contact data", [("a", "A", None, cond("DE_Master", "Channel", "Equal", "Silver"))],
                 remainder_next=None),
    ]
    w, j = build_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "Gold"}))
    assert st.steps == [Step("UPD-1", "UPD-1-out"), Step("SPLIT-1", "a"), Step("SPLIT-2", "a")]
    # the snapshot is used by evaluate only when it is given
    snapshot = lookup_from({("DE_Master", "Channel"): "Gold"})
    live = lookup_from({("DE_Master", "Channel"): "Silver"})
    assert graph.evaluate(journey_tier, live, schema(), False, snapshot) is True
    assert graph.evaluate(journey_tier, live, schema(), False) is False


def test_absent_row_reads_null_in_every_field():
    """Spec: "On an absent row, IsNull on any of its fields is true and every other operator is false"."""
    acts = [decision("SPLIT-1", "s", [("null", "N", "UPD-N", cond("DE_Form", "Source", "IsNull")),
                                      ("consent", "C", "UPD-C", cond("DE_Form", "Consent", "Is", "true"))],
                     remainder_next="UPD-R"),
            update("UPD-N", "N", [], None), update("UPD-C", "C", [], None), update("UPD-R", "R", [], None)]
    w, j = row_walker(acts)
    absent = lookup_from({("DE_Form", "ContactId"): None, ("DE_Form", "Source"): "Web", ("DE_Form", "Consent"): "True"})
    assert w.run(w.start(), absent).steps[0] == Step("SPLIT-1", "null")
    present = lookup_from({("DE_Form", "ContactId"): "TST_C1", ("DE_Form", "Source"): "Web",
                           ("DE_Form", "Consent"): "True"})
    assert w.run(w.start(), present).steps[0] == Step("SPLIT-1", "consent")
    # the same masking as a plain lookup, for callers that evaluate formulas themselves
    masked = graph.row_lookup(absent, {"de_form": ("DE_Form", "ContactId")})
    assert masked(("DE_Form", "Consent")) is None and masked(("DE_Master", "Perm")) is graph.UNKNOWN


def test_update_contact_inserts_an_absent_row():
    """Spec: "An Update contact on that table inserts the row: later reads see it present"; the fields it does
    not write read their default value, null when they have none (docs/assumptions.md, rows 20, 29 and 46)."""
    acts = [
        update("UPD-1", "record", [("DE_Form", "Source", "Journey")], "SPLIT-1"),
        decision("SPLIT-1", "still absent?", [("a", "A", None, cond("DE_Form", "ContactId", "IsNull"))],
                 remainder_next="SPLIT-2"),
        decision("SPLIT-2", "consent", [("a", "A", None, cond("DE_Form", "Consent", "IsNull"))], remainder_next=None),
    ]
    w, j = row_walker(acts)
    st = w.run(w.start(), lookup_from({("DE_Form", "ContactId"): None, ("DE_Form", "Consent"): "True"}))
    assert st.steps[1:] == [Step("SPLIT-1", "remainder_path"), Step("SPLIT-2", "a")]
    assert st.inserted == {"de_form"}
    # a row that was there keeps its other values
    st = w.run(w.start(), lookup_from({("DE_Form", "ContactId"): "TST_C1", ("DE_Form", "Consent"): "True"}))
    assert st.steps[1:] == [Step("SPLIT-1", "remainder_path"), Step("SPLIT-2", "remainder_path")]


def test_inserted_row_gives_unwritten_fields_their_default_and_may_fail_on_a_required_field():
    """The spec says only that the insert makes the row present. For the other fields the walker follows the
    assumptions listed for inserts (docs/assumptions.md, rows 19, 20, 29 and 46): an unwritten field reads its
    default value; an insert that leaves a non-nullable field without a default empty may fail, so the path is
    unknown from that Update contact on (spec, "Honest outcomes")."""
    form = table("DE_Form", [field("ContactId", pk=True, nullable=False), field("Welcomed", "Boolean", default="True"),
                             field("Seen", "Date", default="GETDATE()"), field("Source", "Text", length=20)],
                 sendable_field="ContactId")
    acts = [
        update("UPD-1", "record", [("DE_Form", "Source", "Journey")], "SPLIT-1"),
        decision("SPLIT-1", "welcomed?", [("a", "A", "SPLIT-2", cond("DE_Form", "Welcomed", "Is", "true"))],
                 remainder_next=None),
        decision("SPLIT-2", "seen?", [("a", "A", None, cond("DE_Form", "Seen", "IsNotNull"))], remainder_next=None),
    ]
    j = journey(acts)

    def walker(form_table):
        tables = {t.name: t for t in (MASTER, ENTRY, form_table)}
        return graph.Walker(j, graph.Schema(tables), {}, False, datetime(2026, 10, 11, 12, 0),
                            row_fields={"DE_Form": ("DE_Form", "ContactId")})

    absent = lookup_from({("DE_Form", "ContactId"): None, ("DE_Form", "Welcomed"): None, ("DE_Form", "Seen"): None})
    w = walker(form)
    st = w.run(w.start(), absent)
    assert st.stop == graph.STOP_EXIT and st.steps[1:] == [Step("SPLIT-1", "a"), Step("SPLIT-2", "a")]
    assert w.inserted_default(("DE_Form", "Seen")) == "2026-10-11 12:00"
    # A required field the update does not write: the insert may fail, the path is unknown from UPD-1 on.
    strict = table("DE_Form", [field("ContactId", pk=True, nullable=False), field("Welcomed", "Boolean", default="True"),
                               field("Seen", "Date", default="GETDATE()"), field("Source", "Text", length=20),
                               field("Channel", "Text", nullable=False)], sendable_field="ContactId")
    w = walker(strict)
    st = w.run(w.start(), absent)
    assert st.stop == graph.STOP_UNSUPPORTED and st.steps == [Step("UPD-1", None)] and st.current == "UPD-1"
    assert "Channel" in st.stop_detail and "può fallire" in st.stop_detail and not st.writes
    # A row that is there is updated, not inserted: nothing can fail.
    st = w.run(w.start(), lookup_from({("DE_Form", "ContactId"): "TST_C1", ("DE_Form", "Welcomed"): "False",
                                       ("DE_Form", "Seen"): None}))
    assert st.stop == graph.STOP_EXIT and st.steps[1:] == [Step("SPLIT-1", "remainder_path")]


def test_fixed_wait_minutes_and_case_paths():
    from tests.helpers import wait_attr, wait_dur
    from cjhelper.model import KIND_WAIT_STO, Activity, Case, RandomBranch, RandomInfo

    assert graph.fixed_wait_minutes(wait_dur("W-1", 5, "MINUTES", None)) == 5
    assert graph.fixed_wait_minutes(wait_dur("W-2", 2, "DAYS", None)) == 2 * 24 * 60
    assert graph.fixed_wait_minutes(wait_dur("W-3", 1, "Hour", None)) == 60
    assert graph.fixed_wait_minutes(wait_dur("W-4", 1, "MONTHS", None)) == 28 * 24 * 60   # the shortest month
    assert graph.fixed_wait_minutes(wait_dur("W-5", 3, "FORTNIGHTS", None)) is None
    assert graph.fixed_wait_minutes(wait_attr("W-6", "DE_Master", "Channel", None)) == 0
    assert graph.fixed_wait_minutes(Activity("STO-1", "STOWAIT", KIND_WAIT_STO, "", [], wait_amount=24)) == 0
    branch = RandomBranch("b", 50, (Step("U-2", None),), (), (), "U-2")
    case = Case("S01-C01", "S01", 1, {}, [Step("U-1", "o"), Step("R-1", None)], [], [], [], [], "R-1", 50,
                random=RandomInfo("R-1", [branch], 1, 9))
    assert graph.case_paths(case) == [["U-1", "R-1", "U-2"]]


# ---------------------------------------------------------------------------
# Spec, "Extensions for time-dependent journeys (step 3)": the clock, waits, exit criteria, SMS, fixed delays
# ---------------------------------------------------------------------------

ENTRY_TIME = datetime(2026, 10, 11, 12, 0)
DATED = table("DE_Dated", [field("ContactId", pk=True, nullable=False), field("EndDate", "Date"),
                           field("Status", "Text", length=20)], sendable_field="ContactId")
END_DATE = ("DE_Dated", "EndDate")
STATUS = ("DE_Dated", "Status")


def clock_walker(acts, exits=(), entry_time=None, predicate=None, tables=(MASTER, ENTRY, DATED), row_fields=None):
    j = journey(acts)
    j.exits = list(exits)
    w = graph.Walker(j, graph.Schema({t.name: t for t in tables}), {}, False, ENTRY_TIME, entry_time=entry_time,
                     ends_at_once_when_unknown=predicate, row_fields=row_fields)
    return w, j


def sto(key, next_key):
    return Activity(key=key, raw_type="STOWAIT", kind=KIND_WAIT_STO, name="sto",
                    outcomes=[Outcome(key=key + "-out", label="", next=next_key)], wait_amount=24, wait_unit="HOURS")


def sms(key, next_key, with_outcome=True):
    outcomes = [Outcome(key=key + "-out", label="", next=next_key)] if with_outcome else []
    return Activity(key=key, raw_type="SMSSYNC", kind=KIND_SMS, name=key, outcomes=outcomes, sms_name=key)


def offset_wait(key, next_key, amount=None, unit=None, time_of_day=None):
    act = wait_attr(key, "DE_Dated", "EndDate", next_key)
    act.wait_offset_amount, act.wait_offset_unit, act.wait_time_of_day = amount, unit, time_of_day
    return act


def relative(table_name, field_name, operator, text):
    return Condition(table_name, field_name, operator, text, relative=criteria.relative_date(text))


def times(state):
    """Activity key -> the clock of its step."""
    return {p.activity_key: p.at for p in state.timeline}


def assert_timeline_matches_steps(state):
    assert len(state.timeline) == len(state.steps)
    assert [(p.activity_key, p.outcome_key) for p in state.timeline] == [(s.activity_key, s.outcome_key)
                                                                         for s in state.steps]


def test_clock_starts_at_the_entry_time_and_waits_by_duration_advance_it():
    """Spec, step 3: "A wait by duration advances the clock by its amount (a month counts 28 days, as before)";
    "A send-time wait advances the clock by nothing"; the clock starts at the entry time."""
    acts = [wait_dur("W-1", 2, "DAYS", "W-2"), wait_dur("W-2", 3, "HOURS", "W-3"), wait_dur("W-3", 1, "MONTHS", "W-4"),
            wait_dur("W-4", 30, "MINUTES", "STO-1"), sto("STO-1", "W-5"), wait_dur("W-5", 1, "Week", "U-1"),
            update("U-1", "u", [], None)]
    w, j = clock_walker(acts)
    st = w.start()
    assert st.clock == ENTRY_TIME
    st = w.run(st, lookup_from({}))
    assert st.stop == graph.STOP_EXIT and st.waits == ["W-1", "W-2", "W-3", "W-4", "STO-1", "W-5"]
    assert_timeline_matches_steps(st)
    assert times(st) == {
        "W-1": datetime(2026, 10, 13, 12, 0), "W-2": datetime(2026, 10, 13, 15, 0),
        "W-3": datetime(2026, 11, 10, 15, 0),            # 28 days, not the calendar month
        "W-4": datetime(2026, 11, 10, 15, 30), "STO-1": datetime(2026, 11, 10, 15, 30),
        "W-5": datetime(2026, 11, 17, 15, 30), "U-1": datetime(2026, 11, 17, 15, 30),
    }
    assert st.clock == datetime(2026, 11, 17, 15, 30)
    # The entry time is a setting: the clock starts there, not at the generation time.
    w, j = clock_walker(acts, entry_time=datetime(2026, 10, 20, 8, 0))
    st = w.run(w.start(), lookup_from({}))
    assert st.timeline[0].at == datetime(2026, 10, 22, 8, 0)
    # A duration that cannot be read advances the clock by nothing (it counts 0 in fixed_delays too).
    w, j = clock_walker([wait_dur("W-1", 3, "FORTNIGHTS", "U-1"), update("U-1", "u", [], None)])
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_EXIT and times(st) == {"W-1": ENTRY_TIME, "U-1": ENTRY_TIME}


def test_wait_end_applies_the_offset_and_the_time_of_day():
    """Spec, step 3: "until the date in its field plus its offset (waitDuration and waitUnit, which may be
    negative ...), at the time of day given by specifiedTime when it has one, otherwise at the time of day held
    in the field"."""
    day = datetime(2026, 10, 26, 18, 45)
    assert graph.wait_end(offset_wait("W", None), day) == day
    assert graph.wait_end(offset_wait("W", None, 2, "DAYS"), day) == datetime(2026, 10, 28, 18, 45)
    assert graph.wait_end(offset_wait("W", None, -13, "DAYS"), day) == datetime(2026, 10, 13, 18, 45)
    assert graph.wait_end(offset_wait("W", None, -13, "days", "09:30"), day) == datetime(2026, 10, 13, 9, 30)
    assert graph.wait_end(offset_wait("W", None, None, None, "7:05"), datetime(2026, 10, 20, 23, 59, 30)) \
        == datetime(2026, 10, 20, 7, 5)
    assert graph.wait_end(offset_wait("W", None, -2, "HOURS"), day) == datetime(2026, 10, 26, 16, 45)
    assert graph.wait_end(offset_wait("W", None, 1, "WEEK"), day) == datetime(2026, 11, 2, 18, 45)
    # Calendar months for the offset (criteria.shift_date), clamped to the end of a shorter month.
    assert graph.wait_end(offset_wait("W", None, 1, "MONTHS"), datetime(2026, 1, 31, 10, 0)) \
        == datetime(2026, 2, 28, 10, 0)
    with pytest.raises(criteria.UnsupportedOperator):
        graph.wait_end(offset_wait("W", None, 3, "FORTNIGHTS"), day)
    with pytest.raises(criteria.UnsupportedOperator):
        graph.wait_end(offset_wait("W", None, None, None, "half past nine"), day)


def run_attr_wait(value, amount=None, unit=None, time_of_day=None, predicate=None):
    w, j = clock_walker([offset_wait("W-1", "U-1", amount, unit, time_of_day), update("U-1", "u", [], None)],
                        predicate=predicate)
    values = {} if value is graph.UNKNOWN else {END_DATE: value}
    return w.run(w.start(), lookup_from(values)), w


def test_wait_by_attribute_holds_the_contact_until_its_date():
    """Spec, step 3: the wait ends at the date plus the offset; "When that moment is already past, the contact goes
    on at once"; "When the field is null, the contact goes on at once (assumption)"."""
    st, _ = run_attr_wait("2026-10-20 08:15", 2, "DAYS")
    assert st.stop == graph.STOP_EXIT and st.waits == ["W-1"]
    assert times(st) == {"W-1": datetime(2026, 10, 22, 8, 15), "U-1": datetime(2026, 10, 22, 8, 15)}
    assert_timeline_matches_steps(st)
    st, _ = run_attr_wait("2026-10-26 12:00", -13, "DAYS")
    assert times(st)["W-1"] == datetime(2026, 10, 13, 12, 0)
    st, _ = run_attr_wait("2026-10-26 12:00", -13, "DAYS", "09:30")
    assert times(st)["W-1"] == datetime(2026, 10, 13, 9, 30)
    st, _ = run_attr_wait("2026-10-20 18:45", None, None, "09:30")
    assert times(st)["W-1"] == datetime(2026, 10, 20, 9, 30)
    st, _ = run_attr_wait("2026-10-20")                      # a date alone: midnight
    assert times(st)["W-1"] == datetime(2026, 10, 20, 0, 0)
    # Already past, by the date itself or once the negative offset is applied: the contact goes on at once.
    for value, amount in (("2026-10-01 12:00", None), ("2026-10-22 12:00", -13)):
        st, _ = run_attr_wait(value, amount, "DAYS")
        assert st.stop == graph.STOP_EXIT and times(st)["W-1"] == ENTRY_TIME
    # A time of day that falls before the clock on the same day is past too.
    st, _ = run_attr_wait("2026-10-11 23:00", None, None, "09:00")
    assert times(st)["W-1"] == ENTRY_TIME
    # Null, or a value that is not a readable date: the wait ends at once and the walk goes on.
    for value in (None, "", "not a date"):
        st, _ = run_attr_wait(value, -13, "DAYS")
        assert st.stop == graph.STOP_EXIT and st.waits == ["W-1"] and times(st)["W-1"] == ENTRY_TIME
    # An offset unit the generator does not know: the end of the wait cannot be computed, the path is unknown.
    st, _ = run_attr_wait("2026-10-26 12:00", 3, "FORTNIGHTS")
    assert st.stop == graph.STOP_ERROR and st.steps == [Step("W-1", None)] and st.current == "W-1"
    assert "FORTNIGHTS" in st.stop_detail
    assert_timeline_matches_steps(st)


def test_wait_by_attribute_on_an_undecided_field():
    """Without a predicate the wait ends at once (the behaviour before step 3); with it, the caller decides: True
    ends the wait at once, False stops the walk on the field so the caller can branch on its values."""
    st, _ = run_attr_wait(graph.UNKNOWN, -13, "DAYS")
    assert st.stop == graph.STOP_EXIT and times(st)["W-1"] == ENTRY_TIME
    asked = []
    st, _ = run_attr_wait(graph.UNKNOWN, -13, "DAYS", predicate=lambda ref: asked.append(ref) or True)
    assert st.stop == graph.STOP_EXIT and times(st)["W-1"] == ENTRY_TIME and asked == [END_DATE]
    st, w = run_attr_wait(graph.UNKNOWN, -13, "DAYS", predicate=lambda ref: False)
    assert st.stop == graph.STOP_UNDETERMINED and st.pending_field == END_DATE
    assert st.current == "W-1" and "W-1" not in st.visited
    assert st.steps == [] and st.timeline == [] and st.waits == [] and st.clock == ENTRY_TIME
    # The caller assigns the field and re-runs the wait from a copy of the state.
    child = w.run(st.clone(), lookup_from({END_DATE: "2026-10-26 12:00"}))
    assert child.stop == graph.STOP_EXIT and child.waits == ["W-1"]
    assert times(child) == {"W-1": datetime(2026, 10, 13, 12, 0), "U-1": datetime(2026, 10, 13, 12, 0)}


def test_wait_by_attribute_reads_the_value_an_update_contact_wrote():
    """Contact Data is read live: an Update contact that writes the date moves the end of the wait."""
    acts = [update("U-0", "set", [("DE_Dated", "EndDate", "2026-10-30 07:00")], "W-1"),
            offset_wait("W-1", "U-1"), update("U-1", "u", [], None)]
    w, j = clock_walker(acts)
    st = w.run(w.start(), lookup_from({END_DATE: "2026-10-12 12:00"}))
    assert times(st)["W-1"] == datetime(2026, 10, 30, 7, 0)


def test_relative_date_decision_reads_the_clock_after_a_wait():
    """Spec, step 3: a relative date "compares the field with the clock's date moved by n units". A field one day
    after the entry time is "After Today" at entry, and no longer after a wait of two days."""
    after_today = relative("DE_Dated", "EndDate", "After", ";+;0;days")
    split = decision("SPLIT-1", "after today?", [("a", "A", "U-A", after_today)], remainder_next="U-R")
    tail = [update("U-A", "A", [], None), update("U-R", "R", [], None)]
    lookup = lookup_from({END_DATE: "2026-10-12 12:00"})
    w, j = clock_walker([split] + tail)
    st = w.run(w.start(), lookup)
    assert st.steps[0] == Step("SPLIT-1", "a") and times(st)["SPLIT-1"] == ENTRY_TIME
    split = decision("SPLIT-1", "after today?", [("a", "A", "U-A", after_today)], remainder_next="U-R")
    w, j = clock_walker([wait_dur("W-1", 2, "DAYS", "SPLIT-1"), split] + tail)
    st = w.run(w.start(), lookup)
    assert st.steps[1] == Step("SPLIT-1", "remainder_path")
    assert times(st)["SPLIT-1"] == datetime(2026, 10, 13, 12, 0)
    # evaluate passes the clock to the criteria; without it a relative date cannot be evaluated.
    s = graph.Schema({t.name: t for t in (DATED,)})
    assert graph.evaluate(after_today, lookup, s, False, now=ENTRY_TIME) is True
    assert graph.evaluate(after_today, lookup, s, False, now=datetime(2026, 10, 13, 12, 0)) is False
    with pytest.raises(criteria.UnsupportedOperator):
        graph.evaluate(after_today, lookup, s, False)


def gone(source="contact", name="EXITCRITERIA"):
    formula = Condition("DE_Dated", "Status", "Equal", "Gone", source=source)
    return JourneyFilter(key=name, name=name, kind="exit", formula=formula, description="Status is Gone")


def exit_journey(first=None):
    """[U-0 writes Status = Gone] -> W-1 (1 day) -> U-1."""
    acts = [wait_dur("W-1", 1, "DAYS", "U-1"), update("U-1", "u", [], None)]
    if first is not None:
        acts.insert(0, first)
    return acts


def test_exit_criteria_are_evaluated_at_the_end_of_a_wait():
    """Spec, step 3: "At the end of every wait on a path the exit criteria are evaluated with the clock; when they
    are true the contact leaves the journey there", with the data sources of a decision split."""
    write_gone = update("U-0", "set", [("DE_Dated", "Status", "Gone")], "W-1")
    # Contact Data, read live: the value the Update contact wrote meets the criterion.
    w, j = clock_walker(exit_journey(write_gone), exits=[gone()])
    st = w.run(w.start(), lookup_from({STATUS: "Active"}))
    assert st.stop == graph.STOP_EXIT_CRITERIA and st.current is None and st.exit_by_criteria == "W-1"
    assert st.steps == [Step("U-0", "U-0-out"), Step("W-1", None)] and st.waits == ["W-1"]
    assert st.timeline[-1] == TimelinePoint("W-1", None, datetime(2026, 10, 12, 12, 0))
    assert_timeline_matches_steps(st)
    assert "Status is Gone" in st.stop_detail and "W-1" in st.stop_detail
    # Journey Data reads the snapshot taken at entry: the write does not change it, the contact stays.
    w, j = clock_walker(exit_journey(write_gone), exits=[gone(SOURCE_JOURNEY)])
    st = w.run(w.start(), lookup_from({STATUS: "Active"}))
    assert st.stop == graph.STOP_EXIT and st.exit_by_criteria is None
    assert st.steps[1:] == [Step("W-1", "W-1-out"), Step("U-1", "U-1-out")]
    # ... and it meets the criterion when the entry row holds the value.
    st = w.run(w.start(), lookup_from({STATUS: "Gone"}))
    assert st.stop == graph.STOP_EXIT_CRITERIA
    # False: the walk goes on.
    w, j = clock_walker(exit_journey(), exits=[gone()])
    st = w.run(w.start(), lookup_from({STATUS: "Active"}))
    assert st.stop == graph.STOP_EXIT and st.steps == [Step("W-1", "W-1-out"), Step("U-1", "U-1-out")]


def test_exit_criteria_on_an_undecided_field_stop_the_walk_before_the_wait_ends():
    w, j = clock_walker(exit_journey(), exits=[gone()])
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_UNDETERMINED and st.pending_field == STATUS
    assert st.current == "W-1" and "W-1" not in st.visited
    assert st.steps == [] and st.waits == [] and st.clock == ENTRY_TIME
    # The caller assigns the field and re-runs the wait: the clock moves once.
    child = w.run(st.clone(), lookup_from({STATUS: "Gone"}))
    assert child.stop == graph.STOP_EXIT_CRITERIA and child.waits == ["W-1"]
    assert child.timeline == [TimelinePoint("W-1", None, datetime(2026, 10, 12, 12, 0))]
    child = w.run(st.clone(), lookup_from({STATUS: "Active"}))
    assert child.stop == graph.STOP_EXIT and child.clock == datetime(2026, 10, 12, 12, 0)
    # A criterion that is true decides whatever the others still need.
    unknown_first = JourneyFilter("EX-1", "EX-1", "exit", cond("DE_Master", "Channel", "Equal", "X"))
    w, j = clock_walker(exit_journey(), exits=[unknown_first, gone(name="EX-2")])
    st = w.run(w.start(), lookup_from({STATUS: "Gone"}))
    assert st.stop == graph.STOP_EXIT_CRITERIA and "EX-2" in st.stop_detail


def test_exit_criteria_are_never_evaluated_without_a_wait_and_only_when_evaluable():
    """Exit criteria are checked "when the contact's time in a wait activity expires": not elsewhere. Criteria
    the generator cannot evaluate are reported, not modelled."""
    always = JourneyFilter("EX-1", "EX-1", "exit", cond("DE_Dated", "Status", "IsNull"))
    w, j = clock_walker([update("U-1", "a", [], "U-2"), update("U-2", "b", [], None)], exits=[always, gone()])
    st = w.run(w.start(), lookup_from({}))      # the criteria would be true, or need a value, if evaluated
    assert st.stop == graph.STOP_EXIT and st.exit_by_criteria is None
    not_modelled = JourneyFilter("EX-1", "EX-1", "exit", cond("DE_Dated", "Status", "IsNull"),
                                 unsupported_reason="relationship missing in the export")
    no_formula = JourneyFilter("EX-2", "EX-2", "exit", None, unsupported_reason="nessun criterio")
    w, j = clock_walker(exit_journey(), exits=[not_modelled, no_formula])
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_EXIT and st.steps[0] == Step("W-1", "W-1-out")
    assert graph.evaluable_exits(j) == []
    # An operator the PoC cannot evaluate (the loader marks such criteria unsupported): the path is unknown.
    odd = JourneyFilter("EX-3", "EX-3", "exit", cond("DE_Dated", "Status", "Weird", "x"))
    w, j = clock_walker(exit_journey(), exits=[odd])
    st = w.run(w.start(), lookup_from({STATUS: "Active"}))
    assert st.stop == graph.STOP_ERROR and st.steps == [Step("W-1", None)] and "Weird" in st.stop_detail
    assert_timeline_matches_steps(st)


def test_exit_criteria_read_the_clock_at_the_end_of_each_wait():
    """A relative date in the exit criteria compares with the clock when the wait ends: a send-time wait ends at
    once and keeps the contact, the wait of two days that follows takes it out."""
    expired = JourneyFilter("EXIT", "EXIT", "exit", relative("DE_Dated", "EndDate", "Before", ";+;0;days"),
                            description="EndDate before today")
    acts = [sto("STO-1", "W-1"), wait_dur("W-1", 2, "DAYS", "U-1"), update("U-1", "u", [], None)]
    w, j = clock_walker(acts, exits=[expired])
    st = w.run(w.start(), lookup_from({END_DATE: "2026-10-12 12:00"}))
    assert st.steps == [Step("STO-1", "STO-1-out"), Step("W-1", None)] and st.exit_by_criteria == "W-1"
    assert times(st) == {"STO-1": ENTRY_TIME, "W-1": datetime(2026, 10, 13, 12, 0)}
    # After a wait by attribute too.
    acts = [offset_wait("W-1", "U-1", 1, "DAYS"), update("U-1", "u", [], None)]
    w, j = clock_walker(acts, exits=[expired])
    st = w.run(w.start(), lookup_from({END_DATE: "2026-10-12 12:00"}))
    assert st.stop == graph.STOP_EXIT_CRITERIA and times(st) == {"W-1": datetime(2026, 10, 13, 12, 0)}


def test_timeline_has_one_point_per_step_with_the_clock():
    """Spec, step 3: "Every case carries its timeline: the clock at every step of the expected path"; an email
    and an SMS are sent and an engagement split is evaluated at the clock time."""
    emails = {"A1": email_asset("A1", "Demo", has_link=False)}
    acts = [decision("SPLIT-1", "s", [("a", "A", "EMAIL-1", cond("DE_Master", "Channel", "Equal", "X"))],
                     remainder_next=None),
            email("EMAIL-1", "Demo", "A1", "W-1"), wait_dur("W-1", 1, "DAYS", "ENG-1"),
            engagement("ENG-1", "open", "open", "EMAIL-1", "SMS-1", None), sms("SMS-1", "W-2"),
            wait_dur("W-2", 2, "HOURS", "U-1"), update("U-1", "u", [], None)]
    j = journey(acts)
    w = graph.Walker(j, schema(), emails, False, ENTRY_TIME)
    st = w.run(w.start(), lookup_from({("DE_Master", "Channel"): "X"}))
    assert st.stop == graph.STOP_CHOICE and st.current == "ENG-1"
    w.take(st, j.activities["ENG-1"], j.activities["ENG-1"].outcomes[0])
    w.run(st, lookup_from({("DE_Master", "Channel"): "X"}))
    day1, later = datetime(2026, 10, 12, 12, 0), datetime(2026, 10, 12, 14, 0)
    assert st.timeline == [
        TimelinePoint("SPLIT-1", "a", ENTRY_TIME), TimelinePoint("EMAIL-1", "EMAIL-1-out", ENTRY_TIME),
        TimelinePoint("W-1", "W-1-out", day1), TimelinePoint("ENG-1", "branchResult-1", day1),
        TimelinePoint("SMS-1", "SMS-1-out", day1), TimelinePoint("W-2", "W-2-out", later),
        TimelinePoint("U-1", "U-1-out", later),
    ]
    assert_timeline_matches_steps(st)
    assert st.emails_sent == ["EMAIL-1"] and st.sms_sent == ["SMS-1"]
    # A copy carries the clock and keeps its own lists.
    copy = st.clone()
    assert copy.clock == later and copy.timeline == st.timeline and copy.sms_sent == ["SMS-1"]
    copy.timeline.append(TimelinePoint("X", None, later))
    copy.sms_sent.append("X")
    assert len(st.timeline) == 7 and st.sms_sent == ["SMS-1"]


def test_sms_is_a_send_without_branching():
    """Spec, step 3, "SMS sends": "SMSSYNC is a send ... It has no branching"."""
    w, j = clock_walker([sms("SMS-1", "U-1"), update("U-1", "u", [], None)])
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_EXIT and st.sms_sent == ["SMS-1"] and st.emails_sent == []
    assert st.steps == [Step("SMS-1", "SMS-1-out"), Step("U-1", "U-1-out")]
    assert not graph.is_split(j.activities["SMS-1"])
    # Without an outcome the contact leaves the journey there, as after an email with none.
    w, j = clock_walker([sms("SMS-1", None, with_outcome=False)])
    st = w.run(w.start(), lookup_from({}))
    assert st.stop == graph.STOP_EXIT and st.steps == [Step("SMS-1", None)] and st.sms_sent == ["SMS-1"]
    assert st.exit_by_criteria is None


def test_datetime_now_written_by_an_update_contact_is_the_clock():
    """Spec, step 3: "{{DateTime.Now}} written by an Update contact is the clock time"."""
    acts = [wait_dur("W-1", 2, "DAYS", "UPD-1"),
            update("UPD-1", "set", [("DE_Master", "Channel", "{{DateTime.Now}}")], None)]
    w, j = clock_walker(acts)
    st = w.run(w.start(), lookup_from({}))
    assert st.overrides[("DE_Master", "Channel")] == "2026-10-13 12:00"
    assert st.writes[0].value == "2026-10-13 12:00"
    w, j = clock_walker(acts[1:], entry_time=datetime(2026, 10, 20, 8, 0))
    st = w.run(w.start(), lookup_from({}))
    assert st.overrides[("DE_Master", "Channel")] == "2026-10-20 08:00"


def test_current_date_default_of_an_inserted_row_is_the_clock_at_the_insert():
    form = table("DE_Form", [field("ContactId", pk=True, nullable=False), field("Seen", "Date", default="GETDATE()"),
                             field("Source", "Text", length=20)], sendable_field="ContactId")
    acts = [wait_dur("W-1", 1, "DAYS", "UPD-1"), update("UPD-1", "record", [("DE_Form", "Source", "Journey")], "W-2"),
            wait_dur("W-2", 1, "DAYS", None)]
    w, j = clock_walker(acts, tables=(MASTER, ENTRY, form), row_fields={"DE_Form": ("DE_Form", "ContactId")})
    lookup = lookup_from({("DE_Form", "ContactId"): None, ("DE_Form", "Seen"): None})
    st = w.run(w.start(), lookup)
    assert st.clock == datetime(2026, 10, 13, 12, 0)
    assert w.value_of(st, ("DE_Form", "Seen"), lookup) == "2026-10-12 12:00"


def test_fixed_delays_on_a_diamond():
    """Spec, step 3: the cut points use "every fixed delay with which a contact can reach that split (the sum of
    the waits by duration on each path from the start, waits by attribute counting zero)"."""
    acts = [decision("D-1", "s", [("a", "A", "W-A", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="WA-1"),
            wait_dur("W-A", 2, "DAYS", "J-1"),
            offset_wait("WA-1", "W-B", -13, "DAYS"), wait_dur("W-B", 3, "HOURS", "STO-1"), sto("STO-1", "J-1"),
            update("J-1", "join", [], "W-C"), wait_dur("W-C", 1, "DAYS", "END"), update("END", "end", [], None),
            update("ORPHAN", "never reached", [], "END")]
    j = journey(acts)
    j.start_key = "D-1"
    delays, warnings = graph.fixed_delays(j)
    assert warnings == []
    assert delays == {"D-1": {0.0}, "W-A": {0.0}, "WA-1": {0.0}, "W-B": {0.0}, "STO-1": {0.125}, "J-1": {2.0, 0.125},
                      "W-C": {2.0, 0.125}, "END": {3.0, 1.125}, "ORPHAN": set()}


def test_fixed_delays_report_an_unknown_duration_once_and_cap_the_sets():
    acts = [decision("D-1", "s", [("a", "A", "W-A", cond("DE_Master", "Perm", "Is", "true"))], remainder_next="J-1"),
            wait_dur("W-A", 1, "DAYS", "J-1"), update("J-1", "join", [], "W-X"),
            wait_dur("W-X", 3, "FORTNIGHTS", "END"), update("END", "end", [], None)]
    delays, warnings = graph.fixed_delays(journey(acts))
    assert delays["END"] == {0.0, 1.0}
    assert len(warnings) == 1 and "W-X" in warnings[0] and "FORTNIGHTS" in warnings[0]
    # Six splits in a row, each with or without a wait of 2^i minutes: 64 delays reach the end, 32 are kept.
    acts = []
    for i in range(6):
        nxt = f"D-{i + 1}" if i < 5 else "END"
        acts += [decision(f"D-{i}", "s", [("a", "A", f"W-{i}", cond("DE_Master", "Perm", "Is", "true"))],
                          remainder_next=nxt),
                 wait_dur(f"W-{i}", 2 ** i, "MINUTES", nxt)]
    acts.append(update("END", "end", [], None))
    delays, warnings = graph.fixed_delays(journey(acts))
    assert len(delays["D-5"]) == 32 and delays["END"] == {m / 1440 for m in range(32)}
    assert len(warnings) == 1 and "'END'" in warnings[0] and "32" in warnings[0]


def test_fixed_delays_do_not_follow_a_loop():
    acts = [update("U-1", "a", [], "W-1"), wait_dur("W-1", 1, "DAYS", "U-2"), update("U-2", "b", [], "U-1")]
    j = journey(acts)
    j.start_key = "U-1"
    delays, warnings = graph.fixed_delays(j)
    assert delays == {"U-1": {0.0}, "W-1": {0.0}, "U-2": {1.0}} and warnings == []
