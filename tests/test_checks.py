"""Trust checks (cjhelper/checks.py): each check passes on a clean run and fails on the mutation it must catch.

One small world (examples/two_levels: Master -> Target -> Config, 4 scenarios, 6 cases, 18 contacts) is
generated once; every test mutates a copy of its files or of its result objects and runs the checks again.
The extensions beyond the pilot (rows present or absent, entries no query fills) use a hand-made world.
"""

from __future__ import annotations

import copy
import csv
import io
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cjhelper import checks, coverage, datagen, outputs, pipeline
from cjhelper.model import (ENTRY_AUTOMATION, ENTRY_RUN_ONCE, ENTRY_SCHEDULED, LEVEL_CONTACT, Field, Settings,
                            Step)
from tests.helpers import cond, decision, field, journey, package, table, table_model, update

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "two_levels" / "package.json"

# The order of Checker.run_all.
SCHEMA, JOINS, ENTRY, SIMULATION, COVERAGE, ADDRESSES = range(6)

C1 = "TST_R01_C0001"   # first contact of case S01-C01 (scenario TST_R01_S01, MarketingPermission True)


@pytest.fixture(scope="module")
def world():
    settings = Settings(generation_time=datetime(2026, 10, 10, 9, 0), real_inbox="qa@test.com")
    return pipeline.generate(str(EXAMPLE), settings, with_testbook=False)


def run(world, files=None, coverage=None, data=None):
    return checks.run_checks(world.package, world.table_model, coverage or world.coverage, data or world.data,
                             files if files is not None else dict(world.files), world.settings,
                             world.generation_time)


def edit(world, table, change, files=None):
    """A copy of the files (``world.files`` by default) where ``change(rows)`` has edited the rows of
    ``<table>.csv`` in place."""
    files = dict(files if files is not None else world.files)
    reader = csv.DictReader(io.StringIO(files[f"{table}.csv"].decode("utf-8"), newline=""))
    rows = list(reader)
    change(rows)
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=reader.fieldnames, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    files[f"{table}.csv"] = out.getvalue().encode("utf-8")
    return files


def set_value(key_field, key, field, value):
    def change(rows):
        for row in rows:
            if row[key_field] == key:
                row[field] = value
    return change


def drop(key_field, key):
    def change(rows):
        rows[:] = [row for row in rows if row[key_field] != key]
    return change


def failed(results, index):
    check = results[index]
    assert not check.passed, f"{check.name} should fail"
    return " | ".join(check.details)


def test_clean_run_passes_every_check(world):
    assert all(c.passed for c in world.checks), [(c.name, c.details) for c in world.checks if not c.passed]
    assert all(c.passed for c in run(world))
    assert [c.name for c in world.checks] == [
        "I valori rispettano tipo, lunghezza, null e chiave primaria",
        "Ogni chiave trova le sue righe nelle altre tabelle",
        "Le righe della tabella di ingresso corrispondono alla entry query",
        "Ogni contatto simulato dai file segue il percorso previsto",
        "Ogni branch raggiungibile è coperto e ogni branch non coperto ha un motivo",
        "Solo i contatti con un'azione manuale usano l'inbox reale, gli altri hanno indirizzi non recapitabili",
        "Due run con lo stesso input danno gli stessi file",
    ]


@pytest.mark.parametrize("table, field, value, words", [
    ("DE_Master", "FirstName", "x" * 51, "ha 51 caratteri, lo schema ne ammette 50"),   # Text(50)
    ("DE_Master", "MobilePhone", "333 abc", "non è un numero di telefono"),
    ("DE_Config", "IGNOREPERM", "Yes", "non è True o False"),
])
def test_schema_catches_length_and_type(world, table, field, value, words):
    key_field = "CampaignName" if table == "DE_Config" else "ContactId"
    key = "TST_R01_S02" if table == "DE_Config" else C1
    detail = failed(run(world, edit(world, table, set_value(key_field, key, field, value))), SCHEMA)
    assert field in detail and words in detail


@pytest.mark.parametrize("value", ["2026-10-10", "10/10/2026 09:00"])
def test_schema_catches_a_date_in_another_format(world, value):
    files = edit(world, "DE_Target", set_value("SubscriberId", C1, "CreatedDate", value))
    detail = failed(run(world, files), SCHEMA)
    assert "CreatedDate" in detail and "non è una data nel formato %Y-%m-%d %H:%M" in detail


def test_joins_catch_a_target_row_without_config_row(world):
    files = edit(world, "DE_Target", set_value("SubscriberId", C1, "CampaignName", "TST_R01_S99"))
    detail = failed(run(world, files), JOINS)
    assert "valori di DE_Target.CampaignName non hanno una riga in DE_Config.CampaignName" in detail
    assert "TST_R01_S99" in detail


def test_joins_catch_a_contact_in_two_scenarios(world):
    files = edit(world, "DE_Target", lambda rows: rows.append(
        {"SubscriberId": C1, "CampaignName": "TST_R01_S02", "CreatedDate": rows[0]["CreatedDate"]}))
    detail = failed(run(world, files), JOINS)
    assert f"il contatto {C1} è collegato a 2 righe di scenario" in detail and "più righe in DE_Target" in detail


def test_joins_and_coverage_catch_contacts_missing_from_the_files(world):
    files = None
    for table, key_field in (("DE_Entry", "ContactId"), ("DE_Master", "ContactId"), ("DE_Target", "SubscriberId")):
        files = edit(world, table, drop(key_field, C1), files)
    results = run(world, files)
    assert "contatti generati non hanno righe in DE_Master" in failed(results, JOINS)
    detail = failed(results, COVERAGE)
    assert C1 in detail and "contatti non hanno righe in DE_Entry.csv" in detail


def test_joins_catch_a_row_of_a_contact_that_was_not_generated(world):
    files = edit(world, "DE_Master", lambda rows: rows.append(dict(rows[0], ContactId="TST_R01_C9999")))
    detail = failed(run(world, files), JOINS)
    assert "TST_R01_C9999" in detail and "non sono stati generati" in detail


def test_entry_query_catches_a_value_it_would_not_produce(world):
    files = edit(world, "DE_Entry", set_value("ContactId", C1, "ActionId", "something else"))
    detail = failed(run(world, files), ENTRY)
    assert "ActionId vale 'something else' ma la entry query dà" in detail


def test_simulation_catches_a_contact_relinked_to_another_scenario(world):
    files = edit(world, "DE_Entry", set_value("ContactId", C1, "CampaignName", "TST_R01_S02"))
    files = edit(world, "DE_Target", set_value("SubscriberId", C1, "CampaignName", "TST_R01_S02"), files)
    results = run(world, files)
    assert results[JOINS].passed   # the links are consistent: only the scenario is wrong
    detail = failed(results, SIMULATION)
    assert f"il contatto {C1} (caso S01-C01) è collegato allo scenario 'TST_R01_S02'" in detail
    assert "creato per lo scenario 'TST_R01_S01'" in detail


def test_simulation_catches_a_value_that_changes_the_path(world):
    files = edit(world, "DE_Master", set_value("ContactId", C1, "MarketingPermission", "False"))
    detail = failed(run(world, files), SIMULATION)
    assert C1 in detail and "SPLIT-3/remainder_path" in detail and "invece di" in detail


@pytest.mark.parametrize("covered_by, expected", [
    (["S02-C01"], "il percorso di S02-C01 non lo attraversa"),   # a real case on SPLIT-1/path_b, not path_a
    (["S99-C01"], "cita il caso sconosciuto S99-C01"),            # no such case
])
def test_coverage_catches_a_wrong_covered_by(world, covered_by, expected):
    branches = [replace(b, covered_by=covered_by) if (b.activity_key, b.outcome_key) == ("SPLIT-1", "path_a") else b
                for b in world.coverage.branches]
    detail = failed(run(world, coverage=replace(world.coverage, branches=branches)), COVERAGE)
    assert "SPLIT-1" in detail and expected in detail


def test_addresses_catch_a_real_inbox_without_manual_action(world):
    address = f"qa+{C1}@test.com"
    files = edit(world, "DE_Entry", set_value("ContactId", C1, "Emailaddress", address))
    files = edit(world, "DE_Master", set_value("ContactId", C1, "Emailaddress", address), files)
    detail = failed(run(world, files), ADDRESSES)
    assert f"{C1} dovrebbe avere un indirizzo non recapitabile ma ha '{address}'" in detail


def test_addresses_catch_a_manual_action_without_real_inbox(world):
    contacts = [replace(c, uses_real_inbox=True) if c.key == C1 else c for c in world.data.contacts]
    detail = failed(run(world, data=replace(world.data, contacts=contacts)), ADDRESSES)
    assert f"{C1} deve usare l'inbox reale" in detail


def test_determinism_catches_a_changed_or_missing_file(world):
    first = {k: v for k, v in world.files.items() if k.endswith(".csv")}
    assert checks.check_determinism(first, dict(first)).passed
    changed = dict(first, **{"DE_Master.csv": first["DE_Master.csv"].replace(b"True", b"False", 1)})
    result = checks.check_determinism(first, changed)
    assert not result.passed and result.details == ["DE_Master.csv è diverso tra due run con lo stesso input"]
    missing = {k: v for k, v in first.items() if k != "DE_Config.csv"}
    result = checks.check_determinism(first, missing)
    assert not result.passed and result.details == ["DE_Config.csv è prodotto da uno solo dei due run"]


# ---------------------------------------------------------------------------
# Spec, "Extensions beyond the pilot": rows present or absent, and entries no query fills
# ---------------------------------------------------------------------------

def _absent_world(inserting: bool = False, kind: str = ENTRY_AUTOMATION):
    """A small hand-made world where a split tests whether the contact has a row in DE_Form (IsNull on its field
    linked to the contact key). With ``inserting``, the contacts without a row get one from an Update contact on
    DE_Form, and a second existence test sees it."""
    entry = table("DE_Entry", [field("SubscriberKey", pk=True, nullable=False), field("EmailAddress", "EmailAddress")],
                  sendable_field="SubscriberKey")
    form = table("DE_Form", [field("SubscriberKey", pk=True, nullable=False), field("Consent", "Boolean")])
    log = table("DE_Log", [field("SubscriberKey", pk=True, nullable=False), field("Status")])
    absent = cond("DE_Form", "SubscriberKey", "IsNull")
    if inserting:
        activities = [
            decision("SPLIT-1", "Form submitted?", [("absent", "No form", "UPD-1", absent)], remainder_next="UPD-3"),
            update("UPD-1", "Add form", [("DE_Form", "Consent", "false")], "SPLIT-2"),
            decision("SPLIT-2", "Still no form?", [("absent", "Still none", "UPD-2", absent)], remainder_next="UPD-3"),
            update("UPD-2", "None", [("DE_Log", "Status", "none")], None),
            update("UPD-3", "Form", [("DE_Log", "Status", "form")], None),
        ]
    else:
        activities = [
            decision("SPLIT-1", "Form submitted?", [("absent", "No form", "UPD-1", absent)], remainder_next="SPLIT-2"),
            decision("SPLIT-2", "Consent", [("yes", "Consent given", "UPD-2",
                                             cond("DE_Form", "Consent", "Is", "true"))], remainder_next="UPD-3"),
            update("UPD-1", "No form", [("DE_Log", "Status", "none")], None),
            update("UPD-2", "Given", [("DE_Log", "Status", "given")], None),
            update("UPD-3", "Not given", [("DE_Log", "Status", "not given")], None),
        ]
    pkg = package(journey(activities, entry_table="DE_Entry"), [entry, form, log])
    pkg.entry_event.kind = kind
    model = table_model("DE_Entry", "SubscriberKey", {"DE_Entry": LEVEL_CONTACT, "DE_Form": LEVEL_CONTACT},
                        [("contact", [("DE_Entry", "SubscriberKey"), ("DE_Form", "SubscriberKey")])], [entry, form, log])
    model.existence_tested = {"DE_Form": ("DE_Form", "SubscriberKey")}
    now = datetime(2026, 10, 12, 12, 0)
    settings = Settings(generation_time=now)
    cov = coverage.compute_coverage(pkg, model, settings, now)
    data = datagen.generate_data(pkg, model, cov, settings, now)
    files = {f"{name}.csv": outputs.write_table_csv(rows, pkg.tables[name]) for name, rows in data.tables.items()}
    return SimpleNamespace(package=pkg, table_model=model, coverage=cov, data=data, files=files, settings=settings,
                           generation_time=now)


def _absent_keys(world) -> list[str]:
    return [key for case in world.coverage.cases if world.coverage.row_absent(case, "DE_Form")
            for key in case.contact_keys]


def test_joins_accept_the_absent_rows_and_nothing_else():
    world = _absent_world()
    absent = _absent_keys(world)
    assert len(absent) == 3 and all(c.passed for c in run(world)), [c.details for c in run(world) if not c.passed]
    form_keys = {row["SubscriberKey"] for row in checks.read_csv_bytes(world.files["DE_Form.csv"])[1]}
    assert not form_keys & set(absent)                       # the absent rows are really absent
    # A row for a contact whose case has no row there is an error...
    extra = edit(world, "DE_Form", lambda rows: rows.append({"SubscriberKey": absent[0], "Consent": "True"}))
    detail = failed(run(world, extra), JOINS)
    assert f"il cui caso prevede che non abbiano una riga in DE_Form, ne hanno una: {absent[0]}" in detail
    # ...and so is a missing row for any other contact.
    present = sorted(form_keys)[0]
    detail = failed(run(world, edit(world, "DE_Form", drop("SubscriberKey", present))), JOINS)
    assert f"non hanno una riga in DE_Form.SubscriberKey: {present}" in detail
    assert f"contatti generati non hanno righe in DE_Form: {present}" in detail


def test_simulation_applies_rows_present_or_absent_and_inserted_rows():
    world = _absent_world(inserting=True)
    assert _absent_keys(world)
    assert all(c.passed for c in run(world)), [c.details for c in run(world) if not c.passed]
    # Without the "row present / row absent" rule the Update contact would not insert the row.
    blind = replace(world.table_model, existence_tested={})
    results = checks.run_checks(world.package, blind, world.coverage, world.data, dict(world.files), world.settings,
                                world.generation_time)
    assert "SPLIT-2/absent" in failed(results, SIMULATION)


@pytest.mark.parametrize("kind, words", [
    (ENTRY_RUN_ONCE, "una data extension con pianificazione Run Once"),
    (ENTRY_SCHEDULED, "una data extension con una pianificazione ricorrente"),
])
def test_entry_check_does_not_apply_without_an_entry_query(kind, words):
    world = _absent_world(kind=kind)
    entry_check = run(world)[ENTRY]
    assert entry_check.passed and entry_check.details == [
        f"{checks.NOT_APPLICABLE}: l'entry source di questo journey è {words}, che nessuna query popola, quindi le "
        "righe di ingresso sono quelle di DE_Entry.csv e non c'è una entry query con cui confrontarle"]


# ---------------------------------------------------------------------------
# Spec, step 3, "Honest checks": the clock, date offsets, what the entry check cannot verify
# ---------------------------------------------------------------------------

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
NOW = datetime(2026, 10, 11, 12, 0)


def example_world(name, **settings):
    return pipeline.generate(str(EXAMPLES / name / "package.json"), Settings(generation_time=NOW, **settings),
                             with_testbook=False)


def test_simulation_runs_with_the_entry_time_and_the_exit_criteria_of_the_coverage():
    world = example_world("relative_date_after_today", entry_time=NOW + timedelta(days=3))
    assert all(c.passed for c in run(world)), [(c.name, c.details) for c in run(world) if not c.passed]
    # The relative dates read differently on another clock, so the paths of the coverage are not followed.
    other_clock = checks.run_checks(world.package, world.table_model, world.coverage, world.data, dict(world.files),
                                    replace(world.settings, entry_time=None), world.generation_time)
    assert "segue il percorso" in failed(other_clock, SIMULATION)
    world = example_world("exit_criteria_at_wait_end")
    [leaving] = [c for c in world.coverage.cases if c.exit_by_criteria]
    assert leaving.steps[-1] == Step(leaving.exit_by_criteria, None)
    assert all(c.passed for c in run(world)), [(c.name, c.details) for c in run(world) if not c.passed]


def test_entry_check_verifies_the_date_part_of_a_date_offset_filter():
    world = example_world("date_offset_filter")      # DE_Member.CreatedDate is yesterday's date
    assert run(world)[ENTRY].passed
    same_day = edit(world, "DE_Member", set_value("ContactId", C1, "CreatedDate", "2026-10-10 03:15"))
    assert run(world, same_day)[ENTRY].passed
    other_day = edit(world, "DE_Member", set_value("ContactId", C1, "CreatedDate", "2026-10-09 12:00"))
    assert C1 in failed(run(world, other_day), ENTRY)


def test_entry_check_reports_what_it_cannot_verify_and_fails_only_on_the_rest():
    world = example_world("case_when_identifier")
    entry = run(world)[ENTRY]
    assert entry.passed
    assert any(d.startswith("non verificato: DE_Entry.Email_Identifier è calcolata da un'espressione non compresa (CASE WHEN")
               for d in entry.details)
    assert "non verificato: DE_Backup è unita con LEFT JOIN, letta come inner join" in entry.details
    broken = edit(world, "DE_Entry", set_value("ContactId", C1, "Emailaddress", "someone@else.example"))
    assert "Emailaddress" in failed(run(world, broken), ENTRY)


def test_a_column_computed_by_an_expression_not_understood_is_not_verified_even_when_no_split_reads_it():
    """Spec, step 3, "Honest checks": a column the check cannot verify is "not verified", whether a split reads it
    or not (RET_99_4W: Service_Call_Identifier, computed by a CASE and read by no split, holds a placeholder)."""
    world = example_world("case_when_identifier")
    unread = replace(world.coverage, contact_fields=[ref for ref in world.coverage.contact_fields
                                                     if ref != ("DE_Entry", "Email_Identifier")])
    placeholder = edit(world, "DE_Entry", set_value("ContactId", C1, "Email_Identifier", "TST_Email_Identifier_1"))
    entry = run(world, placeholder, coverage=unread)[ENTRY]
    assert entry.passed, entry.details
    assert any(d.startswith("non verificato: DE_Entry.Email_Identifier è calcolata da un'espressione non compresa")
               for d in entry.details)
    assert not any("non lo popola" in d for d in entry.details)


@pytest.mark.parametrize("default, value, passes", [
    ("GetDate()", "2026-01-31 08:15", True),     # any date: the column holds what the table fills when inserting
    ("GetDate()", "not a date", False),
    (None, "2026-01-31 08:15", False),           # no such default: the query leaves the column empty
])
def test_entry_column_the_query_does_not_fill_accepts_any_date_when_it_defaults_to_the_current_date(default, value, passes):
    world = example_world("date_offset_filter")
    package = copy.deepcopy(world.package)
    package.tables["DE_Entry"].fields.append(Field("Loaded", "Date", None, False, True, default, 99))
    header, rows = checks.read_csv_bytes(world.files["DE_Entry.csv"])
    lines = [",".join(header + ["Loaded"])] + [",".join(row[h] or "" for h in header) + f",{value}" for row in rows]
    files = dict(world.files, **{"DE_Entry.csv": "\r\n".join(lines).encode("utf-8")})
    results = checks.run_checks(package, world.table_model, world.coverage, world.data, files, world.settings,
                                world.generation_time)
    assert results[ENTRY].passed is passes
