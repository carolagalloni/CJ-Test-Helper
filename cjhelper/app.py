"""The one-page Streamlit app (spec priority 5, "The app").

Spec: "The app is a single Streamlit page run locally, with no login and nothing stored: the file is
processed in memory and discarded. The tester goes through four steps on the same page":

1. Upload the package export JSON.
2. Review what was detected: journey name, number of activities by type, the tables with their role,
   and any warnings.
3. Adjust settings if needed; every setting has a default.
4. Generate, look at the summary and a preview of each CSV, then download the zip.

Spec, "Behaviour at the edges": "A file that is not valid JSON, or holds no journey, gives a plain
error message and nothing else."

Spec, "Extensions beyond the pilot": with a Run Once entry source "All scenarios enter together, so 'one
active scenario at a time' is not offered"; "report.md and the page open with a warning when unsupported
elements leave branches not covered" (``outputs.unsupported_warning``); the notes before importing follow
the entry source, as report.md's How to run does.

The page is a thin layer ("Code structure": the Streamlit page is a thin layer on top of the
package). Every decision comes from the package: ``loader`` and ``tables`` for what was detected,
``model.Settings`` for the defaults, ``pipeline.generate`` for the files and checks, the generated
rows (``scenarios.csv``) for the active scenario and its activation field, ``outputs`` for reading
the CSVs back, for the file names and for the zip. This module only turns their results into
widgets and text. The look follows docs/brand.md (layout, colours, fonts) and docs/ux-review.md.

Run it with ``streamlit run cjhelper/app.py`` from the repository root, so that
``.streamlit/config.toml`` applies. ``main()`` draws the header, the stepper and step 1 and passes the uploaded
bytes to ``render()``, which draws steps 2 to 4; the tests drive ``render()`` directly.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import html
import io
import json
import logging
import re
import sys
import traceback
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional

import streamlit as st

# ``streamlit run cjhelper/app.py`` runs this file as a script, with only cjhelper/ on the path.
_ROOT = str(Path(__file__).resolve().parent.parent)
if not __package__ and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from cjhelper import loader, outputs, pipeline, tables  # noqa: E402
from cjhelper.loader import LoadError  # noqa: E402
from cjhelper.model import (  # noqa: E402
    ENTRY_AUTOMATION,
    ENTRY_RUN_ONCE,
    ENTRY_SCHEDULED,
    KIND_DECISION,
    KIND_EMAIL,
    KIND_ENGAGEMENT,
    KIND_RANDOM,
    KIND_SMS,
    KIND_UNSUPPORTED,
    KIND_UPDATE,
    KIND_WAIT_ATTRIBUTE,
    KIND_WAIT_DURATION,
    KIND_WAIT_STO,
    STATUS_NOT_COVERED,
    FieldRef,
    GenerationResult,
    Journey,
    Package,
    Settings,
    TableModel,
)

LOG = logging.getLogger(__name__)

TITLE = "CJ Test Helper"
# The spec's four steps: the title of each card, and its name and description in the stepper.
STEP_UPLOAD = "Carica l'export"
STEP_REVIEW = "Verifica il rilevamento"
STEP_SETTINGS = "Impostazioni"
STEP_GENERATE = "Genera e scarica"
STEPPER = (("Carica", "Export del package"), ("Verifica", "Cosa è stato rilevato"),
           ("Imposta", "Valori modificabili"), ("Genera e scarica", ""))
UPLOAD_LABEL = "Export del package (JSON)"
UPLOAD_NOTE = ("Il file JSON che Package Manager esporta per il journey. Viene letto in memoria e poi scartato: "
               "non viene salvato nulla.")
GENERATE_LABEL = "Genera"
GENERATE_NOTE = "La generazione può richiedere circa un minuto. Il risultato compare qui sotto al termine."
SPINNER_TEXT = "Generazione dei dati di test in corso. Può richiedere circa un minuto."
DOWNLOAD_LABEL = "Scarica lo zip"
MORE_SETTINGS = "Altre impostazioni"
TECHNICAL_DETAILS = "Dettagli tecnici"

# Spec, "Behaviour at the edges": a plain message; it ends with what to do next.
NEXT_STEP = "Scegli il file JSON prodotto da Package Manager."
UNEXPECTED_READ = "Il file non è stato letto a causa di un errore imprevisto del generatore."
UNEXPECTED_GENERATION = "I dati di test non sono stati generati a causa di un errore imprevisto del generatore."

PREVIEW_ROWS = 10        # first rows of each CSV shown in step 4
DETAIL_LIMIT = 20        # details shown under a failed trust check

DETECTION_KEY = "cjhelper.detection"   # st.session_state: (file digest, DetectionSummary)
RESULT_KEY = "cjhelper.result"         # st.session_state: StoredRun of the last generation
PENDING_KEY = "cjhelper.pending"       # st.session_state: (file digest, Settings) from the click to the generation
UPLOAD_KEY = "cjhelper.upload"         # the file uploader

# Where the page is, from the file and the session state. It decides which cards are open and what the stepper shows.
PHASE_EMPTY = "empty"          # no file
PHASE_ERROR = "error"          # the file cannot be read
PHASE_REVIEW = "review"        # read; nothing generated for it yet
PHASE_RUNNING = "running"      # Generate was clicked; this run generates
PHASE_DONE = "done"            # generated
PHASE_FAILED = "failed"        # the generation failed

STATIC = Path(__file__).resolve().parent / "static"
STYLE_FILE = Path(__file__).resolve().parent / "app.css"
LOGO_FILE = STATIC / "lutech-logo.svg"
ICON_FILE = STATIC / "lutech-pictogram.svg"

# The page, the messages of the generator (warnings, trust check names, loader errors) and the downloaded
# files are in Italian (glossary in docs/brand.md). Salesforce's own names (activity types, the roles of
# report.md) and the team's English terms stay in English.

# Plain names of the activity kinds, in the order of the spec's "Supported in the PoC" table.
ACTIVITY_NAMES: dict[str, str] = {
    KIND_DECISION: "Decision split",
    KIND_ENGAGEMENT: "Engagement split",
    KIND_RANDOM: "Random split",
    KIND_WAIT_DURATION: "Wait by duration",
    KIND_WAIT_ATTRIBUTE: "Wait by attribute",
    KIND_WAIT_STO: "Send-time wait",
    KIND_EMAIL: "Email",
    KIND_SMS: "SMS",
    KIND_UPDATE: "Update contact",
    KIND_UNSUPPORTED: "Non supportata",
}

# The level of a table (``TableLine.level``) as the page writes it.
LEVEL_NAMES: dict[str, str] = {"contact": "contatto", "scenario": "scenario"}

# The manual actions of a case (``ManualAction.kind``) as a verb.
ACTION_VERBS: dict[str, str] = {"open": "aprire", "click": "cliccare"}

# The spec's settings.
SETTING_LABELS: dict[str, str] = {
    "contacts_per_case": "Contatti per caso",
    "contacts_per_random_case": "Contatti per caso di random split",
    "real_inbox": "Inbox reale per i test di open e click",
    "real_phone": "Numero di telefono reale",
    "fake_phone": "Numero di telefono fittizio",
    "window_start_days": "Inizio della finestra di validità (giorni dalla generazione)",
    "window_end_days": "Fine della finestra di validità (giorni dalla generazione)",
    "wait_offset_hours": "Offset della data di wait (ore)",
    "wait_offset_before_engagement_hours": "Offset della data di wait prima di un engagement split (ore)",
    "date_format": "Formato delle date nei CSV",
    "one_active_scenario": "Un solo scenario attivo alla volta",
    "activation_field": "Campo di attivazione",
    "run_id": "Run identifier nelle chiavi",
    "case_insensitive_text": "I confronti di testo ignorano maiuscole e minuscole",
    "min_validity_days": "Validità minima dei dati (giorni)",
}
NUMBER_SETTINGS = ("contacts_per_case", "contacts_per_random_case", "window_start_days", "window_end_days",
                   "wait_offset_hours", "wait_offset_before_engagement_hours", "min_validity_days")
TEXT_SETTINGS = ("real_inbox", "real_phone", "fake_phone", "run_id")

# Visible notes under a field (spec wording where it has some).
SETTING_NOTES: dict[str, str] = {
    "real_inbox": ("I contatti che devono aprire o cliccare un'email ricevono questa inbox con plus-addressing. "
                   "Se resta vuota, ricevono indirizzi non recapitabili e i branch relativi risultano non coperti."),
    "run_id": ("Viene aggiunto a ogni chiave, per esempio TST_R01_C0001. Usane uno nuovo a ogni generazione, così "
               "le evidenze di due run non si mescolano."),
    "real_phone": ("Lo riceve un contatto per ogni caso il cui percorso invia un SMS, così il tester può controllarlo. "
                   "Se resta vuoto, l'SMS non può essere verificato."),
}
SETTING_HELP: dict[str, str] = {
    "fake_phone": "Il numero palesemente fittizio scritto per ogni contatto.",
    "wait_offset_hours": ("Le date dei wait by attribute sono l'ora di generazione più questo offset: con 0 il wait "
                          "termina subito."),
    "wait_offset_before_engagement_hours": ("Per un wait by attribute sul percorso verso un engagement split: il "
                                            "tempo a disposizione per aprire l'email."),
    "activation_field": ("Un campo Boolean di una tabella a livello di scenario. Con un solo scenario attivo alla "
                         "volta, nei CSV è attivo solo il primo scenario."),
    "case_insensitive_text": ("Resta da verificare se Salesforce tratta Email ed EMAIL come lo stesso valore; con "
                              "l'opzione disattivata sono valori diversi."),
    "min_validity_days": ("I valori scelti rispetto all'ora di ingresso restano validi almeno questi giorni, dove le "
                          "classi di valori lo permettono. Con 0 i dati vanno attivati lo stesso giorno."),
}
DEFAULT_DATE_PATTERN = "YYYY-MM-DD HH:MM"     # how the spec writes the default date format

# What each file of the zip is for (spec, "Outputs").
FILE_PURPOSES: dict[str, str] = {
    outputs.SCENARIOS_FILE: "gli scenari in ordine di esecuzione, con quello attivo all'inizio",
    outputs.TESTBOOK_FILE: "i test e i risultati attesi",
    outputs.REPORT_FILE: "cosa è coperto, cosa no e perché, e come eseguire i test",
}

# Text shown with st.markdown is escaped, so that names such as _Sent or values with * stay plain.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()<>#+\-.!|~$:&@])")
_FILE_NAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_PARENTHESES = re.compile(r"\s*\(([^()]*)\)")
_LIST_SEPARATOR = re.compile(r",\s*|\s+and\s+")


# ---------------------------------------------------------------------------
# What step 2 shows (load and table model only, no generation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TableLine:
    """One table of the export as step 2 shows it, in the words of report.md's Tables section."""
    name: str
    role: str              # report.md's role names, e.g. "source, split", "written", "nessuno"
    level: str             # "contact", "scenario", or "-" for a table without a CSV
    csv: bool              # gets a CSV
    managed: bool = False  # a synchronized (managed) data extension: shown as such, its CSV is imported as usual


@dataclass
class DetectionSummary:
    """Spec, step 2: "journey name, number of activities by type, the tables with their role, and any warnings"."""
    journey_name: str
    activity_count: int
    activities: list[tuple[str, int]]      # (plain activity type, count), in the spec's order
    tables: list[TableLine]                # the tables that get a CSV first, then the others
    csv_files: list[str]                   # file names of the tables that get a CSV
    warnings: list[str]                    # from loading and table selection, one plain line each
    ids_left_out: bool                     # some warnings named activities that only report.md lists
    activation_field: Optional[FieldRef]   # suggested from the entry query filters, or None
    activation_choices: list[FieldRef]     # Boolean fields of the scenario-level tables
    entry_kind: str = ENTRY_AUTOMATION     # EntryEvent.kind: with Run Once, one active scenario is not offered
    exits: list[str] = field(default_factory=list)   # the exit criteria, one line each (step 3, "Exit criteria")


def activity_counts(journey: Journey) -> list[tuple[str, int]]:
    """Number of activities of each kind present in the journey, with plain names, in the spec's order."""
    counts = Counter(activity.kind for activity in journey.activities.values())
    known = [kind for kind in ACTIVITY_NAMES if counts[kind]]
    others = sorted(kind for kind in counts if kind not in ACTIVITY_NAMES)
    return [(ACTIVITY_NAMES.get(kind, kind), counts[kind]) for kind in known + others]


def role_text(roles: Iterable[str]) -> str:
    """The roles of a table as report.md writes them, e.g. ``{"split", "source"}`` -> "source, split"."""
    return outputs.role_text(roles)


def exit_lines(package: Package) -> list[str]:
    """Spec, step 3, "Exit criteria and goals": each exit criterion of the journey with whether the generator
    evaluates it, as report.md's Criteri di uscita section says."""
    return [f"{outputs.exit_description(e)}: {outputs.exit_evaluation_text(e)}"
            for e in getattr(package.journey, "exits", None) or []]


def table_lines(table_model: TableModel, managed: Iterable[str] = ()) -> list[TableLine]:
    """Spec, step 2: "the tables with their role"; the tables that get a CSV come first."""
    managed = set(managed)
    lines = [TableLine(name=role.name, role=role_text(role.roles),
                       level=(role.level or "-") if role.selected else "-", csv=role.selected,
                       managed=role.name in managed)
             for role in table_model.tables.values()]
    return [line for line in lines if line.csv] + [line for line in lines if not line.csv]


def activation_choices(package: Package, table_model: TableModel) -> list[FieldRef]:
    """The fields that can be the activation field: the Boolean fields of the scenario-level tables.

    The suggestion of the table model comes first if it is not among them, so it can always be chosen.
    """
    choices: list[FieldRef] = []
    for name in table_model.scenario_tables:
        table = package.tables.get(name)
        if table is not None:
            choices.extend((table.name, f.name) for f in table.fields if f.type.lower() == "boolean")
    suggested = table_model.activation_field
    if suggested is not None and suggested not in choices:
        choices.insert(0, suggested)
    return choices


def warning_lines(warnings: Iterable[str], activity_keys: Iterable[str]) -> tuple[list[str], bool]:
    """One sentence-case line per warning, suspicious patterns first, as report.md orders them.

    A list of activity keys in parentheses is left to report.md, which names them; the second value
    says whether that happened. Suspicious patterns are marked by other modules with the prefix
    ``outputs.SUSPICIOUS_PREFIX``.
    """
    keys = set(activity_keys)
    patterns: list[str] = []
    others: list[str] = []
    left_out = False
    for warning in _unique(_one_line(w) for w in warnings):
        text, removed = _without_key_lists(warning, keys)
        left_out = left_out or removed
        if text.lower().startswith(outputs.SUSPICIOUS_PREFIX):
            rest = text[len(outputs.SUSPICIOUS_PREFIX):].strip()
            patterns.append(f"Pattern sospetto: {rest}")
        else:
            others.append(text[:1].upper() + text[1:])
    return patterns + others, left_out


def summarize_detection(package: Package, table_model: TableModel) -> DetectionSummary:
    lines, left_out = warning_lines(list(package.warnings) + list(table_model.warnings), package.journey.activities)
    return DetectionSummary(
        journey_name=package.journey.name,
        activity_count=package.journey.activity_count,
        activities=activity_counts(package.journey),
        tables=table_lines(table_model, [t.name for t in package.tables.values() if getattr(t, "managed", False)]),
        csv_files=[outputs.table_file_name(name) for name in table_model.selected_tables],
        warnings=lines,
        ids_left_out=left_out,
        activation_field=table_model.activation_field,
        activation_choices=activation_choices(package, table_model),
        entry_kind=outputs.entry_kind(package),
        exits=exit_lines(package),
    )


def load_summary(data: bytes) -> DetectionSummary:
    """Read the export and select its tables, without generating anything. Raises LoadError."""
    package = loader.load_package(data)
    return summarize_detection(package, tables.build_table_model(package))


# ---------------------------------------------------------------------------
# Settings (step 3)
# ---------------------------------------------------------------------------

def default_form_values(activation_field: Optional[FieldRef], entry_kind: str = ENTRY_AUTOMATION) -> dict[str, Any]:
    """The value each setting widget starts with: the spec defaults, which are ``Settings()``'s.

    Spec settings table: "One active scenario at a time: On when an activation field is detected";
    "Activation field: Suggested from the entry query filters" (``activation_field``, or None). Spec,
    "Entry source and procedure": with a Run Once entry "All scenarios enter together, so 'one active
    scenario at a time' is not offered": the setting is off and has no activation field.
    """
    defaults = Settings()
    values = {name: getattr(defaults, name) for name in SETTING_LABELS}
    run_once = entry_kind == ENTRY_RUN_ONCE
    values["one_active_scenario"] = activation_field is not None and not run_once
    values["activation_field"] = None if run_once else activation_field
    return values


def settings_from_form(values: Mapping[str, Any]) -> Settings:
    """Map the values of the settings widgets to ``Settings``.

    Numbers become integers and the one-line texts lose surrounding spaces; the date format is kept as
    typed. The activation field only applies when one active scenario at a time is on. The settings
    that the spec does not list (seed, generation time) keep their defaults.
    """
    one_active = bool(values["one_active_scenario"])
    activation = values.get("activation_field") if one_active else None
    return Settings(
        **{name: int(values[name]) for name in NUMBER_SETTINGS},
        **{name: str(values[name] or "").strip() for name in TEXT_SETTINGS},
        date_format=str(values["date_format"]),
        one_active_scenario=one_active,
        activation_field=tuple(activation) if activation else None,
        case_insensitive_text=bool(values["case_insensitive_text"]),
    )


def days_text(days: int) -> str:
    """A validity window bound in words, as report.md's "Settings used" says it."""
    if days == 0:
        return "Al momento della generazione."
    side = "prima della" if days < 0 else "dopo la"
    return f"{_count(abs(days), 'giorno', 'giorni')} {side} generazione."


def date_format_note(now: datetime) -> str:
    default = Settings().date_format
    return f"Di default le date sono scritte come {DEFAULT_DATE_PATTERN}, per esempio {now.strftime(default)}."


# ---------------------------------------------------------------------------
# What step 4 shows
# ---------------------------------------------------------------------------

@dataclass
class StoredRun:
    """The last generation, kept in st.session_state so that a download does not generate again."""
    digest: str                                 # of the uploaded bytes it was generated from
    settings: Settings                          # it was generated with
    result: Optional[GenerationResult] = None   # None when it failed
    zip_bytes: bytes = b""
    error: str = ""                             # one plain sentence when it failed
    details: str = ""                           # the exception and where it happened


def done_summary(result: GenerationResult) -> str:
    """Step 4's summary line once the data is generated: when, and with which run identifier."""
    run_id = result.settings.run_id
    return f"Completato alle {result.generation_time:%H:%M}" + (f" · run {run_id}" if run_id else "")


def checks_banner(result: GenerationResult) -> tuple[str, str, bool]:
    """The trust checks' result at the top of step 4: a short count, one sentence, and whether all passed."""
    total = len(result.checks)
    passed = sum(1 for check in result.checks if check.passed)
    failed = total - passed
    if failed:
        what = "trust check non superato" if failed == 1 else "trust check non superati"
        return (f"{passed} di {total}", f"{failed} {what}: leggi i dettagli qui sotto prima di importare.", False)
    if total:
        return f"{total} di {total}", "Tutti i trust check sono superati.", True
    return "0 di 0", "Nessun trust check è stato eseguito.", False


def branch_counts(result: GenerationResult) -> tuple[int, int, int, int]:
    """Covered (with or without a manual action), of which with a manual action, not covered, and all branches."""
    stats = result.coverage.stats
    covered, manual, missing = stats.get("covered", 0), stats.get("covered_manual", 0), stats.get("not_covered", 0)
    return covered + manual, manual, missing, covered + manual + missing


def summary_cells(result: GenerationResult) -> list[tuple[str, str, str]]:
    """Spec, "Outputs": "number of scenarios, cases and contacts, and branches covered, covered with manual
    action, and not covered" (the counts of the coverage, as in report.md and the command line).

    One cell per count: (value, what it is out of, label). The branches covered count those covered with a
    manual action too, which the label then names.
    """
    stats = result.coverage.stats
    covered, manual, missing, total = branch_counts(result)
    return [
        (str(stats.get("scenarios", 0)), "", "Scenari"),
        (str(stats.get("cases", 0)), "", "Casi"),
        (str(stats.get("contacts", 0)), "", "Contatti"),
        (str(covered), f"/ {total}", f"Branch coperti, {manual} con azione manuale"),
        (str(missing), "", "Branch non coperti"),
    ]


def summary_notes(result: GenerationResult) -> list[str]:
    """Plain sentences under the counts.

    - The branches not covered that lie behind another branch (``BranchStatus.behind``, the root cause
      report.md groups them by), so that the count does not read as a failure.
    - Spec, "Behaviour at the edges": "If the real inbox is empty, generation still works. The open and
      click contacts get undeliverable addresses, and those branches are reported as not covered."
    """
    notes: list[str] = []
    missing = [b for b in result.coverage.branches if b.status == STATUS_NOT_COVERED]
    behind = [b for b in missing if b.behind is not None]
    if behind:
        roots = {tuple(b.behind) for b in behind}
        verb = "sta" if len(behind) == 1 else "stanno"
        notes.append(f"{len(behind)} dei {len(missing)} branch non coperti {verb} dietro a {len(roots)} branch che "
                     f"nessun contatto può raggiungere (vedi {outputs.REPORT_FILE}).")
    elif missing:
        notes.append(f"{outputs.REPORT_FILE} indica il motivo di ogni branch non coperto.")
    if _inbox_missing(result):
        kinds = _unique(action.kind for case in result.coverage.cases for action in case.manual_actions)
        verbs = _join_words([ACTION_VERBS.get(kind, kind) for kind in kinds], "o")
        notes.append(f"Non è stata indicata un'inbox reale: i contatti che devono {verbs} un'email ricevono "
                     "indirizzi non recapitabili e i branch relativi risultano non coperti.")
    return notes


def _inbox_missing(result: GenerationResult) -> bool:
    """Some case needs a manual action on an email, and no real inbox was given."""
    needed = any(case.manual_actions for case in result.coverage.cases)
    return needed and not result.settings.real_inbox.strip()


def import_notes(result: GenerationResult) -> list[str]:
    """Spec, "One scenario at a time" and "Data is read live", in the words of report.md's How to run.

    The active scenario and its activation field come from the generated scenarios.csv rows. Spec, "Entry
    source and procedure": the entry procedure follows the entry source (``outputs.entry_kind``).
    """
    notes = [f"Apri prima {outputs.REPORT_FILE}: la sezione Come eseguire i test indica l'ordine di import."]
    notes.extend(entry_notes(result))
    rows = result.data.scenarios_rows
    active = [row for row in rows if str(row.get("active_at_start", "")).lower() == "yes"]
    field = next((row.get("activation_field") for row in rows if row.get("activation_field")), None)
    if field and len(active) == 1:
        notes.append(f"Nei CSV è attivo solo {active[0].get('scenario_key')}, il primo scenario in ordine di "
                     "esecuzione.")
        notes.append(f"Esegui gli scenari uno alla volta, in ordine di esecuzione. Quando i contatti di uno scenario "
                     f"sono usciti dal journey, attiva il successivo: imposta {field} a True sulla sua riga e a False "
                     "su tutte le altre. Non cambiare altro e non reimportare nulla.")
    elif rows:
        notes.append("Nei CSV tutti gli scenari sono attivi, quindi vengono eseguiti insieme.")
    return notes


def entry_notes(result: GenerationResult) -> list[str]:
    """The entry procedure for an entry source other than the automation of the pilot (spec, "Entry source and
    procedure"); nothing for an automation entry, whose procedure is the import order of report.md."""
    kind = outputs.entry_kind(result.package)
    if kind == ENTRY_AUTOMATION:
        return []
    entry = getattr(getattr(result, "table_model", None), "entry_table", None) or "la tabella di ingresso"
    if kind == ENTRY_RUN_ONCE:
        return [f"Entry source Run Once: importa tutti i CSV, poi attiva il journey. Entrano solo le righe presenti in "
                f"{entry} all'attivazione, che deve contenere solo righe di test. Un nuovo tentativo richiede una "
                "nuova versione del journey e un nuovo run identifier."]
    if kind == ENTRY_SCHEDULED:
        return [f"Entry source con pianificazione ricorrente: i contatti dovrebbero entrare alla valutazione "
                f"successiva di {entry}. La procedura è da verificare."]
    return [f"L'entry source è {outputs.entry_source_text(result.package)}: i contatti non entrano con l'import di "
            f"un CSV. Usa una copia del journey con entry source una data extension con le stesse colonne di {entry}, "
            "e importa lì il CSV di ingresso."]


def generation_warnings(result: GenerationResult) -> tuple[list[str], bool]:
    """Warnings of the coverage and of the data generation (step 2 already showed the others). The
    missing real inbox is said in the summary instead (``summary_notes``)."""
    inbox_note = _inbox_missing(result)
    warnings = [w for w in list(result.coverage.warnings) + list(result.data.warnings)
                if not (inbox_note and w.startswith("Nessuna inbox reale"))]
    return warning_lines(warnings, result.package.journey.activities)


def csv_table(content: bytes, limit: int = PREVIEW_ROWS) -> tuple[list[str], list[list[str]], int]:
    """The header and the first ``limit`` rows of a written CSV, every value as the file writes it, and the
    number of rows."""
    records = [record for record in csv.reader(io.StringIO(content.decode("utf-8-sig"), newline="")) if record]
    if not records:
        return [], [], 0
    return records[0], records[1:limit + 1], len(records) - 1


def preview_files(result: GenerationResult) -> list[str]:
    """The CSVs of the zip, scenarios.csv first because it is the run plan."""
    names = [name for name in result.files if name.lower().endswith(".csv")]
    return sorted(names, key=lambda name: name != outputs.SCENARIOS_FILE)


def download_lines(result: GenerationResult) -> list[str]:
    """Spec, "Outputs": what each file of the zip is for."""
    data_files = [name for name in result.files if name.lower().endswith(".csv") and name not in FILE_PURPOSES]
    lines = []
    if data_files:
        lines.append(f"{_join_words(data_files, 'e')}: i dati da importare.")
    for name in result.files:
        if name in FILE_PURPOSES:
            lines.append(f"{name}: {FILE_PURPOSES[name]}.")
        elif name not in data_files:
            lines.append(f"{name}.")
    return lines


def zip_file_name(journey_name: str) -> str:
    """``<journey name>_test_data.zip``; characters a file name cannot hold become ``_``."""
    safe = _FILE_NAME_UNSAFE.sub("_", journey_name).strip(" .")
    return f"{safe or 'journey'}_test_data.zip"


# ---------------------------------------------------------------------------
# Error messages
# ---------------------------------------------------------------------------

def is_file_error(error: BaseException) -> bool:
    """The errors that say something about the file: not valid JSON, not UTF-8, no journey, no entry table."""
    return isinstance(error, (LoadError, json.JSONDecodeError, UnicodeDecodeError))


def error_message(error: BaseException) -> str:
    """Plain words for a file that cannot be read, ending with what to do next.

    Spec, "Behaviour at the edges": "A file that is not valid JSON, or holds no journey, gives a plain
    error message and nothing else." LoadError already says why in plain words; for a JSON error the
    position comes from the parser's error that LoadError replaced.
    """
    if not is_file_error(error):
        return UNEXPECTED_READ
    parse_error = error if isinstance(error, json.JSONDecodeError) else _json_error_behind(error)
    if parse_error is not None:
        text = (f"Il file non è un JSON valido: c'è un problema alla riga {parse_error.lineno}, "
                f"colonna {parse_error.colno}.")
    elif isinstance(error, UnicodeDecodeError):
        text = "Il file non è testo UTF-8, quindi non è un export JSON valido."
    else:   # the loader's own words, in Italian like the other messages of the generator
        text = str(error).strip() or "Il file non può essere letto come export di un package."
    if not text.endswith("."):
        text += "."
    return f"{text} {NEXT_STEP}"


def technical_details(error: BaseException) -> str:
    return "".join(traceback.format_exception(type(error), error, error.__traceback__)).strip()


def _json_error_behind(error: BaseException) -> Optional[json.JSONDecodeError]:
    for cause in (error.__cause__, error.__context__):
        if isinstance(cause, json.JSONDecodeError):
            return cause
    return None


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def main() -> None:
    """Header, stepper and step 1; the uploaded bytes go to ``render``, which draws steps 2 to 4.

    The look is docs/brand.md's: a header with the logo, a stepper, and one card per step that closes
    with a one-line summary once its step is done.
    """
    st.set_page_config(page_title=TITLE, page_icon=str(ICON_FILE), layout="wide")
    st.html(STYLE_FILE)   # a .css path: Streamlit wraps it in <style>
    st.html(_header_html())
    stepper = st.empty()
    uploaded = st.session_state.get(UPLOAD_KEY)
    data = uploaded.getvalue() if uploaded is not None else None
    phase = _phase(data)
    stepper.html(_stepper_html(phase))   # drawn now, so that it shows during a generation too
    if uploaded is None:
        summary = _md("il file JSON che Package Manager esporta per il journey")
    else:
        summary = _md(f"{uploaded.name} · {_size_text(len(data))}")
    with _card(1, "upload", STEP_UPLOAD, summary, expanded=phase in (PHASE_EMPTY, PHASE_ERROR),
               state="now" if phase in (PHASE_EMPTY, PHASE_ERROR) else "done"):
        st.file_uploader(UPLOAD_LABEL, key=UPLOAD_KEY)
        st.caption(_md(UPLOAD_NOTE))
    final = render(data)
    if final != phase:
        stepper.html(_stepper_html(final))


def render(data: Optional[bytes]) -> str:
    """Steps 2 to 4 for the uploaded bytes (None: no file, so nothing to show). Returns the phase.

    Generate takes three short runs of the page, so that its button is disabled while it works: the
    click keeps the settings as pending, the next run shows the button disabled and generates, and a
    last run shows the result with the button enabled again. (Streamlit ignores a click on a button
    that is drawn disabled in the same run, so the click and the generation cannot share one run.)

    Each card's ``expanded`` follows the phase, so a card opens or closes when the phase changes and
    otherwise stays as the tester left it.
    """
    if data is None:
        _forget()
        return PHASE_EMPTY
    try:
        digest, summary = _detection(data)
    except Exception as error:   # spec: a plain error message and nothing else
        _forget()
        if is_file_error(error):
            st.error(_md(error_message(error)))
        else:
            LOG.exception("The export could not be read")
            _show_failure(UNEXPECTED_READ, technical_details(error))
        return PHASE_ERROR
    phase = _phase(data)
    review = phase == PHASE_REVIEW
    with _card(2, "review", STEP_REVIEW, _detection_summary(summary), expanded=review, state="done"):
        _show_detection(summary)
    pending = st.session_state.get(PENDING_KEY)
    running = pending is not None and pending[0] == digest
    with _card(3, "settings", STEP_SETTINGS, _settings_summary(phase), expanded=review,
               state="now" if review else "done"):
        settings, submitted = _settings_form(summary, running)
    if submitted:
        st.session_state.pop(RESULT_KEY, None)
        st.session_state[PENDING_KEY] = (digest, settings)
        st.rerun()
    stored = st.session_state.get(RESULT_KEY)
    shown = stored if stored is not None and stored.digest == digest and stored.settings == settings else None
    with _card(4, "generate", STEP_GENERATE, _generate_summary(phase, shown), expanded=not review,
               state="todo" if review else "now"):
        slot = st.empty()   # the waiting note, then the spinner in its place: no faded note during a generation
        if running:
            st.session_state.pop(PENDING_KEY, None)
            with slot.container():
                _generate(data, digest, pending[1])
        if shown is None:
            slot.caption("I dati di test compaiono qui dopo che premi Genera nelle impostazioni.")
        elif shown.result is None:
            _show_failure(shown.error, shown.details)
        else:
            render_result(shown.result, shown.zip_bytes)
    return phase


def render_result(result: GenerationResult, zip_bytes: bytes) -> None:
    """Step 4 after a generation, in the order a tester needs it: the warning on unsupported elements when they
    leave branches not covered, the trust checks' result, the counts,
    what to do before importing beside the trust checks one by one, a preview of each CSV, the download,
    and the warnings of the generation."""
    warning = outputs.unsupported_warning(result)
    if warning:   # spec, "Honest outcomes": the page opens with this warning
        st.warning(_md(f"{warning} {outputs.REPORT_FILE} spiega il motivo nelle sezioni Branch non coperti ed "
                       "Elementi non supportati."))
    deadline = outputs.activation_deadline_text(result)
    if deadline:   # spec, step 3, "Honest checks": the deadline for importing and activating, when the data is perishable
        st.warning(_md(deadline))
    chip, sentence, ok = checks_banner(result)
    st.markdown(f'<div class="cj-banner{"" if ok else " failed"}"><span class="cj-chip">{_html(chip)}</span>'
                f'<span>{_html(sentence)}</span></div>', unsafe_allow_html=True)
    st.markdown(_strip_html(result), unsafe_allow_html=True)
    for note in summary_notes(result):
        st.caption(_md(note))

    left, right = st.columns([1.1, 0.9], gap="large")
    with left:
        _label("Prima di importare")
        st.markdown("\n".join(f"{n}. {_md(_one_line(note))}" for n, note in enumerate(import_notes(result), 1)))
    with right:
        _label("Trust check")
        checks_box = st.container(key="checks")
        lines: list[str] = []
        for check in result.checks:
            badge = ":green-badge[Superato]" if check.passed else ":red-badge[Non superato]"
            lines.append(f"- {badge} {_md(_one_line(check.name))}")
            if not check.passed:
                lines.extend(f"    - {_md(_one_line(detail))}" for detail in check.details[:DETAIL_LIMIT])
                if len(check.details) > DETAIL_LIMIT:
                    lines.append(f"    - e altri {len(check.details) - DETAIL_LIMIT}")
        if lines:
            checks_box.markdown("\n".join(lines))

    _label("Anteprima dei CSV")
    names = preview_files(result)
    with st.container(key="previews"):
        for tab, name in zip(st.tabs(names), names):
            with tab:
                header, rows, total = csv_table(result.files[name])
                st.dataframe(_frame(header, rows), hide_index=True)
                shown = f"Prime {len(rows)} di {total} righe" if total > len(rows) else _count(total, "riga", "righe")
                st.caption(f"{shown} · {_count(len(header), 'colonna', 'colonne')}. Il file completo è nello zip.")

    file_name = zip_file_name(result.package.journey.name)
    with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
        st.download_button(DOWNLOAD_LABEL, data=zip_bytes, file_name=file_name, mime="application/zip",
                           type="primary")
        st.markdown(_md(f"{file_name} contiene {len(result.files)} file:"))
    st.markdown("\n".join(f"- {_md(_one_line(line))}" for line in download_lines(result)))

    warnings, left_out = generation_warnings(result)
    if warnings:
        with st.expander(f"Warning della generazione ({len(warnings)})"):
            _show_list(warnings, empty="")
            if left_out:
                st.caption(_md(f"{outputs.REPORT_FILE} elenca le attività coinvolte."))
    else:
        st.caption("Nessun warning dalla generazione.")


def _phase(data: Optional[bytes]) -> str:
    if data is None:
        return PHASE_EMPTY
    try:
        digest, _ = _detection(data)
    except Exception:   # render() says why
        return PHASE_ERROR
    pending = st.session_state.get(PENDING_KEY)
    if pending is not None and pending[0] == digest:
        return PHASE_RUNNING
    stored = st.session_state.get(RESULT_KEY)
    if stored is not None and stored.digest == digest:
        return PHASE_DONE if stored.result is not None else PHASE_FAILED
    return PHASE_REVIEW


def _detection(data: bytes) -> tuple[str, DetectionSummary]:
    """Step 2 for these bytes, read once per file: a new file drops what the previous one gave."""
    digest = hashlib.sha256(data).hexdigest()
    cached = st.session_state.get(DETECTION_KEY)
    if cached is not None and cached[0] == digest:
        return cached
    _forget()
    summary = load_summary(data)
    st.session_state[DETECTION_KEY] = (digest, summary)
    return digest, summary


def _forget() -> None:
    """Nothing is kept once the file is gone or replaced."""
    st.session_state.pop(DETECTION_KEY, None)
    st.session_state.pop(RESULT_KEY, None)
    st.session_state.pop(PENDING_KEY, None)


@contextmanager
def _card(number: int, name: str, title: str, summary: str, expanded: bool, state: str) -> Iterator[None]:
    """One step as a card: an expander whose label is the step's title and a one-line summary (Markdown).

    ``state`` ("done", "now" or "todo") picks the icon and, through the container's key, its colour.
    """
    icon = ":material/check_circle:" if state == "done" else f":material/counter_{number}:"
    with st.container(key=f"card-{name}-{state}"):
        with st.expander(f"**{_md(title)}** {summary}", expanded=expanded, icon=icon):
            yield


def _detection_summary(summary: DetectionSummary) -> str:
    warnings = len(summary.warnings)
    return (f"{_md(summary.journey_name)} · {_count(summary.activity_count, 'attività', 'attività')} · "
            f"{_count(len(summary.csv_files), 'tabella', 'tabelle')} con CSV · "
            + (f":yellow-badge[{warnings} warning]" if warnings else "nessun warning"))


def _settings_summary(phase: str) -> str:
    stored = st.session_state.get(RESULT_KEY)
    if phase in (PHASE_DONE, PHASE_FAILED) and stored is not None:
        used = stored.settings
        # The address as inline code: Markdown would make it a mailto link.
        inbox = f"inbox `{used.real_inbox.replace('`', '')}`" if used.real_inbox else _md("nessuna inbox reale")
        rest = _md(f" · run {used.run_id} · {_count(used.contacts_per_case, 'contatto', 'contatti')} per caso")
        return inbox + rest
    if phase == PHASE_RUNNING:
        return _md("generazione in corso con le impostazioni scelte")
    return _md("ogni impostazione ha un valore di default")


def _generate_summary(phase: str, stored: Optional[StoredRun]) -> str:
    if phase == PHASE_RUNNING:
        return _md("in corso")
    if stored is not None and stored.result is not None:
        return _md(done_summary(stored.result))
    if stored is not None:
        return _md("non riuscita")
    return _md("da avviare")


def _show_detection(summary: DetectionSummary) -> None:
    """Spec, step 2: "journey name, number of activities by type, the tables with their role, and any warnings"."""
    left, right = st.columns([1, 1.4], gap="large")
    with left:
        _label("Attività per tipo")
        st.markdown(_markdown_table(["Tipo di attività", "Numero"], summary.activities, right=(1,)))
    with right:
        _label("Tabelle con CSV")
        with_csv = [line for line in summary.tables if line.csv]
        if with_csv:
            rows = [(line.name + (" (sincronizzata dal CRM)" if line.managed else ""), line.role,
                     _level_text(line.level)) for line in with_csv]
            st.markdown(_markdown_table(["Tabella", "Ruolo", "Livello"], rows))
        else:
            st.markdown("Nessuna tabella ha un CSV.")
        without = [f"{line.name} ({line.role})" for line in summary.tables if not line.csv]
        if without:
            st.caption(_md(f"Senza CSV: {_join_words(without)}."))
    if summary.exits:
        _label("Criteri di uscita")
        _show_list(summary.exits, empty="")
    if summary.warnings:
        with st.expander(f"Warning trovati nell'export ({len(summary.warnings)})"):
            _show_list(summary.warnings, empty="")
            if summary.ids_left_out:
                st.caption(_md(f"{outputs.REPORT_FILE} elenca le attività coinvolte, una volta generati i dati di "
                               "test."))
    else:
        st.caption("Nessun warning nell'export.")


def _settings_form(summary: DetectionSummary, running: bool) -> tuple[Settings, bool]:
    """Step 3 in a form whose submit button is Generate, so the settings apply together.

    The real inbox and the run identifier come first; the other settings wait in a closed expander.
    The Generate button is disabled while a generation runs (``running``).
    """
    defaults = default_form_values(summary.activation_field, summary.entry_kind)
    run_once = summary.entry_kind == ENTRY_RUN_ONCE
    values: dict[str, Any] = {}
    with st.form("settings", border=False):
        left, right = st.columns(2, gap="large")
        with left:
            values["real_inbox"] = _text("real_inbox", defaults, placeholder="nome@dominio")
            st.caption(_md(SETTING_NOTES["real_inbox"]))
        with right:
            values["run_id"] = _text("run_id", defaults)
            st.caption(_md(SETTING_NOTES["run_id"]))
        with st.expander(MORE_SETTINGS):
            _label("Contatti")
            left, right = st.columns(2, vertical_alignment="bottom")
            with left:
                values["contacts_per_case"] = _number("contacts_per_case", defaults, min_value=1)
            with right:
                values["contacts_per_random_case"] = _number("contacts_per_random_case", defaults, min_value=1)
            _label("Recapiti")
            left, right = st.columns(2, vertical_alignment="top")
            with left:
                values["real_phone"] = _text("real_phone", defaults)
                st.caption(_md(SETTING_NOTES["real_phone"]))
            with right:
                values["fake_phone"] = _text("fake_phone", defaults)
            _label("Date")
            left, right = st.columns(2, vertical_alignment="bottom")
            with left:
                values["window_start_days"] = _number("window_start_days", defaults)
                st.caption(_md(days_text(int(values["window_start_days"]))))
            with right:
                values["window_end_days"] = _number("window_end_days", defaults)
                st.caption(_md(days_text(int(values["window_end_days"]))))
            left, right = st.columns(2, vertical_alignment="bottom")
            with left:
                values["wait_offset_hours"] = _number("wait_offset_hours", defaults)
            with right:
                values["wait_offset_before_engagement_hours"] = _number("wait_offset_before_engagement_hours",
                                                                        defaults)
            left, right = st.columns(2, vertical_alignment="bottom")
            with left:
                values["min_validity_days"] = _number("min_validity_days", defaults, min_value=0)
            values["date_format"] = _text("date_format", defaults)
            st.caption(_md(date_format_note(datetime.now())))
            _label("Scenari")
            values["one_active_scenario"] = st.checkbox(SETTING_LABELS["one_active_scenario"],
                                                        value=defaults["one_active_scenario"],
                                                        disabled=run_once or not summary.activation_choices)
            st.caption(_md(_activation_note(summary)))
            values["activation_field"] = None
            if summary.activation_choices and not run_once:
                choices = summary.activation_choices
                suggested = summary.activation_field
                values["activation_field"] = st.selectbox(
                    SETTING_LABELS["activation_field"], choices, format_func=_field_text,
                    index=choices.index(suggested) if suggested in choices else None,
                    placeholder="Scegli un campo", help=SETTING_HELP["activation_field"])
            values["case_insensitive_text"] = st.checkbox(SETTING_LABELS["case_insensitive_text"],
                                                          value=defaults["case_insensitive_text"],
                                                          help=SETTING_HELP["case_insensitive_text"])
        submitted = st.form_submit_button(GENERATE_LABEL, type="primary", disabled=running)
        st.caption(GENERATE_NOTE)
    return settings_from_form(values), submitted


def _number(name: str, defaults: Mapping[str, Any], min_value: Optional[int] = None) -> int:
    return st.number_input(SETTING_LABELS[name], value=defaults[name], min_value=min_value, step=1,
                           help=SETTING_HELP.get(name))


def _text(name: str, defaults: Mapping[str, Any], placeholder: Optional[str] = None) -> str:
    return st.text_input(SETTING_LABELS[name], value=defaults[name], placeholder=placeholder,
                         help=SETTING_HELP.get(name))


def _activation_note(summary: DetectionSummary) -> str:
    if summary.entry_kind == ENTRY_RUN_ONCE:
        return ("L'entry source è una data extension Run Once: tutti gli scenari entrano insieme quando il journey "
                "viene attivato, quindi l'opzione non è disponibile.")
    if summary.activation_field is not None:
        return (f"Campo di attivazione rilevato nei filtri della entry query: "
                f"{_field_text(summary.activation_field)}. Disattiva l'opzione per rendere attivi tutti gli scenari "
                "nei CSV.")
    if summary.activation_choices:
        return "Nei filtri della entry query non è stato rilevato un campo di attivazione."
    return ("Non è stato rilevato un campo di attivazione e nessuna tabella a livello di scenario ha un campo "
            "Boolean.")


def _generate(data: bytes, digest: str, settings: Settings) -> None:
    """Spec, step 4: generate from the bytes in memory; the result is kept for this session only.

    The page then runs once more, so that the Generate button is enabled again under the result.
    """
    try:
        with st.spinner(SPINNER_TEXT):
            result = pipeline.generate(data, settings)
            zip_bytes = outputs.build_zip(result.files)
        stored = StoredRun(digest, settings, result=result, zip_bytes=zip_bytes)
    except Exception as error:   # one plain sentence on the page, the details in a closed expander
        LOG.exception("The generation failed")
        stored = StoredRun(digest, settings, error=UNEXPECTED_GENERATION, details=technical_details(error))
    st.session_state[RESULT_KEY] = stored
    st.rerun()


def _show_failure(message: str, details: str) -> None:
    st.error(_md(message))
    with st.expander(TECHNICAL_DETAILS):
        st.code(details, language=None)


# ---------------------------------------------------------------------------
# Small text helpers
# ---------------------------------------------------------------------------

def _md(text: str) -> str:
    """Text shown through Markdown exactly as written: Markdown characters are escaped."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _label(text: str) -> None:
    """A small section label inside a card: the four steps are the only headings."""
    st.markdown(f'<p class="cj-label">{_html(text)}</p>', unsafe_allow_html=True)


def _html(text: str) -> str:
    return html.escape(str(text))


def _header_html() -> str:
    """Logo (docs/brand.md: primary version, at least 90 px wide) and app name."""
    logo = base64.b64encode(LOGO_FILE.read_bytes()).decode("ascii")
    return (f'<div class="cj-header"><img src="data:image/svg+xml;base64,{logo}" alt="Lutech">'
            f'<span class="cj-sep"></span><span class="cj-name">{_html(TITLE)}</span></div>'
            f'<div class="cj-rule"></div>')


def _stepper_html(phase: str) -> str:
    """The four steps in a row: done, now (the step the tester is on) or todo."""
    if phase in (PHASE_EMPTY, PHASE_ERROR):
        states = ["now", "todo", "todo", "todo"]
    elif phase == PHASE_REVIEW:
        states = ["done", "done", "now", "todo"]
    else:
        states = ["done", "done", "done", "now"]
    last = {PHASE_RUNNING: "In corso", PHASE_DONE: "Completato", PHASE_FAILED: "Non riuscita"}.get(phase, "Da avviare")
    cells = []
    for number, ((name, note), state) in enumerate(zip(STEPPER, states), start=1):
        cells.append(f'<div class="{state}"><span class="cj-num">{number:02d}</span>'
                     f'<b>{_html(name)}</b>{_html(note or last)}</div>')
    return f'<div class="cj-stepper">{"".join(cells)}</div>'


def _strip_html(result: GenerationResult) -> str:
    cells = "".join(f'<div><b>{_html(value)}' + (f' <small>{_html(out_of)}</small>' if out_of else "")
                    + f'</b><span>{_html(label)}</span></div>' for value, out_of, label in summary_cells(result))
    return f'<div class="cj-strip">{cells}</div>'


def _frame(header: list[str], rows: list[list[str]]) -> Any:
    """A table of strings for st.dataframe, every value as the CSV writes it. Repeated column names get a
    suffix, because a table cannot show two columns with the same name."""
    import pandas as pd   # a Streamlit dependency

    names: list[str] = []
    for name in header:
        unique, n = name, 2
        while unique in names:
            unique, n = f"{name} ({n})", n + 1
        names.append(unique)
    width = len(names)
    cells = [(row + [""] * width)[:width] for row in rows]
    return pd.DataFrame(cells, columns=names, dtype="string")


def _size_text(size: int) -> str:
    if size < 1024:
        return f"{size} byte"
    if size < 1024 * 1024:
        return f"{round(size / 1024)} KB"
    return f"{size / (1024 * 1024):.1f} MB".replace(".", ",")


def _show_list(items: list[str], empty: str) -> None:
    if items:
        st.markdown("\n".join(f"- {_md(_one_line(item))}" for item in items))
    elif empty:
        st.markdown(empty)


def _markdown_table(header: list[str], rows: Iterable[Iterable[Any]], right: tuple[int, ...] = ()) -> str:
    def line(cells: Iterable[Any]) -> str:
        return "| " + " | ".join(_md(_one_line(str(cell))) for cell in cells) + " |"
    rule = "| " + " | ".join("---:" if i in right else "---" for i in range(len(header))) + " |"
    return "\n".join([line(header), rule] + [line(row) for row in rows])


def _without_key_lists(text: str, keys: set[str]) -> tuple[str, bool]:
    """Drop parenthesised lists of two or more activity keys, which report.md names in full."""
    removed = False

    def replace(match: re.Match) -> str:
        nonlocal removed
        items = [item.strip() for item in _LIST_SEPARATOR.split(match.group(1)) if item.strip()]
        if len(items) >= 2 and all(item in keys for item in items):
            removed = True
            return ""
        return match.group(0)

    return _PARENTHESES.sub(replace, text), removed


def _level_text(level: str) -> str:
    return LEVEL_NAMES.get(level, level)


def _field_text(ref: FieldRef) -> str:
    return f"{ref[0]}.{ref[1]}"


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _join_words(items: list[str], last: str = "e") -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {last} " + items[-1]


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


if __name__ == "__main__":
    main()
