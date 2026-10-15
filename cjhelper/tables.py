"""Table selection and field levels: ``build_table_model(package) -> TableModel``.

Spec rules implemented here (docs/spec.md):

- "Table selection": "A table gets a CSV when something reads it; tables that are only
  written to are skipped." The roles, as the spec defines them:

  - entry table: "The table bound to the journey's entry trigger";
  - split table: "Any table named in a split condition or in a wait-by-attribute";
  - source table: "Any table in the FROM or JOIN of a query that fills the entry table,
    followed back until no query writes it";
  - written-only table: "Target of an update contact step or of a query, never read by a
    split or by the entry query": no CSV.

- "Journey model and coverage", step 3: "A field is contact-level when its table has one row
  per contact (joined to the entry on the contact key). It is scenario-level when many contacts
  share one row (joined on any other field)."

- "Data generation rules", "Keys": a key is "identical in every table that joins on it", and
  scenario keys are "written into the field that joins contacts to the scenario-level table".
  The fields that must hold the same key are grouped in ``KeyGroup`` objects.

- "One scenario at a time": the activation field is "suggested from the entry query filters".

Rules of the spec section "Extensions beyond the pilot (agreed 12 October 2026)":

- "Row present or absent": a contact-level table other than the entry table, whose existence a split
  tests (IsNull or IsNotNull on its field linked to the contact key, or the supported attribute
  comparison), is listed in ``TableModel.existence_tested``. Never for the tables the entry queries
  read (the entry query and the queries feeding its sources), nor for the entry table.
- "Table levels keep the rule of Classify fields by level (join field), not the cardinality
  declared in Contact Builder": levels still follow the key groups below.
- Entry source: an entry that is not fed by an automation (Run Once, recurring schedule, CloudPages
  form, API event) normally has no entry query; that is not reported. An automation entry whose
  automation is missing from the export is reported as such.
- Missing relationship: a table read through an attribute set that no relationship of the export names
  (``Package.missing_relationships``) is not linked to the contact; its reason says so.

Rules of the spec section "Extensions for time-dependent journeys (step 3, 14 October 2026)":

- "Tables reached through other tables": "Every table on the path from the contact to a read table is a
  **link table**: it gets a CSV, with the role 'link', because without its rows the journey cannot read the
  table at the end of the path." The path is ``Condition.path`` and ``Activity.wait_path`` (table names,
  nearest the contact first, filled by loader.py). "Levels keep the rule of Classify fields by level": the
  link tables only make the key groups reach the table at the end of the path. "When the export gives no
  path for a read table that no selected table joins, the generator looks for the shortest chain of
  relationships from the contact to it and uses it with a warning; with no chain at all, the earlier rule
  applies (one row shared by all contacts, assumption)."
- "Columns the entry query derives and columns it copies": "A column of the entry table copied as a plain
  column from a source table ... holds the same value as its source: the two fields are one variable ...
  The same holds through a chain of followed queries." Those groups are ``TableModel.aliases``; copies of
  key fields stay in the key groups.
- "A LEFT JOIN source still gets one row per contact (read as an inner join, as before); the report says
  so": the source keeps its CSV and its reason says so.
- "Synchronized data extensions": a managed table keeps its CSV; its reason says that it is synchronized from
  the CRM and that the CSV is imported like the others.
- A ``DateOffsetFilter`` of the entry query is never an activation field (only equality filters are).
- "Exit criteria and goals": the exit criteria the generator evaluates (``graph.evaluable_exits``) read their
  tables at the end of every wait, as a split does, so their tables count as split tables (decision of the
  team, 14 October 2026: they get a CSV, and their paths make link tables).

The SQL is read through ``sqlparse_lite.parse_query``; this module only looks at the parsed form.
Every decision is explained in ``TableRole.reason`` or in ``TableModel.warnings``, in plain words.
The parser warnings of the queries followed are repeated in ``TableModel.warnings``, prefixed
with the query name.

Readings of the spec that are not spelled out there (each is visible in a reason or a warning):

- ``ROLE_WRITTEN`` is the spec's "written-only" role: a table that is read keeps only its
  reading roles, and its reason says it is also written.
- ``ROLE_LINK`` in the same way: a link table that is also the entry, a split or a source table keeps
  those roles only, and its reason says it is on the path (so the pilot keeps its roles).
- The conditions of every outcome name their path, also the outcomes the walk never evaluates (behind an
  unsupported one): their tables are split tables and get a CSV, so the tables leading to them are link
  tables too.
- The chain of relationships inferred for a table no level reaches starts at any table holding a field of
  the contact key's group (a group-to-set link counts as "from the contact"); the relationships between two
  tables are followed both ways. Every table on the chain before the read table is on its path; those
  without a CSV become link tables.
- A selected table that is not joined to the entry is scenario-level: one row shared by all
  contacts (warning).
- A table joined to the entry on the contact key is contact-level even when it is also joined to
  a scenario-level table: the contact key link wins.
- A group-to-set relationship (the contact model pointing at a table) links a field that holds
  the contact key (``model.Relationship``), so that field joins the contact key's group.
- A plain column copied by a followed query (``SELECT M.ContactId`` into the entry table) holds the
  same value as its source on every row, so it carries the source's key, and the other way round.
- Contact key: when the entry table has no sendable field, no contact link and no primary key,
  its first field is used (warning).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from . import graph, sqlparse_lite
from .model import (
    ENTRY_AUTOMATION,
    KIND_DECISION,
    KIND_WAIT_ATTRIBUTE,
    LEVEL_CONTACT,
    LEVEL_SCENARIO,
    ROLE_ENTRY,
    ROLE_LINK,
    ROLE_SOURCE,
    ROLE_SPLIT,
    ROLE_WRITTEN,
    SOURCE_JOURNEY,
    ColumnRef,
    Condition,
    EqualityFilter,
    FieldRef,
    KeyGroup,
    Package,
    ParsedQuery,
    Query,
    SelectColumn,
    Table,
    TableModel,
    TableRef,
    TableRole,
    formula_conditions,
)

# KeyGroup.kind values.
GROUP_CONTACT = "contact"
GROUP_SCENARIO = "scenario"
GROUP_OTHER = "other"

# A table with one of these roles is read by the journey, so it gets a CSV.
READING_ROLES = frozenset({ROLE_ENTRY, ROLE_SPLIT, ROLE_SOURCE, ROLE_LINK})

# Who reads a table directly: a decision split, a wait by attribute, an exit criterion of the journey.
READ_SPLIT = "split"
READ_WAIT = "wait"
READ_EXIT = "exit"
# Up to this many readers are named by key in a reason; above it they are counted by kind.
READERS_NAMED = 5

NO_ROLE_REASON = "Né letta né scritta da questo journey."
INFERRED_PATH = "percorso dedotto dalle relazioni dell'export, non indicato dallo split"
LEFT_JOIN_NOTE = "Unita con LEFT JOIN: le righe generate la trattano come inner join."
MANAGED_NOTE = ("Data extension sincronizzata dal CRM: il CSV si importa come gli altri e le righe arriveranno "
                "anche dalla sincronizzazione.")


def build_table_model(package: Package) -> TableModel:
    """Apply the table selection rule, group the key fields and classify tables and fields by level.

    Steps, in order:

    1. the entry table and the query that fills it (the entry query);
    2. who reads each table (splits, waits by attribute, the queries followed back from the entry
       query), who writes it (update contact steps, queries) and which tables are on the path from the
       contact to a table a split or a wait reads (step 3, link tables);
    3. roles and selection: the entry, split, source and link tables get a CSV, the others do not;
    4. the contact key field, then the key groups;
    5. the tables no level reaches get the shortest chain of relationships from the contact, when there
       is one: its tables become link tables, and the roles and the selection are made again with them;
    6. the level of each selected table, then the kind of each key group, then the level of each field;
    7. the activation field suggested from the entry query filters, the tables whose existence a split
       tests, the columns the followed queries copy (aliases).
    """
    warnings: list[str] = []
    names = _Names(package.tables)
    entry = _entry_table(package, names)

    automation_queries = _entry_automation_queries(package, warnings)
    parse = _Parser(package, warnings)
    entry_queries = _find_entry_queries(package, entry, automation_queries, names, warnings)
    entry_query = _choose_entry_query(entry_queries, entry, parse, names, warnings)
    entry_parsed = parse(entry_query) if entry_query else None

    uses = {name: _Use() for name in package.tables}
    _record_split_reads(package, names, uses, warnings)
    source_queries = _follow_sources(package, entry, entry_query, entry_queries, parse, automation_queries,
                                     names, uses, warnings)
    _record_writes(package, names, uses)

    order = _table_order(package.tables, entry)
    roles, selected = _selection(order, entry, uses)

    contact_key = _contact_key_field(package, entry, names, warnings)
    keys = _link_keys(contact_key, package, source_queries, names, warnings)
    unlinked = {name for name in (names.table(t) for t in package.missing_relationships) if name}
    chains = _inferred_links(package, names, keys, contact_key, entry, order, selected, unlinked)
    if chains:
        _record_inferred_links(chains, uses, warnings)
        roles, selected = _selection(order, entry, uses)

    levels, level_reasons = _table_levels(entry, selected, package.tables, keys, contact_key, warnings, unlinked)
    key_groups = _key_groups(keys, contact_key, levels, entry, selected, package.tables)
    field_levels = _field_levels(selected, package.tables, levels, key_groups, entry, entry_parsed, names)
    activation, activation_note = _activation_field(entry_parsed, levels, names, warnings)
    existence, existence_splits = _existence_tested(package, entry, roles, levels, keys, contact_key, names)
    aliases = _aliases(source_queries, names, key_groups, selected, order, package.tables)

    entry_query_name = entry_query.name if entry_query else None
    table_roles: dict[str, TableRole] = {}
    for name in order:
        is_selected = name in selected
        if is_selected:
            notes = [
                LEFT_JOIN_NOTE if ROLE_SOURCE in roles[name] and uses[name].left_joins else "",
                MANAGED_NOTE if package.tables[name].managed else "",
                activation_note if activation and activation[0] == name else "",
                _existence_note(existence[name], existence_splits[name]) if name in existence else "",
            ]
            reason = _selected_reason(roles[name], uses[name], entry_query_name,
                                      levels[name], level_reasons[name], notes)
        elif ROLE_WRITTEN in roles[name]:
            reason = f"Scritta {_writers_text(uses[name])}, mai letta da uno split o dalla entry query: nessun CSV."
        else:
            reason = NO_ROLE_REASON
        table_roles[name] = TableRole(name=name, roles=roles[name], selected=is_selected,
                                      level=levels.get(name), reason=reason)

    return TableModel(
        entry_table=entry,
        contact_key_field=contact_key,
        tables=table_roles,
        key_groups=key_groups,
        field_levels=field_levels,
        entry_query=entry_parsed,
        source_queries=source_queries,
        scenario_tables=[name for name in selected if levels[name] == LEVEL_SCENARIO],
        contact_tables=[name for name in selected if levels[name] == LEVEL_CONTACT],
        activation_field=activation,
        warnings=list(dict.fromkeys(warnings)),
        existence_tested=existence,
        aliases=aliases,
    )


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

class _Names:
    """Case-insensitive lookup of tables and fields.

    SQL, split criteria and schemas do not always agree on letter case (the pilot's entry query
    writes ``C.active`` for the schema's ``ACTIVE``). The model keeps the schema spelling.
    """

    def __init__(self, tables: dict[str, Table]):
        self.tables = tables
        self._by_lower = {name.lower(): name for name in tables}

    def table(self, name: Optional[str]) -> Optional[str]:
        """The schema spelling of a table name, or None when the export has no such table."""
        if not name:
            return None
        return self._by_lower.get(name.lower())

    def field_ref(self, table: Optional[str], field_name: Optional[str]) -> Optional[FieldRef]:
        """``(table, field)`` in the schema spelling, or None when either is unknown."""
        table_name = self.table(table)
        if table_name is None or not field_name:
            return None
        found = self.tables[table_name].field(field_name)
        return (table_name, found.name) if found else None

    def why_unknown(self, table: Optional[str], field_name: Optional[str]) -> str:
        """Plain words saying why ``field_ref`` found nothing."""
        if not table:
            return f"la tabella della colonna {field_name} non è nota"
        if self.table(table) is None:
            return f"la tabella {table} non è nell'export"
        return f"{self.table(table)} non ha il campo {field_name}"


def _table_order(tables: dict[str, Table], entry: str) -> list[str]:
    """Entry table first, then the others by name: a stable order for the report and the CSVs."""
    others = sorted((name for name in tables if name != entry), key=lambda name: (name.lower(), name))
    return [entry] + others


# ---------------------------------------------------------------------------
# Entry table and entry query
# ---------------------------------------------------------------------------

def _entry_table(package: Package, names: _Names) -> str:
    """Spec, entry table: "The table bound to the journey's entry trigger" (``Journey.entry_table``).

    loader.py refuses an export without one, so a missing entry table here is a programming error.
    """
    entry = names.table(package.journey.entry_table)
    if entry is None:
        raise ValueError(f"la tabella di ingresso {package.journey.entry_table!r} del journey "
                         "non è tra le tabelle dell'export")
    return entry


def _entry_automation_queries(package: Package, warnings: list[str]) -> list[Query]:
    """The queries run by the automation that feeds the entry event, in step order."""
    automation = package.entry_event.automation_name if package.entry_event else None
    if not automation:
        return []
    by_name: dict[str, Query] = {}
    for query in package.queries:
        by_name.setdefault(query.name, query)
    out: list[Query] = []
    for name in package.automation_queries.get(automation, []):
        query = by_name.get(name)
        if query is None:
            warnings.append(f"L'automation di ingresso {automation} esegue la query {name}, che non è nell'export")
        elif query not in out:
            out.append(query)
    return out


def _find_entry_queries(package: Package, entry: str, automation_queries: list[Query], names: _Names,
                        warnings: list[str]) -> list[Query]:
    """Every query that fills the entry table (contract in docs/plan.md, "tables.py").

    Spec, source table: "Any table in the FROM or JOIN of a query that fills the entry table", so
    all of them count, not only the last. They are the queries of the entry automation whose target
    is the entry table, in step order. When the automation has none, every query of the export that
    fills the entry table is taken (warning). When no query fills the entry table there is no entry
    query (warning), and the entry table alone is the source side.
    """
    in_automation = [q for q in automation_queries if names.table(q.target_table) == entry]
    if in_automation:
        return in_automation

    event = package.entry_event
    fed_by_automation = event is not None and event.kind == ENTRY_AUTOMATION
    if event is None:
        why = "Il journey non ha un entry event"
    elif not fed_by_automation:
        why = f"L'entry event {event.name} non è alimentato da un'automation"
    elif not event.automation_name:
        why = f"L'automation che alimenta l'entry event {event.name} non è nell'export"
    else:
        why = (f"Nessuna query dell'automation di ingresso {event.automation_name} popola la tabella "
               f"di ingresso {entry}")
    elsewhere = [q for q in package.queries if names.table(q.target_table) == entry]
    if not elsewhere:
        # An entry that no automation feeds (Run Once, recurring schedule, form, API event) is filled by the
        # import itself: having no entry query is the normal case there, not something to report.
        if event is None or fed_by_automation:
            warnings.append(f"{why}, e nessun'altra query dell'export popola {entry}: non c'è una entry query, "
                            "quindi l'unica sorgente è la tabella di ingresso")
    elif len(elsewhere) == 1:
        warnings.append(f"{why}; la query {elsewhere[0].name} popola {entry} e viene presa come entry query")
    else:
        warnings.append(f"{why}; le query dell'export che popolano {entry} vengono prese come entry query")
    return elsewhere


def _choose_entry_query(entry_queries: list[Query], entry: str, parse: "_Parser", names: _Names,
                        warnings: list[str]) -> Optional[Query]:
    """The one entry query that builds the entry rows: the last query in step order.

    When several queries fill the entry table (warning), the tables read by all of them are source
    tables, but the entry rows come from one query. A last query that reads only the entry table
    (a post-processing update of the entry rows) is not the one that builds them: the last query that
    reads some other table is taken instead.
    """
    if not entry_queries:
        return None
    chosen = entry_queries[-1]
    if len(entry_queries) == 1:
        return chosen
    reading_others = [q for q in entry_queries if any(names.table(ref.table) != entry for ref in parse(q).sources)]
    if reading_others:
        chosen = reading_others[-1]
    how = "l'ultima nell'ordine degli step" if chosen is entry_queries[-1] else \
        f"l'ultima che legge una tabella diversa da {entry}"
    warnings.append(f"Più query popolano la tabella di ingresso {entry} "
                    f"({_join_words([q.name for q in entry_queries])}): "
                    f"le tabelle che leggono sono tutte tabelle sorgente, e le righe di ingresso sono costruite da "
                    f"{chosen.name}, {how}")
    return chosen


class _Parser:
    """Parse each query once; its parser warnings are repeated in the table model, prefixed with the
    query name."""

    def __init__(self, package: Package, warnings: list[str]):
        self.package = package
        self.warnings = warnings
        self._done: dict[str, ParsedQuery] = {}

    def __call__(self, query: Query) -> ParsedQuery:
        if query.name not in self._done:
            self._done[query.name] = _parse(query, self.package, self.warnings)
        return self._done[query.name]


def _parse(query: Query, package: Package, warnings: list[str]) -> ParsedQuery:
    """Parse one query; its parser warnings are repeated here, prefixed with the query name."""
    parsed = sqlparse_lite.parse_query(query, package.tables)
    prefix = f"Query {query.name}: "
    warnings.extend(w if w.startswith(prefix) else prefix + w for w in parsed.warnings)
    return parsed


# ---------------------------------------------------------------------------
# Who reads and who writes each table
# ---------------------------------------------------------------------------

@dataclass
class _Use:
    """Who reads and who writes one table: the facts behind its roles and its reason."""
    splits: list[str] = field(default_factory=list)              # decision splits naming it in a condition
    waits: list[str] = field(default_factory=list)               # waits by attribute on one of its fields
    exits: list[str] = field(default_factory=list)               # evaluable exit criteria naming it
    read_by_query: Optional[str] = None                          # the followed query that first read it
    query_fills: Optional[str] = None                            # the table that query fills
    update_steps: list[str] = field(default_factory=list)        # update contact steps writing it
    written_by_queries: list[str] = field(default_factory=list)  # queries whose target it is
    # On the path from the contact to these read tables: read table -> who reads it that way, as
    # (READ_SPLIT / READ_WAIT / READ_EXIT, activity or exit criterion key), in order of first read.
    link_for: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    inferred_links: set[str] = field(default_factory=set)        # read tables of link_for whose path was inferred
    left_joins: list[str] = field(default_factory=list)          # followed queries reading it with a LEFT JOIN

    def readers(self) -> list[tuple[str, str]]:
        """Who reads this table directly, as in ``link_for``: splits, then waits, then exit criteria."""
        return ([(READ_SPLIT, key) for key in self.splits] + [(READ_WAIT, key) for key in self.waits]
                + [(READ_EXIT, key) for key in self.exits])

    def read_list(self, kind: str) -> list[str]:
        return {READ_SPLIT: self.splits, READ_WAIT: self.waits, READ_EXIT: self.exits}[kind]


def _record_split_reads(package: Package, names: _Names, uses: dict[str, _Use], warnings: list[str]) -> None:
    """Spec, split table: "Any table named in a split condition or in a wait-by-attribute".

    The tables come from every condition of every decision outcome formula and from the date
    field of every wait-by-attribute. A table or field that is not in the export is reported.

    Spec, step 3, "Exit criteria and goals": the exit criteria the generator evaluates
    (``graph.evaluable_exits``) read their conditions at the end of every wait, as a split does, so the
    tables they name count as split tables (team decision); the others are not modelled and read nothing.

    Spec, step 3, "Tables reached through other tables": "Every table on the path from the contact to a
    read table is a link table". The path of a condition is ``Condition.path`` (Journey Data reads the
    entry row: no path), the path of a wait ``Activity.wait_path``; each of its tables records a link use
    (``_Use.link_for``: the read table and its reader). A path table that is not in the export is reported.
    """
    reads: list[tuple[str, str, str, str, tuple[str, ...]]] = []   # (reader kind, key, table, field, path)

    def read_condition(kind: str, key: str, condition: Condition) -> None:
        if condition.source == SOURCE_JOURNEY:
            if names.table(condition.table) is not None:   # Journey Data of another entry event names no table
                reads.append((kind, key, condition.table, condition.field, ()))
        else:
            reads.append((kind, key, condition.table, condition.field, condition.path))

    for activity in package.journey.activities.values():
        for outcome in activity.outcomes:
            for condition in formula_conditions(outcome.formula):
                read_condition(READ_SPLIT, activity.key, condition)
        if activity.kind == KIND_WAIT_ATTRIBUTE and activity.wait_field:
            reads.append((READ_WAIT, activity.key, activity.wait_field[0], activity.wait_field[1],
                          activity.wait_path))
    for exit_ in graph.evaluable_exits(package.journey):
        for condition in formula_conditions(exit_.formula):
            read_condition(READ_EXIT, exit_.key, condition)

    unknown_tables: dict[str, list[str]] = {}
    unknown_fields: dict[str, list[str]] = {}
    unknown_links: dict[tuple[str, str], list[str]] = {}
    for kind, key, table_name, field_name, path in reads:
        table = names.table(table_name)
        if table is None:
            _add_once(unknown_tables.setdefault(table_name, []), key)
            continue
        _add_once(uses[table].read_list(kind), key)
        if names.field_ref(table, field_name) is None:
            _add_once(unknown_fields.setdefault(f"{table}.{field_name}", []), key)
        for link_name in path:
            link = names.table(link_name)
            if link is None:
                _add_once(unknown_links.setdefault((link_name, table), []), key)
            elif link != table:
                _add_once(uses[link].link_for.setdefault(table, []), (kind, key))
    for table_name, keys in unknown_tables.items():
        warnings.append(f"La tabella {table_name} è letta da {_short_list(keys)} ma non è nell'export: "
                        "non è possibile crearne il CSV")
    for ref_text, keys in unknown_fields.items():
        warnings.append(f"{ref_text} è letto da {_short_list(keys)} ma non è un campo dell'export")
    for (link_name, table), keys in unknown_links.items():
        warnings.append(f"La tabella {link_name}, sul percorso tra il contatto e {table} letta da {_short_list(keys)}, "
                        "non è nell'export: non è possibile crearne il CSV")


def _follow_sources(package: Package, entry: str, entry_query: Optional[Query], entry_queries: list[Query],
                    parse: _Parser, automation_queries: list[Query], names: _Names, uses: dict[str, _Use],
                    warnings: list[str]) -> list[ParsedQuery]:
    """Spec, source table: "Any table in the FROM or JOIN of a query that fills the entry table,
    followed back until no query writes it".

    Breadth-first from the FROM/JOIN tables of every query that fills the entry table (the entry
    query first, then the others in step order). For each new source table, every query whose
    target it is gets parsed and its own FROM/JOIN tables are followed in turn: the queries of the
    entry automation first (step order), then the other queries of the export.
    Each table and each query is followed once, and the walk starts at the entry table, which is
    therefore never a source: cycles stop (the pilot has a query that writes its Config table from
    Config and the entry table).

    Returns the parsed queries followed, the entry query first.
    """
    if entry_query is None:
        return []
    search_order = automation_queries + [q for q in package.queries if q not in automation_queries]
    starting = [entry_query] + [q for q in entry_queries if q is not entry_query]
    followed = [parse(q) for q in starting]
    done_queries = {q.name for q in starting}
    done_tables = {entry}
    pending: deque[tuple[TableRef, ParsedQuery]] = deque((ref, parsed) for parsed in followed for ref in parsed.sources)
    while pending:
        ref, reader = pending.popleft()
        table = names.table(ref.table)
        if table is None:
            warnings.append(f"Query {reader.name}: la tabella {ref.table}, letta dalla query, non è nell'export, "
                            "quindi non è possibile crearne il CSV")
            continue
        if ref.optional and table != entry:
            _add_once(uses[table].left_joins, reader.name)   # spec, step 3: "A LEFT JOIN source ... the report says so"
        if table in done_tables:
            continue
        done_tables.add(table)
        uses[table].read_by_query = reader.name
        uses[table].query_fills = names.table(reader.target_table) or reader.target_table
        for query in search_order:
            if query.name in done_queries or names.table(query.target_table) != table:
                continue
            done_queries.add(query.name)
            parsed = parse(query)
            followed.append(parsed)
            pending.extend((source, parsed) for source in parsed.sources)
    return followed


def _record_writes(package: Package, names: _Names, uses: dict[str, _Use]) -> None:
    """Who writes each table: update contact steps (``Activity.writes``) and queries (their target).

    A write to a table that is not in the export is left out: loader.py already reports the
    unresolved reference, and a table that is only written gets no CSV anyway.
    """
    for activity in package.journey.activities.values():
        for write in activity.writes:
            table = names.table(write.table)
            if table is not None:
                _add_once(uses[table].update_steps, activity.key)
    for query in package.queries:
        table = names.table(query.target_table)
        if table is not None:
            _add_once(uses[table].written_by_queries, query.name)


def _roles(name: str, entry: str, use: _Use) -> set[str]:
    """Spec "Table selection": entry, split and source tables are read; a table that is only
    written is the spec's "written-only table" (``ROLE_WRITTEN``). A table that is read and also
    written keeps its reading roles only; its reason says it is also written.

    Spec, step 3, "Tables reached through other tables": a table on the path from the contact to a read
    table is a link table (``ROLE_LINK``), which gets a CSV. A link table that is also the entry, a split or
    a source table keeps those roles only; its reason says it is on the path. A table read by an evaluable
    exit criterion is a split table (team decision, see ``_record_split_reads``)."""
    roles: set[str] = set()
    if name == entry:
        roles.add(ROLE_ENTRY)
    if use.splits or use.waits or use.exits:
        roles.add(ROLE_SPLIT)
    if use.read_by_query:
        roles.add(ROLE_SOURCE)
    if not roles and use.link_for:
        roles.add(ROLE_LINK)
    if not roles and (use.update_steps or use.written_by_queries):
        roles.add(ROLE_WRITTEN)
    return roles


def _selection(order: list[str], entry: str, uses: dict[str, _Use]) -> tuple[dict[str, set[str]], list[str]]:
    """The roles of every table and the tables that get a CSV (a reading role), in ``order``."""
    roles = {name: _roles(name, entry, uses[name]) for name in order}
    return roles, [name for name in order if roles[name] & READING_ROLES]


# ---------------------------------------------------------------------------
# Contact key and key groups
# ---------------------------------------------------------------------------

def _contact_key_field(package: Package, entry: str, names: _Names, warnings: list[str]) -> FieldRef:
    """The entry table's field that holds the contact key (spec "Keys": contact keys TST_C0001...).

    In this order (contract in docs/plan.md): the entry table's sendable field
    (``sendableCustomObjectField``); the field a group-to-set relationship links to the contact on
    the entry table; a single-field primary key; the first primary key field. Every fallback is
    reported. With no primary key at all the first field is used (assumption, reported).
    """
    table = package.tables[entry]
    if table.sendable_field:
        found = table.field(table.sendable_field)
        if found:
            return (entry, found.name)
        warnings.append(f"Il campo sendable {table.sendable_field} della tabella di ingresso {entry} non è nel "
                        f"suo schema")
    for rel in package.relationships:
        if rel.left_table is None and names.table(rel.right_table) == entry:
            found = table.field(rel.right_field)
            if found:
                warnings.append(f"La tabella di ingresso {entry} non ha un campo sendable utilizzabile: la chiave del "
                                f"contatto è {found.name}, il campo che la relazione {rel.name} collega al contatto")
                return (entry, found.name)
    primary_key = table.primary_key
    if len(primary_key) == 1:
        warnings.append(f"La tabella di ingresso {entry} non ha un campo sendable utilizzabile né un collegamento al "
                        f"contatto: la chiave del contatto è la sua chiave primaria {primary_key[0]}")
        return (entry, primary_key[0])
    if primary_key:
        warnings.append(f"La tabella di ingresso {entry} non ha un campo sendable utilizzabile né un collegamento al "
                        f"contatto, e la sua chiave primaria ha più campi ({_join_words(primary_key)}): come chiave "
                        f"del contatto si prende il primo, {primary_key[0]}")
        return (entry, primary_key[0])
    if table.fields:
        warnings.append(f"La tabella di ingresso {entry} non ha un campo sendable utilizzabile, né un collegamento al "
                        f"contatto, né una chiave primaria: come chiave del contatto si prende il suo primo campo, "
                        f"{table.fields[0].name} (assunzione)")
        return (entry, table.fields[0].name)
    raise ValueError(f"la tabella di ingresso {entry} non ha campi")


class _KeyUnion:
    """Union-find over fields: fields linked by an equality end in the same group."""

    def __init__(self) -> None:
        self._parent: dict[FieldRef, FieldRef] = {}

    def __contains__(self, ref: FieldRef) -> bool:
        return ref in self._parent

    def add(self, ref: FieldRef) -> None:
        self._parent.setdefault(ref, ref)

    def find(self, ref: FieldRef) -> FieldRef:
        while self._parent[ref] != ref:
            ref = self._parent[ref]
        return ref

    def union(self, a: FieldRef, b: FieldRef) -> None:
        self.add(a)
        self.add(b)
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a

    def same(self, a: FieldRef, b: FieldRef) -> bool:
        return a in self and b in self and self.find(a) == self.find(b)

    def groups(self) -> list[list[FieldRef]]:
        """Every group, its members in the order they were first linked."""
        by_root: dict[FieldRef, list[FieldRef]] = {}
        for ref in self._parent:
            by_root.setdefault(self.find(ref), []).append(ref)
        return list(by_root.values())


def _link_keys(contact_key: FieldRef, package: Package, source_queries: list[ParsedQuery], names: _Names,
               warnings: list[str]) -> _KeyUnion:
    """Group the fields that must hold the same generated key (spec "Keys": a key is "identical in
    every table that joins on it").

    Links, in this order:

    1. the join equalities of the followed queries (``JOIN ... ON a = b``) and the relationships
       between two tables (contract in docs/plan.md);
    2. the contact model's own links (group-to-set relationships): ``model.Relationship`` says their
       field holds the contact key, so it joins the contact key's group;
    3. the plain columns the followed queries copy (``SELECT M.ContactId`` into the entry table): the
       copy holds the same value as its source on every row, so it joins the source's key group, or
       the source joins the copy's. A copy where neither side is a key is left out, so ordinary
       copied columns (email, phone) never become keys.

    The contact key field is always in a group, even when nothing joins on it.
    """
    keys = _KeyUnion()
    keys.add(contact_key)
    for left, right in _join_links(package, source_queries, names, warnings):
        if left != right:   # a field equal to itself links nothing
            keys.union(left, right)
    for ref in _contact_links(package, names, warnings):
        keys.union(contact_key, ref)
    copies = _select_copies(source_queries, names)
    changed = True
    while changed:   # repeated so that keys also flow through a chain of queries
        changed = False
        for source, target in copies:
            if (source in keys or target in keys) and not keys.same(source, target):
                keys.union(source, target)
                changed = True
    return keys


def _join_links(package: Package, source_queries: list[ParsedQuery], names: _Names,
                warnings: list[str]) -> list[tuple[FieldRef, FieldRef]]:
    """Join equalities of the followed queries, then relationships between two tables, both sides in
    the schema spelling. A side that cannot be resolved skips that link, with a warning."""
    links: list[tuple[FieldRef, FieldRef]] = []
    for parsed in source_queries:
        for join in parsed.joins:
            what = (f"Query {parsed.name}: il join {_column_text(join.left)} = {_column_text(join.right)} "
                    "viene ignorato")
            link = _resolve_link(names, (join.left.table, join.left.column),
                                 (join.right.table, join.right.column), what, warnings)
            if link:
                links.append(link)
    for rel in package.relationships:
        if rel.left_table is None:
            continue   # group-to-set: see _contact_links
        link = _resolve_link(names, (rel.left_table, rel.left_field), (rel.right_table, rel.right_field),
                             f"La relazione {rel.name} viene ignorata", warnings)
        if link:
            links.append(link)
    return links


def _resolve_link(names: _Names, left: tuple[Optional[str], Optional[str]], right: tuple[Optional[str], Optional[str]],
                  what: str, warnings: list[str]) -> Optional[tuple[FieldRef, FieldRef]]:
    left_ref = names.field_ref(*left)
    right_ref = names.field_ref(*right)
    if left_ref is None or right_ref is None:
        problem = names.why_unknown(*left) if left_ref is None else names.why_unknown(*right)
        warnings.append(f"{what}, perché {problem}")
        return None
    return left_ref, right_ref


def _contact_links(package: Package, names: _Names, warnings: list[str]) -> list[FieldRef]:
    """Fields the contact model links to the contact (group-to-set relationships, ``left_table`` None)."""
    out: list[FieldRef] = []
    for rel in package.relationships:
        if rel.left_table is not None:
            continue
        ref = names.field_ref(rel.right_table, rel.right_field)
        if ref is None:
            problem = names.why_unknown(rel.right_table, rel.right_field)
            warnings.append(f"La relazione {rel.name} viene ignorata, perché {problem}")
            continue
        out.append(ref)
    return out


def _select_copies(source_queries: list[ParsedQuery], names: _Names) -> list[tuple[FieldRef, FieldRef]]:
    """``(source column, target column)`` for every plain column in the SELECT of the followed queries.

    Columns the target table does not have (the pilot's entry query selects ``FirstName``, which the
    entry table lacks) and columns that cannot be resolved are left out.
    """
    out: list[tuple[FieldRef, FieldRef]] = []
    for parsed in source_queries:
        for item in parsed.select:
            if not isinstance(item.expr, SelectColumn):
                continue
            source = names.field_ref(item.expr.source.table, item.expr.source.column)
            target = names.field_ref(parsed.target_table, item.target_column)
            if source is not None and target is not None and source != target:
                out.append((source, target))
    return out


# ---------------------------------------------------------------------------
# Paths inferred from the relationships
# ---------------------------------------------------------------------------

def _inferred_links(package: Package, names: _Names, keys: _KeyUnion, contact_key: FieldRef, entry: str,
                    order: list[str], selected: list[str], unlinked: set[str]) -> dict[str, list[str]]:
    """Spec, step 3, "Tables reached through other tables": "When the export gives no path for a read table
    that no selected table joins, the generator looks for the shortest chain of relationships from the
    contact to it and uses it with a warning; with no chain at all, the earlier rule applies".

    The read tables are the selected tables that the level walk does not reach, other than those whose
    attribute set has no relationship in the export (``unlinked``: their reason says so already). The chain
    starts at any table holding a field of the contact key's group, which a group-to-set link puts there
    ("from the contact"); it follows the relationships between two tables, both ways (``model.Relationship``),
    breadth-first, the tables with a CSV first, so a shortest chain is found and ties keep the export order.

    Returns read table -> the tables of its chain between the contact and it, nearest the contact first.
    A read table with no chain is left out.
    """
    reached, _ = _reached_levels(entry, selected, package.tables, keys, contact_key)
    targets = [name for name in selected if name not in reached and name not in unlinked]
    if not targets:
        return {}
    contact_root = keys.find(contact_key)
    selected_set = set(selected)
    starts = [name for name in order
              if any(ref in keys and keys.find(ref) == contact_root
                     for ref in ((name, f.name) for f in package.tables[name].fields))]
    starts.sort(key=lambda name: name not in selected_set)   # stable: the tables with a CSV first
    neighbours = _relationship_neighbours(package, names)
    parent: dict[str, Optional[str]] = {name: None for name in starts}
    queue = deque(starts)
    while queue:
        current = queue.popleft()
        for other in neighbours.get(current, []):
            if other not in parent:
                parent[other] = current
                queue.append(other)
    chains: dict[str, list[str]] = {}
    for target in targets:
        if target not in parent:
            continue
        chain: list[str] = []
        step = parent[target]
        while step is not None:
            chain.append(step)
            step = parent[step]
        chains[target] = chain[::-1]
    return chains


def _relationship_neighbours(package: Package, names: _Names) -> dict[str, list[str]]:
    """Table -> the tables a relationship of the export links it to, both ways, in export order. Group-to-set
    relationships (the contact) and relationships with a side that is not in the export are left out."""
    neighbours: dict[str, list[str]] = {}
    for rel in package.relationships:
        if rel.left_table is None:
            continue
        left = names.field_ref(rel.left_table, rel.left_field)
        right = names.field_ref(rel.right_table, rel.right_field)
        if left is None or right is None or left[0] == right[0]:
            continue
        _add_once(neighbours.setdefault(left[0], []), right[0])
        _add_once(neighbours.setdefault(right[0], []), left[0])
    return neighbours


def _record_inferred_links(chains: dict[str, list[str]], uses: dict[str, _Use], warnings: list[str]) -> None:
    """Every table of an inferred chain is on the path to its read table, as if the export named the path
    (the tables without a CSV become link tables); one warning per read table says the path was inferred."""
    for target, chain in chains.items():
        readers = uses[target].readers()
        for name in chain:
            link = uses[name].link_for.setdefault(target, [])
            for reader in readers:
                _add_once(link, reader)
            uses[name].inferred_links.add(target)
        warnings.append(f"La tabella {target} è raggiunta dal contatto attraverso {_join_words(chain)} "
                        f"({INFERRED_PATH})")


# ---------------------------------------------------------------------------
# Levels
# ---------------------------------------------------------------------------

def _table_levels(entry: str, selected: list[str], tables: dict[str, Table], keys: _KeyUnion,
                  contact_key: FieldRef, warnings: list[str],
                  unlinked: frozenset[str] | set[str] = frozenset()) -> tuple[dict[str, str], dict[str, str]]:
    """Spec step 3: "A field is contact-level when its table has one row per contact (joined to the
    entry on the contact key). It is scenario-level when many contacts share one row (joined on any
    other field)."

    - The entry table is contact-level.
    - A selected table holding a field of the contact key's group is joined to the entry on the
      contact key: contact-level. This link wins over any other, so a table joined on the contact key
      stays contact-level even when it is also joined to a scenario-level table (the pilot's Target is
      joined to Master on the contact key and to Config on the campaign name).
    - Breadth-first from the contact-level tables over the other key groups (two tables are neighbours
      when they hold fields of the same group): a table first reached this way is joined on another
      field, so it is scenario-level; the neighbours of a scenario-level table are scenario-level too.
    - A selected table never reached is not joined to the entry at all: scenario-level, with a warning
      (assumption: one row shared by all contacts). When its attribute set has no relationship in the
      export (``unlinked``, spec: "relationship missing in the export"), the reason and the warning say
      so instead: the splits reading it are unsupported, so its rows only fill the CSV.

    Only selected tables get a level, but the groups were built over every table, so a link that
    passes through a table without a CSV still counts. The link tables of the step 3 extension are
    selected, so the walk reaches the table at the end of their path (spec, step 3: "the link table joined
    on the contact key is contact-level, the table it points to through another field is scenario-level").

    Returns the level of each selected table and the plain-words reason for it.
    """
    levels, reasons = _reached_levels(entry, selected, tables, keys, contact_key)
    for name in selected:
        if name not in levels and name in unlinked:
            levels[name] = LEVEL_SCENARIO
            reasons[name] = ("il suo attribute set non ha alcuna relazione nell'export (relazione mancante "
                             "nell'export), quindi non si sa come è collegata al contatto: gli split che la leggono "
                             "sono non supportati")
            warnings.append(f"La tabella {name} è letta da uno split tramite un attribute set che non ha alcuna "
                            "relazione nell'export (relazione mancante nell'export): le condizioni su questa tabella "
                            "sono non supportate e le sue righe non sono collegate ai contatti")
        elif name not in levels:
            levels[name] = LEVEL_SCENARIO
            reasons[name] = ("non collegata alla tabella di ingresso: trattata come una riga condivisa da tutti i "
                             "contatti (assunzione)")
            warnings.append(f"La tabella {name} non è collegata alla tabella di ingresso: è trattata come una riga "
                            "condivisa da tutti i contatti (a livello di scenario). È un'assunzione: verifica come il "
                            "journey collega questa tabella")
    return levels, reasons


def _reached_levels(entry: str, selected: list[str], tables: dict[str, Table], keys: _KeyUnion,
                    contact_key: FieldRef) -> tuple[dict[str, str], dict[str, str]]:
    """The walk of ``_table_levels`` over the key groups: the level and its reason for every selected
    table it reaches. The tables it does not reach are missing from the result."""
    members = _group_members(keys, selected, tables)
    contact_root = keys.find(contact_key)
    levels = {entry: LEVEL_CONTACT}
    reasons = {entry: "è la tabella di ingresso, una riga per contatto"}
    for table, field_name in members.get(contact_root, {}).items():
        if table not in levels:
            levels[table] = LEVEL_CONTACT
            reasons[table] = f"collegata alla tabella di ingresso tramite la chiave del contatto ({table}.{field_name})"

    queue = deque(name for name in selected if name in levels)
    while queue:
        current = queue.popleft()
        for root, tables_in_group in members.items():
            if root == contact_root or current not in tables_in_group:
                continue
            for other, other_field in tables_in_group.items():
                if other in levels:
                    continue
                levels[other] = LEVEL_SCENARIO
                reasons[other] = (f"collegata tramite {current}.{tables_in_group[current]} = {other}.{other_field}, "
                                  "non tramite la chiave del contatto, quindi più contatti condividono una riga")
                queue.append(other)
    return levels, reasons


def _group_members(keys: _KeyUnion, selected: list[str], tables: dict[str, Table]) -> dict[FieldRef, dict[str, str]]:
    """For each key group (by its root): the selected tables holding one of its fields, each with the
    first such field in schema order."""
    members: dict[FieldRef, dict[str, str]] = {}
    for name in selected:
        for f in tables[name].fields:
            ref = (name, f.name)
            if ref in keys:
                members.setdefault(keys.find(ref), {}).setdefault(name, f.name)
    return members


def _key_groups(keys: _KeyUnion, contact_key: FieldRef, levels: dict[str, str], entry: str, selected: list[str],
                tables: dict[str, Table]) -> list[KeyGroup]:
    """Spec "Keys". The contact key's group is kind "contact" (contact keys TST_C0001...). A group
    holding a field of a scenario-level table is kind "scenario": it is the field that joins contacts
    to the scenario-level table (scenario keys TST_S01...). The other groups are kind "other".

    Fields of tables without a CSV stay in their group; a group with no field of a selected table is
    left out, since no CSV holds it. Order: contact group, scenario groups, other groups; inside a
    group, the entry table first (its contact key field before its other fields), then tables with a
    CSV, then the others, by name and schema order. So the first field of a table in a group is the
    one that holds that group's key for the table.
    """
    contact_root = keys.find(contact_key)
    selected_set = set(selected)

    def sort_key(ref: FieldRef) -> tuple:
        table, field_name = ref
        return (table != entry, ref != contact_key, table not in selected_set, table.lower(), table,
                tables[table].field_names.index(field_name))

    groups: list[KeyGroup] = []
    for refs in keys.groups():
        if not any(table in selected_set for table, _ in refs):
            continue
        if keys.find(refs[0]) == contact_root:
            kind = GROUP_CONTACT
        elif any(levels.get(table) == LEVEL_SCENARIO for table, _ in refs):
            kind = GROUP_SCENARIO
        else:
            kind = GROUP_OTHER
        groups.append(KeyGroup(kind=kind, fields=sorted(refs, key=sort_key)))
    kind_order = {GROUP_CONTACT: 0, GROUP_SCENARIO: 1, GROUP_OTHER: 2}
    groups.sort(key=lambda group: (kind_order[group.kind], sort_key(group.fields[0])))
    return groups


def _field_levels(selected: list[str], tables: dict[str, Table], levels: dict[str, str],
                  key_groups: list[KeyGroup], entry: Optional[str] = None,
                  entry_parsed: Optional[ParsedQuery] = None, names=None) -> dict[FieldRef, str]:
    """Every field of every selected table takes its table's level, except the fields of a
    contact-level table that belong to a scenario key group: they hold the scenario key (the pilot's
    Target.Description and the entry table's CampaignName), so they are scenario-level. An entry
    column that the entry query copies from a scenario-level column is scenario-level too: every
    contact of the scenario gets the same value."""
    scenario_key_fields = {ref for group in key_groups if group.kind == GROUP_SCENARIO for ref in group.fields}
    out: dict[FieldRef, str] = {}
    for name in selected:
        for f in tables[name].fields:
            ref = (name, f.name)
            level = levels[name]
            if level == LEVEL_CONTACT and ref in scenario_key_fields:
                level = LEVEL_SCENARIO
            out[ref] = level
    if entry is not None and entry_parsed is not None and names is not None and entry in tables:
        for item in entry_parsed.select:
            if not isinstance(item.expr, SelectColumn):
                continue
            target = tables[entry].field(item.target_column)
            source = names.field_ref(item.expr.source.table, item.expr.source.column)
            if target is None or source is None:
                continue
            if levels.get(source[0]) == LEVEL_SCENARIO:
                out[(entry, target.name)] = LEVEL_SCENARIO
    return out


# ---------------------------------------------------------------------------
# Columns copied by the followed queries
# ---------------------------------------------------------------------------

def _aliases(source_queries: list[ParsedQuery], names: _Names, key_groups: list[KeyGroup], selected: list[str],
             order: list[str], tables: dict[str, Table]) -> list[list[FieldRef]]:
    """Spec, step 3, "Columns the entry query derives and columns it copies": "A column of the entry table
    copied as a plain column from a source table (M.Entitle_RequestCancellationDate) holds the same value as
    its source: the two fields are one variable for the coverage (as the key groups are for keys) ... The
    same holds through a chain of followed queries."

    The plain column copies of every followed query (``SelectColumn`` only: never a literal or an
    expression) are grouped, through chains, when both tables get a CSV and neither field is in a key group:
    the copies of keys are already in the key groups (``_link_keys``). Each group lists its fields in schema
    order (table order of the model, then the field's position); the groups follow their first field.
    """
    key_fields = {ref for group in key_groups for ref in group.fields}
    selected_set = set(selected)
    union = _KeyUnion()
    for source, target in _select_copies(source_queries, names):
        if source in key_fields or target in key_fields:
            continue
        if source[0] in selected_set and target[0] in selected_set:
            union.union(source, target)
    position = {name: index for index, name in enumerate(order)}

    def schema_order(ref: FieldRef) -> tuple[int, int]:
        return position[ref[0]], tables[ref[0]].field_names.index(ref[1])

    groups = [sorted(refs, key=schema_order) for refs in union.groups()]
    return sorted(groups, key=lambda refs: schema_order(refs[0]))


# ---------------------------------------------------------------------------
# Row present or absent
# ---------------------------------------------------------------------------

EXISTENCE_OPERATORS = frozenset({"IsNull", "IsNotNull"})


def _existence_tested(package: Package, entry: str, roles: dict[str, set[str]], levels: dict[str, str],
                      keys: _KeyUnion, contact_key: FieldRef,
                      names: _Names) -> tuple[dict[str, FieldRef], dict[str, list[str]]]:
    """Spec, "Row present or absent": "A contact-level table other than the entry table, whose existence a
    split tests (IsNull or IsNotNull on its field linked to the contact key, or the attribute comparison
    above), gets a contact-level variable "row present / row absent". No such variable for tables the entry
    query joins, or that no split tests for existence."

    The field linked to the contact key is a field of the table in the contact key's group. The tables the
    entry query joins are read here as every source table (the entry query and the queries feeding its
    sources), since a missing row there changes the entry rows themselves. Only conditions that are
    evaluated count: an unsupported outcome, and the outcomes after it, are never evaluated.

    Returns table -> the tested field, in order of first test, and table -> the splits testing it.
    """
    contact_root = keys.find(contact_key)
    tested: dict[str, FieldRef] = {}
    splits: dict[str, list[str]] = {}
    for activity in package.journey.activities.values():
        if activity.kind != KIND_DECISION:
            continue
        for outcome in activity.outcomes:
            if outcome.unsupported_reason:
                break   # the walk stops at an unsupported outcome: the outcomes after it are never evaluated
            for condition in formula_conditions(outcome.formula):
                if condition.operator not in EXISTENCE_OPERATORS and condition.value_attribute is None:
                    continue
                ref = names.field_ref(condition.table, condition.field)
                if ref is None or ref not in keys or keys.find(ref) != contact_root:
                    continue
                table = ref[0]
                if table == entry or levels.get(table) != LEVEL_CONTACT or ROLE_SOURCE in roles.get(table, set()):
                    continue
                tested.setdefault(table, ref)
                _add_once(splits.setdefault(table, []), activity.key)
    return tested, splits


def _existence_note(ref: FieldRef, split_keys: list[str]) -> str:
    """The sentence added to the reason of a table whose existence a split tests."""
    who = (f"Lo split {split_keys[0]} verifica" if len(split_keys) == 1
           else f"Gli split {_short_list(split_keys)} verificano")
    return (f"{who} se il contatto ha una riga in questa tabella tramite {ref[1]}, il campo collegato alla chiave "
            "del contatto: i contatti possono avere la riga o non averla.")


# ---------------------------------------------------------------------------
# Activation field
# ---------------------------------------------------------------------------

def _activation_field(entry_parsed: Optional[ParsedQuery], levels: dict[str, str], names: _Names,
                      warnings: list[str]) -> tuple[Optional[FieldRef], str]:
    """Spec "One scenario at a time": the activation field is "suggested from the entry query filters
    (ACTIVE in the pilot)".

    The suggestion is the first equality filter of the entry query on a Boolean field (per schema) of
    a scenario-level table whose value is true, e.g. ``C.active = 'True'``. Any other filter is not an
    activation field, a ``DateOffsetFilter`` (the date part equal to today moved by some days) included.
    Returns the field in the schema spelling, or None, and a sentence for the table's reason.
    """
    if entry_parsed is None:
        return None, ""
    candidates: list[tuple[FieldRef, str]] = []
    for flt in entry_parsed.filters:
        if not isinstance(flt, EqualityFilter):
            continue
        ref = names.field_ref(flt.column.table, flt.column.column)
        if ref is None or levels.get(ref[0]) != LEVEL_SCENARIO:
            continue
        if names.tables[ref[0]].field(ref[1]).type.lower() != "boolean":
            continue
        if flt.value.strip().lower() not in ("true", "1"):
            continue
        candidates.append((ref, f"{flt.column.column} = '{flt.value}'"))
    if not candidates:
        return None, ""
    ref, filter_text = candidates[0]
    if len(candidates) > 1:
        others = [f"{table}.{field_name}" for (table, field_name), _ in candidates[1:]]
        warnings.append(f"Più filtri della entry query possono indicare il campo di attivazione: si suggerisce "
                        f"{ref[0]}.{ref[1]} (il primo), non {_join_words(others)}")
    return ref, (f"Il suo campo {ref[1]} è suggerito come campo di attivazione (filtro della entry query "
                 f"{filter_text}).")


# ---------------------------------------------------------------------------
# Reasons and small text helpers
# ---------------------------------------------------------------------------

def _selected_reason(roles: set[str], use: _Use, entry_query_name: Optional[str], level: str,
                     level_reason: str, notes: list[str]) -> str:
    """Plain words for a table that gets a CSV: who reads it, who else writes it, the paths it is on
    (step 3, link tables), its level, then ``notes`` (LEFT JOIN, managed table, activation field, row
    present or absent)."""
    reads = _reads_text(use, entry_query_name)
    link = _link_text(use)
    if ROLE_ENTRY in roles:
        if entry_query_name:
            text = f"Tabella di ingresso del journey, popolata dalla entry query {entry_query_name}"
        else:
            text = "Tabella di ingresso del journey, non popolata da alcuna query dell'export"
        if reads:
            text += f"; letta anche {_join_words(reads)}"
        writers = _writers_text(use, skip_query=entry_query_name)
    elif reads:
        text = f"Letta {_join_words(reads)}"
        writers = _writers_text(use)
    else:   # a link table only: the path is what it is read for
        text, link = link[0].upper() + link[1:], ""
        writers = _writers_text(use)
    if writers:
        text += f"; scritta anche {writers}"
    level_name = "A livello di contatto" if level == LEVEL_CONTACT else "A livello di scenario"
    parts = [f"{text}."]
    if link:
        parts.append(f"È anche {link}.")
    parts.append(f"{level_name}: {level_reason}.")
    parts.extend(note for note in notes if note)
    return " ".join(parts)


def _link_text(use: _Use) -> str:
    """Spec, step 3: a link table is needed "because without its rows the journey cannot read the table at
    the end of the path". The clause ``"sul percorso tra il contatto e X letta da SPLIT-1: senza le sue righe
    il journey non può leggere X"``, empty when the table is on no path."""
    if not use.link_for:
        return ""
    parts: list[str] = []
    for table, readers in use.link_for.items():
        part = f"{table} letta da {_readers_text(readers)}" if readers else table
        if table in use.inferred_links:
            part += f" ({INFERRED_PATH})"
        parts.append(part)
    return (f"sul percorso tra il contatto e {', e tra il contatto e '.join(parts)}: senza le sue righe il journey "
            f"non può leggere {_join_words(list(use.link_for))}")


def _readers_text(readers: list[tuple[str, str]]) -> str:
    """The readers of a table on a path: their keys when they are few (``SPLIT-1 e WAIT-1``), else a count
    by kind (``62 split e 3 Wait by attribute``)."""
    if len(readers) <= READERS_NAMED:
        return _join_words([key for _, key in readers])
    counts = {kind: sum(1 for reader_kind, _ in readers if reader_kind == kind)
              for kind in (READ_SPLIT, READ_WAIT, READ_EXIT)}
    words = {READ_SPLIT: ("split", "split"), READ_WAIT: ("Wait by attribute", "Wait by attribute"),
             READ_EXIT: ("criterio di uscita", "criteri di uscita")}
    return _join_words([_count(n, *words[kind]) for kind, n in counts.items() if n])


def _reads_text(use: _Use, entry_query_name: Optional[str]) -> list[str]:
    phrases: list[str] = []
    if use.splits:
        phrases.append("da " + _count(len(use.splits), "split", "split"))
    if use.waits:
        phrases.append("da " + _count(len(use.waits), "Wait by attribute", "Wait by attribute"))
    if use.exits:
        phrases.append("dai criteri di uscita (exit criteria)")
    if use.read_by_query:
        if use.read_by_query == entry_query_name:
            phrases.append(f"dalla entry query {use.read_by_query}")
        else:
            phrases.append(f"dalla query {use.read_by_query}, che popola {use.query_fills}")
    return phrases


def _writers_text(use: _Use, skip_query: Optional[str] = None) -> str:
    phrases: list[str] = []
    if use.update_steps:
        phrases.append("da " + _count(len(use.update_steps), "attività Update contact", "attività Update contact"))
    queries = [name for name in use.written_by_queries if name != skip_query]
    if queries:
        phrases.append(("dalla query " if len(queries) == 1 else "dalle query ") + _join_words(queries))
    return _join_words(phrases)


def _add_once(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _join_words(items: list[str]) -> str:
    """``a``, ``a e b``, ``a, b e c`` (Italian: the texts built with it are in Italian)."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " e " + items[-1]


def _short_list(items: list[str], limit: int = 5) -> str:
    if len(items) <= limit:
        return _join_words(items)
    return ", ".join(items[:limit]) + f" e altri {len(items) - limit}"


def _count(n: int, singular: str, plural: Optional[str] = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _column_text(ref: ColumnRef) -> str:
    return f"{ref.table}.{ref.column}" if ref.table else ref.column
