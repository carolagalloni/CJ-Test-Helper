"""Tests for cjhelper.tables: table selection, key groups and levels (spec "Table selection",
"Journey model and coverage" step 3, "Data generation rules" / "Keys").

The packages are built by hand and ``sqlparse_lite.parse_query`` is replaced by a stub that
returns hand-made ParsedQuery objects, so these tests do not depend on the SQL parser.
"""

import importlib.util
import sys
import types
from dataclasses import replace
from pathlib import Path

import pytest

import cjhelper

SQLPARSE_WRITTEN = importlib.util.find_spec("cjhelper.sqlparse_lite") is not None
if not SQLPARSE_WRITTEN:
    # sqlparse_lite.py is written in parallel. Until it exists, a placeholder lets cjhelper.tables
    # import; every unit test below replaces parse_query anyway, and the pilot test is skipped.
    _placeholder = types.ModuleType("cjhelper.sqlparse_lite")

    def _not_written_yet(query, schema):
        raise NotImplementedError("cjhelper.sqlparse_lite is not written yet")

    _placeholder.parse_query = _not_written_yet
    sys.modules["cjhelper.sqlparse_lite"] = _placeholder
    cjhelper.sqlparse_lite = _placeholder

from cjhelper import tables  # noqa: E402
from cjhelper.model import (  # noqa: E402
    ENTRY_AUTOMATION,
    ENTRY_OTHER,
    ENTRY_RUN_ONCE,
    KIND_DECISION,
    KIND_UPDATE,
    KIND_WAIT_ATTRIBUTE,
    LEVEL_CONTACT,
    LEVEL_SCENARIO,
    ROLE_ENTRY,
    ROLE_LINK,
    ROLE_SOURCE,
    ROLE_SPLIT,
    ROLE_WRITTEN,
    SOURCE_JOURNEY,
    Activity,
    ColumnRef,
    Condition,
    ConditionGroup,
    DateOffsetFilter,
    EntryEvent,
    EqualityFilter,
    Field,
    JoinEquality,
    Journey,
    JourneyFilter,
    Outcome,
    Package,
    ParsedQuery,
    Query,
    Relationship,
    SelectColumn,
    SelectConcat,
    SelectItem,
    SelectLiteral,
    Table,
    TableRef,
    Write,
    formula_conditions,
)

PILOT = Path(__file__).resolve().parent.parent / "fixtures" / "CJ template complesso-v1.json"

ENTRY = "DE_Entry"
MASTER = "DE_Master"
TARGET = "DE_Target"
CONFIG = "DE_Config"


# ---------------------------------------------------------------------------
# Hand-made packages
# ---------------------------------------------------------------------------

def text(name, pk=False):
    return Field(name=name, type="Text", length=50, is_primary_key=pk, is_nullable=not pk, default=None, ordinal=0)


def boolean(name):
    return Field(name=name, type="Boolean", length=None, is_primary_key=False, is_nullable=False,
                 default="False", ordinal=0)


def date(name):
    return Field(name=name, type="Date", length=None, is_primary_key=False, is_nullable=True, default=None, ordinal=0)


def make_table(name, *fields, sendable_field=None, managed=False):
    numbered = [replace(f, ordinal=i) for i, f in enumerate(fields)]
    return Table(id=f"id-{name}", name=name, customer_key=name, fields=numbered,
                 is_sendable=sendable_field is not None, sendable_field=sendable_field, managed=managed)


def cond(table, field, value="true", operator="Is", path=()):
    """A Contact Data condition; ``path`` = the tables between the contact and ``table`` (as loader.py fills it)."""
    return Condition(table=table, field=field, operator=operator, value=value, path=tuple(path))


def decision(key, *conditions):
    """A decision split with one outcome per condition, plus the remainder."""
    outcomes = [Outcome(key=f"{key}-path{i}", label=f"path {i}", next=None, formula=c)
                for i, c in enumerate(conditions, 1)]
    outcomes.append(Outcome(key="remainder_path", label="Remainder", next=None, is_remainder=True))
    return Activity(key=key, raw_type="MULTICRITERIADECISION", kind=KIND_DECISION, name=key, outcomes=outcomes)


def wait_on(key, table, field, path=()):
    return Activity(key=key, raw_type="WAIT", kind=KIND_WAIT_ATTRIBUTE, name=key,
                    outcomes=[Outcome(key=f"{key}-out", label="", next=None)], wait_field=(table, field),
                    wait_path=tuple(path))


def update(key, table, field, value="x"):
    return Activity(key=key, raw_type="UPDATECONTACTDATA", kind=KIND_UPDATE, name=key,
                    outcomes=[Outcome(key=f"{key}-out", label="", next=None)],
                    writes=[Write(table=table, field=field, value=value)])


def query(name, target):
    return Query(id=f"id-{name}", name=name, target_table=target, sql=f"-- {name}: parsed by the stub",
                 update_type="Update")


def relation(left_table, left_field, right_table, right_field):
    """A relationship; ``left_table`` None is a group-to-set link (the contact model pointing at a table)."""
    return Relationship(name=f"{left_table or 'Contact'} -> {right_table}", left_table=left_table,
                        left_field=left_field, right_table=right_table, right_field=right_field,
                        left_cardinality="One", right_cardinality="One")


def make_package(table_list, activities=(), entry=ENTRY, queries=(), automation=(), relationships=()):
    """A package whose entry event is fed by automation A_Entry running ``automation`` (query names)."""
    activities = list(activities)
    journey = Journey(name="J", key="J", activities={a.key: a for a in activities},
                      start_key=activities[0].key if activities else None, entry_event_key="EV",
                      entry_table=entry, activity_count=len(activities))
    return Package(name="P", journey=journey, tables={t.name: t for t in table_list},
                   relationships=list(relationships), queries=list(queries),
                   entry_event=EntryEvent(key="EV", name="EV", table=entry, automation_name="A_Entry"),
                   emails={}, automation_queries={"A_Entry": list(automation)})


def col(ref):
    """``"Table.Column"`` -> ColumnRef; ``".Column"`` is a column the parser could not tie to a table."""
    table, _, column = ref.partition(".")
    return ColumnRef(table=table, column=column)


def parsed(name, target, sources, joins=(), filters=(), select=(), warnings=(), optional=()):
    """A ParsedQuery: joins are (left, right) pairs, filters (column, value) equalities,
    select (target column, source column) plain copies; the ``optional`` sources are LEFT JOINed."""
    return ParsedQuery(
        name=name, target_table=target,
        sources=[TableRef(table=t, alias=None, optional=t in optional) for t in sources],
        joins=[JoinEquality(left=col(a), right=col(b)) for a, b in joins],
        filters=[EqualityFilter(column=col(c), value=v) for c, v in filters],
        select=[SelectItem(target_column=t, expr=SelectColumn(source=col(s))) for t, s in select],
        warnings=list(warnings))


class ParserStub:
    """Stands in for sqlparse_lite.parse_query: answers by query name and records the calls."""

    def __init__(self):
        self.answers = {}
        self.calls = []

    def add(self, parsed_query):
        self.answers[parsed_query.name] = parsed_query

    def __call__(self, query, schema):
        self.calls.append(query.name)
        return self.answers[query.name]


@pytest.fixture
def sql(monkeypatch):
    stub = ParserStub()
    monkeypatch.setattr(tables.sqlparse_lite, "parse_query", stub)
    return stub


def summary(model):
    return {name: (role.roles, role.selected, role.level) for name, role in model.tables.items()}


def groups(model):
    return [(group.kind, group.fields) for group in model.key_groups]


def fields_of(package, name):
    return [f.name for f in package.tables[name].fields]


def pilot_like(sql):
    """The pilot's shape in miniature (docs/progress.md, "Facts checked on the pilot export")."""
    table_list = [
        make_table(ENTRY, text("ContactId", pk=True), text("CampaignName", pk=True), text("Emailaddress"),
                   boolean("Control Group"), sendable_field="ContactId"),
        make_table(MASTER, text("ContactId", pk=True), text("Emailaddress"), boolean("IsMailOffers"),
                   sendable_field="ContactId"),
        make_table(TARGET, text("SalesforceId", pk=True), text("Description", pk=True), date("CreatedDate"),
                   sendable_field="SalesforceId"),
        make_table(CONFIG, text("CampaignName", pk=True), text("CHANNEL1"), date("WAIT1"), boolean("ACTIVE"),
                   boolean("HAS_RUN")),
        make_table("DE_Log", text("ContactId", pk=True), text("CampaignName", pk=True), sendable_field="ContactId"),
        make_table("DE_Entry_Backup", text("ContactId", pk=True), sendable_field="ContactId"),
        make_table("DE_Check", text("CampaignName"), boolean("Active")),
        make_table("DE_Unused", text("Id", pk=True)),
    ]
    activities = [
        decision("SPLIT-1", cond(CONFIG, "CHANNEL1", "Email", "Equal")),
        decision("SPLIT-2", cond(MASTER, "IsMailOffers"), cond(CONFIG, "CHANNEL1", "DM", "Equal")),
        wait_on("WAIT-1", CONFIG, "WAIT1"),
        update("UPDATE-1", "DE_Log", "CampaignName", "Demo"),
        update("UPDATE-2", ENTRY, "Control Group", "true"),
    ]
    queries = [query("Q_Check", "DE_Check"), query("Q_Entry", ENTRY), query("Q_ConfigUpdate", CONFIG),
               query("Q_Backup", "DE_Entry_Backup")]
    sql.add(parsed("Q_Entry", ENTRY, [MASTER, TARGET, CONFIG],
                   joins=[("DE_Master.ContactId", "DE_Target.SalesforceId"),
                          ("DE_Target.Description", "DE_Config.CampaignName")],
                   filters=[("DE_Config.active", "True"), ("DE_Config.HAS_RUN", "False")],
                   select=[("ContactId", "DE_Master.ContactId"), ("CampaignName", "DE_Config.CampaignName"),
                           ("Emailaddress", "DE_Master.Emailaddress"), ("FirstName", "DE_Master.FirstName")]))
    sql.add(parsed("Q_ConfigUpdate", CONFIG, [CONFIG, ENTRY],
                   joins=[("DE_Config.CampaignName", "DE_Entry.CampaignName")],
                   select=[("CampaignName", "DE_Config.CampaignName")]))
    relationships = [
        relation(None, None, MASTER, "ContactId"),
        relation(MASTER, "ContactId", ENTRY, "ContactId"),
        relation(ENTRY, "CampaignName", CONFIG, "CampaignName"),
    ]
    return make_package(table_list, activities, ENTRY, queries,
                        automation=["Q_Check", "Q_Entry", "Q_ConfigUpdate", "Q_Backup"], relationships=relationships)


# ---------------------------------------------------------------------------
# The pilot's shape
# ---------------------------------------------------------------------------

def test_pilot_shape_roles_levels_and_keys(sql):
    package = pilot_like(sql)
    model = tables.build_table_model(package)

    assert summary(model) == {
        ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        "DE_Check": ({ROLE_WRITTEN}, False, None),
        CONFIG: ({ROLE_SPLIT, ROLE_SOURCE}, True, LEVEL_SCENARIO),
        "DE_Entry_Backup": ({ROLE_WRITTEN}, False, None),
        "DE_Log": ({ROLE_WRITTEN}, False, None),
        MASTER: ({ROLE_SPLIT, ROLE_SOURCE}, True, LEVEL_CONTACT),
        TARGET: ({ROLE_SOURCE}, True, LEVEL_CONTACT),
        "DE_Unused": (set(), False, None),
    }
    assert list(model.tables) == [ENTRY, "DE_Check", CONFIG, "DE_Entry_Backup", "DE_Log", MASTER, TARGET, "DE_Unused"]
    assert model.selected_tables == [ENTRY, CONFIG, MASTER, TARGET]
    assert model.contact_tables == [ENTRY, MASTER, TARGET]
    assert model.scenario_tables == [CONFIG]

    # The query writing Config from Config and the entry table is followed once, then the walk stops.
    assert model.entry_query.name == "Q_Entry"
    assert [q.name for q in model.source_queries] == ["Q_Entry", "Q_ConfigUpdate"]
    assert sql.calls == ["Q_Entry", "Q_ConfigUpdate"]

    assert model.contact_key_field == (ENTRY, "ContactId")
    assert groups(model) == [
        ("contact", [(ENTRY, "ContactId"), (MASTER, "ContactId"), (TARGET, "SalesforceId")]),
        ("scenario", [(ENTRY, "CampaignName"), (CONFIG, "CampaignName"), (TARGET, "Description")]),
    ]

    selected_fields = {(name, f) for name in model.selected_tables for f in fields_of(package, name)}
    assert set(model.field_levels) == selected_fields
    assert model.field_levels[(TARGET, "Description")] == LEVEL_SCENARIO
    assert model.field_levels[(TARGET, "SalesforceId")] == LEVEL_CONTACT
    assert model.field_levels[(TARGET, "CreatedDate")] == LEVEL_CONTACT
    assert model.field_levels[(ENTRY, "CampaignName")] == LEVEL_SCENARIO
    assert model.field_levels[(ENTRY, "Control Group")] == LEVEL_CONTACT
    assert {model.field_levels[(CONFIG, f)] for f in fields_of(package, CONFIG)} == {LEVEL_SCENARIO}
    assert {model.field_levels[(MASTER, f)] for f in fields_of(package, MASTER)} == {LEVEL_CONTACT}

    assert model.activation_field == (CONFIG, "ACTIVE")
    assert model.warnings == []


def test_pilot_shape_reasons_are_plain_words(sql):
    model = tables.build_table_model(pilot_like(sql))
    reasons = {name: role.reason for name, role in model.tables.items()}

    assert reasons[ENTRY] == ("Tabella di ingresso del journey, popolata dalla entry query Q_Entry; scritta anche "
                              "da 1 attività Update contact. A livello di contatto: è la tabella di ingresso, una "
                              "riga per contatto.")
    assert reasons[MASTER] == ("Letta da 1 split e dalla entry query Q_Entry. A livello di contatto: collegata alla "
                               "tabella di ingresso tramite la chiave del contatto (DE_Master.ContactId).")
    assert reasons[TARGET] == ("Letta dalla entry query Q_Entry. A livello di contatto: collegata alla tabella di "
                               "ingresso tramite la chiave del contatto (DE_Target.SalesforceId).")
    assert reasons[CONFIG] == (
        "Letta da 2 split, da 1 Wait by attribute e dalla entry query Q_Entry; scritta anche dalla query "
        "Q_ConfigUpdate. A livello di scenario: collegata tramite DE_Entry.CampaignName = DE_Config.CampaignName, "
        "non tramite la chiave del contatto, quindi più contatti condividono una riga. Il suo campo ACTIVE è "
        "suggerito come campo di attivazione (filtro della entry query active = 'True').")
    assert reasons["DE_Log"] == ("Scritta da 1 attività Update contact, mai letta da uno split o dalla entry query: "
                                 "nessun CSV.")
    assert reasons["DE_Check"] == "Scritta dalla query Q_Check, mai letta da uno split o dalla entry query: nessun CSV."
    assert reasons["DE_Entry_Backup"] == ("Scritta dalla query Q_Backup, mai letta da uno split o dalla entry query: "
                                          "nessun CSV.")
    assert reasons["DE_Unused"] == "Né letta né scritta da questo journey."


# ---------------------------------------------------------------------------
# Entry query
# ---------------------------------------------------------------------------

def test_no_entry_query_leaves_the_entry_table_alone(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("Segment"), sendable_field="ContactId"),
         make_table(CONFIG, text("CampaignName", pk=True), boolean("ACTIVE"))],
        [decision("SPLIT-1", cond(CONFIG, "ACTIVE")), decision("SPLIT-2", cond(ENTRY, "Segment", "A", "Equal"))])
    model = tables.build_table_model(package)

    assert model.entry_query is None
    assert model.source_queries == []
    assert sql.calls == []
    assert summary(model) == {
        ENTRY: ({ROLE_ENTRY, ROLE_SPLIT}, True, LEVEL_CONTACT),
        CONFIG: ({ROLE_SPLIT}, True, LEVEL_SCENARIO),
    }
    assert groups(model) == [("contact", [(ENTRY, "ContactId")])]
    assert model.field_levels == {
        (ENTRY, "ContactId"): LEVEL_CONTACT, (ENTRY, "Segment"): LEVEL_CONTACT,
        (CONFIG, "CampaignName"): LEVEL_SCENARIO, (CONFIG, "ACTIVE"): LEVEL_SCENARIO,
    }
    assert model.activation_field is None
    assert model.warnings == [
        "Nessuna query dell'automation di ingresso A_Entry popola la tabella di ingresso DE_Entry, e nessun'altra "
        "query dell'export popola DE_Entry: non c'è una entry query, quindi l'unica sorgente è la tabella di "
        "ingresso",
        "La tabella DE_Config non è collegata alla tabella di ingresso: è trattata come una riga condivisa da tutti "
        "i contatti (a livello di scenario). È un'assunzione: verifica come il journey collega questa tabella",
    ]
    assert "non popolata da alcuna query dell'export" in model.tables[ENTRY].reason
    assert "A livello di scenario: non collegata alla tabella di ingresso" in model.tables[CONFIG].reason


def test_every_query_filling_the_entry_table_is_followed(sql):
    """Spec, source table: "Any table in the FROM or JOIN of a query that fills the entry table".
    Both queries of the entry automation fill the entry table: the tables they read are source
    tables with a CSV; the entry rows come from the last one (changed in review: only the last one
    used to be followed, and the first one's source table got no CSV)."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True)),
         make_table("DE_Other", text("ContactId", pk=True))],
        queries=[query("Q_First", ENTRY), query("Q_Second", ENTRY)], automation=["Q_First", "Q_Second"])
    sql.add(parsed("Q_First", ENTRY, ["DE_Other"], select=[("ContactId", "DE_Other.ContactId")]))
    sql.add(parsed("Q_Second", ENTRY, [MASTER], select=[("ContactId", "DE_Master.ContactId")],
                   warnings=["an OR group in WHERE is not understood"]))
    model = tables.build_table_model(package)

    assert model.entry_query.name == "Q_Second"
    assert [q.name for q in model.source_queries] == ["Q_Second", "Q_First"]
    assert sorted(sql.calls) == ["Q_First", "Q_Second"]
    assert model.warnings == [
        "Query Q_Second: an OR group in WHERE is not understood",
        "Più query popolano la tabella di ingresso DE_Entry (Q_First e Q_Second): le tabelle che leggono sono "
        "tutte tabelle sorgente, e le righe di ingresso sono costruite da Q_Second, l'ultima nell'ordine degli step",
    ]
    assert "scritta anche dalla query Q_First" in model.tables[ENTRY].reason
    assert model.tables[MASTER].level == LEVEL_CONTACT
    other = model.tables["DE_Other"]
    assert (other.roles, other.selected, other.level) == ({ROLE_SOURCE}, True, LEVEL_CONTACT)
    assert other.reason.startswith("Letta dalla query Q_First, che popola DE_Entry.")


def test_a_post_processing_query_on_the_entry_table_does_not_replace_the_entry_query(sql):
    """An update step after the entry query that reads only the entry table must not hide the real
    entry query: its source tables keep their CSV and the activation field is still found."""
    package = pilot_like(sql)
    package.queries.append(query("Q_Mark", ENTRY))
    package.automation_queries["A_Entry"].insert(2, "Q_Mark")
    sql.add(parsed("Q_Mark", ENTRY, [ENTRY], select=[("ContactId", "DE_Entry.ContactId")]))
    model = tables.build_table_model(package)

    assert model.entry_query.name == "Q_Entry"
    assert [q.name for q in model.source_queries] == ["Q_Entry", "Q_Mark", "Q_ConfigUpdate"]
    assert model.selected_tables == [ENTRY, CONFIG, MASTER, TARGET]
    assert model.tables[TARGET].roles == {ROLE_SOURCE}
    assert model.activation_field == (CONFIG, "ACTIVE")
    assert model.warnings == [
        "Più query popolano la tabella di ingresso DE_Entry (Q_Entry e Q_Mark): le tabelle che leggono sono "
        "tutte tabelle sorgente, e le righe di ingresso sono costruite da Q_Entry, l'ultima che legge una tabella "
        "diversa da DE_Entry",
    ]


def test_entry_query_found_outside_the_entry_automation(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True))],
        queries=[query("Q_Unrelated", MASTER), query("Q_Elsewhere", ENTRY)],
        automation=["Q_Unrelated", "Q_Missing"])
    sql.add(parsed("Q_Elsewhere", ENTRY, [MASTER], select=[("ContactId", "DE_Master.ContactId")]))
    sql.add(parsed("Q_Unrelated", MASTER, []))
    model = tables.build_table_model(package)

    assert model.entry_query.name == "Q_Elsewhere"
    # Master is a source, so the query writing it is followed too.
    assert [q.name for q in model.source_queries] == ["Q_Elsewhere", "Q_Unrelated"]
    assert model.warnings == [
        "L'automation di ingresso A_Entry esegue la query Q_Missing, che non è nell'export",
        "Nessuna query dell'automation di ingresso A_Entry popola la tabella di ingresso DE_Entry; la query "
        "Q_Elsewhere popola DE_Entry e viene presa come entry query",
    ]


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_table_both_written_and_read_is_selected(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), boolean("Flag")),
         make_table(CONFIG, text("CampaignName", pk=True), text("Channel")),
         make_table("DE_Log", text("ContactId", pk=True))],
        [decision("SPLIT-1", cond(MASTER, "Flag"), cond(CONFIG, "Channel", "Email", "Equal")),
         update("UPDATE-1", MASTER, "Flag", "false"),
         update("UPDATE-2", "DE_Log", "ContactId")],
        queries=[query("Q_Config", CONFIG)],
        relationships=[relation(MASTER, "ContactId", ENTRY, "ContactId")])
    model = tables.build_table_model(package)

    master, config, log = model.tables[MASTER], model.tables[CONFIG], model.tables["DE_Log"]
    assert (master.roles, master.selected, master.level) == ({ROLE_SPLIT}, True, LEVEL_CONTACT)
    assert master.reason.startswith("Letta da 1 split; scritta anche da 1 attività Update contact.")
    assert (config.roles, config.selected) == ({ROLE_SPLIT}, True)
    assert config.reason.startswith("Letta da 1 split; scritta anche dalla query Q_Config.")
    assert (log.roles, log.selected, log.level) == ({ROLE_WRITTEN}, False, None)
    assert sql.calls == []   # Q_Config feeds no source table: it is never parsed


def test_split_on_a_table_or_field_missing_from_the_export(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId")],
        [decision("SPLIT-1", cond("DE_Ghost", "X")), decision("SPLIT-2", cond(ENTRY, "Nope")),
         decision("SPLIT-3", cond("DE_Ghost", "Y"))])
    model = tables.build_table_model(package)

    assert list(model.tables) == [ENTRY]
    assert "La tabella DE_Ghost è letta da SPLIT-1 e SPLIT-3 ma non è nell'export: non è possibile crearne il CSV" \
        in model.warnings
    assert "DE_Entry.Nope è letto da SPLIT-2 ma non è un campo dell'export" in model.warnings


# ---------------------------------------------------------------------------
# Following the sources back
# ---------------------------------------------------------------------------

def test_source_chain_of_two_queries(sql):
    """Entry <- Q1 FROM DE_A; DE_A <- Q2 FROM DE_B, in another automation: both tables are sources."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("Email"), sendable_field="ContactId"),
         make_table("DE_A", text("Id", pk=True), text("Email")),
         make_table("DE_B", text("Key", pk=True), text("Mail"))],
        queries=[query("Q2", "DE_A"), query("Q1", ENTRY)], automation=["Q1"])
    sql.add(parsed("Q1", ENTRY, ["DE_A"], select=[("ContactId", "DE_A.Id"), ("Email", "DE_A.Email")]))
    sql.add(parsed("Q2", "DE_A", ["DE_B"], select=[("Id", "DE_B.Key"), ("Email", "DE_B.Mail")]))
    model = tables.build_table_model(package)

    assert [q.name for q in model.source_queries] == ["Q1", "Q2"]
    assert summary(model) == {
        ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        "DE_A": ({ROLE_SOURCE}, True, LEVEL_CONTACT),
        "DE_B": ({ROLE_SOURCE}, True, LEVEL_CONTACT),
    }
    assert model.tables["DE_A"].reason.startswith("Letta dalla entry query Q1; scritta anche dalla query Q2.")
    assert model.tables["DE_B"].reason.startswith("Letta dalla query Q2, che popola DE_A.")
    # The copied contact key links the three tables; the copied e-mail columns do not become keys.
    assert groups(model) == [("contact", [(ENTRY, "ContactId"), ("DE_A", "Id"), ("DE_B", "Key")])]
    assert model.warnings == []


def test_writers_of_a_source_are_followed_entry_automation_first(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table("DE_A", text("ContactId", pk=True)),
         make_table("DE_B", text("ContactId", pk=True)),
         make_table("DE_C", text("ContactId", pk=True))],
        queries=[query("Q_Nightly", "DE_A"), query("Q_Entry", ENTRY), query("Q_Refresh", "DE_A")],
        automation=["Q_Refresh", "Q_Entry"])
    sql.add(parsed("Q_Entry", ENTRY, ["DE_A"], select=[("ContactId", "DE_A.ContactId")]))
    sql.add(parsed("Q_Refresh", "DE_A", ["DE_B"], select=[("ContactId", "DE_B.ContactId")]))
    sql.add(parsed("Q_Nightly", "DE_A", ["DE_C"], select=[("ContactId", "DE_C.ContactId")]))
    model = tables.build_table_model(package)

    assert [q.name for q in model.source_queries] == ["Q_Entry", "Q_Refresh", "Q_Nightly"]
    assert {name: role.roles for name, role in model.tables.items()} == {
        ENTRY: {ROLE_ENTRY}, "DE_A": {ROLE_SOURCE}, "DE_B": {ROLE_SOURCE}, "DE_C": {ROLE_SOURCE}}
    assert model.contact_tables == [ENTRY, "DE_A", "DE_B", "DE_C"]


def test_cycles_stop(sql):
    """Config is written from Config and the entry table (as in the pilot); DE_A and DE_B fill each other."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("CampaignName"), sendable_field="ContactId"),
         make_table(CONFIG, text("CampaignName", pk=True)),
         make_table("DE_A", text("ContactId", pk=True), text("Campaign")),
         make_table("DE_B", text("ContactId", pk=True), text("Campaign"))],
        queries=[query("Q_Entry", ENTRY), query("Q_Config", CONFIG), query("Q_A", "DE_A"), query("Q_B", "DE_B")],
        automation=["Q_Entry", "Q_Config"])
    sql.add(parsed("Q_Entry", ENTRY, ["DE_A", CONFIG], joins=[("DE_A.Campaign", "DE_Config.CampaignName")],
                   select=[("ContactId", "DE_A.ContactId"), ("CampaignName", "DE_Config.CampaignName")]))
    sql.add(parsed("Q_Config", CONFIG, [CONFIG, ENTRY], joins=[("DE_Config.CampaignName", "DE_Entry.CampaignName")]))
    sql.add(parsed("Q_A", "DE_A", ["DE_B"], select=[("ContactId", "DE_B.ContactId"), ("Campaign", "DE_B.Campaign")]))
    sql.add(parsed("Q_B", "DE_B", ["DE_A"], select=[("ContactId", "DE_A.ContactId")]))
    model = tables.build_table_model(package)

    assert sql.calls == ["Q_Entry", "Q_A", "Q_Config", "Q_B"]
    assert [q.name for q in model.source_queries] == sql.calls
    assert summary(model) == {
        ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        "DE_A": ({ROLE_SOURCE}, True, LEVEL_CONTACT),
        "DE_B": ({ROLE_SOURCE}, True, LEVEL_CONTACT),
        CONFIG: ({ROLE_SOURCE}, True, LEVEL_SCENARIO),
    }
    assert groups(model) == [
        ("contact", [(ENTRY, "ContactId"), ("DE_A", "ContactId"), ("DE_B", "ContactId")]),
        ("scenario", [(ENTRY, "CampaignName"), ("DE_A", "Campaign"), ("DE_B", "Campaign"), (CONFIG, "CampaignName")]),
    ]
    assert model.warnings == []


# ---------------------------------------------------------------------------
# Contact key
# ---------------------------------------------------------------------------

def test_contact_key_is_the_sendable_field_in_schema_spelling(sql):
    package = make_package([make_table(ENTRY, text("Id", pk=True), text("ContactId"), sendable_field="contactid")])
    model = tables.build_table_model(package)

    assert model.contact_key_field == (ENTRY, "ContactId")
    assert not any("chiave del contatto" in w for w in model.warnings)


def test_contact_key_falls_back_to_the_contact_link_of_the_entry_table(sql):
    package = make_package([make_table(ENTRY, text("Id", pk=True), text("SubscriberKey"))],
                           relationships=[relation(None, None, "de_entry", "subscriberkey")])
    model = tables.build_table_model(package)

    assert model.contact_key_field == (ENTRY, "SubscriberKey")
    assert ("La tabella di ingresso DE_Entry non ha un campo sendable utilizzabile: la chiave del contatto è "
            "SubscriberKey, il campo che la relazione Contact -> de_entry collega al contatto") in model.warnings


def test_contact_key_falls_back_to_a_single_field_primary_key(sql):
    package = make_package(
        [make_table(ENTRY, text("Email"), text("Id", pk=True)), make_table(MASTER, text("ContactId", pk=True))],
        relationships=[relation(None, None, MASTER, "ContactId")])   # links another table, not the entry
    model = tables.build_table_model(package)

    assert model.contact_key_field == (ENTRY, "Id")
    assert ("La tabella di ingresso DE_Entry non ha un campo sendable utilizzabile né un collegamento al contatto: "
            "la chiave del contatto è la sua chiave primaria Id") in model.warnings


def test_contact_key_falls_back_to_the_first_of_several_primary_key_fields(sql):
    package = make_package(
        [make_table(ENTRY, text("Email"), text("ContactId", pk=True), text("CampaignName", pk=True))])
    model = tables.build_table_model(package)

    assert model.contact_key_field == (ENTRY, "ContactId")
    assert any("più campi (ContactId e CampaignName)" in w and "si prende il primo, ContactId" in w
               for w in model.warnings)


def test_sendable_field_missing_from_the_schema_falls_back(sql):
    package = make_package([make_table(ENTRY, text("Id", pk=True), sendable_field="ContactId")])
    model = tables.build_table_model(package)

    assert model.contact_key_field == (ENTRY, "Id")
    assert "Il campo sendable ContactId della tabella di ingresso DE_Entry non è nel suo schema" in model.warnings


# ---------------------------------------------------------------------------
# Levels and key groups
# ---------------------------------------------------------------------------

def test_contact_key_link_wins_over_a_scenario_neighbour(sql):
    """Target is joined to Master on the contact key and to Config on the campaign. The relationships list
    the Config links first, so a walk taking the first link it meets would reach Target from Config."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("CampaignName"), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), boolean("Flag")),
         make_table(TARGET, text("SalesforceId", pk=True), text("Description")),
         make_table(CONFIG, text("CampaignName", pk=True), text("Channel"))],
        [decision("SPLIT-1", cond(MASTER, "Flag"), cond(CONFIG, "Channel", "SMS", "Equal"),
                  cond(TARGET, "Description", "X"))],
        relationships=[relation(ENTRY, "CampaignName", CONFIG, "CampaignName"),
                       relation(CONFIG, "CampaignName", TARGET, "Description"),
                       relation(ENTRY, "ContactId", MASTER, "ContactId"),
                       relation(MASTER, "ContactId", TARGET, "SalesforceId")])
    model = tables.build_table_model(package)

    assert {name: role.level for name, role in model.tables.items()} == {
        ENTRY: LEVEL_CONTACT, CONFIG: LEVEL_SCENARIO, MASTER: LEVEL_CONTACT, TARGET: LEVEL_CONTACT}
    assert model.field_levels[(TARGET, "Description")] == LEVEL_SCENARIO
    assert model.field_levels[(TARGET, "SalesforceId")] == LEVEL_CONTACT


def test_neighbour_of_a_scenario_table_is_scenario_level(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("CampaignName"), sendable_field="ContactId"),
         make_table(CONFIG, text("CampaignName", pk=True), text("PromoCode")),
         make_table("DE_Promo", text("PromoCode", pk=True), text("Discount"))],
        [decision("SPLIT-1", cond(CONFIG, "PromoCode", "P1", "Equal"), cond("DE_Promo", "Discount", "10", "Equal"))],
        relationships=[relation(ENTRY, "CampaignName", CONFIG, "CampaignName"),
                       relation(CONFIG, "PromoCode", "DE_Promo", "PromoCode")])
    model = tables.build_table_model(package)

    assert model.scenario_tables == [CONFIG, "DE_Promo"]
    assert groups(model) == [
        ("contact", [(ENTRY, "ContactId")]),
        ("scenario", [(ENTRY, "CampaignName"), (CONFIG, "CampaignName")]),
        ("scenario", [(CONFIG, "PromoCode"), ("DE_Promo", "PromoCode")]),
    ]
    assert ("A livello di scenario: collegata tramite DE_Config.PromoCode = DE_Promo.PromoCode, non tramite la "
            "chiave del contatto, quindi più contatti condividono una riga.") in model.tables["DE_Promo"].reason
    assert not any("non è collegata alla tabella di ingresso" in w for w in model.warnings)


def test_link_through_a_table_without_csv_still_counts(sql):
    """Entry -> Log -> Foo on the contact key: Log is only written, Foo is read by a split."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table("DE_Log", text("ContactId", pk=True)),
         make_table("DE_Foo", text("ContactId", pk=True), boolean("Flag"))],
        [decision("SPLIT-1", cond("DE_Foo", "Flag")), update("UPDATE-1", "DE_Log", "ContactId")],
        relationships=[relation(ENTRY, "ContactId", "DE_Log", "ContactId"),
                       relation("DE_Log", "ContactId", "DE_Foo", "ContactId")])
    model = tables.build_table_model(package)

    assert model.tables["DE_Foo"].level == LEVEL_CONTACT
    # Members without a CSV stay in the group.
    assert groups(model) == [("contact", [(ENTRY, "ContactId"), ("DE_Foo", "ContactId"), ("DE_Log", "ContactId")])]


def test_contact_model_link_holds_the_contact_key(sql):
    """The entry table is not in the contact model; Master is linked to the contact itself (group-to-set)."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), text("Segment"), boolean("Flag")),
         make_table(CONFIG, text("Segment", pk=True), text("Channel"))],
        [decision("SPLIT-1", cond(MASTER, "Flag"), cond(CONFIG, "Channel", "Email", "Equal"))],
        relationships=[relation(None, None, MASTER, "ContactId"), relation(MASTER, "Segment", CONFIG, "Segment")])
    model = tables.build_table_model(package)

    assert model.contact_tables == [ENTRY, MASTER]
    assert model.scenario_tables == [CONFIG]
    assert groups(model) == [
        ("contact", [(ENTRY, "ContactId"), (MASTER, "ContactId")]),
        ("scenario", [(CONFIG, "Segment"), (MASTER, "Segment")]),
    ]
    assert model.field_levels[(MASTER, "Segment")] == LEVEL_SCENARIO
    assert model.field_levels[(MASTER, "Flag")] == LEVEL_CONTACT


def test_contact_key_field_comes_first_in_its_group(sql):
    """Two entry fields hold the contact key: the sendable one is listed first, so a consumer taking the
    first entry field of the group gets the contact key field."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("SubscriberKey"), sendable_field="SubscriberKey")],
        relationships=[relation(None, None, ENTRY, "ContactId")])
    model = tables.build_table_model(package)

    assert model.contact_key_field == (ENTRY, "SubscriberKey")
    assert groups(model) == [("contact", [(ENTRY, "SubscriberKey"), (ENTRY, "ContactId")])]


def test_entry_query_copy_carries_the_contact_key_without_relationships(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("Email"), text("CampaignName"), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), text("Email"), text("Segment")),
         make_table(CONFIG, text("Segment", pk=True), text("CampaignName"))],
        queries=[query("Q_Entry", ENTRY)], automation=["Q_Entry"])
    sql.add(parsed("Q_Entry", ENTRY, [MASTER, CONFIG], joins=[("DE_Master.Segment", "DE_Config.Segment")],
                   select=[("ContactId", "DE_Master.ContactId"), ("Email", "DE_Master.Email"),
                           ("CampaignName", "DE_Config.CampaignName")]))
    model = tables.build_table_model(package)

    assert model.contact_tables == [ENTRY, MASTER]
    assert model.scenario_tables == [CONFIG]
    assert groups(model) == [
        ("contact", [(ENTRY, "ContactId"), (MASTER, "ContactId")]),
        ("scenario", [(CONFIG, "Segment"), (MASTER, "Segment")]),
    ]
    assert model.warnings == []


def test_unresolvable_join_sides_are_skipped_with_a_warning(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), text("CampaignName")),
         make_table(CONFIG, text("CampaignName", pk=True))],
        queries=[query("Q_Entry", ENTRY)], automation=["Q_Entry"],
        relationships=[relation(MASTER, "ContactId", "DE_Gone", "ContactId")])
    sql.add(parsed("Q_Entry", ENTRY, [MASTER, CONFIG, "DE_Missing"],
                   joins=[(".CampaignName", "DE_Config.CampaignName"),
                          ("DE_Master.Nope", "DE_Config.CampaignName"),
                          ("DE_Missing.X", "DE_Config.CampaignName")],
                   select=[("ContactId", "DE_Master.ContactId")]))
    model = tables.build_table_model(package)

    assert model.warnings == [
        "Query Q_Entry: la tabella DE_Missing, letta dalla query, non è nell'export, quindi non è possibile "
        "crearne il CSV",
        "Query Q_Entry: il join CampaignName = DE_Config.CampaignName viene ignorato, perché la tabella della "
        "colonna CampaignName non è nota",
        "Query Q_Entry: il join DE_Master.Nope = DE_Config.CampaignName viene ignorato, perché DE_Master non ha il "
        "campo Nope",
        "Query Q_Entry: il join DE_Missing.X = DE_Config.CampaignName viene ignorato, perché la tabella DE_Missing "
        "non è nell'export",
        "La relazione DE_Master -> DE_Gone viene ignorata, perché la tabella DE_Gone non è nell'export",
        "La tabella DE_Config non è collegata alla tabella di ingresso: è trattata come una riga condivisa da tutti "
        "i contatti (a livello di scenario). È un'assunzione: verifica come il journey collega questa tabella",
    ]
    assert model.tables[MASTER].level == LEVEL_CONTACT
    assert model.tables[CONFIG].level == LEVEL_SCENARIO


def test_a_field_equal_to_itself_makes_no_key_group(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("Segment"), sendable_field="ContactId")],
        [decision("SPLIT-1", cond(ENTRY, "Segment", "A", "Equal"))],
        relationships=[relation(ENTRY, "Segment", ENTRY, "segment")])
    model = tables.build_table_model(package)

    assert groups(model) == [("contact", [(ENTRY, "ContactId")])]
    assert model.field_levels[(ENTRY, "Segment")] == LEVEL_CONTACT


# ---------------------------------------------------------------------------
# Activation field and letter case
# ---------------------------------------------------------------------------

def _activation_package(sql, filters):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), text("CampaignName"), boolean("Vip")),
         make_table(CONFIG, text("CampaignName", pk=True), boolean("ACTIVE"), text("Label"))],
        queries=[query("Q_Entry", ENTRY)], automation=["Q_Entry"])
    sql.add(parsed("Q_Entry", ENTRY, [MASTER, CONFIG], joins=[("DE_Master.CampaignName", "DE_Config.CampaignName")],
                   filters=filters, select=[("ContactId", "DE_Master.ContactId")]))
    return package


def test_boolean_filter_on_false_is_not_an_activation_field(sql):
    package = _activation_package(sql, [
        ("DE_Config.ACTIVE", "False"),   # false
        ("DE_Master.Vip", "True"),       # contact-level table
        ("DE_Config.Label", "True"),     # not a Boolean field
    ])
    model = tables.build_table_model(package)

    assert model.tables[CONFIG].level == LEVEL_SCENARIO
    assert model.activation_field is None
    assert "campo di attivazione" not in model.tables[CONFIG].reason


def test_first_true_boolean_filter_is_the_activation_field(sql):
    package = _activation_package(sql, [("DE_Config.Label", "x"), ("de_config.active", "TRUE"),
                                        ("DE_CONFIG.Active", "true")])
    model = tables.build_table_model(package)

    assert model.activation_field == (CONFIG, "ACTIVE")
    assert any(w.startswith("Più filtri della entry query possono indicare il campo di attivazione")
               for w in model.warnings)


def test_sql_and_criteria_spelling_is_normalised_to_the_schema(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("CampaignName"), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True), text("CampaignName"), boolean("Optin")),
         make_table(CONFIG, text("CampaignName", pk=True), text("CHANNEL1"), date("WAIT1"), boolean("ACTIVE"))],
        [decision("SPLIT-1", cond("de_config", "channel1", "Email", "Equal")),
         decision("SPLIT-2", cond("DE_MASTER", "OPTIN")),
         wait_on("WAIT-1", "de_config", "wait1")],
        queries=[query("Q_Entry", "de_entry")], automation=["Q_Entry"],
        relationships=[relation("de_master", "contactid", "DE_ENTRY", "CONTACTID")])
    sql.add(parsed("Q_Entry", "de_entry", ["de_master", "DE_CONFIG"],
                   joins=[("de_master.CAMPAIGNNAME", "de_config.campaignname")],
                   filters=[("DE_CONFIG.active", "true")],
                   select=[("contactid", "de_master.contactid"), ("campaignname", "de_config.CampaignName")]))
    model = tables.build_table_model(package)

    assert model.entry_query.name == "Q_Entry"
    assert summary(model) == {
        ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        CONFIG: ({ROLE_SPLIT, ROLE_SOURCE}, True, LEVEL_SCENARIO),
        MASTER: ({ROLE_SPLIT, ROLE_SOURCE}, True, LEVEL_CONTACT),
    }
    assert groups(model) == [
        ("contact", [(ENTRY, "ContactId"), (MASTER, "ContactId")]),
        ("scenario", [(ENTRY, "CampaignName"), (CONFIG, "CampaignName"), (MASTER, "CampaignName")]),
    ]
    assert model.activation_field == (CONFIG, "ACTIVE")
    assert model.field_levels[(MASTER, "Optin")] == LEVEL_CONTACT
    assert model.field_levels[(CONFIG, "CHANNEL1")] == LEVEL_SCENARIO
    assert model.warnings == []


# ---------------------------------------------------------------------------
# Extensions beyond the pilot: entry source, row present or absent, missing relationship
# ---------------------------------------------------------------------------

FORM = "DE_Form"


def run_once_package(activities, kind=ENTRY_RUN_ONCE, extra_tables=(), relationships=None):
    """The shape of the new exports: a data extension entry (no automation, no query), the entry table
    sendable on SubscriberKey outside Contact Builder, and a form table linked to the contact by a
    group-to-set relationship on SubscriberKey."""
    table_list = [
        make_table(ENTRY, text("SubscriberKey", pk=True), text("Segment"), sendable_field="SubscriberKey"),
        make_table(FORM, text("SubscriberKey", pk=True), boolean("Consent"), sendable_field="SubscriberKey"),
        *extra_tables,
    ]
    if relationships is None:
        relationships = [relation(None, None, FORM, "SubscriberKey")]
    package = make_package(table_list, activities, relationships=relationships)
    package.entry_event = EntryEvent(key="EV", name="EV", table=ENTRY, automation_name=None, kind=kind,
                                     event_type="EmailAudience", schedule_mode="runOnce")
    return package


def journey_cond(field, operator, value=None):
    return Condition(table=ENTRY, field=field, operator=operator, value=value, source=SOURCE_JOURNEY)


@pytest.mark.parametrize("kind", [ENTRY_RUN_ONCE, ENTRY_OTHER])
def test_entry_not_fed_by_an_automation_needs_no_entry_query(sql, kind):
    model = tables.build_table_model(run_once_package([decision("SPLIT-1", journey_cond("Segment", "Equal", "A"))],
                                                      kind=kind))
    assert model.entry_query is None
    assert model.warnings == []
    assert "non popolata da alcuna query dell'export" in model.tables[ENTRY].reason


def test_automation_missing_from_the_export_is_named_as_such(sql):
    package = make_package([make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId")])
    package.entry_event = EntryEvent(key="EV", name="EV", table=ENTRY, automation_name=None, kind=ENTRY_AUTOMATION)
    model = tables.build_table_model(package)
    assert model.warnings == [
        "L'automation che alimenta l'entry event EV non è nell'export, e nessun'altra query dell'export popola "
        "DE_Entry: non c'è una entry query, quindi l'unica sorgente è la tabella di ingresso"]


@pytest.mark.parametrize("condition", [
    cond(FORM, "SubscriberKey", None, "IsNull"),
    cond(FORM, "subscriberkey", None, "IsNotNull"),
    Condition(table=FORM, field="SubscriberKey", operator="Equal", value=None,
              value_attribute="Event.EV.SubscriberKey"),
])
def test_existence_test_on_the_field_linked_to_the_contact_key(sql, condition):
    model = tables.build_table_model(run_once_package([decision("SPLIT-1", condition)]))
    # Spec, "Row present or absent": a contact-level table other than the entry table whose existence a
    # split tests gets a "row present / row absent" variable.
    assert model.existence_tested == {FORM: (FORM, "SubscriberKey")}
    assert model.tables[FORM].level == LEVEL_CONTACT
    assert model.tables[FORM].reason.endswith(
        "Lo split SPLIT-1 verifica se il contatto ha una riga in questa tabella tramite SubscriberKey, il campo "
        "collegato alla chiave del contatto: i contatti possono avere la riga o non averla.")


def test_existence_tests_name_every_split(sql):
    model = tables.build_table_model(run_once_package([
        decision("SPLIT-1", cond(FORM, "SubscriberKey", None, "IsNull")),
        decision("SPLIT-2", cond(FORM, "Consent")),
        decision("SPLIT-3", cond(FORM, "SubscriberKey", None, "IsNull")),
    ]))
    assert model.existence_tested == {FORM: (FORM, "SubscriberKey")}
    assert "Gli split SPLIT-1 e SPLIT-3 verificano se il contatto ha una riga" in model.tables[FORM].reason


@pytest.mark.parametrize("condition", [
    cond(FORM, "Consent", None, "IsNull"),                  # not the field linked to the contact key
    cond(FORM, "SubscriberKey", "TST", "Equal"),            # a value, not an existence test
    journey_cond("SubscriberKey", "IsNull"),                # the entry table: every contact has its row
])
def test_no_existence_test_for_other_conditions(sql, condition):
    model = tables.build_table_model(run_once_package([decision("SPLIT-1", condition)]))
    assert model.existence_tested == {}


def test_no_existence_test_behind_an_unsupported_outcome(sql):
    split = decision("SPLIT-1", cond(FORM, "SubscriberKey", None, "IsNull"))
    split.outcomes[0].unsupported_reason = "not evaluated"
    assert tables.build_table_model(run_once_package([split])).existence_tested == {}
    # the outcomes after an unsupported one are never evaluated either
    split = decision("SPLIT-1", cond(FORM, "Consent"), cond(FORM, "SubscriberKey", None, "IsNull"))
    split.outcomes[0].unsupported_reason = "not evaluated"
    assert tables.build_table_model(run_once_package([split])).existence_tested == {}


def test_no_existence_test_for_a_table_the_entry_query_joins(sql):
    package = pilot_like(sql)
    package.journey.activities["SPLIT-3"] = decision("SPLIT-3", cond(MASTER, "ContactId", None, "IsNull"),
                                                     cond(TARGET, "SalesforceId", None, "IsNotNull"))
    model = tables.build_table_model(package)
    # Spec: "No such variable for tables the entry query joins".
    assert model.tables[MASTER].level == LEVEL_CONTACT and ROLE_SOURCE in model.tables[MASTER].roles
    assert model.existence_tested == {}


def test_table_whose_attribute_set_has_no_relationship(sql):
    package = run_once_package([decision("SPLIT-1", cond(FORM, "Consent"))], relationships=[])
    package.journey.activities["SPLIT-1"].outcomes[0].unsupported_reason = "relazione mancante nell'export"
    package.missing_relationships = [FORM]
    model = tables.build_table_model(package)
    # Spec, "Missing relationship": its conditions are unsupported; the table is still read by a split.
    assert model.tables[FORM].selected and model.tables[FORM].level == LEVEL_SCENARIO
    assert "relazione mancante nell'export" in model.tables[FORM].reason
    assert "assunzione" not in model.tables[FORM].reason
    assert model.warnings == [
        "La tabella DE_Form è letta da uno split tramite un attribute set che non ha alcuna relazione nell'export "
        "(relazione mancante nell'export): le condizioni su questa tabella sono non supportate e le sue righe non "
        "sono collegate ai contatti"]
    assert model.existence_tested == {}


def test_journey_data_of_another_event_names_no_table(sql):
    other = Condition(table="DEAudience-2", field="Segment", operator="Equal", value="A", source=SOURCE_JOURNEY)
    split = decision("SPLIT-1", other)
    split.outcomes[0].unsupported_reason = "another entry event"
    model = tables.build_table_model(run_once_package([split]))
    assert model.warnings == []
    assert model.selected_tables == [ENTRY]


def test_journey_data_reads_the_entry_table(sql):
    model = tables.build_table_model(run_once_package([decision("SPLIT-1", journey_cond("Segment", "Equal", "A"))]))
    assert model.tables[ENTRY].roles == {ROLE_ENTRY, ROLE_SPLIT}
    assert model.field_levels[(ENTRY, "Segment")] == LEVEL_CONTACT


# ---------------------------------------------------------------------------
# Step 3: link tables, inferred paths, aliases, LEFT JOIN and managed sources
# ---------------------------------------------------------------------------

HL_ENTRY = "DE_HL_Entry"
HOLIDAY_ENTRY = "Holiday_Entry"
HOLIDAY = "Holiday_DE"


def holiday_package(with_paths=True):
    """The shape of the step 3 exports (examples/link_table_path): a Run Once entry sendable on SubscriberKey,
    Holiday_Entry linked to the contact by a group-to-set relationship on SubscriberKey, Holiday_DE linked to
    Holiday_Entry on HKey. A split reads Holiday_DE.HStart and a wait by attribute Holiday_DE.HEnd, both through
    Holiday_Entry (as loader.py fills ``Condition.path`` and ``Activity.wait_path``); a Master table linked to
    the contact on SubscriberKey is read by another split."""
    path = (HOLIDAY_ENTRY,) if with_paths else ()
    table_list = [
        make_table(HL_ENTRY, text("SubscriberKey", pk=True), text("EmailAddress"), sendable_field="SubscriberKey"),
        make_table(HOLIDAY_ENTRY, text("SubscriberKey", pk=True), text("HKey")),
        make_table(HOLIDAY, text("HKey"), date("HStart"), date("HEnd")),
        make_table(MASTER, text("SubscriberKey", pk=True), boolean("Optin")),
    ]
    activities = [
        decision("SPLIT-1", cond(HOLIDAY, "HStart", ";+;0;days", "After", path),
                 cond(HOLIDAY, "HStart", ";+;0;days", "AtOrBefore", path)),
        wait_on("WAIT-1", HOLIDAY, "HEnd", path),
        decision("SPLIT-2", cond(MASTER, "Optin")),
    ]
    relationships = [relation(None, None, HOLIDAY_ENTRY, "SubscriberKey"),
                     relation(HOLIDAY_ENTRY, "HKey", HOLIDAY, "HKey"),
                     relation(None, None, MASTER, "SubscriberKey")]
    package = make_package(table_list, activities, entry=HL_ENTRY, relationships=relationships)
    package.entry_event = EntryEvent(key="EV", name="EV", table=HL_ENTRY, automation_name=None, kind=ENTRY_RUN_ONCE)
    return package


def test_link_table_on_the_path_of_a_split_and_a_wait(sql):
    """Spec, step 3, "Tables reached through other tables": "Every table on the path from the contact to a read
    table is a link table: it gets a CSV, with the role 'link' ... the link table joined on the contact key is
    contact-level, the table it points to through another field is scenario-level ..., and the scenario key is
    written into the linking field of the link table." """
    model = tables.build_table_model(holiday_package())

    assert summary(model) == {
        HL_ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        MASTER: ({ROLE_SPLIT}, True, LEVEL_CONTACT),
        HOLIDAY: ({ROLE_SPLIT}, True, LEVEL_SCENARIO),
        HOLIDAY_ENTRY: ({ROLE_LINK}, True, LEVEL_CONTACT),
    }
    assert model.contact_tables == [HL_ENTRY, MASTER, HOLIDAY_ENTRY]
    assert model.scenario_tables == [HOLIDAY]
    assert groups(model) == [
        ("contact", [(HL_ENTRY, "SubscriberKey"), (MASTER, "SubscriberKey"), (HOLIDAY_ENTRY, "SubscriberKey")]),
        ("scenario", [(HOLIDAY, "HKey"), (HOLIDAY_ENTRY, "HKey")]),
    ]
    assert model.field_levels[(HOLIDAY_ENTRY, "HKey")] == LEVEL_SCENARIO
    assert model.field_levels[(HOLIDAY_ENTRY, "SubscriberKey")] == LEVEL_CONTACT
    assert {model.field_levels[(HOLIDAY, f)] for f in ("HKey", "HStart", "HEnd")} == {LEVEL_SCENARIO}
    assert model.tables[HOLIDAY_ENTRY].reason == (
        "Sul percorso tra il contatto e Holiday_DE letta da SPLIT-1 e WAIT-1: senza le sue righe il journey non può "
        "leggere Holiday_DE. A livello di contatto: collegata alla tabella di ingresso tramite la chiave del contatto "
        "(Holiday_Entry.SubscriberKey).")
    assert model.tables[HOLIDAY].reason.startswith("Letta da 1 split e da 1 Wait by attribute. A livello di scenario: "
                                                   "collegata tramite Holiday_Entry.HKey = Holiday_DE.HKey")
    assert model.existence_tested == {}
    assert model.aliases == []
    assert model.warnings == []


def test_a_path_given_by_the_wait_alone_is_enough(sql):
    """The split names no path, the wait does: Holiday_Entry is a link table from the wait's path, and the
    split's table is reached through it, so nothing has to be inferred."""
    package = holiday_package(with_paths=False)
    package.journey.activities["WAIT-1"].wait_path = (HOLIDAY_ENTRY,)
    model = tables.build_table_model(package)
    assert model.tables[HOLIDAY_ENTRY].roles == {ROLE_LINK}
    assert model.tables[HOLIDAY_ENTRY].reason.startswith(
        "Sul percorso tra il contatto e Holiday_DE letta da WAIT-1: senza le sue righe")
    assert model.tables[HOLIDAY].level == LEVEL_SCENARIO
    assert model.warnings == []


def test_link_table_that_is_also_read_keeps_its_roles(sql):
    """The pilot's Config is read through Master and the entry table (``path`` as loader.py fills it from the
    pilot export): both keep their roles; their reason says they are on the path too."""
    package = pilot_like(sql)
    path = (MASTER, ENTRY)
    package.journey.activities["SPLIT-1"] = decision("SPLIT-1", cond(CONFIG, "CHANNEL1", "Email", "Equal", path))
    package.journey.activities["WAIT-1"] = wait_on("WAIT-1", CONFIG, "WAIT1", path)
    before = tables.build_table_model(pilot_like(sql))
    model = tables.build_table_model(package)

    assert summary(model) == summary(before)
    assert groups(model) == groups(before)
    assert model.field_levels == before.field_levels
    assert model.warnings == []
    link = ("È anche sul percorso tra il contatto e DE_Config letta da SPLIT-1 e WAIT-1: senza le sue righe il "
            "journey non può leggere DE_Config.")
    assert model.tables[MASTER].reason == (
        f"Letta da 1 split e dalla entry query Q_Entry. {link} A livello di contatto: collegata alla tabella di "
        "ingresso tramite la chiave del contatto (DE_Master.ContactId).")
    assert model.tables[ENTRY].reason == (
        "Tabella di ingresso del journey, popolata dalla entry query Q_Entry; scritta anche da 1 attività Update "
        f"contact. {link} A livello di contatto: è la tabella di ingresso, una riga per contatto.")
    assert model.tables[CONFIG].reason == before.tables[CONFIG].reason


def test_written_link_table_and_unknown_path_table(sql):
    package = holiday_package()
    package.journey.activities["UPDATE-1"] = update("UPDATE-1", HOLIDAY_ENTRY, "HKey", "B")
    package.journey.activities["SPLIT-3"] = decision("SPLIT-3", cond(HOLIDAY, "HStart", None, "IsNull",
                                                                      ("DE_Ghost_Link", HOLIDAY_ENTRY)))
    model = tables.build_table_model(package)

    assert model.tables[HOLIDAY_ENTRY].roles == {ROLE_LINK}
    assert model.tables[HOLIDAY_ENTRY].reason.startswith(
        "Sul percorso tra il contatto e Holiday_DE letta da SPLIT-1, WAIT-1 e SPLIT-3: senza le sue righe il journey "
        "non può leggere Holiday_DE; scritta anche da 1 attività Update contact. A livello di contatto:")
    assert model.warnings == [
        "La tabella DE_Ghost_Link, sul percorso tra il contatto e Holiday_DE letta da SPLIT-3, non è nell'export: non "
        "è possibile crearne il CSV"]


def test_journey_data_names_no_path(sql):
    """Journey Data reads the entry row: a path on such a condition (loader.py leaves it empty) is not followed."""
    package = holiday_package()
    package.journey.activities["SPLIT-4"] = decision("SPLIT-4", Condition(
        table=HL_ENTRY, field="EmailAddress", operator="IsNull", value=None, source=SOURCE_JOURNEY,
        path=(MASTER,)))
    model = tables.build_table_model(package)
    assert model.tables[MASTER].roles == {ROLE_SPLIT}
    assert "percorso" not in model.tables[MASTER].reason


def test_inferred_chain_when_the_export_gives_no_path(sql):
    """Spec, step 3: "When the export gives no path for a read table that no selected table joins, the generator
    looks for the shortest chain of relationships from the contact to it and uses it with a warning"."""
    model = tables.build_table_model(holiday_package(with_paths=False))

    assert summary(model) == {
        HL_ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        MASTER: ({ROLE_SPLIT}, True, LEVEL_CONTACT),
        HOLIDAY: ({ROLE_SPLIT}, True, LEVEL_SCENARIO),
        HOLIDAY_ENTRY: ({ROLE_LINK}, True, LEVEL_CONTACT),
    }
    assert groups(model) == [
        ("contact", [(HL_ENTRY, "SubscriberKey"), (MASTER, "SubscriberKey"), (HOLIDAY_ENTRY, "SubscriberKey")]),
        ("scenario", [(HOLIDAY, "HKey"), (HOLIDAY_ENTRY, "HKey")]),
    ]
    assert model.warnings == [
        "La tabella Holiday_DE è raggiunta dal contatto attraverso Holiday_Entry (percorso dedotto dalle relazioni "
        "dell'export, non indicato dallo split)"]
    assert model.tables[HOLIDAY_ENTRY].reason.startswith(
        "Sul percorso tra il contatto e Holiday_DE letta da SPLIT-1 e WAIT-1 (percorso dedotto dalle relazioni "
        "dell'export, non indicato dallo split): senza le sue righe il journey non può leggere Holiday_DE.")
    assert "non collegata alla tabella di ingresso" not in model.tables[HOLIDAY].reason


def test_inferred_chain_is_the_shortest_and_follows_relationships_both_ways(sql):
    """Entry <- DE_L1 (ContactId) <- DE_L2 (Region) -> DE_X (Zone): the relationships point both ways along the
    chain. A longer chain DE_L1 -> DE_A -> DE_B -> DE_X is not taken. DE_L1 holds the contact key but has no CSV:
    it is on the path too."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table("DE_L1", text("ContactId", pk=True), text("Region"), text("Other")),
         make_table("DE_L2", text("Region", pk=True), text("Zone")),
         make_table("DE_A", text("Other", pk=True), text("Code")),
         make_table("DE_B", text("Code", pk=True), text("Zone")),
         make_table("DE_X", text("Zone", pk=True), boolean("Flag"))],
        [decision("SPLIT-1", cond("DE_X", "Flag"))],
        relationships=[relation("DE_L1", "ContactId", ENTRY, "ContactId"),
                       relation("DE_L1", "Other", "DE_A", "Other"),
                       relation("DE_A", "Code", "DE_B", "Code"),
                       relation("DE_B", "Zone", "DE_X", "Zone"),
                       relation("DE_L2", "Region", "DE_L1", "Region"),
                       relation("DE_L2", "Zone", "DE_X", "Zone")])
    model = tables.build_table_model(package)

    assert model.warnings == [
        "Nessuna query dell'automation di ingresso A_Entry popola la tabella di ingresso DE_Entry, e nessun'altra "
        "query dell'export popola DE_Entry: non c'è una entry query, quindi l'unica sorgente è la tabella di "
        "ingresso",
        "La tabella DE_X è raggiunta dal contatto attraverso DE_L1 e DE_L2 (percorso dedotto dalle relazioni "
        "dell'export, non indicato dallo split)",
    ]
    assert summary(model) == {
        ENTRY: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        "DE_A": (set(), False, None),
        "DE_B": (set(), False, None),
        "DE_L1": ({ROLE_LINK}, True, LEVEL_CONTACT),
        "DE_L2": ({ROLE_LINK}, True, LEVEL_SCENARIO),
        "DE_X": ({ROLE_SPLIT}, True, LEVEL_SCENARIO),
    }
    assert model.tables["DE_L2"].reason.startswith("Sul percorso tra il contatto e DE_X letta da SPLIT-1 (percorso "
                                                   "dedotto")
    assert model.field_levels[("DE_L1", "Region")] == LEVEL_SCENARIO
    assert model.field_levels[("DE_L1", "Other")] == LEVEL_CONTACT


def test_no_chain_keeps_the_earlier_rule(sql):
    """Spec, step 3: "with no chain at all, the earlier rule applies (one row shared by all contacts,
    assumption)". DE_Island is linked to a table no chain from the contact reaches."""
    package = holiday_package(with_paths=False)
    package.relationships = [relation(None, None, MASTER, "SubscriberKey")]
    package.tables["DE_Island"] = make_table("DE_Island", text("Code", pk=True))
    package.relationships.append(relation("DE_Island", "Code", HOLIDAY, "HKey"))
    model = tables.build_table_model(package)

    assert summary(model)[HOLIDAY_ENTRY] == (set(), False, None)
    assert summary(model)["DE_Island"] == (set(), False, None)
    assert model.tables[HOLIDAY].level == LEVEL_SCENARIO
    assert "non collegata alla tabella di ingresso" in model.tables[HOLIDAY].reason
    assert model.warnings == [
        "La tabella Holiday_DE non è collegata alla tabella di ingresso: è trattata come una riga condivisa da tutti "
        "i contatti (a livello di scenario). È un'assunzione: verifica come il journey collega questa tabella"]


def test_many_readers_are_counted_by_kind(sql):
    """Up to five readers are named by key; above five the link reason counts them by kind."""
    package = holiday_package()
    for n in range(2, 5):
        package.journey.activities[f"SPLIT-H{n}"] = decision(
            f"SPLIT-H{n}", cond(HOLIDAY, "HStart", None, "IsNull", (HOLIDAY_ENTRY,)))
    model = tables.build_table_model(package)
    assert model.tables[HOLIDAY_ENTRY].reason.startswith(
        "Sul percorso tra il contatto e Holiday_DE letta da SPLIT-1, WAIT-1, SPLIT-H2, SPLIT-H3 e SPLIT-H4: senza")

    package.journey.activities["WAIT-2"] = wait_on("WAIT-2", HOLIDAY, "HStart", (HOLIDAY_ENTRY,))
    model = tables.build_table_model(package)
    assert model.tables[HOLIDAY_ENTRY].reason.startswith(
        "Sul percorso tra il contatto e Holiday_DE letta da 4 split e 2 Wait by attribute: senza le sue righe il "
        "journey non può leggere Holiday_DE.")


def exit_criterion(formula, key="EXITCRITERIA", unsupported_reason=None):
    return JourneyFilter(key=key, name="Exit", kind="exit", formula=formula, unsupported_reason=unsupported_reason)


def test_table_read_only_by_an_exit_criterion_gets_a_csv(sql):
    """Team decision on spec step 3, "Exit criteria and goals": the tables an evaluable exit criterion reads
    count as split tables, so they get a CSV; the table on their path is a link table."""
    package = holiday_package()
    package.tables["DE_Contract"] = make_table("DE_Contract", text("SubscriberKey", pk=True), date("CancelDate"))
    package.relationships.append(relation(None, None, "DE_Contract", "SubscriberKey"))
    package.tables["DE_Promo"] = make_table("DE_Promo", text("HKey", pk=True), boolean("Active"))
    package.relationships.append(relation(HOLIDAY_ENTRY, "HKey", "DE_Promo", "HKey"))
    package.journey.exits = [exit_criterion(ConditionGroup(operator="OR", items=(
        cond("DE_Contract", "CancelDate", None, "IsNotNull"),
        cond("DE_Promo", "Active", "true", "Is", (HOLIDAY_ENTRY,)),
        cond(MASTER, "Optin", "false"))))]
    model = tables.build_table_model(package)

    contract, promo = model.tables["DE_Contract"], model.tables["DE_Promo"]
    assert (contract.roles, contract.selected, contract.level) == ({ROLE_SPLIT}, True, LEVEL_CONTACT)
    assert contract.reason == ("Letta dai criteri di uscita (exit criteria). A livello di contatto: collegata alla "
                               "tabella di ingresso tramite la chiave del contatto (DE_Contract.SubscriberKey).")
    assert (promo.roles, promo.selected, promo.level) == ({ROLE_SPLIT}, True, LEVEL_SCENARIO)
    assert model.tables[MASTER].reason.startswith("Letta da 1 split e dai criteri di uscita (exit criteria).")
    assert model.tables[HOLIDAY_ENTRY].reason.startswith(
        "Sul percorso tra il contatto e Holiday_DE letta da SPLIT-1 e WAIT-1, e tra il contatto e DE_Promo letta da "
        "EXITCRITERIA: senza le sue righe il journey non può leggere Holiday_DE e DE_Promo.")
    assert model.warnings == []


def test_exit_criteria_not_evaluated_read_nothing(sql):
    """An exit criterion the generator cannot evaluate is reported as not modelled (loader.py): it reads no table.
    A Journey Data condition of an exit criterion reads the entry table, as a split's does."""
    package = holiday_package()
    package.tables["DE_Contract"] = make_table("DE_Contract", text("SubscriberKey", pk=True), date("CancelDate"))
    package.journey.exits = [
        exit_criterion(cond("DE_Contract", "CancelDate", None, "IsNotNull"), unsupported_reason="operatore"),
        exit_criterion(Condition(table=HL_ENTRY, field="EmailAddress", operator="IsNull", value=None,
                                 source=SOURCE_JOURNEY), key="EXIT-2"),
    ]
    model = tables.build_table_model(package)
    assert summary(model)["DE_Contract"] == (set(), False, None)
    assert model.tables[HL_ENTRY].roles == {ROLE_ENTRY, ROLE_SPLIT}
    assert "letta anche dai criteri di uscita (exit criteria)" in model.tables[HL_ENTRY].reason


def test_entry_copies_are_aliases_and_keys_are_not(sql):
    """Spec, step 3, "Columns the entry query derives and columns it copies": a plain column copy is one variable
    with its source. The copies of ContactId and CampaignName are keys (key groups); FirstName is not a column of
    the entry table."""
    model = tables.build_table_model(pilot_like(sql))
    assert model.aliases == [[(ENTRY, "Emailaddress"), (MASTER, "Emailaddress")]]


def test_aliases_through_a_chain_of_queries_and_only_plain_columns(sql):
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("Email"), text("Label"), text("Code"),
                    sendable_field="ContactId"),
         make_table("DE_A", text("Id", pk=True), text("Email"), text("Name")),
         make_table("DE_B", text("Key", pk=True), text("Mail"), text("Name"))],
        queries=[query("Q2", "DE_A"), query("Q1", ENTRY)], automation=["Q1"])
    q1 = parsed("Q1", ENTRY, ["DE_A"], select=[("ContactId", "DE_A.Id"), ("Email", "DE_A.Email")])
    q1.select += [SelectItem(target_column="Label", expr=SelectLiteral(value="Gold")),
                  SelectItem(target_column="Code", expr=SelectConcat(parts=(SelectColumn(source=col("DE_A.Name")),)))]
    sql.add(q1)
    sql.add(parsed("Q2", "DE_A", ["DE_B"], select=[("Id", "DE_B.Key"), ("Email", "DE_B.Mail"), ("Name", "DE_B.Name")]))
    model = tables.build_table_model(package)

    assert groups(model) == [("contact", [(ENTRY, "ContactId"), ("DE_A", "Id"), ("DE_B", "Key")])]
    assert model.aliases == [[(ENTRY, "Email"), ("DE_A", "Email"), ("DE_B", "Mail")],
                             [("DE_A", "Name"), ("DE_B", "Name")]]


def test_left_join_source_says_it_is_read_as_an_inner_join(sql):
    """Spec, step 3: "A LEFT JOIN source still gets one row per contact (read as an inner join, as before); the
    report says so." """
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), text("Score"), sendable_field="ContactId"),
         make_table(MASTER, text("ContactId", pk=True)),
         make_table("DE_Score", text("ContactId", pk=True), text("Score"))],
        queries=[query("Q_Entry", ENTRY)], automation=["Q_Entry"])
    sql.add(parsed("Q_Entry", ENTRY, [MASTER, "DE_Score"], joins=[("DE_Master.ContactId", "DE_Score.ContactId")],
                   select=[("ContactId", "DE_Master.ContactId"), ("Score", "DE_Score.Score")], optional=["DE_Score"]))
    model = tables.build_table_model(package)

    score = model.tables["DE_Score"]
    assert (score.roles, score.selected, score.level) == ({ROLE_SOURCE}, True, LEVEL_CONTACT)
    assert score.reason == (
        "Letta dalla entry query Q_Entry. A livello di contatto: collegata alla tabella di ingresso tramite la chiave "
        "del contatto (DE_Score.ContactId). Unita con LEFT JOIN: le righe generate la trattano come inner join.")
    assert "LEFT JOIN" not in model.tables[MASTER].reason
    assert model.aliases == [[(ENTRY, "Score"), ("DE_Score", "Score")]]


def test_managed_table_keeps_its_csv_and_says_it_is_synchronized_from_the_crm(sql):
    """Spec, step 3, "Synchronized data extensions": the CSV is produced and imported like the others; the reason
    says the table is synchronized from the CRM. A managed table the journey does not read gets no CSV and no note."""
    package = make_package(
        [make_table(ENTRY, text("ContactId", pk=True), sendable_field="ContactId"),
         make_table("CampaignMember_Salesforce_3", text("Id", pk=True), text("ContactId"), text("Status"),
                    managed=True),
         make_table("Account_Salesforce", text("Id", pk=True), managed=True)],
        queries=[query("Q_Entry", ENTRY)], automation=["Q_Entry"])
    sql.add(parsed("Q_Entry", ENTRY, ["CampaignMember_Salesforce_3"], filters=[("CampaignMember_Salesforce_3.Status",
                                                                                "Responded")],
                   select=[("ContactId", "CampaignMember_Salesforce_3.ContactId")]))
    model = tables.build_table_model(package)

    member = model.tables["CampaignMember_Salesforce_3"]
    assert (member.roles, member.selected, member.level) == ({ROLE_SOURCE}, True, LEVEL_CONTACT)
    assert member.reason.endswith("Data extension sincronizzata dal CRM: il CSV si importa come gli altri e le "
                                  "righe arriveranno anche dalla sincronizzazione.")
    assert "non importabile" not in member.reason
    assert model.tables["Account_Salesforce"].reason == "Né letta né scritta da questo journey."


def test_date_offset_filter_is_not_an_activation_field(sql):
    package = _activation_package(sql, [])
    sql.answers["Q_Entry"].filters = [DateOffsetFilter(column=col("DE_Config.ACTIVE"), days=0),
                                      DateOffsetFilter(column=col("DE_Config.Label"), days=-1)]
    model = tables.build_table_model(package)

    assert model.tables[CONFIG].level == LEVEL_SCENARIO
    assert model.activation_field is None
    assert "campo di attivazione" not in model.tables[CONFIG].reason


# ---------------------------------------------------------------------------
# The pilot export, end to end (needs loader.py and sqlparse_lite.py)
# ---------------------------------------------------------------------------

def test_pilot_export():
    if not SQLPARSE_WRITTEN:
        pytest.skip("cjhelper.sqlparse_lite is not written yet")
    loader = pytest.importorskip("cjhelper.loader")
    package = loader.load_package(PILOT)
    model = tables.build_table_model(package)

    entry, master = "DE_NCM_AdHoc_Entry", "SF_Master_DataExtension_DE"
    target, config = "DE_NCRM_AdHoc_Target", "DE_NCRM_AD_HOC_CONFIG"
    assert summary(model) == {
        entry: ({ROLE_ENTRY}, True, LEVEL_CONTACT),
        master: ({ROLE_SPLIT, ROLE_SOURCE}, True, LEVEL_CONTACT),
        target: ({ROLE_SOURCE}, True, LEVEL_CONTACT),
        config: ({ROLE_SPLIT, ROLE_SOURCE}, True, LEVEL_SCENARIO),
        "DE_SkyDE_AdHoc_Log_partial": ({ROLE_WRITTEN}, False, None),
        "DE_NCRM_AdHoc_Entry_Backup": ({ROLE_WRITTEN}, False, None),
        "DE_Check_Active_Campaigns": ({ROLE_WRITTEN}, False, None),
    }
    assert model.entry_query.name == "Q_SKYDE_AdHoc_Target"
    assert [q.name for q in model.source_queries] == [
        "Q_SKYDE_AdHoc_Target", "Q_SKYDE_NCRM_AD_HOC_CONFIGURATION_UPDATE"]
    assert model.contact_key_field == (entry, "ContactId")
    assert groups(model) == [
        ("contact", [(entry, "ContactId"), (target, "SalesforceId"), (master, "ContactId")]),
        ("scenario", [(entry, "CampaignName"), (config, "CampaignName"), (target, "Description")]),
    ]
    assert model.field_levels[(target, "Description")] == LEVEL_SCENARIO
    assert model.field_levels[(target, "SalesforceId")] == LEVEL_CONTACT
    assert {model.field_levels[(config, f)] for f in fields_of(package, config)} == {LEVEL_SCENARIO}
    assert {model.field_levels[(master, f)] for f in fields_of(package, master)} == {LEVEL_CONTACT}
    assert model.activation_field == (config, "ACTIVE")
    assert model.contact_tables == [entry, target, master]
    assert model.scenario_tables == [config]
    assert not any("non è collegata alla tabella di ingresso" in w for w in model.warnings)
    # the pilot never tests whether a row exists (its IsNull conditions read IsDunning of a source table)
    assert model.existence_tested == {}
    # Step 3: the entry query's plain copies of Master columns the entry table has, keys excluded (ContactId,
    # CampaignName); FirstName, LastName... are not entry columns, ActionId is a CONCAT.
    assert model.aliases == [[(entry, name), (master, name)]
                             for name in ("Emailaddress", "AccountId", "AccountStatus", "MobilePhone")]
    # Config is read through Master and the entry table (loader.py's paths): they keep their roles, and their
    # reason says they are on the path; no chain is inferred.
    if any(c.path for a in package.journey.activities.values() for o in a.outcomes
           for c in formula_conditions(o.formula)):
        assert "È anche sul percorso tra il contatto e DE_NCRM_AD_HOC_CONFIG" in model.tables[master].reason
        assert "È anche sul percorso tra il contatto e DE_NCRM_AD_HOC_CONFIG" in model.tables[entry].reason
    assert not any("percorso dedotto" in w for w in model.warnings)
