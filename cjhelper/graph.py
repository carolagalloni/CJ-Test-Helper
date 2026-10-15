"""Journey graph: walk a contact through the activities and record what happens.

Spec, "Journey model and coverage", step 1: each activity is a node, each outcome a
branch pointing to the next activity, the start is the activity no other activity
points to. Step 2: decision split outcomes are tried in order, the first true one
wins, the remainder means none matched. Update-contact writes are tracked so a
later split sees the written value.

Spec, "Extensions beyond the pilot":

- Data sources. A Journey Data condition (``Condition.source == SOURCE_JOURNEY``) reads the
  entry row as it was at entry: "later Update contact writes do not change it". Contact Data
  is read live, so it sees the writes ("applies to Contact Data only").
- Row present or absent. For the tables given in ``row_fields`` (table -> its field linked
  to the contact key) a contact may have no row: the row is absent when that field reads
  null. "On an absent row, IsNull on any of its fields is true and every other operator is
  false": every field of an absent row reads null. "An Update contact on that table inserts
  the row: later reads see it present": the linked field then reads GENERATED_KEY, the
  written fields their written value. The spec says nothing about the other fields; the
  walker follows the assumptions already listed for inserts (docs/assumptions.md, rows 19,
  20, 29 and 46): an unwritten field reads its default value (``inserted_default``), null when
  it has none, and an insert that leaves a non-nullable field without a default empty may
  fail and take the contact out of the journey, so the path is unknown from that Update
  contact on (``insert_problem``).

Spec, "Extensions for time-dependent journeys (step 3, 14 October 2026)":

- The clock. "The clock starts at the entry time" (``Walker(entry_time=...)``, default the
  generation time ``now``). "A wait by duration advances the clock by its amount (a month counts
  28 days, as before)"; "a send-time wait advances the clock by nothing". "A wait by attribute
  holds the contact until the date in its field plus its offset ... at the time of day given by
  specifiedTime when it has one, otherwise at the time of day held in the field. When that moment
  is already past, the contact goes on at once": ``clock = max(clock, wait_end(activity, date))``.
  "When the field is null, the contact goes on at once (assumption)". Decision splits and exit
  criteria are evaluated with the clock (relative dates); "an email and an SMS are sent at the clock
  time; {{DateTime.Now}} written by an Update contact is the clock time". "Every case carries its
  timeline": ``WalkState.timeline`` holds one TimelinePoint per Step, in the same order, with the
  clock when the step is recorded (for a wait, the moment it ends).
- Exit criteria. "Journey Builder evaluates the exit criteria of a journey for each contact when the
  contact's time in a wait activity expires (documented)": at the end of every wait the evaluable
  exit criteria (``JourneyFilter.formula`` set, no ``unsupported_reason``) are evaluated with the
  clock and the same data sources as a decision split; when one is true the contact leaves the
  journey there (``Step(wait, None)``, ``STOP_EXIT_CRITERIA``). Goals are not modelled.
- SMS. "SMSSYNC is a send ... It has no branching": recorded in ``WalkState.sms_sent`` like an email.
- ``fixed_delays``: "every fixed delay with which a contact can reach that split (the sum of the
  waits by duration on each path from the start, waits by attribute counting zero)".

Assumptions of the walker for step 3 (the spec does not say; each is in the developer report):

- A wait by attribute whose value is not a readable date ends at once, as a null one does. Dates
  are read as criteria.py reads them (ISO 8601, a time zone offset converted to UTC and dropped),
  or in the walker's date format.
- A wait by attribute whose field the walk has not decided (UNKNOWN) ends at once unless the caller's
  predicate ``ends_at_once_when_unknown`` says otherwise; without a predicate it always ends at once,
  which is the behaviour before step 3 (the field of a "wait only" date is written by datagen with
  the date rule of the waits). A wait whose field the export does not identify ends at once too.
- Simplification: the field of a wait by attribute is read live (``value_of``) even when its
  expression is Journey Data (``{{Event...}}``): Activity keeps no data source, and on the entry
  table the live value differs from the snapshot only when an Update contact wrote the field.
- The offset of a wait by attribute moves the date with ``criteria.shift_date`` (months and years
  are calendar steps), then ``wait_time_of_day`` replaces the time of day (seconds dropped). The
  time of day applies to waits by attribute only: a wait by duration advances by its amount.
- A wait by duration whose duration cannot be read advances the clock by nothing, as it counts 0 in
  ``fixed_delays``.
- When several exit criteria are evaluable, the contact leaves when any of them is true; the first
  true one, in export order, is named.
- A current-date default of a field in a row an Update contact inserts is the clock at the insert.

The walker here serves two callers:

- coverage.py explores the journey symbolically: some field values are not yet
  decided, so a formula can evaluate to UNKNOWN and the caller branches on the
  possible values of the field. ``walk`` stops and reports where it got stuck.
- checks.py re-simulates a written contact with every value known, to prove the
  contact takes the path it was created for.

Both use the same code, so the check cannot drift from the generator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import criteria
from .model import (
    ACTIVITY_KINDS,
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
    GENERATED_KEY,
    SOURCE_JOURNEY,
    Activity,
    BranchRef,
    Condition,
    ConditionGroup,
    EmailAsset,
    FieldRef,
    Formula,
    Journey,
    JourneyFilter,
    Outcome,
    RelativeDate,
    Step,
    Table,
    TimelinePoint,
    Value,
    Write,
    formula_conditions,
    formula_text,
)

SPLIT_KINDS = (KIND_DECISION, KIND_ENGAGEMENT, KIND_RANDOM)
WAIT_KINDS = (KIND_WAIT_DURATION, KIND_WAIT_ATTRIBUTE, KIND_WAIT_STO)

DATETIME_NOW = "{{DateTime.Now}}"

# Schema defaults that stand for the current date and time (``defaultValue`` as written, lower case, no spaces).
CURRENT_DATE_DEFAULTS = ("getdate()", "getutcdate()", "sysdatetime()")


class Unknown:
    """Sentinel for a value that is not decided yet (three-valued logic)."""

    def __repr__(self) -> str:
        return "UNKNOWN"


UNKNOWN = Unknown()


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

class Schema:
    """Case-insensitive access to field types, with canonical (schema) spelling of field refs."""

    def __init__(self, tables: dict[str, Table]):
        self._tables = {name.lower(): t for name, t in tables.items()}
        self._canonical: dict[FieldRef, FieldRef] = {}
        self._types: dict[FieldRef, str] = {}

    def table(self, name: str) -> Optional[Table]:
        return self._tables.get(name.lower())

    def canonical(self, ref: FieldRef) -> FieldRef:
        """``(table, field)`` spelled as in the schema; unknown names are returned unchanged."""
        hit = self._canonical.get(ref)
        if hit is not None:
            return hit
        table = self.table(ref[0])
        if table is None:
            out = ref
        else:
            f = table.field(ref[1])
            out = (table.name, ref[1]) if f is None else (table.name, f.name)
        self._canonical[ref] = out
        return out

    def field_type(self, ref: FieldRef) -> str:
        hit = self._types.get(ref)
        if hit is not None:
            return hit
        table = self.table(ref[0])
        f = table.field(ref[1]) if table is not None else None
        out = f.type if f is not None else "Text"
        self._types[ref] = out
        return out

    def is_nullable(self, ref: FieldRef) -> bool:
        table = self.table(ref[0])
        if table is None:
            return True
        f = table.field(ref[1])
        return f.is_nullable if f is not None else True


# ---------------------------------------------------------------------------
# Three-valued formula evaluation
# ---------------------------------------------------------------------------

ValueLookup = Callable[[FieldRef], "Value | Unknown"]


def evaluate(formula: Optional[Formula], lookup: ValueLookup, schema: Schema,
             case_insensitive: bool, snapshot: Optional[ValueLookup] = None,
             now: Optional[datetime] = None) -> "bool | Unknown":
    """Evaluate a split formula. UNKNOWN propagates only when it could still change the answer.

    AND: any False -> False; else any UNKNOWN -> UNKNOWN; else True.
    OR:  any True -> True;   else any UNKNOWN -> UNKNOWN; else False.
    The remainder outcome has no formula and is handled by the caller.
    ``lookup`` answers Contact Data conditions; ``snapshot``, when given, answers Journey Data
    conditions (the entry row as it was at entry), otherwise ``lookup`` answers them too.
    ``now`` is the clock when the contact reaches the activity (spec, step 3, "The clock"); a condition
    with a relative date needs it (criteria.evaluate_condition raises UnsupportedOperator without it).
    """
    if formula is None:
        return True
    if isinstance(formula, Condition):
        ref = schema.canonical((formula.table, formula.field))
        value = _reader(formula, lookup, snapshot)(ref)
        if isinstance(value, Unknown):
            return UNKNOWN
        return criteria.evaluate_condition(formula, value, schema.field_type(ref), case_insensitive, now=now)
    assert isinstance(formula, ConditionGroup)
    saw_unknown = False
    for item in formula.items:
        r = evaluate(item, lookup, schema, case_insensitive, snapshot, now)
        if isinstance(r, Unknown):
            saw_unknown = True
        elif formula.operator == "AND" and r is False:
            return False
        elif formula.operator == "OR" and r is True:
            return True
    if saw_unknown:
        return UNKNOWN
    return formula.operator == "AND"


def first_unknown_field(formula: Optional[Formula], lookup: ValueLookup, schema: Schema,
                        snapshot: Optional[ValueLookup] = None) -> Optional[FieldRef]:
    """The first field, in document order, whose value is not decided yet."""
    for cond in formula_conditions(formula):
        ref = schema.canonical((cond.table, cond.field))
        if isinstance(_reader(cond, lookup, snapshot)(ref), Unknown):
            return ref
    return None


def _reader(cond: Condition, lookup: ValueLookup, snapshot: Optional[ValueLookup]) -> ValueLookup:
    """Journey Data conditions read the snapshot taken at entry, Contact Data conditions the live value."""
    if snapshot is not None and cond.source == SOURCE_JOURNEY:
        return snapshot
    return lookup


def is_null(value: "Value | Unknown") -> bool:
    """Null as the criteria read it: None or an empty string (criteria.evaluate_condition)."""
    return value is None or value == ""


def row_lookup(lookup: ValueLookup, row_fields: dict[str, FieldRef]) -> ValueLookup:
    """Spec, "Row present or absent": "On an absent row, IsNull on any of its fields is true and every other
    operator is false (confirmed: a contact with no row reads its key as null)".

    ``row_fields`` maps a lower-case table name to its field linked to the contact key (canonical); the
    row is absent when that field reads null, and every field of an absent row then reads null.
    """
    if not row_fields:
        return lookup

    def read(ref: FieldRef):
        linked = row_fields.get(ref[0].lower())
        if linked is None or linked == ref:
            return lookup(ref)
        present = lookup(linked)
        if isinstance(present, Unknown):
            return UNKNOWN
        return None if is_null(present) else lookup(ref)
    return read


# ---------------------------------------------------------------------------
# Walking
# ---------------------------------------------------------------------------

STOP_EXIT = "exit"                    # the journey ended
STOP_UNDETERMINED = "undetermined"    # a decision split needs a value the caller has not decided
STOP_CHOICE = "choice"                # an engagement or random split needs the caller's choice
STOP_UNSUPPORTED = "unsupported"      # an activity type the PoC does not understand
STOP_LOOP = "loop"                    # an activity was reached twice on the same path
STOP_DANGLING = "dangling"            # an outcome points to a missing activity
STOP_ERROR = "error"                  # an operator or value the PoC cannot evaluate
STOP_EXIT_CRITERIA = "exit_criteria"  # the exit criteria took the contact out at the end of a wait (spec, step 3)


@dataclass
class WalkState:
    """Everything that happened to one contact so far. Copy with ``clone`` before branching."""
    steps: list[Step] = field(default_factory=list)
    emails_sent: list[str] = field(default_factory=list)      # email activity keys
    writes: list[Write] = field(default_factory=list)
    waits: list[str] = field(default_factory=list)            # wait activity keys
    overrides: dict[FieldRef, Value] = field(default_factory=dict)   # update-contact writes, canonical refs
    inserted: set[str] = field(default_factory=set)           # lower-case tables an update contact wrote into
    visited: set[str] = field(default_factory=set)
    current: Optional[str] = None                             # activity key to execute next
    stop: Optional[str] = None                                # STOP_* once the walk ended
    stop_detail: str = ""                                     # plain words
    pending_field: Optional[FieldRef] = None                  # STOP_UNDETERMINED: the field to decide
    pending_outcomes: list[Outcome] = field(default_factory=list)  # STOP_CHOICE: the outcomes to choose from
    # Spec, step 3, "The clock": the clock of this contact (set by Walker.start to the entry time), the clock at
    # every step (one TimelinePoint per Step, same order), the SMS sends, and the wait at whose end the exit
    # criteria took the contact out of the journey.
    clock: Optional[datetime] = None
    timeline: list[TimelinePoint] = field(default_factory=list)
    sms_sent: list[str] = field(default_factory=list)         # SMS activity keys
    exit_by_criteria: Optional[str] = None
    inserted_at: dict[str, datetime] = field(default_factory=dict)   # lower-case table -> clock of its first write

    def clone(self) -> "WalkState":
        return WalkState(
            steps=list(self.steps), emails_sent=list(self.emails_sent), writes=list(self.writes),
            waits=list(self.waits), overrides=dict(self.overrides), inserted=set(self.inserted),
            visited=set(self.visited),
            current=self.current, stop=None, stop_detail="", pending_field=None, pending_outcomes=[],
            clock=self.clock, timeline=list(self.timeline), sms_sent=list(self.sms_sent),
            exit_by_criteria=self.exit_by_criteria, inserted_at=dict(self.inserted_at),
        )

    @property
    def end_activity_key(self) -> Optional[str]:
        return self.steps[-1].activity_key if self.steps else None

    def branches(self) -> list[BranchRef]:
        return [(s.activity_key, s.outcome_key) for s in self.steps if s.outcome_key is not None]


@dataclass
class EngagementVerdict:
    """Whether the Yes branch of an engagement split can be taken on this path, and why not."""
    possible: bool
    reason: str = ""


def engagement_verdict(activity: Activity, state: WalkState, emails: dict[str, EmailAsset],
                       journey: Journey) -> EngagementVerdict:
    """Spec: the Yes branch is reachable only if the path sent the referenced email earlier;
    a click needs a link in that email."""
    ref = activity.engagement_ref
    if activity.engagement_kind not in ("open", "click"):
        return EngagementVerdict(False, "non è stato possibile leggere dall'export il tipo di engagement "
                                        "(open o click)")
    if not ref:
        return EngagementVerdict(False, "lo split non indica l'email a cui si riferisce")
    if ref not in state.emails_sent:
        email = journey.activities.get(ref)
        name = email.email_name if email is not None and email.email_name else ref
        return EngagementVerdict(False, f"l'email di riferimento '{name}' ({ref}) non viene inviata sui percorsi "
                                        f"che raggiungono questo split")
    if activity.engagement_kind == "click":
        email = journey.activities.get(ref)
        asset = emails.get(email.email_asset_id) if email is not None and email.email_asset_id else None
        if asset is None or not asset.has_link:
            name = email.email_name if email is not None and email.email_name else ref
            return EngagementVerdict(False, f"l'email di riferimento '{name}' non contiene link da cliccare")
    return EngagementVerdict(True)


class Walker:
    """Executes activities one at a time against a value lookup.

    ``lookup(ref)`` returns the contact's value for a canonical field ref, as it is at entry, or UNKNOWN.
    For Contact Data, update-contact writes recorded in ``state.overrides`` take precedence over the
    lookup; Journey Data always reads the lookup (the snapshot taken at entry).
    ``row_fields`` lists the tables with the "row present / row absent" variable (table -> its field
    linked to the contact key, as in TableModel.existence_tested); see the module docstring.

    Spec, step 3, "The clock": ``entry_time`` is where every contact's clock starts (default ``now``,
    the generation time). ``ends_at_once_when_unknown(ref)`` answers for a wait by attribute whose
    field the walk has not decided: True, the wait ends at once and the walk goes on; False, the walk
    stops with STOP_UNDETERMINED on that field so the caller can branch on its values. Without it,
    such a wait always ends at once (the behaviour before step 3).
    """

    def __init__(self, journey: Journey, schema: Schema, emails: dict[str, EmailAsset],
                 case_insensitive: bool, now: Optional[datetime] = None, date_format: str = "%Y-%m-%d %H:%M",
                 row_fields: Optional[dict[str, FieldRef]] = None, entry_time: Optional[datetime] = None,
                 ends_at_once_when_unknown: Optional[Callable[[FieldRef], bool]] = None):
        self.journey = journey
        self.schema = schema
        self.emails = emails
        self.case_insensitive = case_insensitive
        self.now = now or datetime.now()
        self.entry_time = entry_time or self.now
        self.ends_at_once_when_unknown = ends_at_once_when_unknown
        self.date_format = date_format
        self.row_fields: dict[str, FieldRef] = {}
        for ref in (row_fields or {}).values():
            linked = schema.canonical(ref)
            self.row_fields[linked[0].lower()] = linked
        # Optional observer called when a decision split is taken, with the values of the fields
        # its outcomes read at that moment (coverage.py uses it to explain unreachable branches).
        self.on_decision: Optional[Callable[[Activity, dict[FieldRef, Value]], None]] = None

    def start(self) -> WalkState:
        """A contact at the start of the journey; its clock reads the entry time (spec, step 3)."""
        state = WalkState()
        state.clock = self.entry_time
        state.current = self.journey.start_key
        if state.current is None:
            state.stop = STOP_ERROR
            state.stop_detail = "il journey non ha un'attività iniziale"
        return state

    def value_of(self, state: WalkState, ref: FieldRef, lookup: ValueLookup) -> "Value | Unknown":
        """Contact Data, read live: the value written by an update contact, else the contact's row."""
        if ref in state.overrides:
            return state.overrides[ref]
        linked = self.row_fields.get(ref[0].lower())
        if linked is not None and ref[0].lower() in state.inserted:
            present = lookup(linked)
            if not isinstance(present, Unknown) and is_null(present):
                # The row was absent and an update contact inserted it.
                if ref == linked:
                    return GENERATED_KEY
                return self.inserted_default(ref, state.inserted_at.get(ref[0].lower()))
        return row_lookup(lookup, self.row_fields)(ref)

    def inserted_default(self, ref: FieldRef, at: Optional[datetime] = None) -> Value:
        """A field an Update contact did not write, in a row it inserted: the field's default value, as an
        insert gives it (assumptions rows 20, 29 and 46), or null when the field has no default. A
        current-date default is the time of the insert, ``at`` (the clock when the Update contact ran,
        spec step 3: "{{DateTime.Now}} written by an Update contact is the clock time"), else ``now``."""
        table = self.schema.table(ref[0])
        found = table.field(ref[1]) if table is not None else None
        default = (found.default or "").strip() if found is not None else ""
        if not default:
            return None
        if default.lower().replace(" ", "") in CURRENT_DATE_DEFAULTS:
            return (at or self.now).strftime(self.date_format)
        return default

    def insert_problem(self, activity: Activity, state: WalkState, lookup: ValueLookup) -> Optional[str]:
        """Why this Update contact may fail for this contact, or None.

        It inserts the contact's row in a table with the "row present / row absent" variable when the row is
        absent and no earlier Update contact on the path inserted it. An insert that leaves a field that is
        not nullable and has no default without a value fails, and the contact leaves the journey
        (assumptions rows 19 and 46): the field linked to the contact key gets the contact key, the written
        fields their value, the others nothing."""
        written: dict[str, set[str]] = {}
        for w in activity.writes:
            ref = self.schema.canonical((w.table, w.field))
            written.setdefault(ref[0].lower(), set()).add(ref[1].lower())
        for table_name, fields in written.items():
            linked = self.row_fields.get(table_name)
            if linked is None or table_name in state.inserted:
                continue
            present = lookup(linked)
            if isinstance(present, Unknown) or not is_null(present):
                continue          # the row is there: an update, not an insert
            table = self.schema.table(linked[0])
            missing = [f.name for f in (table.fields if table is not None else [])
                       if not f.is_nullable and not (f.default or "").strip()
                       and f.name.lower() not in fields and f.name.lower() != linked[1].lower()]
            if missing:
                names = ", ".join(missing)
                return (f"l'Update contact '{activity.key}' inserisce la riga del contatto in {linked[0]}, che manca, "
                        f"ma non scrive {names}, che non {'ammette' if len(missing) == 1 else 'ammettono'} null e "
                        f"non {'ha' if len(missing) == 1 else 'hanno'} un valore di default: l'inserimento può "
                        "fallire e far uscire il contatto dal journey")
        return None

    def snapshot_of(self, ref: FieldRef, lookup: ValueLookup) -> "Value | Unknown":
        """Journey Data: the entry row as it was at entry, whatever the update contacts wrote."""
        return row_lookup(lookup, self.row_fields)(ref)

    def run(self, state: WalkState, lookup: ValueLookup,
            choose: Optional[Callable[[Activity, WalkState], Optional[Outcome]]] = None) -> WalkState:
        """Advance until the journey exits or the walk needs the caller (see STOP_*).

        ``choose(activity, state)`` answers engagement and random splits with an outcome;
        returning None means the caller wants to branch itself (STOP_CHOICE).
        """
        while state.stop is None:
            self.step(state, lookup, choose)
        return state

    def step(self, state: WalkState, lookup: ValueLookup,
             choose: Optional[Callable[[Activity, WalkState], Optional[Outcome]]] = None) -> None:
        key = state.current
        if key is None:
            state.stop = STOP_EXIT
            return
        activity = self.journey.activities.get(key)
        if activity is None:
            state.stop = STOP_DANGLING
            state.stop_detail = f"un branch porta a un'attività mancante, '{key}'"
            return
        if key in state.visited:
            state.stop = STOP_LOOP
            state.stop_detail = f"l'attività '{key}' è raggiunta due volte sullo stesso percorso"
            return
        state.visited.add(key)
        if state.clock is None:
            state.clock = self.entry_time   # a state not made by start(): the clock starts at the entry time

        kind = activity.kind
        if kind == KIND_DECISION:
            self._decision(activity, state, lookup)
        elif kind in (KIND_ENGAGEMENT, KIND_RANDOM):
            outcome = choose(activity, state) if choose is not None else None
            if outcome is None:
                state.stop = STOP_CHOICE
                state.pending_outcomes = list(activity.outcomes)
                return
            self._take(activity, outcome, state)
        elif kind == KIND_UNSUPPORTED:
            self._record(state, key, None)
            state.stop = STOP_UNSUPPORTED
            state.stop_detail = f"l'attività '{key}' è di un tipo non supportato, {activity.raw_type}"
        elif kind in WAIT_KINDS:
            self._wait(activity, state, lookup)
        else:
            if kind == KIND_UPDATE:
                problem = self.insert_problem(activity, state, lookup)
                if problem is not None:
                    # Spec, "Honest outcomes": past an element the walk cannot predict, the path is unknown.
                    self._record(state, key, None)
                    state.stop = STOP_UNSUPPORTED
                    state.stop_detail = problem
                    return
            if kind == KIND_EMAIL:
                state.emails_sent.append(key)
            elif kind == KIND_SMS:
                # Spec, step 3, "SMS sends": "SMSSYNC is a send ... It has no branching", sent at the clock time.
                state.sms_sent.append(key)
            elif kind == KIND_UPDATE:
                self._update(activity, state)
            self._take(activity, activity.outcomes[0] if activity.outcomes else None, state)

    def take(self, state: WalkState, activity: Activity, outcome: Optional[Outcome]) -> None:
        """Record that ``outcome`` of ``activity`` was taken and move on. Public for callers
        that answer engagement and random splits themselves after STOP_CHOICE."""
        state.stop = None
        state.stop_detail = ""
        state.pending_outcomes = []
        self._take(activity, outcome, state)

    def _record(self, state: WalkState, activity_key: str, outcome_key: Optional[str]) -> None:
        """Append a Step and its TimelinePoint, the clock as the step is recorded (spec, step 3: "Every case
        carries its timeline: the clock at every step of the expected path")."""
        if state.clock is None:
            state.clock = self.entry_time
        state.steps.append(Step(activity_key, outcome_key))
        state.timeline.append(TimelinePoint(activity_key, outcome_key, state.clock))

    def _take(self, activity: Activity, outcome: Optional[Outcome], state: WalkState) -> None:
        if outcome is None:
            self._record(state, activity.key, None)
            state.current = None
            state.stop = STOP_EXIT
            return
        self._record(state, activity.key, outcome.key)
        state.current = outcome.next
        if outcome.next is None:
            state.stop = STOP_EXIT

    # -- waits and exit criteria (spec, step 3) -------------------------------------------

    def _wait(self, activity: Activity, state: WalkState, lookup: ValueLookup) -> None:
        """Hold the contact until the wait ends, then apply the exit criteria, then move on.

        The clock and ``waits`` change only once the walk can go past the wait: when the walk stops on a field
        to decide (the wait's date, or a field of the exit criteria), the caller re-runs the whole wait.
        """
        end = self._wait_end_at(activity, state, lookup)
        if end is None:
            return
        met = self._exit_criteria(activity, state, lookup, end)
        if state.stop is not None and state.stop != STOP_ERROR:
            return
        state.clock = end
        state.waits.append(activity.key)
        if state.stop == STOP_ERROR:
            self._record(state, activity.key, None)
            return
        if met is not None:
            # Spec, step 3: "when they are true the contact leaves the journey there".
            self._record(state, activity.key, None)
            state.exit_by_criteria = activity.key
            state.current = None
            state.stop = STOP_EXIT_CRITERIA
            state.stop_detail = (f"il contatto esce dal journey alla fine del wait '{activity.key}', perché soddisfa "
                                 f"il criterio di uscita (exit criteria) {_filter_text(met)}")
            return
        self._take(activity, activity.outcomes[0] if activity.outcomes else None, state)

    def _wait_end_at(self, activity: Activity, state: WalkState, lookup: ValueLookup) -> Optional[datetime]:
        """When the wait ends for this contact, never before the clock; None when the walk stopped here.

        Spec, step 3, "The clock": a wait by duration "advances the clock by its amount (a month counts 28 days,
        as before)"; a send-time wait "advances the clock by nothing"; a wait by attribute ends at
        ``wait_end(activity, date)`` and "when that moment is already past, the contact goes on at once";
        "when the field is null, the contact goes on at once (assumption)". See the module docstring for the
        undecided field, an unreadable date or duration, and the Journey Data simplification.
        """
        clock = state.clock if state.clock is not None else self.entry_time
        if activity.kind == KIND_WAIT_DURATION:
            return clock + timedelta(minutes=fixed_wait_minutes(activity) or 0)
        if activity.kind != KIND_WAIT_ATTRIBUTE or activity.wait_field is None:
            return clock
        ref = self.schema.canonical(activity.wait_field)
        value = self.value_of(state, ref, lookup)
        if isinstance(value, Unknown):
            if self.ends_at_once_when_unknown is None or self.ends_at_once_when_unknown(ref):
                return clock
            state.stop = STOP_UNDETERMINED
            state.pending_field = ref
            state.visited.discard(activity.key)   # the caller will re-run this activity
            return None
        moment = self.read_date(value)
        if moment is None:
            return clock
        try:
            end = wait_end(activity, moment)
        except criteria.UnsupportedOperator as exc:
            self._record(state, activity.key, None)
            state.stop = STOP_ERROR
            state.stop_detail = f"non è possibile calcolare quando termina il wait '{activity.key}': {exc}"
            return None
        return max(clock, end)

    def read_date(self, value: "Value | Unknown") -> Optional[datetime]:
        """The date and time a wait by attribute reads, or None when the value is null or not a readable date.

        As criteria.py reads dates (ISO 8601, ``YYYY-MM-DD[ HH:MM[:SS]]``, a time zone offset converted to UTC
        and dropped), or in the walker's date format (the form of ``{{DateTime.Now}}`` writes)."""
        if isinstance(value, Unknown) or is_null(value):
            return None
        text = str(value).strip()
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.strptime(text, self.date_format)
            except ValueError:
                return None
        if moment.tzinfo is not None:
            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
        return moment

    def _exit_criteria(self, wait: Activity, state: WalkState, lookup: ValueLookup,
                       end: datetime) -> Optional[JourneyFilter]:
        """The exit criterion that takes the contact out at the end of ``wait``, or None.

        Spec, step 3, "Exit criteria and goals": "At the end of every wait on a path the exit criteria are
        evaluated with the clock", with "the same operators, data sources and relative dates as a decision
        split" (Contact Data live, Journey Data from the snapshot). Only the evaluable ones count (a formula and
        no ``unsupported_reason``); goals are not modelled. A field the walk has not decided stops it with
        STOP_UNDETERMINED (the caller re-runs the wait); an operator that cannot be evaluated with STOP_ERROR.
        """
        def value(ref: FieldRef):
            return self.value_of(state, ref, lookup)

        def snapshot(ref: FieldRef):
            return self.snapshot_of(ref, lookup)

        undecided: Optional[JourneyFilter] = None
        for exit_ in evaluable_exits(self.journey):
            try:
                result = evaluate(exit_.formula, value, self.schema, self.case_insensitive, snapshot, now=end)
            except criteria.UnsupportedOperator as exc:
                state.stop = STOP_ERROR
                state.stop_detail = (f"il criterio di uscita (exit criteria) {_filter_text(exit_)}, valutato alla fine "
                                     f"del wait '{wait.key}', usa un operatore che il PoC non supporta: {exc}")
                return None
            if result is True:
                return exit_
            if isinstance(result, Unknown) and undecided is None:
                undecided = exit_
        if undecided is not None:
            state.stop = STOP_UNDETERMINED
            state.pending_field = first_unknown_field(undecided.formula, value, self.schema, snapshot)
            state.visited.discard(wait.key)   # the caller will re-run the wait once the field is decided
        return None

    def _decision(self, activity: Activity, state: WalkState, lookup: ValueLookup) -> None:
        """Outcomes in order, first true wins; the remainder when none matched. Evaluated at the clock time
        (spec, step 3: a condition can "compare a date with Today")."""
        def value(ref: FieldRef):
            return self.value_of(state, ref, lookup)

        def snapshot(ref: FieldRef):
            return self.snapshot_of(ref, lookup)

        def observe() -> None:
            if self.on_decision is None:
                return
            seen: dict[FieldRef, Value] = {}
            for o in activity.outcomes:
                for cond in formula_conditions(o.formula):
                    ref = self.schema.canonical((cond.table, cond.field))
                    v = _reader(cond, value, snapshot)(ref)
                    if not isinstance(v, Unknown):
                        seen[ref] = v
            self.on_decision(activity, seen)

        # Spec: outcomes are tried in order and the remainder means none matched, so the remainder
        # is evaluated last whatever its position in the export.
        remainder = [o for o in activity.outcomes if o.is_remainder or o.formula is None]
        ordered = [o for o in activity.outcomes if o not in remainder] + remainder
        for outcome in ordered:
            if outcome.is_remainder or outcome.formula is None:
                observe()
                self._take(activity, outcome, state)
                return
            if outcome.unsupported_reason:
                observe()
                self._record(state, activity.key, None)
                state.stop = STOP_UNSUPPORTED
                state.stop_detail = (f"il branch '{outcome.label or outcome.key}' dello split '{activity.key}' "
                                     f"non è supportato: {outcome.unsupported_reason}")
                return
            try:
                result = evaluate(outcome.formula, value, self.schema, self.case_insensitive, snapshot,
                                  now=state.clock)
            except criteria.UnsupportedOperator as exc:
                self._record(state, activity.key, None)
                state.stop = STOP_ERROR
                state.stop_detail = f"lo split '{activity.key}' usa un operatore che il PoC non supporta: {exc}"
                return
            if isinstance(result, Unknown):
                state.stop = STOP_UNDETERMINED
                state.pending_field = first_unknown_field(outcome.formula, value, self.schema, snapshot)
                state.visited.discard(activity.key)   # the caller will re-run this activity
                return
            if result:
                observe()
                self._take(activity, outcome, state)
                return
        # No remainder outcome and nothing matched: the contact leaves the journey here.
        observe()
        self._record(state, activity.key, None)
        state.current = None
        state.stop = STOP_EXIT
        state.stop_detail = f"lo split '{activity.key}' non trova alcun branch corrispondente e non ha un remainder"

    def _update(self, activity: Activity, state: WalkState) -> None:
        """Spec: written values are tracked so a later split reads the updated value (Contact Data only);
        a write into a table with the "row present / row absent" variable inserts the contact's row.
        Spec, step 3: "{{DateTime.Now}} written by an Update contact is the clock time"."""
        for w in activity.writes:
            ref = self.schema.canonical((w.table, w.field))
            value = w.value
            if value == DATETIME_NOW:
                value = (state.clock or self.entry_time).strftime(self.date_format)
            state.overrides[ref] = value
            state.writes.append(Write(ref[0], ref[1], value))
            if ref[0].lower() in self.row_fields:
                state.inserted.add(ref[0].lower())
                state.inserted_at.setdefault(ref[0].lower(), state.clock or self.entry_time)


# ---------------------------------------------------------------------------
# Paths and waits
# ---------------------------------------------------------------------------

def case_paths(case) -> list[list[str]]:
    """Activity keys of every full path of a case (model.Case).

    A statistical case's ``steps`` stop at the random split; each random branch continues it.
    """
    base = [step.activity_key for step in case.steps]
    if case.random is not None and case.random.branches:
        return [base + [step.activity_key for step in branch.steps] for branch in case.random.branches]
    return [base]


# Minutes per wait unit as written in the export (waitUnit). A month and a year count their shortest
# length, 28 and 365 days, because the durations computed here are minimum durations.
WAIT_UNIT_MINUTES = {
    "MINUTE": 1, "HOUR": 60, "DAY": 24 * 60, "WEEK": 7 * 24 * 60, "MONTH": 28 * 24 * 60, "YEAR": 365 * 24 * 60,
}


def fixed_wait_minutes(activity: Activity) -> Optional[int]:
    """The shortest time a wait by duration or a send-time wait holds a contact, in minutes.

    Spec, "Honest outcomes": the window of a manual action and the minimum duration of a case come from
    the waits on the path. A wait by duration lasts its amount (``specifiedTime``, a time of day the wait
    ends at, can only make it longer). A send-time wait may send at once, so it counts 0. A wait by attribute
    counts 0 here: it ends at a date, which is a value of the data (datagen.py). None when the amount or the
    unit cannot be read.
    """
    if activity.kind == KIND_WAIT_DURATION:
        unit = (activity.wait_unit or "").strip().upper()
        per_unit = WAIT_UNIT_MINUTES.get(unit[:-1] if unit.endswith("S") else unit)
        if per_unit is None or activity.wait_amount is None or activity.wait_amount < 0:
            return None
        return activity.wait_amount * per_unit
    return 0


# The units of a wait's offset (waitUnit, upper case, without the plural S) as criteria.shift_date takes them.
OFFSET_UNITS = {"MINUTE": "minutes", "HOUR": "hours", "DAY": "days", "WEEK": "weeks", "MONTH": "months",
                "YEAR": "years"}


def wait_end(activity: Activity, value: datetime) -> datetime:
    """The moment a wait by attribute whose field holds ``value`` ends (the clock is not considered here).

    Spec, step 3, "The clock": "A wait by attribute holds the contact until the date in its field plus its
    offset (waitDuration and waitUnit, which may be negative: "13 days before Entitle_RequestCancellationDate"
    is an offset of -13 days), at the time of day given by specifiedTime when it has one, otherwise at the time
    of day held in the field." The offset is applied with ``criteria.shift_date`` (minutes, hours, days and weeks
    are fixed lengths; months and years are calendar steps, unlike the 28-day month of a wait by duration), then
    ``wait_time_of_day`` ("HH:MM") replaces the time of day, seconds dropped. The caller keeps the later of this
    moment and the clock ("When that moment is already past, the contact goes on at once"). Raises
    criteria.UnsupportedOperator when the offset unit or the time of day cannot be read.
    """
    end = value
    amount = activity.wait_offset_amount or 0
    if amount:
        unit = offset_unit(activity.wait_offset_unit)
        if unit is None:
            raise criteria.UnsupportedOperator(f"l'unità {activity.wait_offset_unit!r} dello spostamento del wait "
                                               f"'{activity.key}' non è supportata (unità supportate: "
                                               f"{', '.join(sorted(OFFSET_UNITS))})")
        end = criteria.shift_date(end, RelativeDate(sign=1 if amount > 0 else -1, amount=abs(amount), unit=unit))
    time_of_day = (activity.wait_time_of_day or "").strip()
    if time_of_day:
        hour, minute = _time_of_day(time_of_day, activity)
        end = end.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return end


def offset_unit(unit: Optional[str]) -> Optional[str]:
    """``MINUTES``, ``HOURS``, ``DAYS``, ``WEEKS``, ``MONTHS``, ``YEARS`` (any case, with or without the plural S)
    as the lower-case plural unit criteria.shift_date takes; None for anything else."""
    text = (unit or "").strip().upper()
    return OFFSET_UNITS.get(text[:-1] if text.endswith("S") else text)


def _time_of_day(text: str, activity: Activity) -> tuple[int, int]:
    """``(hour, minute)`` of a ``specifiedTime`` written ``H:MM``, ``HH:MM`` or ``HH:MM:SS``."""
    parts = text.split(":")
    if len(parts) in (2, 3) and all(p.strip().isdigit() for p in parts):
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    raise criteria.UnsupportedOperator(f"l'ora del giorno {text!r} a cui termina il wait '{activity.key}' non si "
                                       "può leggere (forma attesa HH:MM)")


def evaluable_exits(journey: Journey) -> list[JourneyFilter]:
    """The exit criteria the walker evaluates at the end of every wait, in export order: those with a formula
    and no ``unsupported_reason`` (spec, step 3: "Exit criteria with a condition the generator cannot evaluate
    are reported as not modelled")."""
    return [e for e in journey.exits if e.formula is not None and e.unsupported_reason is None]


def _filter_text(exit_: JourneyFilter) -> str:
    """How an exit criterion is named in plain words: its description, else its formula, with its name."""
    described = " ".join((exit_.description or "").split()) or formula_text(exit_.formula)
    return f"'{exit_.name}' ({described})" if exit_.name and exit_.name != described else f"'{described}'"


# At most this many fixed delays are kept per activity (the smallest), so that a journey with many paths stays
# cheap to explore.
MAX_FIXED_DELAYS = 32
MINUTES_PER_DAY = 24 * 60


def fixed_delays(journey: Journey) -> tuple[dict[str, set[float]], list[str]]:
    """For every activity key, the fixed delays in days with which a contact can reach it; plus warnings.

    Spec, step 3, "Values of a date field read with relative dates or by a wait with an offset": "every fixed
    delay with which a contact can reach that split (the sum of the waits by duration on each path from the
    start, waits by attribute counting zero)". A wait by duration adds ``fixed_wait_minutes`` (a month counts
    28 days), a send-time wait and a wait by attribute add 0; a wait whose duration cannot be read adds 0 and
    is reported once. The start activity has {0.0}; an activity no path reaches has an empty set. The delays
    are summed in whole minutes and divided by 1440 once, so equal sums give equal floats.

    The paths are followed along the outcomes from the start; an outcome that leads back to an activity already
    on the path is not followed (a journey with a loop is reported by ``structural_problems``). To stay linear in
    the size of the journey the sets are propagated in topological order of a depth-first search instead of
    listing every path (the pilot has hundreds of thousands): on a journey without loops this gives the same
    sets; on one with a loop, the outcomes that close a loop in the search are the ones left out. Each set keeps
    its MAX_FIXED_DELAYS smallest values, with a warning naming the activity (the activities after it then
    start from the kept values).
    """
    activities = journey.activities
    minutes: dict[str, set[int]] = {key: set() for key in activities}
    warnings: list[str] = []
    if journey.start_key is None or journey.start_key not in activities:
        return {key: set() for key in activities}, warnings
    order, back_edges = _depth_first_order(journey)
    minutes[journey.start_key].add(0)
    for key in order:
        if len(minutes[key]) > MAX_FIXED_DELAYS:
            minutes[key] = set(sorted(minutes[key])[:MAX_FIXED_DELAYS])
            warnings.append(f"l'attività '{key}' si può raggiungere con più di {MAX_FIXED_DELAYS} ritardi fissi "
                            f"diversi (somme dei wait by duration sui percorsi dall'inizio): sono tenuti i "
                            f"{MAX_FIXED_DELAYS} più brevi")
        activity = activities[key]
        added = fixed_wait_minutes(activity)
        if added is None:
            warnings.append(f"la durata del wait '{key}' ({activity.wait_amount} {activity.wait_unit}) non si può "
                            "leggere: nei ritardi fissi con cui i contatti raggiungono le attività successive conta 0")
            added = 0
        for outcome in activity.outcomes:
            if outcome.next in minutes and (key, outcome.next) not in back_edges:
                minutes[outcome.next].update(m + added for m in minutes[key])
    return {key: {m / MINUTES_PER_DAY for m in found} for key, found in minutes.items()}, warnings


def _depth_first_order(journey: Journey) -> tuple[list[str], set[tuple[str, str]]]:
    """The activities reachable from the start in topological order (reverse postorder of a depth-first search
    along the outcomes, in export order), and the outcomes ``(from, to)`` that lead back to an activity on the
    search's current path (they close a loop)."""
    activities = journey.activities
    start = journey.start_key
    assert start is not None
    postorder: list[str] = []
    back_edges: set[tuple[str, str]] = set()
    seen = {start}
    on_path = {start}
    stack: list[tuple[str, int]] = [(start, 0)]
    while stack:
        key, index = stack[-1]
        outcomes = activities[key].outcomes
        if index >= len(outcomes):
            stack.pop()
            on_path.discard(key)
            postorder.append(key)
            continue
        stack[-1] = (key, index + 1)
        nxt = outcomes[index].next
        if nxt is None or nxt not in activities:
            continue
        if nxt in on_path:
            back_edges.add((key, nxt))
        elif nxt not in seen:
            seen.add(nxt)
            on_path.add(nxt)
            stack.append((nxt, 0))
    postorder.reverse()
    return postorder, back_edges


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------

def is_split(activity: Activity) -> bool:
    """A decision, engagement or random split, including one reported as unsupported (its outcomes are
    still branches of the journey, all of them not covered)."""
    if activity.kind in SPLIT_KINDS:
        return True
    return activity.kind == KIND_UNSUPPORTED and ACTIVITY_KINDS.get(activity.raw_type) in SPLIT_KINDS


def split_branches(journey: Journey) -> list[tuple[Activity, Outcome]]:
    """Every outcome of every decision, engagement and random split, in export order."""
    return [(a, o) for a in journey.activities.values() if is_split(a) for o in a.outcomes]


def predecessors(journey: Journey) -> dict[str, set[str]]:
    preds: dict[str, set[str]] = {k: set() for k in journey.activities}
    for a in journey.activities.values():
        for o in a.outcomes:
            if o.next is not None and o.next in preds:
                preds[o.next].add(a.key)
    return preds


def activities_behind(journey: Journey, blockers: set[str]) -> set[str]:
    """Activities reachable from the start only through one of the ``blockers``."""
    if journey.start_key is None:
        return set(journey.activities)
    reachable: set[str] = set()
    stack = [journey.start_key]
    while stack:
        k = stack.pop()
        if k in reachable or k in blockers or k not in journey.activities:
            continue
        reachable.add(k)
        for o in journey.activities[k].outcomes:
            if o.next is not None:
                stack.append(o.next)
    return {k for k in journey.activities if k not in reachable and k not in blockers}


def structural_problems(journey: Journey) -> list[str]:
    """Plain-words findings about the graph itself: dangling links, several starts, unreachable activities."""
    problems: list[str] = []
    for a in journey.activities.values():
        for o in a.outcomes:
            if o.next is not None and o.next not in journey.activities:
                problems.append(f"il branch '{o.key}' dell'attività '{a.key}' porta a un'attività mancante, "
                                f"'{o.next}'")
    preds = predecessors(journey)
    starts = [k for k, p in preds.items() if not p]
    if len(starts) > 1:
        problems.append("più attività non hanno collegamenti in entrata: " + ", ".join(starts)
                        + f"; il journey inizia da '{journey.start_key}'")
    unreachable = activities_behind(journey, set())
    if unreachable:
        problems.append("attività non raggiungibili dall'inizio: " + ", ".join(sorted(unreachable)))
    return problems
