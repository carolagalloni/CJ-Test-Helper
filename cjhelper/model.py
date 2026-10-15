"""Shared data model for every module of the generator.

This file is the contract between modules. Every module imports from here and
nothing here imports from the other modules. Keep it plain: dataclasses, type
aliases and small helpers only.

Naming used throughout:

- *table*: a Data Extension, always referred to by its name (``Table.name``).
- *field*: a column of a table, referred to by ``FieldRef = (table_name, field_name)``.
- *branch*: one outcome of one activity, referred to by ``BranchRef = (activity_key, outcome_key)``.
- *value*: every generated or compared value is a ``str`` in CSV form, or ``None`` for null.
  Booleans are ``"True"`` / ``"False"``; dates follow ``Settings.date_format``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional, Union

FieldRef = tuple[str, str]
BranchRef = tuple[str, str]
Value = Optional[str]


# ---------------------------------------------------------------------------
# Tables and relationships (loader.py fills these from the export)
# ---------------------------------------------------------------------------

FIELD_TYPES = ("Text", "Boolean", "Date", "EmailAddress", "Phone", "Number", "Decimal", "Locale")


@dataclass(frozen=True)
class Field:
    name: str
    type: str                      # one of FIELD_TYPES as written in the export
    length: Optional[int]          # None when the type has no length
    is_primary_key: bool
    is_nullable: bool
    default: Optional[str]         # defaultValue as written in the export, e.g. "False", "GETDATE()"
    ordinal: int
    scale: Optional[int] = None    # Decimal: digits after the decimal point ("scale" in the export); None otherwise


@dataclass
class Table:
    id: str                        # entity id in the export
    name: str
    customer_key: Optional[str]
    fields: list[Field]
    is_sendable: bool = False
    sendable_field: Optional[str] = None   # sendableCustomObjectField: the field holding the contact key
    # Spec, step 3, "Synchronized data extensions": a managed data extension (Package Manager issue NO_DEPLOY /
    # ManagedDataExtension, the Salesforce objects synchronized from the CRM): its CSV is imported like the others and
    # its rows also arrive from the sync.
    managed: bool = False

    def field(self, name: str) -> Optional[Field]:
        """Case-insensitive lookup, because SQL and split criteria do not always match the schema's case."""
        index = self.__dict__.get("_by_lower")
        if index is None or len(index) != len(self.fields):
            index = {f.name.lower(): f for f in self.fields}
            self.__dict__["_by_lower"] = index
        return index.get(name.lower())

    @property
    def primary_key(self) -> list[str]:
        return [f.name for f in self.fields if f.is_primary_key]

    @property
    def field_names(self) -> list[str]:
        return [f.name for f in self.fields]


@dataclass(frozen=True)
class Relationship:
    """A link between two tables from attributeRelationships, on one pair of fields.

    Group-to-set links (the contact model itself pointing at a table) have
    ``left_table`` set to None and ``right_field`` naming the contact key field.
    """
    name: str
    left_table: Optional[str]
    left_field: Optional[str]
    right_table: str
    right_field: str
    left_cardinality: str          # "One" / "Many" as written
    right_cardinality: str


@dataclass
class EmailAsset:
    id: str
    name: str
    subject: str
    has_link: bool                 # True when the HTML or text view holds an href


# ---------------------------------------------------------------------------
# Split criteria (criteria.py produces these)
# ---------------------------------------------------------------------------

# Where a condition reads its value (spec, "Extensions beyond the pilot", "Data sources of a condition").
SOURCE_CONTACT = "contact"   # Contact Data, <attribute set>.<field>: read live when the contact reaches the split
SOURCE_JOURNEY = "journey"   # Journey Data, Event.<eventDefinitionKey>.<field>: the entry row, a snapshot taken at entry


@dataclass(frozen=True)
class RelativeDate:
    """A condition value written ``;<sign>;<n>;<unit>`` (spec, step 3, "Relative dates in conditions"): the
    clock's date moved by ``amount`` units in the direction of ``sign``. ``unit`` is kept as written, lower
    case (``days``, ``weeks``, ``months``, ``years``, ``hours``, ``minutes``)."""
    sign: int                      # +1 or -1
    amount: int
    unit: str


@dataclass(frozen=True)
class Condition:
    """One atomic condition: ``table.field operator value``.

    As criteria.py writes it, ``table`` is the attribute-set name of the key (Contact Data) or the
    event definition key of ``Event.<key>.<field>`` (Journey Data); loader.py maps it to the table
    name before anything else sees it (the trigger's entry table for Journey Data). ``value`` is None
    for operators without a value (IsNull, IsNotNull) and when the value is another attribute.

    ``source`` keeps the data source (SOURCE_CONTACT or SOURCE_JOURNEY), so that the snapshot rule of
    Journey Data can be applied: Update contact writes never change what a Journey Data condition reads.

    ``value_attribute`` is set when the condition compares the field with another attribute
    (``ValueParameterName``, or ``ValueIsReference``) instead of a literal, written as in the export
    (``Event.<key>.<field>`` or ``<attribute set>.<field>``). The only supported form is "the field
    linked to the contact key of a contact-level table Equal the contact key", which loader.py
    normalises so that ``table.field`` is that linked field: the condition reads "the contact has a row
    in ``table``" (criteria.evaluate_condition: true when the field holds a value). Any other form is
    reported as unsupported by loader.py, never read as a comparison with an empty value.
    """
    table: str
    field: str
    operator: str                  # as written: Is, Equal, IsNull, NotEqual, Between, ...
    value: Value                   # Between: "low|high" as written
    source: str = SOURCE_CONTACT
    value_attribute: Optional[str] = None
    # Spec, step 3: a relative date value, parsed by criteria.py (``value`` keeps the text as written).
    relative: Optional[RelativeDate] = None
    # Spec, step 3, "Tables reached through other tables": the tables between the contact and ``table``, nearest
    # the contact first, excluding ``table`` itself; empty when the export gives no path. As criteria.py writes it,
    # ``path`` holds the RELATIONSHIP IDS of the condition (``groupToSetRelationshipId`` of ``UiMetaData``, then the
    # ``RelationshipID`` of each ``<AttributePath>`` wrapper); loader.py replaces them with the table names.
    path: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConditionGroup:
    operator: Literal["AND", "OR"]
    items: tuple["Formula", ...]


Formula = Union[Condition, ConditionGroup]


def formula_conditions(formula: Optional[Formula]) -> list[Condition]:
    """All atomic conditions of a formula, in document order."""
    if formula is None:
        return []
    if isinstance(formula, Condition):
        return [formula]
    out: list[Condition] = []
    for item in formula.items:
        out.extend(formula_conditions(item))
    return out


def formula_text(formula: Optional[Formula]) -> str:
    """Readable rendering, e.g. ``(A.x Is 'true' AND (A.y Is 'false' OR A.y IsNull))``."""
    if formula is None:
        return "<remainder>"
    if isinstance(formula, Condition):
        if formula.value_attribute is not None:
            return f"{formula.table}.{formula.field} {formula.operator} {formula.value_attribute}"
        if formula.value is None:
            return f"{formula.table}.{formula.field} {formula.operator}"
        return f"{formula.table}.{formula.field} {formula.operator} {formula.value!r}"
    inner = f" {formula.operator} ".join(formula_text(i) for i in formula.items)
    return f"({inner})"


# ---------------------------------------------------------------------------
# Journey (loader.py builds it, graph.py walks it)
# ---------------------------------------------------------------------------

# Normalised activity kinds. The raw export type is kept in Activity.raw_type.
KIND_DECISION = "decision"           # MULTICRITERIADECISION
KIND_ENGAGEMENT = "engagement"       # ENGAGEMENTDECISION
KIND_RANDOM = "random"               # RANDOMSPLIT
KIND_WAIT_DURATION = "wait_duration" # WAIT with uiType WAITBYDURATION
KIND_WAIT_ATTRIBUTE = "wait_attribute"  # WAIT with uiType WAITBYATTRIBUTE
KIND_WAIT_STO = "wait_sto"           # STOWAIT (send-time optimisation)
KIND_EMAIL = "email"                 # EMAILV2
KIND_UPDATE = "update"               # UPDATECONTACTDATA
KIND_SMS = "sms"                     # SMSSYNC: a send to the journey's default mobile number (spec, step 3)
KIND_UNSUPPORTED = "unsupported"     # anything else: reported, never guessed

# The one lookup table to extend for new activity types (spec: "one lookup table each").
ACTIVITY_KINDS: dict[str, str] = {
    "MULTICRITERIADECISION": KIND_DECISION,
    "ENGAGEMENTDECISION": KIND_ENGAGEMENT,
    "RANDOMSPLIT": KIND_RANDOM,
    "WAIT": KIND_WAIT_DURATION,       # refined to wait_attribute by loader when uiType says so
    "STOWAIT": KIND_WAIT_STO,
    "EMAILV2": KIND_EMAIL,
    "UPDATECONTACTDATA": KIND_UPDATE,
    "SMSSYNC": KIND_SMS,
}


@dataclass
class Outcome:
    key: str
    label: str                     # metaData.label, or a fallback such as the key
    next: Optional[str]            # key of the next activity, None = journey exit
    formula: Optional[Formula] = None   # decision split only; None for the remainder outcome
    is_remainder: bool = False     # decision split: taken when no earlier outcome matched
    percentage: Optional[int] = None    # random split only
    when: Optional[bool] = None    # engagement split only: True = Yes branch, False = No branch
    description: str = ""          # metaData.criteriaDescription when present
    unsupported_reason: Optional[str] = None   # decision outcome the PoC cannot evaluate (operator, unknown field)


@dataclass
class Write:
    """One field written by an update-contact activity."""
    table: str
    field: str
    value: Value                   # literal as written; "{{DateTime.Now}}" means the current time


@dataclass
class Activity:
    key: str
    raw_type: str
    kind: str                      # one of the KIND_* constants
    name: str
    outcomes: list[Outcome]
    # kind-specific details, all optional:
    wait_amount: Optional[int] = None         # wait_duration; wait_sto: the sliding window (longest delay)
    wait_unit: Optional[str] = None           # wait_duration: MINUTES, HOURS, DAYS, ...; wait_sto: HOURS
    wait_field: Optional[FieldRef] = None     # wait_attribute: (table, field) holding the date
    wait_timezone: Optional[str] = None
    # Spec, step 3, "The clock": a wait by attribute ends at the field's date plus this offset (``waitDuration``
    # and ``waitUnit`` of the activity, the amount possibly negative: "13 days before" is -13 DAYS), at the time
    # of day ``wait_time_of_day`` (``specifiedTime``) when it is given, else at the time held in the field.
    wait_offset_amount: Optional[int] = None
    wait_offset_unit: Optional[str] = None
    wait_time_of_day: Optional[str] = None    # "HH:MM" as written, None or "" when not given
    # Spec, step 3, "Tables reached through other tables": the attribute sets between the contact and the
    # wait's table, as in Condition.path (from the segments of ``waitEndDateAttributeExpression``).
    wait_path: tuple[str, ...] = ()
    sms_asset_id: Optional[str] = None        # sms: configurationArguments.assetId as written
    sms_name: Optional[str] = None            # sms: the activity name
    email_asset_id: Optional[str] = None      # email
    email_name: Optional[str] = None          # email: asset name, or activity name as fallback
    engagement_kind: Optional[str] = None     # engagement: "open" or "click"
    engagement_ref: Optional[str] = None      # engagement: key of the referenced email activity
    writes: list[Write] = field(default_factory=list)   # update
    unsupported_reason: Optional[str] = None  # unsupported: why, in plain words (unknown type, operator, field...)

    def outcome(self, key: str) -> Optional[Outcome]:
        for o in self.outcomes:
            if o.key == key:
                return o
        return None


@dataclass
class JourneyFilter:
    """An exit criterion (or a goal) of the journey (spec, step 3, "Exit criteria and goals"): its formula, mapped
    like a decision split's, or None with ``unsupported_reason`` when the generator cannot evaluate it. Exit
    criteria are evaluated at the end of every wait, with the clock (documented)."""
    key: str                       # the export key, e.g. EXITCRITERIA
    name: str
    kind: str                      # "exit" or "goal"
    formula: Optional[Formula]
    description: str = ""          # metaData.criteriaDescription when present
    unsupported_reason: Optional[str] = None


@dataclass
class Journey:
    name: str
    key: str
    activities: dict[str, Activity]      # by activity key, in export order
    start_key: Optional[str]             # the activity no outcome points to
    entry_event_key: Optional[str]       # eventDefinitionKey of the trigger
    entry_table: Optional[str]           # table bound to the entry trigger
    default_email_event_key: Optional[str] = None   # event key used by defaults.email, for the anomaly check
    default_email_field: Optional[str] = None       # entry table field the journey sends emails to (defaults.email)
    activity_count: int = 0
    entry_mode: Optional[str] = None     # entryMode as written: MultipleEntries, OnceAndDone, SingleEntryAcrossAllVersions
    goal_count: int = 0                  # goals defined on the journey: not modelled, reported by loader.py
    exit_count: int = 0                  # exit criteria defined on the journey (see ``exits``)
    # Spec, step 3: the exit criteria, parsed; graph.py evaluates the supported ones at the end of every wait.
    exits: list[JourneyFilter] = field(default_factory=list)
    goals: list[JourneyFilter] = field(default_factory=list)       # parsed, reported, not modelled
    default_mobile_field: Optional[str] = None      # entry table field the journey sends SMS to (defaults.mobileNumber)

    def branches(self) -> list[tuple[Activity, Outcome]]:
        return [(a, o) for a in self.activities.values() for o in a.outcomes]


# ---------------------------------------------------------------------------
# Queries (loader.py gives the raw SQL, sqlparse_lite.py parses it)
# ---------------------------------------------------------------------------

@dataclass
class Query:
    id: str
    name: str
    target_table: str
    sql: str                       # with {{mcpm#...}} references already resolved to table names
    update_type: str               # Update, Overwrite, Append as written


@dataclass(frozen=True)
class ColumnRef:
    table: str                     # table name (alias already resolved); "" when it could not be resolved
    column: str


@dataclass(frozen=True)
class TableRef:
    table: str
    alias: Optional[str]
    optional: bool = False         # LEFT JOIN source: the row may be absent (read as an inner join, reported)


@dataclass(frozen=True)
class JoinEquality:
    left: ColumnRef
    right: ColumnRef


@dataclass(frozen=True)
class EqualityFilter:
    column: ColumnRef
    value: str                     # literal as written, without quotes


@dataclass(frozen=True)
class IsNullFilter:
    column: ColumnRef
    negated: bool = False


@dataclass(frozen=True)
class DateBoundFilter:
    """``now >= column`` (bound="start") or ``now <= column`` (bound="end")."""
    column: ColumnRef
    bound: Literal["start", "end"]


@dataclass(frozen=True)
class DateOffsetFilter:
    """``CAST(column AS DATE) = CAST(DATEADD(day, days, GETDATE()) AS DATE)``: the column's date part is the
    current date moved by ``days`` (spec, step 3, "Columns the entry query derives and columns it copies")."""
    column: ColumnRef
    days: int


@dataclass(frozen=True)
class UnsupportedFilter:
    text: str


Filter = Union[EqualityFilter, IsNullFilter, DateBoundFilter, DateOffsetFilter, UnsupportedFilter]


@dataclass(frozen=True)
class SelectColumn:
    source: ColumnRef


@dataclass(frozen=True)
class SelectLiteral:
    value: str


@dataclass(frozen=True)
class SelectNull:
    """``NULL AS x``: the entry column is empty."""


@dataclass(frozen=True)
class SelectCurrentDate:
    format: Optional[str]          # .NET format string from FORMAT(GETDATE(), '...'), None for a plain date


@dataclass(frozen=True)
class SelectConcat:
    parts: tuple["SelectExpr", ...]


@dataclass(frozen=True)
class SelectUnsupported:
    text: str


SelectExpr = Union[SelectColumn, SelectLiteral, SelectNull, SelectCurrentDate, SelectConcat, SelectUnsupported]


@dataclass(frozen=True)
class SelectItem:
    target_column: str             # alias, or the source column name
    expr: SelectExpr


@dataclass
class ParsedQuery:
    name: str
    target_table: str
    sources: list[TableRef]        # FROM and JOIN tables
    joins: list[JoinEquality]
    filters: list[Filter]          # WHERE, split on AND; OR groups become UnsupportedFilter
    select: list[SelectItem]
    select_all: bool = False       # SELECT *
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The whole export
# ---------------------------------------------------------------------------

# Entry source kinds (spec, "Extensions beyond the pilot", "Entry source and procedure"), read from the
# event definition's type and metaData.scheduleFlowMode by loader.py.
ENTRY_AUTOMATION = "automation"      # data extension fed and scheduled by an automation (the pilot)
ENTRY_RUN_ONCE = "run_once"          # data extension, Run Once: only the rows present at activation enter
ENTRY_SCHEDULED = "scheduled"        # data extension, recurring schedule (or a schedule mode not recognised)
ENTRY_OTHER = "other"                # CloudPages form, API event or any other: not driven by a CSV import


@dataclass
class EntryEvent:
    key: str
    name: str
    table: str
    automation_name: Optional[str]           # the feeding automation; only for an ENTRY_AUTOMATION entry
    kind: str = ENTRY_AUTOMATION             # one of ENTRY_*
    event_type: str = ""                     # type as written: AutomationAudience, EmailAudience, CloudPages...
    schedule_mode: Optional[str] = None      # metaData.scheduleFlowMode as written: automation, runOnce, ...
    run_once_mode: Optional[str] = None      # metaData.runOnceScheduleMode as written: onPublish, ...
    entry_filter: Optional[str] = None       # entry filter criteria as written, when not empty (not modelled)


@dataclass
class Package:
    name: str
    journey: Journey
    tables: dict[str, Table]                 # by table name
    relationships: list[Relationship]
    queries: list[Query]
    entry_event: Optional[EntryEvent]
    emails: dict[str, EmailAsset]            # by asset id
    automation_queries: dict[str, list[str]] = field(default_factory=dict)  # automation name -> query names in step order
    warnings: list[str] = field(default_factory=list)   # anything odd met while reading, in plain words
    unsupported_activities: list[str] = field(default_factory=list)  # keys of activities with an unknown type
    missing_relationships: list[str] = field(default_factory=list)   # tables whose attribute set, read by a split,
                                                                      # has no relationship in the export


# ---------------------------------------------------------------------------
# Settings (defaults from the spec's settings table)
# ---------------------------------------------------------------------------

@dataclass
class Settings:
    contacts_per_case: int = 3
    contacts_per_random_case: int = 50
    real_inbox: str = ""                     # "name@domain"; empty = none
    real_phone: str = ""
    fake_phone: str = "333333333333"
    window_start_days: int = -1              # validity window starts yesterday
    window_end_days: int = 7                 # and ends in 7 days
    wait_offset_hours: int = 0
    wait_offset_before_engagement_hours: int = 24
    date_format: str = "%Y-%m-%d %H:%M"      # shown in the app as YYYY-MM-DD HH:MM
    one_active_scenario: Optional[bool] = None   # None = on when an activation field is detected
    activation_field: Optional[FieldRef] = None  # None = suggested from the entry query filters
    run_id: str = "R01"
    case_insensitive_text: bool = False
    seed: int = 20261011
    generation_time: Optional[datetime] = None   # None = now; tests pass a fixed value
    # Spec, step 3: the clock starts at the entry time (None = the generation time); values chosen relative to
    # it must stay valid for at least this many days of late activation where a class of values allows it.
    entry_time: Optional[datetime] = None
    min_validity_days: int = 1

    def contact_key(self, n: int) -> str:
        return f"TST_{self.run_id}_C{n:04d}"

    def scenario_key(self, n: int) -> str:
        return f"TST_{self.run_id}_S{n:02d}"


# ---------------------------------------------------------------------------
# Table selection and field levels (tables.py produces these)
# ---------------------------------------------------------------------------

ROLE_ENTRY = "entry"
ROLE_SPLIT = "split"
ROLE_SOURCE = "source"
ROLE_WRITTEN = "written"
ROLE_LINK = "link"               # spec, step 3: on the path from the contact to a table a split or a wait reads

LEVEL_CONTACT = "contact"
LEVEL_SCENARIO = "scenario"


@dataclass
class TableRole:
    name: str
    roles: set[str]                  # subset of ROLE_*
    selected: bool                   # gets a CSV
    level: Optional[str]             # LEVEL_CONTACT / LEVEL_SCENARIO for selected tables
    reason: str                      # plain words for the report


@dataclass
class KeyGroup:
    """Fields that must hold the same generated key, across tables."""
    kind: str                        # "contact", "scenario" or "other"
    fields: list[FieldRef]


@dataclass
class TableModel:
    entry_table: str
    contact_key_field: FieldRef                 # (entry table, its contact key field)
    tables: dict[str, TableRole]                # every table of the export
    key_groups: list[KeyGroup]
    field_levels: dict[FieldRef, str]           # for every field of every selected table
    entry_query: Optional[ParsedQuery]          # the query that fills the entry table, parsed
    source_queries: list[ParsedQuery]           # entry query plus the queries feeding its sources, in order
    scenario_tables: list[str]
    contact_tables: list[str]
    activation_field: Optional[FieldRef]        # suggested from the entry query filters
    warnings: list[str] = field(default_factory=list)
    # Contact-level tables, other than the entry table and the tables the entry queries read, whose
    # existence a split tests (IsNull / IsNotNull on the field linked to the contact key, or the supported
    # attribute comparison): table -> that linked field. Each gets a "row present / row absent" variable.
    existence_tested: dict[str, FieldRef] = field(default_factory=dict)
    # Spec, step 3, "Columns the entry query derives and columns it copies": groups of fields that hold the same
    # value on every row because a followed query copies one into the other as a plain column (the entry
    # table's copy of a source column). Key fields are in ``key_groups`` instead. The coverage treats each
    # group as one variable; datagen writes the same value in every member.
    aliases: list[list[FieldRef]] = field(default_factory=list)

    @property
    def selected_tables(self) -> list[str]:
        return [t.name for t in self.tables.values() if t.selected]


# ---------------------------------------------------------------------------
# Coverage (coverage.py produces these; datagen.py and outputs.py consume them)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Step:
    activity_key: str
    outcome_key: Optional[str]       # None for the last activity when the journey exits there


@dataclass(frozen=True)
class TimelinePoint:
    """The clock when a contact executes one step of its path (spec, step 3, "The clock"): one per Step, same
    order. ``at`` is the clock as the contact reaches the activity; for a wait it is the moment the wait ends."""
    activity_key: str
    outcome_key: Optional[str]
    at: datetime


@dataclass(frozen=True)
class ManualAction:
    activity_key: str                # the engagement split
    kind: str                        # "open" or "click"
    email_activity_key: str          # the email to act on
    email_name: str


@dataclass(frozen=True)
class RandomBranch:
    outcome_key: str
    percentage: int
    steps: tuple[Step, ...]          # the path taken after this outcome
    emails_sent: tuple[str, ...]
    writes: tuple[Write, ...]
    end_activity_key: Optional[str]
    unknown_from: Optional[str] = None   # as Case.unknown_from, for this branch's path
    unknown_reason: str = ""
    timeline: tuple[TimelinePoint, ...] = ()   # spec, step 3: the clock along this branch's steps
    exit_by_criteria: Optional[str] = None     # the wait at whose end the exit criteria take the contact out


@dataclass
class ActionWindow:
    """The time a tester has for one manual open or click (spec, "Extensions beyond the pilot", "Honest
    outcomes": "The deadline of a manual open or click is computed from the waits between the email and the
    engagement split, with a warning when the window is under one hour").

    coverage.py fills the waits and their fixed duration; datagen.py fills ``deadline``, ``short`` and
    ``email_not_before``, because the date of a wait by attribute is the value it writes. Without a wait by
    attribute the deadline is relative: ``minutes`` after the email arrives. The window starts when the email
    can arrive, so the waits before the email count too (``wait_keys_before``).
    """
    activity_key: str                    # the engagement split
    email_activity_key: str
    wait_keys: list[str]                 # waits between the email and the split, in path order
    minutes: Optional[int]               # fixed duration of those waits (send-time waits count 0); None = unknown
    attribute_wait_key: Optional[str] = None   # the last wait by attribute among them: the window ends at its date
    minutes_after_attribute_wait: Optional[int] = 0   # plus the fixed duration of the waits after it
    deadline: Optional[str] = None       # when the contacts reach the split at the earliest (datagen)
    short: bool = False                  # the window is under one hour (datagen)
    wait_keys_before: list[str] = field(default_factory=list)   # waits on the path before the email, in order
    email_not_before: Optional[str] = None   # the earliest arrival of the email, when the waits before it delay
                                             # it past the generation time (datagen)


@dataclass
class RandomInfo:
    activity_key: str
    branches: list[RandomBranch]
    expected_low: int                # accepted range for the smallest side, inclusive
    expected_high: int


@dataclass
class ContactClass:
    id: int
    values: dict[FieldRef, Value]    # representative contact-level values
    predicate_truth: tuple[bool, ...]
    member_count: int                # how many contact-level value combinations it stands for


@dataclass
class Case:
    id: str                          # "S01-C01"
    scenario_id: str                 # "S01"
    class_id: int
    contact_values: dict[FieldRef, Value]
    steps: list[Step]                # deterministic part of the path, start to end (or to the random split)
    emails_sent: list[str]           # email activity keys, in order
    writes: list[Write]
    waits: list[str]                 # activity keys of waits crossed, in order
    manual_actions: list[ManualAction]
    end_activity_key: Optional[str]
    contact_count: int
    random: Optional[RandomInfo] = None     # set for the statistical case
    contact_keys: list[str] = field(default_factory=list)   # filled by datagen
    summary: str = ""                # short path summary for the testbook title
    inbox_blocked_reason: Optional[str] = None   # manual action impossible: a split fixes the address
    # Spec, "Honest outcomes": "Past an unsupported element the expected result says the path is unknown from
    # that activity on". The activity where the simulated path stopped without reaching an exit (an unsupported
    # activity or branch, an operator the PoC cannot evaluate...), and why; None when the path is known to the end.
    unknown_from: Optional[str] = None
    unknown_reason: str = ""
    action_windows: list[ActionWindow] = field(default_factory=list)   # one per manual action, same order
    # Spec, "Honest outcomes": "The testbook gives each case's minimum duration." Fixed duration waits on the
    # path, in minutes (send-time waits and waits by attribute count 0: the date of the last wait by attribute is
    # ``not_before``); for a statistical case the longest of its branches. None when a duration is unknown.
    min_duration_minutes: Optional[int] = 0
    not_before: Optional[str] = None     # the case cannot end before this date: the last wait by attribute on
                                         # its path plus the fixed waits after it (datagen); None without one
    # Spec, step 3, "The clock": the clock at every step of the expected path (one TimelinePoint per Step).
    timeline: list[TimelinePoint] = field(default_factory=list)
    # Spec, step 3, "Exit criteria and goals": the wait at whose end the exit criteria take the contact out of
    # the journey (the expected path ends there), None when the contact reaches an exit of the canvas.
    exit_by_criteria: Optional[str] = None
    # Spec, step 3, "Validity of the data": how many whole days the activation can slip before a value of this
    # case's rows is read differently by a split or a wait; None when nothing in the case depends on the clock.
    validity_days: Optional[int] = None
    sms_sent: list[str] = field(default_factory=list)   # SMS activity keys on the path, in order


@dataclass
class Scenario:
    id: str                          # "S01"
    key: str                         # "TST_R01_S01", filled by datagen
    values: dict[FieldRef, Value]    # every scenario-level field
    cases: list[Case]
    run_order: int
    has_manual_action: bool
    is_statistical: bool = False


STATUS_COVERED = "covered"
STATUS_MANUAL = "covered with manual action"
STATUS_NOT_COVERED = "not covered"


@dataclass
class BranchStatus:
    activity_key: str
    activity_name: str
    activity_kind: str
    outcome_key: str
    outcome_label: str
    status: str                      # STATUS_*
    reason: str                      # why, in plain words (always set for not covered)
    covered_by: list[str]            # case ids
    statistical: bool = False        # covered only through a random split
    behind: Optional[BranchRef] = None   # not covered: the nearest reached branch this one lies behind (root cause)


@dataclass
class CoverageResult:
    scenarios: list[Scenario]
    branches: list[BranchStatus]
    classes: list[ContactClass]
    scenario_fields: list[FieldRef]  # scenario-level fields read by the journey, in order
    contact_fields: list[FieldRef]
    domains: dict[FieldRef, list[Value]]
    unsupported: list[str]           # activity keys stopped as unsupported
    other_values: dict[FieldRef, Value] = field(default_factory=dict)   # the 'other' value of each field
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    # Spec, "Row present or absent": table -> its field linked to the contact key, for each table with the
    # "row present / row absent" variable (TableModel.existence_tested, as coverage uses it). In
    # Case.contact_values that field holds GENERATED_KEY when the contact has a row and None when it has none;
    # the other fields of an absent row are None too, and datagen writes no row for that contact.
    row_fields: dict[str, FieldRef] = field(default_factory=dict)
    # Elements of a supported type the generator cannot handle for some contacts: an activity at which some paths
    # stop because the walk cannot predict it (an Update contact whose insert of an absent row may fail,
    # graph.Walker.insert_problem), or a branch "<split>/<outcome>" whose condition compares a field that is not a
    # number or a date by order (no value inside that range is generated). Key -> why; the keys are in
    # ``unsupported`` too.
    unsupported_reasons: dict[str, str] = field(default_factory=dict)
    # Spec, step 3, "Exit criteria and goals": one coverage target per evaluable exit criterion, "exit criteria met
    # at the end of a wait", with activity_key = the criterion's key, outcome_key = "met", activity_kind = "exit".
    exit_targets: list[BranchStatus] = field(default_factory=list)
    # Spec, step 3, "Validity of the data": the earliest validity over all cases (days of late activation the data
    # tolerates), None when nothing depends on the clock; ``perishable`` lists, in plain words, the values it comes from.
    validity_days: Optional[int] = None
    perishable: list[str] = field(default_factory=list)

    @property
    def cases(self) -> list[Case]:
        return [c for s in self.scenarios for c in s.cases]

    def row_absent(self, case: Case, table: str) -> bool:
        """True when the contacts of ``case`` have no row in ``table`` (spec, "Row present or absent")."""
        ref = self.row_fields.get(table)
        return ref is not None and ref in case.contact_values and case.contact_values[ref] is None


# The value of a key field in coverage domains and contact values: "the generated key" (data rule 1). For the
# field linked to the contact key of a table with the "row present / row absent" variable it means "row present".
GENERATED_KEY = "<generated key>"


# ---------------------------------------------------------------------------
# Generated data and checks
# ---------------------------------------------------------------------------

@dataclass
class TableData:
    table: str
    columns: list[str]               # schema order
    rows: list[dict[str, Value]]


@dataclass
class Contact:
    key: str
    case_id: str
    scenario_id: str
    uses_real_inbox: bool
    uses_real_phone: bool
    email: str


@dataclass
class GeneratedData:
    tables: dict[str, TableData]     # by table name, selected tables only
    contacts: list[Contact]
    scenarios_rows: list[dict[str, Value]]   # content of scenarios.csv
    placeholders: list[str]          # entry columns that got a placeholder, with the reason
    warnings: list[str] = field(default_factory=list)


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: list[str]               # failures or notes, plain words


@dataclass
class GenerationResult:
    package: Package
    table_model: TableModel
    coverage: CoverageResult
    data: GeneratedData
    checks: list[CheckResult]
    settings: Settings
    files: dict[str, bytes]          # file name -> content, everything that goes into the zip
    generation_time: datetime
