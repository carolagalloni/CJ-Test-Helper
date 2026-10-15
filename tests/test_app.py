"""The one-page app (spec "The app"): the page through Streamlit's AppTest, and its helpers.

``AppTest.from_file`` runs the whole page (title, step 1 and the uploader); ``AppTest.from_function``
runs ``app.render(bytes)``, the steps below the uploader, with the bytes passed in. Step 4 is tested
with a stand-in generator that returns a small result at once; only one test runs the full
generation on the pilot (about 45 s), and it is marked ``slow``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest, element_tree

from cjhelper import app, outputs, pipeline
from cjhelper.loader import LoadError
from cjhelper.model import (
    ENTRY_AUTOMATION,
    ENTRY_OTHER,
    ENTRY_RUN_ONCE,
    ENTRY_SCHEDULED,
    STATUS_COVERED,
    STATUS_NOT_COVERED,
    CheckResult,
    Settings,
)

ROOT = Path(__file__).resolve().parent.parent
APP_FILE = ROOT / "cjhelper" / "app.py"
PILOT = ROOT / "fixtures" / "CJ template complesso-v1.json"
PILOT_JOURNEY = "CJ_SkYDE_NCRM_Demo_final"
PILOT_CSV_TABLES = ["DE_NCM_AdHoc_Entry", "SF_Master_DataExtension_DE", "DE_NCRM_AD_HOC_CONFIG", "DE_NCRM_AdHoc_Target"]
PILOT_ACTIVATION = ("DE_NCRM_AD_HOC_CONFIG", "ACTIVE")
TIMEOUT = 60

needs_pilot = pytest.mark.skipif(not PILOT.exists(), reason="pilot export not present")
CAN_UPLOAD = hasattr(getattr(element_tree, "FileUploader", None), "set_value")   # AppTest drives st.file_uploader


def _render_page(data):
    """The page below the uploader, as AppTest.from_function runs it."""
    from cjhelper import app
    app.render(data)


def _result_page(result, zip_bytes):
    """Step 4's output alone, as AppTest.from_function runs it."""
    from cjhelper import app
    app.render_result(result, zip_bytes)


def _plain(markdown: str) -> str:
    """Undo the page's Markdown escaping."""
    return re.sub(r"\\([^A-Za-z0-9\s])", r"\1", markdown)


def _markdown(at) -> str:
    return "\n".join(_plain(m.value) for m in at.markdown)


def _captions(at) -> list[str]:
    return [_plain(c.value) for c in at.caption]


def _widget(widgets, label: str):
    found = [w for w in widgets if w.label == label]
    assert len(found) == 1, (label, [w.label for w in widgets])
    return found[0]


def _expander(at, label: str):
    found = [e for e in at.expander if _plain(e.label) == label]
    assert len(found) == 1, (label, [e.label for e in at.expander])
    return found[0]


def _cards(at) -> list[str]:
    """The labels of the step cards, which open with the step's title in bold."""
    return [_plain(e.label) for e in at.expander if e.label.startswith("**")]


def _card(at, title: str):
    found = [e for e in at.expander if _plain(e.label).startswith(f"**{title}**")]
    assert len(found) == 1, (title, _cards(at))
    return found[0]


def _labels(at) -> list[str]:
    """The small section labels inside the cards."""
    return re.findall(r'<p class="cj-label">(.+?)</p>', "\n".join(m.value for m in at.markdown))


def _html_bodies(at) -> str:
    return "\n".join(element.proto.body for element in at.get("html"))


def _step_content(at) -> list:
    """Everything a step after the upload would show."""
    cards = [e for e in at.expander if not _plain(e.label).startswith(f"**{app.STEP_UPLOAD}**")]
    return (list(at.header) + list(at.subheader) + list(at.get("dataframe")) + list(at.table)
            + list(at.button) + list(at.checkbox) + list(at.number_input) + list(at.text_input)
            + list(at.selectbox) + cards + list(at.get("download_button")))


@pytest.fixture(scope="module")
def pilot_bytes() -> bytes:
    return PILOT.read_bytes()


# ---------------------------------------------------------------------------
# Step 1: upload, and the edges
# ---------------------------------------------------------------------------

def test_page_without_file_shows_the_upload_step_and_no_error():
    at = AppTest.from_file(str(APP_FILE)).run(timeout=TIMEOUT)
    assert not at.exception
    html = _html_bodies(at)
    assert f'<span class="cj-name">{app.TITLE}</span>' in html and 'alt="Lutech"' in html   # the header
    assert '<div class="now"><span class="cj-num">01</span><b>Carica</b>' in html         # the stepper
    assert _cards(at) == [f"**{app.STEP_UPLOAD}** il file JSON che Package Manager esporta per il journey"]
    assert _card(at, app.STEP_UPLOAD).proto.expanded is True
    uploaders = at.get("file_uploader")
    assert len(uploaders) == 1 and uploaders[0].label == app.UPLOAD_LABEL
    assert _captions(at) == [app.UPLOAD_NOTE]          # visible, says where the file comes from
    assert not at.error
    assert not _step_content(at)


@pytest.mark.skipif(not CAN_UPLOAD, reason="this Streamlit's AppTest cannot drive st.file_uploader")
def test_the_uploader_takes_any_file_and_the_page_answers_with_one_sentence():
    at = AppTest.from_file(str(APP_FILE)).run(timeout=TIMEOUT)
    uploader = at.get("file_uploader")[0]
    assert uploader.allowed_type == []                  # no file-type filter: the page's own message answers
    uploader.set_value(("export.txt", b"just some notes", "text/plain")).run(timeout=TIMEOUT)
    assert not at.exception
    assert [_plain(e.value) for e in at.error] == [
        "Il file non è un JSON valido: c'è un problema alla riga 1, colonna 1. " + app.NEXT_STEP]
    assert _cards(at) == [f"**{app.STEP_UPLOAD}** export.txt · 15 byte"]
    assert _card(at, app.STEP_UPLOAD).proto.expanded is True      # open, to choose another file
    assert not _step_content(at)


@pytest.mark.parametrize("data, words", [
    (b"{not json", "Il file non è un JSON valido: c'è un problema alla riga 1, colonna 2."),
    (b"", "Il file non è un JSON valido: c'è un problema alla riga 1, colonna 1."),
    (b"[1, 2]", "deve contenere un oggetto JSON."),
    (b'{"entities": {"dataExtensions": {}}}', "L'export non contiene alcun journey."),
    (b'{"name": "x"}', "quindi non contiene alcun journey."),
    (b"\xff\xfe\x00{", "Il file non è testo UTF-8"),   # the loader's own words
])
def test_unreadable_file_shows_one_plain_error_and_nothing_else(data, words):
    at = AppTest.from_function(_render_page, args=(data,)).run(timeout=TIMEOUT)
    assert not at.exception
    assert len(at.error) == 1
    message = _plain(at.error[0].value)
    assert words in message and message.endswith(" " + app.NEXT_STEP) and "\n" not in message
    assert "Expecting" not in message
    assert not at.header and not at.markdown and not at.caption and not _step_content(at)


def test_without_a_file_nothing_is_kept():
    at = AppTest.from_function(_render_page, args=(None,))
    at.session_state[app.DETECTION_KEY] = ("digest", "summary")
    at.session_state[app.RESULT_KEY] = "result"
    at.session_state[app.PENDING_KEY] = ("digest", "settings")
    at.run(timeout=TIMEOUT)
    assert not at.exception
    for key in (app.DETECTION_KEY, app.RESULT_KEY, app.PENDING_KEY):
        assert key not in at.session_state
    assert not at.header and not at.error and not _step_content(at)


def test_error_messages_are_plain_and_end_with_the_next_step():
    assert app.error_message(LoadError("L'export non contiene alcun journey.")) == (
        "L'export non contiene alcun journey. Scegli il file JSON prodotto da Package Manager.")
    with pytest.raises(LoadError) as not_json:
        from cjhelper import loader
        loader.load_package(b"{oops")
    assert app.error_message(not_json.value) == ("Il file non è un JSON valido: c'è un problema alla riga 1, "
                                                 "colonna 2. " + app.NEXT_STEP)
    with pytest.raises(json.JSONDecodeError) as raw_json:
        json.loads("\n\n  ]")
    assert app.error_message(raw_json.value).startswith("Il file non è un JSON valido: c'è un problema alla riga 3, "
                                                        "colonna 3.")
    with pytest.raises(UnicodeDecodeError) as not_text:
        b"\xff".decode("utf-8")
    assert app.error_message(not_text.value) == ("Il file non è testo UTF-8, quindi non è un export JSON "
                                                 "valido. " + app.NEXT_STEP)
    assert app.is_file_error(LoadError("x")) and not app.is_file_error(KeyError("name"))
    assert app.error_message(KeyError("name")) == app.UNEXPECTED_READ
    try:
        {}["name"]
    except KeyError as error:
        details = app.technical_details(error)
    assert details.startswith("Traceback") and details.endswith("KeyError: 'name'")


@needs_pilot
def test_an_unexpected_error_while_reading_shows_a_sentence_and_closed_details(pilot_bytes, monkeypatch):
    def broken(package):
        raise AttributeError("'TableModel' object has no attribute 'x'")
    monkeypatch.setattr("cjhelper.tables.build_table_model", broken)
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    assert not at.exception
    assert [_plain(e.value) for e in at.error] == [app.UNEXPECTED_READ]
    details = _expander(at, app.TECHNICAL_DETAILS)
    assert details.proto.expanded is False
    assert "AttributeError: 'TableModel' object has no attribute 'x'" in details.code[0].value
    assert not at.header


# ---------------------------------------------------------------------------
# Step 2: what was detected
# ---------------------------------------------------------------------------

@needs_pilot
def test_detection_summary_on_pilot(pilot_bytes):
    summary = app.load_summary(pilot_bytes)
    assert summary.journey_name == PILOT_JOURNEY
    assert summary.activity_count == 178
    counts = dict(summary.activities)
    assert sum(counts.values()) == 178
    assert counts == {"Decision split": 106, "Engagement split": 6, "Random split": 1, "Wait by duration": 10,
                      "Wait by attribute": 3, "Send-time wait": 7, "Email": 11, "Update contact": 34}
    names = [line.name for line in summary.tables]
    assert sorted(names[:4]) == sorted(PILOT_CSV_TABLES)            # the tables that get a CSV come first
    assert all(line.csv for line in summary.tables[:4]) and not any(line.csv for line in summary.tables[4:])
    assert sorted(summary.csv_files) == sorted(f"{name}.csv" for name in PILOT_CSV_TABLES)
    lines = {line.name: line for line in summary.tables}
    assert (lines["DE_NCM_AdHoc_Entry"].role, lines["DE_NCM_AdHoc_Entry"].level) == ("entry", "contact")
    assert (lines["DE_NCRM_AD_HOC_CONFIG"].role, lines["DE_NCRM_AD_HOC_CONFIG"].level) == ("source, split", "scenario")
    assert lines["DE_NCRM_AdHoc_Target"].role == "source"
    assert lines["DE_SkyDE_AdHoc_Log_partial"] == app.TableLine("DE_SkyDE_AdHoc_Log_partial", "written", "-", False)
    assert summary.activation_field == PILOT_ACTIVATION
    assert PILOT_ACTIVATION in summary.activation_choices
    assert all(table == "DE_NCRM_AD_HOC_CONFIG" for table, _ in summary.activation_choices)
    patterns = [w for w in summary.warnings if w.startswith("Pattern sospetto: ")]
    assert patterns and summary.warnings[:len(patterns)] == patterns   # suspicious patterns first
    assert all(w[:1].isupper() for w in summary.warnings)
    assert not any("UPDATECONTACTDATAV2" in w for w in summary.warnings) and summary.ids_left_out


@needs_pilot
def test_detection_step_on_pilot_shows_journey_activities_and_the_four_csv_tables(pilot_bytes):
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    assert not at.exception and not at.error
    assert [card.split("**")[1] for card in _cards(at)] == [app.STEP_REVIEW, app.STEP_SETTINGS, app.STEP_GENERATE]
    assert not at.header and not at.subheader           # the cards are the only titles
    review = _card(at, app.STEP_REVIEW)
    assert _plain(review.label) == (f"**{app.STEP_REVIEW}** {PILOT_JOURNEY} · 178 attività · 4 tabelle con CSV · "
                                    ":yellow-badge[5 warning]")
    assert review.proto.expanded is True
    assert _card(at, app.STEP_SETTINGS).proto.expanded is True
    assert _card(at, app.STEP_GENERATE).proto.expanded is False
    assert _plain(_card(at, app.STEP_GENERATE).label).endswith(" da avviare")
    assert _labels(at)[:2] == ["Attività per tipo", "Tabelle con CSV"]
    text = _markdown(at)
    assert "| Decision split | 106 |" in text and "| Non supportata |" not in text
    rows = re.findall(r"^\| (\S+) \| ([^|]+) \| ([^|]+) \|$", text, flags=re.MULTILINE)
    assert sorted(name for name, _, _ in rows if name not in ("Tabella", "---")) == sorted(PILOT_CSV_TABLES)
    assert ("DE_NCRM_AD_HOC_CONFIG", "source, split", "scenario") in rows
    assert ("DE_NCM_AdHoc_Entry", "entry", "contatto") in rows
    captions = _captions(at)
    assert ("Senza CSV: DE_Check_Active_Campaigns (written), DE_NCRM_AdHoc_Entry_Backup (written) e "
            "DE_SkyDE_AdHoc_Log_partial (written).") in captions
    warnings = _expander(at, "Warning trovati nell'export (5)")
    assert warnings.proto.expanded is False
    assert "- Pattern sospetto: la costante 'DemoSKY' è scritta nel campo di chiave primaria" in text
    assert "UPDATECONTACTDATAV2" not in text
    assert any(c.startswith("report.md elenca le attività coinvolte") for c in captions)
    assert [b.label for b in at.button] == [app.GENERATE_LABEL]
    assert not at.get("download_button")      # nothing generated yet


def test_detection_step_lists_the_exit_criteria():
    """Spec, step 3, "Exit criteria and goals": the review step names the exit criteria and whether they are modelled."""
    data = (ROOT / "examples" / "exit_criteria_at_wait_end" / "package.json").read_bytes()
    line = "Status not equal Active OR IsDunning is True: valutato alla fine di ogni wait"
    assert app.load_summary(data).exits == [line]
    at = AppTest.from_function(_render_page, args=(data,)).run(timeout=TIMEOUT)
    assert not at.exception and "Criteri di uscita" in _labels(at)
    assert f"- {line}" in _markdown(at)
    assert app.role_text({"link", "source"}) == "collegamento, source"


def test_activity_counts_and_roles_use_plain_names():
    activities = {key: SimpleNamespace(kind=kind) for key, kind in
                  [("a", "decision"), ("b", "unsupported"), ("c", "decision"), ("d", "wait_sto"), ("e", "new_kind")]}
    journey = SimpleNamespace(activities=activities)
    assert app.activity_counts(journey) == [("Decision split", 2), ("Send-time wait", 1), ("Non supportata", 1),
                                            ("new_kind", 1)]
    assert app.role_text({"source", "split"}) == "source, split"      # as report.md writes it
    assert app.role_text({"written"}) == "written"
    assert app.role_text(set()) == "nessuno"


def test_warning_lines_put_patterns_first_and_leave_key_lists_to_the_report():
    keys = ["SPLIT-1", "SPLIT-2", "UPDATE-1"]
    lines, left_out = app.warning_lines([
        "assets/1 has no value at 'data/x'.",
        "pattern sospetto: 2 different conditions share the split name 'Consent' (SPLIT-1, SPLIT-2)",
        "Decision split Consent (SPLIT-1) is reported as unsupported.",
        "pattern sospetto: 2 different conditions share the split name 'Consent' (SPLIT-1, SPLIT-2)",
        "the note (not keys, SPLIT-1) stays",
    ], keys)
    assert lines == ["Pattern sospetto: 2 different conditions share the split name 'Consent'",
                     "Assets/1 has no value at 'data/x'.",
                     "Decision split Consent (SPLIT-1) is reported as unsupported.",
                     "The note (not keys, SPLIT-1) stays"]
    assert left_out
    assert app.warning_lines(["One plain warning."], keys) == (["One plain warning."], False)


# ---------------------------------------------------------------------------
# Step 3: settings
# ---------------------------------------------------------------------------

def test_settings_mapping_gives_the_spec_defaults():
    settings = app.settings_from_form(app.default_form_values(PILOT_ACTIVATION))
    assert settings.contacts_per_case == 3
    assert settings.contacts_per_random_case == 50
    assert settings.real_inbox == "" and settings.real_phone == ""
    assert settings.fake_phone == "333333333333"
    assert (settings.window_start_days, settings.window_end_days) == (-1, 7)
    assert settings.wait_offset_hours == 0
    assert settings.wait_offset_before_engagement_hours == 24
    assert settings.date_format == "%Y-%m-%d %H:%M"
    assert settings.one_active_scenario is True and settings.activation_field == PILOT_ACTIVATION
    assert settings.run_id == "R01"
    assert settings.case_insensitive_text is False
    defaults = Settings()
    assert (settings.seed, settings.generation_time) == (defaults.seed, defaults.generation_time)


def test_settings_mapping_without_a_detected_activation_field_is_off():
    values = app.default_form_values(None)
    assert values["one_active_scenario"] is False and values["activation_field"] is None
    settings = app.settings_from_form(values)
    assert settings.one_active_scenario is False and settings.activation_field is None


def test_settings_mapping_reads_the_changed_values():
    values = app.default_form_values(PILOT_ACTIVATION)
    values.update(contacts_per_case=5.0, contacts_per_random_case=80, real_inbox="  tester@example.com ",
                  real_phone=" 3471234567", fake_phone="390000000000", window_start_days=-2, window_end_days=3,
                  wait_offset_hours=-1, wait_offset_before_engagement_hours=48, date_format="%d/%m/%Y %H:%M",
                  one_active_scenario=False, run_id=" R07 ", case_insensitive_text=True)
    settings = app.settings_from_form(values)
    assert settings == Settings(contacts_per_case=5, contacts_per_random_case=80, real_inbox="tester@example.com",
                                real_phone="3471234567", fake_phone="390000000000", window_start_days=-2,
                                window_end_days=3, wait_offset_hours=-1, wait_offset_before_engagement_hours=48,
                                date_format="%d/%m/%Y %H:%M", one_active_scenario=False, activation_field=None,
                                run_id="R07", case_insensitive_text=True)
    assert isinstance(settings.contacts_per_case, int)
    values.update(one_active_scenario=True, activation_field=("DE_NCRM_AD_HOC_CONFIG", "HAS_RUN"))
    assert app.settings_from_form(values).activation_field == ("DE_NCRM_AD_HOC_CONFIG", "HAS_RUN")


def test_settings_mapping_with_a_run_once_entry_does_not_offer_one_active_scenario():
    """Spec, "Entry source and procedure": with Run Once "All scenarios enter together, so 'one active scenario at
    a time' is not offered"."""
    values = app.default_form_values(PILOT_ACTIVATION, ENTRY_RUN_ONCE)
    assert values["one_active_scenario"] is False and values["activation_field"] is None
    settings = app.settings_from_form(values)
    assert settings.one_active_scenario is False and settings.activation_field is None


def test_settings_map_the_minimum_validity_of_the_data():
    """Spec, step 3, "Validity of the data": the setting "minimum validity of the data" (default 1 day)."""
    assert app.SETTING_LABELS["min_validity_days"] == "Validità minima dei dati (giorni)"
    assert app.settings_from_form(app.default_form_values(PILOT_ACTIVATION)).min_validity_days == 1
    settings = app.settings_from_form({**app.default_form_values(PILOT_ACTIVATION), "min_validity_days": 0.0})
    assert settings.min_validity_days == 0 and isinstance(settings.min_validity_days, int)


@needs_pilot
def test_settings_with_a_run_once_entry_disable_one_active_scenario(pilot_bytes, monkeypatch):
    monkeypatch.setattr("cjhelper.outputs.entry_kind", lambda package: ENTRY_RUN_ONCE)
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    assert not at.exception
    one_active = _widget(at.checkbox, app.SETTING_LABELS["one_active_scenario"])
    assert one_active.value is False and one_active.disabled
    assert not [w for w in at.selectbox if w.label == app.SETTING_LABELS["activation_field"]]
    assert ("L'entry source è una data extension Run Once: tutti gli scenari entrano insieme quando il journey viene "
            "attivato, quindi l'opzione non è disponibile.") in _captions(at)


def test_settings_notes_in_words():
    assert app.days_text(-1) == "1 giorno prima della generazione."
    assert app.days_text(7) == "7 giorni dopo la generazione."
    assert app.days_text(0) == "Al momento della generazione."
    assert app.date_format_note(datetime(2026, 10, 11, 13, 45)) == (
        "Di default le date sono scritte come YYYY-MM-DD HH:MM, per esempio 2026-10-11 13:45.")


@needs_pilot
def test_settings_start_at_the_spec_defaults_with_inbox_and_run_identifier_first(pilot_bytes):
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    labels = app.SETTING_LABELS
    assert [w.label for w in at.text_input][:2] == [labels["real_inbox"], labels["run_id"]]
    more = _expander(at, app.MORE_SETTINGS)
    assert more.proto.expanded is False
    numbers = {name: _widget(at.number_input, labels[name]).value for name in app.NUMBER_SETTINGS}
    assert numbers == {"contacts_per_case": 3, "contacts_per_random_case": 50, "window_start_days": -1,
                       "window_end_days": 7, "wait_offset_hours": 0, "wait_offset_before_engagement_hours": 24,
                       "min_validity_days": 1}
    assert {w.label for w in more.number_input} == {labels[name] for name in app.NUMBER_SETTINGS}
    texts = {name: _widget(at.text_input, labels[name]).value
             for name in ("real_inbox", "real_phone", "fake_phone", "date_format", "run_id")}
    assert texts == {"real_inbox": "", "real_phone": "", "fake_phone": "333333333333",
                     "date_format": "%Y-%m-%d %H:%M", "run_id": "R01"}
    captions = _captions(at)
    for note in (app.SETTING_NOTES["real_inbox"], app.SETTING_NOTES["run_id"], app.SETTING_NOTES["real_phone"],
                 "1 giorno prima della generazione.", "7 giorni dopo la generazione.", app.GENERATE_NOTE):
        assert note in captions
    assert any(c.startswith("Di default le date sono scritte come YYYY-MM-DD HH:MM, per esempio ") for c in captions)
    one_active = _widget(at.checkbox, labels["one_active_scenario"])
    assert one_active.value is True and not one_active.disabled
    assert any("rilevato nei filtri della entry query: DE_NCRM_AD_HOC_CONFIG.ACTIVE." in c for c in captions)
    activation = _widget(at.selectbox, labels["activation_field"])
    assert activation.value == PILOT_ACTIVATION
    assert "DE_NCRM_AD_HOC_CONFIG.HAS_RUN" in activation.options
    assert _widget(at.checkbox, labels["case_insensitive_text"]).value is False
    generate = _widget(at.button, app.GENERATE_LABEL)
    assert generate.proto.type == "primary" and not generate.disabled


# ---------------------------------------------------------------------------
# Step 4: the result
# ---------------------------------------------------------------------------

def _stub_result(checks=None, files=None, journey_name="CJ demo", real_inbox="", one_active=True,
                 entry_kind=ENTRY_AUTOMATION, unsupported=False):
    """The parts of a GenerationResult that step 4 reads. ``unsupported``: the branch without a root cause is not
    covered because of an unsupported element."""
    rows = "".join(f"TST_R01_C{n:04d},,x_{n}\n" for n in range(1, 13)).encode()
    if files is None:
        files = {"DE_Entry.csv": b"ContactId,Email,Note\n" + rows,
                 "scenarios.csv": b"scenario_key,run_order\nTST_R01_S01,1\nTST_R01_S02,2\n",
                 "testbook.xlsx": b"xlsx", "report.md": b"# Report\n"}
    field = "DE_Config.ACTIVE" if one_active else None
    scenario_rows = [{"scenario_key": "TST_R01_S01", "active_at_start": "yes", "activation_field": field},
                     {"scenario_key": "TST_R01_S02", "active_at_start": "no" if one_active else "yes",
                      "activation_field": field}]
    branches = ([SimpleNamespace(status=STATUS_COVERED, behind=None)] * 4
                + [SimpleNamespace(status=STATUS_NOT_COVERED, behind=("SPLIT-1", "yes"))] * 2
                + [SimpleNamespace(status=STATUS_NOT_COVERED, behind=("SPLIT-2", "yes"))]
                + [SimpleNamespace(status=STATUS_NOT_COVERED, behind=None,
                                   reason="branch non supportato: x" if unsupported else "non raggiungibile: x")])
    cases = [SimpleNamespace(manual_actions=[SimpleNamespace(kind="open")]), SimpleNamespace(manual_actions=[])]
    event = SimpleNamespace(kind=entry_kind, event_type="CloudPagesSmartCaptureSubmissionEvent")
    return SimpleNamespace(
        package=SimpleNamespace(journey=SimpleNamespace(name=journey_name, activities={"SPLIT-1": 1, "SPLIT-2": 2}),
                                entry_event=event, unsupported_activities=[]),
        table_model=SimpleNamespace(entry_table="DE_Entry"),
        coverage=SimpleNamespace(stats={"scenarios": 2, "cases": 3, "contacts": 9, "covered": 4,
                                        "covered_manual": 1, "not_covered": 4},
                                 branches=branches, cases=cases, unsupported=[],
                                 warnings=["pattern sospetto: T.F is compared with 'Email' and 'EMAIL' "
                                           "(SPLIT-1, SPLIT-2)"]),
        data=SimpleNamespace(warnings=["Nessuna inbox reale impostata. I contatti di 1 caso con un open o un click "
                                       "manuale ricevono indirizzi non recapitabili.", "the entry query selects 2 columns it does not have."],
                             scenarios_rows=scenario_rows),
        checks=checks if checks is not None else [CheckResult("Every key joins", True, [])],
        files=files,
        settings=Settings(real_inbox=real_inbox),
        generation_time=datetime(2026, 10, 11, 13, 45),
    )


def test_summary_import_and_download_notes():
    result = _stub_result()
    assert app.done_summary(result) == "Completato alle 13:45 · run R01"
    assert app.checks_banner(result) == ("1 di 1", "Tutti i trust check sono superati.", True)
    failing = _stub_result([CheckResult("A", True, []), CheckResult("B", False, ["why"])])
    assert app.checks_banner(failing) == (
        "1 di 2", "1 trust check non superato: leggi i dettagli qui sotto prima di importare.", False)
    assert app.checks_banner(_stub_result([]))[2] is False
    assert app.summary_cells(result) == [("2", "", "Scenari"), ("3", "", "Casi"), ("9", "", "Contatti"),
                                         ("5", "/ 9", "Branch coperti, 1 con azione manuale"),
                                         ("4", "", "Branch non coperti")]
    assert app.summary_notes(result) == [
        "3 dei 4 branch non coperti stanno dietro a 2 branch che nessun contatto può raggiungere (vedi report.md).",
        "Non è stata indicata un'inbox reale: i contatti che devono aprire un'email ricevono indirizzi non "
        "recapitabili e i branch relativi risultano non coperti."]
    assert app.summary_notes(_stub_result(real_inbox="tester@example.com"))[1:] == []
    assert app.import_notes(result) == [
        "Apri prima report.md: la sezione Come eseguire i test indica l'ordine di import.",
        "Nei CSV è attivo solo TST_R01_S01, il primo scenario in ordine di esecuzione.",
        "Esegui gli scenari uno alla volta, in ordine di esecuzione. Quando i contatti di uno scenario sono usciti "
        "dal journey, attiva il successivo: imposta DE_Config.ACTIVE a True sulla sua riga e a False su tutte le "
        "altre. Non cambiare altro e non reimportare nulla."]
    assert app.import_notes(_stub_result(one_active=False))[1:] == [
        "Nei CSV tutti gli scenari sono attivi, quindi vengono eseguiti insieme."]
    run_once = _stub_result(one_active=False, entry_kind=ENTRY_RUN_ONCE)
    assert app.import_notes(run_once)[1:] == [
        "Entry source Run Once: importa tutti i CSV, poi attiva il journey. Entrano solo le righe presenti in DE_Entry "
        "all'attivazione, che deve contenere solo righe di test. Un nuovo tentativo richiede una nuova versione del "
        "journey e un nuovo run identifier.",
        "Nei CSV tutti gli scenari sono attivi, quindi vengono eseguiti insieme."]
    cloudpages = _stub_result(one_active=False, entry_kind=ENTRY_OTHER)
    assert app.import_notes(cloudpages)[1] == (
        "L'entry source è un form CloudPages (CloudPagesSmartCaptureSubmissionEvent): i contatti non entrano con "
        "l'import di un CSV. Usa una copia del journey con entry source una data extension con le stesse colonne di "
        "DE_Entry, e importa lì il CSV di ingresso.")
    assert app.import_notes(_stub_result(one_active=False, entry_kind=ENTRY_SCHEDULED))[1].startswith(
        "Entry source con pianificazione ricorrente")
    assert app.download_lines(result) == [
        "DE_Entry.csv: i dati da importare.",
        "scenarios.csv: gli scenari in ordine di esecuzione, con quello attivo all'inizio.",
        "testbook.xlsx: i test e i risultati attesi.",
        "report.md: cosa è coperto, cosa no e perché, e come eseguire i test."]
    assert app.preview_files(result) == ["scenarios.csv", "DE_Entry.csv"]
    warnings, left_out = app.generation_warnings(result)
    assert warnings == ["Pattern sospetto: T.F is compared with 'Email' and 'EMAIL'",
                        "The entry query selects 2 columns it does not have."] and left_out


def test_result_step_shows_summary_import_previews_download_checks_and_warnings():
    checks = [CheckResult("Every key joins", True, ["a note on a passed check"]),
              CheckResult("Values respect the schema", False, [f"problem {n}" for n in range(1, 24)])]
    at = AppTest.from_function(_result_page, args=(_stub_result(checks), b"zip bytes")).run(timeout=TIMEOUT)
    assert not at.exception and not at.error and not at.subheader
    assert _labels(at) == ["Prima di importare", "Trust check", "Anteprima dei CSV"]
    text = _markdown(at)
    assert ('<div class="cj-banner failed"><span class="cj-chip">1 di 2</span><span>1 trust check non superato: '
            'leggi i dettagli qui sotto prima di importare.</span></div>') in text
    for value, label in [("2", "Scenari"), ("3", "Casi"), ("9", "Contatti"), ("4", "Branch non coperti")]:
        assert f"<b>{value}</b><span>{label}</span>" in text
    assert "<b>5 <small>/ 9</small></b><span>Branch coperti, 1 con azione manuale</span>" in text
    captions = _captions(at)
    assert "3 dei 4 branch non coperti stanno dietro a 2 branch che nessun contatto può raggiungere (vedi report.md)." in captions
    assert any(c.startswith("Non è stata indicata un'inbox reale: i contatti che devono aprire un'email") for c in captions)
    assert "1. Apri prima report.md: la sezione Come eseguire i test indica l'ordine di import." in text
    assert "2. Nei CSV è attivo solo TST_R01_S01, il primo scenario in ordine di esecuzione." in text
    assert "- :green-badge[Superato] Every key joins" in text and "a note on a passed check" not in text
    assert "- :red-badge[Non superato] Values respect the schema" in text
    assert "    - problem 20" in text and "problem 21" not in text and "    - e altri 3" in text
    assert [tab.label for tab in at.tabs] == ["scenarios.csv", "DE_Entry.csv"]
    frames = [frame.value for frame in at.dataframe]
    assert list(frames[0].columns) == ["scenario_key", "run_order"] and len(frames[0]) == 2
    entry = frames[1]
    assert list(entry.columns) == ["ContactId", "Email", "Note"] and len(entry) == app.PREVIEW_ROWS
    assert list(entry.iloc[0]) == ["TST_R01_C0001", "", "x_1"]          # the values as the file writes them
    assert "2 righe · 2 colonne. Il file completo è nello zip." in captions
    assert "Prime 10 di 12 righe · 3 colonne. Il file completo è nello zip." in captions
    assert "CJ demo_test_data.zip contiene 4 file:" in text
    assert "- DE_Entry.csv: i dati da importare." in text
    warnings = _expander(at, "Warning della generazione (2)")
    assert warnings.proto.expanded is False
    assert "- Pattern sospetto: T.F is compared with 'Email' and 'EMAIL'" in text
    assert "Nessuna inbox reale impostata" not in text           # said in the summary instead
    assert "report.md elenca le attività coinvolte." in captions
    buttons = at.get("download_button")
    assert len(buttons) == 1 and buttons[0].label == app.DOWNLOAD_LABEL and buttons[0].proto.type == "primary"


def test_result_step_shows_the_activation_deadline_when_the_data_is_perishable():
    """Spec, step 3, "Honest checks": the page, like report.md, gives the deadline for importing and activating."""
    result = _stub_result()
    result.coverage.validity_days = 2
    result.settings.entry_time = datetime(2026, 10, 11, 12, 0)
    at = AppTest.from_function(_result_page, args=(result, b"zip bytes")).run(timeout=TIMEOUT)
    assert not at.exception
    assert [_plain(w.value) for w in at.warning] == [
        "Importa i file e attiva il journey entro 2026-10-13 12:00: i valori scelti rispetto all'ora di ingresso "
        "restano validi 2 giorni."]


def test_result_step_opens_with_the_warning_on_unsupported_elements():
    """Spec, "Honest outcomes": "report.md and the page open with a warning when unsupported elements leave branches
    not covered"."""
    at = AppTest.from_function(_result_page, args=(_stub_result(), b"zip bytes")).run(timeout=TIMEOUT)
    assert not at.exception and not at.warning
    at = AppTest.from_function(_result_page, args=(_stub_result(unsupported=True), b"zip bytes")).run(timeout=TIMEOUT)
    assert not at.exception
    assert [_plain(w.value) for w in at.warning] == [
        "1 branch su 8 non è coperto a causa di elementi non supportati. Per questi branch i dati di test non indicano "
        "il percorso dei contatti. report.md spiega il motivo nelle sezioni Branch non coperti ed Elementi non "
        "supportati."]


@needs_pilot
def test_generate_runs_once_and_the_download_does_not_generate_again(pilot_bytes, monkeypatch):
    calls = []

    def stand_in_generate(data, settings):
        calls.append(settings)
        return _stub_result(journey_name=PILOT_JOURNEY)

    monkeypatch.setattr(pipeline, "generate", stand_in_generate)
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    _widget(at.text_input, app.SETTING_LABELS["real_inbox"]).input("tester@example.com")
    _widget(at.button, app.GENERATE_LABEL).click().run(timeout=TIMEOUT)
    assert not at.exception and not at.error
    assert len(calls) == 1 and calls[0].real_inbox == "tester@example.com"
    assert not _widget(at.button, app.GENERATE_LABEL).disabled          # enabled again under the result
    assert app.PENDING_KEY not in at.session_state
    generate = _card(at, app.STEP_GENERATE)
    assert _plain(generate.label) == f"**{app.STEP_GENERATE}** Completato alle 13:45 · run R01"
    assert generate.proto.expanded is True            # the result opens step 4 and closes the steps before it
    assert _card(at, app.STEP_REVIEW).proto.expanded is False
    settings_card = _card(at, app.STEP_SETTINGS)
    assert settings_card.proto.expanded is False
    assert _plain(settings_card.label) == f"**{app.STEP_SETTINGS}** inbox `tester@example.com` · run R01 · 3 contatti per caso"
    stored = at.session_state[app.RESULT_KEY]
    with zipfile.ZipFile(io.BytesIO(stored.zip_bytes)) as archive:
        assert archive.namelist() == list(stored.result.files)

    at.get("download_button")[0].click().run(timeout=TIMEOUT)
    assert not at.exception
    assert len(calls) == 1                     # the download reran the page, not the generation
    assert at.session_state[app.RESULT_KEY] is stored and len(at.get("download_button")) == 1


@needs_pilot
def test_a_failed_generation_shows_one_sentence_and_closed_technical_details(pilot_bytes, monkeypatch):
    def failing_generate(data, settings):
        raise AttributeError("'CoverageEngine' object has no attribute '_flag_case_variants'")

    monkeypatch.setattr(pipeline, "generate", failing_generate)
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    _widget(at.button, app.GENERATE_LABEL).click().run(timeout=TIMEOUT)
    assert not at.exception
    assert [_plain(e.value) for e in at.error] == [app.UNEXPECTED_GENERATION]
    details = _expander(at, app.TECHNICAL_DETAILS)
    assert details.proto.expanded is False
    assert "'CoverageEngine' object has no attribute '_flag_case_variants'" in details.code[0].value
    assert "_flag_case_variants" not in _markdown(at)
    assert not _widget(at.button, app.GENERATE_LABEL).disabled and not at.get("download_button")


@needs_pilot
def test_a_kept_result_shows_only_for_the_same_file_and_settings(pilot_bytes):
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    settings = app.settings_from_form(app.default_form_values(PILOT_ACTIVATION))
    stub = _stub_result(journey_name=PILOT_JOURNEY)
    digest = hashlib.sha256(pilot_bytes).hexdigest()

    at.session_state[app.RESULT_KEY] = app.StoredRun(digest, settings, result=stub, zip_bytes=b"zip")
    at.run(timeout=TIMEOUT)
    assert "cj-banner" in _markdown(at) and len(at.get("download_button")) == 1

    other = app.settings_from_form({**app.default_form_values(PILOT_ACTIVATION), "run_id": "R02"})
    at.session_state[app.RESULT_KEY] = app.StoredRun(digest, other, result=stub, zip_bytes=b"zip")
    at.run(timeout=TIMEOUT)
    assert "cj-banner" not in _markdown(at) and not at.get("download_button")


def test_csv_table_keeps_the_first_rows_as_written():
    content = b"A,B,C\n1,,x\n2,\"a,b\",\n3,True,\n"
    assert app.csv_table(content, limit=2) == (["A", "B", "C"], [["1", "", "x"], ["2", "a,b", ""]], 3)
    assert app.csv_table(content)[1][-1] == ["3", "True", ""]
    assert app.csv_table(b"") == ([], [], 0)
    assert app.csv_table(b"A,B\n") == (["A", "B"], [], 0)
    frame = app._frame(["A", "A", "B"], [["1", "2"]])
    assert list(frame.columns) == ["A", "A (2)", "B"] and list(frame.iloc[0]) == ["1", "2", ""]


def test_zip_file_name():
    assert app.zip_file_name(PILOT_JOURNEY) == "CJ_SkYDE_NCRM_Demo_final_test_data.zip"
    assert app.zip_file_name("Journey 1/2: test?") == "Journey 1_2_ test__test_data.zip"
    assert app.zip_file_name("") == "journey_test_data.zip"


@pytest.mark.slow
@needs_pilot
def test_generate_on_pilot_then_download_without_generating_again(pilot_bytes, monkeypatch):
    calls = []
    real_generate = pipeline.generate

    def counting_generate(*args, **kwargs):
        calls.append(args[1] if len(args) > 1 else kwargs.get("settings"))
        return real_generate(*args, **kwargs)

    monkeypatch.setattr(pipeline, "generate", counting_generate)
    at = AppTest.from_function(_render_page, args=(pilot_bytes,)).run(timeout=TIMEOUT)
    _widget(at.text_input, app.SETTING_LABELS["real_inbox"]).input("tester@example.com")
    _widget(at.button, app.GENERATE_LABEL).click().run(timeout=600)
    assert not at.exception and not at.error
    assert len(calls) == 1 and calls[0].real_inbox == "tester@example.com" and calls[0].contacts_per_case == 3

    stored = at.session_state[app.RESULT_KEY]
    result = stored.result
    text = _markdown(at)
    assert _plain(_card(at, app.STEP_GENERATE).label).endswith(
        f"Completato alle {result.generation_time:%H:%M} · run R01")
    covered, manual, missing, total = app.branch_counts(result)
    stats = result.coverage.stats
    for value, label in [(stats["scenarios"], "Scenari"), (stats["cases"], "Casi"), (stats["contacts"], "Contatti"),
                         (missing, "Branch non coperti")]:
        assert f"<b>{value}</b><span>{label}</span>" in text
    assert f"<b>{covered} <small>/ {total}</small></b><span>Branch coperti, {manual} con azione manuale</span>" in text
    assert total == stats["covered"] + stats["covered_manual"] + stats["not_covered"]
    captions = _captions(at)
    assert any(re.match(r"\d+ dei \d+ branch non coperti stanno dietro a \d+ branch che nessun contatto può "
                        r"raggiungere", c) for c in captions)
    assert not any("Non è stata indicata un'inbox reale" in c for c in captions)
    assert "2. Nei CSV è attivo solo TST_R01_S01, il primo scenario in ordine di esecuzione." in text
    assert "imposta DE_NCRM_AD_HOC_CONFIG.ACTIVE a True sulla sua riga" in text
    assert len(re.findall(r"^- :(green|red)-badge\[(Superato|Non superato)\] ", text,
                          flags=re.MULTILINE)) == len(result.checks)
    tabs = [tab.label for tab in at.tabs]
    assert tabs[0] == "scenarios.csv"
    assert sorted(tabs) == sorted([f"{t}.csv" for t in PILOT_CSV_TABLES] + ["scenarios.csv"])
    frames = dict(zip(tabs, (frame.value for frame in at.dataframe)))
    assert len(frames) == len(tabs) and not at.table and not at.code
    entry = frames["DE_NCM_AdHoc_Entry.csv"]
    entry_columns = [f.name for f in result.package.tables["DE_NCM_AdHoc_Entry"].fields]
    assert list(entry.columns) == entry_columns
    file_rows = list(csv.reader(result.files["DE_NCM_AdHoc_Entry.csv"].decode("utf-8").splitlines()))
    assert [list(row) for row in entry.itertuples(index=False)] == file_rows[1:app.PREVIEW_ROWS + 1]
    assert all(len(frame) <= app.PREVIEW_ROWS for frame in frames.values())
    with zipfile.ZipFile(io.BytesIO(stored.zip_bytes)) as archive:
        assert archive.namelist() == list(result.files)
    assert sorted(result.files) == sorted([f"{t}.csv" for t in PILOT_CSV_TABLES]
                                          + [outputs.SCENARIOS_FILE, outputs.TESTBOOK_FILE, outputs.REPORT_FILE])
    assert "CJ_SkYDE_NCRM_Demo_final_test_data.zip contiene 7 file:" in text
    assert "UPDATECONTACTDATAV2" not in text and "MULTICRITERIADECISIONV2" not in text

    at.get("download_button")[0].click().run(timeout=TIMEOUT)
    assert not at.exception
    assert len(calls) == 1                     # the download reran the page, not the generation
    assert at.session_state[app.RESULT_KEY] is stored
    assert len(at.get("download_button")) == 1
