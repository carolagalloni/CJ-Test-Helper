"""Builders for the examples of the spec section "Extensions for time-dependent journeys (step 3, 14 October 2026)".

package.json only; expected.json and README.md are written by hand from docs/spec.md (never from the generator).
Reuses the helpers of examples/build_examples.py and examples/build_extension_examples.py, so every package keeps
the pilot's shape, and adds the raw shapes step 3 introduces (as described for the real exports, names invented):

- relative date values in conditions: <Value><![CDATA[;+;13;days]]></Value> (also ;-;7;days), with the operators
  After, AtOrAfter, Before, AtOrBefore, and IsNot;
- a wait by attribute with an offset: configurationArguments.waitDuration -13, waitUnit "DAYS", specifiedTime "",
  waitEndDateAttributeExpression "{{Contact.Attribute.<Set>."<Field>"}}";
- the exit criteria of a journey: journey.exits = [{"key": "EXITCRITERIA", "type": "ExitCriteria", "outcomes": [],
  "arguments": {"filterResult": ...}, "configurationArguments": {"schemaVersionId": 257, "criteria": <XML>},
  "metaData": {"criteriaDescription": ..., "chainType": "none"}}];
- an SMSSYNC send and the journey default defaults.mobileNumber (the inner shape of SMSSYNC is invented: no real
  activity was available; only `type`, `key`, `name`, `outcomes` and configurationArguments.assetId are relied on).

Run:  python examples/build_step3_time_examples.py [name ...]
(writes examples/<name>/package.json, then validates package and expected.json like build_examples.py, plus the
step 3 shapes: relative values, wait offsets, exit criteria and their targets in expected.json, the mobile default)
"""
from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLES_DIR))
import build_examples as B  # noqa: E402
import build_extension_examples as X  # noqa: E402
from build_examples import (Journey, Package, add_email_asset, add_entry_source, add_journey, boolean,  # noqa: E402
                            criteria, cset, date, email_field, export, phone, ref, sql_master_only, table_sql, text)

RELATIVE_OPERATORS = ("After", "AtOrAfter", "Before", "AtOrBefore")
RELATIVE_RE = re.compile(r"^;[+-];\d+;(days|weeks|months|years|hours|minutes)$")


# ---------------------------------------------------------------------------
# Step 3 shapes
# ---------------------------------------------------------------------------

def rel(amount: int, unit: str = "days") -> str:
    """A relative date value as the real exports write it: ;+;13;days, ;-;7;days ("Today Plus 13 days")."""
    return ";%s;%d;%s" % ("-" if amount < 0 else "+", abs(amount), unit)


def criteria_or(*items: str) -> str:
    """Top-level OR group: FilterDefinition > ConditionSet OR > items."""
    return "<FilterDefinition>%s</FilterDefinition>" % cset("OR", *items)


def wait_days(j: Journey, key: str, nxt: str | None, amount: int) -> None:
    """Wait by duration in DAYS (shape of build_examples.Journey.wait_duration), with a readable name."""
    j.wait_duration(key, nxt, amount, "DAYS")
    j.activities[-1]["name"] = "%d day%s" % (amount, "" if amount == 1 else "s")


def wait_attribute_offset(j: Journey, key: str, table: str, field: str, nxt: str | None, offset: int,
                          unit: str, name: str) -> None:
    """Wait by attribute with an offset: "13 days before EndDate" is waitDuration -13, waitUnit DAYS.

    Starts from build_examples.Journey.wait_attribute (metaData.attribute, timeZone) and applies the step 3 shape:
    the expression names the attribute set and the field only, specifiedTime is empty.
    """
    j.wait_attribute(key, table, field, nxt)
    activity = j.activities[-1]
    expression = '{{Contact.Attribute.%s."%s"}}' % (table, field)
    activity["name"] = name
    activity["arguments"]["waitEndDateAttributeDataBound"] = expression
    activity["configurationArguments"].update({"waitDuration": offset, "waitUnit": unit, "specifiedTime": "",
                                               "waitEndDateAttributeExpression": expression})


def exit_criteria(pkg: Package, journey_name: str, xml: str, description: str) -> dict:
    """One element of journey.exits, in the shape of the real exports (filterResult invented in its details)."""
    return {"key": "EXITCRITERIA", "type": "ExitCriteria", "outcomes": [],
            "arguments": {"filterResult": "{{Contact.FilterId.%s}}" % pkg.gid("exit-filter", journey_name)},
            "configurationArguments": {"schemaVersionId": 257, "criteria": xml},
            "metaData": {"criteriaDescription": description, "chainType": "none"}}


def set_exits(pkg: Package, exits: list[dict]) -> None:
    pkg.entities["journeys"][pkg.journey_id]["data"]["exits"] = exits


def sms(j: Journey, key: str, name: str, asset_id: str, nxt: str | None) -> None:
    """SMSSYNC send (inner shape invented; the loader relies on type, key, name, outcomes and assetId)."""
    j.activities.append({
        "type": "SMSSYNC",
        "arguments": {"contactKey": "{{Contact.Key}}", "definitionId": "{{Context.DefinitionId}}",
                      "mobileNumber": "{{InteractionDefaults.MobileNumber}}"},
        "description": "", "id": j._uid(key), "key": key, "metaData": {"isConfigured": True, "category": "message"},
        "configurationArguments": {"applicationExtensionKey": "jb-sms-activity", "assetId": asset_id,
                                   "isTransactional": False},
        "name": name, "outcomes": [j._simple_outcome(key, nxt)], "schema": {"arguments": {}}})


def set_mobile_default(pkg: Package, event_id: str, column: str) -> None:
    """journey.defaults.mobileNumber reading an entry column, written like defaults.email."""
    data = pkg.entities["journeys"][pkg.journey_id]["data"]
    old = data["defaults"]
    key_ref = ref("eventDefinitions", event_id, "eventDefinitionKey")
    data["defaults"] = {"email": old["email"], "mobileNumber": ['{{Event.%s."%s"}}' % (key_ref, column)],
                        "properties": old["properties"]}


# ---------------------------------------------------------------------------
# The examples
# ---------------------------------------------------------------------------

def build_relative_date_after_today() -> dict:
    pkg = Package("relative_date_after_today", "EX relative date after today")
    B.setup_contact_only(pkg, [date("StartDate")])
    add_email_asset(pkg, "903101", "Starting Soon", "STARTING SOON", with_link=False)
    m = "DE_Master"
    j = Journey(pkg, "EX_RelativeDateAfterToday")
    j.decision("SPLIT-1", "Start date", [
        ("path_a", "After today", criteria(pkg.cond(m, "StartDate", "After", rel(0))), "EMAIL-1",
         "StartDate after Today"),
        ("path_b", "Today or before", criteria(pkg.cond(m, "StartDate", "AtOrBefore", rel(0))), "UPDATE-2",
         "StartDate on or before Today"),
        ("remainder_path", "Remainder", None, "UPDATE-3", None)])
    j.email("EMAIL-1", "903101", "UPDATE-1")
    B.log_update(j, "UPDATE-1", "Future start", "FutureStart", None)
    B.log_update(j, "UPDATE-2", "Already started", "Started", None)
    B.log_update(j, "UPDATE-3", "No start date", "NoStartDate", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_RelativeDateAfterToday_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_wait_offset_then_relative_split() -> dict:
    pkg = Package("wait_offset_then_relative_split", "EX wait offset then relative split")
    B.setup_contact_only(pkg, [date("EndDate")])
    add_email_asset(pkg, "903201", "Renewal Reminder", "YOUR CONTRACT ENDS SOON", with_link=False)
    m = "DE_Master"
    j = Journey(pkg, "EX_WaitOffsetThenRelativeSplit")
    wait_attribute_offset(j, "WAIT-1", m, "EndDate", "SPLIT-1", -13, "DAYS", "13 days before EndDate")
    j.decision("SPLIT-1", "End date still 13 days ahead", [
        ("path_a", "In time", criteria(pkg.cond(m, "EndDate", "AtOrAfter", rel(13))), "EMAIL-1",
         "EndDate on or after Today Plus 13 days"),
        ("remainder_path", "Late", None, "UPDATE-2", None)])
    j.email("EMAIL-1", "903201", "UPDATE-1")
    B.log_update(j, "UPDATE-1", "Reminder sent", "ReminderSent", None)
    B.log_update(j, "UPDATE-2", "Too late", "TooLate", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_WaitOffsetThenRelativeSplit_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_exit_criteria_at_wait_end() -> dict:
    pkg = Package("exit_criteria_at_wait_end", "EX exit criteria at wait end")
    B.setup_contact_only(pkg, [text("Status", 20, nullable=False), boolean("IsDunning")])
    add_email_asset(pkg, "903301", "Active Offer", "AN OFFER FOR ACTIVE CUSTOMERS", with_link=False)
    m = "DE_Master"
    j = Journey(pkg, "EX_ExitCriteriaAtWaitEnd")
    wait_days(j, "WAIT-1", "SPLIT-1", 1)
    j.decision("SPLIT-1", "Active?", [
        ("path_a", "Active", criteria(pkg.cond(m, "Status", "Equal", "Active")), "EMAIL-1", "Status equal Active"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    j.email("EMAIL-1", "903301", "UPDATE-1")
    B.log_update(j, "UPDATE-1", "Offer sent", "OfferSent", None)
    B.log_update(j, "UPDATE-2", "Not active", "NotActive", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_ExitCriteriaAtWaitEnd_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    xml = criteria_or(pkg.cond(m, "Status", "NotEqual", "Active"), pkg.cond(m, "IsDunning", "Is", "true"))
    set_exits(pkg, [exit_criteria(pkg, j.name, xml, "Status not equal Active OR IsDunning is True")])
    return export(pkg)


def build_exit_criteria_removes_everyone() -> dict:
    pkg = Package("exit_criteria_removes_everyone", "EX exit criteria removes everyone")
    B.setup_contact_only(pkg, [date("EndDate"), boolean("IsDunning"), boolean("Optin")])
    add_email_asset(pkg, "903401", "Retention Offer", "STAY WITH US", with_link=False)
    m = "DE_Master"
    j = Journey(pkg, "EX_ExitCriteriaRemovesEveryone")
    j.decision("SPLIT-1", "End date known?", [
        ("path_a", "Known", criteria(pkg.cond(m, "EndDate", "IsNotNull", "")), "WAIT-1", "EndDate is not null"),
        ("remainder_path", "Remainder", None, "UPDATE-3", None)])
    wait_days(j, "WAIT-1", "SPLIT-2", 2)
    j.decision("SPLIT-2", "Opt-in", [
        ("path_a", "true", criteria(pkg.cond(m, "Optin", "Is", "true")), "EMAIL-1", "Optin is True"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    j.email("EMAIL-1", "903401", "UPDATE-1")
    B.log_update(j, "UPDATE-1", "Offer sent", "OfferSent", None)
    B.log_update(j, "UPDATE-2", "No opt-in", "NoOptin", None)
    B.log_update(j, "UPDATE-3", "No end date", "NoEndDate", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_ExitCriteriaRemovesEveryone_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    xml = criteria_or(pkg.cond(m, "EndDate", "IsNotNull", ""), pkg.cond(m, "IsDunning", "Is", "true"))
    set_exits(pkg, [exit_criteria(pkg, j.name, xml, "EndDate is not null OR IsDunning is True")])
    return export(pkg)


def build_relative_date_chain_waits() -> dict:
    pkg = Package("relative_date_chain_waits", "EX relative date chain waits")
    X.entry_table(pkg, "DE_RC_Entry", [date("EndDate")])
    X.log_table(pkg, "DE_RC_Log")
    add_email_asset(pkg, "903501", "Renewal Window", "YOUR RENEWAL WINDOW IS OPEN", with_link=False)
    j = Journey(pkg, "EX_RelativeDateChainWaits")
    event_id, _ = X.add_de_entry(pkg, j.name, "DE_RC_Entry")

    def window(upper: int, lower: int) -> str:
        return criteria(X.jd_cond(event_id, "EndDate", "AtOrBefore", rel(upper)),
                        X.jd_cond(event_id, "EndDate", "AtOrAfter", rel(lower)))

    j.decision("SPLIT-1", "Ends in 45 to 90 days", [
        ("path_a", "In window", window(90, 45), "EMAIL-1",
         "EndDate on or before Today Plus 90 days AND EndDate on or after Today Plus 45 days"),
        ("remainder_path", "Outside", None, "UPDATE-3", None)])
    j.email("EMAIL-1", "903501", "WAIT-1")
    wait_days(j, "WAIT-1", "SPLIT-2", 2)
    j.decision("SPLIT-2", "Still in window", [
        ("path_a", "In window", window(88, 45), "UPDATE-1",
         "EndDate on or before Today Plus 88 days AND EndDate on or after Today Plus 45 days"),
        ("remainder_path", "Outside", None, "UPDATE-2", None)])
    X.log(j, "UPDATE-1", "Still in window", "DE_RC_Log", "StillInWindow", None)
    X.log(j, "UPDATE-2", "Left the window", "DE_RC_Log", "LeftWindow", None)
    X.log(j, "UPDATE-3", "Outside the window", "DE_RC_Log", "OutsideWindow", None)
    X.add_journey_ext(pkg, j, event_id, "EmailAudience")
    return export(pkg)


def build_relative_date_before_minus_days() -> dict:
    pkg = Package("relative_date_before_minus_days", "EX relative date before minus days")
    B.setup_contact_only(pkg, [date("LastOrder")])
    add_email_asset(pkg, "903701", "We Miss You", "WE MISS YOU", with_link=False)
    m = "DE_Master"
    j = Journey(pkg, "EX_RelativeDateBeforeMinusDays")
    j.decision("SPLIT-1", "No order this week", [
        ("path_a", "Lapsed", criteria(pkg.cond(m, "LastOrder", "Before", rel(-7))), "EMAIL-1",
         "LastOrder before Today Minus 7 days"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    j.email("EMAIL-1", "903701", "UPDATE-1")
    B.log_update(j, "UPDATE-1", "Win-back sent", "WinBack", None)
    B.log_update(j, "UPDATE-2", "Recent order", "Recent", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_RelativeDateBeforeMinusDays_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_sms_real_phone() -> dict:
    pkg = Package("sms_real_phone", "EX sms real phone")
    pkg.table("DE_Entry", [text("ContactId", 50, pk=True, nullable=False), email_field(), phone("MobilePhone")],
              sendable_field="ContactId")
    pkg.table("DE_Master", [text("ContactId", 50, pk=True, nullable=False), text("FirstName"), email_field(),
                            phone("Phone"), boolean("SmsOptOut")], sendable_field="ContactId")
    B.log_table(pkg)
    pkg.contact_model([("DE_Master", "ContactId", ""), ("DE_Entry", "ContactId", "ContactId")])
    m = "DE_Master"
    j = Journey(pkg, "EX_SmsRealPhone")
    j.decision("SPLIT-1", "SMS allowed?", [
        ("path_a", "Allowed", criteria(pkg.cond(m, "SmsOptOut", "IsNot", "true")), "SMSSYNC-1",
         "SmsOptOut is not True"),
        ("remainder_path", "Opted out", None, "UPDATE-2", None)])
    sms(j, "SMSSYNC-1", "Reminder SMS", "903601", "UPDATE-1")
    B.log_update(j, "UPDATE-1", "SMS sent", "SmsSent", None)
    B.log_update(j, "UPDATE-2", "SMS opted out", "SmsOptedOut", None)
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress,\n    M.Phone AS MobilePhone\nFROM %s M"
           % table_sql(pkg.tables["DE_Master"]))
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_SmsRealPhone_Entry", sql)
    add_journey(pkg, j, event)
    set_mobile_default(pkg, event, "MobilePhone")
    return export(pkg)


# Example directory -> builder. Directories sharing a builder get the same package.json.
STEP3 = {
    "relative_date_after_today": build_relative_date_after_today,
    "wait_offset_then_relative_split": build_wait_offset_then_relative_split,
    "exit_criteria_at_wait_end": build_exit_criteria_at_wait_end,
    "exit_criteria_removes_everyone": build_exit_criteria_removes_everyone,
    "relative_date_chain_waits": build_relative_date_chain_waits,
    "relative_date_before_minus_days": build_relative_date_before_minus_days,
    "sms_real_phone": build_sms_real_phone,
    "sms_no_real_phone": build_sms_real_phone,          # same package, no real phone in settings
}


# ---------------------------------------------------------------------------
# Validation of the step 3 shapes (on top of build_examples.validate_package / validate_expected)
# ---------------------------------------------------------------------------

def _conditions(xml: str):
    root = ET.fromstring(xml)                           # raises on malformed XML
    return root.iter("Condition")


def validate_step3(directory: Path) -> None:
    path = directory / "package.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    entities = doc["entities"]
    journey = entities["journeys"][doc["selectedEntities"]["journeys"][0]]["data"]
    xmls = []
    for a in journey["activities"]:
        if a["type"] == "MULTICRITERIADECISION":
            xmls.extend(a["configurationArguments"]["criteria"].values())
        if a["type"] == "WAIT" and a["metaData"].get("uiType") == "WAITBYATTRIBUTE":
            config = a["configurationArguments"]
            assert isinstance(config["waitDuration"], int) and config["waitUnit"] in ("DAYS", "HOURS", "MINUTES"), \
                "%s: wait offset of %s" % (path, a["key"])
            assert re.fullmatch(r'\{\{Contact\.Attribute\.[^"]+\."[^"]+"\}\}', config["waitEndDateAttributeExpression"]), \
                "%s: wait expression of %s" % (path, a["key"])
        if a["type"] == "SMSSYNC":
            assert a["configurationArguments"].get("assetId"), "%s: SMS without assetId" % path
    exits = journey.get("exits") or []
    for e in exits:
        assert e["key"] and e["type"] == "ExitCriteria" and e["outcomes"] == [], "%s: exit shape" % path
        xmls.append(e["configurationArguments"]["criteria"])
    for xml in xmls:
        for c in _conditions(xml):
            value = (c.findtext(".//Value") or "")
            if c.get("Operator") in RELATIVE_OPERATORS:
                assert RELATIVE_RE.match(value), "%s: relative value %r" % (path, value)
    mobile = (journey.get("defaults") or {}).get("mobileNumber")
    if mobile:
        column = re.search(r'\."([^"]+)"\}\}$', mobile[0]).group(1)
        event = next(iter(entities["eventDefinitions"].values()))["data"]
        de_id = re.search(r"dataExtensions/([^/]+)/", event["dataExtensionId"]).group(1)
        names = [f["name"] for f in entities["dataExtensions"][de_id]["data"]["fields"]]
        assert column in names, "%s: mobile default %s not in the entry table" % (path, column)
    expected_path = directory / "expected.json"
    if expected_path.exists():
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        wanted = {"%s/met" % e["key"] for e in exits}
        listed = set(expected.get("exit_targets") or {})
        assert listed == wanted, "%s: exit_targets %s != %s" % (expected_path, sorted(listed), sorted(wanted))
        count = expected.get("activity_count")
        assert count is None or count == len(journey["activities"]), \
            "%s: activity_count %s != %d" % (expected_path, count, len(journey["activities"]))
        validate_names(expected_path, expected, entities, {a["key"] for a in journey["activities"]})


def validate_names(path: Path, expected: dict, entities: dict, activity_keys: set[str]) -> None:
    """Every Table.Field and activity key that expected.json names exists in the package: a typo would make a
    case or a proposed key silently match nothing."""
    schemas = {e["data"]["name"]: {f["name"] for f in e["data"]["fields"]}
               for e in entities["dataExtensions"].values()}

    def field(ref_: str) -> None:
        t, f = ref_.split(".", 1)
        assert t in schemas and f in schemas[t], "%s: unknown field %s" % (path, ref_)

    def activity(key) -> None:
        assert key is None or key in activity_keys, "%s: unknown activity %s" % (path, key)

    for t in list(expected.get("csv_tables", [])) + list(expected.get("table_levels", {})):
        assert t in schemas, "%s: unknown table %s" % (path, t)
    for ref_ in expected.get("csv_values", {}):
        field(ref_)
    for case in expected.get("cases", []):
        for key in ("contact_values", "contact_constraints", "contact_values_any_of", "scenario_values"):
            for ref_ in case.get(key, {}):
                field(ref_)
        for row in case.get("contact_rows", []):
            for ref_ in row:
                field(ref_)
        assert len(case.get("contact_rows", [])) in (0, case.get("contact_count", 0)), \
            "%s: contact_rows must list every contact of the case" % path
        for key in list(case.get("timeline", {})) + case.get("emails_sent", []) + case.get("sms_sent", []):
            activity(key)
        activity(case.get("end_activity"))
        activity(case.get("exit_by_criteria"))


def main(names=None) -> int:
    names = names or list(STEP3)
    for name in names:
        d = EXAMPLES_DIR / name
        d.mkdir(exist_ok=True)
        (d / "package.json").write_text(json.dumps(STEP3[name](), indent=1, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    for name in names:
        d = EXAMPLES_DIR / name
        branches = B.validate_package(d / "package.json")
        if (d / "expected.json").exists():
            B.validate_expected(d / "expected.json", branches)
        validate_step3(d)
        print("ok  %-34s %3d split branches %s" % (name, len(branches), branches))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
