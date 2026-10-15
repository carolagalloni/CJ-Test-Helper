"""Data generation: turn the scenarios and cases of the coverage into rows, one table at a time.

Spec, "Data generation rules": every generated value comes from one of five sources, applied in
this priority order:

    1. Key or join field               -> generated key, identical in every table that joins on it
    2. Field read by a split           -> the value the case requires, written exactly as in the condition
    3. Field filtered by the entry query -> a value that passes the filter, so the contact really enters
    4. Email, phone, date              -> the email, phone and date rules
    5. Any other field                 -> placeholder by type: text ``TST_<field>_<n>`` cut to the field
                                          length, Boolean set to its default or false

Spec, "Table selection": "The entry CSV is kept consistent with the source CSVs: its rows are what
the entry query would produce from them." So the entry table is not filled by the five rules
directly. Its rows are computed from the entry query's select expressions over the source rows of
each contact, and a requirement on an entry column (a key, a split value, a wait date) is pushed
back onto the source column that the select copies into it.

Phone numbers. The spec gives one contact per case the tester's real number "in each case whose
path sends a message to a phone". An SMS send (step 3, "SMS sends") goes to the entry column the journey
names as its default mobile number: in a case with ``Case.sms_sent`` and a real number in the settings, the
first contact gets that number in this column and in the source column the entry SELECT copies it from
(``Contact.uses_real_phone``). Every other phone field, and the other contacts, get ``Settings.fake_phone``.

Spec, step 3 ("Columns the entry query derives and columns it copies", "Synchronized data extensions"):

- The fields of an alias group (``TableModel.aliases``) hold one value: the one the coverage gives to the
  group's representative is written into every member, the entry copies included (they come from the SELECT).
- An entry column that a split reads and that the entry query computes with an expression the generator does
  not understand takes the value the case needs (rule 2), with a warning instead of a placeholder.
- A filter on the date part moved by a number of days gives the generation time moved by that number of days.
- A managed (synchronized) data extension keeps its CSV; a note says it is synchronized from the CRM, so its
  rows will also arrive from the sync.

Spec, "Extensions beyond the pilot":

- "A contact whose row is absent has no row in that table's CSV. This is the only exception to 'each
  contact row finds its rows in the linked tables'." The tables with the "row present / row absent"
  variable are ``CoverageResult.row_fields``; their linked field reads None in the case's values when the
  row is absent. The entry rows and the other tables are unchanged.
- "The deadline of a manual open or click is computed from the waits between the email and the
  engagement split, with a warning when the window is under one hour." coverage.py gives each manual action
  its waits (``Case.action_windows``); the deadline is the date written for the last wait by attribute
  among them plus the fixed waits after it, and ``Case.not_before`` is the same for the case's whole path.
- An entry that no automation feeds (Run Once, recurring schedule, CloudPages form, API event) has no entry
  query by design: its entry rows come from the general rules without a warning.

Nothing here reads the clock or draws random numbers: the generation time is passed in, so the same
inputs always give the same rows ("The output is reproducible").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .criteria import UnsupportedOperator
from .graph import CURRENT_DATE_DEFAULTS, case_paths, fixed_wait_minutes, wait_end
from .model import (
    ENTRY_AUTOMATION,
    ENTRY_RUN_ONCE,
    GENERATED_KEY,
    KIND_DECISION,
    KIND_WAIT_ATTRIBUTE,
    KIND_WAIT_DURATION,
    KIND_WAIT_STO,
    LEVEL_CONTACT,
    LEVEL_SCENARIO,
    ActionWindow,
    Case,
    ColumnRef,
    Contact,
    CoverageResult,
    DateBoundFilter,
    DateOffsetFilter,
    EqualityFilter,
    Field,
    FieldRef,
    GeneratedData,
    IsNullFilter,
    ManualAction,
    Package,
    Scenario,
    SelectColumn,
    SelectConcat,
    SelectCurrentDate,
    SelectLiteral,
    SelectUnsupported,
    Settings,
    Table,
    TableData,
    TableModel,
    UnsupportedFilter,
    Value,
    formula_conditions,
)

# Columns of scenarios.csv that come before the scenario-level fields (plan.md, "datagen.py").
SCENARIO_COLUMNS = [
    "scenario_key",
    "run_order",
    "has_manual_action",
    "is_statistical",
    "case_count",
    "contact_count",
    "first_contact_key",
    "last_contact_key",
    "active_at_start",
    "activation_field",
]

UNDELIVERABLE_DOMAIN = "example.invalid"   # spec: "<contact key>@example.invalid"
LOCALE_PLACEHOLDER = "en-US"

# Rule kinds with their place in the spec's priority order: (priority 1..5, order inside it).
# Inside priority 3 the one-active-scenario activation value refines the equality filter on the
# same field; inside priority 4 a wait date is more specific than the plain date rule.
_PRIORITY = {
    "key": (1, 0),
    "split": (2, 0),
    "activation": (3, 0),
    "filter": (3, 1),
    "wait": (4, 0),
    "email": (4, 1),
    "phone": (4, 2),
    "date": (4, 3),
    "placeholder": (5, 0),
}

_RULE_NAMES = {
    "key": "la chiave generata",
    "split": "il valore richiesto da uno split",
    "activation": "il valore di attivazione dell'impostazione \"Un solo scenario attivo alla volta\"",
    "filter": "il valore richiesto dal filtro della entry query",
    "wait": "la data del wait",
    "email": "l'indirizzo email",
    "phone": "il numero di telefono",
    "date": "la data di generazione",
    "placeholder": "il placeholder",
}

# Requirements an entry column passes back to the source column the select copies into it.
_WAIT_KINDS = (KIND_WAIT_ATTRIBUTE, KIND_WAIT_DURATION, KIND_WAIT_STO)

_PUSHED_BACK_KINDS = ("key", "split", "activation", "wait", "email", "phone", "date")

# Rules whose value is a requirement: when a higher rule wins over one of them, a warning says so.
_REQUIREMENT_KINDS = ("key", "split", "activation", "filter", "wait")

# Defaults that mean "the current date" in a Data Extension schema (the export writes GETDATE()).
_CURRENT_DATE_DEFAULTS = CURRENT_DATE_DEFAULTS   # shared with graph.Walker.inserted_default

_SKIP = object()   # a rule that does not apply to this row: try the next rule

# Spec, "Honest outcomes": "a warning when the window is under one hour".
SHORT_WINDOW_MINUTES = 60

# The manual action as a verb in the warnings ("open" and "click" are ManualAction.kind values).
_ACTION_VERBS = {"open": "aprire", "click": "cliccare"}


def generate_data(
    package: Package,
    model: TableModel,
    coverage: CoverageResult,
    settings: Settings,
    generation_time: datetime,
) -> GeneratedData:
    """Turn the coverage into rows per selected table, following the spec's data generation rules.

    Also fills ``Scenario.key`` and ``Case.contact_keys`` in place (scenarios in run order, cases in
    the order coverage lists them), and returns one ``Contact`` record per generated contact.
    """
    return _Generator(package, model, coverage, settings, generation_time).run()


def one_active_not_offered(package: Package) -> bool:
    """Spec, "Entry source and procedure": with a Run Once entry "All scenarios enter together, so 'one
    active scenario at a time' is not offered"."""
    return package.entry_event is not None and package.entry_event.kind == ENTRY_RUN_ONCE


def activation_setting(
    package: Package, model: TableModel, settings: Settings
) -> tuple[bool, Optional[FieldRef], list[str]]:
    """Resolve the "one active scenario at a time" setting: (on, activation field, warnings).

    Spec: "The app therefore has an optional 'one active scenario' setting with an activation field,
    suggested from the entry query filters (ACTIVE in the pilot). When on, only the first scenario is
    active in the CSV." Settings table: "One active scenario at a time: On when an activation field
    is detected"; "Activation field: Suggested from the entry query filters".

    The field comes from ``settings.activation_field``, else ``model.activation_field``. The mode
    needs a Boolean field of a table that gets a CSV; otherwise it is switched off with a warning.
    The returned field uses the schema spelling.

    Spec, "Entry source and procedure", Run Once row: "All scenarios enter together, so 'one active scenario
    at a time' is not offered": on a Run Once entry the setting is ignored, with a warning saying why.
    """
    warnings: list[str] = []
    ref = settings.activation_field or model.activation_field
    on = settings.one_active_scenario if settings.one_active_scenario is not None else ref is not None
    if not on:
        return False, None, warnings
    if one_active_not_offered(package):
        warnings.append(
            "L'impostazione \"Un solo scenario attivo alla volta\" non si applica a un ingresso Run Once: tutti gli "
            "scenari entrano insieme quando il journey viene attivato, quindi tutti gli scenari sono attivi nel CSV."
        )
        return False, None, warnings
    if ref is None:
        warnings.append(
            "L'impostazione \"Un solo scenario attivo alla volta\" è attiva, ma nessun campo di attivazione è "
            "stato indicato o rilevato. Tutti gli scenari sono attivi nel CSV."
        )
        return False, None, warnings
    table = _find_table(package.tables, ref[0])
    field = table.field(ref[1]) if table is not None else None
    if table is None or field is None:
        warnings.append(
            f"Il campo di attivazione {ref[0]}.{ref[1]} non esiste nell'export. L'impostazione \"Un solo "
            "scenario attivo alla volta\" viene disattivata, quindi tutti gli scenari sono attivi nel CSV."
        )
        return False, None, warnings
    if table.name.lower() not in {name.lower() for name in model.selected_tables}:
        warnings.append(
            f"Il campo di attivazione {table.name}.{field.name} è in una tabella che non ha un CSV. L'impostazione "
            "\"Un solo scenario attivo alla volta\" viene disattivata, quindi tutti gli scenari sono attivi nel CSV."
        )
        return False, None, warnings
    if _field_type(field) != "boolean":
        warnings.append(
            f"Il campo di attivazione {table.name}.{field.name} è di tipo {field.type}, non Boolean. L'impostazione "
            "\"Un solo scenario attivo alla volta\" viene disattivata, quindi tutti gli scenari sono attivi nel CSV."
        )
        return False, None, warnings
    return True, (table.name, field.name), warnings


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _norm(ref: FieldRef) -> tuple[str, str]:
    """Field lookups are case-insensitive (plan.md, "Rules for everyone")."""
    return (ref[0].lower(), ref[1].lower())


def _find_table(tables: dict[str, Table], name: str) -> Optional[Table]:
    if name in tables:
        return tables[name]
    low = name.lower()
    for table in tables.values():
        if table.name.lower() == low:
            return table
    return None


def _field_type(field: Field) -> str:
    return (field.type or "").strip().lower()


def _schema_fields(table: Table) -> list[Field]:
    """Fields in schema order (spec: "header row with the exact field names in schema order")."""
    return sorted(table.fields, key=lambda f: f.ordinal)


def boolean_text(value: Optional[str]) -> Optional[str]:
    """Spec file format: "True/False for Booleans". Returns None when the value is not a Boolean."""
    if value is None:
        return None
    low = str(value).strip().lower()
    if low in ("true", "1"):
        return "True"
    if low in ("false", "0"):
        return "False"
    return None


def _bool_text(flag: bool) -> str:
    return "True" if flag else "False"


def _lookup(values: dict[FieldRef, Value], ref: FieldRef) -> tuple[bool, Value]:
    """Case-insensitive lookup of a field in a values dict: (found, value)."""
    if ref in values:
        return True, values[ref]
    wanted = _norm(ref)
    for key, value in values.items():
        if _norm(key) == wanted:
            return True, value
    return False, None


def _as_text(value) -> Value:
    """Every value is a str in CSV form, or None for null (plan.md, "Rules for everyone")."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _count(number: int, singular: str, plural: str) -> str:
    """A number with its noun in the right form for the warnings: "1 caso", "2 casi"."""
    return f"{number} {singular}" if number == 1 else f"{number} {plural}"


def _has_unsupported(expr: object) -> bool:
    """True when a select expression is, or contains, an expression the generator does not understand."""
    if isinstance(expr, SelectConcat):
        return any(_has_unsupported(part) for part in expr.parts)
    return isinstance(expr, SelectUnsupported)


_DOTNET_TOKENS = {"yyyy": "%Y", "MM": "%m", "dd": "%d", "HH": "%H", "mm": "%M", "ss": "%S"}
_DOTNET_PATTERN = re.compile("yyyy|MM|dd|HH|mm|ss")


def _local_dotnet_to_strftime(dotnet_format: str) -> str:
    """Fallback converter for the .NET tokens yyyy MM dd HH mm ss; anything else is kept literally."""
    out: list[str] = []
    position = 0
    for match in _DOTNET_PATTERN.finditer(dotnet_format):
        out.append(dotnet_format[position:match.start()].replace("%", "%%"))
        out.append(_DOTNET_TOKENS[match.group()])
        position = match.end()
    out.append(dotnet_format[position:].replace("%", "%%"))
    return "".join(out)


def _dotnet_to_strftime(dotnet_format: str) -> str:
    """Convert a .NET date format (``FORMAT(GETUTCDATE(), 'yyyyMMdd_HHmm')``) to strftime.

    Uses ``sqlparse_lite.dotnet_to_strftime``; falls back to a tiny local converter while that
    module is not available.
    """
    try:
        from .sqlparse_lite import dotnet_to_strftime
    except ImportError:
        return _local_dotnet_to_strftime(dotnet_format)
    return dotnet_to_strftime(dotnet_format)


def _fixed_minutes(keys: list[str], activities: dict) -> Optional[int]:
    """Fixed duration of the waits among ``keys`` in minutes (graph.fixed_wait_minutes); None when unknown."""
    total = 0
    for key in keys:
        activity = activities.get(key)
        if activity is None or activity.kind not in _WAIT_KINDS:
            continue
        minutes = fixed_wait_minutes(activity)
        if minutes is None:
            return None
        total += minutes
    return total


def _minutes_text(minutes: int) -> str:
    """A duration in words: "5 minuti", "1 ora", "2 giorni"."""
    for size, one, many in ((24 * 60, "giorno", "giorni"), (60, "ora", "ore"), (1, "minuto", "minuti")):
        if minutes % size == 0:
            return _count(minutes // size, one, many)
    return _count(minutes, "minuto", "minuti")


def manual_wait_delay(wait_key: str, case: Case, offset_hours: int,
                      activities: Optional[dict] = None) -> Optional[timedelta]:
    """How long after the generation time a wait by attribute that leaves time for a manual action ends; None
    when it lies between no email and the engagement split that needs an action on it.

    Spec, "Dates", "Wait date before an engagement split", as decided by the team on 13 October 2026: the
    fixed waits before the email on the path delay its arrival, so they are added to the date, and the window
    still lasts the offset: generation time + the fixed waits before the email + the offset multiplied by
    the position of the manual action on the path (1 for the first engagement split, 2 for the second, ...), so
    that each deadline comes one offset after the previous one. A wait whose duration is unknown, and a wait
    by attribute, count 0 here; ``_fill_timing`` still warns when the window is too short."""
    if not case.manual_actions:
        return None
    best: Optional[timedelta] = None
    for keys in case_paths(case):
        if wait_key not in keys:
            continue
        actions = sorted((a for a in case.manual_actions if a.activity_key in keys),
                         key=lambda a: keys.index(a.activity_key))
        for position, action in enumerate(actions, start=1):
            end = keys.index(action.activity_key)
            start = keys.index(action.email_activity_key) if action.email_activity_key in keys[:end] else -1
            if wait_key not in keys[start + 1:end]:
                continue
            before = 0
            for key in keys[:max(start, 0)]:
                activity = (activities or {}).get(key)
                if activity is not None and activity.kind in _WAIT_KINDS:
                    before += fixed_wait_minutes(activity) or 0
            delay = timedelta(hours=offset_hours * position, minutes=before)
            if best is None or delay > best:
                best = delay
    return best


def wait_precedes_manual_action(wait_key: str, case: Case, activities: Optional[dict] = None) -> bool:
    """True when this wait is the one that leaves the tester time for a manual action.

    Spec, "Dates": "Wait date before an engagement split | Wait-by-attribute on the path to an
    engagement split | Generation time plus 24 hours, leaving time to open the email". Read as: on
    the case's path, the wait lies between the email to open or click and the engagement split
    that needs the action. A wait before the email is sent gets no extra time: it would only delay
    the send, and a later wait set to the same date would then end at once.
    """
    if not case.manual_actions:
        return False
    for keys in case_paths(case):
        if wait_key not in keys:
            continue
        for action in case.manual_actions:
            if action.activity_key not in keys:
                continue
            end = keys.index(action.activity_key)
            start = keys.index(action.email_activity_key) if action.email_activity_key in keys[:end] else -1
            between = keys[start + 1:end]
            if wait_key in between:
                return True
    return False


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Rule:
    """One source of value for a field. ``via`` names the entry column a pushed-back rule comes from."""
    kind: str
    detail: object = None
    via: Optional[str] = None

    @property
    def priority(self) -> tuple[int, int]:
        return _PRIORITY[self.kind]

    def describe(self) -> str:
        name = _RULE_NAMES[self.kind]
        return f"{name} della colonna di ingresso {self.via}" if self.via else name


@dataclass
class _Row:
    """Where a row stands: its number in its table, its scenario and, for contact rows, its contact."""
    number: int
    scenario: Scenario
    case: Optional[Case] = None
    contact_key: Optional[str] = None


class _Generator:
    def __init__(self, package: Package, model: TableModel, coverage: CoverageResult,
                 settings: Settings, generation_time: datetime):
        self.package = package
        self.model = model
        self.coverage = coverage
        self.settings = settings
        self.generation_time = generation_time
        self.warnings: list[str] = []
        self.placeholders: list[str] = []
        self._seen_messages: set[str] = set()
        self._placeholder_columns: set[tuple[str, str]] = set()
        self._rule_cache: dict[tuple[str, str], list[_Rule]] = {}
        self._row_index: dict[str, dict[str, dict[str, Value]]] = {}
        self._other_keys: dict[tuple[int, str], str] = {}
        self._contact_email: dict[str, str] = {}
        # The contact key is an email address when a field of the contact key group has that type.
        self._email_keys = any(
            _field_type(f) == "emailaddress"
            for group in model.key_groups if group.kind == "contact"
            for (table_name, field_name) in group.fields
            for t in [_find_table(package.tables, table_name)] if t is not None
            for f in [t.field(field_name)] if f is not None)
        self._active_scenario: Optional[Scenario] = None

        self._entry = _find_table(package.tables, model.entry_table)
        if self._entry is None:
            self._warn(f"La tabella di ingresso {model.entry_table} non è nell'export, quindi non riceve righe.")
        self._check_settings()
        self._tables_plan = self._plan_tables()
        self._levels = {table.name.lower(): level for table, level in self._tables_plan}
        self._key_group_index = self._index_key_groups()
        self._group_owner = self._key_group_owners()
        self._wait_fields = self._index_wait_fields()
        self._alias_groups = self._index_aliases()
        self._split_sources = self._index_split_fields()
        self._filters = self._index_filters()
        self._one_active, self._activation_ref = self._resolve_activation()
        self._entry_plan: dict[str, object] = {}
        self._feeds: dict[tuple[str, str], list[str]] = {}
        self._build_entry_plan()
        self._sms_targets: set[tuple[str, str]] = set()
        self._sms_ready = False
        self._real_phone_contacts: set[str] = set()
        self._plan_sms()
        self._warn_managed_tables()

    # -- bookkeeping ------------------------------------------------------------------------

    def _warn(self, message: str) -> None:
        """Every judgement call becomes a warning, listed once."""
        if message not in self._seen_messages:
            self._seen_messages.add(message)
            self.warnings.append(message)

    def _note_placeholder(self, table: Table, field: Field, reason: str) -> None:
        """GeneratedData.placeholders: entry columns that got a placeholder, with the reason."""
        key = (table.name, field.name)
        if key not in self._placeholder_columns:
            self._placeholder_columns.add(key)
            self.placeholders.append(f"{table.name}.{field.name}: {reason}. Viene scritto un placeholder.")

    def _table(self, name: str) -> Optional[Table]:
        return _find_table(self.package.tables, name)

    def _format_date(self, moment: datetime) -> str:
        """Dates in the CSVs follow ``Settings.date_format`` (spec settings: "Date format in the CSVs")."""
        return moment.strftime(self.settings.date_format)

    # -- setup ------------------------------------------------------------------------------

    def _check_settings(self) -> None:
        if "%" not in self.settings.date_format:
            self._warn(
                f"Il formato data {self.settings.date_format!r} non contiene direttive strftime, come %Y. "
                "Ogni data viene scritta così, come testo letterale."
            )

    def _plan_tables(self) -> list[tuple[Table, str]]:
        """The selected tables, in the order of the table model, with their level."""
        plan: list[tuple[Table, str]] = []
        for name in self.model.selected_tables:
            table = self._table(name)
            if table is None:
                self._warn(f"La tabella selezionata {name} non è nell'export, quindi non ha un CSV.")
                continue
            plan.append((table, self._table_level(table)))
        if self._entry is not None and all(table is not self._entry for table, _ in plan):
            self._warn(f"La tabella di ingresso {self._entry.name} non è tra le tabelle selezionate, quindi non ha un CSV.")
        return plan

    def _table_level(self, table: Table) -> str:
        """Spec, "Classify fields by level": one row per contact, or one row shared by many contacts."""
        if table is self._entry:
            return LEVEL_CONTACT
        role = next((r for r in self.model.tables.values() if r.name.lower() == table.name.lower()), None)
        if role is not None and role.level in (LEVEL_CONTACT, LEVEL_SCENARIO):
            return role.level
        if table.name.lower() in {name.lower() for name in self.model.scenario_tables}:
            return LEVEL_SCENARIO
        if table.name.lower() not in {name.lower() for name in self.model.contact_tables}:
            self._warn(f"Il livello della tabella {table.name} non è noto, quindi riceve una riga per contatto.")
        return LEVEL_CONTACT

    def _index_key_groups(self) -> dict[tuple[str, str], int]:
        index: dict[tuple[str, str], int] = {}
        for number, group in enumerate(self.model.key_groups):
            for ref in group.fields:
                index.setdefault(_norm(ref), number)
        return index

    def _key_group_owners(self) -> dict[int, str]:
        """An "other" key group holds one key per scenario when one of its tables is scenario-level,
        else one key per contact, so that the tables it links join row by row."""
        owners: dict[int, str] = {}
        for number, group in enumerate(self.model.key_groups):
            scenario_level = any(self._levels.get(ref[0].lower()) == LEVEL_SCENARIO for ref in group.fields)
            owners[number] = LEVEL_SCENARIO if scenario_level else LEVEL_CONTACT
        return owners

    def _index_wait_fields(self) -> dict[tuple[str, str], list[str]]:
        """Spec, "Dates": "Wait date | Field used by a wait-by-attribute"."""
        waits: dict[tuple[str, str], list[str]] = {}
        for activity in self.package.journey.activities.values():
            if activity.kind != KIND_WAIT_ATTRIBUTE:
                continue
            if activity.wait_field is None:
                self._warn(f"Il wait by attribute {activity.key} non indica un campo data, quindi per questo wait non "
                           "viene riempito nessun campo.")
                continue
            waits.setdefault(_norm(activity.wait_field), []).append(activity.key)
        return waits

    def _index_aliases(self) -> dict[tuple[str, str], list[FieldRef]]:
        """Spec, step 3: member of an alias group (``TableModel.aliases``) -> the whole group."""
        index: dict[tuple[str, str], list[FieldRef]] = {}
        for group in self.model.aliases:
            for ref in group:
                index.setdefault(_norm(ref), list(group))
        return index

    def _index_split_fields(self) -> dict[tuple[str, str], tuple[str, FieldRef]]:
        """Fields read by a split: the coverage's scenario-level and contact-level fields.

        A field that only a wait by attribute reads (no decision condition names it) follows the wait
        rule even when the coverage lists it, because the spec says that field "is filled according
        to the date settings".
        """
        condition_fields = {
            _norm((condition.table, condition.field))
            for activity in self.package.journey.activities.values()
            if activity.kind == KIND_DECISION
            for outcome in activity.outcomes
            for condition in formula_conditions(outcome.formula)
        }
        sources: dict[tuple[str, str], tuple[str, FieldRef]] = {}
        for level, refs in ((LEVEL_SCENARIO, self.coverage.scenario_fields),
                            (LEVEL_CONTACT, self.coverage.contact_fields)):
            for ref in refs:
                key = _norm(ref)
                # Spec, step 3: the coverage names an alias group by its representative; every member of the
                # group holds that value, and a read of any member counts for the group.
                members = [_norm(member) for member in self._alias_groups.get(key, [])] or [key]
                if any(m in self._wait_fields for m in members) and not any(m in condition_fields for m in members):
                    self._warn(
                        f"{ref[0]}.{ref[1]} è tra i campi della coverage, ma lo legge solo un wait by attribute. "
                        "Viene riempito come data del wait."
                    )
                    continue
                sources.setdefault(key, (level, ref))
                for member in members:
                    sources.setdefault(member, (level, ref))
        return sources

    def _index_filters(self) -> dict[tuple[str, str], list[object]]:
        """Spec priority 3: "Field filtered by the entry query: a value that passes the filter".

        The filters of the queries followed back from the entry query (tables.py, "Source table")
        are applied too, in order, because the automation's own queries must still find the rows
        they expect: on the pilot, the configuration update query reads Config rows
        ``WHERE RUN_TIME IS NULL`` and switches the scenario off after the run, so RUN_TIME stays
        empty. The entry query comes first and wins when two queries filter the same field."""
        filters: dict[tuple[str, str], list[object]] = {}
        filter_source: dict[tuple[str, str], str] = {}
        queries = list(self.model.source_queries) if self.model.source_queries else []
        if self.model.entry_query is not None and self.model.entry_query not in queries:
            queries.insert(0, self.model.entry_query)
        for query in queries:
            label = "La entry query" if query is self.model.entry_query else f"La query {query.name}"
            for flt in query.filters:
                if isinstance(flt, UnsupportedFilter):
                    self._warn(
                        f"{label} usa il filtro {flt.text!r}, che non è supportato. Le righe generate potrebbero "
                        "non superarlo, quindi i contatti potrebbero non entrare nel journey."
                    )
                    continue
                resolved = self._resolve_column(flt.column, query)
                if resolved is None:
                    self._warn(
                        f"{label} filtra su {flt.column.table or '?'}.{flt.column.column}, che non corrisponde a "
                        "nessun campo di una tabella sorgente. Il filtro non viene applicato alle righe generate."
                    )
                    continue
                table, field = resolved
                if table is self._entry:
                    self._warn(
                        f"{label} filtra sulla tabella di ingresso stessa, sulla colonna {table.name}.{field.name}. "
                        "I contatti generati sono nuovi, quindi il filtro non viene applicato alle righe generate."
                    )
                    continue
                if table.name.lower() not in self._levels:
                    self._warn(
                        f"{label} filtra su {table.name}.{field.name}, ma {table.name} non ha un CSV. "
                        "Il filtro non viene applicato alle righe generate."
                    )
                    continue
                key = _norm((table.name, field.name))
                same_field = filters.setdefault(key, [])
                if same_field and same_field[0] != flt:
                    if filter_source[key] == label:
                        self._warn(
                            f"{label} ha più filtri su {table.name}.{field.name}. Per sceglierne il valore si usa "
                            "solo il primo."
                        )
                    else:
                        self._warn(
                            f"{label} filtra anche {table.name}.{field.name}. Per sceglierne il valore si usa il "
                            f"filtro della {filter_source[key][3:]}."
                        )
                    continue
                filter_source.setdefault(key, label)
                same_field.append(flt)
                if query is not self.model.entry_query:
                    self._warn(
                        f"{label} filtra {table.name}.{field.name} e l'automation la esegue su {table.name}. "
                        "Anche le righe generate superano quel filtro. È un'assunzione: le query dell'automation "
                        "devono trovarle."
                    )
        return filters

    def _resolve_activation(self) -> tuple[bool, Optional[FieldRef]]:
        on, ref, warnings = activation_setting(self.package, self.model, self.settings)
        for message in warnings:
            self._warn(message)
        return on, ref

    def _resolve_column(self, column: ColumnRef, query=None) -> Optional[tuple[Table, Field]]:
        """A query column as (table, field) with the schema spelling; unqualified columns are looked
        up in the query's source tables (the entry query by default) and must match exactly one of them."""
        if column.table:
            table = self._table(column.table)
            field = table.field(column.column) if table is not None else None
            return (table, field) if field is not None else None
        query = query if query is not None else self.model.entry_query
        matches: list[tuple[Table, Field]] = []
        for source in (query.sources if query is not None else []):
            table = self._table(source.table)
            field = table.field(column.column) if table is not None else None
            if field is not None and all(t is not table for t, _ in matches):
                matches.append((table, field))
        return matches[0] if len(matches) == 1 else None

    def _build_entry_plan(self) -> None:
        """Map each entry column to the select expression that fills it (spec, "Table selection")."""
        query = self.model.entry_query
        entry = self._entry
        if query is None or entry is None:
            return
        if query.select_all:
            for field in _schema_fields(entry):
                for source in query.sources:
                    table = self._table(source.table)
                    source_field = table.field(field.name) if table is not None and table is not entry else None
                    if source_field is not None:
                        self._entry_plan[field.name] = SelectColumn(ColumnRef(table.name, source_field.name))
                        break
        if query.select:
            ignored: list[str] = []
            explicit: set[str] = set()
            for item in query.select:
                field = entry.field(item.target_column)
                if field is None:
                    ignored.append(item.target_column)
                    continue
                if field.name in explicit:
                    self._warn(
                        f"La entry query riempie {entry.name}.{field.name} due volte. Si usa la prima espressione."
                    )
                    continue
                explicit.add(field.name)
                self._entry_plan[field.name] = item.expr   # an explicit item wins over a column copied by *
            if ignored:
                self._warn(
                    f"La entry query seleziona colonne che {entry.name} non ha. Queste colonne vengono ignorate: "
                    + ", ".join(ignored) + "."
                )
        for column, expr in self._entry_plan.items():
            if not isinstance(expr, SelectColumn):
                continue
            resolved = self._resolve_column(expr.source)
            if resolved is not None and resolved[0] is not entry:
                source_table, source_field = resolved
                self._feeds.setdefault(_norm((source_table.name, source_field.name)), []).append(column)

    # -- rules ------------------------------------------------------------------------------

    def _own_rules(self, table: Table, field: Field) -> list[_Rule]:
        """The rules that apply to a field by itself, in the spec's priority order."""
        ref = _norm((table.name, field.name))
        rules: list[_Rule] = []
        if ref in self._key_group_index:
            rules.append(_Rule("key", self._key_group_index[ref]))
        if ref in self._split_sources:
            rules.append(_Rule("split", self._split_sources[ref]))
        if self._activation_ref is not None and ref == _norm(self._activation_ref):
            rules.append(_Rule("activation"))
        if ref in self._filters:
            rules.append(_Rule("filter", self._filters[ref][0]))
        if ref in self._wait_fields:
            rules.append(_Rule("wait", tuple(self._wait_fields[ref])))
        field_type = _field_type(field)
        if field_type == "emailaddress" or self._is_default_email_field(table, field):
            rules.append(_Rule("email"))
        elif field_type == "phone" or ref in self._sms_targets:
            rules.append(_Rule("phone"))
        elif field_type == "date":
            rules.append(_Rule("date"))
        rules.append(_Rule("placeholder"))
        return sorted(rules, key=lambda rule: rule.priority)

    def _is_default_email_field(self, table: Table, field: Field) -> bool:
        """The entry table field the journey sends emails to (``defaults.email`` in the export)."""
        name = self.package.journey.default_email_field
        return (name is not None and self._entry is not None and table is self._entry
                and field.name.lower() == name.lower())

    def _rules(self, table: Table, field: Field) -> list[_Rule]:
        """Own rules plus the requirements of the entry columns this field is selected into."""
        cache_key = (table.name, field.name)
        if cache_key in self._rule_cache:
            return self._rule_cache[cache_key]
        rules = self._own_rules(table, field)
        if table is not self._entry and self._entry is not None:
            for column in self._feeds.get(_norm(cache_key), []):
                entry_field = self._entry.field(column)
                for rule in self._own_rules(self._entry, entry_field):
                    if rule.kind in _PUSHED_BACK_KINDS:
                        rules.append(_Rule(rule.kind, rule.detail, via=column))
        rules = sorted(rules, key=lambda rule: rule.priority)   # stable: own rules first on ties
        self._rule_cache[cache_key] = rules
        return rules

    def _field_value(self, table: Table, field: Field, row: _Row) -> Value:
        """The value of one field: the first rule, in priority order, that applies to this row."""
        rules = self._rules(table, field)
        for position, rule in enumerate(rules):
            value = self._rule_value(rule, table, field, row)
            if value is _SKIP:
                continue
            value = self._normalise(_as_text(value), table, field)
            if rule.kind == "key" and field.length and value and len(value) > field.length:
                self._warn(
                    f"Le chiavi generate sono lunghe {_count(len(value), 'carattere', 'caratteri')}, ma "
                    f"{table.name}.{field.name} ne contiene solo {field.length}. Le chiavi non vengono tagliate, così "
                    "le tabelle restano collegate dal join."
                )
            self._report_overridden(table, field, row, rule, value, rules[position + 1:])
            return value
        raise AssertionError("the placeholder rule always applies")

    def _report_overridden(self, table: Table, field: Field, row: _Row, chosen: _Rule,
                           value: Value, lower_rules: list[_Rule]) -> None:
        """Spec priority order: a higher rule wins; say so when a lower requirement is lost."""
        for rule in lower_rules:
            if rule.kind not in _REQUIREMENT_KINDS:
                continue
            if chosen.kind == "activation" and rule.kind == "filter":
                continue   # the activation value is the filter value of the active scenario
            if isinstance(rule.detail, IsNullFilter) and rule.detail.negated:
                if value is None or value == "":
                    self._warn(
                        f"{table.name}.{field.name} riceve {chosen.describe()}, che su alcune righe è vuoto. "
                        "Secondo l'ordine di priorità della spec, il filtro IS NOT NULL della entry query su questo "
                        "campo non è rispettato."
                    )
                continue
            other = self._rule_value(rule, table, field, row)
            if other is _SKIP:
                continue
            other = self._normalise(_as_text(other), table, field)
            if other != value:
                self._warn(
                    f"{table.name}.{field.name} riceve {chosen.describe()} e non {rule.describe()}, "
                    f"secondo l'ordine di priorità della spec. Con {rule.describe()} il valore sarebbe stato {other!r}."
                )

    def _rule_value(self, rule: _Rule, table: Table, field: Field, row: _Row) -> object:
        """The value a rule gives on this row, or _SKIP when the rule does not apply to it."""
        kind = rule.kind
        if kind == "key":
            return self._key_value(rule.detail, table, field, row)
        if kind == "split":
            return self._split_value(rule.detail, row)
        if kind == "activation":
            return "True" if row.scenario is self._active_scenario else "False"
        if kind == "filter":
            return self._filter_value(rule.detail)
        if kind == "wait":
            return self._wait_value(rule.detail, row)
        if kind == "email":
            return self._email_value(table, field, row)
        if kind == "phone":
            if row.contact_key in self._real_phone_contacts and _norm((table.name, field.name)) in self._sms_targets:
                return self.settings.real_phone.strip()
            return self.settings.fake_phone
        if kind == "date":
            return self._format_date(self.generation_time)
        return self._placeholder(table, field, row.number)

    def _fallback_value(self, table: Table, field: Field, row: _Row) -> Value:
        """Placeholder by type for a field whose own value cannot be computed (rules 4 and 5)."""
        for rule in self._own_rules(table, field):
            if rule.kind in ("wait", "email", "phone", "date", "placeholder"):
                value = self._rule_value(rule, table, field, row)
                if value is not _SKIP:
                    return self._normalise(_as_text(value), table, field)
        return self._placeholder(table, field, row.number)

    # -- rule 1: keys -----------------------------------------------------------------------

    def _key_value(self, group_number: int, table: Table, field: Field, row: _Row) -> object:
        """Spec: "Key or join field: generated key, identical in every table that joins on it".

        Contact keys go into the contact key group, scenario keys into the field that joins contacts
        to the scenario-level table, and any other join group gets ``TST_<run>_K<n>``.
        """
        group = self.model.key_groups[group_number]
        if group.kind == "contact":
            if row.contact_key is None:
                self._warn(
                    f"{table.name}.{field.name} dovrebbe contenere la chiave del contatto, ma {table.name} ha una riga "
                    "per scenario. Lo riempiono le regole a priorità più bassa."
                )
                return _SKIP
            return row.contact_key
        if group.kind == "scenario":
            return row.scenario.key
        if self._group_owner[group_number] == LEVEL_SCENARIO or row.contact_key is None:
            return self._other_key(group_number, row.scenario.key)
        return self._other_key(group_number, row.contact_key)

    def _other_key(self, group_number: int, owner: str) -> str:
        """``TST_<run>_K<n>``: one key per scenario or per contact, shared by every field of the group."""
        slot = (group_number, owner)
        if slot not in self._other_keys:
            self._other_keys[slot] = f"TST_{self.settings.run_id}_K{len(self._other_keys) + 1:04d}"
        return self._other_keys[slot]

    # -- rule 2: split values ---------------------------------------------------------------

    def _split_value(self, source: tuple[str, FieldRef], row: _Row) -> object:
        """Spec: "Field read by a split: the value the case requires, written exactly as in the
        condition (same letter case)". Scenario-level fields come from ``Scenario.values``,
        contact-level ones from ``Case.contact_values``; None means an empty cell."""
        level, ref = source
        if level == LEVEL_SCENARIO:
            found, value = _lookup(row.scenario.values, ref)
            if not found:
                self._warn(f"La coverage non dà un valore per {ref[0]}.{ref[1]} nello scenario {row.scenario.id}, "
                           "quindi lo riempiono le regole a priorità più bassa.")
                return _SKIP
            return self._other_or_value(ref, value)
        if row.case is not None:
            found, value = _lookup(row.case.contact_values, ref)
            if not found:
                self._warn(f"La coverage non dà un valore per {ref[0]}.{ref[1]} nel caso {row.case.id}, "
                           "quindi lo riempiono le regole a priorità più bassa.")
                return _SKIP
            return self._other_or_value(ref, value)
        # A row shared by the whole scenario holds a contact-level field: its cases must agree.
        values = [value for case in row.scenario.cases for found, value in [_lookup(case.contact_values, ref)] if found]
        if not values:
            return _SKIP
        if len(set(values)) > 1:
            self._warn(
                f"{ref[0]}.{ref[1]} è letto a livello di contatto, ma la sua riga è condivisa dallo scenario "
                f"{row.scenario.id}, i cui casi richiedono valori diversi. Viene scritto il valore del primo caso: {values[0]!r}."
            )
        return values[0]

    def _other_or_value(self, ref: FieldRef, value: Value) -> object:
        """The coverage's 'other' value of an email or phone field only means "not one of the
        condition values": the address or number rules (spec, "Email" and "Phone") give it. The
        coverage's generated-key marker means "the generated key": the key rule gives it."""
        if value == GENERATED_KEY:
            return _SKIP
        other = self.coverage.other_values.get(ref)
        if other is None or value != other:
            return value
        table = self._table(ref[0])
        field = table.field(ref[1]) if table is not None else None
        if field is not None and _field_type(field) in ("emailaddress", "phone"):
            return _SKIP
        return value

    # -- rule 3: entry query filters --------------------------------------------------------

    def _filter_value(self, flt: object) -> object:
        """Spec priority 3 and "Dates": an equality filter gets its literal; a date-range filter gets
        the validity window ("Starts yesterday, ends in 7 days" by default); IS NULL gets an empty cell."""
        if isinstance(flt, EqualityFilter):
            return flt.value
        if isinstance(flt, DateBoundFilter):
            days = self.settings.window_start_days if flt.bound == "start" else self.settings.window_end_days
            return self._format_date(self.generation_time + timedelta(days=days))
        if isinstance(flt, DateOffsetFilter):
            # Spec, step 3: the date part moves by ``days``; the time of day stays the generation time's.
            return self._format_date(self.generation_time + timedelta(days=flt.days))
        if isinstance(flt, IsNullFilter):
            return _SKIP if flt.negated else None   # IS NOT NULL: any value of the lower rules passes
        return _SKIP

    # -- rule 4: wait dates, email, phone ---------------------------------------------------

    def _wait_value(self, wait_keys: tuple[str, ...], row: _Row) -> str:
        """Spec, "Dates": a wait date is the generation time plus the wait offset (default 0 hours, "so
        the wait ends at once"), or plus the offset before an engagement split (default 24 hours)
        when a case crosses the wait and later needs a manual action; in that case the fixed waits before
        the email are added too (``manual_wait_delay``, team decision of 13 October 2026). A scenario-level
        field is decided over all the cases of its scenario, a contact-level field for the contact's own case."""
        cases = [row.case] if row.case is not None else list(row.scenario.cases)
        activities = self.package.journey.activities
        # Two engagement splits on one path need two deadlines: the second wait ends one offset after the
        # first, so that the second email is sent before its own deadline starts (``manual_wait_delay``).
        delays = [manual_wait_delay(key, case, self.settings.wait_offset_before_engagement_hours, activities)
                  for case in cases for key in wait_keys]
        delays = [d for d in delays if d is not None]
        delay = max(delays) if delays else timedelta(hours=self.settings.wait_offset_hours)
        return self._format_date(self.generation_time + delay)

    def _email_value(self, table: Table, field: Field, row: _Row) -> str:
        """Spec, "Email": the contact's address (see ``_build_contacts``)."""
        if row.contact_key is not None:
            return self._contact_email[row.contact_key]
        self._warn(
            f"{table.name}.{field.name} è un campo email in una tabella con una riga per scenario, quindi riceve "
            f"l'indirizzo non recapitabile <chiave dello scenario>@{UNDELIVERABLE_DOMAIN}."
        )
        return f"{row.scenario.key}@{UNDELIVERABLE_DOMAIN}"

    # -- rule 5: placeholders ---------------------------------------------------------------

    def _placeholder(self, table: Table, field: Field, number: int) -> str:
        """Spec: "Placeholder by type: text TST_<field>_<n> cut to the field length, Boolean set to its
        default or false"; plan.md adds Date -> generation time, Number/Decimal -> n, Locale -> en-US.
        ``n`` is the row number in the table, starting at 1."""
        field_type = _field_type(field)
        if field_type == "boolean":
            return boolean_text(field.default) or "False"
        if field_type in ("number", "decimal"):
            return str(number)
        if field_type == "locale":
            return LOCALE_PLACEHOLDER
        if field_type == "date":
            return self._format_date(self.generation_time)
        if field_type not in ("text", "emailaddress", "phone"):
            self._warn(f"{table.name}.{field.name} ha il tipo sconosciuto {field.type!r}, quindi riceve un placeholder di testo.")
        if field_type == "text" and field.name.lower().startswith("email"):
            self._warn(
                f"{table.name}.{field.name} è un campo Text con il nome di un indirizzo email. Solo i campi EmailAddress "
                "ricevono indirizzi, quindi riceve un placeholder di testo."
            )
        if field.is_primary_key:
            # Spec, "Keys": primary keys are unique within each table and carry the run identifier.
            # Shorter forms are used when the field is too short for the full one.
            run = self.settings.run_id
            forms = [(f"TST_{run}_P{number:04d}", f"TST_{run}_P0001"), (f"{run}_P{number:04d}", f"{run}_P0001"),
                     (f"P{number:04d}", "P0001"), (str(number), "1")]
            for text, example in forms:
                if not field.length or len(text) <= field.length:
                    if not text.startswith("TST_"):
                        self._warn(f"{table.name}.{field.name} è una chiave primaria di "
                                   f"{_count(field.length, 'carattere', 'caratteri')}, troppo corta per le chiavi "
                                   f"TST_{run}_P. Vengono scritti valori univoci più corti, come {example!r}.")
                    return text
            self._warn(f"{table.name}.{field.name} è una chiave primaria di {_count(field.length, 'carattere', 'caratteri')}, "
                       "troppo corta per chiavi generate univoche. I suoi valori vengono tagliati e possono ripetersi.")
            return str(number)[:field.length]
        text = f"TST_{field.name}_{number}"
        return text[:field.length] if field.length else text

    def _normalise(self, value: Value, table: Table, field: Field) -> Value:
        """Spec file format: "True/False for Booleans"."""
        if value is None or _field_type(field) != "boolean":
            return value
        boolean = boolean_text(value)
        if boolean is None:
            self._warn(f"{table.name}.{field.name} è Boolean ma riceve il valore {value!r}, che viene scritto così com'è.")
            return value
        return boolean

    # -- keys and contacts ------------------------------------------------------------------

    def _assign_keys(self, scenarios: list[Scenario]) -> None:
        """Spec, "Keys": scenario keys TST_<run>_S01..., contact keys TST_<run>_C0001... The contact number
        counts up over the whole run, scenarios in run order and cases in the order given."""
        number = 0
        inbox = self._real_inbox()
        for scenario in scenarios:
            scenario.key = self.settings.scenario_key(scenario.run_order)
            for case in scenario.cases:
                if case.contact_count <= 0:
                    self._warn(f"Il caso {case.id} chiede {case.contact_count} contatti, quindi non ne riceve nessuno.")
                keys = []
                real = bool(case.manual_actions) and inbox is not None and not case.inbox_blocked_reason
                for _ in range(max(case.contact_count, 0)):
                    number += 1
                    key = self.settings.contact_key(number)
                    if self._email_keys:
                        # The contact key is an email address field: the address forms of the spec already
                        # contain the key and are unique, so they serve as the key itself.
                        key = f"{inbox[0]}+{key}@{inbox[1]}" if real else f"{key}@{UNDELIVERABLE_DOMAIN}"
                    keys.append(key)
                case.contact_keys = keys
        scenario_keys = [scenario.key for scenario in scenarios]
        if len(set(scenario_keys)) != len(scenario_keys):
            self._warn("Due scenari hanno lo stesso ordine di esecuzione, quindi condividono la chiave dello scenario.")
        self._allocate_other_keys(scenarios)

    def _allocate_other_keys(self, scenarios: list[Scenario]) -> None:
        """Number the "other" group keys in a predictable order: group by group, then by owner."""
        for number, group in enumerate(self.model.key_groups):
            if group.kind in ("contact", "scenario"):
                continue
            if not any(ref[0].lower() in self._levels for ref in group.fields):
                continue
            for scenario in scenarios:
                if self._group_owner[number] == LEVEL_SCENARIO:
                    self._other_key(number, scenario.key)
                    continue
                for case in scenario.cases:
                    for key in case.contact_keys:
                        self._other_key(number, key)

    def _real_inbox(self) -> Optional[tuple[str, str]]:
        """The tester's real inbox as (local part, domain), or None when it is empty or unusable."""
        inbox = (self.settings.real_inbox or "").strip()
        if not inbox:
            return None
        local, at, domain = inbox.rpartition("@")
        if not at or not local or not domain or " " in inbox:
            self._warn(
                f"L'inbox reale {inbox!r} non è un indirizzo nella forma nome@dominio. Non viene usata, quindi "
                "ogni contatto riceve un indirizzo non recapitabile."
            )
            return None
        return local, domain

    def _plan_sms(self) -> None:
        """Spec, "Phone" and step 3, "SMS sends": in each case whose path sends an SMS, one contact gets the
        tester's real number in the entry column the journey sends to (``defaults.mobileNumber``) and in the
        source column the entry SELECT copies it from. ``_sms_targets`` holds those columns; ``_sms_ready``
        says the real number can be written. One warning says why not when a case sends an SMS and it cannot."""
        if not any(case.sms_sent for case in self.coverage.cases):
            return
        entry = self._entry
        name = self.package.journey.default_mobile_field
        column = entry.field(name) if entry is not None and name else None
        reasons: list[str] = []
        if not (self.settings.real_phone or "").strip():
            reasons.append("nessun numero reale impostato, quindi tutti i contatti ricevono il numero finto "
                           f"{self.settings.fake_phone}")
        if not name:
            reasons.append("il journey non indica la colonna del numero di telefono a cui invia gli SMS "
                           "(defaults.mobileNumber)")
        elif column is None:
            reasons.append(f"la colonna {name} del numero di invio non è in "
                           f"{entry.name if entry is not None else self.model.entry_table}")
        targets: list[FieldRef] = []
        if column is not None:
            targets.append((entry.name, column.name))
            if self.model.entry_query is not None:
                expr = self._entry_plan.get(column.name)
                resolved = self._resolve_column(expr.source) if isinstance(expr, SelectColumn) else None
                if resolved is None or resolved[0] is entry:
                    reasons.append(f"la entry query non copia {entry.name}.{column.name} da una colonna di una "
                                   "tabella sorgente, quindi il numero reale non si può scrivere")
                    targets = []
                else:
                    targets.append((resolved[0].name, resolved[1].name))
        if reasons:
            self._warn("Gli SMS del journey non si possono verificare: " + "; ".join(reasons) + ". "
                       "L'SMS parte comunque sul percorso, ma nessun contatto lo riceve su un numero reale.")
            return
        for ref in targets:
            for member in self._alias_groups.get(_norm(ref), [ref]):
                self._sms_targets.add(_norm(member))
            self._sms_targets.add(_norm(ref))
        self._sms_ready = True

    def _warn_managed_tables(self) -> None:
        """Spec, step 3, "Synchronized data extensions": the CSV of a managed table is produced and imported like
        the others; the note tells the tester that the rows will also arrive from the sync."""
        for table, _ in self._tables_plan:
            if table.managed:
                self._warn(
                    f"{table.name} è una data extension sincronizzata dal CRM (managed): il CSV si importa come gli "
                    "altri e le sue righe arriveranno anche dalla sincronizzazione."
                )

    def _build_contacts(self, scenarios: list[Scenario]) -> list[Contact]:
        """Spec, "Email": "Contacts that must open or click get the tester's real inbox with
        plus-addressing, for example name+TST_C0042@domain. All other contacts get an undeliverable
        address, <contact key>@example.invalid." A contact must open or click when its case has a
        manual action; with no real inbox, it gets the undeliverable address too."""
        inbox = self._real_inbox()
        contacts: list[Contact] = []
        manual_without_inbox = 0
        for scenario in scenarios:
            for case in scenario.cases:
                real = bool(case.manual_actions) and inbox is not None and not case.inbox_blocked_reason
                if self._sms_ready and case.sms_sent and case.contact_keys:
                    self._real_phone_contacts.add(case.contact_keys[0])   # the first contact receives the SMS
                if case.manual_actions and inbox is None:
                    manual_without_inbox += 1
                if case.manual_actions and case.inbox_blocked_reason:
                    self._warn(f"Il caso {case.id} richiede un open o un click che non si può fare: "
                               f"{case.inbox_blocked_reason}. I suoi contatti mantengono l'indirizzo richiesto dal percorso.")
                for key in case.contact_keys:
                    if self._email_keys:
                        email = key
                    else:
                        email = f"{inbox[0]}+{key}@{inbox[1]}" if real else f"{key}@{UNDELIVERABLE_DOMAIN}"
                    self._contact_email[key] = email
                    contacts.append(Contact(key=key, case_id=case.id, scenario_id=scenario.id,
                                            uses_real_inbox=real, uses_real_phone=key in self._real_phone_contacts,
                                            email=email))
        if manual_without_inbox:
            self._warn(
                f"Nessuna inbox reale impostata. I contatti di {_count(manual_without_inbox, 'caso', 'casi')} "
                f"con un open o un click manuale ricevono indirizzi non recapitabili che terminano con @{UNDELIVERABLE_DOMAIN}."
            )
        return contacts

    def _check_engagement_waits(self, scenarios: list[Scenario]) -> None:
        """Spec, "Dates": the wait before an engagement split leaves the tester time to open the email.
        Warn when no wait by attribute lies between the email and the split on a case's path."""
        activities = self.package.journey.activities
        for scenario in scenarios:
            for case in scenario.cases:
                for action in case.manual_actions:
                    if not self._has_wait_before(case, action, activities):
                        self._warn(
                            f"Il caso {case.id} non ha un wait tra l'email {action.email_name!r} "
                            f"e l'engagement split {action.activity_key}. Il tester potrebbe non avere il tempo di "
                            f"{_ACTION_VERBS.get(action.kind, 'aprire o cliccare')} l'email prima che lo split venga valutato."
                        )

    def _fill_timing(self, scenarios: list[Scenario]) -> None:
        """Spec, "Honest outcomes": "The deadline of a manual open or click is computed from the waits between
        the email and the engagement split, with a warning when the window is under one hour. The testbook gives
        each case's minimum duration."

        The window of a manual action opens when the email can arrive: the generation time, delayed by the
        waits before the email on the case's path (``ActionWindow.wait_keys_before``; ``email_not_before`` when
        they delay it). With a wait by attribute between the email and the split, the deadline is the earliest
        time the contacts reach the split (``_leave_waits``): each wait by attribute holds them until the date
        written for it, or lets them go at once when that date has already passed. Without a wait by attribute
        the window is relative to the email (``minutes``). A window is short when it is under one hour: its
        deadline is less than an hour after the email can arrive, or its fixed waits last less than an hour. A
        short window with no wait at all is already reported by ``_check_engagement_waits``.
        ``Case.not_before`` is the same date for the whole path of the case."""
        activities = self.package.journey.activities
        short = timedelta(minutes=SHORT_WINDOW_MINUTES)
        for scenario in scenarios:
            for case in scenario.cases:
                for window in case.action_windows:
                    email_at = self._leave_waits(window.wait_keys_before, self.generation_time, scenario, case)
                    if email_at is not None and email_at > self.generation_time:
                        window.email_not_before = self._format_date(email_at)
                    start = email_at if email_at is not None else self.generation_time
                    if window.attribute_wait_key is not None:
                        moment = self._leave_waits(window.wait_keys, start, scenario, case)
                        window.deadline = self._format_date(moment) if moment is not None else None
                        window.short = moment is not None and moment - start < short
                    else:
                        window.short = window.minutes is not None and window.minutes < SHORT_WINDOW_MINUTES
                    if window.short and window.wait_keys:
                        self._warn_short_window(window, case, activities)
                latest: Optional[datetime] = None
                for keys in case_paths(case):
                    waits = [key for key in keys if key in activities and activities[key].kind in _WAIT_KINDS]
                    attribute = [key for key in waits if activities[key].kind == KIND_WAIT_ATTRIBUTE]
                    if not attribute:
                        continue
                    after = _fixed_minutes(waits[waits.index(attribute[-1]) + 1:], activities)
                    moment = self._wait_moment(attribute[-1], after, scenario, case)
                    if moment is not None and (latest is None or moment > latest):
                        latest = moment
                case.not_before = self._format_date(latest) if latest is not None else None

    def _leave_waits(self, wait_keys: list[str], start: datetime, scenario: Scenario,
                     case: Case) -> Optional[datetime]:
        """The earliest time a contact that reaches the first of ``wait_keys`` at ``start`` is past all of them,
        in path order: a wait by duration adds its amount, a send-time wait nothing (it may send at once), a
        wait by attribute holds the contact until the date written for it, or not at all once that date has
        passed. None when a duration or a date is not known."""
        activities = self.package.journey.activities
        moment = start
        for key in wait_keys:
            activity = activities.get(key)
            if activity is None:
                continue
            if activity.kind == KIND_WAIT_ATTRIBUTE:
                until = self._wait_moment(key, 0, scenario, case)
                if until is None:
                    return None
                moment = max(moment, until)
            else:
                minutes = fixed_wait_minutes(activity)
                if minutes is None:
                    return None
                moment += timedelta(minutes=minutes)
        return moment

    def _wait_moment(self, wait_key: str, minutes_after: Optional[int], scenario: Scenario,
                     case: Case) -> Optional[datetime]:
        """The moment the wait by attribute ends in this case's rows (the date written for it moved by the offset
        and the time of day of the wait, ``graph.wait_end``), plus ``minutes_after``; None when the date or the
        minutes are not known."""
        activity = self.package.journey.activities.get(wait_key)
        if activity is None or activity.wait_field is None or minutes_after is None:
            return None
        table = self._table(activity.wait_field[0])
        field = table.field(activity.wait_field[1]) if table is not None else None
        if field is None:
            return None
        level = self._levels.get(table.name.lower())
        owner = scenario.key if level == LEVEL_SCENARIO else (case.contact_keys[0] if case.contact_keys else None)
        row = self._row_index.get(table.name.lower(), {}).get(owner) if owner is not None else None
        value = row.get(field.name) if row is not None else None
        if value is None:
            return None
        try:
            moment = datetime.strptime(value, self.settings.date_format)
            moment = wait_end(activity, moment)   # spec, step 3: the offset and the time of day of the wait
        except (ValueError, UnsupportedOperator):
            return None
        return moment + timedelta(minutes=minutes_after)

    def _warn_short_window(self, window: ActionWindow, case: Case, activities: dict) -> None:
        """One warning per email and engagement split whose window is under one hour."""
        email = activities.get(window.email_activity_key)
        split = activities.get(window.activity_key)
        email_name = email.email_name if email is not None and email.email_name else window.email_activity_key
        split_name = f"'{split.name}' ({window.activity_key})" if split is not None and split.name else window.activity_key
        action = next((a for a in case.manual_actions if a.activity_key == window.activity_key), None)
        verb = _ACTION_VERBS.get(action.kind if action is not None else "", "aprire o cliccare")
        if len(window.wait_keys) == 1:
            waits, last = f"il wait {window.wait_keys[0]}", ("può terminare", "dura")
        else:
            waits, last = f"i wait {', '.join(window.wait_keys)}", ("possono terminare", "durano in tutto")
        if window.attribute_wait_key is not None and window.email_not_before is not None:
            # The waits before the email delay its arrival: the window is measured from there.
            delay = (f"i wait {', '.join(window.wait_keys_before)} che la precedono"
                     if len(window.wait_keys_before) > 1 else f"il wait {window.wait_keys_before[0]} che la precede")
            if window.deadline == window.email_not_before:
                time_left = (f"lo split viene valutato appena arriva l'email, perché a quell'ora la data del wait "
                             f"{window.attribute_wait_key} è già passata: il tester non ha il tempo di {verb} l'email")
            else:
                time_left = f"il tester ha meno di un'ora per {verb} l'email dopo il suo arrivo"
            self._warn(
                f"L'email {email_name!r} arriva non prima del {window.email_not_before}, per {delay}, e la scadenza "
                f"per {verb} l'email prima dell'engagement split {split_name} è il {window.deadline}: {time_left}. "
                "L'offset della data di wait prima di un engagement split si cambia nelle impostazioni: per dare "
                "tempo al tester va alzato."
            )
        elif window.attribute_wait_key is not None:
            self._warn(
                f"La scadenza per {verb} l'email {email_name!r} prima dell'engagement split {split_name} è il "
                f"{window.deadline}, meno di un'ora dopo l'ora di generazione: il tester ha meno di un'ora. "
                "L'offset della data di wait prima di un engagement split si cambia nelle impostazioni."
            )
        elif window.minutes == 0:
            self._warn(
                f"Tra l'email {email_name!r} e l'engagement split {split_name} {waits} {last[0]} subito: il tester "
                f"potrebbe non avere il tempo di {verb} l'email prima che lo split venga valutato."
            )
        else:
            self._warn(
                f"Tra l'email {email_name!r} e l'engagement split {split_name} {waits} {last[1]} "
                f"{_minutes_text(window.minutes)}: il tester ha meno di un'ora per {verb} l'email prima che lo split "
                "venga valutato."
            )

    @staticmethod
    def _has_wait_before(case: Case, action: ManualAction, activities: dict) -> bool:
        for keys in case_paths(case):
            if action.activity_key not in keys:
                continue
            end = keys.index(action.activity_key)
            start = keys.index(action.email_activity_key) if action.email_activity_key in keys[:end] else -1
            if any(activities.get(key) is not None and activities[key].kind in _WAIT_KINDS
                   for key in keys[start + 1:end]):
                return True
        return False

    # -- rows -------------------------------------------------------------------------------

    def run(self) -> GeneratedData:
        scenarios = sorted(self.coverage.scenarios, key=lambda s: (s.run_order, s.id))
        self._assign_keys(scenarios)
        contacts = self._build_contacts(scenarios)
        self._active_scenario = scenarios[0] if (self._one_active and scenarios) else None
        self._check_engagement_waits(scenarios)

        contact_rows = self._contact_contexts(scenarios)
        scenario_rows = [_Row(number, scenario) for number, scenario in enumerate(scenarios, 1)]
        rows_by_table: dict[str, list[dict[str, Value]]] = {}
        for table, level in self._tables_plan:
            if table is self._entry:
                continue
            contexts = scenario_rows if level == LEVEL_SCENARIO else self._present_rows(table, contact_rows)
            rows = [self._generic_row(table, context) for context in contexts]
            rows_by_table[table.name] = rows
            self._index_rows(table, level, contexts, rows)
        if self._entry is not None and any(table is self._entry for table, _ in self._tables_plan):
            rows_by_table[self._entry.name] = self._entry_rows(contact_rows)
            self._index_rows(self._entry, LEVEL_CONTACT, contact_rows, rows_by_table[self._entry.name])
        self._fill_timing(scenarios)

        tables = {
            table.name: TableData(table=table.name, columns=[f.name for f in _schema_fields(table)],
                                  rows=rows_by_table[table.name])
            for table, _ in self._tables_plan
        }
        return GeneratedData(tables=tables, contacts=contacts, scenarios_rows=self._scenario_rows(scenarios),
                             placeholders=self.placeholders, warnings=self.warnings)

    @staticmethod
    def _contact_contexts(scenarios: list[Scenario]) -> list[_Row]:
        """Spec, "Table selection" and plan.md: one row per contact, contacts in key order."""
        contexts: list[_Row] = []
        for scenario in scenarios:
            for case in scenario.cases:
                for key in case.contact_keys:
                    contexts.append(_Row(len(contexts) + 1, scenario, case, key))
        return contexts

    def _present_rows(self, table: Table, contexts: list[_Row]) -> list[_Row]:
        """Spec, "Row present or absent": "A contact whose row is absent has no row in that table's CSV."
        The rows left are numbered again, so ``n`` stays the row number in the table."""
        linked = next((ref for name, ref in self.coverage.row_fields.items() if name.lower() == table.name.lower()),
                      None)
        if linked is None:
            return contexts
        present = [context for context in contexts
                   if context.case is None or _lookup(context.case.contact_values, linked) != (True, None)]
        if len(present) == len(contexts):
            return contexts
        return [_Row(number, context.scenario, context.case, context.contact_key)
                for number, context in enumerate(present, 1)]

    def _generic_row(self, table: Table, row: _Row) -> dict[str, Value]:
        return {field.name: self._field_value(table, field, row) for field in _schema_fields(table)}

    def _index_rows(self, table: Table, level: str, contexts: list[_Row], rows: list[dict[str, Value]]) -> None:
        index = self._row_index.setdefault(table.name.lower(), {})
        for context, row in zip(contexts, rows):
            owner = context.scenario.key if level == LEVEL_SCENARIO else context.contact_key
            index[owner] = row

    def _joined_rows(self, row: _Row) -> dict[str, dict[str, Value]]:
        """The source rows that join for one contact: its own row in each contact-level table and its
        scenario's row in each scenario-level table."""
        joined: dict[str, dict[str, Value]] = {}
        for table_name, index in self._row_index.items():
            owner = row.scenario.key if self._levels.get(table_name) == LEVEL_SCENARIO else row.contact_key
            if owner in index:
                joined[table_name] = index[owner]
        return joined

    # -- the entry table --------------------------------------------------------------------

    def _entry_rows(self, contexts: list[_Row]) -> list[dict[str, Value]]:
        """Spec, "Table selection": "The entry CSV is kept consistent with the source CSVs: its rows are
        what the entry query would produce from them." Without an entry query, the entry table is
        filled by the general rules like any contact-level table."""
        entry = self._entry
        if self.model.entry_query is None:
            event = self.package.entry_event
            if event is None or event.kind == ENTRY_AUTOMATION:
                # An entry that no automation feeds has no entry query by design: nothing to warn about.
                self._warn(
                    f"Nessuna entry query riempie {entry.name}, quindi le sue righe vengono generate dalle regole "
                    "generali, come per ogni tabella a livello di contatto, e niente garantisce che corrispondano alle "
                    "tabelle sorgente."
                )
            return [self._generic_row(entry, context) for context in contexts]
        rows: list[dict[str, Value]] = []
        for context in contexts:
            sources = self._joined_rows(context)
            rows.append({field.name: self._entry_value(entry, field, context, sources)
                         for field in _schema_fields(entry)})
        return rows

    def _entry_value(self, entry: Table, field: Field, row: _Row, sources: dict[str, dict[str, Value]]) -> Value:
        expr = self._entry_plan.get(field.name)
        if expr is None:
            return self._entry_absent_value(entry, field, row)
        value, problem = self._evaluate(expr, sources)
        if problem is not None:
            rules = self._own_rules(entry, field)
            if rules and rules[0].kind == "key":
                key = self._rule_value(rules[0], entry, field, row)
                if key is not _SKIP:
                    self._warn(
                        f"{entry.name}.{field.name} è un campo chiave, ma {problem}. "
                        "Viene scritta la chiave generata, così le tabelle restano collegate dal join."
                    )
                    return _as_text(key)
            required = self._computed_by_query_value(entry, field, expr, row)
            if required is not _SKIP:
                return required
            self._note_placeholder(entry, field, problem)
            return self._fallback_value(entry, field, row)
        value = self._normalise(_as_text(value), entry, field)
        self._check_entry_requirements(entry, field, row, value)
        return value

    def _computed_by_query_value(self, entry: Table, field: Field, expr: object, row: _Row) -> object:
        """Spec, step 3, "Columns the entry query derives": an entry column that a split reads and that the entry
        query computes with an expression the generator does not understand takes the value the case needs
        (rule 2), with a warning in place of a placeholder. _SKIP when the column is not in that situation."""
        if not _has_unsupported(expr):
            return _SKIP
        rule = next((r for r in self._own_rules(entry, field) if r.kind == "split"), None)
        if rule is None:
            return _SKIP
        value = self._rule_value(rule, entry, field, row)
        if value is _SKIP:
            return _SKIP
        self._warn(
            f"{entry.name}.{field.name}: la entry query la calcola con un'espressione non compresa; il CSV contiene "
            "il valore che il caso richiede, valido con l'import diretto."
        )
        return self._normalise(_as_text(value), entry, field)

    def _evaluate(self, expr: object, sources: dict[str, dict[str, Value]]) -> tuple[Value, Optional[str]]:
        """Spec, "Table selection": "simple select expressions (column, literal, CONCAT, current date)";
        "Anything more complex is reported, and the affected entry column gets a placeholder".
        Returns (value, problem); a problem means the column gets a placeholder."""
        if isinstance(expr, SelectColumn):
            return self._column_value(expr.source, sources)
        if isinstance(expr, SelectLiteral):
            return expr.value, None
        if isinstance(expr, SelectCurrentDate):
            return self._current_date(expr.format)
        if isinstance(expr, SelectConcat):
            parts: list[str] = []
            for part in expr.parts:
                value, problem = self._evaluate(part, sources)
                if problem is not None:
                    return None, problem
                parts.append(value or "")   # T-SQL CONCAT turns NULL into an empty string
            return "".join(parts), None
        if isinstance(expr, SelectUnsupported):
            return None, f"la entry query lo riempie con l'espressione non supportata {expr.text!r}"
        return None, f"la entry query lo riempie con un'espressione che il generatore non conosce: {expr!r}"

    def _column_value(self, column: ColumnRef, sources: dict[str, dict[str, Value]]) -> tuple[Value, Optional[str]]:
        resolved = self._resolve_column(column)
        if resolved is None:
            return None, (f"la entry query copia {column.table or '?'}.{column.column}, che non corrisponde a nessun "
                          "campo di una tabella sorgente")
        table, field = resolved
        if table is self._entry:
            return None, f"la entry query legge la tabella di ingresso stessa, attraverso {table.name}.{field.name}"
        source_row = sources.get(table.name.lower())
        if source_row is None:
            return None, f"la entry query copia {table.name}.{field.name}, ma {table.name} non ha righe generate"
        return source_row.get(field.name), None

    def _current_date(self, dotnet_format: Optional[str]) -> tuple[Value, Optional[str]]:
        """``GETDATE()`` and friends: the generation time, in ``Settings.date_format`` for a plain date
        or in the query's own format for ``FORMAT(GETUTCDATE(), '...')``."""
        if dotnet_format is None:
            return self._format_date(self.generation_time), None
        try:
            pattern = _dotnet_to_strftime(dotnet_format)
        except (ValueError, KeyError) as error:
            return None, f"il formato data {dotnet_format!r} della entry query non si può convertire: {error}"
        return self.generation_time.strftime(pattern), None

    def _entry_absent_value(self, entry: Table, field: Field, row: _Row) -> Value:
        """Entry columns the query does not fill get what the query would produce: the schema default,
        else an empty cell when nullable, else a placeholder (listed in ``placeholders``). A key column
        is the exception: priority 1 says it holds the generated key, so that the tables join."""
        rules = self._own_rules(entry, field)
        if rules[0].kind == "key":
            value = self._rule_value(rules[0], entry, field, row)
            if value is not _SKIP:
                self._warn(
                    f"{entry.name}.{field.name} è un campo chiave, ma la entry query non lo riempie. Viene scritta "
                    "la chiave generata, così le tabelle restano collegate dal join."
                )
                return _as_text(value)
        if any(rule.kind == "split" for rule in rules):
            self._warn(
                f"{entry.name}.{field.name} è letto da uno split, ma la entry query non lo riempie. La riga di "
                "ingresso riceve quello che produrrebbe la query, cioè il valore di default o una cella vuota, non il "
                "valore del caso."
            )
        for rule in rules:
            if rule.kind == "email" and self._is_default_email_field(entry, field):
                self._warn(
                    f"{entry.name}.{field.name} è l'indirizzo di invio del journey, ma la entry query non lo "
                    "riempie. L'indirizzo viene scritto perché le email possano partire. Il controllo sulla tabella "
                    "di ingresso lo consente."
                )
                return self._normalise(_as_text(self._rule_value(rule, entry, field, row)), entry, field)
            if rule.kind == "wait":
                self._warn(
                    f"{entry.name}.{field.name} è la data di un wait by attribute, ma la entry query non la "
                    "riempie. La data del wait viene scritta perché i contatti possano superare il wait. Il controllo "
                    "sulla tabella di ingresso lo consente."
                )
                return self._normalise(_as_text(self._rule_value(rule, entry, field, row)), entry, field)
        default = self._default_value(entry, field)
        if default is not None:
            return default
        if field.is_nullable:
            return None
        self._note_placeholder(
            entry, field, "la entry query non lo riempie, non ammette null e non ha un valore di default")
        return self._fallback_value(entry, field, row)

    def _default_value(self, table: Table, field: Field) -> Value:
        """The schema default as a CSV value: Booleans as True/False, GETDATE() as the generation time."""
        default = field.default
        if default is None or not default.strip():
            return None
        if default.strip().lower().replace(" ", "") in _CURRENT_DATE_DEFAULTS:
            return self._format_date(self.generation_time)
        return self._normalise(default, table, field)

    def _check_entry_requirements(self, entry: Table, field: Field, row: _Row, value: Value) -> None:
        """Warn when the value the entry query produces differs from what the entry column needs."""
        for rule in self._own_rules(entry, field):
            if rule.kind not in ("key", "split", "activation", "wait"):
                continue
            required = self._rule_value(rule, entry, field, row)
            if required is _SKIP:
                continue
            required = self._normalise(_as_text(required), entry, field)
            if required != value:
                self._warn(
                    f"La entry query riempie {entry.name}.{field.name} con un valore diverso da {required!r}, che "
                    f"sarebbe {rule.describe()}. La riga di ingresso mantiene quello che produce la query."
                )
            return   # only the highest requirement counts

    # -- scenarios.csv ----------------------------------------------------------------------

    def _scenario_rows(self, scenarios: list[Scenario]) -> list[dict[str, Value]]:
        """Spec, "Outputs": "scenarios.csv: One row per scenario: its key, its scenario-level values, run
        order, what to activate". Under one active scenario at a time, only the first is active at the
        start; ``activation_field`` names the field to set to True on the scenario's row when its turn comes."""
        rows: list[dict[str, Value]] = []
        for scenario in scenarios:
            keys = [key for case in scenario.cases for key in case.contact_keys]
            active = not self._one_active or scenario is self._active_scenario
            row: dict[str, Value] = {
                "scenario_key": scenario.key,
                "run_order": str(scenario.run_order),
                "has_manual_action": _bool_text(scenario.has_manual_action),
                "is_statistical": _bool_text(scenario.is_statistical),
                "case_count": str(len(scenario.cases)),
                "contact_count": str(len(keys)),
                "first_contact_key": keys[0] if keys else None,
                "last_contact_key": keys[-1] if keys else None,
                "active_at_start": "yes" if active else "no",
                "activation_field": (f"{self._activation_ref[0]}.{self._activation_ref[1]}"
                                     if self._one_active and self._activation_ref is not None else None),
            }
            for ref in self.coverage.scenario_fields:
                column, value = self._scenario_field(ref, scenario)
                row[column] = value
            rows.append(row)
        return rows

    def _scenario_field(self, ref: FieldRef, scenario: Scenario) -> tuple[str, Value]:
        """A scenario-level value for scenarios.csv, named ``Table.Field`` in the schema spelling."""
        found, value = _lookup(scenario.values, ref)
        value = _as_text(value) if found else None
        table = self._table(ref[0])
        field = table.field(ref[1]) if table is not None else None
        if field is None:
            return f"{ref[0]}.{ref[1]}", value
        return f"{table.name}.{field.name}", self._normalise(value, table, field)
