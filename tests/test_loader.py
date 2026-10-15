"""Unit tests for cjhelper.loader on small hand-built exports, plus one test module section on the pilot.

The hand-built export follows docs/export-format.md. Its journey (activity keys):

    DECISION-1  Channel: CHANNEL = Email -> EMAIL-1 | CHANNEL = SMS -> UPDATE-1 | remainder -> exit
    EMAIL-1     Welcome (asset with a link)           -> WAITATTR-1
    WAITATTR-1  wait on Config Set."WAIT1"             -> ENGAGE-1
    ENGAGE-1    opened EMAIL-1?  yes -> ENGAGE-2   no -> RANDOM-1
    ENGAGE-2    clicked EMAIL-1? yes -> WAITDUR-1  no -> exit
    WAITDUR-1   2 hours                                 -> STO-1
    STO-1       send-time wait, 24 hours window         -> EMAIL-2
    EMAIL-2     Plain (asset without a link, by legacy id) -> exit
    RANDOM-1    90% -> UPDATE-1, 10% -> exit
    UPDATE-1    writes Log.Note, Log.Counter (number), Log.Stamp ({{DateTime.Now}}), Entry.Flag -> DECISION-2
    DECISION-2  Consent: Perm true AND (Opt In false OR Opt In null) -> exit | remainder -> exit
"""

import copy
import json
from pathlib import Path

import pytest

from cjhelper import loader
from cjhelper.loader import ENGAGEMENT_KINDS, LoadError, load_package, resolve_references
from cjhelper.model import (
    ENTRY_AUTOMATION,
    ENTRY_OTHER,
    ENTRY_RUN_ONCE,
    ENTRY_SCHEDULED,
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
    SOURCE_CONTACT,
    SOURCE_JOURNEY,
    Condition,
    ConditionGroup,
    Relationship,
    RelativeDate,
    Write,
    formula_conditions,
)

PILOT = Path(__file__).resolve().parent.parent / "fixtures" / "CJ template complesso-v1.json"

ENTRY, MASTER, CONFIG, LOG = "de-entry", "de-master", "de-config", "de-log"
S_ENTRY, S_MASTER, S_CONFIG = "set-entry", "set-master", "set-config"
GROUP, EVENT, OLD_EVENT, AUTOMATION = "grp-1", "ev-1", "ev-2", "auto-1"
Q_ENTRY, Q_CHECK = "q-entry", "q-check"
ASSET_LINK, ASSET_PLAIN = "111", "222"
JOURNEY, OTHER_JOURNEY = "j-1", "j-2"


# ---------------------------------------------------------------------------
# Builders for a small export in the pilot's shape
# ---------------------------------------------------------------------------

def ref(kind, entity_id, path):
    return "{{mcpm#/entities/%s/%s/data/%s}}" % (kind, entity_id, path)


def entity(data):
    return {"issues": [], "originID": None, "originEID": None, "data": data}


def field(name, type_="Text", ordinal=0, length=50, pk=False, nullable=True, default=None):
    raw = {"name": name, "type": type_, "ordinal": ordinal, "isPrimaryKey": pk, "isNullable": nullable}
    if length is not None:
        raw["length"] = length
    if default is not None:
        raw["defaultValue"] = default
    return raw


def table(name, fields, sendable_field=None):
    data = {"name": name, "customerKey": f"KEY-{name}", "isSendable": sendable_field is not None,
            "sendableCustomObjectField": sendable_field, "fields": fields}
    if sendable_field:
        data["sendableSubscriberField"] = "_SubscriberKey"
    return entity(data)


def condition(set_id, field_name, operator, value="", group=None, through=()):
    """A Contact Data condition as the pilot writes it: the group-to-set relationship ``group`` in UiMetaData, then one
    <AttributePath> per relationship of ``through``, the one nearest the contact outermost."""
    inner = f"<Value><![CDATA[{value}]]></Value>"
    for relationship_id in reversed(through):
        inner = (f'<AttributePath RelationshipID="{ref("attributeRelationships", relationship_id, "id")}">{inner}'
                 "</AttributePath>")
    ui = ""
    if group is not None:
        ui = (' UiMetaData="{&quot;groupToSetRelationshipId&quot;:&quot;%s&quot;}"'
              % ref("attributeRelationships", group, "id"))
    return (f'<Condition Key="{ref("attributeSets", set_id, "name")}.{field_name}" Operator="{operator}"{ui}>{inner}'
            "</Condition>")


# The attribute paths of the hand-built export, as in the pilot: DE_Master is linked to the contact, DE_Config is
# reached through DE_Master and DE_Entry.
MASTER_PATH = {"group": "rel-root"}
CONFIG_PATH = {"group": "rel-root", "through": ("rel-me", "rel-ec")}


def condition_set(*items, operator="AND"):
    return f'<ConditionSet Operator="{operator}" ConditionSetName="Individual Filter Grouping">{"".join(items)}</ConditionSet>'


def filter_definition(*items, operator="AND"):
    return f"<FilterDefinition>{condition_set(*items, operator=operator)}</FilterDefinition>"


def outcome(key, next_=None, label=None, description=None, **arguments):
    meta = {}
    if label is not None:
        meta["label"] = label
    if description is not None:
        meta["criteriaDescription"] = description
    raw = {"key": key, "arguments": arguments, "metaData": meta}
    if next_ is not None:
        raw["next"] = next_
    return raw


def activity(key, type_, outcomes, name="", config=None, arguments=None, meta=None):
    return {"key": key, "type": type_, "name": name, "outcomes": outcomes,
            "configurationArguments": config or {}, "arguments": arguments or {}, "metaData": meta or {}}


def decision(key, outcomes, criteria, name="Decision"):
    return activity(key, "MULTICRITERIADECISION", outcomes, name, config={"criteria": criteria})


def wait_by_duration(key, next_, amount=2, unit="HOURS"):
    return activity(key, "WAIT", [outcome(f"{key}-o", next_)], f"{amount} {unit}",
                    config={"waitDuration": amount, "waitUnit": unit, "timeZone": "W. Europe Standard Time",
                            "waitEndDateAttributeExpression": ""},
                    meta={"uiType": "WAITBYDURATION"})


def wait_by_attribute(key, next_, expression, attribute_id=None, offset=0):
    meta = {"uiType": "WAITBYATTRIBUTE"}
    if attribute_id is not None:
        meta["attribute"] = {"id": attribute_id, "displayType": "Date"}
    return activity(key, "WAIT", [outcome(f"{key}-o", next_)], "on WAIT1",
                    config={"waitDuration": offset, "waitUnit": "DAYS", "timeZone": "W. Europe Standard Time",
                            "waitEndDateAttributeExpression": expression},
                    meta=meta)


def email(key, next_, email_id, name="Email"):
    return activity(key, "EMAILV2", [outcome(f"{key}-o", next_)], name,
                    config={"triggeredSend": {"emailId": email_id, "emailSubject": "Subject",
                                              "senderProfileId": "{{mcpm:senderProfile}}"}})


def engagement(key, stats_type, ref_key, yes_next, no_next, name="Engagement", urls=()):
    return activity(key, "ENGAGEMENTDECISION",
                    [outcome("branchResult-1", yes_next, "Yes", when=True),
                     outcome("branchResult-2", no_next, "No", when=False)],
                    name, config={"statsTypeId": stats_type, "refActivityCustomerKey": ref_key,
                                  "engagementUrls": {"urls": list(urls)}})


def write(de_id, field_name, value):
    return {"dataExtensionId": ref("dataExtensions", de_id, "id"),
            "field": ref("dataExtensions", de_id, f"fields/name:{field_name}/id"), "value": value}


def update(key, next_, writes, name="Update"):
    return activity(key, "UPDATECONTACTDATA", [outcome(f"{key}-o", next_)], name,
                    arguments={"activityData": {"updateContactFields": writes}})


def default_activities():
    channel = decision("DECISION-1", [
        outcome("path_email", "EMAIL-1", "Email", "CHANNEL equal Email"),
        outcome("path_sms", "UPDATE-1", "SMS"),
        outcome("remainder_path", None, "Remainder"),
    ], {
        "path_email": filter_definition(condition(S_CONFIG, "CHANNEL", "Equal", "Email", **CONFIG_PATH)),
        "path_sms": filter_definition(condition(S_CONFIG, "CHANNEL", "Equal", "SMS", **CONFIG_PATH)),
    }, name="Channel")
    consent = decision("DECISION-2", [
        outcome("ok", None, None, "Perm is True AND ( Opt In is False OR Opt In is null )"),
        outcome("remainder_path", None, "Remainder"),
    ], {
        "ok": filter_definition(
            condition(S_MASTER, "Perm", "Is", "true", **MASTER_PATH),
            condition_set(condition(S_MASTER, "Opt In", "Is", "false", **MASTER_PATH),
                          condition(S_MASTER, "Opt In", "IsNull", "", **MASTER_PATH), operator="OR")),
    }, name="Consent")
    return [
        channel,
        email("EMAIL-1", "WAITATTR-1", ref("assets", ASSET_LINK, "data/email/legacy/legacyId"), name="Welcome mail"),
        wait_by_attribute("WAITATTR-1", "ENGAGE-1",
                          '{{Contact.Attribute.Contacts.Master Set.DE_Entry.Config Set."WAIT1"}}',
                          attribute_id=f'{ref("attributeSets", S_CONFIG, "name")}.WAIT1'),
        engagement("ENGAGE-1", 2, "EMAIL-1", "ENGAGE-2", "RANDOM-1", name="Open Welcome"),
        engagement("ENGAGE-2", 3, "EMAIL-1", "WAITDUR-1", None, name="Click Welcome"),
        wait_by_duration("WAITDUR-1", "STO-1"),
        activity("STO-1", "STOWAIT", [outcome("STO-1-o", "EMAIL-2")],
                 config={"params": {"enableRandomTime": False, "slidingWindowHours": 24}}),
        email("EMAIL-2", None, "999", name="Second mail"),
        activity("RANDOM-1", "RANDOMSPLIT", [outcome("branchResult-1", "UPDATE-1", "90%", percentage=90),
                                             outcome("branchResult-2", None, "10%", percentage=10)]),
        update("UPDATE-1", "DECISION-2", [
            write(LOG, "Note", "free text"),
            write(LOG, "Counter", 1),
            write(LOG, "Stamp", "{{DateTime.Now}}"),
            write(ENTRY, "flag", True),
        ], name="Log"),
        consent,
    ]


def journey(name, activities, event_id=EVENT, mobile_event_id=OLD_EVENT):
    return entity({
        "name": name,
        "key": f"key-{name}",
        "definitionType": "Multistep",
        "triggers": [{
            "key": "TRIGGER",
            "type": "AutomationAudience",
            "metaData": {"eventDefinitionId": ref("eventDefinitions", event_id, "id"),
                         "eventDefinitionKey": ref("eventDefinitions", event_id, "eventDefinitionKey")},
        }],
        "defaults": {
            "email": ["{{Event." + ref("eventDefinitions", event_id, "eventDefinitionKey") + '."Emailaddress"}}'],
            "mobileNumber": ["{{Event." + ref("eventDefinitions", mobile_event_id, "eventDefinitionKey")
                             + '."Mobile"}}'],
        },
        "activities": activities,
    })


def make_export(activities=None):
    """A small export: four tables, three attribute sets, three relationships, one entry event and automation."""
    sql = ("SELECT M.ContactId, C.CampaignName FROM "
           f"{ref('dataExtensions', MASTER, 'namePrefix')}[{ref('dataExtensions', MASTER, 'name')}] M "
           f"JOIN {ref('dataExtensions', CONFIG, 'namePrefix')}[{ref('dataExtensions', CONFIG, 'name')}] C "
           "ON M.ContactId = C.CampaignName WHERE C.ACTIVE = 'True'")
    return {
        "name": "Test package",
        "selectedEntities": {"journeys": [JOURNEY]},
        "entities": {
            "dataExtensions": {
                ENTRY: table("DE_Entry", [
                    field("CampaignName", ordinal=1, pk=True, nullable=False),
                    field("ContactId", ordinal=0, pk=True, nullable=False, length=255),
                    field("Flag", "Boolean", ordinal=3, length=None, default="False"),
                    field("Emailaddress", "EmailAddress", ordinal=2, length=254),
                ], sendable_field="ContactId"),
                MASTER: table("DE_Master", [
                    field("ContactId", pk=True, nullable=False),
                    field("Perm", "Boolean", ordinal=1, length=None),
                    field("Opt In", "Boolean", ordinal=2, length=None),
                    field("Mobile", "Phone", ordinal=3),
                ], sendable_field="ContactId"),
                CONFIG: table("DE_Config", [
                    field("CampaignName", pk=True, nullable=False, length=250),
                    field("CHANNEL", ordinal=1),
                    field("WAIT1", "Date", ordinal=2, length=None, nullable=False, default="GETDATE()"),
                    field("ACTIVE", "Boolean", ordinal=3, length=None, nullable=False, default="False"),
                ]),
                LOG: table("DE_Log", [
                    field("ContactId", pk=True, nullable=False),
                    field("Note", ordinal=1),
                    field("Counter", ordinal=2),
                    field("Stamp", "Date", ordinal=3, length=None),
                ], sendable_field="ContactId"),
            },
            "attributeGroups": {GROUP: entity({"name": "GROUP"})},
            "attributeSets": {
                S_MASTER: entity({"name": "Master Set", "storageLogicalType": "DataExtension",
                                  "storageName": ref("dataExtensions", MASTER, "name"),
                                  "groupId": ref("attributeGroups", GROUP, "id")}),
                S_ENTRY: entity({"name": "DE_Entry", "storageLogicalType": "DataExtension",
                                 "storageName": ref("dataExtensions", ENTRY, "name")}),
                S_CONFIG: entity({"name": "Config Set", "storageLogicalType": "DataExtension",
                                  "storageName": ref("dataExtensions", CONFIG, "name")}),
            },
            "attributeRelationships": {
                "rel-root": entity({
                    "name": "GROUP <- DE_Master", "isGroupToSetRelationship": True,
                    "relationshipAttributes": [{"rightAttributeName": "ContactId",
                                                "rightDataExtensionID": ref("dataExtensions", MASTER, "id")}],
                    "leftItem": {"relationshipType": "AttributeGroup", "cardinality": "One",
                                 "identifier": ref("attributeGroups", GROUP, "id"),
                                 "name": ref("attributeGroups", GROUP, "name")},
                    "rightItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                                  "name": ref("attributeSets", S_MASTER, "name")}}),
                "rel-me": entity({
                    "name": "DE_Master -> DE_Entry", "isGroupToSetRelationship": False,
                    "relationshipAttributes": [{"leftAttributeName": "ContactId", "rightAttributeName": "contactid"}],
                    "leftItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                                 "identifier": ref("attributeSets", S_MASTER, "id")},
                    "rightItem": {"relationshipType": "AttributeSet", "cardinality": "Many",
                                  "name": ref("attributeSets", S_ENTRY, "name")}}),
                "rel-ec": entity({
                    "name": "DE_Entry -> DE_Config", "isGroupToSetRelationship": False,
                    "relationshipAttributes": [{"leftAttributeName": "CampaignName",
                                                "rightAttributeName": "CampaignName"}],
                    "leftItem": {"relationshipType": "AttributeSet", "cardinality": "Many",
                                 "identifier": ref("attributeSets", S_ENTRY, "id")},
                    "rightItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                                  "name": ref("attributeSets", S_CONFIG, "name")}}),
            },
            "eventDefinitions": {
                EVENT: entity({"name": "Entry event", "type": "AutomationAudience",
                               "eventDefinitionKey": "DEAudience-1",
                               "dataExtensionId": ref("dataExtensions", ENTRY, "id"),
                               "arguments": {"automationId": ref("automations", AUTOMATION, "id"), "criteria": ""}}),
                OLD_EVENT: entity({"name": "Old event", "type": "AutomationAudience",
                                   "eventDefinitionKey": "DEAudience-2",
                                   "dataExtensionId": ref("dataExtensions", ENTRY, "id"), "arguments": {}}),
            },
            "automations": {
                AUTOMATION: entity({"name": "A_Entry", "steps": [
                    {"activities": [{"name": ref("queryActivities", Q_CHECK, "name"),
                                     "activityObjectId": ref("queryActivities", Q_CHECK, "id"),
                                     "objectTypeId": 300}]},
                    {"activities": [{"name": ref("dataExtensions", LOG, "name"),
                                     "activityObjectId": ref("verificationActivities", "v-1", "id"),
                                     "objectTypeId": 1000}]},
                    {"activities": [{"name": ref("queryActivities", Q_ENTRY, "name"),
                                     "activityObjectId": ref("queryActivities", Q_ENTRY, "id"),
                                     "objectTypeId": 300}]},
                ]}),
            },
            "queryActivities": {
                Q_ENTRY: entity({"name": "Q_Entry", "targetId": ref("dataExtensions", ENTRY, "id"),
                                 "targetUpdateTypeName": "Update", "queryText": sql}),
                Q_CHECK: entity({"name": "Q_Check", "targetId": ref("dataExtensions", LOG, "id"),
                                 "targetUpdateTypeName": "Overwrite",
                                 "queryText": f"SELECT ContactId FROM [{ref('dataExtensions', LOG, 'name')}]"}),
            },
            "assets": {
                ASSET_LINK: entity({"name": "Welcome", "assetType": {"name": "htmlemail"},
                                    "views": {"subjectline": {"content": "Hello"},
                                              "html": {"content": '<p>Hi</p><A HREF="https://example.com">Go</A>'},
                                              "text": {"content": ""}}}),
                ASSET_PLAIN: entity({"name": "Plain", "assetType": {"name": "htmlemail"},
                                     "data": {"email": {"legacy": {"legacyId": 999}}},
                                     "views": {"subjectline": {"content": "Plain news"},
                                               "html": {"content": "<p>No link here</p>"},
                                               "text": {"content": "No link here"}}}),
            },
            "journeys": {JOURNEY: journey("J_Test", default_activities() if activities is None else activities)},
        },
    }


def load(activities=None):
    return load_package(make_export(activities))


def has_warning(package, *fragments):
    return any(all(fragment in warning for fragment in fragments) for warning in package.warnings)


def with_public_holidays(export):
    """Add the public holiday shape of the step 3 exports: the contact is linked to DE_Holiday_Entry (attribute set
    PublicHoliday_Entry, group-to-set on ContactId), which points to DE_Holiday (attribute set PublicHoliday_DE) on
    PHKey. The set names differ from the table names on purpose."""
    entities = export["entities"]
    entities["dataExtensions"]["de-ph-entry"] = table("DE_Holiday_Entry", [
        field("ContactId", pk=True, nullable=False), field("PHKey", ordinal=1)])
    entities["dataExtensions"]["de-ph"] = table("DE_Holiday", [
        field("PHKey", pk=True, nullable=False), field("PHStart", "Date", ordinal=1, length=None),
        field("PHEnd", "Date", ordinal=2, length=None)])
    entities["attributeGroups"]["grp-ph"] = entity({"name": "PublicHoliday"})
    for set_id, name, de_id in (("set-ph-entry", "PublicHoliday_Entry", "de-ph-entry"),
                                ("set-ph", "PublicHoliday_DE", "de-ph")):
        entities["attributeSets"][set_id] = entity({
            "name": name, "storageLogicalType": "DataExtension", "storageName": ref("dataExtensions", de_id, "name"),
            "groupId": ref("attributeGroups", "grp-ph", "id")})
    entities["attributeRelationships"]["rel-ph-root"] = entity({
        "name": "PublicHoliday <- PublicHoliday_Entry", "isGroupToSetRelationship": True,
        "relationshipAttributes": [{"rightAttributeName": "ContactId"}],
        "leftItem": {"relationshipType": "AttributeGroup", "cardinality": "One",
                     "identifier": ref("attributeGroups", "grp-ph", "id"), "name": ref("attributeGroups", "grp-ph", "name")},
        "rightItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                      "name": ref("attributeSets", "set-ph-entry", "name")}})
    entities["attributeRelationships"]["rel-ph"] = entity({
        "name": "PublicHoliday_Entry -> PublicHoliday_DE", "isGroupToSetRelationship": False,
        "relationshipAttributes": [{"leftAttributeName": "PHKey", "rightAttributeName": "PHKey"}],
        "leftItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                     "identifier": ref("attributeSets", "set-ph-entry", "id")},
        "rightItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                      "name": ref("attributeSets", "set-ph", "name")}})
    return export


def reader_for(export):
    """The loader's reader after it has read the tables, attribute sets, relationships and entry event of ``export``,
    as load_package does before the journey: unit tests give it conditions built by hand."""
    entities = resolve_references(export["entities"], export["entities"])
    reader = loader._ExportReader(entities, [])
    reader.read_tables()
    reader.read_attribute_sets()
    reader.relationships = reader.read_relationships()
    reader.entry = reader.read_entry_event(entities["journeys"][JOURNEY]["data"])
    return reader


# ---------------------------------------------------------------------------
# resolve_references
# ---------------------------------------------------------------------------

ENTITIES = {
    "dataExtensions": {
        "de-1": {"data": {"name": "DE_One", "namePrefix": "ENT.", "customerKey": "K1"}},
        "de-2": {"data": {"name": "DE_Two"}},
    },
    "attributeSets": {"set-1": {"data": {"name": "Set One", "storageName": "{{mcpm#/entities/dataExtensions/de-2/data/name}}"}}},
    "eventDefinitions": {"ev-1": {"data": {"eventDefinitionKey": "DEAudience-1"}}},
    "assets": {"55": {"data": {"name": "Mail", "data": {"email": {"options": {}}}}}},
}


@pytest.mark.parametrize("text, expected", [
    ("{{mcpm#/entities/dataExtensions/de-1/data/name}}", "DE_One"),
    ("{{mcpm#/entities/dataExtensions/de-1/data/id}}", "de-1"),
    ("{{mcpm#/entities/dataExtensions/de-1/data/customerKey}}", "K1"),
    ("{{mcpm#/entities/dataExtensions/de-1/data/fields/name:Control Group/id}}", "Control Group"),
    ("{{mcpm#/entities/dataExtensions/de-1/data/namePrefix}}", "ENT."),
    ("{{mcpm#/entities/dataExtensions/de-2/data/namePrefix}}", ""),
    ("{{mcpm#/entities/eventDefinitions/ev-1/data/eventDefinitionKey}}", "DEAudience-1"),
    ("{{mcpm#/entities/categories/123/data/id}}", "123"),
])
def test_reference_rules(text, expected):
    warnings = []
    assert resolve_references(text, ENTITIES, warnings) == expected
    assert warnings == []


def test_references_inside_sql_and_xml_and_personalisation():
    sql = ("SELECT * FROM {{mcpm#/entities/dataExtensions/de-2/data/namePrefix}}"
           "[{{mcpm#/entities/dataExtensions/de-2/data/name}}] A JOIN "
           "{{mcpm#/entities/dataExtensions/de-1/data/namePrefix}}[{{mcpm#/entities/dataExtensions/de-1/data/name}}] B")
    assert resolve_references(sql, ENTITIES) == "SELECT * FROM [DE_Two] A JOIN ENT.[DE_One] B"
    xml = ('<Condition Key="{{mcpm#/entities/attributeSets/set-1/data/name}}.Field X" Operator="Is" '
           'UiMetaData="{&quot;id&quot;:&quot;{{mcpm#/entities/attributeRelationships/r-9/data/id}}&quot;}">')
    assert resolve_references(xml, ENTITIES) == (
        '<Condition Key="Set One.Field X" Operator="Is" UiMetaData="{&quot;id&quot;:&quot;r-9&quot;}">')
    default = '{{Event.{{mcpm#/entities/eventDefinitions/ev-1/data/eventDefinitionKey}}."Emailaddress"}}'
    assert resolve_references(default, ENTITIES) == '{{Event.DEAudience-1."Emailaddress"}}'


def test_a_value_holding_a_reference_is_resolved_in_turn():
    assert resolve_references("{{mcpm#/entities/attributeSets/set-1/data/storageName}}", ENTITIES) == "DE_Two"


def test_missing_path_gives_the_entity_id_and_a_warning():
    warnings = []
    text = "{{mcpm#/entities/assets/55/data/data/email/legacy/legacyId}}"
    assert resolve_references(text, ENTITIES, warnings) == "55"
    assert len(warnings) == 1 and "assets/55" in warnings[0] and "data/email/legacy/legacyId" in warnings[0]


def test_missing_entity_gives_the_entity_id_and_a_warning():
    warnings = []
    assert resolve_references("{{mcpm#/entities/dataExtensions/de-9/data/name}}", ENTITIES, warnings) == "de-9"
    assert len(warnings) == 1 and "dataExtensions/de-9" in warnings[0]


def test_same_missing_reference_is_reported_once():
    warnings = []
    text = "{{mcpm#/entities/dataExtensions/de-9/data/name}}"
    resolve_references([text, {"a": text}], ENTITIES, warnings)
    assert len(warnings) == 1


def test_other_placeholders_stay_as_written():
    for text in ("{{mcpm:senderProfile}}", "{{mcpm:deliveryProfile}}", "{{DateTime.Now}}",
                 "{{Contact.Attribute.Contacts.Master.\"WAIT1\"}}", "{{Context.ContactKey}}"):
        assert resolve_references(text, ENTITIES) == text


def test_resolution_is_recursive_and_leaves_the_input_unchanged():
    value = {"a": ["{{mcpm#/entities/dataExtensions/de-1/data/name}}", 1, None, True],
             "b": {"c": "x {{mcpm#/entities/dataExtensions/de-2/data/name}} y"}}
    original = copy.deepcopy(value)
    assert resolve_references(value, ENTITIES) == {"a": ["DE_One", 1, None, True], "b": {"c": "x DE_Two y"}}
    assert value == original


def test_numeric_value_is_written_as_text():
    entities = {"assets": {"7": {"data": {"data": {"email": {"legacy": {"legacyId": 999}}}}}}}
    assert resolve_references("{{mcpm#/entities/assets/7/data/data/email/legacy/legacyId}}", entities) == "999"


def test_loaded_package_holds_no_reference():
    package = load()
    for query in package.queries:
        assert "{{mcpm" not in query.sql
    assert "[DE_Master] M" in package.queries[0].sql and "[DE_Config] C" in package.queries[0].sql


def test_loading_a_dict_does_not_change_it():
    export = make_export()
    original = copy.deepcopy(export)
    load_package(export)
    assert export == original


# ---------------------------------------------------------------------------
# Sources and errors
# ---------------------------------------------------------------------------

def test_every_kind_of_source_is_read(tmp_path):
    export = make_export()
    text = json.dumps(export)
    path = tmp_path / "export.json"
    path.write_text(text, encoding="utf-8")
    for source in (export, text, text.encode("utf-8"), "﻿" + text, path, str(path)):
        assert load_package(source).journey.name == "J_Test"


@pytest.mark.parametrize("source", [
    b"{not json", "{not json", "", b"", "[1, 2, 3]", b"\xff\xfe\x00{", "42", '"text"',
])
def test_invalid_json_raises_load_error(source):
    with pytest.raises(LoadError):
        load_package(source)


def test_missing_file_raises_load_error(tmp_path):
    with pytest.raises(LoadError):
        load_package(tmp_path / "missing.json")
    with pytest.raises(LoadError):
        load_package(str(tmp_path / "missing.json"))


def test_unsupported_source_type_raises_load_error():
    with pytest.raises(LoadError):
        load_package(12)


def test_export_without_journey_raises_load_error():
    export = make_export()
    del export["entities"]["journeys"]
    with pytest.raises(LoadError, match="non contiene alcun journey"):
        load_package(export)
    with pytest.raises(LoadError):
        load_package({"name": "not an export"})


def test_journey_without_trigger_raises_load_error():
    export = make_export()
    export["entities"]["journeys"][JOURNEY]["data"]["triggers"] = []
    with pytest.raises(LoadError, match="trigger di ingresso"):
        load_package(export)


def test_missing_entry_event_raises_load_error():
    export = make_export()
    del export["entities"]["eventDefinitions"][EVENT]
    with pytest.raises(LoadError, match="entry event del journey non è nell'export"):
        load_package(export)


def test_missing_entry_table_raises_load_error():
    export = make_export()
    del export["entities"]["dataExtensions"][ENTRY]
    with pytest.raises(LoadError, match="tabella di ingresso dell'entry event"):
        load_package(export)


# ---------------------------------------------------------------------------
# Journey selection
# ---------------------------------------------------------------------------

def with_two_journeys(selected):
    export = make_export()
    export["entities"]["journeys"][OTHER_JOURNEY] = journey("J_Other", [update("ONLY-1", None, [])])
    if selected is None:
        del export["selectedEntities"]
    else:
        export["selectedEntities"]["journeys"] = selected
    return export


def test_selected_journey_is_used():
    package = load_package(with_two_journeys([OTHER_JOURNEY]))
    assert package.journey.name == "J_Other"
    assert not has_warning(package, "seleziona") and not has_warning(package, "nessuno è selezionato")
    assert not has_warning(package, "journey selezionato")


def test_single_journey_is_used_without_selection():
    export = make_export()
    del export["selectedEntities"]
    package = load_package(export)
    assert package.journey.name == "J_Test"
    assert not has_warning(package, "seleziona")


def test_several_journeys_without_selection_use_the_first_with_a_warning():
    package = load_package(with_two_journeys(None))
    assert package.journey.name == "J_Test"
    assert has_warning(package, "contiene 2 journey", "J_Test")


def test_selected_journey_missing_from_the_export_is_reported():
    package = load_package(with_two_journeys(["j-404"]))
    assert package.journey.name == "J_Test"
    assert has_warning(package, "j-404")


def test_references_of_other_journeys_are_not_reported():
    export = with_two_journeys([JOURNEY])
    other = export["entities"]["journeys"][OTHER_JOURNEY]["data"]
    other["activities"][0]["name"] = ref("dataExtensions", "de-404", "name")
    package = load_package(export)
    assert package.journey.name == "J_Test"
    assert not has_warning(package, "de-404")


def test_several_selected_journeys_use_the_first_with_a_warning():
    package = load_package(with_two_journeys([OTHER_JOURNEY, JOURNEY]))
    assert package.journey.name == "J_Other"
    assert has_warning(package, "seleziona 2 journey")


# ---------------------------------------------------------------------------
# Tables, relationships, queries, automations, emails, entry
# ---------------------------------------------------------------------------

def test_tables_with_fields_in_ordinal_order():
    package = load()
    assert list(package.tables) == ["DE_Entry", "DE_Master", "DE_Config", "DE_Log"]
    entry = package.tables["DE_Entry"]
    assert entry.id == ENTRY and entry.customer_key == "KEY-DE_Entry"
    assert entry.field_names == ["ContactId", "CampaignName", "Emailaddress", "Flag"]
    assert entry.is_sendable and entry.sendable_field == "ContactId"
    assert entry.primary_key == ["ContactId", "CampaignName"]
    flag = entry.field("flag")
    assert (flag.type, flag.length, flag.is_nullable, flag.default, flag.ordinal) == ("Boolean", None, True, "False", 3)
    contact = entry.field("ContactId")
    assert (contact.length, contact.is_primary_key, contact.is_nullable, contact.default) == (255, True, False, None)
    assert not package.tables["DE_Config"].is_sendable and package.tables["DE_Config"].sendable_field is None


def test_shared_tables_are_read_and_odd_fields_reported():
    export = make_export()
    shared = table("DE_Shared", [
        {"name": "Id", "type": "Text", "ordinal": 0, "isPrimaryKey": True, "length": "50"},
        {"name": "Spot", "type": "Geolocation", "ordinal": 1, "isPrimaryKey": False, "isNullable": True},
    ])
    export["entities"]["sharedDataExtensions"] = {"de-shared": shared}
    package = load_package(export)
    assert list(package.tables)[-1] == "DE_Shared"
    id_field = package.tables["DE_Shared"].field("Id")
    assert (id_field.length, id_field.is_nullable) == (50, False)    # no isNullable: a primary key is not nullable
    assert has_warning(package, "DE_Shared.Id", "null")
    assert has_warning(package, "DE_Shared.Spot", "Geolocation")


def test_two_tables_with_the_same_name_keep_the_first():
    export = make_export()
    export["entities"]["sharedDataExtensions"] = {"de-copy": table("DE_Log", [field("Other")])}
    package = load_package(export)
    assert package.tables["DE_Log"].id == LOG
    assert has_warning(package, "Due tabelle si chiamano DE_Log")


def test_relationships_resolved_through_attribute_sets():
    package = load()
    assert package.relationships == [
        Relationship("GROUP <- DE_Master", None, None, "DE_Master", "ContactId", "One", "One"),
        Relationship("DE_Master -> DE_Entry", "DE_Master", "ContactId", "DE_Entry", "ContactId", "One", "Many"),
        Relationship("DE_Entry -> DE_Config", "DE_Entry", "CampaignName", "DE_Config", "CampaignName", "Many", "One"),
    ]
    # the field was written "contactid" in the relationship: the schema spelling is kept, with a warning
    assert has_warning(package, "DE_Entry.contactid", "ContactId")


def test_queries_and_automation_steps_in_order():
    package = load()
    assert [(q.id, q.name, q.target_table, q.update_type) for q in package.queries] == [
        (Q_ENTRY, "Q_Entry", "DE_Entry", "Update"),
        (Q_CHECK, "Q_Check", "DE_Log", "Overwrite"),
    ]
    assert package.queries[0].sql == ("SELECT M.ContactId, C.CampaignName FROM [DE_Master] M JOIN [DE_Config] C "
                                      "ON M.ContactId = C.CampaignName WHERE C.ACTIVE = 'True'")
    # step order, data verification (objectTypeId 1000) skipped
    assert package.automation_queries == {"A_Entry": ["Q_Check", "Q_Entry"]}


def test_automation_query_missing_from_the_export_is_reported():
    export = make_export()
    del export["entities"]["queryActivities"][Q_CHECK]
    package = load_package(export)
    assert package.automation_queries == {"A_Entry": [Q_CHECK, "Q_Entry"]}
    assert has_warning(package, "A_Entry", Q_CHECK)


def test_email_assets_and_links():
    package = load()
    assert set(package.emails) == {ASSET_LINK, ASSET_PLAIN}
    welcome, plain = package.emails[ASSET_LINK], package.emails[ASSET_PLAIN]
    assert (welcome.name, welcome.subject, welcome.has_link) == ("Welcome", "Hello", True)
    assert (plain.name, plain.subject, plain.has_link) == ("Plain", "Plain news", False)


def test_link_in_text_view_or_template_slot_counts():
    export = make_export()
    views = export["entities"]["assets"][ASSET_PLAIN]["data"]["views"]
    views["text"]["content"] = 'Go to <a  href="https://example.com">'
    assert load_package(export).emails[ASSET_PLAIN].has_link is True
    views["text"]["content"] = ""
    views["html"]["slots"] = {"main": {"blocks": {"b1": {"content": '<a href="https://example.com">x</a>'}}}}
    assert load_package(export).emails[ASSET_PLAIN].has_link is True


def test_entry_event_and_automation():
    package = load()
    event = package.entry_event
    assert (event.key, event.name, event.table, event.automation_name) == (
        "DEAudience-1", "Entry event", "DE_Entry", "A_Entry")
    journey_ = package.journey
    assert (journey_.name, journey_.key) == ("J_Test", "key-J_Test")
    assert (journey_.entry_table, journey_.entry_event_key) == ("DE_Entry", "DEAudience-1")
    assert journey_.default_email_event_key == "DEAudience-1"
    assert (journey_.default_email_field, journey_.default_mobile_field) == ("Emailaddress", "Mobile")


def test_default_mobile_number_is_read_like_the_default_email():
    """Spec, step 3, "SMS sends": the journey's default mobile number is defaults.mobileNumber, "a column of the entry
    table, as defaults.email for emails"."""
    export = make_export()
    defaults = export["entities"]["journeys"][JOURNEY]["data"]["defaults"]
    defaults["mobileNumber"] = ["{{Event." + ref("eventDefinitions", EVENT, "eventDefinitionKey") + '."Phone 2"}}']
    package = load_package(export)
    assert package.journey.default_mobile_field == "Phone 2"
    assert has_warning(package, "valore di default mobileNumber", "Phone 2", "non ha questo campo")
    del defaults["mobileNumber"]
    assert load_package(export).journey.default_mobile_field is None


def test_default_reading_another_entry_event_is_reported():
    package = load()
    assert has_warning(package, "mobileNumber", "DEAudience-2", "Old event", "DEAudience-1")
    assert not has_warning(package, "valore di default email")


def test_default_email_reading_another_entry_event():
    export = make_export()
    export["entities"]["journeys"][JOURNEY] = journey("J_Test", default_activities(), mobile_event_id=EVENT)
    export["entities"]["journeys"][JOURNEY]["data"]["defaults"]["email"] = [
        "{{Event." + ref("eventDefinitions", OLD_EVENT, "eventDefinitionKey") + '."Emailaddress"}}']
    package = load_package(export)
    assert package.journey.default_email_event_key == "DEAudience-2"
    assert has_warning(package, "valore di default email", "DEAudience-2")
    assert not has_warning(package, "mobileNumber", "non dall'entry event")
    # Spec, "Honest outcomes": "A journey default (email, mobile number) that names a column the entry data
    # extension lacks is reported": mobileNumber now reads the trigger's event, whose table has no Mobile.
    assert has_warning(package, "valore di default mobileNumber", "Mobile", "DE_Entry", "non ha questo campo")


def test_entry_event_found_by_key_when_the_id_is_missing():
    export = make_export()
    trigger = export["entities"]["journeys"][JOURNEY]["data"]["triggers"][0]
    trigger["metaData"]["eventDefinitionId"] = "unknown-id"
    assert load_package(export).entry_event.name == "Entry event"


# ---------------------------------------------------------------------------
# Journey and activities
# ---------------------------------------------------------------------------

def test_activity_kinds_and_order():
    package = load()
    activities = package.journey.activities
    assert list(activities) == ["DECISION-1", "EMAIL-1", "WAITATTR-1", "ENGAGE-1", "ENGAGE-2", "WAITDUR-1",
                                "STO-1", "EMAIL-2", "RANDOM-1", "UPDATE-1", "DECISION-2"]
    assert {key: a.kind for key, a in activities.items()} == {
        "DECISION-1": KIND_DECISION, "EMAIL-1": KIND_EMAIL, "WAITATTR-1": KIND_WAIT_ATTRIBUTE,
        "ENGAGE-1": KIND_ENGAGEMENT, "ENGAGE-2": KIND_ENGAGEMENT, "WAITDUR-1": KIND_WAIT_DURATION,
        "STO-1": KIND_WAIT_STO, "EMAIL-2": KIND_EMAIL, "RANDOM-1": KIND_RANDOM, "UPDATE-1": KIND_UPDATE,
        "DECISION-2": KIND_DECISION,
    }
    assert activities["DECISION-1"].raw_type == "MULTICRITERIADECISION"
    assert package.journey.activity_count == 11
    assert package.unsupported_activities == []


def test_start_activity_is_the_one_nothing_points_to():
    assert load().journey.start_key == "DECISION-1"


def test_several_start_candidates_take_the_first_with_a_warning():
    activities = default_activities()
    activities.insert(0, update("ORPHAN-1", None, [write(LOG, "Note", "x")]))
    package = load(activities)
    assert package.journey.start_key == "ORPHAN-1"
    assert has_warning(package, "ORPHAN-1", "DECISION-1", "collegamenti in entrata")


def test_decision_outcomes_formula_and_remainder():
    channel = load().journey.activities["DECISION-1"]
    assert channel.name == "Channel"
    assert [(o.key, o.label, o.next, o.is_remainder) for o in channel.outcomes] == [
        ("path_email", "Email", "EMAIL-1", False),
        ("path_sms", "SMS", "UPDATE-1", False),
        ("remainder_path", "Remainder", None, True),
    ]
    # the attribute set "Config Set" is mapped to its table through storageName; its attribute path (contact ->
    # DE_Master -> DE_Entry -> DE_Config) gives the tables between the contact and DE_Config (spec, step 3)
    assert channel.outcomes[0].formula == Condition("DE_Config", "CHANNEL", "Equal", "Email",
                                                    path=("DE_Master", "DE_Entry"))
    assert channel.outcomes[1].formula == Condition("DE_Config", "CHANNEL", "Equal", "SMS",
                                                    path=("DE_Master", "DE_Entry"))
    assert channel.outcomes[2].formula is None
    assert channel.outcomes[0].description == "CHANNEL equal Email"


def test_decision_nested_formula_and_label_fallback():
    consent = load().journey.activities["DECISION-2"]
    first = consent.outcomes[0]
    # DE_Master is the table linked to the contact: its path is empty
    assert first.formula == ConditionGroup("AND", (
        Condition("DE_Master", "Perm", "Is", "true"),
        ConditionGroup("OR", (
            Condition("DE_Master", "Opt In", "Is", "false"),
            Condition("DE_Master", "Opt In", "IsNull", None),
        )),
    ))
    # no metaData.label: the criteria description is the label
    assert first.label == "Perm is True AND ( Opt In is False OR Opt In is null )"


def test_condition_on_unknown_attribute_set_keeps_the_name_with_a_warning():
    activities = default_activities()
    activities[0]["configurationArguments"]["criteria"]["path_sms"] = filter_definition(
        '<Condition Key="DE_Log.Note" Operator="Equal"><Value><![CDATA[x]]></Value></Condition>')
    package = load(activities)
    assert package.journey.activities["DECISION-1"].outcomes[1].formula == Condition("DE_Log", "Note", "Equal", "x")
    assert has_warning(package, "Nessun attribute set si chiama DE_Log")


def test_condition_field_spelling_follows_the_schema():
    activities = default_activities()
    activities[0]["configurationArguments"]["criteria"]["path_sms"] = filter_definition(
        condition(S_CONFIG, "channel", "Equal", "SMS"))
    package = load(activities)
    assert package.journey.activities["DECISION-1"].outcomes[1].formula.field == "CHANNEL"
    assert has_warning(package, "DE_Config.channel", "CHANNEL")


def test_condition_on_a_field_missing_from_the_schema_is_reported():
    activities = default_activities()
    activities[0]["configurationArguments"]["criteria"]["path_sms"] = filter_definition(
        condition(S_CONFIG, "CHANNEL9", "Equal", "SMS"))
    package = load(activities)
    # Spec: "the rest is reported, not guessed": a field missing from the schema cannot be typed,
    # so that outcome is unsupported; outcomes before it stay coverable (first match wins).
    channel = package.journey.activities["DECISION-1"]
    assert channel.kind == KIND_DECISION
    assert channel.outcomes[0].unsupported_reason is None
    assert "CHANNEL9" in channel.outcomes[1].unsupported_reason
    assert has_warning(package, "CHANNEL9", "DE_Config")


def test_unreadable_criteria_make_the_split_unsupported():
    activities = default_activities()
    activities[0]["configurationArguments"]["criteria"]["path_sms"] = "<FilterDefinition><broken"
    package = load(activities)
    channel = package.journey.activities["DECISION-1"]
    assert channel.kind == KIND_UNSUPPORTED and channel.raw_type == "MULTICRITERIADECISION"
    assert all(o.formula is None and not o.is_remainder for o in channel.outcomes)
    assert package.unsupported_activities == ["DECISION-1"]
    assert has_warning(package, "DECISION-1", "non supportata", "SMS")


def test_unsupported_operator_is_reported():
    # Between was the example here until the extension section of the spec added it to the operators.
    activities = default_activities()
    activities[0]["configurationArguments"]["criteria"]["path_sms"] = filter_definition(
        condition(S_CONFIG, "CHANNEL", "NotContains", "A"))
    package = load(activities)
    channel = package.journey.activities["DECISION-1"]
    # Spec: an unknown operator is "listed in the report, and the branches behind it are marked as
    # not covered"; the outcome is unsupported instead of crashing the generation.
    assert channel.kind == KIND_DECISION
    assert "NotContains" in channel.outcomes[1].unsupported_reason
    assert has_warning(package, "DECISION-1", "NotContains", "non supporta")


def test_decision_without_remainder_is_reported():
    activities = default_activities()
    activities[0]["outcomes"] = activities[0]["outcomes"][:2]
    package = load(activities)
    assert has_warning(package, "DECISION-1", "remainder")


def test_engagement_splits_open_and_click():
    activities = load().journey.activities
    opened, clicked = activities["ENGAGE-1"], activities["ENGAGE-2"]
    assert (opened.engagement_kind, opened.engagement_ref) == ("open", "EMAIL-1")
    assert (clicked.engagement_kind, clicked.engagement_ref) == ("click", "EMAIL-1")
    assert [(o.key, o.label, o.when, o.next) for o in opened.outcomes] == [
        ("branchResult-1", "Yes", True, "ENGAGE-2"), ("branchResult-2", "No", False, "RANDOM-1")]
    assert [o.next for o in clicked.outcomes] == ["WAITDUR-1", None]
    assert ENGAGEMENT_KINDS == {2: "open", 3: "click"}


def test_engagement_split_with_unknown_statistics_type():
    activities = default_activities()
    activities[3]["configurationArguments"]["statsTypeId"] = 9
    package = load(activities)
    split = package.journey.activities["ENGAGE-1"]
    assert split.kind == KIND_ENGAGEMENT and split.engagement_kind is None
    assert has_warning(package, "ENGAGE-1", "9")


def test_engagement_statistics_type_written_as_text():
    activities = default_activities()
    activities[3]["configurationArguments"]["statsTypeId"] = "3"
    assert load(activities).journey.activities["ENGAGE-1"].engagement_kind == "click"


def test_engagement_split_referencing_a_missing_email_is_reported():
    activities = default_activities()
    activities[4]["configurationArguments"]["refActivityCustomerKey"] = "EMAIL-404"
    package = load(activities)
    assert package.journey.activities["ENGAGE-2"].engagement_ref == "EMAIL-404"
    assert has_warning(package, "ENGAGE-2", "EMAIL-404")


def test_click_split_on_specific_links_is_reported():
    activities = default_activities()
    activities[4]["configurationArguments"]["engagementUrls"] = {"urls": ["https://example.com/offer"]}
    assert has_warning(load(activities), "ENGAGE-2", "https://example.com/offer")


def test_random_split_percentages():
    random_split = load().journey.activities["RANDOM-1"]
    assert [(o.key, o.label, o.percentage, o.next) for o in random_split.outcomes] == [
        ("branchResult-1", "90%", 90, "UPDATE-1"), ("branchResult-2", "10%", 10, None)]


def test_random_split_percentages_not_adding_up_are_reported():
    activities = default_activities()
    activities[8]["outcomes"][1]["arguments"]["percentage"] = 20
    assert has_warning(load(activities), "RANDOM-1", "100")


def test_wait_by_duration():
    wait = load().journey.activities["WAITDUR-1"]
    assert (wait.wait_amount, wait.wait_unit, wait.wait_timezone, wait.wait_field) == (
        2, "HOURS", "W. Europe Standard Time", None)


def test_wait_by_attribute_reads_the_field_from_the_expression():
    wait = load().journey.activities["WAITATTR-1"]
    assert wait.wait_field == ("DE_Config", "WAIT1")
    assert wait.wait_timezone == "W. Europe Standard Time"


def test_wait_by_attribute_falls_back_to_the_attribute_id():
    activities = default_activities()
    activities[2]["configurationArguments"]["waitEndDateAttributeExpression"] = ""
    package = load(activities)
    assert package.journey.activities["WAITATTR-1"].wait_field == ("DE_Config", "WAIT1")


def test_wait_by_attribute_without_any_field_is_reported():
    activities = default_activities()
    activities[2]["configurationArguments"]["waitEndDateAttributeExpression"] = ""
    activities[2]["metaData"].pop("attribute")
    package = load(activities)
    assert package.journey.activities["WAITATTR-1"].wait_field is None
    assert has_warning(package, "WAITATTR-1", "non è possibile identificare")


def test_wait_by_attribute_offset_and_time_of_day():
    """Spec, step 3, "The clock": the wait holds the contact "until the date in its field plus its offset (waitDuration
    and waitUnit, which may be negative ...), at the time of day given by specifiedTime when it has one"."""
    wait = load().journey.activities["WAITATTR-1"]
    assert (wait.wait_offset_amount, wait.wait_offset_unit, wait.wait_time_of_day) == (None, None, None)
    activities = default_activities()
    activities[2]["configurationArguments"].update(waitDuration=-13, waitUnit="DAYS", specifiedTime="00:01")
    package = load(activities)
    wait = package.journey.activities["WAITATTR-1"]
    assert (wait.wait_offset_amount, wait.wait_offset_unit, wait.wait_time_of_day) == (-13, "DAYS", "00:01")
    # the offset is modelled now: no warning about it
    assert not has_warning(package, "WAITATTR-1")
    activities[2]["configurationArguments"].update(waitDuration="2", waitUnit="Weeks", specifiedTime="")
    package = load(activities)
    wait = package.journey.activities["WAITATTR-1"]
    assert (wait.wait_offset_amount, wait.wait_offset_unit, wait.wait_time_of_day) == (2, "Weeks", None)
    assert not has_warning(package, "WAITATTR-1")


def test_wait_by_attribute_offset_in_an_unknown_unit_is_reported():
    activities = default_activities()
    activities[2]["configurationArguments"].update(waitDuration=3, waitUnit="FORTNIGHTS")
    package = load(activities)
    wait = package.journey.activities["WAITATTR-1"]
    assert (wait.kind, wait.wait_offset_amount, wait.wait_offset_unit) == (KIND_WAIT_ATTRIBUTE, 3, "FORTNIGHTS")
    assert has_warning(package, "WAITATTR-1", "'FORTNIGHTS'", "non è possibile calcolare quando il wait termina")


def test_wait_by_attribute_path_from_the_expression():
    """Spec, step 3, "Tables reached through other tables": the segments of the wait's attribute expression name the
    path. ``Contacts`` (not an attribute set) is left out, and so is the set of the field itself."""
    wait = load().journey.activities["WAITATTR-1"]
    # {{Contact.Attribute.Contacts.Master Set.DE_Entry.Config Set."WAIT1"}}, the pilot's shape
    assert wait.wait_path == ("DE_Master", "DE_Entry")
    activities = default_activities()
    config = activities[2]["configurationArguments"]
    config["waitEndDateAttributeExpression"] = '{{Contact.Attribute.Contacts.Config Set."WAIT1"}}'
    assert load(activities).journey.activities["WAITATTR-1"].wait_path == ()
    config["waitEndDateAttributeExpression"] = '{{Contact.Attribute.GROUP.DE_Entry.Config Set."WAIT1"}}'
    assert load(activities).journey.activities["WAITATTR-1"].wait_path == ("DE_Entry",)
    # no expression: the field comes from metaData.attribute.id, which gives no path
    config["waitEndDateAttributeExpression"] = ""
    wait = load(activities).journey.activities["WAITATTR-1"]
    assert (wait.wait_field, wait.wait_path) == (("DE_Config", "WAIT1"), ())


def test_wait_by_attribute_path_through_a_link_table():
    """The public holiday shape of the step 3 exports: {{Contact.Attribute.Contacts.PublicHoliday_Entry.PublicHoliday_DE.
    "PHEnd"}} reads PublicHoliday_DE through PublicHoliday_Entry."""
    export = with_public_holidays(make_export())
    activities = export["entities"]["journeys"][JOURNEY]["data"]["activities"]
    activities[2]["configurationArguments"]["waitEndDateAttributeExpression"] = (
        '{{Contact.Attribute.Contacts.PublicHoliday_Entry.PublicHoliday_DE."PHEnd"}}')
    wait = load_package(export).journey.activities["WAITATTR-1"]
    assert (wait.wait_field, wait.wait_path) == (("DE_Holiday", "PHEnd"), ("DE_Holiday_Entry",))


@pytest.mark.parametrize("meta, key, config, expected", [
    ({}, "WAITATTR-1", {"waitEndDateAttributeExpression": '{{Contact.Attribute.Contacts.Config Set."WAIT1"}}'},
     KIND_WAIT_ATTRIBUTE),
    ({"uiType": ""}, "WAITBYATTRIBUTE-7", {"waitEndDateAttributeExpression": ""}, KIND_WAIT_ATTRIBUTE),
    ({}, "WAITBYDURATION-8", {"waitDuration": 1, "waitUnit": "DAYS", "waitEndDateAttributeExpression": ""},
     KIND_WAIT_DURATION),
])
def test_wait_without_ui_type_is_told_apart_by_its_expression_or_key(meta, key, config, expected):
    attribute_wait = default_activities()[2]
    raw = activity(key, "WAIT", [outcome(f"{key}-o", "ENGAGE-1")], "wait", config=config,
                   meta=dict(meta, attribute=attribute_wait["metaData"]["attribute"]))
    activities = default_activities()
    activities[2] = raw
    activities[1]["outcomes"][0]["next"] = key
    package = load(activities)
    assert package.journey.activities[key].kind == expected
    assert not has_warning(package, key)


def test_wait_that_nothing_identifies_is_unsupported():
    activities = default_activities()
    activities[5]["metaData"].pop("uiType")
    activities[5]["key"] = "PAUSE-1"
    activities[4]["outcomes"][0]["next"] = "PAUSE-1"
    package = load(activities)
    assert package.journey.activities["PAUSE-1"].kind == KIND_UNSUPPORTED
    assert package.unsupported_activities == ["PAUSE-1"]
    assert has_warning(package, "PAUSE-1", "metaData.uiType", "non supportata")


def test_wait_with_unknown_ui_type_is_unsupported():
    activities = default_activities()
    activities[5]["metaData"]["uiType"] = "WAITUNTILDATE"
    package = load(activities)
    assert package.journey.activities["WAITDUR-1"].kind == KIND_UNSUPPORTED
    assert package.unsupported_activities == ["WAITDUR-1"]
    assert has_warning(package, "WAITDUR-1", "WAITUNTILDATE")


def test_send_time_wait():
    wait = load().journey.activities["STO-1"]
    assert (wait.kind, wait.wait_amount, wait.wait_unit) == (KIND_WAIT_STO, 24, "HOURS")


def test_email_with_and_without_link():
    package = load()
    first, second = package.journey.activities["EMAIL-1"], package.journey.activities["EMAIL-2"]
    # EMAIL-1 names the asset through a legacyId path the asset does not have: its entity id is used
    assert (first.email_asset_id, first.email_name) == (ASSET_LINK, "Welcome")
    assert package.emails[first.email_asset_id].has_link is True
    assert has_warning(package, f"assets/{ASSET_LINK}", "legacyId")
    # EMAIL-2 names the asset by its legacy id
    assert (second.email_asset_id, second.email_name) == (ASSET_PLAIN, "Plain")
    assert package.emails[second.email_asset_id].has_link is False


def test_email_whose_asset_is_missing_uses_the_activity_name():
    activities = default_activities()
    activities[7]["configurationArguments"]["triggeredSend"]["emailId"] = "777"
    package = load(activities)
    second = package.journey.activities["EMAIL-2"]
    assert (second.email_asset_id, second.email_name) == ("777", "Second mail")
    assert has_warning(package, "EMAIL-2", "777")


def test_update_contact_writes():
    package = load()
    log = package.journey.activities["UPDATE-1"]
    assert log.writes == [
        Write("DE_Log", "Note", "free text"),
        Write("DE_Log", "Counter", "1"),                 # a number is kept as written, as text
        Write("DE_Log", "Stamp", "{{DateTime.Now}}"),    # kept literally
        Write("DE_Entry", "Flag", "True"),               # field spelled "flag" in the export
    ]
    assert has_warning(package, "DE_Entry.flag", "Flag")


def test_update_contact_on_unknown_table_is_reported():
    activities = default_activities()
    activities[9]["arguments"]["activityData"]["updateContactFields"].append(write("de-404", "X", "y"))
    package = load(activities)
    assert package.journey.activities["UPDATE-1"].writes[-1] == Write("de-404", "X", "y")
    assert has_warning(package, "UPDATE-1", "de-404")


def test_unsupported_activity_type():
    activities = default_activities()
    activities.insert(5, activity("PUSH-1", "PUSHNOTIFICATIONACTIVITY", [outcome("PUSH-1-o", None)], "Send push"))
    activities[4]["outcomes"][0]["next"] = "PUSH-1"
    package = load(activities)
    push = package.journey.activities["PUSH-1"]
    assert (push.kind, push.raw_type, push.name) == (KIND_UNSUPPORTED, "PUSHNOTIFICATIONACTIVITY", "Send push")
    assert [o.key for o in push.outcomes] == ["PUSH-1-o"]
    assert package.unsupported_activities == ["PUSH-1"]


def sms_activity(key, next_, asset_id="902566", name="SMS_Reminder"):
    """An SMSSYNC activity as the step 3 exports write it (configurationArguments trimmed)."""
    return activity(key, "SMSSYNC", [outcome(f"{key}-o", next_)], name,
                    config={"applicationExtensionKey": "jb-mobile-connect-send-sms-sync",
                            "assetId": ref("assets", asset_id, "id"), "codeId": "{{mcpm:smsCode}}",
                            "isUsingDEAttributesForPersonalization": True},
                    meta={"isConfigured": True})


def test_sms_send():
    """Spec, step 3, "SMS sends": "SMSSYNC is a send to the phone number of the journey's default mobile number ... It
    has no branching." """
    activities = default_activities()
    activities.insert(5, sms_activity("SMSSYNC-1", "WAITDUR-1"))
    activities[4]["outcomes"][0]["next"] = "SMSSYNC-1"
    export = make_export(activities)
    export["entities"]["assets"]["902566"] = entity({"name": "SMS_Reminder", "assetType": {"name": "jsonmessage"}})
    package = load_package(export)
    sms = package.journey.activities["SMSSYNC-1"]
    assert (sms.kind, sms.raw_type, sms.sms_asset_id, sms.sms_name) == (KIND_SMS, "SMSSYNC", "902566", "SMS_Reminder")
    assert [o.next for o in sms.outcomes] == ["WAITDUR-1"]
    assert package.unsupported_activities == []
    assert not has_warning(package, "SMSSYNC-1")


def test_issues_on_the_sms_asset_the_journey_sends_are_kept():
    activities = default_activities()
    activities.insert(5, sms_activity("SMSSYNC-1", "WAITDUR-1"))
    activities[4]["outcomes"][0]["next"] = "SMSSYNC-1"
    export = make_export(activities)
    assets = export["entities"]["assets"]
    assets["902566"] = entity({"name": "SMS_Reminder", "assetType": {"name": "jsonmessage"}})
    assets["902566"]["issues"] = [{"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "SMS_ODD"}]
    assets["902567"] = entity({"name": "Unused SMS", "assetType": {"name": "jsonmessage"}})
    assets["902567"]["issues"] = [{"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "UNUSED"}]
    package = load_package(export)
    assert has_warning(package, "sull'asset SMS_Reminder (902566)", "SMS_ODD")
    assert not has_warning(package, "UNUSED")


def test_link_to_a_missing_activity_is_reported():
    activities = default_activities()
    activities[5]["outcomes"][0]["next"] = "NOWHERE-1"
    assert has_warning(load(activities), "WAITDUR-1", "NOWHERE-1")


def test_hand_built_export_warnings_are_the_expected_ones():
    package = load()
    expected = [
        ("assets/111", "legacyId"),             # EMAIL-1 names its asset through a path the asset lacks
        ("DE_Entry.contactid", "ContactId"),    # relationship field spelled differently from the schema
        ("DE_Entry.flag", "Flag"),              # update-contact field spelled differently from the schema
        ("mobileNumber", "DEAudience-2"),       # journey default reading another entry event
    ]
    assert len(package.warnings) == len(expected), package.warnings
    for fragments in expected:
        assert has_warning(package, *fragments), fragments


# ---------------------------------------------------------------------------
# Extensions beyond the pilot: conditions
# ---------------------------------------------------------------------------

def journey_condition(field_name, operator, value="", event_id=EVENT):
    """A Journey Data condition as the real exports write it: Event.<eventDefinitionKey>.<field>."""
    key = "Event.%s.%s" % (ref("eventDefinitions", event_id, "eventDefinitionKey"), field_name)
    return (f'<Condition IsEphemeralAttribute="true" Key="{key}" Operator="{operator}" UiMetaData="{{}}">'
            f"<Value><![CDATA[{value}]]></Value></Condition>")


def comparison(set_id, field_name, operator, parameter):
    """An attribute compared with another attribute (ValueParameterName, empty <Value>)."""
    return (f'<Condition Key="{ref("attributeSets", set_id, "name")}.{field_name}" Operator="{operator}" '
            f'UiMetaData="{{}}" ValueParameterName="{parameter}"><Value><![CDATA[]]></Value></Condition>')


def load_sms_path(xml, export=None):
    """Load the hand-built export with the SMS outcome of DECISION-1 reading ``xml``; returns (package, outcome)."""
    export = export or make_export()
    criteria = export["entities"]["journeys"][JOURNEY]["data"]["activities"][0]["configurationArguments"]["criteria"]
    criteria["path_sms"] = filter_definition(xml)
    package = load_package(export)
    return package, package.journey.activities["DECISION-1"].outcomes[1]


def test_journey_data_reads_the_trigger_entry_table():
    package, sms = load_sms_path(journey_condition("flag", "Is", "true"))
    # Spec: Event.<eventDefinitionKey>.<Field> "reads column Field of the data extension of the trigger's
    # entry event"; the source is kept for the snapshot rule.
    assert sms.formula == Condition("DE_Entry", "Flag", "Is", "true", source=SOURCE_JOURNEY)
    assert sms.unsupported_reason is None
    assert has_warning(package, "DE_Entry.flag", "Flag")
    assert not has_warning(package, "Nessun attribute set si chiama Event")


def test_journey_data_of_another_event_is_unsupported():
    package, sms = load_sms_path(journey_condition("CampaignName", "Equal", "X", event_id=OLD_EVENT))
    # Spec: "A key naming an event definition other than the trigger's is unsupported."
    assert sms.formula.source == SOURCE_JOURNEY
    assert "DEAudience-2" in sms.unsupported_reason and "Old event" in sms.unsupported_reason
    assert "DEAudience-1" in sms.unsupported_reason
    assert has_warning(package, "DECISION-1", "DEAudience-2", "non coperti")


def test_attribute_comparison_on_the_linked_key_reads_row_present():
    package, sms = load_sms_path(comparison(S_MASTER, "ContactId", "Equal", "Event.DEAudience-1.ContactId"))
    # Spec: supported only as "key field of a contact-level linked table Equal the contact key".
    assert sms.formula == Condition("DE_Master", "ContactId", "Equal", None, source=SOURCE_CONTACT,
                                    value_attribute="Event.DEAudience-1.ContactId")
    assert sms.unsupported_reason is None


def test_mirror_attribute_comparison_is_normalised():
    key = "Event.%s.ContactId" % ref("eventDefinitions", EVENT, "eventDefinitionKey")
    xml = (f'<Condition IsEphemeralAttribute="true" Key="{key}" Operator="Equal" ValueIsReference="true">'
           f"<Value><![CDATA[Master Set.ContactId]]></Value></Condition>")
    package, sms = load_sms_path(xml)
    assert sms.formula == Condition("DE_Master", "ContactId", "Equal", None, source=SOURCE_CONTACT,
                                    value_attribute="Event.DEAudience-1.ContactId")
    assert sms.unsupported_reason is None


@pytest.mark.parametrize("set_id, field_name, operator, parameter", [
    (S_MASTER, "Mobile", "Equal", "Event.DEAudience-1.ContactId"),          # not the field linked to the contact
    (S_MASTER, "ContactId", "Equal", "Event.DEAudience-1.Emailaddress"),    # not the contact key
    (S_MASTER, "ContactId", "NotEqual", "Event.DEAudience-1.ContactId"),    # not Equal
    (S_MASTER, "ContactId", "Equal", "Event.DEAudience-2.ContactId"),       # another entry event
    (S_CONFIG, "CampaignName", "Equal", "Event.DEAudience-1.CampaignName"),  # scenario-level table
    (S_MASTER, "ContactId", "Equal", "Config Set.CampaignName"),             # two Contact Data fields
])
def test_other_attribute_comparisons_are_unsupported(set_id, field_name, operator, parameter):
    package, sms = load_sms_path(comparison(set_id, field_name, operator, parameter))
    # Spec: "Any other form is unsupported and reported; it is never read as a comparison with an empty value."
    assert sms.formula.value is None and sms.formula.value_attribute == parameter
    assert "confronta" in sms.unsupported_reason and parameter in sms.unsupported_reason
    assert has_warning(package, "DECISION-1", parameter, "non coperti")


def test_between_is_supported_when_written_low_high():
    _, sms = load_sms_path(condition(S_CONFIG, "CHANNEL", "Between", "A|M"))
    assert sms.formula == Condition("DE_Config", "CHANNEL", "Between", "A|M")
    assert sms.unsupported_reason is None


def test_between_value_not_written_low_high_is_unsupported():
    package, sms = load_sms_path(condition(S_CONFIG, "CHANNEL", "Between", "A"))
    assert "Between" in sms.unsupported_reason and "basso|alto" in sms.unsupported_reason
    assert has_warning(package, "DECISION-1", "Between", "'A'", "non coperti")


def test_attribute_set_without_relationship_is_unsupported():
    export = make_export()
    del export["entities"]["attributeRelationships"]["rel-ec"]     # the only relationship naming Config Set
    package = load_package(export)
    channel = package.journey.activities["DECISION-1"]
    # Spec: "An attribute set read by a split with no relationship in the export: its conditions are
    # unsupported, with the reason 'relationship missing in the export'."
    assert all("relazione mancante nell'export" in o.unsupported_reason for o in channel.outcomes[:2])
    assert package.missing_relationships == ["DE_Config"]
    assert package.journey.activities["DECISION-2"].outcomes[0].unsupported_reason is None
    assert has_warning(package, "DECISION-1", "relazione mancante nell'export")


def test_pilot_shape_has_no_missing_relationship():
    assert load().missing_relationships == []


# ---------------------------------------------------------------------------
# Extensions beyond the pilot: issues, asset warnings, schema
# ---------------------------------------------------------------------------

def test_package_manager_issues_of_level_warning_or_error_are_reported():
    export = make_export()
    entities = export["entities"]
    entities["journeys"][JOURNEY]["issues"] = [
        {"type": "ENTITY_NOT_FOUND", "level": "WARNING",
         "cause": {"type": "ATTRIBUTE_GROUP", "level": "ERROR", "message": "Unable to find attribute relationship"},
         "entity": {"entityType": "attributeRelationships", "retrievalProperty": "id", "value": "rel-404",
                    "locationInformation": {"activityName": "Channel", "activityType": "MULTICRITERIADECISION"}}},
        {"type": "UNSUPPORTED_AMPSCRIPT", "level": "INFO", "issue": "UNRESOLVED_VARIABLE"},
    ]
    entities["dataExtensions"][LOG]["issues"] = [
        {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "SOMETHING_ODD"}]
    export["issues"] = [{"type": "UNSUPPORTED_FUNCTIONALITY", "level": "ERROR", "functionality": "PACKAGE_LEVEL"}]
    package = load_package(export)
    # The level is the most severe of the issue and its cause (team decision, 13 October 2026): a WARNING issue
    # with an ERROR cause is reported as an error.
    assert has_warning(package, "Package Manager segnala un errore sul journey J_Test", "attributeRelationships",
                       "rel-404", "'Channel'", "Messaggio di Package Manager: Unable to find attribute relationship")
    assert has_warning(package, "Package Manager segnala un warning sulla data extension DE_Log", "SOMETHING_ODD")
    assert has_warning(package, "Package Manager segnala un errore sul package", "PACKAGE_LEVEL")
    assert not has_warning(package, "UNRESOLVED_VARIABLE")


def test_issues_about_assets_the_journey_does_not_send_are_dropped():
    export = make_export()
    assets = export["entities"]["assets"]
    assets["333"] = entity({"name": "Unused block", "assetType": {"name": "htmlblock"}, "views": {}})
    assets["333"]["issues"] = [{"type": "ENTITY_NOT_FOUND", "level": "WARNING",
                                "entity": {"entityType": "dataExtensions", "retrievalProperty": "name",
                                           "value": "Lookup_DE"}}]
    export["entities"]["landingPages"] = {"lp-1": entity({"name": "Form page"})}
    export["entities"]["landingPages"]["lp-1"]["issues"] = [
        {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "UNSUPPORTED_BLOCK"}]
    assets[ASSET_LINK]["issues"] = [
        # an image of the sent email: not used for routing or sending
        {"type": "ENTITY_NOT_FOUND", "level": "WARNING",
         "entity": {"entityType": "assets", "retrievalProperty": "fileProperties.publishedURL",
                    "value": "https://image.example.com/x.png"}},
        # a table the sent email looks up: about the sent asset itself
        {"type": "ENTITY_NOT_FOUND", "level": "WARNING",
         "entity": {"entityType": "dataExtensions", "retrievalProperty": "name", "value": "Content_DE"}},
    ]
    package = load_package(export)
    assert not has_warning(package, "Lookup_DE")
    assert not has_warning(package, "UNSUPPORTED_BLOCK")
    assert not has_warning(package, "image.example.com")
    assert has_warning(package, "sull'asset Welcome (111)", "Content_DE")


def test_issues_about_content_used_only_indirectly_are_kept_only_when_errors():
    """Team decision of 13 October 2026 (docs/progress.md): on content the journey uses only indirectly (an
    image inside a sent email, a landing page linked from it) only the issues of level error stay; on content
    the journey does not use at all every issue is dropped."""
    export = make_export()
    entities = export["entities"]
    assets = entities["assets"]
    assets[ASSET_LINK]["data"]["views"]["html"]["content"] += (
        f'<a href="{ref("landingPages", "lp-1", "name")}">form</a>')
    entities["landingPages"] = {"lp-1": entity({"name": "Linked page"}), "lp-2": entity({"name": "Lone page"})}
    entities["landingPages"]["lp-1"]["issues"] = [
        {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "LINKED_WARNING"},
        {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "ERROR", "functionality": "LINKED_ERROR"}]
    entities["landingPages"]["lp-2"]["issues"] = [
        {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "ERROR", "functionality": "LONE_ERROR"}]
    assets[ASSET_LINK]["issues"] = [
        {"type": "ENTITY_NOT_FOUND", "level": "WARNING",
         "entity": {"entityType": "assets", "retrievalProperty": "fileProperties.publishedURL",
                    "value": "https://image.example.com/warning.png"}},
        {"type": "ENTITY_NOT_FOUND", "level": "WARNING", "cause": {"level": "ERROR"},
         "entity": {"entityType": "assets", "retrievalProperty": "fileProperties.publishedURL",
                    "value": "https://image.example.com/error.png"}}]
    package = load_package(export)
    assert has_warning(package, "Package Manager segnala un errore sulla landing page Linked page", "LINKED_ERROR")
    assert not has_warning(package, "LINKED_WARNING")
    assert not has_warning(package, "LONE_ERROR")
    assert has_warning(package, "Package Manager segnala un errore sull'asset Welcome (111)", "error.png")
    assert not has_warning(package, "warning.png")


def test_reference_warnings_about_assets_the_journey_does_not_send_are_dropped():
    export = make_export()
    assets = export["entities"]["assets"]
    image = ref("assets", "555", "fileProperties/publishedURL")
    assets["333"] = entity({"name": "Unused template", "views": {"html": {"content": f'<img src="{image}">'}},
                            "data": {"x": ref("dataExtensions", "de-404", "name")}})
    assets[ASSET_LINK]["data"]["views"]["html"]["content"] += f'<img src="{image}">'
    export["entities"]["landingPages"] = {
        "lp-1": entity({"name": "Page", "content": ref("primaryLandingPages", "lp-1", "definition/pageId")})}
    package = load_package(export)
    assert not has_warning(package, "assets/555")
    assert not has_warning(package, "de-404")
    assert not has_warning(package, "primaryLandingPages")
    # the sent asset's own reference stays reported (test_hand_built_export_warnings_are_the_expected_ones)
    assert has_warning(package, f"assets/{ASSET_LINK}", "legacyId")


def test_decimal_scale_is_read():
    export = make_export()
    export["entities"]["dataExtensions"][LOG]["data"]["fields"].append(
        dict(field("Score", "Decimal", ordinal=4, length=24), scale=4))
    log_table = load_package(export).tables["DE_Log"]
    assert (log_table.field("Score").type, log_table.field("Score").length, log_table.field("Score").scale) == (
        "Decimal", 24, 4)
    assert log_table.field("Note").scale is None


# ---------------------------------------------------------------------------
# Extensions beyond the pilot: entry source, entry mode, goals and exits
# ---------------------------------------------------------------------------

def with_entry_source(event_type, schedule_mode=None, automation_id="0a1b2c3d-0000-4000-8000-00000000abcd",
                      **meta):
    """The hand-built export with its entry event turned into another kind of entry source."""
    export = make_export()
    event = export["entities"]["eventDefinitions"][EVENT]["data"]
    event["type"] = event_type
    event["metaData"] = dict({"scheduleState": "No Schedule", "criteriaDescription": None}, **meta)
    if schedule_mode is not None:
        event["metaData"]["scheduleFlowMode"] = schedule_mode
    event["arguments"] = {"automationId": automation_id, "criteria": "", "useHighWatermark": False}
    export["entities"]["journeys"][JOURNEY]["data"]["triggers"][0]["type"] = event_type
    return export


def test_automation_entry_is_the_pilot_procedure():
    event = load().entry_event
    assert (event.kind, event.event_type, event.automation_name, event.entry_filter) == (
        ENTRY_AUTOMATION, "AutomationAudience", "A_Entry", None)


def test_run_once_entry_does_not_report_its_scheduler_as_an_automation():
    package = load_package(with_entry_source("EmailAudience", "runOnce", runOnceScheduleMode="onPublish"))
    event = package.entry_event
    assert (event.kind, event.schedule_mode, event.run_once_mode, event.automation_name) == (
        ENTRY_RUN_ONCE, "runOnce", "onPublish", None)
    # the automationId of a Run Once data extension entry is the scheduler, not a feeding automation
    assert not has_warning(package, "alimentato")


def test_data_extension_entry_with_another_schedule_is_a_procedure_to_verify():
    package = load_package(with_entry_source("EmailAudience", "everyHour"))
    assert package.entry_event.kind == ENTRY_SCHEDULED
    assert package.entry_event.automation_name is None
    assert has_warning(package, "Entry event", "'everyHour'", "ricorrente", "da verificare")
    assert not has_warning(package, "alimentato")
    package = load_package(with_entry_source("EmailAudience"))
    assert package.entry_event.kind == ENTRY_SCHEDULED
    assert has_warning(package, "Entry event", "pianificazione non indicata", "da verificare")


@pytest.mark.parametrize("event_type", ["CloudPagesSmartCaptureSubmissionEvent", "APIEvent", ""])
def test_other_entry_sources_are_not_driven_by_an_import(event_type):
    package = load_package(with_entry_source(event_type, automation_id=None))
    assert package.entry_event.kind == ENTRY_OTHER
    assert package.entry_event.table == "DE_Entry"


def test_automation_entry_whose_automation_is_missing_is_reported():
    export = make_export()
    del export["entities"]["automations"][AUTOMATION]
    package = load_package(export)
    assert package.entry_event.kind == ENTRY_AUTOMATION and package.entry_event.automation_name is None
    assert has_warning(package, "Entry event", "alimentato dall'automation", AUTOMATION)


def test_entry_filter_is_reported():
    export = make_export()
    export["entities"]["eventDefinitions"][EVENT]["data"]["arguments"]["criteria"] = "<FilterDefinition/>"
    package = load_package(export)
    assert package.entry_event.entry_filter == "<FilterDefinition/>"
    assert has_warning(package, "Entry event", "filtro di ingresso", "non lo modella")
    export = make_export()
    trigger = export["entities"]["journeys"][JOURNEY]["data"]["triggers"][0]
    trigger["configurationArguments"] = {"criteria": "", "filterDefinitionId": "00000000-0000-0000-0000-000000000000"}
    assert load_package(export).entry_event.entry_filter is None
    trigger["configurationArguments"]["filterDefinitionId"] = "f-1"
    assert load_package(export).entry_event.entry_filter == "filterDefinitionId f-1"


def test_entry_mode_goals_and_exits():
    package = load()
    assert (package.journey.entry_mode, package.journey.goal_count, package.journey.exit_count) == (None, 0, 0)
    assert (package.journey.exits, package.journey.goals) == ([], [])
    export = make_export()
    data = export["entities"]["journeys"][JOURNEY]["data"]
    data.update(entryMode="OnceAndDone", goals=[{"key": "goal-1"}], exits=[{"key": "exit-1"}, {"key": "exit-2"}])
    package = load_package(export)
    assert (package.journey.entry_mode, package.journey.goal_count, package.journey.exit_count) == (
        "OnceAndDone", 1, 2)
    assert has_warning(package, "1 goal", "non li modella")
    # Spec, step 3: an exit criterion the generator cannot evaluate is "reported as not modelled"; one without
    # criteria is kept with the reason "nessun criterio"
    assert [(e.key, e.kind, e.formula, e.unsupported_reason) for e in package.journey.exits] == [
        ("exit-1", "exit", None, "nessun criterio"), ("exit-2", "exit", None, "nessun criterio")]
    assert has_warning(package, "criterio di uscita (exit criteria) 'exit-1' non è modellato", "nessun criterio")
    assert has_warning(package, "'exit-2' non è modellato")


def test_wait_by_attribute_on_journey_data_reads_the_entry_table():
    activities = default_activities()
    key = ref("eventDefinitions", EVENT, "eventDefinitionKey")
    activities[2]["configurationArguments"]["waitEndDateAttributeExpression"] = '{{Event.%s."Flag"}}' % key
    assert load(activities).journey.activities["WAITATTR-1"].wait_field == ("DE_Entry", "Flag")
    activities[2]["configurationArguments"]["waitEndDateAttributeExpression"] = '{{Event.%s."Flag"}}' % (
        ref("eventDefinitions", OLD_EVENT, "eventDefinitionKey"))
    package = load(activities)
    assert package.journey.activities["WAITATTR-1"].wait_field is None
    assert has_warning(package, "WAITATTR-1", "DEAudience-2", "non è l'entry event del trigger")


def test_wait_by_attribute_fallback_reads_journey_data_too():
    activities = default_activities()
    key = ref("eventDefinitions", EVENT, "eventDefinitionKey")
    activities[2]["configurationArguments"]["waitEndDateAttributeExpression"] = ""
    activities[2]["metaData"]["attribute"]["id"] = f"Event.{key}.Flag"
    assert load(activities).journey.activities["WAITATTR-1"].wait_field == ("DE_Entry", "Flag")


# ---------------------------------------------------------------------------
# Step 3: paths from the contact to the table a condition reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected_table, expected_path", [
    # the pilot's shape: contact -> DE_Master (group-to-set) -> DE_Entry -> DE_Config
    (Condition("Config Set", "CHANNEL", "Equal", "x", path=("rel-root", "rel-me", "rel-ec")),
     "DE_Config", ("DE_Master", "DE_Entry")),
    # the ids out of document order: the chain from the contact gives the order
    (Condition("Config Set", "CHANNEL", "Equal", "x", path=("rel-ec", "rel-me", "rel-root")),
     "DE_Config", ("DE_Master", "DE_Entry")),
    # the table linked to the contact itself: nothing between
    (Condition("Master Set", "Perm", "Is", "true", path=("rel-root",)), "DE_Master", ()),
    # the step 3 exports' public holidays: contact -> DE_Holiday_Entry -> DE_Holiday
    (Condition("PublicHoliday_DE", "PHStart", "After", ";+;0;days", path=("rel-ph-root", "rel-ph")),
     "DE_Holiday", ("DE_Holiday_Entry",)),
    # no path in the export
    (Condition("Config Set", "CHANNEL", "Equal", "x"), "DE_Config", ()),
    # Journey Data has no path
    (Condition("DEAudience-1", "Flag", "Is", "true", source=SOURCE_JOURNEY, path=("rel-root",)), "DE_Entry", ()),
])
def test_condition_path_names_the_tables_between_the_contact_and_the_table(raw, expected_table, expected_path):
    """Spec, step 3, "Tables reached through other tables": the relationship ids of the condition (criteria.py writes
    them in Condition.path) become the tables between the contact and the condition's table, nearest the contact
    first, the table itself left out."""
    reader = reader_for(with_public_holidays(make_export()))
    mapped = reader.map_condition(raw)
    assert (mapped.table, mapped.path) == (expected_table, expected_path)
    assert not any("relazione" in w and "non è nell'export" in w for w in reader.warnings)


def test_unknown_relationship_ids_are_dropped_with_one_warning_each():
    reader = reader_for(make_export())
    mapped = reader.map_condition(Condition("Config Set", "CHANNEL", "Equal", "x",
                                            path=("rel-404", "rel-root", "rel-me", "rel-404", "rel-405", "rel-ec")))
    assert mapped.path == ("DE_Master", "DE_Entry")
    reader.map_condition(Condition("Master Set", "Perm", "Is", "true", path=("rel-404",)))
    unknown = [w for w in reader.warnings if "non è nell'export" in w and "relazione" in w]
    assert len(unknown) == 2 and any("rel-404" in w for w in unknown) and any("rel-405" in w for w in unknown)


def test_condition_path_through_load_package():
    """The same rule from the raw criteria XML (UiMetaData groupToSetRelationshipId, then each <AttributePath>):
    needs criteria.py of step 3 (Condition.path and Condition.relative)."""
    export = with_public_holidays(make_export())
    xml = condition("set-ph", "PHStart", "After", ";+;0;days", group="rel-ph-root", through=("rel-ph",))
    package, sms = load_sms_path(xml, export)
    assert sms.formula == Condition("DE_Holiday", "PHStart", "After", ";+;0;days",
                                    relative=RelativeDate(sign=1, amount=0, unit="days"), path=("DE_Holiday_Entry",))
    assert sms.unsupported_reason is None
    email = package.journey.activities["DECISION-1"].outcomes[0]
    assert email.formula.path == ("DE_Master", "DE_Entry")


# ---------------------------------------------------------------------------
# Step 3: relative dates and the new operators
# ---------------------------------------------------------------------------

TODAY = RelativeDate(sign=1, amount=0, unit="days")


@pytest.mark.parametrize("mapped, fragments", [
    (Condition("DE_Config", "WAIT1", "After", ";+;0;days", relative=TODAY), None),
    (Condition("DE_Config", "WAIT1", "AtOrBefore", ";-;7;days", relative=RelativeDate(-1, 7, "days")), None),
    (Condition("DE_Config", "WAIT1", "Before", ";+;2;hours", relative=RelativeDate(1, 2, "hours")), None),
    (Condition("DE_Entry", "Flag", "IsNot", "true"), None),
    (Condition("DE_Config", "WAIT1", "AtOrAfter", "2026-10-14"), None),
    (Condition("DE_Config", "CHANNEL", "After", ";+;0;days", relative=TODAY),
     ("il branch 'X'", "valore relativo a oggi su un campo di tipo Text", "DE_Config.CHANNEL")),
    (Condition("DE_Config", "WAIT1", "After", ";+;1;fortnights", relative=RelativeDate(1, 1, "fortnights")),
     ("valore relativo a oggi", "'fortnights'", "non supporta")),
    (Condition("DE_Config", "WAIT1", "Equal", ";+;0;days", relative=TODAY),
     ("valore relativo a oggi", "'Equal'", "non lo accetta")),
])
def test_relative_dates_are_supported_only_on_dates_in_known_units_with_date_operators(mapped, fragments):
    """Spec, step 3, "Relative dates in conditions": After, AtOrAfter, Before, AtOrBefore take a relative value, IsNot is
    the negation of Is; "Any other value form, unit or operator is unsupported"."""
    reason = reader_for(make_export()).unsupported_formula(mapped, "il branch 'X'")
    if fragments is None:
        assert reason is None
    else:
        assert reason is not None and all(fragment in reason for fragment in fragments), reason


def test_relative_date_conditions_through_load_package():
    package, sms = load_sms_path(condition(S_CONFIG, "WAIT1", "AtOrAfter", ";+;13;days", **CONFIG_PATH))
    assert sms.formula.relative == RelativeDate(sign=1, amount=13, unit="days")
    assert sms.unsupported_reason is None
    package, sms = load_sms_path(condition(S_CONFIG, "CHANNEL", "Before", ";+;30;days", **CONFIG_PATH))
    assert "valore relativo a oggi su un campo di tipo Text" in sms.unsupported_reason
    assert has_warning(package, "DECISION-1", "campo di tipo Text", "non coperti")


# ---------------------------------------------------------------------------
# Step 3: exit criteria and goals
# ---------------------------------------------------------------------------

def journey_filter(key, criteria=None, name="", description=None):
    """An exit criterion or a goal as the step 3 exports write it."""
    raw = {"id": f"id-{key}", "key": key, "name": name, "description": "", "type": "ExitCriteria", "outcomes": [],
           "arguments": {"filterResult": "{{Contact.FilterId.f-1}}"},
           "configurationArguments": {"schemaVersionId": 257, "filterDefinitionId": "f-1"},
           "metaData": {"chainType": "none"}}
    if criteria is not None:
        raw["configurationArguments"]["criteria"] = criteria
    if description is not None:
        raw["metaData"]["criteriaDescription"] = description
    return raw


def with_filters(exits=(), goals=()):
    export = make_export()
    export["entities"]["journeys"][JOURNEY]["data"].update(exits=list(exits), goals=list(goals))
    return load_package(export)


EXIT_XML = filter_definition(condition(S_MASTER, "Perm", "Is", "false", **MASTER_PATH),
                             journey_condition("flag", "Is", "true"), operator="OR")


def test_exit_criteria_are_parsed_like_decision_criteria():
    """Spec, step 3: the generator "models exit criteria whose conditions it can evaluate (the same operators, data
    sources and relative dates as a decision split)"; Journey Builder evaluates them "when the contact's time in a
    wait activity expires (documented)"."""
    package = with_filters(exits=[journey_filter("EXITCRITERIA", EXIT_XML,
                                                 description="Perm is False  OR Flag is True")])
    journey_ = package.journey
    assert journey_.exit_count == 1
    exit_ = journey_.exits[0]
    assert (exit_.key, exit_.name, exit_.kind, exit_.unsupported_reason) == ("EXITCRITERIA", "EXITCRITERIA", "exit", None)
    assert exit_.description == "Perm is False  OR Flag is True"
    assert exit_.formula == ConditionGroup("OR", (
        Condition("DE_Master", "Perm", "Is", "false"),
        Condition("DE_Entry", "Flag", "Is", "true", source=SOURCE_JOURNEY)))
    assert has_warning(package, "1 criterio di uscita (exit criteria)", "alla fine di ogni wait",
                       'Criterio: "Perm is False OR Flag is True"')
    assert not has_warning(package, "non è modellato") and not has_warning(package, "non li modella")


def test_exit_criteria_without_description_quote_the_formula():
    package = with_filters(exits=[journey_filter("EXIT-1", EXIT_XML, name="Leave"),
                                  journey_filter("EXIT-2", filter_definition(condition(S_CONFIG, "CHANNEL", "Equal",
                                                                                       "Off", **CONFIG_PATH)))])
    assert [e.name for e in package.journey.exits] == ["Leave", "EXIT-2"]
    assert has_warning(package, "2 criteri di uscita (exit criteria)", "ne soddisfa uno",
                       "(DE_Master.Perm Is 'false' OR DE_Entry.Flag Is 'true')", "DE_Config.CHANNEL Equal 'Off'")


def test_exit_criteria_the_generator_cannot_evaluate_are_not_modelled():
    """Spec, step 3: "Exit criteria with a condition the generator cannot evaluate are reported as not modelled"."""
    package = with_filters(exits=[
        journey_filter("EXIT-OK", EXIT_XML, description="Perm is False OR Flag is True"),
        journey_filter("EXIT-OP", filter_definition(condition(S_MASTER, "Perm", "NotContains", "x", **MASTER_PATH))),
        journey_filter("EXIT-XML", "<FilterDefinition><broken"),
        journey_filter("EXIT-NONE"),
    ])
    by_key = {e.key: e for e in package.journey.exits}
    assert by_key["EXIT-OK"].unsupported_reason is None
    assert "il criterio usa l'operatore 'NotContains'" in by_key["EXIT-OP"].unsupported_reason
    assert by_key["EXIT-OP"].formula == Condition("DE_Master", "Perm", "NotContains", "x")
    assert by_key["EXIT-XML"].formula is None and "non si possono leggere" in by_key["EXIT-XML"].unsupported_reason
    assert (by_key["EXIT-NONE"].formula, by_key["EXIT-NONE"].unsupported_reason) == (None, "nessun criterio")
    # the one the generator evaluates is reported as such, each of the others as not modelled, with its reason
    assert has_warning(package, "1 criterio di uscita (exit criteria)", "alla fine di ogni wait")
    assert has_warning(package, "'EXIT-OP' non è modellato", "NotContains")
    assert has_warning(package, "'EXIT-XML' non è modellato", "non si possono leggere")
    assert has_warning(package, "'EXIT-NONE' non è modellato: nessun criterio")


def test_exit_criteria_with_a_relative_date():
    xml = filter_definition(condition(S_CONFIG, "WAIT1", "Before", ";+;30;days", **CONFIG_PATH))
    exit_ = with_filters(exits=[journey_filter("EXITCRITERIA", xml)]).journey.exits[0]
    assert exit_.unsupported_reason is None
    assert exit_.formula.relative == RelativeDate(sign=1, amount=30, unit="days")
    assert exit_.formula.path == ("DE_Master", "DE_Entry")


def test_goals_are_parsed_and_reported_as_not_modelled():
    package = with_filters(goals=[journey_filter("GOAL-1", EXIT_XML, name="Bought"), journey_filter("GOAL-2")])
    goals = package.journey.goals
    assert package.journey.goal_count == 2
    assert [(g.key, g.name, g.kind, g.unsupported_reason) for g in goals] == [
        ("GOAL-1", "Bought", "goal", None), ("GOAL-2", "GOAL-2", "goal", "nessun criterio")]
    assert isinstance(goals[0].formula, ConditionGroup)
    assert has_warning(package, "2 goal", "non li modella")
    assert package.journey.exits == [] and not has_warning(package, "exit criteria")


# ---------------------------------------------------------------------------
# Step 3: synchronized (managed) data extensions
# ---------------------------------------------------------------------------

MANAGED_ISSUE = {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "sourceJobType": "PACKAGE",
                 "functionality": "NO_DEPLOY", "objectType": "ManagedDataExtension"}


def test_managed_data_extension_is_marked_and_reported_once():
    """Spec, step 3, "Synchronized data extensions": a table that Package Manager marks as a managed data extension
    (NO_DEPLOY, ManagedDataExtension ...) is imported like the others; the warning says its rows also arrive from
    the sync."""
    export = make_export()
    export["entities"]["sharedDataExtensions"] = {"de-cm": table("CampaignMember_Salesforce_9", [field("Id")])}
    export["entities"]["sharedDataExtensions"]["de-cm"]["issues"] = [
        MANAGED_ISSUE, {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "SHARED_DE"}]
    package = load_package(export)
    assert package.tables["CampaignMember_Salesforce_9"].managed is True
    assert not any(t.managed for name, t in package.tables.items() if name != "CampaignMember_Salesforce_9")
    managed = [w for w in package.warnings if "NO_DEPLOY" in w]
    assert len(managed) == 1
    assert all(fragment in managed[0] for fragment in (
        "La data extension CampaignMember_Salesforce_9 è sincronizzata", "ManagedDataExtension",
        "le sue righe arrivano anche dalla sincronizzazione"))
    assert "non si può riempire" not in managed[0]
    # the other issue of the same table is still a Package Manager warning
    assert has_warning(package, "sulla data extension CampaignMember_Salesforce_9", "SHARED_DE")


@pytest.mark.parametrize("issue", [
    {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "INFO", "objectType": "ManagedDataExtension"},
    {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "functionality": "NO_DEPLOY"},
])
def test_either_mark_makes_a_managed_data_extension(issue):
    export = make_export()
    export["entities"]["dataExtensions"][LOG]["issues"] = [issue]
    package = load_package(export)
    assert package.tables["DE_Log"].managed is True
    assert has_warning(package, "La data extension DE_Log è sincronizzata")


def test_export_level_issue_naming_a_table_marks_it_managed():
    export = make_export()
    export["issues"] = [dict(MANAGED_ISSUE, entity={"entityType": "dataExtensions", "retrievalProperty": "name",
                                                    "value": "DE_Config"})]
    package = load_package(export)
    assert package.tables["DE_Config"].managed is True
    assert [w for w in package.warnings if "NO_DEPLOY" in w] == [
        w for w in package.warnings if w.startswith("La data extension DE_Config è sincronizzata")]
    assert not load().tables["DE_Config"].managed


# ---------------------------------------------------------------------------
# The pilot export
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pilot():
    return load_package(PILOT)


def test_pilot_journey(pilot):
    journey_ = pilot.journey
    assert journey_.name == "CJ_SkYDE_NCRM_Demo_final"
    assert journey_.activity_count == 178 and len(journey_.activities) == 178
    kinds = {}
    for item in journey_.activities.values():
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    assert kinds == {KIND_DECISION: 106, KIND_UPDATE: 34, KIND_WAIT_DURATION: 10, KIND_WAIT_ATTRIBUTE: 3,
                     KIND_WAIT_STO: 7, KIND_EMAIL: 11, KIND_ENGAGEMENT: 6, KIND_RANDOM: 1}
    assert journey_.start_key == "MULTICRITERIADECISIONV2-1"
    assert pilot.unsupported_activities == []


def test_pilot_entry_tables_and_relationships(pilot):
    assert pilot.journey.entry_table == "DE_NCM_AdHoc_Entry"
    assert pilot.entry_event.table == "DE_NCM_AdHoc_Entry"
    assert pilot.entry_event.automation_name == "A_SkyDE_NCRM_AdHoc_CJ_entry"
    assert (pilot.entry_event.kind, pilot.entry_event.schedule_mode) == (ENTRY_AUTOMATION, "automation")
    assert (pilot.journey.entry_mode, pilot.journey.goal_count, pilot.journey.exit_count) == ("MultipleEntries", 0, 0)
    assert pilot.missing_relationships == []
    assert len(pilot.tables) == 7
    assert len(pilot.relationships) == 3
    group_to_set = [r for r in pilot.relationships if r.left_table is None]
    assert len(group_to_set) == 1
    assert (group_to_set[0].right_table, group_to_set[0].right_field) == ("SF_Master_DataExtension_DE", "ContactId")


def test_pilot_queries(pilot):
    assert len(pilot.queries) == 4
    for query in pilot.queries:
        assert "{{mcpm" not in query.sql
    entry_query = next(q for q in pilot.queries if q.target_table == "DE_NCM_AdHoc_Entry")
    assert "[SF_Master_DataExtension_DE] M" in entry_query.sql
    assert "[DE_NCRM_AdHoc_Target] T" in entry_query.sql
    assert "[DE_NCRM_AD_HOC_CONFIG] C" in entry_query.sql
    backup = next(q for q in pilot.queries if q.name == "Q_SkyDE_AdHoc_Backup")
    assert "[DE_NCM_AdHoc_Entry]" in backup.sql
    assert pilot.automation_queries["A_SkyDE_NCRM_AdHoc_CJ_entry"] == [
        "Q_Check_Active_campaigns", "Q_SKYDE_AdHoc_Target", "Q_SKYDE_NCRM_AD_HOC_CONFIGURATION_UPDATE",
        "Q_SkyDE_AdHoc_Backup"]


def test_pilot_email_has_no_link(pilot):
    emails = [a for a in pilot.journey.activities.values() if a.kind == KIND_EMAIL]
    assert {a.email_asset_id for a in emails} == {"1806207"}
    assert {a.email_name for a in emails} == {"Demo Sky"}
    assert pilot.emails["1806207"].has_link is False


def test_pilot_decision_conditions_name_real_tables(pilot):
    tables = set()
    for item in pilot.journey.activities.values():
        if item.kind != KIND_DECISION:
            continue
        assert item.outcomes[-1].is_remainder and item.outcomes[-1].formula is None
        for outcome_ in item.outcomes[:-1]:
            for condition_ in formula_conditions(outcome_.formula):
                tables.add(condition_.table)
                assert pilot.tables[condition_.table].field(condition_.field) is not None
                assert condition_.source == SOURCE_CONTACT and condition_.value_attribute is None
    assert tables == {"SF_Master_DataExtension_DE", "DE_NCRM_AD_HOC_CONFIG"}


def test_pilot_wait_fields(pilot):
    waits = [a for a in pilot.journey.activities.values() if a.kind == KIND_WAIT_ATTRIBUTE]
    assert sorted(a.wait_field for a in waits) == [
        ("DE_NCRM_AD_HOC_CONFIG", "WAIT1"), ("DE_NCRM_AD_HOC_CONFIG", "WAIT2"), ("DE_NCRM_AD_HOC_CONFIG", "WAIT3")]


def test_pilot_step3_fields(pilot):
    """What the step 3 rules read on the pilot: no exit criterion, goal, SMS, managed table or wait offset; the
    Config conditions and waits name the path the export gives (contact -> Master -> Entry -> Config)."""
    journey_ = pilot.journey
    assert (journey_.exits, journey_.goals, journey_.default_mobile_field) == ([], [], "MobilePhone")
    assert not any(t.managed for t in pilot.tables.values())
    assert not any(a.kind == KIND_SMS for a in journey_.activities.values())
    paths = {(c.table, c.path) for a in journey_.activities.values() for o in a.outcomes
             for c in formula_conditions(o.formula)}
    assert paths == {("SF_Master_DataExtension_DE", ()),
                     ("DE_NCRM_AD_HOC_CONFIG", ("SF_Master_DataExtension_DE", "DE_NCM_AdHoc_Entry"))}
    waits = [a for a in journey_.activities.values() if a.kind == KIND_WAIT_ATTRIBUTE]
    assert {(a.wait_offset_amount, a.wait_time_of_day, a.wait_path) for a in waits} == {
        (None, None, ("SF_Master_DataExtension_DE", "DE_NCM_AdHoc_Entry"))}


def test_pilot_engagement_splits(pilot):
    splits = [a for a in pilot.journey.activities.values() if a.kind == KIND_ENGAGEMENT]
    assert len(splits) == 6
    for split in splits:
        assert split.engagement_ref == "EMAILV2-1"
        assert split.engagement_kind == ("open" if split.name.startswith("Open") else "click")
        assert split.name.startswith(("Open", "Click"))
        assert [o.when for o in split.outcomes] == [True, False]


def test_pilot_update_contact_write_to_the_entry_table(pilot):
    entry_writes = [w for a in pilot.journey.activities.values() for w in a.writes
                    if w.table == "DE_NCM_AdHoc_Entry"]
    # the export writes the number 1 into fields/name:Control Group/id of the entry table
    assert len(entry_writes) == 11
    assert all(w == Write("DE_NCM_AdHoc_Entry", "Control Group", "1") for w in entry_writes)


def test_pilot_warnings(pilot):
    # two reading warnings plus the carried-over checks: constant writes into the Log table's three
    # primary-key fields and the one-to-one relationship to the entry table with a wider primary key
    plain = [w for w in pilot.warnings if not w.startswith("pattern sospetto")]
    assert len(plain) == 2, pilot.warnings
    assert has_warning(pilot, "assets/1806207", "legacyId")
    assert has_warning(pilot, "mobileNumber", "CJ_SkYDE_NCRM_Demo", "DEAudience-6021dd4c")


def test_constant_write_into_primary_key_is_flagged():
    """Spec check: an update contact step that writes a constant into a primary-key field."""
    package = load(default_activities())
    assert not any("campo di chiave primaria" in w for w in package.warnings)
    activities = default_activities()
    activities.append({
        "key": "UPDATE-PK", "type": "UPDATECONTACTDATA", "name": "log",
        "arguments": {"activityData": {"updateContactFields": [
            {"dataExtensionId": ref("dataExtensions", ENTRY, "id"),
             "field": ref("dataExtensions", ENTRY, "fields/name:ContactId/id"), "value": "FIXED"},
            {"dataExtensionId": ref("dataExtensions", ENTRY, "id"),
             "field": ref("dataExtensions", ENTRY, "fields/name:ContactId/id"), "value": "{{DateTime.Now}}"},
        ]}},
        "outcomes": [{"key": "o", "next": None, "metaData": {}}],
    })
    package = load(activities)
    flagged = [w for w in package.warnings if "campo di chiave primaria" in w]
    assert len(flagged) == 1 and "'FIXED'" in flagged[0] and "UPDATE-PK" in flagged[0]


def test_pilot_flags_constant_writes_and_wide_one_to_one_join(pilot):
    writes = [w for w in pilot.warnings if "campo di chiave primaria" in w]
    assert any("DE_SkyDE_AdHoc_Log_partial.CampaignName" in w and "'DemoSKY'" in w for w in writes)
    assert any("DE_SkyDE_AdHoc_Log_partial.ActionId" in w and "'ActionId'" in w for w in writes)
    joins = [w for w in pilot.warnings if "relazione uno a uno" in w]
    assert len(joins) == 1 and "DE_NCM_AdHoc_Entry" in joins[0] and "ActionId" in joins[0] and "CampaignName" in joins[0]
