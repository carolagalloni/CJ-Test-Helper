"""Trust checks: the generator reads the written files back and proves they are right.

Spec, "Development priorities", the meaning of "trusted":

1. Every value respects the type, length, nullability and primary key of its schema.
2. Every key joins: each contact row finds its scenario row and its rows in the linked tables.
3. Each generated contact, simulated again through the journey from the written values,
   takes exactly the path it was created for.
4. Every reachable branch is crossed by at least one case, and every branch not covered is
   listed with its reason.
5. The same JSON and settings give the same data.

Plus the acceptance rule on addresses: contacts needing an open or click use the real inbox,
all others use ``@example.invalid``. The files are read with the standard ``csv`` module, not
with the writer's helpers, so a writer bug cannot hide from the checks.

Spec, "Extensions beyond the pilot":

- Row present or absent: "A contact whose row is absent has no row in that table's CSV. This is the
  only exception to 'each contact row finds its rows in the linked tables'." The join check accepts a
  missing row exactly for the contacts whose case has the row absent (``CoverageResult.row_absent``),
  and requires that those contacts have no row there.
- The simulation from the files applies the same rules as the coverage: a Journey Data condition reads
  the entry row as it was at entry, every field of an absent row reads null, and an Update contact on
  such a table inserts the row (``graph.Walker(..., row_fields=TableModel.existence_tested)``).
- An entry source that no query fills (Run Once, recurring schedule, CloudPages form, API event) has no
  entry query to compare with: the entry check says so and does not apply.

Spec, step 3, "Honest checks":

- The entry check fails only on what it can verify. A column that a split reads and that the query computes with an
  expression the generator does not understand, a filter it does not understand and a LEFT JOIN read as an inner
  join are "not verified" notes. A filter on the date part moved by a number of days is verified on the date part;
  a column the query does not populate and whose schema default is the current date accepts any date.
- The simulation from the files runs with the same clock (``Settings.entry_time``, None = the generation time) and
  the same exit criteria as the coverage: a path that leaves by the exit criteria ends with ``Step(wait, None)``.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Optional

from . import graph, sqlparse_lite
from .model import (
    ENTRY_AUTOMATION,
    ENTRY_OTHER,
    ENTRY_RUN_ONCE,
    ENTRY_SCHEDULED,
    KIND_ENGAGEMENT,
    KIND_RANDOM,
    STATUS_COVERED,
    STATUS_MANUAL,
    STATUS_NOT_COVERED,
    Case,
    CheckResult,
    CoverageResult,
    DateBoundFilter,
    DateOffsetFilter,
    EqualityFilter,
    FieldRef,
    GeneratedData,
    IsNullFilter,
    Package,
    ParsedQuery,
    SelectColumn,
    SelectConcat,
    SelectCurrentDate,
    SelectLiteral,
    SelectNull,
    SelectUnsupported,
    Settings,
    Step,
    TableModel,
    UnsupportedFilter,
    Value,
)

MAX_DETAILS = 25

ENTRY_CHECK = "Le righe della tabella di ingresso corrispondono alla entry query"

# The first words of the note of a check that does not apply to the export (it passes, with that note).
NOT_APPLICABLE = "non si applica"

# The entry source of an entry that no query fills, as the note of the entry check names it.
_ENTRY_SOURCE_WORDS = {
    ENTRY_RUN_ONCE: "una data extension con pianificazione Run Once",
    ENTRY_SCHEDULED: "una data extension con una pianificazione ricorrente",
}

# A branch status as the check details say it (the glossary words of docs/brand.md).
_STATUS_WORDS = {STATUS_COVERED: "coperto", STATUS_MANUAL: "coperto con azione manuale"}


def read_csv_bytes(data: bytes) -> tuple[list[str], list[dict[str, Value]]]:
    """Header and rows; empty cells become None (the CSV convention for null)."""
    text = data.decode("utf-8")
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows:
        return [], []
    header = rows[0]
    out: list[dict[str, Value]] = []
    for r in rows[1:]:
        if not r:
            continue
        out.append({h: (v if v != "" else None) for h, v in zip(header, r)})
    return header, out


def _trim(details: list[str]) -> list[str]:
    if len(details) <= MAX_DETAILS:
        return details
    return details[:MAX_DETAILS] + [f"... e altri {len(details) - MAX_DETAILS}"]


def _parse_date(value: str, date_format: str) -> Optional[datetime]:
    """Spec, Settings: "Date format in the CSVs". A value is a date only when it is written exactly in
    that format (``2026-10-10`` and ``2026-10-11 12:00:00`` are not dates in ``%Y-%m-%d %H:%M``)."""
    try:
        parsed = datetime.strptime(value, date_format)
    except ValueError:
        return None
    return parsed if parsed.strftime(date_format) == value else None


class Checker:
    def __init__(self, package: Package, table_model: TableModel, coverage: CoverageResult,
                 data: GeneratedData, files: dict[str, bytes], settings: Settings, generation_time: datetime):
        self.package = package
        self.table_model = table_model
        self.coverage = coverage
        self.data = data
        self.files = files
        self.settings = settings
        self.now = generation_time
        self.schema = graph.Schema(package.tables)
        self.csv: dict[str, tuple[list[str], list[dict[str, Value]]]] = {}
        for name in table_model.selected_tables:
            content = files.get(f"{name}.csv")
            self.csv[name] = read_csv_bytes(content) if content is not None else ([], [])
        # The rows of each table by contact key (tables with a contact key field) or by scenario key
        # (scenario-level tables), read from the files: the checks never use the generator's own lists
        # to find a contact's rows.
        self.rows_of_contact: dict[str, dict[str, list[dict[str, Value]]]] = {}
        self.rows_of_scenario: dict[str, dict[str, list[dict[str, Value]]]] = {}
        for name in table_model.selected_tables:
            contact_field, scenario_field = self._contact_key_field(name), self._scenario_key_field(name)
            index_field, index = (contact_field, self.rows_of_contact) if contact_field is not None else \
                (scenario_field, self.rows_of_scenario)
            if index_field is None:
                continue
            by_key = index.setdefault(name, {})
            for row in self.csv[name][1]:
                if row.get(index_field) is not None:
                    by_key.setdefault(row[index_field], []).append(row)
        self.date_formats = self._query_date_formats()
        self._excluded: set[str] = set()
        # Spec, "Row present or absent": lower-case table -> the contacts whose case has no row there. These
        # contacts, and only these, must have no row in that table's CSV.
        self.absent: dict[str, set[str]] = {}
        for table in coverage.row_fields:
            keys = {key for case in coverage.cases if coverage.row_absent(case, table) for key in case.contact_keys}
            self.absent.setdefault(table.lower(), set()).update(keys)

    def run_all(self) -> list[CheckResult]:
        return [
            self.check_schema(),
            self.check_joins(),
            self.check_entry_query(),
            self.check_simulation(),
            self.check_coverage(),
            self.check_addresses(),
        ]

    def _query_date_formats(self) -> dict[tuple[str, str], list[str]]:
        """The formats an entry column may hold besides ``Settings.date_format``: a column the entry query
        fills with ``FORMAT(GETDATE(), '<format>')`` holds that format (spec: "simple select
        expressions ... current date")."""
        formats: dict[tuple[str, str], list[str]] = {}
        query = self.table_model.entry_query
        for item in (query.select if query is not None else []):
            if isinstance(item.expr, SelectCurrentDate) and item.expr.format:
                key = (query.target_table.lower(), item.target_column.lower())
                formats.setdefault(key, []).append(sqlparse_lite.dotnet_to_strftime(item.expr.format))
        return formats

    def _is_date(self, table: str, field_name: str, value: str) -> bool:
        formats = [self.settings.date_format] + self.date_formats.get((table.lower(), field_name.lower()), [])
        return any(_parse_date(value, fmt) is not None for fmt in formats)

    # 1 ---------------------------------------------------------------------------------
    def check_schema(self) -> CheckResult:
        details: list[str] = []
        for name in self.table_model.selected_tables:
            table = self.package.tables.get(name)
            header, rows = self.csv[name]
            if table is None:
                details.append(f"{name}: l'export non contiene il suo schema")
                continue
            if not header:
                details.append(f"{name}.csv manca o è vuoto")
                continue
            if header != table.field_names:
                details.append(f"{name}.csv: l'intestazione {header} è diversa dallo schema {table.field_names}")
                continue
            pk_fields = table.primary_key
            seen_pk: set[tuple] = set()
            for i, row in enumerate(rows, start=2):
                for f in table.fields:
                    v = row.get(f.name)
                    if v is None:
                        if not f.is_nullable:
                            details.append(f"{name}.csv riga {i}: {f.name} è vuoto ma non ammette null")
                        continue
                    if f.length is not None and f.type in ("Text", "EmailAddress", "Phone", "Locale") and len(v) > f.length:
                        details.append(f"{name}.csv riga {i}: {f.name} ha {len(v)} caratteri, lo schema ne ammette {f.length}")
                    if f.type == "Boolean" and v not in ("True", "False"):
                        details.append(f"{name}.csv riga {i}: {f.name} = {v!r} non è True o False")
                    elif f.type == "Date" and not self._is_date(name, f.name, v):
                        details.append(f"{name}.csv riga {i}: {f.name} = {v!r} non è una data nel formato {self.settings.date_format}")
                    elif f.type == "Number":
                        try:
                            int(v)
                        except ValueError:
                            details.append(f"{name}.csv riga {i}: {f.name} = {v!r} non è un numero intero")
                    elif f.type == "Decimal":
                        try:
                            float(v)
                        except ValueError:
                            details.append(f"{name}.csv riga {i}: {f.name} = {v!r} non è un numero")
                    elif f.type == "EmailAddress" and ("@" not in v or " " in v):
                        details.append(f"{name}.csv riga {i}: {f.name} = {v!r} non è un indirizzo email")
                    elif f.type == "Phone" and (" " in v or not v.replace("+", "").isdigit()):
                        details.append(f"{name}.csv riga {i}: {f.name} = {v!r} non è un numero di telefono")
                if pk_fields:
                    pk = tuple(row.get(f) for f in pk_fields)
                    if any(p is None for p in pk):
                        details.append(f"{name}.csv riga {i}: la chiave primaria {pk_fields} ha una parte vuota")
                    elif pk in seen_pk:
                        details.append(f"{name}.csv riga {i}: chiave primaria {pk} duplicata")
                    seen_pk.add(pk)
        return CheckResult("I valori rispettano tipo, lunghezza, null e chiave primaria", not details, _trim(details))

    # 2 ---------------------------------------------------------------------------------
    def _column_values(self, ref: FieldRef) -> Optional[set[Value]]:
        table, field_name = ref
        if table not in self.csv:
            return None
        header, rows = self.csv[table]
        if field_name not in header:
            return None
        return {r.get(field_name) for r in rows}

    def check_joins(self) -> CheckResult:
        """Spec: "Every key joins: each contact row finds its scenario row and its rows in the linked
        tables"; acceptance: "Every Target row joins to a Master row and to a Config row".

        Every member of a key group is compared with every other member, in both directions, so a row
        that points at a missing row is found whichever table it is in. Then, read from the files only:
        each contact of each case has exactly one row in every contact-level table, no row belongs to a
        contact that was not generated, and a contact links to one scenario row, not to two.

        The one exception (spec, "Row present or absent"): a contact whose case has the row absent in a table
        with the "row present / row absent" variable has no row in that table, and must have none."""
        details: list[str] = []
        for group in self.table_model.key_groups:
            members = [f for f in group.fields if f[0] in self.csv]
            if len(members) < 2:
                continue
            values: dict[FieldRef, set[str]] = {}
            for member in members:
                found = self._column_values(member)
                if found is None:
                    details.append(f"{member[0]}.{member[1]} fa parte di un gruppo di chiavi ma non è nel CSV")
                else:
                    values[member] = found - {None}
            for member, member_values in values.items():
                for other, other_values in values.items():
                    missing = sorted(member_values - other_values) if other != member else []
                    if group.kind == "contact":
                        # A contact whose case has the row absent has no row in that table (spec, "Row present
                        # or absent"); whether it really has none is checked with the contact rows below.
                        allowed = self.absent.get(other[0].lower(), set())
                        missing = [value for value in missing if value not in allowed]
                    if missing:
                        details.append(f"{len(missing)} valori di {member[0]}.{member[1]} non hanno una riga in "
                                       f"{other[0]}.{other[1]}: " + ", ".join(missing[:5]))
        details.extend(self._check_contact_rows())
        details.extend(self._check_scenario_links())
        return CheckResult("Ogni chiave trova le sue righe nelle altre tabelle", not details, _trim(details))

    def _generated_contacts(self) -> set[str]:
        """The contacts of the cases, which must be the contacts the generator registered."""
        return {key for case in self.coverage.cases for key in case.contact_keys}

    def _check_contact_rows(self) -> list[str]:
        """Each contact of each case has exactly one row in every contact-level table, and a row whose
        key is not a generated contact is reported (spec: "Each contact therefore belongs to exactly one
        scenario and is never reused"). A contact whose case has the row absent has no row in that table."""
        details: list[str] = []
        expected = self._generated_contacts()
        registered = {c.key for c in self.data.contacts}
        if expected != registered:
            odd = sorted(expected ^ registered)
            details.append(f"i contatti dei casi e i contatti registrati non coincidono ({len(odd)}): "
                           + ", ".join(odd[:5]))
        for name in self.table_model.contact_tables:
            if name not in self.rows_of_contact:
                continue
            rows = self.rows_of_contact[name]
            absent = self.absent.get(name.lower(), set()) & expected
            missing = sorted(expected - absent - set(rows))
            if missing:
                details.append(f"{len(missing)} contatti generati non hanno righe in {name}: " + ", ".join(missing[:5]))
            present = sorted(absent & set(rows))
            if present:
                details.append(f"{len(present)} contatti, il cui caso prevede che non abbiano una riga in {name}, ne hanno "
                               "una: " + ", ".join(present[:5]))
            extra = sorted(set(rows) - expected)
            if extra:
                details.append(f"{name} ha righe per {len(extra)} contatti che non sono stati generati: " + ", ".join(extra[:5]))
            several = sorted(key for key, found in rows.items() if len(found) > 1)
            if several:
                details.append(f"{len(several)} contatti hanno più righe in {name}: " + ", ".join(several[:5]))
        return details

    def _check_scenario_links(self) -> list[str]:
        """A contact links to exactly one scenario row: every scenario key written in its own rows is the
        same (spec: "Any matching row counts ... Each contact therefore belongs to exactly one scenario")."""
        details: list[str] = []
        linked: dict[str, dict[str, list[str]]] = {}
        for name in self.table_model.contact_tables:
            scenario_field = self._scenario_key_field(name)
            if scenario_field is None:
                continue
            for contact, rows in self.rows_of_contact.get(name, {}).items():
                for row in rows:
                    if row.get(scenario_field) is not None:
                        linked.setdefault(contact, {}).setdefault(row[scenario_field], []).append(name)
        for contact, scenarios in sorted(linked.items()):
            if len(scenarios) > 1:
                shown = "; ".join(f"{key} in {', '.join(tables)}" for key, tables in sorted(scenarios.items()))
                details.append(f"il contatto {contact} è collegato a {len(scenarios)} righe di scenario: {shown}")
        return details

    def _contact_key_field(self, table: str) -> Optional[str]:
        for group in self.table_model.key_groups:
            if group.kind == "contact":
                for t, f in group.fields:
                    if t == table:
                        return f
        return None

    def _scenario_key_field(self, table: str) -> Optional[str]:
        for group in self.table_model.key_groups:
            if group.kind == "scenario":
                for t, f in group.fields:
                    if t == table:
                        return f
        return None

    # 3 ---------------------------------------------------------------------------------
    def check_entry_query(self) -> CheckResult:
        query = self.table_model.entry_query
        entry = self.table_model.entry_table
        if query is None:
            return CheckResult(ENTRY_CHECK, True, [self._no_entry_query_note()])
        if entry not in self.csv:
            return CheckResult(ENTRY_CHECK, False, [f"{entry}.csv manca"])
        details: list[str] = []
        notes: list[str] = []
        self._excluded = self._exclusion_joins(query)
        joined = self._join_sources(query, details)
        if joined is None:
            return CheckResult(ENTRY_CHECK, False, _trim(details))
        activation = self.settings.activation_field or self.table_model.activation_field
        one_active = self.settings.one_active_scenario
        if one_active is None:
            one_active = activation is not None
        entry_event = self.package.entry_event
        if entry_event is not None and entry_event.kind == ENTRY_RUN_ONCE:
            one_active = False   # spec: with Run Once "'one active scenario at a time' is not offered"
        kept = []
        for combo in joined:
            ok = True
            for flt in query.filters:
                if isinstance(flt, UnsupportedFilter):
                    continue
                col = flt.column
                if isinstance(flt, IsNullFilter) and not flt.negated and col.table in self._excluded:
                    continue   # exclusion join: the generated contacts are new, so they have no row there
                if one_active and activation is not None and col.table.lower() == activation[0].lower() \
                        and col.column.lower() == activation[1].lower():
                    continue   # only the first scenario is active; the others are activated later by the tester
                value = self._combo_value(combo, col)
                if isinstance(flt, EqualityFilter):
                    if not _sql_equal(value, flt.value):
                        ok = False
                elif isinstance(flt, IsNullFilter):
                    if (value is None) == flt.negated:
                        ok = False
                elif isinstance(flt, DateBoundFilter):
                    d = _parse_date(value, self.settings.date_format) if value else None
                    if d is None:
                        ok = False
                    elif flt.bound == "start" and not (self.now >= d):
                        ok = False
                    elif flt.bound == "end" and not (self.now <= d):
                        ok = False
                elif isinstance(flt, DateOffsetFilter):
                    d = _parse_date(value, self.settings.date_format) if value else None
                    if d is None or d.date() != (self.now + timedelta(days=flt.days)).date():
                        ok = False
            if ok:
                kept.append(combo)
        if any(isinstance(f, UnsupportedFilter) for f in query.filters):
            notes.append("alcuni filtri della entry query non sono stati interpretati, quindi non sono stati verificati")
        for source in query.sources:
            if source.optional and source.table not in self._excluded:
                notes.append(f"non verificato: {source.table} è unita con LEFT JOIN, letta come inner join")
        if self._excluded:
            notes.append(f"la entry query esclude {', '.join(sorted(self._excluded))} con un LEFT JOIN e un filtro IS NULL: quel "
                         "join e quel filtro non sono stati verificati, perché i contatti generati sono considerati nuovi")
        if one_active and activation is not None:
            notes.append(f"il filtro sul campo di attivazione {activation[0]}.{activation[1]} non è stato verificato: nel CSV è "
                         "attivo solo il primo scenario e il tester attiva gli altri uno alla volta")

        table = self.package.tables[entry]
        header, rows = self.csv[entry]
        pk = table.primary_key or table.field_names
        # Spec, step 3, "Honest checks": "The trust check on the entry query reports a column it cannot verify (an
        # expression the generator does not understand, ...) as "not verified", with the reason, and fails only on
        # what it can verify." Such a column holds the value the case needs when a split reads it, a placeholder
        # otherwise; either way it is not compared.
        unverified: set[str] = set()
        for item in query.select:
            f = table.field(item.target_column)
            if f is not None and _has_unsupported(item.expr) and f.name not in unverified:
                unverified.add(f.name)
                notes.append(f"non verificato: {entry}.{f.name} è calcolata da un'espressione non compresa "
                             f"({_unsupported_text(item.expr)})")
        expected: dict[tuple, dict[str, Value]] = {}
        for combo in kept:
            row: dict[str, Value] = {}
            if query.select_all:
                for f in table.fields:
                    for src_table, src_row in combo.items():
                        if f.name in src_row:
                            row[f.name] = src_row[f.name]
            for item in query.select:
                f = table.field(item.target_column)
                if f is None:
                    continue
                value = self._select_value(item.expr, combo)
                if value is _SKIP:
                    continue
                row[f.name] = value
            key = tuple(row.get(p) for p in pk)
            expected[key] = row
        actual = {tuple(r.get(p) for p in pk): r for r in rows}
        for key in expected:
            if key not in actual:
                details.append(f"la entry query produrrebbe una riga con chiave {key} che non è in {entry}.csv")
        for key, row in actual.items():
            if key not in expected:
                details.append(f"{entry}.csv ha una riga con chiave {key} che la entry query non produrrebbe")
                continue
            exp = expected[key]
            for col, v in exp.items():
                if row.get(col) != v:
                    details.append(f"{entry}.csv, riga con chiave {key}: {col} vale {row.get(col)!r} ma la entry query dà {v!r}")
            wait_fields = {a.wait_field[1].lower() for a in self.package.journey.activities.values()
                           if a.wait_field is not None and a.wait_field[0].lower() == entry.lower()}
            default_email = (self.package.journey.default_email_field or "").lower()
            for f in table.fields:
                if f.name in exp or f.name in unverified:
                    continue
                if f.name.lower() in wait_fields or (default_email and f.name.lower() == default_email):
                    continue   # a wait date or the send address the journey needs, filled although the query does not
                v = row.get(f.name)
                if v is not None and f.default is not None \
                        and f.default.strip().lower().replace(" ", "") in graph.CURRENT_DATE_DEFAULTS \
                        and self._is_date(entry, f.name, v):
                    continue   # spec, step 3: a column defaulting to the current date accepts any date
                allowed = {None}
                if f.default is not None:
                    allowed.add(_normalise_default(f, f.default))
                if v not in allowed and f.is_nullable:
                    details.append(f"{entry}.csv, riga con chiave {key}: {f.name} vale {v!r} ma la entry query non lo popola")
        return CheckResult(ENTRY_CHECK, not details, _trim(details) + notes)

    def _no_entry_query_note(self) -> str:
        """Why there is nothing to compare when no entry query fills the entry table.

        Spec, "Entry source and procedure": an entry source other than the automation of the pilot is not
        filled by a query (Run Once, recurring schedule, CloudPages form, API event); its entry rows are the
        imported CSV, so the check does not apply. For an automation entry the export simply holds no query."""
        event = self.package.entry_event
        kind = event.kind if event is not None else ENTRY_AUTOMATION
        entry = self.table_model.entry_table
        source = _ENTRY_SOURCE_WORDS.get(kind)
        if kind == ENTRY_OTHER:
            source = f"un evento di tipo {event.event_type}" if event.event_type else "un evento senza tipo"
        if source is not None:
            return (f"{NOT_APPLICABLE}: l'entry source di questo journey è {source}, che nessuna query "
                    f"popola, quindi le righe di ingresso sono quelle di {entry}.csv e non c'è una entry query con cui "
                    "confrontarle")
        return "nessuna entry query nell'export: non c'è nulla da confrontare"

    def _exclusion_joins(self, query: ParsedQuery) -> set[str]:
        """Tables the entry query excludes with ``LEFT JOIN X ON ... WHERE X.key IS NULL``: a contact enters
        only when it has no row in X. The parser reads that as an inner join plus a filter no row can meet;
        here the generated contacts count as new (datagen does the same), so X is left out of the join."""
        source = next((q for q in self.package.queries if q.name == query.name), None)
        if source is None:
            return set()
        return set(sqlparse_lite.exclusion_joins(source, self.package.tables))

    def _join_sources(self, query: ParsedQuery, details: list[str]) -> Optional[list[dict[str, dict[str, Value]]]]:
        """Nested-loop join of the source CSVs along the query's join equalities."""
        sources = [s.table for s in query.sources if s.table not in self._excluded]
        for name in sources:
            if name not in self.csv:
                details.append(f"la tabella sorgente {name} non ha un CSV da mettere in join")
                return None
        combos: list[dict[str, dict[str, Value]]] = [{}]
        for name in sources:
            _, rows = self.csv[name]
            new_combos: list[dict[str, dict[str, Value]]] = []
            for combo in combos:
                for row in rows:
                    candidate = dict(combo)
                    candidate[name] = row
                    if self._joins_hold(query, candidate):
                        new_combos.append(candidate)
            combos = new_combos
        return combos

    def _joins_hold(self, query: ParsedQuery, combo: dict[str, dict[str, Value]]) -> bool:
        for join in query.joins:
            if join.left.table in combo and join.right.table in combo:
                if self._combo_value(combo, join.left) != self._combo_value(combo, join.right):
                    return False
        return True

    def _combo_value(self, combo: dict[str, dict[str, Value]], col) -> Value:
        row = combo.get(col.table)
        if row is None and col.table in self._excluded:
            return None   # a column of an excluded (LEFT JOIN ... IS NULL) table is NULL
        if row is None:
            for name, r in combo.items():
                table = self.package.tables.get(name)
                if table is not None and table.field(col.column) is not None:
                    row, col_table = r, name
                    return row.get(table.field(col.column).name)
            return None
        table = self.package.tables.get(col.table)
        f = table.field(col.column) if table is not None else None
        return row.get(f.name if f is not None else col.column)

    def _select_value(self, expr, combo):
        if isinstance(expr, SelectColumn):
            return self._combo_value(combo, expr.source)
        if isinstance(expr, SelectLiteral):
            return expr.value
        if isinstance(expr, SelectNull):
            return None
        if isinstance(expr, SelectCurrentDate):
            if expr.format:
                from .sqlparse_lite import dotnet_to_strftime
                return self.now.strftime(dotnet_to_strftime(expr.format))
            return self.now.strftime(self.settings.date_format)
        if isinstance(expr, SelectConcat):
            parts = [self._select_value(p, combo) for p in expr.parts]
            if any(p is _SKIP for p in parts):
                return _SKIP
            return "".join("" if p is None else str(p) for p in parts)
        return _SKIP

    # 4 ---------------------------------------------------------------------------------
    def check_simulation(self) -> CheckResult:
        details: list[str] = []
        # The same rules as the coverage (spec, "Extensions beyond the pilot"): Journey Data reads the entry row
        # as written (the lookup reads the files, i.e. the values at entry), every field of an absent row reads
        # null, and an Update contact on such a table inserts the row. Step 3: the clock starts at the entry time
        # and the exit criteria are evaluated at the end of every wait, as in the coverage.
        walker = graph.Walker(self.package.journey, self.schema, self.package.emails,
                              self.settings.case_insensitive_text, self.now, self.settings.date_format,
                              row_fields=self.table_model.existence_tested, entry_time=self.settings.entry_time)
        cases = {c.id: c for c in self.coverage.cases}
        scenario_of = {s.id: s for s in self.coverage.scenarios}
        has_scenario_link = any(self._scenario_key_field(name) for name in self.table_model.contact_tables)
        checked = 0
        for contact in self.data.contacts:
            case = cases.get(contact.case_id)
            scenario = scenario_of.get(contact.scenario_id)
            if case is None or scenario is None:
                details.append(f"il contatto {contact.key} appartiene al caso sconosciuto {contact.case_id}")
                continue
            lookup, link = self._contact_lookup(contact.key)
            if has_scenario_link and link != scenario.key:
                details.append(f"il contatto {contact.key} (caso {case.id}) è collegato allo scenario {link!r} dalle "
                               f"sue righe, ma è stato creato per lo scenario {scenario.key!r}")
            if case.random is None:
                steps = self._simulate(walker, lookup, case, None)
                if steps != case.steps:
                    details.append(f"il contatto {contact.key} (caso {case.id}) segue il percorso {_describe(steps)} invece di {_describe(case.steps)}")
                checked += 1
            else:
                for branch in case.random.branches:
                    steps = self._simulate_branch(walker, lookup, case, branch)
                    expected = list(case.steps[:-1]) + [Step(case.random.activity_key, branch.outcome_key)] + list(branch.steps)
                    if steps != expected:
                        details.append(f"il contatto {contact.key} (caso {case.id}, branch {branch.outcome_key} del random split) "
                                       f"segue il percorso {_describe(steps)} invece di {_describe(expected)}")
                    checked += 1
        notes = [f"{checked} percorsi di contatti simulati dai valori scritti"]
        return CheckResult("Ogni contatto simulato dai file segue il percorso previsto", not details, _trim(details) + notes)

    def _contact_lookup(self, contact_key: str):
        """The value lookup for one contact, read from the files only, and the scenario key it links to.

        Spec: "simulated again through the journey from the written values". The contact's own rows are
        found by its key; its scenario row is the one its own Entry row points to (docs/assumptions.md,
        row 12: "The Config lookup goes through the entry table"), not the scenario the generator has in
        memory. Without a scenario key in the Entry row, the first contact-level table that has one is used.
        A table where the contact has no row reads null in every field: for a table with the "row present /
        row absent" variable that is an absent row.
        """
        entry = self.table_model.entry_table
        link: Optional[str] = None
        for name in [entry] + [n for n in self.table_model.contact_tables if n != entry]:
            scenario_field = self._scenario_key_field(name)
            for row in self.rows_of_contact.get(name, {}).get(contact_key, []):
                if scenario_field is not None and row.get(scenario_field) is not None:
                    link = row[scenario_field]
                    break
            if link is not None:
                break
        rows_by_ref: dict[str, dict[str, Value]] = {}
        for name in self.table_model.selected_tables:
            scenario_field = self._scenario_key_field(name)
            if name in self.rows_of_contact:
                candidates = self.rows_of_contact[name].get(contact_key, [])
                if scenario_field is not None:
                    candidates = [row for row in candidates if row.get(scenario_field) == link]
            elif name in self.rows_of_scenario and link is not None:
                candidates = self.rows_of_scenario[name].get(link, [])
            else:
                continue
            if candidates:
                rows_by_ref[name] = candidates[0]

        def lookup(ref: FieldRef):
            row = rows_by_ref.get(ref[0])
            if row is None:
                return None
            return row.get(ref[1])
        return lookup, link

    def _simulate_branch(self, walker: graph.Walker, lookup, case: Case, branch) -> list[Step]:
        """Simulate one branch of a statistical case: every random split on it takes the branch's outcome."""
        manual_keys = {m.activity_key for m in case.manual_actions}
        choices = {case.random.activity_key: branch.outcome_key}
        for s in branch.steps:
            act = self.package.journey.activities.get(s.activity_key)
            if act is not None and act.kind == KIND_RANDOM and s.outcome_key is not None:
                choices[s.activity_key] = s.outcome_key

        def choose(activity, state):
            if activity.kind == KIND_ENGAGEMENT:
                want_yes = activity.key in manual_keys
                for o in activity.outcomes:
                    if o.when is want_yes:
                        return o
                return activity.outcomes[-1] if activity.outcomes else None
            if activity.kind == KIND_RANDOM:
                chosen = activity.outcome(choices[activity.key]) if activity.key in choices else None
                return chosen or (activity.outcomes[0] if activity.outcomes else None)
            return None

        state = walker.start()
        walker.run(state, lookup, choose)
        return list(state.steps)

    def _simulate(self, walker: graph.Walker, lookup, case: Case, random_outcome: Optional[str]) -> list[Step]:
        manual_keys = {m.activity_key for m in case.manual_actions}
        random_choices: dict[str, str] = {}
        if case.random is not None and random_outcome is not None:
            random_choices[case.random.activity_key] = random_outcome
            branch = next((b for b in case.random.branches if b.outcome_key == random_outcome), None)
            if branch is not None:
                for s in branch.steps:
                    act = self.package.journey.activities.get(s.activity_key)
                    if act is not None and act.kind == KIND_RANDOM and s.outcome_key is not None:
                        random_choices[s.activity_key] = s.outcome_key

        def choose(activity, state):
            if activity.kind == KIND_ENGAGEMENT:
                want_yes = activity.key in manual_keys
                for o in activity.outcomes:
                    if o.when is want_yes:
                        return o
                return activity.outcomes[-1] if activity.outcomes else None
            if activity.kind == KIND_RANDOM:
                chosen = activity.outcome(random_choices[activity.key]) if activity.key in random_choices else None
                return chosen or (activity.outcomes[0] if activity.outcomes else None)
            return None

        state = walker.start()
        walker.run(state, lookup, choose)
        return list(state.steps)

    # 5 ---------------------------------------------------------------------------------
    def check_coverage(self) -> CheckResult:
        details: list[str] = []
        if self.coverage.stats.get("classes_incomplete"):
            details.append("non tutte le combinazioni di valori a livello di contatto sono state esaminate, quindi l'elenco dei "
                           "branch raggiungibili è incompleto (gli split leggono troppi campi a livello di contatto)")
        if self.coverage.stats.get("exploration_truncated"):
            details.append("il journey ha più percorsi distinti di quanti il generatore ne esplori, quindi l'elenco dei branch "
                           "raggiungibili è incompleto")
        cases = {c.id: c for c in self.coverage.cases}
        crossed_by = {c.id: _crossed_branches(c) for c in self.coverage.cases}
        crossed: set[tuple[str, str]] = set().union(*crossed_by.values())
        # Every branch of every split of the journey has a status, and nothing else has one.
        journey_branches = {(a.key, o.key) for a, o in graph.split_branches(self.package.journey)}
        listed = [(b.activity_key, b.outcome_key) for b in self.coverage.branches]
        for ref in sorted(journey_branches - set(listed)):
            details.append(f"il branch {ref} del journey non ha uno stato nel report")
        for ref in sorted(set(listed) - journey_branches):
            details.append(f"il branch {ref} ha uno stato ma non è un branch di uno split del journey")
        for ref in sorted(r for r in listed if listed.count(r) > 1):
            details.append(f"il branch {ref} ha più stati")
        splits = {a.key for a in self.package.journey.activities.values() if graph.is_split(a)}
        for ref in sorted(r for r in crossed if r[0] in splits and r not in journey_branches):
            details.append(f"un caso attraversa {ref}, che non è un branch del journey")
        for b in self.coverage.branches:
            ref = (b.activity_key, b.outcome_key)
            if b.status == STATUS_NOT_COVERED:
                if not b.reason:
                    details.append(f"il branch {ref} non è coperto e non ha un motivo")
                # Spec: with no real inbox the open/click cases still exist, but the branch is
                # reported as not covered; such a branch names its cases in covered_by.
                if ref in crossed and not b.covered_by:
                    details.append(f"il branch {ref} risulta non coperto ma un caso lo attraversa")
            else:
                if not b.covered_by:
                    details.append(f"il branch {ref} risulta {_status(b.status)} ma nessun caso lo copre")
                if ref not in crossed:
                    details.append(f"il branch {ref} risulta {_status(b.status)} ma il percorso di nessun caso lo attraversa")
            for cid in b.covered_by:
                if cid not in cases:
                    details.append(f"il branch {ref} cita il caso sconosciuto {cid}")
                elif ref not in crossed_by[cid]:
                    details.append(f"il branch {ref} cita il caso {cid} tra quelli che lo coprono, ma il percorso di {cid} non lo attraversa")
        # Every case has its contacts in the files (spec: "covered by at least one case of 3 contacts").
        entry_contacts = self.rows_of_contact.get(self.table_model.entry_table, {})
        for c in self.coverage.cases:
            expected = self.settings.contacts_per_random_case if c.random is not None else self.settings.contacts_per_case
            if c.contact_count != expected:
                details.append(f"il caso {c.id} ha {c.contact_count} contatti invece di {expected}")
            if len(c.contact_keys) != c.contact_count:
                details.append(f"il caso {c.id} elenca {len(c.contact_keys)} chiavi dei contatti per {c.contact_count} contatti")
            absent = [key for key in c.contact_keys if key not in entry_contacts]
            if absent:
                details.append(f"caso {c.id}: {len(absent)} dei suoi {len(c.contact_keys)} contatti non hanno righe in "
                               f"{self.table_model.entry_table}.csv: " + ", ".join(absent[:3]))
        return CheckResult("Ogni branch raggiungibile è coperto e ogni branch non coperto ha un motivo", not details, _trim(details))

    # addresses -------------------------------------------------------------------------
    def check_addresses(self) -> CheckResult:
        details: list[str] = []
        real = self.settings.real_inbox.strip()
        for name in self.table_model.contact_tables:
            table = self.package.tables.get(name)
            if table is None or name not in self.csv:
                continue
            ckey = self._contact_key_field(name)
            email_fields = [f.name for f in table.fields if f.type == "EmailAddress"]
            if ckey is None or not email_fields:
                continue
            by_key = {c.key: c for c in self.data.contacts}
            cases = {c.id: c for c in self.coverage.cases}
            scenarios = {s.id: s for s in self.coverage.scenarios}
            _, rows = self.csv[name]
            for row in rows:
                contact = by_key.get(row.get(ckey))
                if contact is None:
                    details.append(f"{name}: la riga di {row.get(ckey)!r} non è di un contatto generato, quindi il suo "
                                   f"indirizzo non può essere corretto")
                    continue
                case = cases.get(contact.case_id)
                scenario = scenarios.get(contact.scenario_id)
                # A split that reads an address field fixes its value (data rule 2 over rule 4); the entry
                # query may copy that value into another address column.
                pins: dict[tuple[str, str], Value] = {}
                for values in ((case.contact_values if case else {}), (scenario.values if scenario else {})):
                    for (t, f), val in values.items():
                        if self.schema.field_type((t, f)) == "EmailAddress" and val != self.coverage.other_values.get((t, f)):
                            pins[(t.lower(), f.lower())] = val
                for ef in email_fields:
                    v = row.get(ef) or ""
                    if (name.lower(), ef.lower()) in pins:
                        pinned = pins[(name.lower(), ef.lower())]
                        if (row.get(ef) or None) != pinned:
                            details.append(f"{name}: {contact.key} deve avere {ef} = {pinned!r} per seguire il suo percorso, ma ha {v!r}")
                        continue
                    if pins and (row.get(ef) or None) in pins.values():
                        continue   # copied from the pinned column by the entry query
                    if contact.uses_real_inbox:
                        if not real or not (v.endswith("@" + real.split("@", 1)[1]) and f"+{contact.key}@" in v):
                            details.append(f"{name}: {contact.key} deve usare l'inbox reale ma ha {v!r}")
                    elif not v.endswith("@example.invalid"):
                        details.append(f"{name}: {contact.key} dovrebbe avere un indirizzo non recapitabile ma ha {v!r}")
        return CheckResult("Solo i contatti con un'azione manuale usano l'inbox reale, gli altri hanno indirizzi non recapitabili", not details, _trim(details))


class _Skip:
    pass


_SKIP = _Skip()


def _has_unsupported(expr) -> bool:
    """True when a select expression is, or contains, an expression the generator does not understand."""
    if isinstance(expr, SelectConcat):
        return any(_has_unsupported(part) for part in expr.parts)
    return isinstance(expr, SelectUnsupported)


def _unsupported_text(expr) -> str:
    """The text of the first expression the generator does not understand inside ``expr``."""
    if isinstance(expr, SelectConcat):
        return next(_unsupported_text(part) for part in expr.parts if _has_unsupported(part))
    return expr.text


def _sql_equal(value: Value, literal: str) -> bool:
    """SQL Server compares case-insensitively under the default collation; booleans accept true/1."""
    if value is None:
        return False
    a, b = value.strip().lower(), literal.strip().lower()
    truthy = {"true": "1", "false": "0"}
    return a == b or truthy.get(a, a) == truthy.get(b, b)


def _normalise_default(f, default: str) -> Value:
    if f.type == "Boolean":
        low = default.strip().lower()
        if low in ("true", "1"):
            return "True"
        if low in ("false", "0"):
            return "False"
    return default


def _crossed_branches(case: Case) -> set[tuple[str, str]]:
    """The branches (activity, outcome) a case's path crosses, the branches after its random split included."""
    crossed = {(s.activity_key, s.outcome_key) for s in case.steps if s.outcome_key is not None}
    if case.random is not None:
        for branch in case.random.branches:
            crossed.add((case.random.activity_key, branch.outcome_key))
            crossed.update((s.activity_key, s.outcome_key) for s in branch.steps if s.outcome_key is not None)
    return crossed


def _status(status: str) -> str:
    return _STATUS_WORDS.get(status, status)


def _describe(steps: list[Step]) -> str:
    return " > ".join(f"{s.activity_key}/{s.outcome_key}" if s.outcome_key else s.activity_key for s in steps)


def check_determinism(first: dict[str, bytes], second: dict[str, bytes]) -> CheckResult:
    """Spec: the same JSON and settings give the same data (the generation time is held fixed)."""
    details: list[str] = []
    for name in sorted(set(first) | set(second)):
        if name not in first or name not in second:
            details.append(f"{name} è prodotto da uno solo dei due run")
        elif first[name] != second[name]:
            details.append(f"{name} è diverso tra due run con lo stesso input")
    return CheckResult("Due run con lo stesso input danno gli stessi file", not details, details)


def run_checks(package: Package, table_model: TableModel, coverage: CoverageResult, data: GeneratedData,
               files: dict[str, bytes], settings: Settings, generation_time: datetime) -> list[CheckResult]:
    return Checker(package, table_model, coverage, data, files, settings, generation_time).run_all()
