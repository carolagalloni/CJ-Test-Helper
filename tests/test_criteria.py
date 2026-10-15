"""Unit tests for cjhelper.criteria: FilterDefinition parsing and condition evaluation."""

from datetime import datetime

import pytest

from cjhelper.criteria import (
    OPERATORS,
    OPERATORS_TRUE_ON_NULL,
    OPERATORS_WITHOUT_VALUE,
    RELATIVE_OPERATORS,
    CriteriaError,
    UnsupportedOperator,
    between_bounds,
    evaluate_condition,
    is_day_level,
    parse_criteria,
    relative_date,
    relative_units,
    shift_date,
    split_attribute,
)
from cjhelper.model import SOURCE_CONTACT, SOURCE_JOURNEY, Condition, ConditionGroup, RelativeDate, formula_text

# The relationship ids condition_xml writes: groupToSetRelationshipId first (spec, step 3, "Tables reached
# through other tables"), then one per <AttributePath>, the outermost first.
ROOT = ("rel-root",)


def condition_xml(key, operator, value="", wrappers=0, with_value=True):
    """One <Condition>, its value optionally wrapped in <AttributePath> elements as in the pilot.

    The wrappers carry the relationship ids rel-0 (outermost), rel-1, ...; UiMetaData carries rel-root.
    """
    inner = f"<Value><![CDATA[{value}]]></Value>" if with_value else ""
    for level in reversed(range(wrappers)):
        inner = f'<AttributePath RelationshipID="rel-{level}">{inner}</AttributePath>'
    return (f'<Condition Key="{key}" Operator="{operator}" '
            f'UiMetaData="{{&quot;groupToSetRelationshipId&quot;:&quot;rel-root&quot;}}">{inner}</Condition>')


def wrapped_path(wrappers):
    """The path condition_xml(..., wrappers=n) gives."""
    return ROOT + tuple(f"rel-{level}" for level in range(wrappers))


def set_xml(*items, operator="AND"):
    return f'<ConditionSet Operator="{operator}" ConditionSetName="Individual Filter Grouping">{"".join(items)}</ConditionSet>'


def filter_xml(*items, operator="AND"):
    return f"<FilterDefinition>{set_xml(*items, operator=operator)}</FilterDefinition>"


# ---------------------------------------------------------------------------
# parse_criteria
# ---------------------------------------------------------------------------

def test_single_condition_collapses_to_the_condition():
    xml = filter_xml(condition_xml("Config Set.CHANNEL1", "Equal", "Email"))
    assert parse_criteria(xml) == Condition("Config Set", "CHANNEL1", "Equal", "Email", path=ROOT)


def test_and_of_two_conditions():
    xml = filter_xml(condition_xml("Master.EmailOptOut", "Is", "false"),
                     condition_xml("Master.MarketingPermission", "Is", "true"))
    assert parse_criteria(xml) == ConditionGroup("AND", (
        Condition("Master", "EmailOptOut", "Is", "false", path=ROOT),
        Condition("Master", "MarketingPermission", "Is", "true", path=ROOT),
    ))


def test_nested_or_with_isnull_and_empty_cdata():
    # The pilot's "Phone consent" criteria: IsMailOffers AND (IsDunning false OR IsDunning null) AND PhoneConsent.
    xml = filter_xml(
        condition_xml("Master.IsMailOffers", "Is", "true"),
        set_xml(condition_xml("Master.IsDunning", "Is", "false"),
                condition_xml("Master.IsDunning", "IsNull", ""),
                operator="OR"),
        condition_xml("Master.PhoneConsent", "Is", "true"),
    )
    assert parse_criteria(xml) == ConditionGroup("AND", (
        Condition("Master", "IsMailOffers", "Is", "true", path=ROOT),
        ConditionGroup("OR", (
            Condition("Master", "IsDunning", "Is", "false", path=ROOT),
            Condition("Master", "IsDunning", "IsNull", None, path=ROOT),
        )),
        Condition("Master", "PhoneConsent", "Is", "true", path=ROOT),
    ))


@pytest.mark.parametrize("wrappers", [1, 2, 3])
def test_value_inside_attribute_path_wrappers(wrappers):
    xml = filter_xml(condition_xml("Config.IGNOREPERM1", "Is", "true", wrappers=wrappers))
    assert parse_criteria(xml) == Condition("Config", "IGNOREPERM1", "Is", "true", path=wrapped_path(wrappers))


def test_field_name_with_a_space():
    xml = filter_xml(condition_xml("Config.Control Group Required", "Is", "true", wrappers=2))
    assert parse_criteria(xml) == Condition("Config", "Control Group Required", "Is", "true", path=wrapped_path(2))


def test_key_splits_on_the_first_dot():
    xml = filter_xml(condition_xml("Config.Field.With.Dots", "Equal", "x"))
    condition = parse_criteria(xml)
    assert (condition.table, condition.field) == ("Config", "Field.With.Dots")


def test_nested_set_with_one_item_collapses():
    xml = filter_xml(condition_xml("A.x", "Is", "true"),
                     set_xml(condition_xml("A.y", "Is", "false"), operator="OR"))
    assert parse_criteria(xml) == ConditionGroup("AND", (
        Condition("A", "x", "Is", "true", path=ROOT),
        Condition("A", "y", "Is", "false", path=ROOT),
    ))


def test_set_operator_is_upper_cased():
    xml = filter_xml(condition_xml("A.x", "Is", "true"), condition_xml("A.y", "Is", "true"), operator="or")
    assert parse_criteria(xml).operator == "OR"


def test_values_are_kept_as_written():
    assert parse_criteria(filter_xml(condition_xml("A.x", "Equal", "EMAIL"))).value == "EMAIL"
    assert parse_criteria(filter_xml(condition_xml("A.x", "Equal", " Email "))).value == " Email "
    assert parse_criteria(filter_xml(condition_xml("A.x", "Equal", ""))).value == ""


def test_operators_without_value_get_none():
    assert parse_criteria(filter_xml(condition_xml("A.x", "IsNotNull", ""))).value is None
    assert parse_criteria(filter_xml(condition_xml("A.x", "IsNull", "", with_value=False))).value is None


def test_unknown_operator_is_parsed_and_rejected_only_when_evaluated():
    # Between used to be the example here; the extension section of the spec added it to the operators.
    condition = parse_criteria(filter_xml(condition_xml("A.x", "NotContains", "1")))
    assert condition == Condition("A", "x", "NotContains", "1", path=ROOT)
    with pytest.raises(UnsupportedOperator):
        evaluate_condition(condition, "1", "Number", False)


def test_xml_namespace_is_ignored():
    xml = ('<FilterDefinition xmlns="http://example.com/filter">'
           + set_xml(condition_xml("A.x", "Is", "true")) + "</FilterDefinition>")
    assert parse_criteria(xml) == Condition("A", "x", "Is", "true", path=ROOT)


@pytest.mark.parametrize("xml", [
    "",
    "   ",
    "not xml at all",
    "<FilterDefinition><ConditionSet Operator='AND'>",
    "<ConditionSet Operator='AND'></ConditionSet>",
    "<FilterDefinition/>",
    "<FilterDefinition><ConditionSet Operator='AND'/></FilterDefinition>",
    filter_xml(condition_xml("A.x", "Is", "true"), set_xml(operator="OR")),
    filter_xml("<Unknown/>"),
    filter_xml(condition_xml("A.x", "Is", "true"), condition_xml("A.y", "Is", "true"), operator="XOR"),
    filter_xml(condition_xml("A.x", "Is", "true"), condition_xml("A.y", "Is", "true"), operator=""),
    filter_xml(condition_xml("NoDot", "Is", "true")),
    filter_xml(condition_xml(".x", "Is", "true")),
    filter_xml(condition_xml("A.", "Is", "true")),
    filter_xml(condition_xml("A.x", "", "true")),
    filter_xml(condition_xml("A.x", "Equal", "", with_value=False)),
    filter_xml('<Condition Key="A.x" Operator="Is"><Value>1</Value><Value>2</Value></Condition>'),
    filter_xml('<Condition Key="A.x" Operator="Is"><Other/><Value>1</Value></Condition>'),
    "<FilterDefinition>" + set_xml(condition_xml("A.x", "Is", "true"))
    + set_xml(condition_xml("A.y", "Is", "true")) + "</FilterDefinition>",
])
def test_bad_criteria_raise_criteria_error(xml):
    with pytest.raises(CriteriaError):
        parse_criteria(xml)


def test_non_string_criteria_raise_criteria_error():
    with pytest.raises(CriteriaError):
        parse_criteria(None)


# ---------------------------------------------------------------------------
# OPERATORS and evaluate_condition
# ---------------------------------------------------------------------------

def check(operator, actual, expected, field_type="Text", case_insensitive=False):
    """Evaluate one condition on a field holding ``actual``."""
    condition = Condition("T", "f", operator, None if operator in OPERATORS_WITHOUT_VALUE else expected)
    return evaluate_condition(condition, actual, field_type, case_insensitive)


def test_operator_table_holds_the_spec_operators():
    # Spec, "Extensions beyond the pilot", "Operators, types and values": the supported operators; spec,
    # step 3, "Relative dates in conditions": After, AtOrAfter, Before, AtOrBefore and IsNot.
    assert set(OPERATORS) == {
        "Is", "Equal", "IsNull",
        "NotEqual", "IsNotNull", "GreaterThan", "LessThan", "GreaterThanOrEqual", "LessThanOrEqual",
        "Contains", "BeginsWith", "EndsWith", "Between",
        "After", "AtOrAfter", "Before", "AtOrBefore", "IsNot",
    }
    assert OPERATORS_WITHOUT_VALUE == {"IsNull", "IsNotNull"}
    assert OPERATORS_TRUE_ON_NULL == {"IsNot"}
    assert RELATIVE_OPERATORS == {"After", "AtOrAfter", "Before", "AtOrBefore"}
    assert (OPERATORS_WITHOUT_VALUE | OPERATORS_TRUE_ON_NULL | RELATIVE_OPERATORS) <= set(OPERATORS)


@pytest.mark.parametrize("actual, expected, result", [
    ("True", "true", True),
    ("true", "true", True),
    ("TRUE", "true", True),
    ("1", "true", True),
    ("False", "false", True),
    ("0", "false", True),
    ("false", "False", True),
    (" True ", "true", True),
    ("False", "true", False),
    ("1", "false", False),
    ("0", "true", False),
])
@pytest.mark.parametrize("operator", ["Is", "Equal"])
def test_boolean_equality_accepts_case_variants_and_digits(operator, actual, expected, result):
    assert check(operator, actual, expected, "Boolean") is result
    # Boolean comparison ignores letter case whatever the text setting says
    assert check(operator, actual, expected, "Boolean", case_insensitive=True) is result


def test_text_equality_follows_the_case_setting():
    assert check("Equal", "Email", "Email") is True
    assert check("Equal", "EMAIL", "Email") is False
    assert check("Equal", "EMAIL", "Email", case_insensitive=True) is True
    assert check("Is", "Email", "EMAIL", case_insensitive=True) is True
    assert check("Equal", "Email ", "Email") is False


@pytest.mark.parametrize("field_type", ["EmailAddress", "Phone", "Locale", "Geolocation", "", None])
def test_other_types_compare_as_text(field_type):
    assert check("Equal", "A@Example.com", "A@Example.com", field_type) is True
    assert check("Equal", "a@example.com", "A@Example.com", field_type) is False
    assert check("Equal", "a@example.com", "A@Example.com", field_type, case_insensitive=True) is True


def test_not_equal():
    assert check("NotEqual", "SMS", "Email") is True
    assert check("NotEqual", "Email", "Email") is False
    assert check("NotEqual", "EMAIL", "Email") is True
    assert check("NotEqual", "EMAIL", "Email", case_insensitive=True) is False
    assert check("NotEqual", "1", "true", "Boolean") is False


def test_number_comparisons_are_numeric():
    assert check("Equal", "5", "5.0", "Number") is True
    assert check("Equal", "5", "6", "Decimal") is False
    assert check("GreaterThan", "10", "9", "Number") is True       # as text "10" < "9"
    assert check("GreaterThan", "9", "9", "Number") is False
    assert check("LessThan", "9", "10", "Decimal") is True
    assert check("GreaterThanOrEqual", "9.0", "9", "Number") is True
    assert check("LessThanOrEqual", "8", "9", "Number") is True
    assert check("LessThanOrEqual", "10", "9", "Number") is False


def test_number_that_does_not_parse():
    assert check("GreaterThan", "abc", "9", "Number") is False
    assert check("LessThan", "abc", "9", "Number") is False
    assert check("Equal", "abc", "abc", "Number") is True
    assert check("Equal", "abc", "9", "Number") is False
    assert check("NotEqual", "abc", "9", "Number") is True


def test_date_only_condition_value_compares_the_date_part():
    assert check("Equal", "2026-10-11 10:30", "2026-10-11", "Date") is True
    assert check("Equal", "2026-10-12 00:00", "2026-10-11", "Date") is False
    assert check("GreaterThan", "2026-10-11 23:59", "2026-10-11", "Date") is False
    assert check("GreaterThan", "2026-10-12 00:00", "2026-10-11", "Date") is True
    assert check("GreaterThanOrEqual", "2026-10-11 08:00", "2026-10-11", "Date") is True
    assert check("LessThan", "2026-10-10 23:59", "2026-10-11", "Date") is True
    assert check("LessThanOrEqual", "2026-10-11 23:59", "2026-10-11", "Date") is True


def test_date_with_time_compares_the_time():
    assert check("Equal", "2026-10-11 10:30", "2026-10-11 10:00", "Date") is False
    assert check("Equal", "2026-10-11T10:30:00", "2026-10-11 10:30", "Date") is True
    assert check("GreaterThan", "2026-10-11 10:30", "2026-10-11 10:00", "Date") is True
    assert check("LessThan", "2026-10-11", "2026-10-11 10:00", "Date") is True
    assert check("Equal", "2026-10-11T10:30:00+02:00", "2026-10-11 08:30", "Date") is True


def test_date_that_does_not_parse():
    assert check("GreaterThan", "11/10/2026", "2026-10-11", "Date") is False
    assert check("Equal", "11/10/2026", "2026-10-11", "Date") is False
    assert check("Equal", "11/10/2026", "11/10/2026", "Date") is True


def test_text_ordering_compares_strings():
    assert check("GreaterThan", "b", "a") is True
    assert check("LessThan", "B", "a") is True
    assert check("LessThan", "B", "a", case_insensitive=True) is False


def test_contains_begins_with_ends_with():
    assert check("Contains", "NoClick_OB", "Click") is True
    assert check("Contains", "NoClick_OB", "click") is False
    assert check("Contains", "NoClick_OB", "click", case_insensitive=True) is True
    assert check("BeginsWith", "Open_Click_OB", "Open") is True
    assert check("BeginsWith", "Open_Click_OB", "open") is False
    assert check("BeginsWith", "Open_Click_OB", "open", case_insensitive=True) is True
    assert check("EndsWith", "Open_Click_OB", "_OB") is True
    assert check("EndsWith", "Open_Click_OB", "_ob") is False
    assert check("EndsWith", "Open_Click_OB", "_ob", case_insensitive=True) is True


@pytest.mark.parametrize("actual, result", [(None, True), ("", True), ("x", False), (" ", False), ("False", False)])
def test_is_null_and_is_not_null(actual, result):
    assert check("IsNull", actual, None) is result
    assert check("IsNotNull", actual, None) is (not result)


@pytest.mark.parametrize("operator", [
    "Is", "Equal", "NotEqual", "GreaterThan", "LessThan", "GreaterThanOrEqual", "LessThanOrEqual",
    "Contains", "BeginsWith", "EndsWith", "Between",
    "After", "AtOrAfter", "Before", "AtOrBefore",
])
@pytest.mark.parametrize("actual", [None, ""])
def test_any_other_operator_on_null_is_false(operator, actual):
    assert check(operator, actual, "x") is False
    assert check(operator, actual, "false", "Boolean") is False


def test_condition_without_value_is_false_for_a_value_operator():
    assert evaluate_condition(Condition("T", "f", "Equal", None), "x", "Text", False) is False


def test_null_operators_ignore_the_condition_value():
    assert evaluate_condition(Condition("T", "f", "IsNull", "ignored"), None, "Text", False) is True


@pytest.mark.parametrize("operator", ["NotContains", "equal", "IsIn", ""])
def test_unknown_operator_raises_unsupported_operator(operator):
    with pytest.raises(UnsupportedOperator):
        evaluate_condition(Condition("T", "f", operator, "x"), "x", "Text", False)


# ---------------------------------------------------------------------------
# Extensions beyond the pilot: Journey Data, attribute comparisons, Between
# ---------------------------------------------------------------------------

def journey_condition_xml(key, operator, value=""):
    """A Journey Data condition as the real exports write it: ephemeral attribute, no AttributePath."""
    return (f'<Condition IsEphemeralAttribute="true" Key="{key}" Operator="{operator}" UiMetaData="{{}}">'
            f"<Value><![CDATA[{value}]]></Value></Condition>")


def test_journey_data_key_names_the_event_and_the_field():
    xml = filter_xml(journey_condition_xml("Event.DEAudience-1.Score", "GreaterThan", "15"))
    assert parse_criteria(xml) == Condition("DEAudience-1", "Score", "GreaterThan", "15",
                                            source=SOURCE_JOURNEY)


def test_journey_data_key_without_the_ephemeral_attribute_is_journey_data():
    xml = filter_xml(condition_xml("Event.APIEvent-1.Field With Spaces", "IsNull"))
    condition = parse_criteria(xml)
    assert (condition.source, condition.table, condition.field) == (SOURCE_JOURNEY, "APIEvent-1", "Field With Spaces")


def test_contact_data_key_keeps_the_contact_source():
    condition = parse_criteria(filter_xml(condition_xml("Optin.SubscriberKey", "IsNull")))
    assert (condition.source, condition.table, condition.value_attribute) == (SOURCE_CONTACT, "Optin", None)


def test_ephemeral_attribute_on_a_key_that_is_not_journey_data_is_rejected():
    xml = filter_xml('<Condition IsEphemeralAttribute="true" Key="Master.Perm" Operator="Is">'
                     "<Value><![CDATA[true]]></Value></Condition>")
    with pytest.raises(CriteriaError, match="Journey Data"):
        parse_criteria(xml)


def test_value_parameter_name_is_an_attribute_not_an_empty_value():
    xml = filter_xml('<Condition Key="Optin.SubscriberKey" Operator="Equal" UiMetaData="{}" '
                     'ValueParameterName="Event.CloudPagesSma-7.SubscriberKey"><Value><![CDATA[]]></Value></Condition>')
    condition = parse_criteria(xml)
    assert condition == Condition("Optin", "SubscriberKey", "Equal", None, source=SOURCE_CONTACT,
                                  value_attribute="Event.CloudPagesSma-7.SubscriberKey")
    assert formula_text(condition) == "Optin.SubscriberKey Equal Event.CloudPagesSma-7.SubscriberKey"


def test_value_is_reference_takes_the_attribute_from_the_value():
    xml = filter_xml('<Condition IsEphemeralAttribute="true" Key="Event.DEAudience-1.SubscriberKey" Operator="Equal" '
                     'ValueIsReference="true"><Value><![CDATA[Log Set.SubscriberKey]]></Value></Condition>')
    condition = parse_criteria(xml)
    assert (condition.source, condition.table, condition.field) == (SOURCE_JOURNEY, "DEAudience-1", "SubscriberKey")
    assert (condition.value, condition.value_attribute) == (None, "Log Set.SubscriberKey")


@pytest.mark.parametrize("xml", [
    # an attribute and a literal at the same time
    '<Condition Key="A.x" Operator="Equal" ValueParameterName="Event.E.y"><Value><![CDATA[1]]></Value></Condition>',
    # a reference without the attribute it refers to
    '<Condition Key="A.x" Operator="Equal" ValueIsReference="true"><Value><![CDATA[]]></Value></Condition>',
])
def test_contradictory_attribute_comparisons_are_rejected(xml):
    with pytest.raises(CriteriaError):
        parse_criteria(filter_xml(xml))


@pytest.mark.parametrize("text, parts", [
    ("Event.DEAudience-1.Score", (SOURCE_JOURNEY, "DEAudience-1", "Score")),
    ("Config Set.Control Group", (SOURCE_CONTACT, "Config Set", "Control Group")),
    ("Event.Field", (SOURCE_CONTACT, "Event", "Field")),     # no event key: read as a set called Event
    ("NoDot", None),
    (".x", None),
])
def test_split_attribute(text, parts):
    assert split_attribute(text) == parts


@pytest.mark.parametrize("value, bounds", [
    ("10|20", ("10", "20")),
    (" 8 | 15 ", ("8", "15")),
    ("10000", None),
    ("1|2|3", None),
    ("|5", None),
    (None, None),
])
def test_between_bounds(value, bounds):
    assert between_bounds(value) == bounds


@pytest.mark.parametrize("actual, result", [
    ("100", True), ("100.0000", True), ("250.5", True), ("300", True),     # both bounds included (confirmed)
    ("99.9999", False), ("300.0001", False), ("-1", False), ("abc", False),
])
@pytest.mark.parametrize("field_type", ["Number", "Decimal"])
def test_between_includes_both_bounds(field_type, actual, result):
    assert check("Between", actual, "100|300", field_type) is result


def test_between_on_dates_and_text():
    assert check("Between", "2026-10-11 10:00", "2026-10-11|2026-10-12", "Date") is True
    assert check("Between", "2026-10-13 00:00", "2026-10-11|2026-10-12", "Date") is False
    assert check("Between", "b", "a|c") is True
    assert check("Between", "d", "a|c") is False
    assert check("Between", "5", "10|1", "Number") is False      # reversed bounds match nothing


def test_between_value_not_written_low_high_cannot_be_evaluated():
    with pytest.raises(UnsupportedOperator, match="basso\\|alto"):
        check("Between", "5", "10000", "Number")


@pytest.mark.parametrize("actual, result", [("TST_R01_C0001", True), ("x", True), (None, False), ("", False)])
def test_attribute_comparison_reads_whether_the_row_is_there(actual, result):
    condition = Condition("Optin", "SubscriberKey", "Equal", None, value_attribute="Event.E.SubscriberKey")
    assert evaluate_condition(condition, actual, "Text", False) is result


def test_attribute_comparison_with_another_operator_cannot_be_evaluated():
    condition = Condition("Optin", "SubscriberKey", "NotEqual", None, value_attribute="Event.E.SubscriberKey")
    with pytest.raises(UnsupportedOperator):
        evaluate_condition(condition, "x", "Text", False)


# ---------------------------------------------------------------------------
# Step 3: relative date values (spec, "Relative dates in conditions")
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 14, 10, 30)     # the clock: Wednesday 14 October 2026, 10:30


@pytest.mark.parametrize("text, relative", [
    (";+;0;days", RelativeDate(1, 0, "days")),          # "Today"
    (";-;7;days", RelativeDate(-1, 7, "days")),         # "Today Minus 7 days"
    (";+;90;days", RelativeDate(1, 90, "days")),        # "Today Plus 90 days"
    (";+;2;weeks", RelativeDate(1, 2, "weeks")),
    (";-;1;months", RelativeDate(-1, 1, "months")),
    (";+;1;years", RelativeDate(1, 1, "years")),
    (";-;3;hours", RelativeDate(-1, 3, "hours")),
    (";+;30;minutes", RelativeDate(1, 30, "minutes")),
    (";+;1;Days", RelativeDate(1, 1, "days")),          # unit lower-cased
    (";-;007;WEEKS", RelativeDate(-1, 7, "weeks")),
    (";+;1;fortnights", RelativeDate(1, 1, "fortnights")),   # kept: the evaluation reports the unit
])
def test_relative_date_values(text, relative):
    assert relative_date(text) == relative


@pytest.mark.parametrize("text", [
    "", "today", "2026-10-14", "+;0;days", ";0;days", ";+;0;days;", ";*;1;days", ";+;-1;days", ";+;1.5;days",
    ";+;;days", ";+;1;", ";+;1;days ", " ;+;1;days", ";+;1;days\n", "; +;1;days", ";+;1;da ys", ";+;1;2days",
    "10|20",
])
def test_other_values_are_not_relative_dates(text):
    assert relative_date(text) is None


def test_relative_date_of_no_value():
    assert relative_date(None) is None


def test_parsed_relative_value_keeps_the_text_as_written():
    condition = parse_criteria(filter_xml(condition_xml("Holiday.PHStart", "After", ";+;0;Days")))
    assert condition == Condition("Holiday", "PHStart", "After", ";+;0;Days", relative=RelativeDate(1, 0, "days"),
                                  path=ROOT)
    condition = parse_criteria(filter_xml(journey_condition_xml("Event.DEAudience-1.EndDate", "AtOrBefore",
                                                                ";-;13;days")))
    assert (condition.source, condition.value, condition.relative) == (SOURCE_JOURNEY, ";-;13;days",
                                                                      RelativeDate(-1, 13, "days"))


@pytest.mark.parametrize("text", ["2026-10-14", ";+;1;days ", "+;0;days", ";+;x;days"])
def test_parsed_bad_relative_form_stays_a_plain_value(text):
    condition = parse_criteria(filter_xml(condition_xml("A.d", "After", text)))
    assert (condition.value, condition.relative) == (text, None)


@pytest.mark.parametrize("operator", ["Equal", "Is", "IsNot", "NotEqual", "GreaterThan", "Contains", "Unknown"])
def test_any_operator_may_carry_a_relative_value_when_parsed(operator):
    # loader.py decides whether the field type allows it; the evaluation accepts it only with RELATIVE_OPERATORS
    condition = parse_criteria(filter_xml(condition_xml("A.d", operator, ";+;0;days")))
    assert condition.relative == RelativeDate(1, 0, "days")


def test_operator_without_value_never_carries_a_relative_value():
    condition = parse_criteria(filter_xml(condition_xml("A.d", "IsNull", ";+;0;days")))
    assert (condition.value, condition.relative) == (None, None)


def relative_check(operator, actual, text, field_type="Date", now=NOW):
    """Evaluate ``operator`` with the relative value ``text`` on a field holding ``actual``."""
    condition = Condition("T", "d", operator, text, relative=relative_date(text))
    return evaluate_condition(condition, actual, field_type, False, now=now)


@pytest.mark.parametrize("operator, actual, text, result", [
    # "Today" (;+;0;days): day level, the time of the field and of the clock do not count
    ("After", "2026-10-15 00:00", ";+;0;days", True),
    ("After", "2026-10-14 23:59", ";+;0;days", False),
    ("After", "2026-10-13", ";+;0;days", False),
    ("AtOrAfter", "2026-10-14 00:00", ";+;0;days", True),       # both bounds inclusive
    ("AtOrAfter", "2026-10-14", ";+;0;days", True),
    ("AtOrAfter", "2026-10-13 23:59", ";+;0;days", False),
    ("Before", "2026-10-13 23:59", ";+;0;days", True),
    ("Before", "2026-10-14 00:00", ";+;0;days", False),
    ("AtOrBefore", "2026-10-14 23:59", ";+;0;days", True),
    ("AtOrBefore", "2026-10-15 00:00", ";+;0;days", False),
    # "Today Minus 7 days" = 7 October; "Today Plus 90 days" = 12 January 2027
    ("Before", "2026-10-06 23:59", ";-;7;days", True),
    ("Before", "2026-10-07 00:00", ";-;7;days", False),
    ("AtOrAfter", "2026-10-07 00:00", ";-;7;days", True),
    ("AtOrBefore", "2027-01-12 23:59", ";+;90;days", True),
    ("AtOrBefore", "2027-01-13 00:00", ";+;90;days", False),
    ("After", "2027-01-13", ";+;90;days", True),
    # weeks, months, years: still day level
    ("AtOrAfter", "2026-10-28 00:00", ";+;2;weeks", True),
    ("AtOrAfter", "2026-10-27 23:59", ";+;2;weeks", False),
    ("After", "2026-11-15", ";+;1;months", True),
    ("After", "2026-11-14 23:59", ";+;1;months", False),
    ("Before", "2025-10-13", ";-;1;years", True),
    ("Before", "2025-10-14 00:00", ";-;1;years", False),
])
def test_relative_date_at_day_level(operator, actual, text, result):
    assert relative_check(operator, actual, text) is result


@pytest.mark.parametrize("operator, actual, text, result", [
    # ;+;2;hours = 14 October 12:30: the full date and time are compared ("Now")
    ("After", "2026-10-14 12:31", ";+;2;hours", True),
    ("After", "2026-10-14 12:30", ";+;2;hours", False),
    ("AtOrAfter", "2026-10-14 12:30", ";+;2;hours", True),
    ("AtOrAfter", "2026-10-14 12:29", ";+;2;hours", False),
    ("Before", "2026-10-14 12:29:59", ";+;2;hours", True),
    ("AtOrBefore", "2026-10-14 12:30:00", ";+;2;hours", True),
    ("AtOrBefore", "2026-10-14 12:30:01", ";+;2;hours", False),
    # ;-;30;minutes = 14 October 10:00
    ("Before", "2026-10-14 09:59", ";-;30;minutes", True),
    ("Before", "2026-10-14 10:00", ";-;30;minutes", False),
    ("AtOrAfter", "2026-10-14", ";-;30;minutes", False),        # a date alone is midnight
    ("Before", "2026-10-14", ";+;0;minutes", True),
])
def test_relative_date_at_datetime_level(operator, actual, text, result):
    assert relative_check(operator, actual, text) is result


def test_day_level_and_datetime_level_read_the_same_value_differently():
    # 08:00 on the clock's day: on or after "Today", but before "Now" (10:30)
    assert relative_check("AtOrAfter", "2026-10-14 08:00", ";+;0;days") is True
    assert relative_check("AtOrAfter", "2026-10-14 08:00", ";+;0;minutes") is False
    assert relative_check("AtOrAfter", "2026-10-14 08:00", ";+;0;hours") is False
    assert relative_check("Before", "2026-10-14 08:00", ";+;0;days") is False
    assert relative_check("Before", "2026-10-14 08:00", ";+;0;hours") is True


def test_relative_date_moves_with_the_clock():
    assert relative_check("After", "2026-10-20", ";+;0;days", now=datetime(2026, 10, 19, 9, 0)) is True
    assert relative_check("After", "2026-10-20", ";+;0;days", now=datetime(2026, 10, 20, 9, 0)) is False


@pytest.mark.parametrize("operator", sorted(RELATIVE_OPERATORS))
@pytest.mark.parametrize("actual", [None, ""])
def test_relative_date_on_null_is_false(operator, actual):
    assert relative_check(operator, actual, ";+;0;days") is False


@pytest.mark.parametrize("operator", sorted(RELATIVE_OPERATORS))
@pytest.mark.parametrize("actual", ["14/10/2026", "abc", "2026-13-01", "true"])
def test_relative_date_on_a_value_that_is_not_a_date_is_false(operator, actual):
    assert relative_check(operator, actual, ";+;0;days") is False
    assert relative_check(operator, actual, ";+;1;hours") is False


def test_relative_date_compares_dates_whatever_the_field_type():
    # loader.py rejects a relative value on a field that is not a Date; here the comparison is on dates anyway
    # (as text "2026-10-14 23:00" would sort after "2026-10-14")
    assert relative_check("After", "2026-10-14 23:00", ";+;0;days", field_type="Text") is False
    assert relative_check("After", "2026-10-15", ";+;0;days", field_type="Text") is True


@pytest.mark.parametrize("actual", ["2026-10-14", None])
def test_relative_date_without_the_clock_cannot_be_evaluated(actual):
    with pytest.raises(UnsupportedOperator, match="orologio"):
        relative_check("After", actual, ";+;0;days", now=None)


@pytest.mark.parametrize("actual", ["2026-10-14", None])
def test_relative_date_with_an_unknown_unit_cannot_be_evaluated(actual):
    with pytest.raises(UnsupportedOperator, match="fortnights"):
        relative_check("After", actual, ";+;1;fortnights")


@pytest.mark.parametrize("operator", ["Is", "Equal", "IsNot", "NotEqual", "GreaterThan", "LessThanOrEqual",
                                      "Contains", "BeginsWith"])
def test_relative_date_with_another_operator_cannot_be_evaluated(operator):
    # Spec, step 3: "The operators that take such a value are After, AtOrAfter, Before, AtOrBefore"; "Any
    # other value form, unit or operator is unsupported and reported as before."
    with pytest.raises(UnsupportedOperator, match="data relativa"):
        relative_check(operator, "2026-10-14", ";+;0;days")


def test_the_clock_is_ignored_by_an_absolute_value():
    condition = Condition("T", "d", "After", "2026-10-11")
    assert evaluate_condition(condition, "2026-10-12", "Date", False, now=NOW) is True
    assert evaluate_condition(condition, "2026-10-12", "Date", False) is True
    assert evaluate_condition(Condition("T", "d", "Equal", "x"), "x", "Text", False, now=NOW) is True


# ---------------------------------------------------------------------------
# Step 3: the date operators and IsNot with absolute values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("operator, actual, result", [
    # a condition date without a time compares on the date part, as GreaterThan & co.
    ("After", "2026-10-12 00:00", True),
    ("After", "2026-10-11 23:59", False),
    ("AtOrAfter", "2026-10-11 00:00", True),
    ("AtOrAfter", "2026-10-10 23:59", False),
    ("Before", "2026-10-10 23:59", True),
    ("Before", "2026-10-11 00:00", False),
    ("AtOrBefore", "2026-10-11 23:59", True),
    ("AtOrBefore", "2026-10-12 00:00", False),
    ("After", "11/10/2026", False),        # not a date
])
def test_date_operators_with_an_absolute_date(operator, actual, result):
    assert check(operator, actual, "2026-10-11", "Date") is result


def test_date_operators_with_an_absolute_date_and_time():
    assert check("After", "2026-10-11 10:01", "2026-10-11 10:00", "Date") is True
    assert check("After", "2026-10-11 10:00", "2026-10-11 10:00", "Date") is False
    assert check("AtOrAfter", "2026-10-11 10:00", "2026-10-11 10:00", "Date") is True
    assert check("Before", "2026-10-11 09:59", "2026-10-11 10:00", "Date") is True
    assert check("AtOrBefore", "2026-10-11 10:00:01", "2026-10-11 10:00", "Date") is False


@pytest.mark.parametrize("operator, same_as", [
    ("After", "GreaterThan"), ("AtOrAfter", "GreaterThanOrEqual"),
    ("Before", "LessThan"), ("AtOrBefore", "LessThanOrEqual"),
])
@pytest.mark.parametrize("actual, expected, field_type", [
    ("10", "9", "Number"), ("9", "9", "Decimal"), ("8.5", "9", "Number"), ("b", "a", "Text"), ("abc", "9", "Number"),
])
def test_date_operators_read_as_the_ordering_operators(operator, same_as, actual, expected, field_type):
    assert check(operator, actual, expected, field_type) is check(same_as, actual, expected, field_type)


@pytest.mark.parametrize("actual, result", [
    ("false", True), ("False", True), ("0", True),
    (None, True), ("", True),                         # spec, step 3: "IsNot true is true for false and for null"
    ("true", False), ("True", False), ("1", False),
])
def test_is_not_on_a_boolean(actual, result):
    assert check("IsNot", actual, "true", "Boolean") is result


def test_is_not_on_text_follows_the_case_setting():
    assert check("IsNot", "SMS", "Email") is True
    assert check("IsNot", "Email", "Email") is False
    assert check("IsNot", "EMAIL", "Email") is True
    assert check("IsNot", "EMAIL", "Email", case_insensitive=True) is False
    assert check("IsNot", None, "Email") is True


def test_is_not_on_numbers_and_dates():
    assert check("IsNot", "5", "5.0", "Number") is False
    assert check("IsNot", "6", "5", "Number") is True
    assert check("IsNot", "2026-10-11 10:30", "2026-10-11", "Date") is False
    assert check("IsNot", "2026-10-12", "2026-10-11", "Date") is True
    assert check("IsNot", "", "2026-10-11", "Date") is True


@pytest.mark.parametrize("actual", [None, ""])
def test_not_equal_keeps_false_on_null_unlike_is_not(actual):
    # spec, step 3: "NotEqual keeps its earlier reading, since the step 2 example operators_text fixes it"
    assert check("NotEqual", actual, "true", "Boolean") is False
    assert check("IsNot", actual, "true", "Boolean") is True


def test_is_not_is_the_negation_of_is_on_values():
    for actual in ("true", "false", "1", "0", "x"):
        assert check("IsNot", actual, "true", "Boolean") is (not check("Is", actual, "true", "Boolean"))


def test_is_not_without_a_condition_value_is_false():
    assert evaluate_condition(Condition("T", "f", "IsNot", None), None, "Text", False) is False


def test_is_not_needs_a_value_when_parsed():
    with pytest.raises(CriteriaError):
        parse_criteria(filter_xml(condition_xml("A.x", "IsNot", "", with_value=False)))
    assert parse_criteria(filter_xml(condition_xml("A.x", "IsNot", "true"))).value == "true"


# ---------------------------------------------------------------------------
# Step 3: shift_date, relative_units, is_day_level
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("moment, rel, shifted", [
    (NOW, RelativeDate(1, 0, "days"), NOW),
    (NOW, RelativeDate(1, 1, "days"), datetime(2026, 10, 15, 10, 30)),
    (NOW, RelativeDate(-1, 7, "days"), datetime(2026, 10, 7, 10, 30)),
    (NOW, RelativeDate(1, 90, "days"), datetime(2027, 1, 12, 10, 30)),
    (NOW, RelativeDate(1, 2, "weeks"), datetime(2026, 10, 28, 10, 30)),
    (NOW, RelativeDate(-1, 1, "weeks"), datetime(2026, 10, 7, 10, 30)),
    (NOW, RelativeDate(1, 3, "hours"), datetime(2026, 10, 14, 13, 30)),
    (NOW, RelativeDate(1, 14, "hours"), datetime(2026, 10, 15, 0, 30)),
    (NOW, RelativeDate(-1, 90, "minutes"), datetime(2026, 10, 14, 9, 0)),
    (NOW, RelativeDate(1, 1, "months"), datetime(2026, 11, 14, 10, 30)),
    (NOW, RelativeDate(-1, 1, "months"), datetime(2026, 9, 14, 10, 30)),
    (NOW, RelativeDate(1, 3, "months"), datetime(2027, 1, 14, 10, 30)),
    (NOW, RelativeDate(-1, 10, "months"), datetime(2025, 12, 14, 10, 30)),
    (NOW, RelativeDate(1, 1, "years"), datetime(2027, 10, 14, 10, 30)),
    (NOW, RelativeDate(-1, 2, "years"), datetime(2024, 10, 14, 10, 30)),
    (datetime(2026, 12, 15), RelativeDate(1, 1, "months"), datetime(2027, 1, 15)),
    (datetime(2026, 1, 15), RelativeDate(-1, 1, "months"), datetime(2025, 12, 15)),
    (datetime(2026, 12, 31, 23, 59), RelativeDate(1, 2, "minutes"), datetime(2027, 1, 1, 0, 1)),
])
def test_shift_date(moment, rel, shifted):
    assert shift_date(moment, rel) == shifted


@pytest.mark.parametrize("moment, rel, shifted", [
    (datetime(2026, 1, 31, 10, 30), RelativeDate(1, 1, "months"), datetime(2026, 2, 28, 10, 30)),
    (datetime(2028, 1, 31), RelativeDate(1, 1, "months"), datetime(2028, 2, 29)),         # leap year
    (datetime(2026, 3, 31), RelativeDate(-1, 1, "months"), datetime(2026, 2, 28)),
    (datetime(2026, 5, 31), RelativeDate(1, 1, "months"), datetime(2026, 6, 30)),
    (datetime(2026, 8, 31), RelativeDate(1, 18, "months"), datetime(2028, 2, 29)),
    (datetime(2026, 12, 31), RelativeDate(-1, 1, "months"), datetime(2026, 11, 30)),
    (datetime(2028, 2, 29), RelativeDate(1, 1, "years"), datetime(2029, 2, 28)),
    (datetime(2028, 2, 29), RelativeDate(-1, 4, "years"), datetime(2024, 2, 29)),
    (datetime(2026, 2, 28), RelativeDate(1, 1, "months"), datetime(2026, 3, 28)),         # no clamp needed
])
def test_shift_date_clamps_the_day_to_the_end_of_the_month(moment, rel, shifted):
    assert shift_date(moment, rel) == shifted


def test_shift_date_with_a_negative_amount_moves_back():
    # a wait by attribute "13 days before" the date: an offset of -13 days (spec, step 3, "The clock")
    assert shift_date(NOW, RelativeDate(1, -13, "days")) == datetime(2026, 10, 1, 10, 30)
    assert shift_date(NOW, RelativeDate(-1, -1, "months")) == datetime(2026, 11, 14, 10, 30)


def test_shift_date_accepts_the_unit_in_any_letter_case():
    assert shift_date(NOW, RelativeDate(1, 1, "DAYS")) == datetime(2026, 10, 15, 10, 30)
    assert shift_date(NOW, RelativeDate(1, 1, "Months")) == datetime(2026, 11, 14, 10, 30)


@pytest.mark.parametrize("unit", ["fortnights", "seconds", "day", ""])
def test_shift_date_with_an_unknown_unit_raises(unit):
    with pytest.raises(UnsupportedOperator):
        shift_date(NOW, RelativeDate(1, 1, unit))


@pytest.mark.parametrize("moment, rel", [
    (datetime(9999, 12, 31), RelativeDate(1, 1, "days")),
    (datetime(9999, 12, 31), RelativeDate(1, 1, "months")),
    (datetime(9999, 12, 31), RelativeDate(1, 1, "years")),
    (datetime(1, 1, 1), RelativeDate(-1, 1, "minutes")),
    (datetime(1, 1, 1), RelativeDate(-1, 1, "months")),
    (NOW, RelativeDate(1, 10 ** 9, "days")),
])
def test_shift_date_outside_the_calendar_raises(moment, rel):
    with pytest.raises(UnsupportedOperator, match="calendario"):
        shift_date(moment, rel)


def test_relative_units():
    assert relative_units() == ("days", "weeks", "months", "years", "hours", "minutes")
    assert all(relative_date(f";+;1;{unit}").unit == unit for unit in relative_units())


@pytest.mark.parametrize("unit, day_level", [
    ("days", True), ("weeks", True), ("months", True), ("years", True), ("hours", False), ("minutes", False),
])
def test_is_day_level(unit, day_level):
    assert is_day_level(RelativeDate(1, 1, unit)) is day_level


def test_is_day_level_with_an_unknown_unit_raises():
    with pytest.raises(UnsupportedOperator):
        is_day_level(RelativeDate(1, 1, "fortnights"))


# ---------------------------------------------------------------------------
# Step 3: relationship ids of the attribute path (spec, "Tables reached through other tables")
# ---------------------------------------------------------------------------

def test_path_holds_the_group_to_set_id_then_the_attribute_paths_in_document_order():
    # The pilot's Config condition, references resolved: contact -> Master (UiMetaData), Master -> Entry,
    # Entry -> Config (AttributePath, outermost first).
    xml = filter_xml(
        '<Condition Key="DE_NCRM_AD_HOC_CONFIG.Control Group Required" Operator="Is" '
        'UiMetaData="{&quot;groupToSetRelationshipId&quot;:&quot;2818d112&quot;}">'
        '<AttributePath RelationshipID="a3d83a75"><AttributePath RelationshipID="3aaa3987">'
        "<Value><![CDATA[true]]></Value></AttributePath></AttributePath></Condition>")
    assert parse_criteria(xml).path == ("2818d112", "a3d83a75", "3aaa3987")


def test_path_with_only_the_group_to_set_id():
    xml = filter_xml('<Condition Key="Master.IsMailOffers" Operator="Is" '
                     'UiMetaData="{&quot;groupToSetRelationshipId&quot;:&quot;2818d112&quot;}">'
                     "<Value><![CDATA[true]]></Value></Condition>")
    assert parse_criteria(xml).path == ("2818d112",)


def test_path_with_only_attribute_paths():
    xml = filter_xml('<Condition Key="A.x" Operator="Is"><AttributePath RelationshipID=" r1 ">'
                     '<AttributePath RelationshipID="r2"><Value><![CDATA[true]]></Value></AttributePath>'
                     "</AttributePath></Condition>")
    assert parse_criteria(xml).path == ("r1", "r2")


@pytest.mark.parametrize("ui_meta_data", [None, "", "{}"])
def test_no_path_without_ui_metadata_or_attribute_path(ui_meta_data):
    meta = "" if ui_meta_data is None else f' UiMetaData="{ui_meta_data}"'
    xml = filter_xml(f'<Condition Key="A.x" Operator="Is"{meta}><Value><![CDATA[true]]></Value></Condition>')
    assert parse_criteria(xml).path == ()


def test_journey_data_condition_has_no_path():
    condition = parse_criteria(filter_xml(journey_condition_xml("Event.DEAudience-1.Score", "Equal", "1")))
    assert condition.path == ()


@pytest.mark.parametrize("ui_meta_data", [
    "not json",
    "{&quot;groupToSetRelationshipId&quot;:",                      # truncated
    "[&quot;rel&quot;]",                                            # not an object
    "{&quot;groupToSetRelationshipId&quot;:5}",
    "{&quot;groupToSetRelationshipId&quot;:&quot; &quot;}",
    "{&quot;groupToSetRelationshipId&quot;:null}",
    "{&quot;id&quot;:&quot;r-9&quot;}",                             # another key
])
def test_unreadable_ui_metadata_is_ignored(ui_meta_data):
    xml = filter_xml(f'<Condition Key="A.x" Operator="Is" UiMetaData="{ui_meta_data}">'
                     '<AttributePath RelationshipID="r1"><Value><![CDATA[true]]></Value></AttributePath></Condition>')
    assert parse_criteria(xml).path == ("r1",)


def test_ui_metadata_escaped_twice_is_read():
    xml = filter_xml('<Condition Key="A.x" Operator="Is" '
                     'UiMetaData="{&amp;quot;groupToSetRelationshipId&amp;quot;:&amp;quot;g1&amp;quot;}">'
                     "<Value><![CDATA[true]]></Value></Condition>")
    assert parse_criteria(xml).path == ("g1",)


def test_attribute_path_without_a_relationship_id_is_skipped():
    xml = filter_xml('<Condition Key="A.x" Operator="Is" '
                     'UiMetaData="{&quot;groupToSetRelationshipId&quot;:&quot;g&quot;}">'
                     '<AttributePath><AttributePath RelationshipID=""><AttributePath RelationshipID="r3">'
                     "<Value><![CDATA[true]]></Value></AttributePath></AttributePath></AttributePath></Condition>")
    assert parse_criteria(xml).path == ("g", "r3")


def test_path_is_kept_for_every_condition_form():
    null_test = parse_criteria(filter_xml(condition_xml("A.x", "IsNull", "", wrappers=1)))
    assert (null_test.value, null_test.path) == (None, ("rel-root", "rel-0"))
    attribute = parse_criteria(filter_xml(
        '<Condition Key="Optin.SubscriberKey" Operator="Equal" '
        'UiMetaData="{&quot;groupToSetRelationshipId&quot;:&quot;g7&quot;}" '
        'ValueParameterName="Event.CloudPagesSma-7.SubscriberKey"><Value><![CDATA[]]></Value></Condition>'))
    assert (attribute.value_attribute, attribute.path) == ("Event.CloudPagesSma-7.SubscriberKey", ("g7",))
    relative = parse_criteria(filter_xml(condition_xml("Holiday.PHEnd", "Before", ";-;1;days", wrappers=2)))
    assert (relative.relative, relative.path) == (RelativeDate(-1, 1, "days"), ("rel-root", "rel-0", "rel-1"))


def test_the_path_does_not_change_the_evaluation():
    with_path = Condition("T", "f", "Equal", "x", path=("g", "r1"))
    assert evaluate_condition(with_path, "x", "Text", False) is True
