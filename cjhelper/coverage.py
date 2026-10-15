"""Coverage: which field values send a contact down each branch, and the smallest set of
scenarios and cases that crosses every reachable branch.

Spec, "Journey model and coverage", steps 3 to 6:

3. Fields are contact-level or scenario-level (tables.py decides; here we read the result).
4. Values are enumerated, not paths. Each field read by a split takes the values named in
   conditions, one extra "other" value, and null when the schema allows it. Contact-level
   combinations are grouped into classes that satisfy the same conditions. The journey is
   simulated for each scenario-level combination and each class.
5. A greedy set cover with a fixed random seed picks the fewest scenarios crossing every
   reachable branch.
6. Paths sharing scenario-level values form a scenario; inside it, each distinct combination
   of contact-level values and manual actions is a case of 3 contacts.

Special activities (spec table): engagement splits get both branches, the Yes branch only
when the referenced email was sent earlier on the path and (for a click) the email has a
link; random splits cannot be forced, the cover prefers paths avoiding them, and branches
reachable only through them get one statistical case of 50 contacts.

Spec, "Extensions beyond the pilot":

- Row present or absent. Each table of ``TableModel.existence_tested`` gets a contact-level
  variable: its field linked to the contact key takes GENERATED_KEY (row present) or None
  (row absent), and every other field of an absent row reads null (graph.py).
- Values strictly inside the ranges. Number, Decimal and Date fields compared with an
  ordering operator or ``Between`` take one value inside each range the condition values cut,
  never a bound, plus the values named by the other operators (``build_domain``).
- Preference. Among values that cover the same branches, the generator prefers those whose
  path rests on no unverified assumption: not null, row present, then values inside the
  ranges rather than on a named value (``_preference``). It decides the representative of
  each contact class, the scenario kept for each signature and, among different scenarios
  crossing the same branches, the one the cover can choose (``_preferred_per_mask``).
- Honest outcomes. A path that stops at an element the PoC cannot evaluate is marked
  ``Case.unknown_from``; each manual action gets its window (``Case.action_windows``) and
  each case its minimum duration, from the waits on its path.

Spec, "Extensions for time-dependent journeys (step 3, 14 October 2026)":

- Aliases. "A column of the entry table copied as a plain column from a source table ... holds the
  same value as its source: the two fields are one variable for the coverage". Each group of
  ``TableModel.aliases`` is one variable, represented by its first member outside the entry table (the
  source column; the entry copy is derived from it), else by its first member (``alias_of``). The walker's
  lookups read the representative for any member; fields, domains and values hold representatives only.
- The clock. The walker starts every contact at the entry time (``Settings.entry_time``, default the
  generation time). A Date field with a relative condition, or read by a wait by attribute with an offset,
  gets its values from cut points and classes of candidates, each with its validity (``_clock_domain``).
  Contact classes evaluate a predicate holding a relative condition at every fixed delay of its split.
- Exit criteria. The fields of the evaluable exit criteria are read like those of the splits. "Every exit
  criterion is one extra coverage target, 'exit criteria met at the end of a wait'": one bit per criterion
  after the split branches (``exit_targets``). "A branch no contact can reach because every contact that
  gets to the wait before it meets the exit criteria is reported as unreachable with that reason"
  (``_exit_reason``).
- Cases carry their timeline, their SMS sends, the wait at whose end they leave by the exit criteria, and
  their validity ("Validity of the data"); the result gives the earliest validity and explains it.

Assumptions and readings of the lead for step 3 (the spec does not fix them; each is in the developer report):

- Two candidates of a clock field are one class when they decide the same way on the same paths
  (``_behaviour``): the journey is walked with only that field known, each split with the clock of the path
  that reaches it, both ways where a formula also depends on other fields; a wait by attribute on another field
  counts 0, as in the fixed delays. The cut points still come from every fixed delay of each split (the clock
  points): the entry time plus d, or the end of a wait by attribute on the field that precedes the split.
- A wait by attribute on a clock field without an offset counts with an offset of 0 (cut point and choice
  rule 2). Rule 2 compares, for each wait on the field, the later of its earliest arrival (entry time plus
  its smallest fixed delay) and the end its date gives; with several waits, the last of them.
- A day-level cut point is a whole number of days (the difference of the dates), so the candidates keep the
  time of day of the entry time; a time-level one (hours, minutes) is exact. The bounds of the absolute
  ordering conditions on a clock field are cut points too.
- Validity is the behaviour re-evaluated one day of slip at a time for the first two months, then by bisection
  up to 366 days, a change read as permanent (each condition is monotone in the slip: the clock moves forward,
  a relative reference with it). It is a property of the value on every path a contact holding it can take
  ("at any delay a contact can reach it with"), not of one case's path.
- In the cover, a leaf holding a value below the minimum validity counts only for the targets no other leaf
  reaches (``_effective_masks``).
- Among the members of a contact class, a value whose validity is below the minimum validity counts against
  the representative, after null and absent rows and before values on a named point.
- Whether the exit criteria are why a branch is never crossed is found by walking every path that leaves by
  the exit criteria on as if they were false (``_exit_shadow``): a branch those walks cross is unreachable
  because of the exit criteria.

The simulation itself lives in graph.py so that checks.py re-simulates with the same code.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Optional

from . import criteria, graph
from .model import (
    GENERATED_KEY,
    KIND_SMS,
    KIND_WAIT_ATTRIBUTE,
    KIND_WAIT_DURATION,
    SOURCE_JOURNEY,
    ActionWindow,
    EqualityFilter,
    KIND_EMAIL,
    IsNullFilter,
    SelectCurrentDate,
    SelectLiteral,
    SelectNull,
    KIND_DECISION,
    KIND_ENGAGEMENT,
    KIND_RANDOM,
    KIND_UNSUPPORTED,
    KIND_UPDATE,
    LEVEL_CONTACT,
    LEVEL_SCENARIO,
    STATUS_COVERED,
    STATUS_MANUAL,
    STATUS_NOT_COVERED,
    Activity,
    BranchRef,
    BranchStatus,
    Case,
    Condition,
    ContactClass,
    CoverageResult,
    FieldRef,
    Formula,
    JourneyFilter,
    ManualAction,
    Outcome,
    Package,
    RandomBranch,
    RandomInfo,
    RelativeDate,
    Scenario,
    Settings,
    Step,
    TableModel,
    TimelinePoint,
    Value,
    Write,
    formula_conditions,
    formula_text,
)

# Safety limits so a strange export cannot hang the generator. Both are reported when hit.
MAX_CONTACT_COMBINATIONS = 1_000_000   # per component of the contact-level fields (build_classes)
MAX_CONTACT_CLASSES = 20_000
MAX_PREFIX_STATES = 200_000          # partial assignments kept by the exact enumeration of a large component
_UNSET = object()                    # a field not assigned yet (CoverageEngine._component_classes_by_prefix)
MAX_SCENARIO_COMBINATIONS = 2_000_000
MAX_PATHS = 400_000

OTHER_TEXT_CANDIDATES = ("OTHER", "OTHER_2", "OTHER_3", "OTHER_4", "X", "Y", "Z", "W", "XX", "YY", "ZZ", "XY", "XZ", "YZ")
OTHER_EMAIL = "other@example.invalid"
OTHER_PHONE = "000000000000"
KEY_MARKER = GENERATED_KEY         # stands for the generated key in the domain of a key field
TEXT_TYPES = ("Text", "EmailAddress", "Phone", "Locale")
ORDER_OPERATORS_UP = ("GreaterThan", "GreaterThanOrEqual")
ORDER_OPERATORS_DOWN = ("LessThan", "LessThanOrEqual")
# Spec, step 3: "The same operators with an absolute date value keep the earlier reading" (as the ordering operators).
DATE_ORDER_OPERATORS = ("After", "AtOrAfter", "Before", "AtOrBefore")
RANGE_OPERATORS = ORDER_OPERATORS_UP + ORDER_OPERATORS_DOWN + ("Between",) + DATE_ORDER_OPERATORS
RANGE_TYPES = ("Number", "Decimal", "Date")    # the field types whose ranges build_domain fills

# Spec, step 3, "Exit criteria and goals": the coverage target of an exit criterion is the branch
# (EXIT_PREFIX + criterion key, EXIT_OUTCOME) of the masks; its BranchStatus has the criterion key itself.
EXIT_PREFIX = "EXIT:"
EXIT_OUTCOME = "met"
EXIT_LABEL = "criteri di uscita soddisfatti"
EXIT_KIND = "exit"
# Spec, step 3, "Validity of the data": "A value that no slip of up to a year changes is not perishable".
MAX_SLIP_DAYS = 366
SLIP_SCAN_DAYS = 62             # slips tried one day at a time before bisection (``_clock_validity``)
MAX_SHADOW_PATHS = 100_000      # paths walked on past the exit criteria to explain unreachable branches


# ---------------------------------------------------------------------------
# Domains
# ---------------------------------------------------------------------------

def _normalise_boolean(value: Value) -> Value:
    if value is None:
        return None
    low = value.strip().lower()
    if low in ("true", "1"):
        return "True"
    if low in ("false", "0"):
        return "False"
    return value


def _clashes(candidate: str, values: list[str]) -> bool:
    """A text 'other' value must not equal, contain or be contained in any condition value,
    so that Equal, Contains, BeginsWith and EndsWith conditions all stay false for it."""
    low = candidate.lower()
    for v in values:
        vl = v.lower()
        if vl and (low == vl or vl in low or low in vl):
            return True
    return False


def _other_text(values: list[str], length: Optional[int]) -> Optional[str]:
    """An 'other' text value that fits the field length; None when no such value exists."""
    for candidate in OTHER_TEXT_CANDIDATES:
        if length is not None and len(candidate) > length:
            continue
        if not _clashes(candidate, values):
            return candidate
    n = 1
    while n < 1000:
        candidate = f"O{n}"
        if length is None or len(candidate) <= length:
            if not _clashes(candidate, values):
                return candidate
        elif length is not None and length < 2:
            break
        n += 1
    return None


def _parse_date(value: str, date_format: str) -> Optional[datetime]:
    for fmt in (date_format, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _shift_date(value: str, days: int, date_format: str) -> Optional[str]:
    parsed = _parse_date(value, date_format)
    return (parsed + timedelta(days=days)).strftime(date_format) if parsed else None


def _shift_number(value: str, delta: int) -> Optional[str]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    n += delta
    return str(int(n)) if n == int(n) else str(n)


def build_domain(field_type: str, conditions: list[Condition], nullable: bool, case_insensitive: bool,
                 date_format: str, length: Optional[int] = None,
                 scale: Optional[int] = None) -> tuple[list[Value], Optional[Value], list[str]]:
    """Spec step 4: the values named in conditions, one extra 'other' value, null when allowed.

    Returns (domain, the 'other' value, warnings). Booleans always take both True and False.
    Text values keep the letter case written in the condition (data rule 2); when text
    comparisons ignore case, spellings that differ only by case are one value. A condition
    value longer than the field can never be stored, so it is left out with a warning.

    Spec, "Extensions beyond the pilot": "Coverage values are chosen strictly inside the ranges
    the conditions define, never on a bound." On a Number, Decimal or Date field compared with an
    ordering operator or Between, the bounds and the values named by the other operators cut the
    values into ranges, and the domain takes one value inside each range (``_range_values``), then
    the named values; a bound itself is never tried. Without such a condition the domain is the
    named values plus one 'other' value above them. A text field gets no value from an ordering
    operator or Between: no value inside a text range is chosen, and a warning says so.

    Spec, step 3: "The same operators with an absolute date value keep the earlier reading", so After,
    AtOrAfter, Before and AtOrBefore with an absolute value cut ranges like the ordering operators. A
    condition with a relative date names no value and cuts no range here: a field that has one gets its
    domain from ``CoverageEngine._clock_domain``.
    """
    warnings: list[str] = []
    values: list[Value] = []
    other: Optional[Value] = None
    if field_type == "Boolean":
        for cond in conditions:
            v = _normalise_boolean(cond.value)
            if v in ("True", "False") and v not in values:
                values.append(v)
        for v in ("True", "False"):
            if v not in values:
                values.append(v)
    elif field_type in ("Number", "Decimal", "Date"):
        is_date = field_type == "Date"
        named: list[str] = []
        bounds: list[str] = []
        for cond in conditions:
            if cond.value is None or cond.relative is not None:
                continue
            if cond.operator in RANGE_OPERATORS:
                bounds.extend(_range_bounds(cond))
            elif cond.value not in named:
                named.append(cond.value)
        if bounds:
            values = _range_values(named + bounds, is_date, date_format, scale if field_type == "Decimal" else 0)
            other = values[-1] if values else None
            values.extend(v for v in named if v not in values)
        else:
            values = list(named)
            if is_date:
                other = _shift_date(named[0], 1, date_format) if named else datetime(2000, 1, 1).strftime(date_format)
                while other in values:
                    other = _shift_date(other, 1, date_format)
            else:
                nums = [float(v) for v in named if _shift_number(v, 0) is not None]
                top = max(nums) + 1 if nums else 0
                other = str(int(top)) if top == int(top) else str(top)
            values.append(other)
    else:
        seen: set[str] = set()
        ranges: list[str] = []
        for cond in conditions:
            v = cond.value
            if v is None or cond.relative is not None:
                continue
            if cond.operator in RANGE_OPERATORS:
                if cond.operator not in ranges:
                    ranges.append(cond.operator)
                continue
            if length is not None and len(v) > length:
                warnings.append(f"il valore {v!r} della condizione è più lungo del campo ({length} caratteri), "
                                "quindi quella condizione non può mai essere vera")
                continue
            key = v.lower() if case_insensitive else v
            if key not in seen:
                seen.add(key)
                values.append(v)
        if ranges:
            warnings.append(f"è confrontato con {', '.join(ranges)} su un campo di tipo {field_type}: il generatore non "
                            "sceglie valori dentro gli intervalli di testo, quindi i branch che li richiedono possono "
                            "risultare non coperti")
        named = [v for v in values if v is not None]
        if field_type == "EmailAddress":
            other = OTHER_EMAIL if not _clashes(OTHER_EMAIL, named) else _other_text(named, length)
        elif field_type == "Phone":
            other = OTHER_PHONE if not _clashes(OTHER_PHONE, named) else _other_text(named, length)
        else:
            other = _other_text(named, length)
        if other is None:
            warnings.append(f"nel campo ({length} caratteri) non entra nessun valore diverso da quelli delle condizioni")
        else:
            values.append(other)
    if nullable:
        values.append(None)
    return values, other, warnings


def _range_bounds(cond: Condition) -> list[str]:
    """The bounds, as written, at which an ordering or Between condition cuts the values (never a relative date)."""
    if cond.value is None or cond.value_attribute is not None or cond.relative is not None:
        return []
    if cond.operator == "Between":
        bounds = criteria.between_bounds(cond.value)
        return list(bounds) if bounds is not None else []
    return [cond.value] if cond.operator in RANGE_OPERATORS else []


_DATE_ONLY = re.compile(r"\d{4}-?\d{2}-?\d{2}")


def _range_values(points: list[str], is_date: bool, date_format: str, scale: Optional[int]) -> list[str]:
    """One value strictly inside each range the points cut: below the lowest, between two neighbours, above
    the highest, in ascending order. A range with no storable value inside (two neighbouring integers, two
    dates a day apart compared on the date) gets none. Points that cannot be read are ignored."""
    if is_date:
        cuts = sorted({d for d in (_parse_date(p.strip(), date_format) for p in points) if d is not None})
        if not cuts:
            return []
        # A condition date written without a time is compared on the date part (criteria.py), so the
        # values must then fall on other days.
        whole_days = any(_DATE_ONLY.fullmatch(p.strip()) for p in points)
        candidates = [cuts[0] - timedelta(days=1)]
        for a, b in zip(cuts, cuts[1:]):
            if whole_days:
                days = (b.date() - a.date()).days
                if days >= 2:
                    candidates.append(datetime.combine(a.date() + timedelta(days=days // 2), time()))
            else:
                candidates.append((a + (b - a) / 2).replace(second=0, microsecond=0))
        candidates.append(cuts[-1] + timedelta(days=1))
        out: list[str] = []
        for c in candidates:
            text = c.strftime(date_format)
            back = _parse_date(text, date_format)
            if back is None or text in out:
                continue
            key = back.date() if whole_days else back
            if all((key < (k.date() if whole_days else k)) or (key > (k.date() if whole_days else k)) for k in cuts):
                out.append(text)
        return out
    cuts = sorted({n for n in (_decimal(p) for p in points) if n is not None})
    if not cuts:
        return []
    numbers: list[Decimal] = []
    below = cuts[0].to_integral_value(rounding=ROUND_FLOOR)
    numbers.append(below if below < cuts[0] else cuts[0] - 1)
    for a, b in zip(cuts, cuts[1:]):
        middle = (a + b) / 2
        for candidate in (middle.to_integral_value(rounding=ROUND_FLOOR),
                          middle.to_integral_value(rounding=ROUND_CEILING), _at_scale(middle, scale)):
            if candidate is not None and a < candidate < b:
                numbers.append(candidate)
                break
    above = cuts[-1].to_integral_value(rounding=ROUND_CEILING)
    numbers.append(above if above > cuts[-1] else cuts[-1] + 1)
    return [_number_text(n) for n in numbers]


def _decimal(text: str) -> Optional[Decimal]:
    try:
        number = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _at_scale(number: Decimal, scale: Optional[int]) -> Optional[Decimal]:
    """A number cut to the digits a Decimal field keeps; None for a Number field (no decimals)."""
    if scale is None:
        return number
    if scale <= 0:
        return None
    return number.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_FLOOR)


def _number_text(number: Decimal) -> str:
    """CSV form of a number: no exponent, no trailing zeros, no decimal point for a whole number."""
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")

# ---------------------------------------------------------------------------
# Exploration results
# ---------------------------------------------------------------------------

@dataclass
class Leaf:
    """One complete simulated path for one contact class and one partial scenario assignment."""
    id: int
    class_id: int
    assigned: dict[FieldRef, Value]            # scenario-level values the path depended on
    manual: tuple[ManualAction, ...]
    randoms: tuple[tuple[str, str], ...]       # (random split key, outcome key) choices made
    steps: list[Step]
    emails_sent: list[str]
    writes: list[Write]
    waits: list[str]
    stop: str
    stop_detail: str
    mask: int = 0                              # bitmask over split branches crossed, then exit criteria met
    unknown_from: Optional[str] = None         # the walk stopped here without reaching an exit
    # Spec, step 3: the clock at every step, the SMS sent, the wait at whose end the exit criteria took it out.
    timeline: list[TimelinePoint] = field(default_factory=list)
    sms_sent: list[str] = field(default_factory=list)
    exit_by_criteria: Optional[str] = None

    @property
    def deterministic(self) -> bool:
        return not self.randoms

    @property
    def end_activity_key(self) -> Optional[str]:
        return self.steps[-1].activity_key if self.steps else None


@dataclass(frozen=True)
class ClockPoint:
    """One moment at which a condition on a clock field is evaluated (spec, step 3, "Values of a date field
    read with relative dates or by a wait with an offset": "every fixed delay with which a contact can reach
    that split").

    ``site`` is the decision split, or EXIT_PREFIX + the criterion key for an exit criterion (evaluated at the
    end of a wait). The clock is the entry time plus ``delay`` days, or the end of one of the ``anchors`` when
    that is later: the waits by attribute on the field that precede the site on some path.
    """
    cond: Condition
    site: str
    delay: float
    anchors: tuple[str, ...] = ()


@dataclass
class _SubClass:
    """The class of one component of the contact-level fields (``CoverageEngine.build_classes``)."""
    values: dict[FieldRef, Value]                  # the representative values of the component's fields
    groups: dict[int, tuple[bool, ...]]            # predicate index -> its truths at its moments
    count: int                                     # how many combinations of the component it stands for
    first: tuple[int, ...]                         # domain indexes of its first member in product order
    best: tuple[int, ...]                          # the preference of the representative


@dataclass
class BranchNode:
    field: FieldRef
    children: dict[Value, "Node"]


@dataclass
class ChoiceNode:
    children: list["Node"]


@dataclass
class LeafNode:
    leaf: Leaf


Node = BranchNode | ChoiceNode | LeafNode


def lookup_leaves(node: Node, assignment: dict[FieldRef, Value]) -> list[Leaf]:
    """The leaves a full scenario assignment reaches in one class's tree (several through choice nodes)."""
    out: list[Leaf] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if isinstance(n, LeafNode):
            out.append(n.leaf)
        elif isinstance(n, BranchNode):
            child = n.children.get(assignment.get(n.field))
            if child is not None:
                stack.append(child)
        else:
            stack.extend(n.children)
    return out


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

class CoverageEngine:
    def __init__(self, package: Package, table_model: TableModel, settings: Settings,
                 generation_time: Optional[datetime] = None):
        self.package = package
        self.journey = package.journey
        self.table_model = table_model
        self.settings = settings
        self.now = generation_time or settings.generation_time or datetime.now()
        # Spec, step 3, "The clock": "The clock starts at the entry time, a setting whose default is the
        # generation time".
        self.entry_time = settings.entry_time or self.now
        self.schema = graph.Schema(package.tables)
        self.warnings: list[str] = []
        # Spec, "Row present or absent": lower-case table -> its field linked to the contact key (canonical).
        self.row_fields: dict[str, FieldRef] = {}
        for ref in table_model.existence_tested.values():
            linked = self.schema.canonical(ref)
            self.row_fields[linked[0].lower()] = linked
        # Spec, step 3, "Columns the entry query derives and columns it copies": member -> representative.
        self.alias_of: dict[FieldRef, FieldRef] = self._alias_map()
        self.walker = graph.Walker(self.journey, self.schema, package.emails,
                                   settings.case_insensitive_text, self.now, settings.date_format,
                                   row_fields=self.row_fields, entry_time=self.entry_time,
                                   ends_at_once_when_unknown=self._wait_ends_at_once)
        self.walker.on_decision = self._observe_decision
        # Spec, step 3: the exit criteria the walker evaluates at the end of every wait, and the fixed delays
        # with which a contact reaches each activity (days; the waits by attribute count zero).
        self.exits: list[JourneyFilter] = graph.evaluable_exits(self.journey)
        self.delays, self.delay_warnings = graph.fixed_delays(self.journey)
        # The exit criteria are only ever evaluated at the end of a wait: without a reachable wait they read no
        # field and take no contact out (their targets are then not covered, with that reason).
        self.waits_reached_by_paths = any(a.kind in graph.WAIT_KINDS and self.delays.get(a.key)
                                          for a in self.journey.activities.values())
        self.active_exits: list[JourneyFilter] = self.exits if self.waits_reached_by_paths else []
        self.preds = graph.predecessors(self.journey)
        self._ancestor_cache: dict[str, set[str]] = {}
        # Spec, step 3, "Validity of the data": field -> domain value -> validity in days (None: not perishable),
        # the condition that reads it differently first, and the cut points of each clock field (days).
        self.validity: dict[FieldRef, dict[Value, Optional[int]]] = {}
        self.validity_change: dict[FieldRef, dict[Value, Optional[ClockPoint]]] = {}
        self.cut_points: dict[FieldRef, list[float]] = {}
        self.condition_sites: dict[FieldRef, list[tuple[Condition, str]]] = {}
        # Spec, step 3, "Exit criteria and goals": per wait, the paths that leave there by the exit criteria and
        # those that go past it; the criteria met there; the leaving paths, to walk them on (``_exit_shadow``).
        self.exited_at: dict[str, int] = {}
        self.passed_at: dict[str, int] = {}
        self.exit_met_at: dict[str, set[str]] = {}
        self.exit_states: list[tuple[ContactClass, graph.WalkState, dict[FieldRef, Value]]] = []
        self._shadow: Optional[dict[BranchRef, set[str]]] = None
        self.case_exits: dict[str, tuple[list[BranchRef], list[BranchRef]]] = {}
        # filled by run()
        self.fields: list[FieldRef] = []
        self.conditions_by_field: dict[FieldRef, list[Condition]] = {}
        self.levels: dict[FieldRef, str] = {}
        self.domains: dict[FieldRef, list[Value]] = {}
        self.other_values: dict[FieldRef, Value] = {}
        self.named_points: dict[FieldRef, set[Value]] = {}   # range fields: domain values that are condition values
        self.contact_fields: list[FieldRef] = []
        self.scenario_fields: list[FieldRef] = []
        self.classes: list[ContactClass] = []
        self.leaves: list[Leaf] = []
        self.trees: dict[int, Node] = {}
        self.branch_index: dict[BranchRef, int] = {}
        self.branch_list: list[tuple[Activity, Outcome]] = []
        self.engagement_blocked: dict[BranchRef, str] = {}
        self.reached_values: dict[str, dict[FieldRef, set[Value]]] = {}
        self.reached_activities: set[str] = set()
        self.exploration_truncated = False
        self.classes_incomplete = False
        # Elements of a supported type the generator cannot handle for some contacts: an activity where some paths
        # stop (an Update contact whose insert may fail), or a branch "<split>/<outcome>" whose range on a text
        # field gets no value (CoverageResult.unsupported_reasons): key -> why.
        self.stopped_at: dict[str, str] = {}

    # -- aliases (spec, step 3) ------------------------------------------------------------

    def _alias_map(self) -> dict[FieldRef, FieldRef]:
        """Spec, step 3: "A column of the entry table copied as a plain column from a source table ... holds the
        same value as its source: the two fields are one variable for the coverage". Each group of
        ``TableModel.aliases`` is represented by its first member whose table is not the entry table (the source
        column: the entry copy is derived from it), else by its first member. Member -> representative, canonical
        refs, representatives left out."""
        entry = (self.table_model.entry_table or "").lower()
        out: dict[FieldRef, FieldRef] = {}
        for group in self.table_model.aliases:
            members = [self.schema.canonical(ref) for ref in group]
            if not members:
                continue
            representative = next((m for m in members if m[0].lower() != entry), members[0])
            for member in members:
                if member != representative:
                    out[member] = representative
        return out

    def _var(self, ref: FieldRef) -> FieldRef:
        """The variable a canonical field ref stands for: its alias representative, or itself."""
        return self.alias_of.get(ref, ref)

    def _cond_var(self, cond: Condition) -> FieldRef:
        return self._var(self.schema.canonical((cond.table, cond.field)))

    def _wait_ends_at_once(self, ref: FieldRef) -> bool:
        """graph.Walker: a wait by attribute whose field is not decided ends at once unless the coverage lists
        values for it (a field a condition reads); then the walk stops so that the exploration branches on it."""
        return self._var(ref) not in self.domains

    # -- step 3 and 4: fields, levels, domains ------------------------------------------

    def collect_fields(self) -> None:
        """Every field read by a decision split or by an evaluable exit criterion, in order of first appearance
        (splits first). Spec, step 3: the exit criteria use "the same operators, data sources and relative dates
        as a decision split". Conditions on any member of an alias group attach to its representative."""
        sites: list[tuple[Condition, str]] = []
        for activity in self.journey.activities.values():
            if activity.kind != KIND_DECISION:
                continue
            for outcome in activity.outcomes:
                if outcome.unsupported_reason:
                    continue   # never evaluated: the walk stops there
                sites.extend((cond, activity.key) for cond in formula_conditions(outcome.formula))
        for exit_ in self.active_exits:
            sites.extend((cond, EXIT_PREFIX + exit_.key) for cond in formula_conditions(exit_.formula))
        for cond, site in sites:
            ref = self._cond_var(cond)
            if ref not in self.conditions_by_field:
                self.fields.append(ref)
                self.conditions_by_field[ref] = []
                self.condition_sites[ref] = []
            self.conditions_by_field[ref].append(cond)
            self.condition_sites[ref].append((cond, site))
        journey_data = {self._cond_var(c) for conds in self.conditions_by_field.values()
                        for c in conds if c.source == SOURCE_JOURNEY}
        for ref in self.fields:
            read_by = ("da uno split" if any(not site.startswith(EXIT_PREFIX) for _, site in self.condition_sites[ref])
                       else "dai criteri di uscita (exit criteria)")
            level = self.table_model.field_levels.get(ref)
            if level is None and ref in journey_data:
                # Spec, "Data sources of a condition": Journey Data is a column of the entry row, one per contact.
                level = LEVEL_CONTACT
            elif level is None:
                level = LEVEL_CONTACT
                self.warnings.append(
                    f"il campo {ref[0]}.{ref[1]} è letto {read_by}, ma la sua tabella non è collegata alla tabella di ingresso: "
                    "è trattato come campo a livello di contatto (assunzione)")
            self.levels[ref] = level
            if self.schema.table(ref[0]) is None or self.schema.table(ref[0]).field(ref[1]) is None:
                self.warnings.append(f"il campo {ref[0]}.{ref[1]} è letto {read_by}, ma non è negli schemi dell'export: è trattato come testo che ammette null")
        self.contact_fields = [f for f in self.fields if self.levels[f] == LEVEL_CONTACT]
        self.scenario_fields = [f for f in self.fields if self.levels[f] == LEVEL_SCENARIO]

    def build_domains(self) -> None:
        fixed, not_null = self._entry_pins()
        key_fields = {ref for g in self.table_model.key_groups if g.kind in ("contact", "scenario") for ref in g.fields}
        for ref in self.fields:
            conds = self.conditions_by_field[ref]
            values = [c.value for c in conds if c.value is not None]
            ftype = self.schema.field_type(ref)
            table = self.schema.table(ref[0])
            field = table.field(ref[1]) if table is not None else None
            length = field.length if field is not None else None
            self._flag_case_variants(ref, ftype, values)
            if ref in self.row_fields.values():
                # Spec, "Row present or absent": the contact has a row (its linked field holds the generated
                # key) or has none (the field reads null).
                self.domains[ref] = [KEY_MARKER, None]
                self.other_values[ref] = KEY_MARKER
                continue
            if ref in key_fields:
                # Data rule 1 wins over rule 2: a key field always holds the generated key, so a split
                # comparing it with a value can never match; IsNotNull stays true.
                self.domains[ref] = [KEY_MARKER]
                self.other_values[ref] = KEY_MARKER
                if values:
                    self.warnings.append(
                        f"{ref[0]}.{ref[1]} è letto da uno split, ma è un campo chiave: contiene la chiave generata, "
                        "quindi una condizione che lo confronta con un valore non può mai essere vera")
                continue
            scale = field.scale if field is not None else None
            clock = self._is_clock_field(ref, ftype)
            if clock:
                domain, other, warnings = self._clock_domain(ref, conds, self.schema.is_nullable(ref)), None, []
            else:
                domain, other, warnings = build_domain(ftype, conds, self.schema.is_nullable(ref),
                                                       self.settings.case_insensitive_text, self.settings.date_format,
                                                       length, scale)
            if (not clock and ftype in ("Number", "Decimal", "Date")
                    and any(c.operator in RANGE_OPERATORS and c.relative is None for c in conds)):
                self.named_points[ref] = {c.value for c in conds if c.operator not in RANGE_OPERATORS}
            for w in warnings:
                self.warnings.append(f"{ref[0]}.{ref[1]}: {w}")
            self.other_values[ref] = other
            if ref in fixed:
                # Data rule 3: a field filtered by the entry query holds a value that passes the filter, so a
                # contact that enters the journey can only show that value to the splits reading it.
                value = _normalise_boolean(fixed[ref]) if ftype == "Boolean" else fixed[ref]
                domain = [value]
                shown = "null" if value is None else repr(value)
                self.warnings.append(
                    f"{ref[0]}.{ref[1]} è letto da uno split ed è fissato dalla entry query: i contatti che entrano "
                    f"nel journey hanno sempre {shown}, quindi si prova solo quel valore")
                if clock:
                    days, change = self._clock_validity(ref, value)
                    self.validity[ref] = {value: days}
                    self.validity_change[ref] = {value: change}
            elif ref in not_null and None in domain:
                domain = [v for v in domain if v is not None]
                self.warnings.append(
                    f"{ref[0]}.{ref[1]} è letto da uno split e la entry query lo filtra con IS NOT NULL, quindi null non si prova")
            self.domains[ref] = domain

    def _flag_case_variants(self, ref: FieldRef, ftype: str, values: list[str]) -> None:
        """Spec, report: 'the same value written in two letter cases (Email and EMAIL in the pilot)'."""
        if ftype in ("Boolean", "Date", "Number", "Decimal"):
            return
        by_lower: dict[str, list[str]] = {}
        for v in values:
            by_lower.setdefault(v.lower(), [])
            if v not in by_lower[v.lower()]:
                by_lower[v.lower()].append(v)
        for spellings in by_lower.values():
            if len(spellings) > 1:
                shown = " e ".join(repr(s) for s in spellings)
                if self.settings.case_insensitive_text:
                    self.warnings.append(
                        f"pattern sospetto: {ref[0]}.{ref[1]} è confrontato con {shown}; "
                        "sono trattati come un solo valore perché i confronti di testo non distinguono maiuscole e minuscole")
                else:
                    self.warnings.append(
                        f"pattern sospetto: {ref[0]}.{ref[1]} è confrontato con {shown}; "
                        "sono valori diversi perché i confronti di testo distinguono maiuscole e minuscole (impostazione)")

    def _entry_pins(self) -> tuple[dict[FieldRef, Value], set[FieldRef]]:
        """Fields pinned by the entry query: equality and IS NULL filters, literal and current-date
        select expressions (every entry row gets that value), and the fields filtered IS NOT NULL.
        Keyed by variable (alias representative)."""
        pinned, not_null = self._entry_pins_by_field()
        out: dict[FieldRef, Value] = {}
        for ref, value in pinned.items():
            out.setdefault(self._var(ref), value)
        return out, {self._var(ref) for ref in not_null}

    def _entry_pins_by_field(self) -> tuple[dict[FieldRef, Value], set[FieldRef]]:
        query = self.table_model.entry_query
        pinned: dict[FieldRef, Value] = {}
        not_null: set[FieldRef] = set()
        if query is None:
            return pinned, not_null
        for flt in query.filters:
            if isinstance(flt, EqualityFilter):
                ref = self._filter_ref(flt.column, query)
                if ref is not None and ref not in pinned:
                    pinned[ref] = flt.value
            elif isinstance(flt, IsNullFilter):
                ref = self._filter_ref(flt.column, query)
                if ref is None:
                    continue
                if flt.negated:
                    not_null.add(ref)
                elif ref not in pinned:
                    pinned[ref] = None
        entry = self.schema.table(self.table_model.entry_table)
        if entry is not None:
            for item in query.select:
                field = entry.field(item.target_column)
                if field is None:
                    continue
                ref = (entry.name, field.name)
                if isinstance(item.expr, SelectLiteral) and ref not in pinned:
                    pinned[ref] = item.expr.value
                elif isinstance(item.expr, SelectNull) and ref not in pinned:
                    pinned[ref] = None
                elif isinstance(item.expr, SelectCurrentDate) and ref not in pinned:
                    pinned[ref] = self.now.strftime(self.settings.date_format)
        return pinned, not_null

    def _filter_ref(self, column, query) -> Optional[FieldRef]:
        if column.table:
            return self.schema.canonical((column.table, column.column))
        matches = []
        for source in query.sources:
            table = self.schema.table(source.table)
            if table is not None and table.field(column.column) is not None and table.name not in [m[0] for m in matches]:
                matches.append((table.name, table.field(column.column).name))
        return matches[0] if len(matches) == 1 else None

    # -- the clock: relative dates and waits by attribute with an offset (spec, step 3) -------

    def _is_clock_field(self, ref: FieldRef, ftype: str) -> bool:
        """Spec, step 3: "For a field with at least one relative condition or one wait by attribute with an
        offset" the values are chosen as offsets from the entry time. Only a Date field."""
        if ftype != "Date":
            return False
        if any(c.relative is not None for c in self.conditions_by_field.get(ref, [])):
            return True
        return any(w.wait_offset_amount for w in self._attribute_waits(ref))

    def _attribute_waits(self, ref: FieldRef) -> list[Activity]:
        """The waits by attribute whose date field is the variable ``ref``, in export order."""
        return [a for a in self.journey.activities.values()
                if a.kind == KIND_WAIT_ATTRIBUTE and a.wait_field is not None
                and self._var(self.schema.canonical(a.wait_field)) == ref]

    def _ancestors(self, key: str) -> set[str]:
        """The activities from which ``key`` can be reached along the outcomes (any path)."""
        found = self._ancestor_cache.get(key)
        if found is None:
            found = set()
            stack = list(self.preds.get(key, ()))
            while stack:
                k = stack.pop()
                if k in found:
                    continue
                found.add(k)
                stack.extend(self.preds.get(k, ()))
            self._ancestor_cache[key] = found
        return found

    def _wait_end_delays(self) -> list[tuple[str, float]]:
        """``(wait key, delay in days at its end)`` for every fixed delay of every wait: the moments at which the
        exit criteria are evaluated (spec, step 3: "when the contact's time in a wait activity expires"). A wait
        by duration adds its amount (graph.fixed_wait_minutes); a wait by attribute and a send-time wait add 0."""
        out: list[tuple[str, float]] = []
        for activity in self.journey.activities.values():
            if activity.kind not in graph.WAIT_KINDS:
                continue
            extra = graph.fixed_wait_minutes(activity) or 0
            for delay in sorted(self.delays.get(activity.key, ())):
                out.append((activity.key, (round(delay * graph.MINUTES_PER_DAY) + extra) / graph.MINUTES_PER_DAY))
        return out

    def _site_moments(self, site: str) -> list[tuple[float, set[str]]]:
        """``(delay, activities before it)`` for every moment a condition at ``site`` is evaluated: each fixed
        delay of a decision split; the end of every wait for an exit criterion (the wait itself counts as before:
        its end is the moment)."""
        if site.startswith(EXIT_PREFIX):
            return [(delay, {key} | self._ancestors(key)) for key, delay in self._wait_end_delays()]
        return [(delay, self._ancestors(site)) for delay in sorted(self.delays.get(site, ()))]

    def _clock_points(self, ref: FieldRef) -> list[ClockPoint]:
        """Every (condition, delay) at which a condition on the clock field ``ref`` is evaluated, with the waits by
        attribute on the field that precede it (the anchors of its clock), in order of the conditions."""
        waits = self._attribute_waits(ref)
        points: list[ClockPoint] = []
        seen: set[tuple] = set()
        for index, (cond, site) in enumerate(self.condition_sites.get(ref, [])):
            for delay, before in self._site_moments(site):
                anchors = tuple(w.key for w in waits if w.key in before)
                key = (index, delay, anchors)
                if key not in seen:
                    seen.add(key)
                    points.append(ClockPoint(cond, site, delay, anchors))
        return points

    def _point_clock(self, point: ClockPoint, moment: Optional[datetime], slip: int) -> datetime:
        """The clock at ``point`` for a contact whose field holds ``moment``, with the entry time slipped by
        ``slip`` days: the entry time plus the delay, or the end of an anchor wait when that is later (spec,
        step 3, "The clock": the wait "holds the contact until the date in its field plus its offset")."""
        now = self.entry_time + timedelta(days=slip + point.delay)
        if moment is not None:
            for key in point.anchors:
                try:
                    end = graph.wait_end(self.journey.activities[key], moment)
                except criteria.UnsupportedOperator:
                    continue
                if end > now:
                    now = end
        return now

    def _clock_vector(self, points: list[ClockPoint], value: Value, slip: int = 0) -> tuple:
        """The truth of every point for ``value`` (None where the condition cannot be evaluated)."""
        moment = self.walker.read_date(value)
        out = []
        for point in points:
            try:
                out.append(criteria.evaluate_condition(point.cond, value, "Date", self.settings.case_insensitive_text,
                                                       now=self._point_clock(point, moment, slip)))
            except criteria.UnsupportedOperator:
                out.append(None)
        return tuple(out)

    def _behaviour(self, ref: FieldRef, value: Value, slip: int, intern: dict[tuple, int],
                   visited: Optional[set] = None) -> int:
        """How a contact whose clock field ``ref`` holds ``value`` goes through the journey when the entry time
        slips by ``slip`` days, every other field undecided: an interned signature of the decisions on every path.

        Spec, step 3: "Candidates that take the same branches at every split that reads the field, whatever the
        delay, are one class". The journey is walked from the start with the clock of each path (waits by duration
        advance it, a wait by attribute on the field holds the contact until its end, a wait by attribute on
        another field counts 0, as in the fixed delays). At a decision split each outcome's formula is reduced
        with the value (``_residual``): false, the next outcome is tried; true, it is taken; undecided (it also
        depends on other fields), both it and the next outcomes are followed. At the end of every wait the exit
        criteria are reduced in the same way (true: the path ends there). Engagement and random splits follow
        every outcome. Update contact writes into the field are read by later Contact Data conditions. The
        signature keeps the activity keys, outcomes and residuals, never the clock, so two values that decide the
        same way on the same paths have the same signature even when a wait on the field ends at another time.
        ``visited`` collects ``(site, days from the entry)`` for every decision split and exit evaluation."""
        entry = self.entry_time + timedelta(days=slip)
        activities = self.journey.activities
        memo: dict[tuple, int] = {}
        on_path: set[str] = set()

        def sign(structure: tuple) -> int:
            found = intern.get(structure)
            if found is None:
                found = intern[structure] = len(intern)
            return found

        def readers(written: dict[FieldRef, Value]):
            def lookup(r: FieldRef):
                if r in written:
                    return written[r]
                return value if self._var(r) == ref else graph.UNKNOWN

            def snapshot(r: FieldRef):
                return value if self._var(r) == ref else graph.UNKNOWN
            return lookup, snapshot

        def offset(clock: datetime) -> float:
            return round((clock - entry) / timedelta(days=1), 6)

        def visit(key: Optional[str], clock: datetime, written: tuple) -> int:
            if key is None:
                return sign(("E",))
            state = (key, clock, written)
            if state in memo:
                return memo[state]
            activity = activities.get(key)
            if activity is None:
                return sign(("dangling", key))
            if key in on_path:
                return sign(("loop", key))
            on_path.add(key)
            written_map = dict(written)
            lookup, snapshot = readers(written_map)
            nxt = activity.outcomes[0].next if activity.outcomes else None
            if activity.kind == KIND_DECISION:
                if visited is not None:
                    visited.add((key, offset(clock)))
                remainder = [o for o in activity.outcomes if o.is_remainder or o.formula is None]
                parts: list[tuple] = []
                for outcome in [o for o in activity.outcomes if o not in remainder] + remainder:
                    if outcome.is_remainder or outcome.formula is None:
                        parts.append((outcome.key, True, visit(outcome.next, clock, written)))
                        break
                    if outcome.unsupported_reason:
                        parts.append((outcome.key, "unsupported"))
                        break
                    result = self._residual(outcome.formula, lookup, clock, snapshot)
                    if result is False:
                        parts.append((outcome.key, False))
                        continue
                    parts.append((outcome.key, result, visit(outcome.next, clock, written)))
                    if result is True:
                        break
                signature = sign(("D", key, tuple(parts)))
            elif activity.kind in (KIND_ENGAGEMENT, KIND_RANDOM):
                signature = sign(("C", key, tuple(visit(o.next, clock, written) for o in activity.outcomes)))
            elif activity.kind in graph.WAIT_KINDS:
                end = clock
                if activity.kind == KIND_WAIT_DURATION:
                    end = clock + timedelta(minutes=graph.fixed_wait_minutes(activity) or 0)
                elif activity.kind == KIND_WAIT_ATTRIBUTE and activity.wait_field is not None \
                        and self._var(self.schema.canonical(activity.wait_field)) == ref:
                    moment = self.walker.read_date(lookup(self.schema.canonical(activity.wait_field)))
                    if moment is not None:
                        try:
                            end = max(clock, graph.wait_end(activity, moment))
                        except criteria.UnsupportedOperator:
                            pass
                exits: list[tuple] = []
                leaves = False
                for exit_ in self.active_exits:
                    if visited is not None:
                        visited.add((EXIT_PREFIX + exit_.key, offset(end)))
                    result = self._residual(exit_.formula, lookup, end, snapshot)
                    if result is True:
                        leaves = True
                        break
                    if result is not False:
                        exits.append((exit_.key, result))
                if leaves:
                    signature = sign(("X", key))
                else:
                    signature = sign(("W", key, tuple(exits), visit(nxt, end, written)))
            elif activity.kind == KIND_UPDATE:
                changed = dict(written_map)
                for w in activity.writes:
                    member = self.schema.canonical((w.table, w.field))
                    if self._var(member) == ref:
                        changed[member] = (clock.strftime(self.settings.date_format) if w.value == graph.DATETIME_NOW
                                           else w.value)
                signature = visit(nxt, clock, tuple(sorted(changed.items(), key=lambda kv: kv[0])))
            elif activity.kind == KIND_UNSUPPORTED:
                signature = sign(("U", key))
            else:
                signature = visit(nxt, clock, written)
            on_path.discard(key)
            memo[state] = signature
            return signature

        return visit(self.journey.start_key, entry, ())

    def _clock_validity(self, ref: FieldRef, value: Value,
                        intern: Optional[dict[tuple, int]] = None) -> tuple[Optional[int], Optional[ClockPoint]]:
        """Spec, step 3, "Validity of the data": "the number of whole days the entry time can slip before any
        condition on the field, at any delay a contact can reach it with, reads differently; the generator
        computes it by re-evaluating the conditions with the entry time moved by one day at a time (so an
        exclusive operator gives one day less than the distance from the cut point). A value that no slip of up
        to a year changes is not perishable".

        The largest k such that every slip j <= k gives the behaviour of slip 0 (``_behaviour``: the same decisions
        on the same paths), and the condition that reads the value differently first; (None, None) when no slip up
        to MAX_SLIP_DAYS changes it. The slips are tried one day at a time for the first SLIP_SCAN_DAYS; beyond, the
        behaviour of slip MAX_SLIP_DAYS is compared and the first change found by bisection, reading a change as
        permanent (each condition is monotone in the slip: the clock only moves forward, a relative reference
        with it)."""
        intern = {} if intern is None else intern
        visited: set = set()
        base = self._behaviour(ref, value, 0, intern, visited)
        changed_at: Optional[int] = None
        for slip in range(1, SLIP_SCAN_DAYS + 1):
            if self._behaviour(ref, value, slip, intern) != base:
                changed_at = slip
                break
        if changed_at is None:
            if self._behaviour(ref, value, MAX_SLIP_DAYS, intern) == base:
                return None, None
            low, high = SLIP_SCAN_DAYS, MAX_SLIP_DAYS     # slip ``low`` behaves as slip 0, slip ``high`` does not
            while high - low > 1:
                middle = (low + high) // 2
                if self._behaviour(ref, value, middle, intern) == base:
                    low = middle
                else:
                    high = middle
            changed_at = high
        return changed_at - 1, self._changed_point(ref, value, changed_at, visited)

    def _changed_point(self, ref: FieldRef, value: Value, slip: int, visited: set) -> Optional[ClockPoint]:
        """The condition that reads ``value`` differently at ``slip`` days: the first clock point of the field whose
        truth changes, preferring one evaluated on the value's own paths (``visited``)."""
        points = self._clock_points(ref)
        before = self._clock_vector(points, value, 0)
        after = self._clock_vector(points, value, slip)
        changed = [p for p, a, b in zip(points, before, after) if a != b]
        on_paths = [p for p in changed if (p.site, round(p.delay, 6)) in visited]
        return (on_paths or changed or [None])[0]

    def _days_from_entry(self, moment: datetime, day_level: bool) -> float:
        """A moment as days from the entry time: whole days between the dates at day level (the candidates keep
        the entry's time of day), the exact fraction otherwise."""
        if day_level:
            return float((moment.date() - self.entry_time.date()).days)
        return round((moment - self.entry_time) / timedelta(days=1), 6)

    def _cut_points(self, ref: FieldRef, points: list[ClockPoint], conds: list[Condition]) -> list[float]:
        """Spec, step 3: "the cut points in days: for each relative condition, n plus every fixed delay with which
        a contact can reach that split ...; for each wait by attribute on the field, minus its offset plus the
        same delays (the value at which the wait ends exactly when the contact reaches it)". The reference of a
        relative condition is ``criteria.shift_date`` of the clock (months and years are calendar steps); the
        bounds of the absolute ordering conditions on the field are cut points too."""
        cuts: set[float] = set()
        for point in points:
            rel = point.cond.relative
            if rel is None:
                continue
            try:
                moment = criteria.shift_date(self.entry_time + timedelta(days=point.delay), rel)
                cuts.add(self._days_from_entry(moment, criteria.is_day_level(rel)))
            except criteria.UnsupportedOperator:
                continue
        for wait in self._attribute_waits(ref):
            amount = wait.wait_offset_amount or 0
            unit = graph.offset_unit(wait.wait_offset_unit) if amount else "days"
            if unit is None:
                continue
            for delay in sorted(self.delays.get(wait.key, ())):
                reached = self.entry_time + timedelta(days=delay)
                try:
                    moment = (criteria.shift_date(reached, RelativeDate(sign=-1 if amount > 0 else 1, amount=abs(amount),
                                                                        unit=unit)) if amount else reached)
                except criteria.UnsupportedOperator:
                    continue
                cuts.add(self._days_from_entry(moment, False))
        for cond in conds:
            if cond.relative is not None or cond.operator not in RANGE_OPERATORS:
                continue
            for bound in _range_bounds(cond):
                parsed = _parse_date(bound.strip(), self.settings.date_format)
                if parsed is not None:
                    cuts.add(self._days_from_entry(parsed, bool(_DATE_ONLY.fullmatch(bound.strip()))))
        return sorted(cuts)

    def _clock_candidates(self, cuts: list[float], conds: list[Condition], nullable: bool) -> list[Value]:
        """Spec, step 3: "The candidate values are the entry time moved by every cut point and by every cut point
        plus or minus one and two days, plus the absolute values named by the other conditions, plus null when
        the schema allows it." Written in the date format of the CSVs, in order of the cut points."""
        fmt = self.settings.date_format
        out: list[Value] = []
        seen: set[str] = set()
        for cut in cuts or [0.0]:
            for step in (-2, -1, 0, 1, 2):
                text = (self.entry_time + timedelta(days=cut + step)).strftime(fmt)
                if text not in seen:
                    seen.add(text)
                    out.append(text)
        for cond in conds:
            if (cond.relative is None and cond.value is not None and cond.value_attribute is None
                    and cond.operator not in RANGE_OPERATORS and cond.value not in seen):
                seen.add(cond.value)
                out.append(cond.value)
        if nullable:
            out.append(None)
        return out

    def _clock_domain(self, ref: FieldRef, conds: list[Condition], nullable: bool) -> list[Value]:
        """Spec, step 3, "Values of a date field read with relative dates or by a wait with an offset":
        "Candidates that take the same branches at every split that reads the field, whatever the delay, are one
        class; the generator keeps one value per class". A class here is a behaviour (``_behaviour``): the same
        decisions on the same paths, each split evaluated with the clock of the path that reaches it (a delay with
        which only other values reach a split does not separate two values). One representative per class
        (``_clock_representative``), sorted by date, null last; their validity is kept in ``validity``, the cut
        points in ``cut_points``."""
        points = self._clock_points(ref)
        cuts = self._cut_points(ref, points, conds)
        candidates = self._clock_candidates(cuts, conds, nullable)
        fmt = self.settings.date_format
        cut_texts = {(self.entry_time + timedelta(days=cut)).strftime(fmt) for cut in cuts}
        intern: dict[tuple, int] = {}
        classes: dict[int, list[Value]] = {}
        for value in candidates:
            classes.setdefault(self._behaviour(ref, value, 0, intern), []).append(value)
        validity = {value: self._clock_validity(ref, value, intern) for value in candidates}
        waits = self._attribute_waits(ref)
        chosen = [self._clock_representative(members, validity, cut_texts, waits) for members in classes.values()]
        chosen.sort(key=lambda v: (v is None, self.walker.read_date(v) or datetime.max, str(v)))
        self.cut_points[ref] = cuts
        self.validity[ref] = {v: validity[v][0] for v in chosen}
        self.validity_change[ref] = {v: validity[v][1] for v in chosen}
        return chosen

    def _clock_representative(self, members: list[Value], validity: dict, cut_texts: set[str],
                              waits: list[Activity]) -> Value:
        """Spec, step 3: one value per class, "chosen in this order: (1) among the candidates whose validity ...
        reaches the setting "minimum validity of the data", or all of them when none does; (2) the one that makes
        the waits by attribute on the field end soonest ...; (3) not on a cut point; (4) the longest validity;
        (5) the earliest date; null never represents a class that has a non-null member." A value that is not
        perishable reaches any minimum and has the longest validity."""
        pool = [m for m in members if m is not None] or list(members)
        minimum = self.settings.min_validity_days
        reaching = [m for m in pool if validity[m][0] is None or validity[m][0] >= minimum]
        pool = reaching or pool

        def key(value: Value):
            moment = self.walker.read_date(value)
            days = validity[value][0]
            return (self._waits_end(waits, moment) if waits else self.entry_time, value in cut_texts,
                    -(days if days is not None else MAX_SLIP_DAYS + 1), moment or datetime.max, str(value))
        return min(pool, key=key)

    def _waits_end(self, waits: list[Activity], moment: Optional[datetime]) -> datetime:
        """When the waits by attribute on a field holding ``moment`` have all ended, at the earliest: for each, the
        later of its earliest arrival (the entry time plus its smallest fixed delay) and the end its date gives."""
        latest = self.entry_time
        for wait in waits:
            delays = self.delays.get(wait.key) or {0.0}
            end = self.entry_time + timedelta(days=min(delays))
            if moment is not None:
                try:
                    end = max(end, graph.wait_end(wait, moment))
                except criteria.UnsupportedOperator:
                    pass
            latest = max(latest, end)
        return latest

    def _point_text(self, point: ClockPoint) -> str:
        """The condition of a clock point, in plain words, for the report."""
        text = f"la condizione {formula_text(point.cond)}"
        if point.site.startswith(EXIT_PREFIX):
            return text + " dei criteri di uscita (exit criteria)"
        activity = self.journey.activities.get(point.site)
        name = activity.name if activity is not None and activity.name else point.site
        return text + (f" dello split '{name}' ({point.site})" if name != point.site else f" dello split '{point.site}'")

    # -- contact classes ----------------------------------------------------------------

    def contact_predicates(self) -> list[Formula]:
        """A pure contact-level outcome formula is one predicate; of a mixed formula, each
        contact-level condition is one predicate. Classes are the distinct truth vectors.
        The exit criteria give predicates in the same way (spec, step 3)."""
        return [formula for formula, _ in self._contact_predicate_points()]

    def _contact_predicate_points(self) -> list[tuple[Formula, tuple[tuple[float, tuple[str, ...]], ...]]]:
        """The predicates of ``contact_predicates``, each with the moments it is evaluated at: ``(delay,
        anchor waits)``. Spec, step 3: a condition can "compare a date with Today", so a predicate holding a
        relative date is evaluated at every fixed delay of its split, and at the end of every wait for an exit
        criterion, each moment one entry of the truth vector; the clock is the entry time plus the delay, or the
        end of a wait by attribute on a contact-level field before the site when that is later. Any other
        predicate does not depend on the clock and is evaluated once, at the entry time."""
        preds: list[Formula] = []
        moments: dict[str, list[tuple[float, tuple[str, ...]]]] = {}

        def add(f: Formula, site: str) -> None:
            key = formula_text(f)
            if key not in moments:
                moments[key] = []
                preds.append(f)
            for moment in self._predicate_moments(f, site):
                if moment not in moments[key]:
                    moments[key].append(moment)

        # A field rewritten by an update-contact step can change the truth of a formula along the
        # path, so such formulas are split into their conditions: classes then stay exact. Journey Data
        # never changes (the snapshot taken at entry); a write into a table with the row present / row
        # absent variable can change every field of that table (it inserts the row).
        written = {self.schema.canonical((w.table, w.field))
                   for a in self.journey.activities.values() for w in a.writes}
        inserting = {ref[0].lower() for ref in written if ref[0].lower() in self.row_fields}

        def may_change(c: Condition, ref: FieldRef) -> bool:
            return c.source != SOURCE_JOURNEY and (ref in written or ref[0].lower() in inserting)

        formulas: list[tuple[Formula, str]] = []
        for activity in self.journey.activities.values():
            if activity.kind != KIND_DECISION:
                continue
            for outcome in activity.outcomes:
                if outcome.formula is None or outcome.unsupported_reason:
                    continue
                formulas.append((outcome.formula, activity.key))
        formulas.extend((exit_.formula, EXIT_PREFIX + exit_.key) for exit_ in self.active_exits)
        for formula, site in formulas:
            conds = formula_conditions(formula)
            refs = [self.schema.canonical((c.table, c.field)) for c in conds]
            levels = {self.levels[self._var(r)] for r in refs}
            if levels == {LEVEL_CONTACT} and not any(may_change(c, r) for c, r in zip(conds, refs)):
                add(formula, site)
            elif LEVEL_CONTACT in levels:
                for c in conds:
                    if self.levels[self._cond_var(c)] == LEVEL_CONTACT:
                        add(c, site)
        return [(f, tuple(moments[formula_text(f)])) for f in preds]

    def _predicate_moments(self, formula: Formula, site: str) -> list[tuple[float, tuple[str, ...]]]:
        """The moments a predicate at ``site`` is evaluated at (see ``_contact_predicate_points``)."""
        if not any(c.relative is not None for c in formula_conditions(formula)):
            return [(0.0, ())]
        waits = [a for a in self.journey.activities.values()
                 if a.kind == KIND_WAIT_ATTRIBUTE and a.wait_field is not None
                 and self.levels.get(self._var(self.schema.canonical(a.wait_field))) == LEVEL_CONTACT]
        out: list[tuple[float, tuple[str, ...]]] = []
        for delay, before in self._site_moments(site):
            moment = (delay, tuple(w.key for w in waits if w.key in before))
            if moment not in out:
                out.append(moment)
        return out

    def _component_classes_by_prefix(self, fields: list[FieldRef], pred_indexes: list[int], preds,
                                     reads) -> Optional[list["_SubClass"]]:
        """The classes of a component too large to enumerate, found exactly field by field.

        The fields are assigned in product order. After each field, two partial assignments whose predicates are
        left with the same residual (each formula simplified with the values assigned so far: decided conditions
        replaced by their truth, AND and OR reduced) have the same completions and the same truths, so they merge:
        their member counts add up; the representative is the one with the smaller (preference, index vector) and
        the first member the smaller index vector, since the preference is a sum over the fields with an order
        invariant under translation. The classes, counts, representatives and first members are those of the full
        product. None when the residuals do not merge enough (MAX_PREFIX_STATES); the caller then truncates."""
        instances = [(k, m, delay, anchors) for k in pred_indexes for m, (delay, anchors) in enumerate(preds[k][1])]
        reads_of = {k: reads[k] for k in pred_indexes}
        memo: dict[tuple, object] = {}

        def residuals(values: dict[FieldRef, Value]) -> tuple:
            def lookup(ref: FieldRef):
                return values.get(self._var(ref), graph.UNKNOWN)
            out = []
            for k, m, delay, anchors in instances:
                seen = tuple(values.get(ref, _UNSET) for ref in reads_of[k])
                key = (k, m, seen)
                found = memo.get(key)
                if found is None:
                    if anchors and any(values.get(self._var(self.schema.canonical(self.journey.activities[a].wait_field)),
                                                  _UNSET) is _UNSET for a in anchors):
                        found = ("pending", seen)     # the clock is not known yet: keep the values read so far
                    elif all(v is not _UNSET for v in seen):
                        found = self._predicate_truth(preds[k][0], lookup, values, delay, anchors)
                    else:
                        found = self._residual(preds[k][0], lookup, self.entry_time + timedelta(days=delay))
                    memo[key] = found
                out.append(found)
            return tuple(out)

        states: dict[tuple, _SubClass] = {residuals({}): _SubClass(values={}, groups={}, count=1, first=(),
                                                                    best=self._preference({}))}
        for ref, domain in zip(fields, (self.domains[f] for f in fields)):
            merged: dict[tuple, tuple[_SubClass, tuple]] = {}
            for state in states.values():
                for i, value in enumerate(domain):
                    values = {**state.values, ref: value}
                    first = state.first + (i,)
                    key = residuals(values)
                    held = merged.get(key)
                    candidate = (self._preference(values), first)
                    if held is None:
                        merged[key] = (_SubClass(values=values, groups={}, count=state.count, first=first,
                                                 best=candidate[0]), first)
                        continue
                    sub, rep_index = held
                    sub.count += state.count
                    if first < sub.first:
                        sub.first = first
                    if candidate < (sub.best, rep_index):
                        sub.values, sub.best = values, candidate[0]
                        merged[key] = (sub, first)
            if len(merged) > MAX_PREFIX_STATES:
                return None
            states = {key: sub for key, (sub, _) in merged.items()}
        out: list[_SubClass] = []
        for key, sub in states.items():
            groups: dict[int, list[bool]] = {k: [] for k in pred_indexes}
            for (k, _, _, _), truth in zip(instances, key):
                groups[k].append(bool(truth))
            sub.groups = {k: tuple(v) for k, v in groups.items()}
            out.append(sub)
        out.sort(key=lambda sub: sub.first)
        return out

    def _residual(self, formula: Formula, lookup, now: datetime, snapshot=None):
        """A formula simplified with the values decided so far: True, False, or what is left of it (a condition,
        or an operator with its remaining items), in a form equal for equal remainders. ``snapshot`` answers
        Journey Data conditions when given (graph.evaluate)."""
        if isinstance(formula, Condition):
            try:
                result = graph.evaluate(formula, lookup, self.schema, self.settings.case_insensitive_text, snapshot,
                                        now=now)
            except criteria.UnsupportedOperator:
                return False
            return formula if isinstance(result, graph.Unknown) else bool(result)
        items = []
        for item in formula.items:
            result = self._residual(item, lookup, now, snapshot)
            if result is True or result is False:
                if (formula.operator == "AND") != result:
                    return result          # a False item decides an AND, a True item decides an OR
                continue
            if result not in items:
                items.append(result)
        if not items:
            return formula.operator == "AND"
        return items[0] if len(items) == 1 else (formula.operator, tuple(items))

    def _clocked_truths(self, index: int, formula: Formula, moments, lookup, values: dict[FieldRef, Value],
                        reads: list[list[FieldRef]], memo: dict[tuple, tuple[bool, ...]]) -> tuple[bool, ...]:
        """The truths of a predicate at each of its moments, memoised by the values of the fields it reads and of
        its anchor waits (a field of an absent row reads null, as ``_mask_absent_rows`` has already written in
        ``values``): the truth depends on nothing else."""
        key = (index,) + tuple(values.get(ref) for ref in reads[index])
        found = memo.get(key)
        if found is None:
            found = tuple(self._predicate_truth(formula, lookup, values, delay, anchors) for delay, anchors in moments)
            memo[key] = found
        return found

    def _predicate_truth(self, formula: Formula, lookup, values: dict[FieldRef, Value], delay: float,
                         anchors: tuple[str, ...]) -> bool:
        """A predicate's truth for one contact-level combination at one moment (``_predicate_moments``)."""
        now = self.entry_time + timedelta(days=delay)
        for key in anchors:
            wait = self.journey.activities[key]
            moment = self.walker.read_date(values.get(self._var(self.schema.canonical(wait.wait_field))))
            if moment is None:
                continue
            try:
                now = max(now, graph.wait_end(wait, moment))
            except criteria.UnsupportedOperator:
                continue
        try:
            return bool(graph.evaluate(formula, lookup, self.schema, self.settings.case_insensitive_text, now=now))
        except criteria.UnsupportedOperator:
            return False

    def build_classes(self) -> None:
        """Spec step 4: "a class groups the contact-level combinations that satisfy the same conditions".

        The combinations are not listed as one cartesian product of every contact-level field: a journey with
        many such fields has hundreds of millions of them. The fields are grouped into connected components by
        "read by the same predicate" (aliases resolved; the fields of a table with the row present / row absent
        variable go with its linked field, since an absent row empties them; a field no predicate reads is a
        component of its own). Each predicate lies in exactly one component, so each component is enumerated on
        its own (``_component_classes``) and the classes are the product of the components' classes: the values
        are the union of their representatives, the truth vector the concatenation in predicate order, the member
        count the product. This is the same partition as the full product: the preference is a sum over the
        components and its lexicographic order is translation-invariant, so the best member of a product class is
        the product of the best members; the first in product order of a product set is the product of the
        firsts, so representatives and the order of the classes (first appearance) are the same as before.
        """
        preds = self._contact_predicate_points()
        fields = self.contact_fields
        position = {ref: i for i, ref in enumerate(fields)}
        parent = list(range(len(fields)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            a, b = find(i), find(j)
            if a != b:
                parent[max(a, b)] = min(a, b)

        # A predicate evaluated at several moments depends only on the fields it reads and on the dates of its
        # anchor waits: its truths are kept per combination of those values.
        reads = [sorted({self._cond_var(c) for c in formula_conditions(p)}
                        | {self._var(self.schema.canonical(self.journey.activities[key].wait_field))
                           for _, anchors in moments for key in anchors}) for p, moments in preds]
        for refs in reads:
            indexes = [position[ref] for ref in refs if ref in position]
            for i in indexes[1:]:
                union(indexes[0], i)
        for table, linked in self.row_fields.items():
            if linked in position:
                for ref in fields:
                    if ref[0].lower() == table:
                        union(position[linked], position[ref])
        members: dict[int, list[int]] = {}
        for i in range(len(fields)):
            members.setdefault(find(i), []).append(i)
        components = list(members.values())             # in order of their first field
        owner: dict[int, int] = {}                       # predicate index -> component index (-1: reads no field)
        for k, refs in enumerate(reads):
            indexes = [position[ref] for ref in refs if ref in position]
            owner[k] = components.index(members[find(indexes[0])]) if indexes else -1
        if any(c == -1 for c in owner.values()):
            components.append([])                        # predicates that read no contact-level field
            for k in owner:
                if owner[k] == -1:
                    owner[k] = len(components) - 1
        memo: dict[tuple, tuple[bool, ...]] = {}
        per_component = [self._component_classes([fields[i] for i in comp],
                                                 [k for k in range(len(preds)) if owner[k] == c], preds, reads, memo)
                         for c, comp in enumerate(components)]

        total = math.prod(len(found) for found in per_component)
        combos = itertools.product(*per_component)
        if total > MAX_CONTACT_CLASSES:
            self.classes_incomplete = True
            self.warnings.append(
                f"{total} classi di contatti: ne sono state tenute solo {MAX_CONTACT_CLASSES}, quindi alcuni branch "
                "raggiungibili possono risultare non coperti")
            combos = itertools.islice(combos, MAX_CONTACT_CLASSES)
        entries = []
        for combo in combos:
            first = [0] * len(fields)
            for comp, sub in zip(components, combo):
                for j, i in enumerate(comp):
                    first[i] = sub.first[j]
            entries.append((first, combo))
        entries.sort(key=lambda entry: entry[0])
        classes: list[ContactClass] = []
        for number, (_, combo) in enumerate(entries, start=1):
            merged: dict[FieldRef, Value] = {}
            groups: dict[int, tuple[bool, ...]] = {}
            count = 1
            for sub in combo:
                merged.update(sub.values)
                groups.update(sub.groups)
                count *= sub.count
            truth = tuple(value for k in range(len(preds)) for value in groups.get(k, ()))
            classes.append(ContactClass(id=number, values={ref: merged[ref] for ref in fields},
                                        predicate_truth=truth, member_count=count))
        self.classes = classes

    def _component_classes(self, fields: list[FieldRef], pred_indexes: list[int], preds, reads,
                           memo: dict[tuple, tuple[bool, ...]]) -> list["_SubClass"]:
        """The classes of one component: the product of its fields' domains (at most MAX_CONTACT_COMBINATIONS,
        with a warning), grouped by the truths of its predicates. Each keeps its member count, the index vector of
        its first member in product order, and its representative: the best by ``_preference`` (spec: "prefers
        those whose path rests on no unverified assumption"), the first in product order on a tie."""
        domains = [self.domains[ref] for ref in fields]
        total = math.prod(len(d) for d in domains) if domains else 1
        if total > MAX_CONTACT_COMBINATIONS and not any(ref[0].lower() in self.row_fields for ref in fields):
            exact = self._component_classes_by_prefix(fields, pred_indexes, preds, reads)
            if exact is not None:
                return exact
        combos = itertools.product(*[range(len(d)) for d in domains])
        if total > MAX_CONTACT_COMBINATIONS:
            self.classes_incomplete = True
            self.warnings.append(
                f"{total} combinazioni di valori a livello di contatto: sono state esaminate solo le prime {MAX_CONTACT_COMBINATIONS}, "
                "quindi mancano alcune classi di contatti e alcuni branch raggiungibili possono risultare non coperti")
            combos = itertools.islice(combos, MAX_CONTACT_COMBINATIONS)
        found: dict[tuple, _SubClass] = {}
        for indexes in combos:
            values = self._mask_absent_rows({ref: domain[i] for ref, domain, i in zip(fields, domains, indexes)})

            def lookup(ref: FieldRef, _values=values):
                return _values.get(self._var(ref), graph.UNKNOWN)

            groups = {}
            for k in pred_indexes:
                formula, moments = preds[k]
                groups[k] = self._clocked_truths(k, formula, moments, lookup, values, reads, memo) if moments else ()
            key = tuple(groups[k] for k in pred_indexes)
            sub = found.get(key)
            preference = self._preference(values)
            if sub is None:
                found[key] = _SubClass(values=values, groups=groups, count=1, first=indexes, best=preference)
            else:
                sub.count += 1
                if preference < sub.best:
                    # Spec: among values that cover the same branches, prefer those whose path rests on no
                    # unverified assumption. Every member of a class takes the same paths.
                    sub.values = values
                    sub.best = preference
        return list(found.values())

    def _mask_absent_rows(self, values: dict[FieldRef, Value]) -> dict[FieldRef, Value]:
        """Spec, "Row present or absent": every field of an absent row reads null, so a contact without the
        row has no other value in that table."""
        if not self.row_fields:
            return values
        for table, linked in self.row_fields.items():
            if linked in values and values[linked] is None:
                for ref in values:
                    if ref != linked and ref[0].lower() == table:
                        values[ref] = None
        return values

    def _preference(self, values: dict[FieldRef, Value]) -> tuple[int, int, int, int]:
        """Spec, "Operators, types and values": "Among values that cover the same branches, the generator
        prefers those whose path rests on no unverified assumption: not null, row present." Lower is
        better: the null values and absent rows, then the absent rows alone, then (spec, step 3, "Validity
        of the data": values are chosen so that the margin is at least the minimum validity "wherever a class
        of values allows it") the values whose validity is below the setting, then the values of a range
        field that sit on a value named by a condition rather than strictly inside a range."""
        absent = {table for table, linked in self.row_fields.items() if linked in values and values[linked] is None}
        nulls = 0
        short = 0
        on_points = 0
        for ref, value in values.items():
            if absent and ref[0].lower() in absent:
                continue
            if value is None:
                nulls += 1
            elif value in self.named_points.get(ref, ()):
                on_points += 1
            if self.validity:
                days = self.validity.get(ref, {}).get(value)
                if days is not None and days < self.settings.min_validity_days:
                    short += 1
        return (nulls + len(absent), len(absent), short, on_points)

    # -- symbolic exploration -----------------------------------------------------------

    def _observe_decision(self, activity: Activity, values: dict[FieldRef, Value]) -> None:
        store = self.reached_values.setdefault(activity.key, {})
        for ref, v in values.items():
            store.setdefault(ref, set()).add(v)

    def explore(self) -> None:
        """One decision tree per contact class: branch on scenario-level values when a split
        needs one, on both outcomes of engagement and random splits, and stop at a leaf."""
        for cls in self.classes:
            state = self.walker.start()
            self.trees[cls.id] = self._explore_from(cls, state, {}, (), ())

    def _lookup(self, cls: ContactClass, assigned: dict[FieldRef, Value]):
        alias_of = self.alias_of

        def lookup(ref: FieldRef):
            if ref in cls.values:
                return cls.values[ref]
            if ref in assigned:
                return assigned[ref]
            source = alias_of.get(ref)
            if source is not None:
                # Spec, step 3: an entry copy reads the value of its source column (one variable).
                if source in cls.values:
                    return cls.values[source]
                if source in assigned:
                    return assigned[source]
            if ref in self.domains:
                return graph.UNKNOWN
            # A field no split reads (only reachable through a tracked write): unknown.
            return graph.UNKNOWN
        return lookup

    def _explore_from(self, cls: ContactClass, state: graph.WalkState, assigned: dict[FieldRef, Value],
                      manual: tuple[ManualAction, ...], randoms: tuple[tuple[str, str], ...]) -> Node:
        if len(self.leaves) >= MAX_PATHS:
            if not self.exploration_truncated:
                self.exploration_truncated = True
                self.warnings.append(
                    f"il journey ha più di {MAX_PATHS} percorsi distinti: l'esplorazione si è fermata a quel limite, quindi "
                    "alcuni branch raggiungibili possono risultare non coperti")
            state.stop = graph.STOP_ERROR
            state.stop_detail = "limite di esplorazione raggiunto"
            return LeafNode(self._leaf(cls, state, assigned, manual, randoms))
        state = self.walker.run(state, self._lookup(cls, assigned), choose=None)
        self.reached_activities.update(s.activity_key for s in state.steps)
        if state.current is not None:
            self.reached_activities.add(state.current)

        if state.stop == graph.STOP_UNDETERMINED:
            ref = self._var(state.pending_field) if state.pending_field is not None else None
            if ref is None or ref not in self.domains:
                self.warnings.append(f"lo split '{state.current}' legge un campo di cui il generatore non sa elencare i valori: "
                                     "il percorso si ferma lì")
                state.stop = graph.STOP_ERROR
                state.stop_detail = f"lo split '{state.current}' legge un campo di cui il generatore non sa elencare i valori"
                return LeafNode(self._leaf(cls, state, assigned, manual, randoms))
            children: dict[Value, Node] = {}
            for value in self.domains[ref]:
                child_state = state.clone()
                child_assigned = dict(assigned)
                child_assigned[ref] = value
                children[value] = self._explore_from(cls, child_state, child_assigned, manual, randoms)
            return BranchNode(ref, children)

        if state.stop == graph.STOP_CHOICE:
            activity = self.journey.activities[state.current]
            children: list[Node] = []
            if activity.kind == KIND_ENGAGEMENT:
                verdict = graph.engagement_verdict(activity, state, self.package.emails, self.journey)
                for outcome in activity.outcomes:
                    if outcome.when is True and not verdict.possible:
                        self.engagement_blocked.setdefault((activity.key, outcome.key), verdict.reason)
                        continue
                    child_state = state.clone()
                    self.walker.take(child_state, activity, outcome)
                    child_manual = manual
                    if outcome.when is True:
                        email = self.journey.activities.get(activity.engagement_ref or "")
                        child_manual = manual + (ManualAction(
                            activity_key=activity.key, kind=activity.engagement_kind or "",
                            email_activity_key=activity.engagement_ref or "",
                            email_name=(email.email_name if email is not None and email.email_name else activity.engagement_ref or "")),)
                    children.append(self._explore_from(cls, child_state, assigned, child_manual, randoms))
            else:  # random split
                for outcome in activity.outcomes:
                    child_state = state.clone()
                    self.walker.take(child_state, activity, outcome)
                    children.append(self._explore_from(cls, child_state, assigned, manual, randoms + ((activity.key, outcome.key),)))
            return ChoiceNode(children)

        return LeafNode(self._leaf(cls, state, assigned, manual, randoms))

    def _leaf(self, cls: ContactClass, state: graph.WalkState, assigned: dict[FieldRef, Value],
              manual: tuple[ManualAction, ...], randoms: tuple[tuple[str, str], ...]) -> Leaf:
        if state.stop in (graph.STOP_LOOP, graph.STOP_DANGLING, graph.STOP_ERROR):
            self.warnings.append(f"percorso interrotto: {state.stop_detail}")
        stop = state.stop or graph.STOP_EXIT
        # Spec, "Honest outcomes": past an element the walk cannot evaluate, the path is unknown from there on.
        # Spec, step 3: a contact the exit criteria take out at the end of a wait has a known end there.
        known = stop in (graph.STOP_EXIT, graph.STOP_EXIT_CRITERIA)
        unknown_from = None if known else (state.current or state.end_activity_key)
        stopped = self.journey.activities.get(state.current or "")
        if stop == graph.STOP_UNSUPPORTED and stopped is not None and stopped.kind == KIND_UPDATE:
            self.stopped_at.setdefault(stopped.key, state.stop_detail)
        exit_wait = state.exit_by_criteria if stop == graph.STOP_EXIT_CRITERIA else None
        leaf = Leaf(id=len(self.leaves), class_id=cls.id, assigned=dict(assigned), manual=manual, randoms=randoms,
                    steps=list(state.steps), emails_sent=list(state.emails_sent), writes=list(state.writes),
                    waits=list(state.waits), stop=stop, stop_detail=state.stop_detail, unknown_from=unknown_from,
                    timeline=state.timeline, sms_sent=state.sms_sent, exit_by_criteria=exit_wait)
        mask = 0
        for ref in state.branches():
            idx = self.branch_index.get(ref)
            if idx is not None:
                mask |= 1 << idx
        if self.active_exits:
            for wait in state.waits:
                if wait != exit_wait:
                    self.passed_at[wait] = self.passed_at.get(wait, 0) + 1
        if exit_wait is not None:
            # Spec, step 3: "Every exit criterion is one extra coverage target": the bit of each criterion met.
            self.exited_at[exit_wait] = self.exited_at.get(exit_wait, 0) + 1
            for index in self._exits_met(cls, state, assigned):
                exit_ = self.exits[index]
                self.exit_met_at.setdefault(exit_wait, set()).add(exit_.key)
                idx = self.branch_index.get((EXIT_PREFIX + exit_.key, EXIT_OUTCOME))
                if idx is not None:
                    mask |= 1 << idx
            self.exit_states.append((cls, state, dict(assigned)))
        leaf.mask = mask
        self.leaves.append(leaf)
        return leaf

    def _exits_met(self, cls: ContactClass, state: graph.WalkState, assigned: dict[FieldRef, Value]) -> list[int]:
        """The indexes (in ``exits``) of the exit criteria a path that left by the exit criteria meets at the end of
        its wait: the only one when there is one, else each re-evaluated with the clock at the wait's end and the
        same data sources as the walker (Contact Data live, Journey Data from the snapshot)."""
        if len(self.exits) == 1:
            return [0]
        lookup = self._lookup(cls, assigned)

        def value(ref: FieldRef):
            return self.walker.value_of(state, ref, lookup)

        def snapshot(ref: FieldRef):
            return self.walker.snapshot_of(ref, lookup)

        met = []
        for index, exit_ in enumerate(self.exits):
            try:
                result = graph.evaluate(exit_.formula, value, self.schema, self.settings.case_insensitive_text,
                                        snapshot, now=state.clock)
            except criteria.UnsupportedOperator:
                continue
            if result is True:
                met.append(index)
        return met

    # -- step 4 continued: every scenario-level combination ------------------------------

    def enumerate_scenarios(self) -> dict[tuple[int, ...], dict[FieldRef, Value]]:
        """Signature (the leaves a full scenario produces across all classes) -> the preferred scenario with
        it (``_preference``; the first one enumerated on a tie)."""
        domains = [self.domains[f] for f in self.scenario_fields]
        total = math.prod(len(d) for d in domains) if domains else 1
        signatures: dict[tuple[int, ...], dict[FieldRef, Value]] = {}
        if total > MAX_SCENARIO_COMBINATIONS:
            self.warnings.append(
                f"{total} combinazioni di valori a livello di scenario superano il limite del generatore "
                f"({MAX_SCENARIO_COMBINATIONS}): gli scenari sono stati costruiti solo dai valori da cui dipendono i percorsi esplorati")
            for leaf in self.leaves:
                assignment = {f: d[0] for f, d in zip(self.scenario_fields, domains)}
                assignment.update(leaf.assigned)
                sig = self._signature(assignment)
                signatures.setdefault(sig, assignment)
            return signatures
        preferences: dict[tuple[int, ...], tuple[int, int, int, int]] = {}
        for combo in itertools.product(*domains):
            assignment = dict(zip(self.scenario_fields, combo))
            sig = self._signature(assignment)
            preference = self._preference(assignment)
            if sig not in signatures or preference < preferences[sig]:
                signatures[sig] = assignment
                preferences[sig] = preference
        return signatures

    def _signature(self, assignment: dict[FieldRef, Value]) -> tuple[int, ...]:
        ids: list[int] = []
        for cls in self.classes:
            ids.extend(leaf.id for leaf in lookup_leaves(self.trees[cls.id], assignment))
        return tuple(sorted(ids))

    # -- step 5: the cover --------------------------------------------------------------

    def choose_cover(self, signatures: dict[tuple[int, ...], dict[FieldRef, Value]]):
        """Greedy set cover over scenarios, then over the cases inside the chosen scenarios.

        Only deterministic leaves (no random split on the path) count here: the cover prefers
        paths that avoid random splits. Ties are broken by a fixed random seed."""
        rng = random.Random(self.settings.seed)
        masks = self._effective_masks([leaf for leaf in self.leaves if leaf.deterministic])
        universe = 0
        for leaf in self.leaves:
            if leaf.deterministic:
                universe |= leaf.mask
        candidates = []
        for sig, assignment in signatures.items():
            mask = 0
            for leaf_id in sig:
                leaf = self.leaves[leaf_id]
                if leaf.deterministic:
                    mask |= masks[leaf.id]
            if mask:
                candidates.append((sig, assignment, mask))
        rng.shuffle(candidates)
        candidates = self._preferred_per_mask(candidates)
        covered = 0
        chosen: list[tuple[tuple[int, ...], dict[FieldRef, Value]]] = []
        while covered != universe:
            best = None
            best_gain = 0
            for cand in candidates:
                gain = bin(cand[2] & ~covered).count("1")
                if gain > best_gain:
                    best, best_gain = cand, gain
            if best is None:
                break
            chosen.append((best[0], best[1]))
            covered |= best[2]
            candidates.remove(best)

        # Cases: the fewest leaves of the chosen scenarios that still cover everything.
        case_candidates: list[tuple[int, Leaf]] = []
        for i, (sig, _) in enumerate(chosen):
            for leaf_id in sig:
                leaf = self.leaves[leaf_id]
                if leaf.deterministic and masks[leaf.id]:
                    case_candidates.append((i, leaf))
        covered = 0
        selected: dict[int, list[Leaf]] = {}
        # Cases without a manual action first, so a branch reachable without opening an email is
        # reported as plainly covered; cases with a manual action only for what remains.
        for with_manual in (False, True):
            pool = [c for c in case_candidates if bool(c[1].manual) == with_manual]
            while covered != universe:
                best = None
                best_key = None
                for cand in pool:
                    gain = bin(masks[cand[1].id] & ~covered).count("1")
                    if gain == 0:
                        continue
                    key = (-gain, cand[0], cand[1].class_id, cand[1].id)
                    if best_key is None or key < best_key:
                        best, best_key = cand, key
                if best is None:
                    break
                selected.setdefault(best[0], []).append(best[1])
                covered |= best[1].mask
                pool.remove(best)
        if not selected:
            # A journey without any split still gets one case, so the CSVs hold data.
            for sig, assignment in signatures.items():
                leaf = next((self.leaves[i] for i in sig if self.leaves[i].deterministic), None)
                if leaf is not None:
                    chosen.append((sig, assignment))
                    selected[len(chosen) - 1] = [leaf]
                    break
        scenarios = [(chosen[i][1], leaves) for i, leaves in sorted(selected.items())]
        return scenarios, universe

    def _effective_masks(self, leaves: list[Leaf]) -> dict[int, int]:
        """Leaf id -> the targets the cover may choose it for. Spec, step 3, "Validity of the data": values are
        chosen so that the margin is at least the setting "minimum validity of the data" "wherever a class of values
        allows it". A leaf whose contact or scenario values include one below the minimum validity counts only for
        the targets that no leaf without such a value reaches (none: it is left out); the others count for all
        theirs."""
        if not self.validity:
            return {leaf.id: leaf.mask for leaf in leaves}
        short = {leaf.id: self._leaf_is_short(leaf) for leaf in leaves}
        robust = 0
        for leaf in leaves:
            if not short[leaf.id]:
                robust |= leaf.mask
        return {leaf.id: leaf.mask & ~robust if short[leaf.id] else leaf.mask for leaf in leaves}

    def _leaf_is_short(self, leaf: Leaf) -> bool:
        """The leaf's class values or scenario values include a value valid for fewer days than the minimum."""
        minimum = self.settings.min_validity_days
        for values in (self.classes[leaf.class_id - 1].values, leaf.assigned):
            for ref, value in values.items():
                days = self.validity.get(ref, {}).get(value)
                if days is not None and days < minimum:
                    return True
        return False

    def _preferred_per_mask(self, candidates: list) -> list:
        """Spec: "Among values that cover the same branches, the generator prefers those whose path rests on
        no unverified assumption". Scenarios crossing the same branches have the same mask; the greedy cover
        can only ever choose the first of them in the shuffled order (the others never gain more), so that
        place goes to the preferred one, and the others are dropped. Ties keep the shuffled order.

        The full preference applies (not null, then values inside ranges; scenario-level fields have no row
        present / row absent variable). The team accepted on 13 October 2026 the one pilot difference this
        makes: S18 gets CHANNEL2 = OTHER instead of empty."""
        best: dict[int, tuple] = {}
        for cand in candidates:
            held = best.get(cand[2])
            if held is None or self._preference(cand[1]) < self._preference(held[1]):
                best[cand[2]] = cand
        out = []
        for cand in candidates:
            chosen = best.pop(cand[2], None)
            if chosen is not None:
                out.append(chosen)
        return out

    # -- random splits: statistical cases -----------------------------------------------

    def choose_statistical(self, signatures: dict[tuple[int, ...], dict[FieldRef, Value]], det_universe: int):
        """Branches crossed only on paths through a random split get a statistical case.

        Random leaves are grouped by scenario, contact class and manual actions: a group is one
        statistical case (its contacts share the values and the actions asked of the tester).
        Groups without a manual action are preferred."""
        random_universe = 0
        for leaf in self.leaves:
            if not leaf.deterministic:
                random_universe |= leaf.mask
        random_universe &= ~det_universe
        if not random_universe:
            return []
        masks = self._effective_masks([leaf for leaf in self.leaves if not leaf.deterministic])
        groups: list[tuple[dict[FieldRef, Value], int, tuple[ManualAction, ...], list[Leaf], int]] = []
        for sig, assignment in signatures.items():
            by_group: dict[tuple[int, tuple[ManualAction, ...]], list[Leaf]] = {}
            for leaf_id in sig:
                leaf = self.leaves[leaf_id]
                if not leaf.deterministic:
                    by_group.setdefault((leaf.class_id, leaf.manual), []).append(leaf)
            for (class_id, manual), leaves in by_group.items():
                mask = 0
                for leaf in leaves:
                    mask |= masks[leaf.id]
                groups.append((assignment, class_id, manual, leaves, mask & random_universe))
        rng = random.Random(self.settings.seed + 1)
        rng.shuffle(groups)
        covered = 0
        chosen = []
        while covered != random_universe:
            best = None
            best_key = None
            for g in groups:
                gain = bin(g[4] & ~covered).count("1")
                if gain == 0:
                    continue
                key = (-gain, len(g[2]), len(g[3]), g[1])
                if best_key is None or key < best_key:
                    best, best_key = g, key
            if best is None:
                break
            chosen.append(best)
            covered |= best[4]
            groups.remove(best)
        return chosen

    def _random_info(self, leaves: list[Leaf]) -> tuple[list[Step], RandomInfo, list[TimelinePoint]]:
        """Cut the leaves at their first random split: the common prefix, one branch per leaf, and the timeline
        of the prefix (spec, step 3).

        A leaf that crosses several random splits is one branch whose percentage is the product
        of the percentages it took; its steps show every random split crossed."""
        first_key = leaves[0].randoms[0][0]
        activity = self.journey.activities[first_key]
        prefix: list[Step] = []
        for step in leaves[0].steps:
            if step.activity_key == first_key:
                break
            prefix.append(step)
        prefix.append(Step(first_key, None))
        cut = len(prefix) - 1
        # Spec, step 3: the timeline of the common part, the random split at the clock it is reached with.
        timeline = list(leaves[0].timeline[:cut])
        if len(leaves[0].timeline) > cut:
            timeline.append(TimelinePoint(first_key, None, leaves[0].timeline[cut].at))
        order = {o.key: i for i, o in enumerate(activity.outcomes)}

        def sort_key(leaf: Leaf):
            return [order.get(leaf.randoms[0][1], 99)] + [k for pair in leaf.randoms for k in pair]

        branches: list[RandomBranch] = []
        for leaf in sorted(leaves, key=sort_key):
            share = 1.0
            for key, outcome_key in leaf.randoms:
                split = self.journey.activities.get(key)
                outcome = split.outcome(outcome_key) if split is not None else None
                share *= (outcome.percentage or 0) / 100.0 if outcome is not None else 0
            idx = next(i for i, st in enumerate(leaf.steps) if st.activity_key == first_key)
            branches.append(RandomBranch(
                outcome_key=leaf.randoms[0][1], percentage=int(round(share * 100)),
                steps=tuple(leaf.steps[idx + 1:]), emails_sent=tuple(leaf.emails_sent),
                writes=tuple(leaf.writes), end_activity_key=leaf.end_activity_key,
                unknown_from=leaf.unknown_from, unknown_reason=leaf.stop_detail if leaf.unknown_from else "",
                timeline=tuple(leaf.timeline[idx + 1:]), exit_by_criteria=leaf.exit_by_criteria))
        n = self.settings.contacts_per_random_case
        smallest = min((b.percentage for b in branches if b.percentage), default=0) / 100.0
        low, high = expected_range(n, smallest)
        return prefix, RandomInfo(activity_key=first_key, branches=branches, expected_low=low,
                                  expected_high=high), timeline

    # -- assembling scenarios, cases and statuses --------------------------------------

    def run(self) -> CoverageResult:
        self.branch_list = graph.split_branches(self.journey)
        self.branch_index = {(a.key, o.key): i for i, (a, o) in enumerate(self.branch_list)}
        # Spec, step 3: one extra coverage target per evaluable exit criterion, after the split branches.
        for i, exit_ in enumerate(self.exits):
            self.branch_index[(EXIT_PREFIX + exit_.key, EXIT_OUTCOME)] = len(self.branch_list) + i
        self.warnings.extend(graph.structural_problems(self.journey))
        self.warnings.extend(self.delay_warnings)
        self.collect_fields()
        self.build_domains()
        self.build_classes()
        self.explore()
        self.warnings.extend(self._dead_wait_warnings())
        self.warnings.extend(self._same_name_different_conditions())
        self.warnings.extend(self._engagement_refers_to_an_earlier_email())
        signatures = self.enumerate_scenarios()
        det_scenarios, det_universe = self.choose_cover(signatures)
        stat_groups = self.choose_statistical(signatures, det_universe)

        # Spec step 6: paths sharing the same scenario-level values form one scenario, so a
        # statistical group joins the scenario with its values when there is one.
        def key_of(assignment: dict[FieldRef, Value]):
            return tuple(sorted(assignment.items(), key=lambda kv: (kv[0], str(kv[1]))))

        pending: dict[tuple, dict] = {}
        for assignment, leaves in det_scenarios:
            entry = pending.setdefault(key_of(assignment), {"values": assignment, "det": [], "stat": []})
            entry["det"].extend(leaves)
        for assignment, class_id, manual, leaves, _mask in stat_groups:
            entry = pending.setdefault(key_of(assignment), {"values": assignment, "det": [], "stat": []})
            entry["stat"].append((class_id, manual, leaves))

        def has_manual(entry: dict) -> bool:
            return any(l.manual for l in entry["det"]) or any(m for _, m, _ in entry["stat"])

        # Spec: scenarios with a manual action come first in the run order; statistical-only last.
        ordered = sorted(pending.values(), key=lambda e: (not e["det"], not has_manual(e)))
        scenarios: list[Scenario] = []
        for order, entry in enumerate(ordered, start=1):
            sid = f"S{order:02d}"
            cases: list[Case] = []
            for leaf in sorted(entry["det"], key=lambda l: (l.class_id, len(l.manual), l.id)):
                cls = self.classes[leaf.class_id - 1]
                case = Case(id=f"{sid}-C{len(cases) + 1:02d}", scenario_id=sid, class_id=cls.id,
                            contact_values=dict(cls.values), steps=list(leaf.steps), emails_sent=list(leaf.emails_sent),
                            writes=list(leaf.writes), waits=list(leaf.waits), manual_actions=list(leaf.manual),
                            end_activity_key=leaf.end_activity_key, contact_count=self.settings.contacts_per_case,
                            timeline=list(leaf.timeline), exit_by_criteria=leaf.exit_by_criteria,
                            sms_sent=list(leaf.sms_sent))
                self.case_exits[case.id] = (self._exit_refs(leaf), [])
                case.summary = self._summary(case.steps, case.exit_by_criteria)
                if case.manual_actions:
                    case.inbox_blocked_reason = self._inbox_block_reason(cls.values, entry["values"])
                if leaf.unknown_from is not None:
                    case.unknown_from, case.unknown_reason = leaf.unknown_from, leaf.stop_detail
                self._timing(case)
                cases.append(case)
            for class_id, manual, leaves in sorted(entry["stat"], key=lambda g: (len(g[1]), g[0])):
                prefix, info, prefix_timeline = self._random_info(leaves)
                cls = self.classes[class_id - 1]
                prefix_keys = [st.activity_key for st in prefix]
                case = Case(id=f"{sid}-C{len(cases) + 1:02d}", scenario_id=sid, class_id=cls.id,
                            contact_values=dict(cls.values), steps=prefix,
                            emails_sent=[k for k in prefix_keys if self.journey.activities[k].kind == KIND_EMAIL],
                            writes=[], waits=[k for k in prefix_keys if self.journey.activities[k].kind in graph.WAIT_KINDS],
                            manual_actions=list(manual), end_activity_key=info.activity_key,
                            contact_count=self.settings.contacts_per_random_case, random=info,
                            timeline=prefix_timeline,
                            sms_sent=[k for k in prefix_keys if self.journey.activities[k].kind == KIND_SMS])
                self.case_exits[case.id] = ([], [ref for leaf in leaves for ref in self._exit_refs(leaf)])
                prefix_summary = self._summary(case.steps)
                case.summary = (prefix_summary + " > random split") if prefix_summary else "random split"
                if case.manual_actions:
                    case.inbox_blocked_reason = self._inbox_block_reason(cls.values, entry["values"])
                self._timing(case)
                cases.append(case)
            for case in cases:
                case.validity_days = self._case_validity(case.contact_values, entry["values"])
            scenarios.append(Scenario(id=sid, key="", values=dict(entry["values"]), cases=cases, run_order=order,
                                      has_manual_action=any(c.manual_actions for c in cases),
                                      is_statistical=not entry["det"]))

        buckets = self._case_buckets(scenarios)
        branches = self._branch_statuses(scenarios, buckets)
        exit_targets = self._exit_statuses(buckets)
        perishable = self._perishable(scenarios)
        self.warnings.extend(self._bound_notes(scenarios))
        validities = [c.validity_days for s in scenarios for c in s.cases if c.validity_days is not None]
        stats = {
            "scenarios": len(scenarios),
            "cases": sum(len(s.cases) for s in scenarios),
            "contacts": sum(c.contact_count for s in scenarios for c in s.cases),
            "branches": len(branches),
            "covered": sum(1 for b in branches if b.status == STATUS_COVERED),
            "covered_manual": sum(1 for b in branches if b.status == STATUS_MANUAL),
            "not_covered": sum(1 for b in branches if b.status == STATUS_NOT_COVERED),
            "contact_classes": len(self.classes),
            "classes_incomplete": int(self.classes_incomplete),
            "exploration_truncated": int(self.exploration_truncated),
            "paths_explored": len(self.leaves),
            "scenario_signatures": len(signatures),
            "exit_targets": len(exit_targets),
            "exit_targets_covered": sum(1 for b in exit_targets if b.status != STATUS_NOT_COVERED),
        }
        return CoverageResult(
            scenarios=scenarios, branches=branches, classes=self.classes,
            scenario_fields=list(self.scenario_fields), contact_fields=list(self.contact_fields),
            domains=dict(self.domains), other_values=dict(self.other_values),
            unsupported=list(self.package.unsupported_activities) + [
                f"{a.key}/{o.key}" for a in self.journey.activities.values() for o in a.outcomes if o.unsupported_reason]
            + [key for key in self.stopped_at if key.partition("/")[0] in self.journey.activities],
            warnings=list(dict.fromkeys(self.warnings)), stats=stats,
            row_fields={linked[0]: linked for linked in self.row_fields.values()},
            unsupported_reasons=dict(self.stopped_at),
            exit_targets=exit_targets, validity_days=min(validities) if validities else None,
            perishable=perishable)

    # -- validity of the data (spec, step 3) ----------------------------------------------

    def _case_validity(self, contact_values: dict[FieldRef, Value],
                       scenario_values: dict[FieldRef, Value]) -> Optional[int]:
        """Spec, step 3, "Validity of the data": the earliest validity of the values of a case's rows (its
        contact values and its scenario values) that have one; None when none is perishable ("A case whose values
        are all not perishable has no validity ... and sets no deadline")."""
        found = [days for values in (contact_values, scenario_values) for ref, value in values.items()
                 for days in [self.validity.get(ref, {}).get(value)] if days is not None]
        return min(found) if found else None

    def _perishable(self, scenarios: list[Scenario]) -> list[str]:
        """One sentence per perishable value that the cases use, in field order, then in domain order: how long it
        stays valid and which condition reads it differently after that. A value below the minimum validity of
        the data is reported too (spec: "a value with a smaller margin is reported")."""
        used: set[tuple[FieldRef, Value]] = set()
        for scenario in scenarios:
            for case in scenario.cases:
                used.update(case.contact_values.items())
            used.update(scenario.values.items())
        out: list[str] = []
        for ref in self.fields:
            for value in self.domains.get(ref, []):
                days = self.validity.get(ref, {}).get(value)
                if days is None or (ref, value) not in used:
                    continue
                change = self.validity_change.get(ref, {}).get(value)
                then = f", poi {self._point_text(change)} lo legge diversamente" if change is not None else ""
                out.append(f"{ref[0]}.{ref[1]} = {value}: resta valido {days} {'giorno' if days == 1 else 'giorni'} "
                           f"dall'ora di ingresso{then}")
                if days < self.settings.min_validity_days:
                    self.warnings.append(
                        f"{ref[0]}.{ref[1]} = {value} resta valido solo {days} {'giorno' if days == 1 else 'giorni'} "
                        f"dall'ora di ingresso, meno della validità minima dei dati impostata "
                        f"({self.settings.min_validity_days} {'giorno' if self.settings.min_validity_days == 1 else 'giorni'}): "
                        "nessun valore che porta il contatto sugli stessi branch ha un margine più ampio")
        return out

    def _bound_notes(self, scenarios: list[Scenario]) -> list[str]:
        """Spec, step 3: "Some classes compare on a bound at the split whatever their value ... The branch is still
        covered, by a value of that class chosen with the rules above, and the testbook and the report say that the
        branch is decided on the bound of a day-level comparison." One note per branch and condition: a day-level relative
        condition evaluated on the path of a case (the outcomes tried up to the one taken) whose date equals the
        reference date at the clock the timeline gives."""
        found: dict[tuple[str, str, str, FieldRef, Value], list[str]] = {}
        for scenario in scenarios:
            for case in scenario.cases:
                values = {**scenario.values, **case.contact_values}
                points = list(case.timeline)
                if case.random is not None:
                    for branch in case.random.branches:
                        points.extend(branch.timeline)
                for point in points:
                    activity = self.journey.activities.get(point.activity_key)
                    if activity is None or activity.kind != KIND_DECISION or point.outcome_key is None:
                        continue
                    remainder = [o for o in activity.outcomes if o.is_remainder or o.formula is None]
                    for outcome in [o for o in activity.outcomes if o not in remainder] + remainder:
                        for cond in formula_conditions(outcome.formula):
                            if cond.relative is None:
                                continue
                            ref = self._cond_var(cond)
                            moment = self.walker.read_date(values.get(ref))
                            try:
                                if moment is None or not criteria.is_day_level(cond.relative):
                                    continue
                                reference = criteria.shift_date(point.at, cond.relative)
                            except criteria.UnsupportedOperator:
                                continue
                            if moment.date() == reference.date():
                                key = (activity.key, point.outcome_key, formula_text(cond), ref, values.get(ref))
                                ids = found.setdefault(key, [])
                                if case.id not in ids:
                                    ids.append(case.id)
                        if outcome.key == point.outcome_key:
                            break
        notes: list[str] = []
        for (split, outcome_key, text, ref, value), ids in found.items():
            activity = self.journey.activities[split]
            outcome = activity.outcome(outcome_key)
            label = label_of(outcome) if outcome is not None else outcome_key
            name = activity.name or split
            cases = ids[0] if len(ids) == 1 else ", ".join(ids)
            notes.append(f"il branch '{label}' dello split '{name}' ({split}) è percorso {'dal caso' if len(ids) == 1 else 'dai casi'} "
                         f"{cases}, in cui {ref[0]}.{ref[1]} = {value} cade esattamente sull'estremo della condizione "
                         f"{text} quando il contatto arriva allo split: il branch è deciso sull'estremo di un "
                         "confronto sul giorno (si confronta la parte data del campo con la data dell'orologio)")
        return notes

    # -- honest outcomes: windows of the manual actions and minimum durations -------------

    def _timing(self, case: Case) -> None:
        """Spec, "Honest outcomes": "The deadline of a manual open or click is computed from the waits
        between the email and the engagement split ... The testbook gives each case's minimum duration."

        Fills ``case.action_windows`` (one per manual action: the waits between its email and its split,
        their fixed duration, the last wait by attribute among them, whose date datagen.py turns into
        the deadline, and the waits before the email, which delay its arrival and so the start of the
        window) and ``case.min_duration_minutes`` (the fixed waits of the longest path of the case)."""
        activities = self.journey.activities
        paths = graph.case_paths(case)
        durations = [self._fixed_minutes(keys, need_dates=False) for keys in paths]
        case.min_duration_minutes = None if any(d is None for d in durations) else max(durations, default=0)
        windows: list[ActionWindow] = []
        for action in case.manual_actions:
            keys = next((p for p in paths if action.activity_key in p), [])
            end = keys.index(action.activity_key) if keys else 0
            start = keys.index(action.email_activity_key) if action.email_activity_key in keys[:end] else -1
            waits = [k for k in keys[start + 1:end] if activities[k].kind in graph.WAIT_KINDS]
            attribute = [k for k in waits if activities[k].kind == KIND_WAIT_ATTRIBUTE]
            after = waits[waits.index(attribute[-1]) + 1:] if attribute else []
            before = [k for k in keys[:max(start, 0)] if activities[k].kind in graph.WAIT_KINDS]
            windows.append(ActionWindow(
                activity_key=action.activity_key, email_activity_key=action.email_activity_key, wait_keys=waits,
                minutes=self._fixed_minutes(waits), attribute_wait_key=attribute[-1] if attribute else None,
                minutes_after_attribute_wait=self._fixed_minutes(after), wait_keys_before=before))
        case.action_windows = windows

    def _fixed_minutes(self, keys: list[str], need_dates: bool = True) -> Optional[int]:
        """Fixed duration of the waits among ``keys``, in minutes (graph.fixed_wait_minutes); None when one
        is unknown, or, with ``need_dates``, when a wait by attribute has no date field (a window that ends
        at an unknown date is unknown)."""
        total = 0
        for key in keys:
            activity = self.journey.activities.get(key)
            if activity is None or activity.kind not in graph.WAIT_KINDS:
                continue
            if need_dates and activity.kind == KIND_WAIT_ATTRIBUTE and activity.wait_field is None:
                return None
            minutes = graph.fixed_wait_minutes(activity)
            if minutes is None:
                return None
            total += minutes
        return total

    def _inbox_block_reason(self, contact_values: dict[FieldRef, Value],
                            scenario_values: dict[FieldRef, Value]) -> Optional[str]:
        """Data rule 2 wins over rule 4: when a split fixes the address field the journey sends to,
        the contacts of that case cannot use the tester's inbox, so their open or click cannot be done."""
        entry = self.table_model.entry_table
        default_email = (self.journey.default_email_field or "").lower()
        absent = {table for table, linked in self.row_fields.items()
                  if linked in contact_values and contact_values[linked] is None}
        for values in (contact_values, scenario_values):
            for ref, value in values.items():
                if ref[0].lower() in absent:
                    continue   # the contact has no row there: no address of that table is written
                is_address = (self.schema.field_type(ref) == "EmailAddress"
                              or (ref[0].lower() == entry.lower() and ref[1].lower() == default_email))
                if not is_address:
                    continue
                if value is None:
                    return f"il percorso richiede che {ref[0]}.{ref[1]} sia vuoto, quindi nessuna email può essere ricevuta"
                if value != self.other_values.get(ref):
                    return (f"il percorso richiede {ref[0]}.{ref[1]} = {value!r}, un indirizzo che il tester non possiede, "
                            "quindi l'email non può essere aperta né cliccata")
        return None

    def _summary(self, steps: list[Step], exit_wait: Optional[str] = None) -> str:
        parts: list[str] = []
        for step in steps:
            activity = self.journey.activities.get(step.activity_key)
            if activity is None or step.outcome_key is None:
                continue
            if activity.kind in (KIND_DECISION, KIND_ENGAGEMENT):
                outcome = activity.outcome(step.outcome_key)
                label = outcome.label if outcome is not None and outcome.label else "condizione vera"
                parts.append(f"{activity.name or activity.key}: {label}")
        if exit_wait is not None:
            # Spec, step 3: "leaves the journey at the end of <wait>, because the exit criteria are met".
            wait = self.journey.activities.get(exit_wait)
            parts.append(f"esce alla fine di '{(wait.name if wait is not None and wait.name else exit_wait)}' "
                         "per i criteri di uscita")
        text = " > ".join(parts)
        return text if len(text) <= 160 else text[:157] + "..."

    def _engagement_refers_to_an_earlier_email(self) -> list[str]:
        """Spec check carried over: an engagement split that references an email never sent on the
        paths reaching it. Read structurally: when another email is the last one sent before the
        split on some path, the split measures an older email than the one just sent."""
        preds = graph.predecessors(self.journey)
        out: list[str] = []
        for activity in self.journey.activities.values():
            if activity.kind != KIND_ENGAGEMENT or not activity.engagement_ref:
                continue
            nearest: set[str] = set()
            seen: set[str] = set()
            stack = list(preds.get(activity.key, ()))
            while stack:
                k = stack.pop()
                if k in seen:
                    continue
                seen.add(k)
                a = self.journey.activities.get(k)
                if a is None:
                    continue
                if a.kind == KIND_EMAIL:
                    nearest.add(k)
                    continue
                stack.extend(preds.get(k, ()))
            others = sorted(k for k in nearest if k != activity.engagement_ref)
            if others:
                ref = self.journey.activities.get(activity.engagement_ref)
                ref_name = ref.email_name if ref is not None and ref.email_name else activity.engagement_ref
                names = [f"'{self.journey.activities[k].email_name or k}' ({k})" for k in others]
                shown = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " oppure " + names[-1]
                out.append(f"pattern sospetto: l'engagement split '{activity.name or activity.key}' ({activity.key}) "
                           f"misura l'email '{ref_name}' ({activity.engagement_ref}), ma su alcuni percorsi l'ultima "
                           f"email inviata prima dello split è {shown}")
        return out

    def _same_name_different_conditions(self) -> list[str]:
        """Spec check carried over: splits with the same name but different conditions."""
        by_name: dict[str, dict[str, str]] = {}
        for activity in self.journey.activities.values():
            if activity.kind != KIND_DECISION or not activity.name:
                continue
            text = " | ".join(formula_text(o.formula) for o in activity.outcomes)
            by_name.setdefault(activity.name, {}).setdefault(text, activity.key)
        out: list[str] = []
        for name, variants in by_name.items():
            if len(variants) > 1:
                keys = ", ".join(variants.values())
                out.append(f"pattern sospetto: {len(variants)} split con condizioni diverse hanno lo stesso nome '{name}' ({keys})")
        return out

    def _case_buckets(self, scenarios: list[Scenario]):
        """The branches each case crosses, by kind of case: plainly, with a manual action, only after a random split
        (with or without a manual action), or in a case whose manual action cannot be done. The exit criteria met
        by a case count as branches (spec, step 3: "Every exit criterion is one extra coverage target")."""
        plain: dict[BranchRef, list[str]] = {}
        manual: dict[BranchRef, list[str]] = {}
        stat_plain: dict[BranchRef, list[str]] = {}
        stat_manual: dict[BranchRef, list[str]] = {}
        blocked: dict[BranchRef, list[tuple[str, str]]] = {}   # manual cases whose action cannot be done
        for scenario in scenarios:
            for case in scenario.cases:
                exits_before, exits_after = self.case_exits.get(case.id, ([], []))
                if case.manual_actions and case.inbox_blocked_reason:
                    refs = [(s.activity_key, s.outcome_key) for s in case.steps if s.outcome_key is not None]
                    if case.random is not None:
                        for rb in case.random.branches:
                            refs.append((case.random.activity_key, rb.outcome_key))
                            refs.extend((s.activity_key, s.outcome_key) for s in rb.steps if s.outcome_key is not None)
                    for ref in refs + exits_before + exits_after:
                        blocked.setdefault(ref, []).append((case.id, case.inbox_blocked_reason))
                    continue
                before = [(s.activity_key, s.outcome_key) for s in case.steps if s.outcome_key is not None]
                before.extend(exits_before)
                after: list[BranchRef] = []
                if case.random is not None:
                    for rb in case.random.branches:
                        after.append((case.random.activity_key, rb.outcome_key))
                        after.extend((s.activity_key, s.outcome_key) for s in rb.steps if s.outcome_key is not None)
                after.extend(exits_after)
                sure = manual if case.manual_actions else plain
                random_bucket = stat_manual if case.manual_actions else stat_plain
                for ref in before:
                    sure.setdefault(ref, []).append(case.id)
                for ref in after:
                    random_bucket.setdefault(ref, []).append(case.id)
        statistical: dict[BranchRef, list[str]] = {}
        for ref, ids in stat_plain.items():
            statistical.setdefault(ref, []).extend(ids)
        for ref, ids in stat_manual.items():
            if ref not in plain and ref not in stat_plain:
                manual.setdefault(ref, []).extend(ids)
                statistical.setdefault(ref, []).extend(ids)
            else:
                statistical.setdefault(ref, []).extend(ids)
        return plain, manual, statistical, blocked

    def _branch_statuses(self, scenarios: list[Scenario], buckets=None) -> list[BranchStatus]:
        """Each split branch: covered (a case without manual action crosses it), covered with
        manual action, or not covered with a reason. A branch crossed only after a random split
        is flagged statistical; the branches before the random split are crossed with certainty."""
        plain, manual, statistical, blocked = buckets if buckets is not None else self._case_buckets(scenarios)
        unsupported = set(self.package.unsupported_activities)
        preds = graph.predecessors(self.journey)
        statuses: list[BranchStatus] = []
        root_reasons: dict[BranchRef, str] = {}
        for activity, outcome in self.branch_list:
            ref = (activity.key, outcome.key)
            label = outcome.label or outcome.key
            stat_note = f"raggiunto solo attraverso un random split; caso statistico di {self.settings.contacts_per_random_case} contatti"
            if _unknown_engagement(activity):
                # Triage RB-5: the split measures neither an open nor a click, so no branch can be claimed,
                # whatever path crosses it; the loader warns that both branches are not covered.
                reason = self._not_covered_reason(activity, outcome, preds)
                root_reasons[ref] = reason
                statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                             STATUS_NOT_COVERED, reason, []))
            elif ref in plain:
                statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                             STATUS_COVERED, "", sorted(set(plain[ref]))))
            elif ref in manual:
                is_stat = ref in statistical and ref not in plain
                reason = "un contatto deve aprire o cliccare l'email" + ("; " + stat_note if is_stat else "")
                if self.settings.real_inbox:
                    statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                                 STATUS_MANUAL, reason, sorted(set(manual[ref])), statistical=is_stat))
                else:
                    statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                                 STATUS_NOT_COVERED,
                                                 "richiede un open o un click, ma non è configurata un'inbox reale; i casi esistono, con indirizzi non recapitabili",
                                                 sorted(set(manual[ref])), statistical=is_stat))
            elif ref in statistical:
                statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                             STATUS_COVERED, stat_note, sorted(set(statistical[ref])), statistical=True))
            elif ref in blocked:
                ids = sorted({cid for cid, _ in blocked[ref]})
                reason = blocked[ref][0][1]
                statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                             STATUS_NOT_COVERED,
                                             f"richiede un open o un click che non si può fare: {reason}; i casi esistono",
                                             ids))
            else:
                behind = None
                if activity.key in self.reached_activities or activity.key in unsupported:
                    reason = self._not_covered_reason(activity, outcome, preds)
                    root_reasons[ref] = reason
                else:
                    roots = self._root_branches(activity.key, preds)
                    if roots:
                        behind = roots[0]
                        root_activity = self.journey.activities[behind[0]]
                        root_outcome = root_activity.outcome(behind[1])
                        root_reason = root_reasons.get(behind)
                        if root_reason is None:
                            root_reason = self._not_covered_reason(root_activity, root_outcome, preds)
                            root_reasons[behind] = root_reason
                        prefix = ("a valle di un elemento non supportato"
                                  if root_reason.startswith(("attività non supportata", "branch non supportato",
                                                             "a valle di un elemento non supportato"))
                                  else "non raggiungibile")
                        # A wait at whose end the exit criteria take everyone out has no label of its own (spec, step 3).
                        root_label = ("fine del wait" if root_activity.kind in graph.WAIT_KINDS
                                      else label_of(root_outcome))
                        reason = (f"{prefix}: si trova a valle del branch '{root_activity.name or behind[0]}: {root_label}' "
                                  f"({behind[0]}), che non è coperto per questo motivo: {root_reason}")
                    else:
                        reason = self._not_covered_reason(activity, outcome, preds)
                statuses.append(BranchStatus(activity.key, activity.name, activity.kind, outcome.key, label,
                                             STATUS_NOT_COVERED, reason, [], behind=behind))
        return statuses

    # -- exit criteria (spec, step 3, "Exit criteria and goals") ---------------------------

    def _exit_refs(self, leaf: Leaf) -> list[BranchRef]:
        """The exit-criteria targets a leaf's mask holds: ``(EXIT_PREFIX + key, EXIT_OUTCOME)`` per criterion met."""
        out = []
        for exit_ in self.exits:
            ref = (EXIT_PREFIX + exit_.key, EXIT_OUTCOME)
            idx = self.branch_index.get(ref)
            if idx is not None and leaf.mask >> idx & 1:
                out.append(ref)
        return out

    def _exit_statuses(self, buckets) -> list[BranchStatus]:
        """Spec, step 3: "Every exit criterion is one extra coverage target, "exit criteria met at the end of a
        wait", so that the cover includes at least one case that leaves the journey this way when it is
        reachable; the summary counts it apart from the branches of the splits." One BranchStatus per evaluable
        criterion, with the same states as a branch."""
        plain, manual, statistical, blocked = buckets
        stat_note = f"raggiunto solo attraverso un random split; caso statistico di {self.settings.contacts_per_random_case} contatti"
        out: list[BranchStatus] = []
        for exit_ in self.exits:
            ref = (EXIT_PREFIX + exit_.key, EXIT_OUTCOME)
            head = (exit_.key, _exit_description(exit_) or exit_.name, EXIT_KIND, EXIT_OUTCOME, EXIT_LABEL)
            if ref in plain:
                out.append(BranchStatus(*head, STATUS_COVERED, "", sorted(set(plain[ref]))))
            elif ref in manual:
                is_stat = ref in statistical
                reason = "un contatto deve aprire o cliccare l'email" + ("; " + stat_note if is_stat else "")
                if self.settings.real_inbox:
                    out.append(BranchStatus(*head, STATUS_MANUAL, reason, sorted(set(manual[ref])), statistical=is_stat))
                else:
                    out.append(BranchStatus(*head, STATUS_NOT_COVERED,
                                            "richiede un open o un click, ma non è configurata un'inbox reale; i casi "
                                            "esistono, con indirizzi non recapitabili",
                                            sorted(set(manual[ref])), statistical=is_stat))
            elif ref in statistical:
                out.append(BranchStatus(*head, STATUS_COVERED, stat_note, sorted(set(statistical[ref])), statistical=True))
            elif ref in blocked:
                out.append(BranchStatus(*head, STATUS_NOT_COVERED,
                                        f"richiede un open o un click che non si può fare: {blocked[ref][0][1]}; i casi esistono",
                                        sorted({cid for cid, _ in blocked[ref]})))
            else:
                out.append(BranchStatus(*head, STATUS_NOT_COVERED, self._exit_not_covered_reason(exit_), []))
        return out

    def _exit_not_covered_reason(self, exit_: JourneyFilter) -> str:
        waits = [a.key for a in self.journey.activities.values() if a.kind in graph.WAIT_KINDS]
        if not waits or not self.waits_reached_by_paths:
            return ("non raggiungibile: nessun percorso del journey passa per un wait, e i criteri di uscita sono "
                    "valutati solo alla fine di un wait")
        if not any(key in self.reached_activities for key in waits):
            return ("non raggiungibile: nessun percorso simulato raggiunge un wait, quindi i criteri di uscita non "
                    "vengono mai valutati")
        return (f"non raggiungibile: nessun contatto che arriva alla fine di un wait soddisfa i criteri di uscita "
                f"({_exit_description(exit_)})")

    def _dead_wait(self, key: str) -> bool:
        """Every explored path that reaches the end of this wait leaves the journey there by the exit criteria."""
        return self.exited_at.get(key, 0) > 0 and self.passed_at.get(key, 0) == 0

    def _exits_text(self, wait_keys) -> str:
        """The descriptions of the exit criteria met at the end of these waits (all of them when none is known)."""
        met = set().union(*(self.exit_met_at.get(k, set()) for k in wait_keys)) if wait_keys else set()
        exits = [e for e in self.exits if e.key in met] or self.exits
        return " oppure ".join(_exit_description(e) for e in exits)

    def _wait_label(self, key: str) -> str:
        activity = self.journey.activities.get(key)
        name = activity.name if activity is not None and activity.name else ""
        return f"'{name}' ({key})" if name and name != key else f"'{key}'"

    def _dead_wait_reason(self, key: str) -> str:
        return (f"non raggiungibile: tutti i contatti che arrivano alla fine del wait {self._wait_label(key)} escono dal "
                f"journey per i criteri di uscita ({self._exits_text([key])})")

    def _dead_wait_warnings(self) -> list[str]:
        """Spec, step 3: "the report must say that every branch behind the first wait is unreachable for this
        reason, as a finding on the journey". One warning per wait at whose end every explored path leaves by the
        exit criteria, when something comes after it."""
        out = []
        for activity in self.journey.activities.values():
            if activity.kind not in graph.WAIT_KINDS or not self._dead_wait(activity.key):
                continue
            if not any(o.next for o in activity.outcomes):
                continue
            out.append(f"rilievo sul journey: tutti i contatti che arrivano alla fine del wait "
                       f"{self._wait_label(activity.key)} soddisfano i criteri di uscita ({self._exits_text([activity.key])}) "
                       "ed escono dal journey, quindi nessuno raggiunge le attività che seguono il wait e ogni branch a "
                       "valle è non raggiungibile: probabile errore di configurazione del journey, da segnalare al cliente "
                       "(decisione del team del 14 ottobre 2026)")
        return out

    def _exit_shadow(self) -> dict[BranchRef, set[str]]:
        """Branch -> the waits at whose end the contacts that would cross it leave by the exit criteria.

        Spec, step 3: "A branch no contact can reach because every contact that gets to the wait before it meets
        the exit criteria is reported as unreachable with that reason." Every explored path that left by the exit
        criteria is walked on from the end of its wait as if the exit criteria were false (a walker on the journey
        without exit criteria), branching on undecided values and on engagement and random splits as the
        exploration does; the branches those walks cross are recorded with the wait. Computed once, on demand, up
        to MAX_SHADOW_PATHS walks (a warning says when the limit is reached)."""
        if self._shadow is not None:
            return self._shadow
        self._shadow = {}
        if not self.exit_states:
            return self._shadow
        journey = dataclasses.replace(self.journey, exits=[])
        walker = graph.Walker(journey, self.schema, self.package.emails, self.settings.case_insensitive_text,
                              self.now, self.settings.date_format, row_fields=self.row_fields,
                              entry_time=self.entry_time, ends_at_once_when_unknown=self._wait_ends_at_once)
        walked = 0
        for cls, state, assigned in self.exit_states:
            wait_key = state.exit_by_criteria
            wait = journey.activities.get(wait_key or "")
            if wait is None:
                continue
            start = state.clone()
            start.steps.pop()
            start.timeline.pop()
            start.exit_by_criteria = None
            walker.take(start, wait, wait.outcomes[0] if wait.outcomes else None)
            stack = [(start, dict(assigned))]
            while stack:
                if walked >= MAX_SHADOW_PATHS:
                    self.warnings.append(
                        f"più di {MAX_SHADOW_PATHS} percorsi da seguire oltre i criteri di uscita: per alcuni branch non "
                        "raggiunti il motivo può non citare i criteri di uscita")
                    return self._shadow
                current, values = stack.pop()
                walker.run(current, self._lookup(cls, values))
                if current.stop == graph.STOP_UNDETERMINED and current.pending_field is not None \
                        and self._var(current.pending_field) in self.domains:
                    ref = self._var(current.pending_field)
                    for value in reversed(self.domains[ref]):
                        child = current.clone()
                        stack.append((child, {**values, ref: value}))
                    continue
                if current.stop == graph.STOP_CHOICE and current.current in journey.activities:
                    activity = journey.activities[current.current]
                    verdict = (graph.engagement_verdict(activity, current, self.package.emails, journey)
                               if activity.kind == KIND_ENGAGEMENT else None)
                    for outcome in reversed(activity.outcomes):
                        if verdict is not None and outcome.when is True and not verdict.possible:
                            continue
                        child = current.clone()
                        walker.take(child, activity, outcome)
                        stack.append((child, values))
                    continue
                walked += 1
                for ref in current.branches():
                    self._shadow.setdefault(ref, set()).add(wait_key)
        return self._shadow

    def _exit_reason(self, activity: Activity, outcome: Outcome) -> Optional[str]:
        """Why a branch is not covered when the exit criteria are the cause, else None.

        A wait at whose end every explored path leaves (``_dead_wait``): "tutti i contatti che arrivano alla fine
        del wait ... escono dal journey per i criteri di uscita"; the activities behind it inherit the reason
        through the root-cause computation. A branch the walks past the exit criteria cross (``_exit_shadow``):
        the contacts that would take it leave earlier, at the end of the waits named."""
        key = activity.key
        if not self.exit_states:
            return None
        if activity.kind in graph.WAIT_KINDS and self._dead_wait(key):
            return self._dead_wait_reason(key)
        waits = self._exit_shadow().get((key, outcome.key))
        if not waits:
            return None
        ordered = [k for k in self.journey.activities if k in waits]
        dead = [k for k in ordered if self._dead_wait(k)]
        if dead and key not in self.reached_activities:
            return self._dead_wait_reason(dead[0])
        shown = ", ".join(self._wait_label(k) for k in ordered)
        reached = "lo split è raggiunto, ma " if key in self.reached_activities else ""
        return (f"non raggiungibile: {reached}i contatti che prenderebbero questo branch escono prima dal journey alla "
                f"fine del wait {shown}, perché soddisfano i criteri di uscita ({self._exits_text(ordered)})")

    def _seen_text(self, ref: FieldRef, values: set[Value]) -> str:
        """The values a split saw in a field, for the reason of a branch that never matched."""
        if ref in self.row_fields.values():
            if all(not graph.is_null(v) for v in values):
                return f"il contatto ha sempre una riga in {ref[0]}"
            if all(graph.is_null(v) for v in values):
                return f"il contatto non ha mai una riga in {ref[0]}"
        elif ref[0].lower() in self.row_fields and all(graph.is_null(v) for v in values):
            return (f"{ref[0]}.{ref[1]} è sempre null, perché il contatto non ha una riga in {ref[0]} oppure il campo "
                    "è vuoto")
        if values == {KEY_MARKER}:
            return f"{ref[0]}.{ref[1]} contiene sempre la chiave generata"
        shown = ", ".join("la chiave generata" if v == KEY_MARKER else "null" if v is None else repr(v)
                          for v in sorted(values, key=lambda x: (x is None, str(x))))
        return f"{ref[0]}.{ref[1]} vale sempre uno tra {shown}"

    def _root_branches(self, key: str, preds: dict[str, set[str]]) -> list[BranchRef]:
        """For an activity no path reaches: the branches of reached activities that lead into the
        unreached region around it. Those branches are the root cause; everything behind them inherits it."""
        region: set[str] = set()
        stack = [key]
        while stack:
            k = stack.pop()
            if k in region:
                continue
            region.add(k)
            for p in preds.get(k, ()):
                if p not in self.reached_activities:
                    stack.append(p)
        roots: set[BranchRef] = set()
        for k in region:
            for p in preds.get(k, ()):
                if p in self.reached_activities:
                    for o in self.journey.activities[p].outcomes:
                        if o.next == k:
                            roots.add((p, o.key))
        return sorted(roots)

    def _not_covered_reason(self, activity: Activity, outcome: Outcome, preds: dict[str, set[str]]) -> str:
        key = activity.key
        if key in self.stopped_at:
            return f"attività non supportata: {self.stopped_at[key]}"
        if key in self.package.unsupported_activities:
            if activity.unsupported_reason:
                return f"attività non supportata: {activity.unsupported_reason}"
            return f"attività non supportata: il PoC non gestisce il tipo {activity.raw_type}"
        if outcome.unsupported_reason:
            return f"branch non supportato: {outcome.unsupported_reason}"
        if _unknown_engagement(activity):
            return ("branch non supportato: lo split misura un tipo di statistica che non è né un open né un click, "
                    "quindi nessuno dei suoi branch può essere considerato coperto")
        if activity.kind == KIND_DECISION:
            remainder = [o for o in activity.outcomes if o.is_remainder or o.formula is None]
            ordered = [o for o in activity.outcomes if o not in remainder] + remainder
            for earlier in ordered:
                if earlier is outcome:
                    break
                if earlier.unsupported_reason:
                    return (f"a valle di un elemento non supportato: questo branch viene dopo il branch non supportato "
                            f"'{label_of(earlier)}' dello stesso split, quindi non viene mai valutato")
        blocked = self.engagement_blocked.get((key, outcome.key))
        if blocked:
            return "non raggiungibile: " + blocked
        exit_reason = self._exit_reason(activity, outcome)
        if exit_reason is not None:
            return exit_reason
        if key not in self.reached_activities:
            parent = sorted(preds.get(key, ()))
            if not parent:
                return "non raggiungibile: nessun percorso dall'inizio del journey raggiunge questa attività"
            return ("non raggiungibile: nessuna combinazione di valori dei campi raggiunge questa attività "
                    f"(segue {', '.join(repr(p) for p in parent)})")
        # The split was reached but this outcome never matched.
        seen = self.reached_values.get(key, {})
        if activity.kind == KIND_DECISION and outcome.formula is not None:
            for cond in formula_conditions(outcome.formula):
                ref = self.schema.canonical((cond.table, cond.field))
                ftype = self.schema.field_type(ref)
                if (cond.operator in RANGE_OPERATORS and ftype not in RANGE_TYPES and cond.value_attribute is None
                        and cond.relative is None):
                    # build_domain picks no value inside a range of such a field, so the branch may well be
                    # reachable in Salesforce: an element the generator does not support, not an unreachable branch.
                    why = (f"la condizione confronta {ref[0]}.{ref[1]}, un campo di tipo {ftype}, con {cond.operator}: il "
                           "generatore sceglie valori dentro un intervallo solo per i campi Number, Decimal e Date, quindi "
                           "nei dati di test nessun contatto la soddisfa, anche se in Salesforce un contatto potrebbe")
                    self.stopped_at[f"{key}/{outcome.key}"] = why
                    return f"branch non supportato: {why}"
            for cond in formula_conditions(outcome.formula):
                ref = self.schema.canonical((cond.table, cond.field))
                table = self.schema.table(ref[0])
                field = table.field(ref[1]) if table is not None else None
                if (cond.value is not None and field is not None and field.length
                        and field.type in TEXT_TYPES and len(cond.value) > field.length):
                    return (f"non raggiungibile: il valore {cond.value!r} della condizione è più lungo di {ref[0]}.{ref[1]} "
                            f"({field.length} caratteri), quindi il campo non può mai contenerlo")
            fields = []
            for cond in formula_conditions(outcome.formula):
                ref = self.schema.canonical((cond.table, cond.field))
                values = seen.get(ref)
                if values:
                    fields.append(self._seen_text(ref, values))
            detail = "; ".join(fields) if fields else "la condizione non è mai vera"
            return (f"non raggiungibile: lo split è raggiunto, ma su quei percorsi la condizione di '{label_of(outcome)}' "
                    f"non è mai vera ({detail})")
        if activity.kind == KIND_DECISION and outcome.is_remainder:
            return "non raggiungibile: lo split è raggiunto, ma la condizione di un branch precedente è sempre vera"
        if activity.kind == KIND_ENGAGEMENT:
            return "non raggiungibile: lo split è raggiunto, ma nessun contatto prende questo branch"
        return "non raggiungibile: nessun percorso simulato attraversa questo branch"


def _unknown_engagement(activity: Activity) -> bool:
    """An engagement split whose statsTypeId is neither an open nor a click (triage RB-5)."""
    return activity.kind == KIND_ENGAGEMENT and activity.engagement_kind not in ("open", "click")


def label_of(outcome: Outcome) -> str:
    return outcome.label or outcome.key


def _exit_description(exit_: JourneyFilter) -> str:
    """An exit criterion in plain words: its description, else its formula."""
    return " ".join((exit_.description or "").split()) or formula_text(exit_.formula)


def expected_range(n: int, p: float) -> tuple[int, int]:
    """Accepted count on the smallest side of a random split, about two standard deviations
    around the mean (spec: 1 to 9 contacts on the 10% side of 50)."""
    if n <= 0 or p <= 0:
        return (0, 0)
    mean = n * p
    sd = math.sqrt(n * p * (1 - p))
    low = max(1, int(round(mean - 2 * sd)))
    high = min(n - 1, int(round(mean + 2 * sd)))
    return (low, max(low, high))


def compute_coverage(package: Package, table_model: TableModel, settings: Settings,
                     generation_time: Optional[datetime] = None) -> CoverageResult:
    """Entry point: spec steps 3 to 6 on one package."""
    return CoverageEngine(package, table_model, settings, generation_time).run()
