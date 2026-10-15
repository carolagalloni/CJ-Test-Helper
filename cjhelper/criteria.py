"""Decision-split criteria: parse the FilterDefinition XML and evaluate one condition.

Spec, "Input: the package export": "Split criteria are XML (FilterDefinition) with
nested AND/OR groups. Each condition names a table, a field, an operator and a value."
Spec, "Journey model and coverage", step 2: "Each decision split outcome becomes a
logical formula over table fields."

Shape read here (docs/plan.md, "criteria.py"; docs/export-format.md)::

    <FilterDefinition>
      <ConditionSet Operator="AND">
        <Condition Key="<attribute set>.<field>" Operator="Is">
          <AttributePath RelationshipID="..."><Value><![CDATA[true]]></Value></AttributePath>
        </Condition>
        <ConditionSet Operator="OR"> ... </ConditionSet>
      </ConditionSet>
    </FilterDefinition>

The condition's ``table`` is the attribute-set name as written in the key; loader.py
maps it to the table name. The value may be wrapped in any number of
``<AttributePath>`` elements; an empty CDATA is an empty value.

Shapes added by the spec section "Extensions beyond the pilot" (docs/export-format.md):

- Journey Data: ``<Condition IsEphemeralAttribute="true" Key="Event.<eventDefinitionKey>.<field>" ...>``.
  The condition gets ``source`` SOURCE_JOURNEY and ``table`` holds the event definition key; loader.py
  maps it to the entry table of the trigger's entry event.
- Attribute compared with an attribute: ``ValueParameterName="<attribute>"`` with an empty ``<Value>``,
  or ``ValueIsReference="true"`` with the attribute as the ``<Value>``. The condition gets
  ``value_attribute`` (as written) and no literal value; loader.py decides whether the form is the
  supported one.
- ``Between`` takes the value ``low|high`` and includes both bounds (confirmed by the team).

Shapes added by the spec section "Extensions for time-dependent journeys (step 3, 14 October 2026)":

- Relative dates: a value written ``;<sign>;<n>;<unit>`` (``;+;0;days`` is "Today", ``;-;7;days`` is
  "Today Minus 7 days") gives the condition ``relative`` (model.RelativeDate, the unit lower-cased);
  ``value`` keeps the text as written. Any operator may carry it when parsed; loader.py decides whether
  the field type and the unit allow it, and ``evaluate_condition`` accepts it only with the operators
  of ``RELATIVE_OPERATORS`` (spec: "The operators that take such a value are After, AtOrAfter, Before,
  AtOrBefore"; "Any other value form, unit or operator is unsupported"). It is evaluated against the
  clock, the ``now`` argument (see ``shift_date``, ``is_day_level``, ``relative_units``).
- Attribute path: the condition's ``path`` holds RELATIONSHIP IDS, as written in the export once the
  references are resolved: the ``groupToSetRelationshipId`` of the ``UiMetaData`` attribute (a JSON
  text; the relationship from the contact to the first attribute set) first, then the
  ``RelationshipID`` of every ``<AttributePath>`` wrapping the value, in document order, so the
  relationship nearest the contact comes first. Only loader.py knows the relationships, so here they
  stay ids; loader.py maps them to the attribute sets (tables) between the contact and the condition's
  table (spec, step 3, "Tables reached through other tables": "the relationship ids in AttributePath
  and UiMetaData"). The comment on ``model.Condition.path`` describes that mapped form.

Operators. Spec: "New activity types and operators are added by extending one lookup
table each." ``OPERATORS`` below is that one table for operators: to support a new
operator, add one entry to it (and, if the operator takes no value, list its name in
``OPERATORS_WITHOUT_VALUE`` right below it; if it is true on null, in
``OPERATORS_TRUE_ON_NULL``; if it takes a relative date, in ``RELATIVE_OPERATORS``).
Nothing else in the generator changes. The table holds the operators of the spec
section "Extensions beyond the pilot": the pilot's ``Is``, ``Equal``, ``IsNull``, the
earlier tool's operators that "can be carried over at low cost" (spec, "Supported in
the PoC") and ``Between``; and those of the step 3 section: the date operators
``After``, ``AtOrAfter``, ``Before``, ``AtOrBefore`` and ``IsNot``, the negation of
``Is``. Any other operator raises ``UnsupportedOperator`` when evaluated; spec:
"Anything else is not an error: it is listed in the report, and the branches behind
it are marked as not covered."

Assumptions (the spec does not say, or marks them as assumptions; each is listed in the developer report):

- A comparison with a null value is false for every operator except ``IsNull``,
  ``IsNotNull`` and ``IsNot``, as in SQL. An empty string is null, as in the CSV files
  (spec, "File format": "empty cell for null"). ``IsNot`` is true on null (spec, step 3,
  assumption: "on a Boolean IsNot true is true for false and for null"); ``NotEqual``
  keeps its earlier reading, false on null.
- ``After``, ``AtOrAfter``, ``Before``, ``AtOrBefore`` with an absolute value read as
  ``GreaterThan``, ``GreaterThanOrEqual``, ``LessThan``, ``LessThanOrEqual`` (spec, step 3:
  "The same operators with an absolute date value keep the earlier reading").
- A relative date is compared on the date part for the units days, weeks, months and years
  ("Today") and on the full date and time for hours and minutes ("Now") (spec, step 3,
  assumption). It compares the field as a date whatever the field type (loader.py rejects
  other types); a field value that is not a date never matches it.
- Moving a date by months or years keeps the day of the month, clamped to the last day of a
  shorter month (31 January plus 1 month is the last day of February); days, weeks, hours and
  minutes are fixed lengths. The spec does not say how Salesforce moves a date by months.
- Ordering operators on text compare the strings (folded when text comparisons
  ignore letter case); on Booleans they compare the normalised ``true``/``false``.
- A number or a date that cannot be parsed is compared as text by ``Is``/``Equal``/
  ``NotEqual`` and never satisfies an ordering operator.
- Dates are read as ISO 8601 (``YYYY-MM-DD[ HH:MM[:SS]]``); a time zone offset is
  converted to UTC and dropped, values without one are compared as written.
- A field type not listed in the model (or missing) is compared as text.
- ``Between`` on text compares the strings, as the ordering operators do; a lower bound
  above the upper bound matches nothing.
- A key written ``Event.<key>.<field>`` is Journey Data even without ``IsEphemeralAttribute``
  (every real export marks it); an attribute set called ``Event`` would be misread.
"""

from __future__ import annotations

import calendar
import html
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional, Union

from .model import (SOURCE_CONTACT, SOURCE_JOURNEY, Condition, ConditionGroup, Formula, RelativeDate, Value,
                    formula_text)

JOURNEY_DATA_PREFIX = "Event"     # Journey Data keys: Event.<eventDefinitionKey>.<field>
BETWEEN_SEPARATOR = "|"           # Between values: low|high
RELATIVE_VALUE = re.compile(r";([+-]);([0-9]+);([A-Za-z]+)")   # relative dates: ;<sign>;<n>;<unit>
GROUP_TO_SET_KEY = "groupToSetRelationshipId"                  # in the UiMetaData JSON of a condition


class CriteriaError(Exception):
    """The criteria XML cannot be read: not XML, not the expected shape, an empty group or an unknown element."""


class UnsupportedOperator(Exception):
    """A condition cannot be evaluated: an operator that is not in OPERATORS, or a relative date with an operator
    that does not take one, with an unsupported unit, or without the clock."""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_criteria(xml: str) -> Formula:
    """Turn one outcome's FilterDefinition XML into a formula.

    Plan: "A set with one item collapses to that item. An empty set or an unknown
    element -> CriteriaError." The root must be ``<FilterDefinition>`` holding exactly
    one ``<ConditionSet>`` (or one ``<Condition>``); several top-level items are not a
    documented shape and raise CriteriaError rather than being guessed as AND.
    """
    if not isinstance(xml, str) or not xml.strip():
        raise CriteriaError("I criteri sono vuoti.")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise CriteriaError(f"I criteri non sono un XML valido ({error}).") from None
    if _tag(root) != "FilterDefinition":
        raise CriteriaError(f"I criteri iniziano con <{_tag(root)}> invece che con <FilterDefinition>.")
    children = list(root)
    if len(children) != 1:
        raise CriteriaError(
            f"<FilterDefinition> contiene {len(children)} elementi di primo livello, mentre è atteso un solo "
            "gruppo di condizioni."
        )
    return _parse_item(children[0])


def _parse_item(element: ET.Element) -> Formula:
    tag = _tag(element)
    if tag == "ConditionSet":
        return _parse_set(element)
    if tag == "Condition":
        return _parse_condition(element)
    raise CriteriaError(f"I criteri contengono l'elemento sconosciuto <{tag}>.")


def _parse_set(element: ET.Element) -> Formula:
    """A ``<ConditionSet>``: its items joined by AND or OR; a single item stands alone."""
    items = [_parse_item(child) for child in element]
    if not items:
        raise CriteriaError("Un <ConditionSet> dei criteri è vuoto.")
    if len(items) == 1:
        return items[0]
    operator = (element.get("Operator") or "").strip().upper()
    if operator not in ("AND", "OR"):
        raise CriteriaError(f"Un <ConditionSet> ha l'operatore {element.get('Operator')!r}, mentre è atteso AND o OR.")
    return ConditionGroup(operator, tuple(items))


def split_attribute(text: str) -> Optional[tuple[str, str, str]]:
    """``(source, owner, field)`` of an attribute as written in a condition key or a value attribute.

    ``Event.<eventDefinitionKey>.<field>`` is Journey Data: ``(SOURCE_JOURNEY, event key, field)``.
    ``<attribute set>.<field>`` is Contact Data: ``(SOURCE_CONTACT, set name, field)``; plan: "Key
    splits on the first . into set and field (field names may contain spaces)". None for anything else.
    """
    prefix, dot, rest = text.partition(".")
    if prefix == JOURNEY_DATA_PREFIX and dot:
        event_key, dot, field_name = rest.partition(".")
        if dot and event_key and field_name:
            return SOURCE_JOURNEY, event_key, field_name
    set_name, dot, field_name = text.partition(".")
    if not dot or not set_name or not field_name:
        return None
    return SOURCE_CONTACT, set_name, field_name


def between_bounds(value: Value) -> Optional[tuple[str, str]]:
    """``(low, high)`` of a Between value written ``low|high``; None when it is not written that way."""
    if value is None:
        return None
    low, separator, high = value.partition(BETWEEN_SEPARATOR)
    if not separator or BETWEEN_SEPARATOR in high or not low.strip() or not high.strip():
        return None
    return low.strip(), high.strip()


def relative_date(value: Value) -> Optional[RelativeDate]:
    """The relative date of a condition value written ``;<sign>;<n>;<unit>``; None for any other value.

    Spec, step 3, "Relative dates in conditions": "A condition value written ;<sign>;<n>;<unit> (for
    example ;+;0;days or ;-;7;days, shown in the interface as "Today", "Today Plus 90 days", "Today Minus
    7 days") compares the field with the clock's date moved by n units." The text must be exactly that
    form (no spaces); the unit is lower-cased and kept even when it is not supported, so that the
    evaluation can report it (``relative_units``).
    """
    if value is None:
        return None
    match = RELATIVE_VALUE.fullmatch(value)
    if match is None:
        return None
    sign, amount, unit = match.groups()
    return RelativeDate(sign=1 if sign == "+" else -1, amount=int(amount), unit=unit.lower())


def _parse_condition(element: ET.Element) -> Condition:
    """A ``<Condition Key="Set.Field" Operator="..">`` with its value.

    The key is Contact Data (``<attribute set>.<field>``) or Journey Data
    (``Event.<eventDefinitionKey>.<field>``, see ``split_attribute``). The value is None for
    operators without a value (model.Condition) and when the value is another attribute
    (``ValueParameterName``, or ``ValueIsReference="true"`` with the attribute in ``<Value>``),
    which goes to ``value_attribute`` as written. A value written as a relative date also gives
    ``relative`` (``relative_date``). ``path`` holds the relationship ids of the condition
    (``_relationship_ids``), which loader.py maps to the tables between the contact and the field.
    """
    key = element.get("Key") or ""
    parts = split_attribute(key)
    if parts is None:
        raise CriteriaError(f"La chiave della condizione {key!r} non è scritta nella forma <attribute set>.<campo> "
                            "né nella forma Event.<event definition key>.<campo>.")
    source, owner, field_name = parts
    if _true(element.get("IsEphemeralAttribute")) and source != SOURCE_JOURNEY:
        raise CriteriaError(f"La condizione {key} è segnata come Journey Data (IsEphemeralAttribute), ma la chiave "
                            "non è nella forma Event.<event definition key>.<campo>.")
    operator = (element.get("Operator") or "").strip()
    if not operator:
        raise CriteriaError(f"La condizione {key} non ha un operatore.")
    values = _values(element)
    if len(values) > 1:
        raise CriteriaError(f"La condizione {key} contiene {len(values)} valori, mentre ne è atteso uno.")
    path = _relationship_ids(element)
    parameter = (element.get("ValueParameterName") or "").strip()
    if parameter:
        if values and values[0].strip():
            raise CriteriaError(f"La condizione {key} ha sia un valore sia un attributo da confrontare "
                                f"(ValueParameterName {parameter}).")
        return Condition(table=owner, field=field_name, operator=operator, value=None, source=source,
                         value_attribute=parameter, path=path)
    if _true(element.get("ValueIsReference")):
        if not values or not values[0].strip():
            raise CriteriaError(f"La condizione {key} confronta il campo con un altro attributo (ValueIsReference), "
                                "ma non indica quale.")
        return Condition(table=owner, field=field_name, operator=operator, value=None, source=source,
                         value_attribute=values[0].strip(), path=path)
    if operator in OPERATORS_WITHOUT_VALUE:
        value: Value = None
    elif values:
        value = values[0]
    elif operator in OPERATORS:
        raise CriteriaError(f"La condizione {key} {operator} non ha un <Value>.")
    else:
        value = None    # unknown operator: UnsupportedOperator is raised when it is evaluated
    return Condition(table=owner, field=field_name, operator=operator, value=value, source=source,
                     relative=relative_date(value), path=path)


def _true(text: Optional[str]) -> bool:
    return (text or "").strip().lower() == "true"


def _values(element: ET.Element) -> list[str]:
    """Texts of the ``<Value>`` elements under a condition, looking through ``<AttributePath>`` wrappers."""
    found: list[str] = []
    for child in element:
        tag = _tag(child)
        if tag == "Value":
            found.append(child.text or "")
        elif tag == "AttributePath":
            found.extend(_values(child))
        else:
            raise CriteriaError(f"Una condizione contiene l'elemento sconosciuto <{tag}>.")
    return found


def _relationship_ids(element: ET.Element) -> tuple[str, ...]:
    """The relationship ids of a condition's attribute path, the one nearest the contact first.

    Spec, step 3, "Tables reached through other tables": "The export names the path: the attribute
    path of the condition (the relationship ids in AttributePath and UiMetaData)". First the
    ``groupToSetRelationshipId`` of ``UiMetaData`` (from the contact to the first attribute set),
    then the ``RelationshipID`` of every ``<AttributePath>`` around the value, outermost first (in the
    pilot: contact -> Master, Master -> Entry, Entry -> Config). Ids are kept as written; a missing
    or empty one is skipped. Empty when the export gives no path (Journey Data: ``UiMetaData="{}"``).
    """
    ids: list[str] = []
    group_id = _group_to_set_id(element.get("UiMetaData"))
    if group_id:
        ids.append(group_id)
    for child in element.iter():     # document order: the outermost AttributePath first
        if _tag(child) == "AttributePath":
            relationship_id = (child.get("RelationshipID") or "").strip()
            if relationship_id:
                ids.append(relationship_id)
    return tuple(ids)


def _group_to_set_id(text: Optional[str]) -> Optional[str]:
    """``groupToSetRelationshipId`` of a ``UiMetaData`` JSON text; None when it is missing or not readable.

    The XML parser has already decoded ``&quot;``; a text escaped twice is unescaped once more.
    """
    if not text or not text.strip():
        return None
    meta = _json_object(text)
    if meta is None and html.unescape(text) != text:
        meta = _json_object(html.unescape(text))
    found = meta.get(GROUP_TO_SET_KEY) if meta is not None else None
    if not isinstance(found, str) or not found.strip():
        return None
    return found.strip()


def _json_object(text: str) -> Optional[dict]:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _tag(element: ET.Element) -> str:
    """Element name without an XML namespace."""
    return element.tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_condition(cond: Condition, value: Value, field_type: str, case_insensitive: bool,
                       now: Optional[datetime] = None) -> bool:
    """Is ``cond`` true for a contact whose field holds ``value``?

    ``value`` is the CSV string or None for null; ``field_type`` is the schema type of
    the field (``Text``, ``Boolean``, ``Date``, ...); ``case_insensitive`` is the
    setting "Text comparisons ignore letter case" (spec default: off); ``now`` is the
    clock when the contact reaches the activity (spec, step 3, "The clock"), needed only
    by a condition with a relative date (``_relative_reference``).
    Plan: "IsNull: None or "". IsNotNull: the opposite. Any other operator on None ->
    False", except ``IsNot`` (spec, step 3: true on null). Raises UnsupportedOperator for
    an operator that is not in OPERATORS, and for a relative date with an operator that
    does not take one, with an unsupported unit, or without ``now``.
    """
    test = OPERATORS.get(cond.operator)
    if test is None:
        raise UnsupportedOperator(f"L'operatore {cond.operator!r} non è supportato ({formula_text(cond)}).")
    if cond.value_attribute is not None:
        # Spec, "Attribute compared with an attribute": supported only as "the field linked to the contact
        # key Equal the contact key", read as "the contact has a row in that table" (loader.py checks the
        # form). The row is there exactly when its linked field holds a value.
        if cond.operator != "Equal":
            raise UnsupportedOperator(f"Il confronto tra due attributi con l'operatore {cond.operator!r} non è "
                                      f"supportato ({formula_text(cond)}).")
        return not _is_null(value)
    if cond.operator in OPERATORS_WITHOUT_VALUE:
        return test(value, None, field_type, case_insensitive)
    expected = cond.value
    if cond.relative is not None:
        expected = _relative_reference(cond, now)
        field_type = DATE_TYPE    # a relative date compares dates (loader.py rejects other field types)
    if expected is None:
        return False
    if _is_null(value):
        return cond.operator in OPERATORS_TRUE_ON_NULL
    return test(value, expected, field_type, case_insensitive)


def _relative_reference(cond: Condition, now: Optional[datetime]) -> str:
    """The relative date of ``cond`` resolved against the clock, written as an absolute condition value.

    Spec, step 3: the value "compares the field with the clock's date moved by n units"; "with the unit
    days, weeks, months or years the field's date part is compared with the clock's date part, as the
    interface says "Today"; with hours or minutes the full date and time are compared with the clock
    ("Now")" (the day-level part confirmed by the team on 14 October 2026; hours and minutes stay an assumption,
    docs/assumptions.md row 48). The reference is therefore written as a date alone at day level and as a
    date and time otherwise, and the operator reads it exactly as an absolute value: "a value without
    a time compares on the date part". "Both bounds are inclusive for AtOrAfter and AtOrBefore."
    """
    assert cond.relative is not None
    if cond.operator not in RELATIVE_OPERATORS:
        raise UnsupportedOperator(f"L'operatore {cond.operator!r} non è supportato con una data relativa "
                                  f"({formula_text(cond)}); la accettano solo {', '.join(sorted(RELATIVE_OPERATORS))}.")
    if now is None:
        raise UnsupportedOperator(f"La condizione {formula_text(cond)} confronta il campo con una data relativa, ma "
                                  "manca l'orologio della simulazione (il momento in cui il contatto la valuta).")
    reference = shift_date(now, cond.relative)
    if is_day_level(cond.relative):
        return reference.date().isoformat()
    return reference.isoformat(sep=" ")


# ---------------------------------------------------------------------------
# Relative dates (spec, step 3, "Relative dates in conditions"); coverage.py uses these helpers too
# ---------------------------------------------------------------------------

DATE_TYPE = "Date"
# Units compared on the date part ("Today") and on the full date and time ("Now"), as written lower case.
DAY_LEVEL_UNITS = ("days", "weeks", "months", "years")
TIME_LEVEL_UNITS = ("hours", "minutes")
_MONTHS_PER_UNIT = {"months": 1, "years": 12}    # calendar units; the others are fixed lengths (timedelta)


def relative_units() -> tuple[str, ...]:
    """The units a relative date may use (spec, step 3: days, weeks, months, years, hours, minutes)."""
    return DAY_LEVEL_UNITS + TIME_LEVEL_UNITS


def is_day_level(rel: RelativeDate) -> bool:
    """True when a condition with ``rel`` compares the date part, False when it compares the date and time.

    Spec, step 3 (day-level part confirmed by the team on 14 October 2026): "with the unit days, weeks, months
    or years the field's date part is compared with the clock's date part ...; with hours or minutes the full
    date and time are compared" (hours and minutes: assumption, docs/assumptions.md row 48).
    Raises UnsupportedOperator for a unit that is not supported.
    """
    return _known_unit(rel) in DAY_LEVEL_UNITS


def shift_date(moment: datetime, rel: RelativeDate) -> datetime:
    """``moment`` moved by ``rel``: by ``rel.sign * rel.amount`` units, so a negative amount moves back too.

    Spec, step 3: a relative value is "the clock's date moved by n units". Days, weeks, hours and
    minutes are fixed lengths; months and years are calendar steps that keep the day of the month,
    clamped to the last day of a shorter month (31 January plus 1 month is 28 or 29 February;
    assumption, see the module docstring). The time of day is kept. Raises UnsupportedOperator for
    a unit that is not supported and for a result outside the calendar.
    """
    unit = _known_unit(rel)
    steps = rel.sign * rel.amount
    try:
        if unit in _MONTHS_PER_UNIT:
            return _add_months(moment, steps * _MONTHS_PER_UNIT[unit])
        return moment + timedelta(**{unit: steps})
    except (OverflowError, ValueError):
        raise UnsupportedOperator(f"La data {moment.isoformat(sep=' ')} spostata di {steps} {unit} esce dal "
                                  "calendario.") from None


def _add_months(moment: datetime, months: int) -> datetime:
    """``moment`` moved by whole calendar months, the day clamped to the last day of the target month."""
    year, month_index = divmod(moment.year * 12 + moment.month - 1 + months, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def _known_unit(rel: RelativeDate) -> str:
    """The unit of ``rel`` in lower case; UnsupportedOperator when it is not one of ``relative_units()``."""
    unit = rel.unit.strip().lower()
    if unit not in relative_units():
        raise UnsupportedOperator(f"L'unità {rel.unit!r} della data relativa non è supportata "
                                  f"(unità supportate: {', '.join(relative_units())}).")
    return unit


# Each test receives (field value, condition value, field type, case_insensitive).
# evaluate_condition handles nulls before calling a test that takes a value, so those
# tests always receive two strings.
OperatorTest = Callable[[Value, Value, str, bool], bool]


def _is_null(value: Value) -> bool:
    return value is None or value == ""


def _test_is_null(actual: Value, _expected: Value, _field_type: str, _case_insensitive: bool) -> bool:
    return _is_null(actual)


def _test_is_not_null(actual: Value, _expected: Value, _field_type: str, _case_insensitive: bool) -> bool:
    return not _is_null(actual)


def _test_equal(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    order = _compare(actual, expected, field_type, case_insensitive)
    if order is None:   # a number or a date that does not parse: compare the text
        return _fold(actual, case_insensitive) == _fold(expected, case_insensitive)
    return order == 0


def _test_not_equal(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return not _test_equal(actual, expected, field_type, case_insensitive)


def _test_greater_than(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _compare(actual, expected, field_type, case_insensitive) == 1


def _test_less_than(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _compare(actual, expected, field_type, case_insensitive) == -1


def _test_greater_or_equal(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _compare(actual, expected, field_type, case_insensitive) in (0, 1)


def _test_less_or_equal(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _compare(actual, expected, field_type, case_insensitive) in (-1, 0)


def _test_contains(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _text_form(expected, field_type, case_insensitive) in _text_form(actual, field_type, case_insensitive)


def _test_begins_with(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _text_form(actual, field_type, case_insensitive).startswith(_text_form(expected, field_type, case_insensitive))


def _test_ends_with(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    return _text_form(actual, field_type, case_insensitive).endswith(_text_form(expected, field_type, case_insensitive))


def _test_between(actual: str, expected: str, field_type: str, case_insensitive: bool) -> bool:
    """Spec: "Between takes the value low|high and includes both bounds (confirmed)"."""
    bounds = between_bounds(expected)
    if bounds is None:
        raise UnsupportedOperator(f"Between ha il valore {expected!r}, che non è nella forma basso|alto.")
    low, high = bounds
    return (_compare(actual, low, field_type, case_insensitive) in (0, 1)
            and _compare(actual, high, field_type, case_insensitive) in (-1, 0))


# The one lookup table for operators (spec: "New activity types and operators are added
# by extending one lookup table each"). Names as written in the criteria XML.
OPERATORS: dict[str, OperatorTest] = {
    # used by the pilot journey (spec, "Supported in the PoC")
    "Is": _test_equal,
    "Equal": _test_equal,
    "IsNull": _test_is_null,
    # carried over from the earlier tool (spec: "they can be carried over at low cost")
    "NotEqual": _test_not_equal,
    "IsNotNull": _test_is_not_null,
    "GreaterThan": _test_greater_than,
    "LessThan": _test_less_than,
    "GreaterThanOrEqual": _test_greater_or_equal,
    "LessThanOrEqual": _test_less_or_equal,
    "Contains": _test_contains,
    "BeginsWith": _test_begins_with,
    "EndsWith": _test_ends_with,
    # spec, "Extensions beyond the pilot": value low|high, both bounds included (confirmed)
    "Between": _test_between,
    # spec, step 3, "Relative dates in conditions": the date operators, with an absolute value read as the
    # ordering operators ("The same operators with an absolute date value keep the earlier reading")
    "After": _test_greater_than,
    "AtOrAfter": _test_greater_or_equal,
    "Before": _test_less_than,
    "AtOrBefore": _test_less_or_equal,
    # spec, step 3: "IsNot is the negation of Is"; unlike NotEqual it is true on null (OPERATORS_TRUE_ON_NULL)
    "IsNot": _test_not_equal,
}

# Operators of the table above that take no value: their condition value is None.
OPERATORS_WITHOUT_VALUE: frozenset[str] = frozenset({"IsNull", "IsNotNull"})

# Operators of the table above that are true when the field is null. Spec, step 3 (assumption): "on a Boolean
# IsNot true is true for false and for null ... the rule "any other operator on null is false" does not apply
# to a negated operator; NotEqual keeps its earlier reading".
OPERATORS_TRUE_ON_NULL: frozenset[str] = frozenset({"IsNot"})

# Operators of the table above that take a relative date value (spec, step 3: "The operators that take such a
# value are After, AtOrAfter, Before, AtOrBefore"; "Any other value form, unit or operator is unsupported").
RELATIVE_OPERATORS: frozenset[str] = frozenset({"After", "AtOrAfter", "Before", "AtOrBefore"})


# ---------------------------------------------------------------------------
# Comparison by field type (plan, "criteria.py": semantics in evaluate_condition)
# ---------------------------------------------------------------------------

_TYPE_FAMILIES = {"boolean": "boolean", "number": "number", "decimal": "number", "date": "date"}
_DATE_ONLY = re.compile(r"\d{4}-?\d{2}-?\d{2}")

Comparable = Union[str, Decimal, datetime, date]


def _family(field_type: Optional[str]) -> str:
    """boolean, number, date, or text (Text, EmailAddress, Phone, Locale and anything unknown)."""
    return _TYPE_FAMILIES.get((field_type or "").strip().lower(), "text")


def _compare(actual: str, expected: str, field_type: str, case_insensitive: bool) -> Optional[int]:
    """-1, 0 or 1 as ``actual`` is below, equal to or above ``expected``; None when they cannot be compared.

    Plan: "Boolean: compare after lower-casing, accepting true/false/1/0. Text,
    EmailAddress, Phone, Locale: exact match; case-insensitive when the setting says so.
    Number/Decimal: numeric compare. Date: parse both sides; date-only condition values
    compare on the date part."
    """
    family = _family(field_type)
    left: Optional[Comparable]
    right: Optional[Comparable]
    if family == "boolean":
        left, right = _boolean_text(actual), _boolean_text(expected)
    elif family == "number":
        left, right = _number(actual), _number(expected)
    elif family == "date":
        left, right = _date(actual), _date(expected)
        if left is not None and right is not None and _DATE_ONLY.fullmatch(expected.strip()):
            left, right = left.date(), right.date()
    else:
        left, right = _fold(actual, case_insensitive), _fold(expected, case_insensitive)
    if left is None or right is None:
        return None
    return (left > right) - (left < right)


def _fold(text: str, case_insensitive: bool) -> str:
    return text.casefold() if case_insensitive else text


def _text_form(text: str, field_type: str, case_insensitive: bool) -> str:
    """The string Contains / BeginsWith / EndsWith look at."""
    if _family(field_type) == "boolean":
        return _boolean_text(text)
    return _fold(text, case_insensitive)


def _boolean_text(text: str) -> str:
    """``true`` / ``false`` for the spellings True, true, TRUE, 1, False, false, 0; anything else lower-cased."""
    lowered = text.strip().lower()
    return {"1": "true", "0": "false"}.get(lowered, lowered)


def _number(text: str) -> Optional[Decimal]:
    try:
        number = Decimal(text.strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _date(text: str) -> Optional[datetime]:
    """An ISO 8601 date or date and time (``YYYY-MM-DD[ HH:MM[:SS]]``), or None."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment
