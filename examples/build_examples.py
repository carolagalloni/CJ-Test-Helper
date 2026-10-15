"""Build the package.json of every hand-made example, so they all share the pilot's shape.

Run from anywhere:  python examples/build_examples.py

Standard library only. The output follows docs/export-format.md and copies the
pilot export ("fixtures/CJ template complesso-v1.json") field by field where it
matters: entity wrappers, {{mcpm#/entities/...}} references, criteria XML with
UiMetaData and nested AttributePath, wait-by-attribute expressions, email
triggeredSend references, update-contact field references.

Only package.json files are written here. expected.json and README.md are
written by hand (the expected answer is derived from docs/spec.md, never from
the generator). After building, validate() checks every example:

- package.json is valid JSON with one journey listed in selectedEntities;
- every outcome `next` points to an existing activity key;
- every {{mcpm#/entities/<kind>/<id>/...}} reference names an existing entity;
- every criteria XML parses, and every decision outcome except the remainder has criteria;
- expected.json lists every outcome of every decision, engagement and random split, and nothing else
  (the pilot directories may move undecidable outcomes to "branches_any_of"; they are validated against
  the export named by their "package" key).
"""

from __future__ import annotations

import json
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMESPACE = uuid.UUID("6d1c2a8e-3b7f-4c55-9a2e-0c0ffee0e5a1")   # fixed: ids are reproducible

# Every kind present in the pilot's `entities` (unused ones stay empty, as in the pilot).
ENTITY_KINDS = [
    "assets", "attributeGroups", "attributeRelationships", "attributeSets", "automations", "categories",
    "cloudPageCollections", "codeResources", "dataExtensions", "dataExtractActivities", "dataFilterDefinitions",
    "dataFilters", "eventDefinitions", "fileTransferActivities", "importActivities", "journeys",
    "journeyTemplates", "landingPages", "microsites", "micrositePages", "micrositePageVersions",
    "primaryLandingPages", "queryActivities", "registeredCustomActivities", "scriptActivities",
    "sendDefinitions", "sharedDataExtensions", "siteMapVersions", "subscriberLists",
    "triggeredSendDefinitions", "verificationActivities",
]
SELECTED_KINDS = ["assets", "attributeGroups", "automations", "cloudPageCollections", "dataExtensions",
                  "journeys", "journeyTemplates", "sendDefinitions", "sharedDataExtensions",
                  "triggeredSendDefinitions"]

GROUP_NAME = "CJ_EXAMPLES"
TIMEZONE = "W. Europe Standard Time"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def ref(kind: str, eid: str, path: str) -> str:
    """A Package Manager reference, e.g. {{mcpm#/entities/dataExtensions/<id>/data/name}}."""
    return "{{mcpm#/entities/%s/%s/data/%s}}" % (kind, eid, path)


def table_sql(de_id: str) -> str:
    """How the pilot writes a table inside SQL: namePrefix reference then [name reference]."""
    return "%s[%s]" % (ref("dataExtensions", de_id, "namePrefix"), ref("dataExtensions", de_id, "name"))


class Package:
    """Collects the entities of one example export."""

    def __init__(self, example: str, package_name: str):
        self.example = example
        self.package_name = package_name
        self.entities: dict[str, dict] = {k: {} for k in ENTITY_KINDS}
        self.tables: dict[str, str] = {}        # table name -> DE id
        self.sets: dict[str, str] = {}          # table name -> attribute set id
        self.rels: dict[str, str] = {}          # relationship name -> id
        self.group_id = ""
        self.journey_id = ""

    def gid(self, kind: str, name: str) -> str:
        return str(uuid.uuid5(NAMESPACE, "%s/%s/%s" % (self.example, kind, name)))

    def add(self, kind: str, eid: str, data: dict) -> None:
        self.entities[kind][eid] = {
            "issues": [], "originID": eid,
            "originEID": "cjexample-" + uuid.uuid5(NAMESPACE, self.example + "/" + eid).hex[:20],
            "data": data,
        }

    # -- data extensions ----------------------------------------------------

    def table(self, name: str, fields: list[dict], sendable_field: str | None = None) -> str:
        de_id = self.gid("de", name)
        for i, f in enumerate(fields):
            f["ordinal"] = i
        data = {
            "dataRetentionProperties": {"dataRetentionPeriodUnitOfMeasure": 3, "dataRetentionPeriodLength": 1,
                                        "isDeleteAtEndOfRetentionPeriod": True,
                                        "isResetRetentionPeriodOnImport": False,
                                        "isRowBasedRetention": False, "rowBasedThreshold": 1000000000},
            "description": "", "isFieldAdditionAllowed": True, "isFieldModificationAllowed": True,
            "isObjectDeletable": True, "isSendable": sendable_field is not None, "isTestable": True,
            "name": name,
        }
        if sendable_field is not None:
            data["sendableCustomObjectField"] = sendable_field
            data["sendableSubscriberField"] = "_SubscriberKey"
        data["customerKey"] = self.gid("customerKey", name).upper()
        data["fields"] = fields
        self.add("dataExtensions", de_id, data)
        self.tables[name] = de_id
        return de_id

    # -- contact model ----------------------------------------------------------

    def contact_model(self, chain: list[tuple[str, str, str]]) -> None:
        """Attribute group, sets and relationships.

        chain[0] = (first table, field, "") is linked to the group (group-to-set, like the pilot's Master);
        each later item (table, left field, right field) is linked to the previous table, like
        Master -> Entry on ContactId and Entry -> Config on CampaignName in the pilot.
        """
        self.group_id = self.gid("group", GROUP_NAME)
        self.add("attributeGroups", self.group_id, {
            "attributeGroupIconKey": "et-icon-databas", "attributeGroupType": "Standard",
            "definitionName": {"value": GROUP_NAME}, "name": GROUP_NAME, "isSystemDefined": False})
        for table, _, _ in chain:
            self.sets[table] = self.gid("set", table)
        previous = None
        for table, left_field, right_field in chain:
            de_id = self.tables[table]
            set_id = self.sets[table]
            if previous is None:
                rel_name = "%s <- %s" % (GROUP_NAME, table)
                rel_id = self.gid("rel", rel_name)
                self.add("attributeRelationships", rel_id, {
                    "relationshipAttributes": [{
                        "rightAttributeName": left_field,
                        "rightDataExtensionID": ref("dataExtensions", de_id, "id"),
                        "rightAttributeID": ref("dataExtensions", de_id, "fields/name:%s/id" % left_field)}],
                    "leftRelationshipIDs": [{"type": "int16", "value": "3"}],
                    "leftRelationshipReferenceType": "CustomerData",
                    "isGroupToSetRelationship": True,
                    "leftItem": {"relationshipType": "AttributeGroup", "cardinality": "One",
                                 "name": ref("attributeGroups", self.group_id, "name"),
                                 "identifier": ref("attributeGroups", self.group_id, "id"),
                                 "connectingID": {"identifierType": "FullyQualifiedName"}},
                    "rightItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                                  "name": ref("attributeSets", set_id, "name")},
                    "name": rel_name})
            else:
                rel_name = "%s -> %s" % (previous, table)
                rel_id = self.gid("rel", rel_name)
                self.add("attributeRelationships", rel_id, {
                    "relationshipAttributes": [{
                        "leftAttributeName": left_field, "rightAttributeName": right_field,
                        "rightDataExtensionID": ref("dataExtensions", de_id, "id"),
                        "rightAttributeID": ref("dataExtensions", de_id, "fields/name:%s/id" % right_field)}],
                    "isGroupToSetRelationship": False,
                    "leftItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                                 "identifier": ref("attributeSets", self.sets[previous], "id"),
                                 "connectingID": {"identifierType": "FullyQualifiedName"}},
                    "rightItem": {"relationshipType": "AttributeSet", "cardinality": "One",
                                  "name": ref("attributeSets", set_id, "name")},
                    "name": rel_name})
            self.rels[table] = rel_id          # the relationship that reaches this table
            self.add("attributeSets", set_id, {
                "storageLogicalType": "DataExtension", "isRoot": False,
                "storageName": ref("dataExtensions", de_id, "name"),
                "storageReferenceID": {"type": "guid", "value": ref("dataExtensions", de_id, "id")},
                "groupId": ref("attributeGroups", self.group_id, "id"),
                "name": table,
                "leftRelationships": ["#/entities/attributeRelationships/%s/data" % rel_id],
                "isSystemDefined": False})
            previous = table
        self.chain = [t for t, _, _ in chain]

    def path_to(self, table: str) -> list[str]:
        """Relationship ids from the first table of the chain to `table`, excluding the group link."""
        idx = self.chain.index(table)
        return [self.rels[t] for t in self.chain[1:idx + 1]]

    # -- criteria -----------------------------------------------------------------

    def cond(self, table: str, field: str, operator: str, value: str = "") -> str:
        """One <Condition>, written exactly like the pilot (UiMetaData + nested AttributePath)."""
        key = "%s.%s" % (ref("attributeSets", self.sets[table], "name"), field)
        ui = "{&quot;groupToSetRelationshipId&quot;:&quot;%s&quot;}" % ref(
            "attributeRelationships", self.rels[self.chain[0]], "id")
        inner = "<Value><![CDATA[%s]]></Value>" % value
        for rel_id in reversed(self.path_to(table)):
            inner = '<AttributePath RelationshipID="%s">%s</AttributePath>' % (
                ref("attributeRelationships", rel_id, "id"), inner)
        return '<Condition Key="%s" Operator="%s" UiMetaData="%s">%s</Condition>' % (key, operator, ui, inner)


def cset(operator: str, *items: str) -> str:
    return '<ConditionSet Operator="%s" ConditionSetName="Individual Filter Grouping">%s</ConditionSet>' % (
        operator, "".join(items))


def criteria(*items: str) -> str:
    """Top level, like the pilot: FilterDefinition > ConditionSet AND > items."""
    return "<FilterDefinition>%s</FilterDefinition>" % cset("AND", *items)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------

def fld(name: str, typ: str, length: int | None = None, pk: bool = False, nullable: bool = True,
        default: str | None = None) -> dict:
    f: dict = {}
    if default is not None:
        f["defaultValue"] = default
    f.update({"description": "", "isHidden": False, "isInheritable": False, "isOverridable": False,
              "isNullable": nullable, "isPrimaryKey": pk, "isReadOnly": False, "isTemplateField": False})
    if length is not None:
        f["length"] = length
    f.update({"masktype": "None", "mustOverride": False, "name": name, "ordinal": 0, "type": typ})
    return f


def text(name, length=50, **kw):
    return fld(name, "Text", length, **kw)


def boolean(name, **kw):
    return fld(name, "Boolean", None, **kw)


def date(name, **kw):
    return fld(name, "Date", None, **kw)


def email_field(name="Emailaddress"):
    return fld(name, "EmailAddress", 254)


def phone(name="MobilePhone"):
    return fld(name, "Phone", 50)


# ---------------------------------------------------------------------------
# Journey activities (shapes copied from the pilot)
# ---------------------------------------------------------------------------

class Journey:
    def __init__(self, pkg: Package, name: str):
        self.pkg = pkg
        self.name = name
        self.activities: list[dict] = []

    def _uid(self, what: str) -> str:
        return self.pkg.gid("journey-item", "%s/%s" % (self.name, what))

    def _simple_outcome(self, key: str, nxt: str | None, join_label: bool = False) -> dict:
        meta = {"invalid": False}
        if join_label:
            meta["joinLabel"] = ""
        out = {"arguments": {}, "key": self._uid(key + "/outcome"), "metaData": meta}
        if nxt is not None:
            out["next"] = nxt
        return out

    def decision(self, key: str, name: str, outcomes: list[tuple]) -> None:
        """outcomes: (outcome key, label, criteria XML or None for the remainder, next, description)."""
        crit = {}
        filter_result = []
        outs = []
        for okey, label, xml, nxt, desc in outcomes:
            if xml is not None:
                crit[okey] = xml
                filter_result.append("%s={{Contact.FilterId.%s}}" % (okey, self._uid(key + "/" + okey)))
                meta = {"label": label, "skipI18n": True, "isLabelFromConversion": False,
                        "criteriaDescription": desc, "invalid": False}
            else:
                filter_result.append("%s=False" % okey)
                meta = {"label": label, "isLabelFromConversion": False, "invalid": False}
            out = {"arguments": {}, "key": okey, "metaData": meta}
            if nxt is not None:
                out["next"] = nxt
            outs.append(out)
        self.activities.append({
            "type": "MULTICRITERIADECISION", "arguments": {"filterResult": ",".join(filter_result)},
            "description": "", "id": self._uid(key), "key": key, "metaData": {"isConfigured": True},
            "configurationArguments": {"schemaVersionId": 252, "criteria": crit},
            "name": name, "outcomes": outs, "schema": {"arguments": {}}})

    def engagement(self, key: str, name: str, kind: str, email_key: str, email_name: str,
                   yes_next: str | None, no_next: str | None) -> None:
        stats = {"open": 2, "click": 3}[kind]
        outs = []
        for okey, when, label, nxt in (("branchResult-1", True, "Yes", yes_next),
                                       ("branchResult-2", False, "No", no_next)):
            out = {"arguments": {"when": when}, "key": okey, "metaData": {"label": label, "invalid": False}}
            if nxt is not None:
                out["next"] = nxt
            outs.append(out)
        self.activities.append({
            "type": "ENGAGEMENTDECISION",
            "arguments": {"definitionId": "{{Context.DefinitionId}}", "contactKey": "{{Context.ContactKey}}",
                          "statsKey": self._uid(key + "/stats")},
            "description": "", "id": self._uid(key), "key": key,
            "metaData": {"isConfigured": True, "guidanceCardKey": "", "refActivityName": email_name},
            "configurationArguments": {"engagementUrls": {"urls": []}, "statsTypeId": stats,
                                       "refActivityCustomerKey": email_key,
                                       "triggeredSendId": self._uid(key + "/tsd"), "statsUrlId": 0},
            "name": name, "outcomes": outs, "schema": {"arguments": {}}})

    def random_split(self, key: str, branches: list[tuple[str, int, str | None]]) -> None:
        outs = []
        for i, (okey, pct, nxt) in enumerate(branches, start=1):
            out = {"arguments": {"percentage": pct}, "key": okey,
                   "metaData": {"label": "%d%%" % pct, "name": "Path %d" % i, "invalid": False}}
            if nxt is not None:
                out["next"] = nxt
            outs.append(out)
        icon = "/extensions/activities/random-split/images/randomsplit.svg"
        self.activities.append({
            "type": "RANDOMSPLIT", "arguments": {}, "description": "", "id": self._uid(key), "key": key,
            "metaData": {"icon": icon, "iconSmall": icon, "category": "flow", "isConfigured": True,
                         "original_icon": icon, "original_iconSmall": icon, "sections": {}},
            "configurationArguments": {}, "name": "", "outcomes": outs, "schema": {"arguments": {}}})

    def _wait_arguments(self, expression: str) -> dict:
        return {"waitEndDateAttributeDataBound": expression, "waitDefinitionId": self._uid("waitdef/" + expression),
                "waitForEventId": "", "executionMode": "{{Context.ExecutionMode}}",
                "startActivityKey": "{{Context.StartActivityKey}}", "waitQueueId": "{{Context.WaitQueueId}}"}

    def wait_duration(self, key: str, nxt: str | None, amount: int = 1, unit: str = "MINUTES") -> None:
        args = self._wait_arguments("")
        args["waitDefinitionId"] = self._uid(key + "/waitdef")
        self.activities.append({
            "type": "WAIT", "arguments": args, "description": "", "id": self._uid(key), "key": key,
            "metaData": {"isConfigured": True, "isExtended": False, "waitType": "duration",
                         "guidanceCardKey": "", "uiType": "WAITBYDURATION"},
            "configurationArguments": {"specifiedTime": "00:00", "timeZone": TIMEZONE, "waitDuration": amount,
                                       "waitUnit": unit, "waitEndDateAttributeExpression": ""},
            "name": "%d %s" % (amount, "minute" if amount == 1 and unit == "MINUTES" else unit.lower()),
            "outcomes": [self._simple_outcome(key, nxt)], "schema": {"arguments": {}}})

    def wait_attribute(self, key: str, table: str, field: str, nxt: str | None) -> None:
        pkg = self.pkg
        idx = pkg.chain.index(table)
        expression = '{{Contact.Attribute.Contacts.%s."%s"}}' % (".".join(pkg.chain[:idx + 1]), field)
        attribute_path = [{"displayName": "Date-Based Attribute", "index": 1},
                          {"displayName": "Contact Data", "index": 2},
                          {"displayName": GROUP_NAME, "index": 3}]
        attribute_path += [{"displayName": t, "index": 4 + i} for i, t in enumerate(pkg.chain[:idx + 1])]
        args = self._wait_arguments(expression)
        args["waitDefinitionId"] = self._uid(key + "/waitdef")
        self.activities.append({
            "type": "WAIT", "arguments": args, "description": "", "id": self._uid(key), "key": key,
            "metaData": {"isConfigured": True, "isExtended": False,
                         "attribute": {"displayName": field, "displayType": "Date", "type": "item",
                                       "id": "%s.%s" % (ref("attributeSets", pkg.sets[table], "name"), field),
                                       "path": {"displayName": ""}, "attributePath": attribute_path},
                         "waitType": "duration", "uiType": "WAITBYATTRIBUTE"},
            "configurationArguments": {"specifiedTime": "", "timeZone": TIMEZONE, "waitDuration": 0,
                                       "waitUnit": "DAYS", "waitEndDateAttributeExpression": expression},
            "name": "on " + field, "outcomes": [self._simple_outcome(key, nxt)], "schema": {"arguments": {}}})

    def sto_wait(self, key: str, nxt: str | None) -> None:
        self.activities.append({
            "type": "STOWAIT",
            "arguments": {"contactKey": "{{Context.ContactKey}}", "jbActivityId": "{{Activity.Id}}",
                          "jbJourneyId": "{{Context.OriginalDefinitionId}}",
                          "jbJourneyVersion": "{{Context.VersionNumber}}",
                          "waitDefinitionId": "00000000-0000-0000-0000-000000000000", "waitForEventId": "",
                          "executionMode": "{{Context.ExecutionMode}}",
                          "startActivityKey": "{{Context.StartActivityKey}}",
                          "waitQueueId": "{{Context.WaitQueueId}}"},
            "description": "", "id": self._uid(key), "key": key,
            "metaData": {"category": "flow", "isConfigured": True},
            "configurationArguments": {"applicationExtensionKey": "f828061a-5f0e-46a9-975e-18c414192d17",
                                       "params": {"enableRandomTime": False, "slidingWindowHours": 24}},
            "name": "", "outcomes": [self._simple_outcome(key, nxt)], "schema": {"arguments": {}}})

    def email(self, key: str, asset_id: str, nxt: str | None) -> None:
        asset = self.pkg.entities["assets"][asset_id]["data"]
        subject = asset["views"]["subjectline"]["content"]
        self.activities.append({
            "type": "EMAILV2",
            "arguments": {"activityId": "{{Activity.Id}}", "definitionId": "{{Context.DefinitionId}}",
                          "emailSubjectDataBound": subject, "contactId": "{{Contact.Id}}",
                          "contactKey": "{{Contact.Key}}", "emailAddress": "{{InteractionDefaults.Email}}",
                          "sourceCustomObjectId": "{{Contact.AddressInfo.Email.SourceCustomObjectId}}",
                          "sourceCustomObjectKey": "{{Contact.AddressInfo.Email.SourceCustomObjectKey}}",
                          "fieldType": "{{Contact.AddressInfo.Email.FieldType}}", "eventData": "{{Event}}",
                          "obfuscationProperties": "{{InteractionDefaults.Email.ObfuscationProperties}}",
                          "customObjectKey": "", "definitionInstanceId": "{{Context.DefinitionInstanceId}}"},
            "description": "", "id": self._uid(key), "key": key, "metaData": {"isConfigured": True, "sections": {}},
            "configurationArguments": {
                "triggeredSend": {
                    "autoAddSubscribers": True, "autoUpdateSubscribers": True, "bccEmail": "", "ccEmail": "",
                    "created": {"name": "", "date": ""}, "deliveryProfileId": "{{mcpm:deliveryProfile}}",
                    "dynamicEmailSubject": subject,
                    "emailId": ref("assets", asset_id, "data/email/legacy/legacyId"),
                    "emailSubject": subject, "exclusionFilter": "", "isMultipart": True,
                    "isSalesforceTracking": True, "isSendLogging": True, "isStoppedOnJobError": False,
                    "isTrackingClicks": True, "modified": {"name": "", "date": ""}, "preHeader": "",
                    "priority": 4, "senderProfileId": "{{mcpm:senderProfile}}",
                    "sendClassificationId": "{{mcpm:sendClassification-transactional}}"},
                "applicationExtensionKey": "jb-email-activity"},
            "name": asset["name"], "outcomes": [self._simple_outcome(key, nxt)], "schema": {"arguments": {}}})

    def update(self, key: str, name: str, table: str, writes: list[tuple[str, object]], nxt: str | None) -> None:
        de_id = self.pkg.tables[table]
        fields = [{"dataExtensionId": ref("dataExtensions", de_id, "id"),
                   "field": ref("dataExtensions", de_id, "fields/name:%s/id" % f), "value": v}
                  for f, v in writes]
        self.activities.append({
            "type": "UPDATECONTACTDATA", "arguments": {"activityData": {"updateContactFields": fields}},
            "description": "", "id": self._uid(key), "key": key, "metaData": {"isConfigured": True},
            "configurationArguments": {"isActivityBatchValidated": True},
            "name": name, "outcomes": [self._simple_outcome(key, nxt, join_label=True)],
            "schema": {"arguments": {}}})

    def other(self, key: str, raw_type: str, name: str, nxt: str | None) -> None:
        """An activity whose type is not in the spec's supported list (kept minimal on purpose)."""
        self.activities.append({
            "type": raw_type, "arguments": {}, "description": "", "id": self._uid(key), "key": key,
            "metaData": {"isConfigured": True}, "configurationArguments": {},
            "name": name, "outcomes": [self._simple_outcome(key, nxt)], "schema": {"arguments": {}}})


# ---------------------------------------------------------------------------
# Entry source, automation, query, journey, asset
# ---------------------------------------------------------------------------

def add_email_asset(pkg: Package, asset_id: str, name: str, subject: str, with_link: bool) -> None:
    if with_link:
        body = ('<p>Hello %%=v(@firstname)=%%,</p><p>Your offer is ready: '
                '<a href="https://www.example.com/offer" alias="offer">see the offer</a></p>')
        plain = "Hello, your offer is ready: https://www.example.com/offer"
    else:
        # No href and no anchor tag anywhere: the email has no link (like the pilot's "Demo Sky").
        body = "<p>Hello %%=v(@firstname)=%%,</p><p>Welcome. This message has no link.</p>"
        plain = "Hello, welcome. This message has no link."
    html = ('%%[ SET @firstname = AttributeValue("FirstName") ]%%\n<!DOCTYPE html>\n<html>\n<head>'
            '<meta charset="UTF-8" /><title>' + subject + '</title></head>\n<body>' + body + '</body>\n</html>')
    pkg.add("assets", asset_id, {
        "assetType": {"id": 208, "name": "htmlemail", "displayName": "HTML Email"},
        "customerKey": pkg.gid("asset-key", asset_id),
        "data": {"email": {"options": {"characterEncoding": "utf-8"}, "legacy": {"legacyId": int(asset_id)}}},
        "description": "", "name": name, "contentType": "application/vnd.etmc.email.Message; kind=paste",
        "views": {"subjectline": {"content": subject}, "preheader": {"content": ""},
                  "html": {"content": html}, "text": {"content": plain}},
        "availableViews": ["subjectline", "preheader", "html"]})


def add_entry_source(pkg: Package, journey_name: str, entry_table: str, query_name: str, sql: str) -> str:
    """Event definition (AutomationAudience) bound to the entry table, its automation and entry query."""
    entry_id = pkg.tables[entry_table]
    query_id = pkg.gid("query", query_name)
    automation_name = "A_%s_entry" % journey_name
    automation_id = pkg.gid("automation", automation_name)
    event_id = pkg.gid("event", journey_name)
    event_key = "DEAudience-" + pkg.gid("event-key", journey_name)
    pkg.add("queryActivities", query_id, {
        "name": query_name, "description": "", "isFrozen": False, "key": query_id.upper(),
        "queryText": sql, "targetId": ref("dataExtensions", entry_id, "id"),
        "targetUpdateTypeId": 1, "targetUpdateTypeName": "Update"})
    serialized = ('<QueryDefinition xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
                  'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                  'xmlns="http://exacttarget.com/wsdl/partnerAPI"><PartnerKey xsi:nil="true" />'
                  '<ObjectID>%s</ObjectID><Name>%s</Name></QueryDefinition>' % (query_id, query_name))
    pkg.add("automations", automation_id, {
        "description": "", "key": pkg.gid("automation-key", automation_name), "name": automation_name,
        "steps": [{"activities": [{"name": ref("queryActivities", query_id, "name"),
                                   "activityObjectId": ref("queryActivities", query_id, "id"),
                                   "objectTypeId": 300, "displayOrder": 0, "serializedObject": serialized}],
                   "annotation": "", "name": ""}],
        "startSource": {"typeId": 1, "schedule": {"iCalRecur": "FREQ=HOURLY;UNTIL=20790606;INTERVAL=2",
                                                  "startDate": "2025-12-31T00:00:00", "timeZoneId": 5}}})
    pkg.add("eventDefinitions", event_id, {
        "name": journey_name, "type": "AutomationAudience",
        "dataExtensionId": ref("dataExtensions", entry_id, "id"), "description": "",
        "eventDefinitionKey": event_key, "mode": "Production", "iconUrl": "/images/icon-data-extension.svg",
        "isVisibleInPicker": False, "category": "Audience",
        "schedule": {"startDateTime": "2025-12-31T00:00:00", "endDateTime": "2079-06-06T00:00:00",
                     "timeZone": TIMEZONE, "occurrences": 234180, "endType": "EndDate", "frequency": "Hourly",
                     "recurrencePattern": "Interval", "interval": 2},
        "disableDEDataLogging": False, "isPlatformObject": False,
        "metaData": {"criteriaDescription": None, "scheduleFlowMode": "automation"},
        "configurationArguments": {"unconfigured": False},
        "arguments": {"serializedObjectType": 9, "automationId": ref("automations", automation_id, "id"),
                      "eventDefinitionId": event_id, "eventDefinitionKey": event_key,
                      "dataExtensionId": entry_id, "criteria": "", "useHighWatermark": True,
                      "resetHighWatermark": False},
        "sourceApplicationExtensionId": "97e942ee-6914-4d3d-9e52-37ecb71f79ed"})
    return event_id


def add_journey(pkg: Package, journey: Journey, event_id: str) -> None:
    jid = pkg.gid("journey", journey.name)
    pkg.journey_id = jid
    event_key_ref = ref("eventDefinitions", event_id, "eventDefinitionKey")
    pkg.add("journeys", jid, {
        "name": journey.name, "key": pkg.gid("journey-key", journey.name), "workflowApiVersion": 1,
        "description": "", "channel": "",
        "defaults": {"email": ['{{Event.%s."Emailaddress"}}' % event_key_ref],
                     "properties": {"analyticsTracking": {"enabled": False, "analyticsType": "google",
                                                          "urlDomainsToTrack": []}}},
        "definitionType": "Multistep", "entryMode": "MultipleEntries", "executionMode": "Production",
        "exits": [], "goals": [], "metaData": {"highThroughputSending": {"email": False}},
        "triggers": [{
            "arguments": {"startActivityKey": "{{Context.StartActivityKey}}",
                          "dequeueReason": "{{Context.DequeueReason}}",
                          "lastExecutedActivityKey": "{{Context.LastExecutedActivityKey}}", "filterResult": "true"},
            "configurationArguments": {"filterDefinitionId": "00000000-0000-0000-0000-000000000000",
                                       "criteria": "", "schemaVersionId": 0},
            "description": "", "id": pkg.gid("trigger", journey.name), "key": "TRIGGER",
            "metaData": {"category": "Audience", "chainType": "None", "configurationRequired": False,
                         "entrySourceGroupConfigUrl": "jb:///data/entry/audience/entrysourcegroupconfig.json",
                         "eventDefinitionId": ref("eventDefinitions", event_id, "id"),
                         "eventDefinitionKey": event_key_ref,
                         "iconUrl": "/images/icon-data-extension.svg", "title": "Data Extension"},
            "name": journey.name, "outcomes": [], "type": "AutomationAudience"}],
        "activities": journey.activities, "version": 1})


def export(pkg: Package) -> dict:
    selected = {k: [] for k in SELECTED_KINDS}
    selected["journeys"] = [pkg.journey_id]
    return {"appVersion": "260.0.26", "id": pkg.gid("package", pkg.package_name)[:10],
            "name": pkg.package_name, "version": 1, "modelVersion": "9", "references": {}, "input": [],
            "config": {"preserveCategories": True, "storeImagesAsReferences": False},
            "entities": pkg.entities, "selectedEntities": selected}


# ---------------------------------------------------------------------------
# Table setups shared by the examples
# ---------------------------------------------------------------------------

def log_table(pkg: Package) -> None:
    """Written-only table: target of update-contact steps, read by nothing (no CSV expected)."""
    pkg.table("DE_Log", [text("ContactId", 50, pk=True, nullable=False), text("LastAction", 50),
                         date("ActionDate")], sendable_field="ContactId")


def setup_contact_only(pkg: Package, master_extra: list[dict]) -> None:
    """Entry + contact-level Master (+ written-only Log). Master -> Entry on ContactId."""
    pkg.table("DE_Entry", [text("ContactId", 50, pk=True, nullable=False), email_field()],
              sendable_field="ContactId")
    pkg.table("DE_Master", [text("ContactId", 50, pk=True, nullable=False), text("FirstName"),
                            email_field(), phone()] + master_extra, sendable_field="ContactId")
    log_table(pkg)
    pkg.contact_model([("DE_Master", "ContactId", ""), ("DE_Entry", "ContactId", "ContactId")])


def setup_with_config(pkg: Package, master_extra: list[dict], config_extra: list[dict]) -> None:
    """Entry + contact-level Master holding CampaignName + scenario-level Config (+ Log).

    Contact model as in the pilot: group -> Master (ContactId) -> Entry (ContactId) -> Config (CampaignName).
    """
    pkg.table("DE_Entry", [text("ContactId", 50, pk=True, nullable=False), email_field(),
                           text("CampaignName", 100, nullable=False)], sendable_field="ContactId")
    pkg.table("DE_Master", [text("ContactId", 50, pk=True, nullable=False), text("FirstName"), email_field(),
                            phone(), text("CampaignName", 100, nullable=False)] + master_extra,
              sendable_field="ContactId")
    pkg.table("DE_Config", [text("CampaignName", 100, pk=True, nullable=False)] + config_extra)
    log_table(pkg)
    pkg.contact_model([("DE_Master", "ContactId", ""), ("DE_Entry", "ContactId", "ContactId"),
                       ("DE_Config", "CampaignName", "CampaignName")])


def sql_master_only(pkg: Package, where: str = "") -> str:
    sql = "SELECT\n    M.ContactId,\n    M.Emailaddress\nFROM %s M" % table_sql(pkg.tables["DE_Master"])
    if where:
        sql += "\nWHERE " + where
    return sql


def sql_master_config(pkg: Package) -> str:
    return ("SELECT\n    M.ContactId,\n    M.Emailaddress,\n    C.CampaignName\nFROM %s M\nJOIN %s C ON M.CampaignName = C.CampaignName"
            % (table_sql(pkg.tables["DE_Master"]), table_sql(pkg.tables["DE_Config"])))


def log_update(j: Journey, key: str, name: str, action: str, nxt: str | None) -> None:
    j.update(key, name, "DE_Log", [("LastAction", action), ("ActionDate", "{{DateTime.Now}}")], nxt)


# ---------------------------------------------------------------------------
# The examples
# ---------------------------------------------------------------------------

def build_one_split() -> dict:
    pkg = Package("one_split", "EX one split")
    setup_contact_only(pkg, [boolean("HasLoyaltyCard")])
    j = Journey(pkg, "EX_OneSplit")
    j.decision("SPLIT-1", "Loyalty card?", [
        ("path_a", "true", criteria(pkg.cond("DE_Master", "HasLoyaltyCard", "Is", "true")), "UPDATE-1",
         "HasLoyaltyCard is True"),
        ("remainder_path", "false", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "Log loyal", "Loyal", None)
    log_update(j, "UPDATE-2", "Log not loyal", "NotLoyal", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_OneSplit_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_repeated_splits() -> dict:
    pkg = Package("repeated_splits", "EX repeated splits")
    setup_with_config(pkg, [], [text("CHANNEL", 50)])

    def channel(value):
        return criteria(pkg.cond("DE_Config", "CHANNEL", "Equal", value))

    j = Journey(pkg, "EX_RepeatedSplits")
    j.decision("SPLIT-1", "Channel first check", [
        ("path_a", "A", channel("A"), "UPDATE-1", "CHANNEL equal A"),
        ("remainder_path", "Remainder", None, "SPLIT-3", None)])
    log_update(j, "UPDATE-1", "Mark A", "A", "SPLIT-2")
    j.decision("SPLIT-2", "Channel check after A", [
        ("path_a", "B", channel("B"), "UPDATE-2", "CHANNEL equal B"),
        ("path_b", "A", channel("A"), "UPDATE-3", "CHANNEL equal A"),
        ("remainder_path", "Remainder", None, None, None)])
    log_update(j, "UPDATE-2", "Mark B after A", "BAfterA", None)
    log_update(j, "UPDATE-3", "Confirm A", "ConfirmA", None)
    j.decision("SPLIT-3", "Channel check after not A", [
        ("path_a", "B", channel("B"), "UPDATE-4", "CHANNEL equal B"),
        ("path_b", "A", channel("A"), "UPDATE-5", "CHANNEL equal A"),
        ("remainder_path", "Remainder", None, "WAIT-1", None)])
    log_update(j, "UPDATE-4", "Mark B", "B", None)
    log_update(j, "UPDATE-5", "Mark A late", "ALate", None)
    j.wait_duration("WAIT-1", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_RepeatedSplits_Entry", sql_master_config(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_and_or_null() -> dict:
    pkg = Package("and_or_null", "EX and or null")
    setup_contact_only(pkg, [boolean("IsActive"), boolean("IsMailOffers"), boolean("IsDunning"),
                             boolean("PhoneConsent")])
    m = "DE_Master"
    j = Journey(pkg, "EX_AndOrNull")
    j.decision("SPLIT-1", "Phone consent", [
        ("path_a", "Consent", criteria(
            pkg.cond(m, "IsMailOffers", "Is", "true"),
            cset("OR", pkg.cond(m, "IsDunning", "Is", "false"), pkg.cond(m, "IsDunning", "IsNull", "")),
            pkg.cond(m, "PhoneConsent", "Is", "true")), "UPDATE-1",
         "IsMailOffers is True AND ( IsDunning is False OR IsDunning is null  ) AND PhoneConsent is True"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "Phone allowed", "PhoneAllowed", "SPLIT-2")
    log_update(j, "UPDATE-2", "Phone not allowed", "PhoneNotAllowed", None)
    j.decision("SPLIT-2", "Dunning unknown?", [
        ("path_a", "Unknown", criteria(pkg.cond(m, "IsDunning", "IsNull", "")), "UPDATE-3", "IsDunning is null"),
        ("remainder_path", "Remainder", None, "UPDATE-4", None)])
    log_update(j, "UPDATE-3", "Dunning unknown", "DunningUnknown", None)
    log_update(j, "UPDATE-4", "Not dunning", "NotDunning", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_AndOrNull_Entry",
                             sql_master_only(pkg, "M.IsActive = 'True'"))
    add_journey(pkg, j, event)
    return export(pkg)


def build_two_levels() -> dict:
    """Pilot-like tables: Master -> Target (ContactId = SubscriberId) -> Config (CampaignName), entry filters."""
    pkg = Package("two_levels", "EX two levels")
    pkg.table("DE_Entry", [text("ContactId", 50, pk=True, nullable=False), email_field(),
                           text("CampaignName", 100, nullable=False), text("ActionId", 255)],
              sendable_field="ContactId")
    pkg.table("DE_Master", [text("ContactId", 50, pk=True, nullable=False), text("FirstName"), email_field(),
                            phone(), boolean("MarketingPermission", default="False"), boolean("IsMailOffers")],
              sendable_field="ContactId")
    pkg.table("DE_Target", [text("SubscriberId", 50, pk=True, nullable=False),
                            text("CampaignName", 100, pk=True, nullable=False),
                            date("CreatedDate", nullable=False, default="GetDate()")],
              sendable_field="SubscriberId")
    pkg.table("DE_Config", [text("CampaignName", 100, pk=True, nullable=False),
                            date("START_DATE", nullable=False, default="GETDATE()"),
                            date("END_DATE", nullable=False, default="GETDATE()"),
                            text("CHANNEL", 50, nullable=False),
                            boolean("IGNOREPERM", nullable=False, default="False"),
                            boolean("ACTIVE", nullable=False, default="False"),
                            boolean("HAS_RUN", nullable=False, default="False")])
    log_table(pkg)
    pkg.contact_model([("DE_Master", "ContactId", ""), ("DE_Entry", "ContactId", "ContactId"),
                       ("DE_Config", "CampaignName", "CampaignName")])
    c, m = "DE_Config", "DE_Master"
    j = Journey(pkg, "EX_TwoLevels")
    j.decision("SPLIT-1", "Channel", [
        ("path_a", "Email", criteria(pkg.cond(c, "CHANNEL", "Equal", "Email")), "SPLIT-2", "CHANNEL equal Email"),
        ("path_b", "DM", criteria(pkg.cond(c, "CHANNEL", "Equal", "DM")), "SPLIT-4", "CHANNEL equal DM"),
        ("remainder_path", "Remainder", None, None, None)])
    j.decision("SPLIT-2", "Ignore permission", [
        ("path_a", "false", criteria(pkg.cond(c, "IGNOREPERM", "Is", "false")), "SPLIT-3", "IGNOREPERM is False"),
        ("remainder_path", "true", None, "UPDATE-1", None)])
    j.decision("SPLIT-3", "Marketing permission", [
        ("path_a", "true", criteria(pkg.cond(m, "MarketingPermission", "Is", "true")), "UPDATE-2",
         "MarketingPermission is True"),
        ("remainder_path", "false", None, "UPDATE-3", None)])
    j.decision("SPLIT-4", "Mail offers", [
        ("path_a", "true", criteria(pkg.cond(m, "IsMailOffers", "Is", "true")), "UPDATE-4", "IsMailOffers is True"),
        ("remainder_path", "false", None, "UPDATE-5", None)])
    log_update(j, "UPDATE-1", "Email forced", "EmailForced", None)
    log_update(j, "UPDATE-2", "Email allowed", "EmailAllowed", None)
    log_update(j, "UPDATE-3", "Email blocked", "EmailBlocked", None)
    log_update(j, "UPDATE-4", "DM sent", "DMSent", None)
    log_update(j, "UPDATE-5", "DM skipped", "DMSkipped", None)
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress,\n    C.CampaignName,\n"
           "CONCAT(\n    M.ContactId, '_',\n    C.CampaignName, '_',\n    FORMAT(GETUTCDATE(), 'yyyyMMdd_HHmm')\n) AS ActionId\n"
           "FROM %s M\nJOIN %s T ON M.ContactId = T.SubscriberId\nJOIN %s C on T.CampaignName = C.CampaignName\n"
           "where C.ACTIVE = 'True'\n"
           "and (CAST(GETUTCDATE() AS DATE) >= CAST(START_DATE AS DATE) AND CAST(GETUTCDATE() AS DATE) <= CAST(END_DATE AS DATE))\n"
           "AND C.HAS_RUN = 'False'"
           % (table_sql(pkg.tables["DE_Master"]), table_sql(pkg.tables["DE_Target"]),
              table_sql(pkg.tables["DE_Config"])))
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_TwoLevels_Entry", sql)
    add_journey(pkg, j, event)
    return export(pkg)


def _engagement_tables(pkg: Package) -> None:
    setup_with_config(pkg, [boolean("MarketingPermission")],
                      [date("WAIT1", nullable=False, default="GETDATE()")])


def build_engagement_sent(with_link: bool = False, example: str = "engagement_sent") -> dict:
    pkg = Package(example, "EX " + example.replace("_", " "))
    _engagement_tables(pkg)
    add_email_asset(pkg, "900101", "Welcome", "WELCOME", with_link=with_link)
    j = Journey(pkg, "EX_EngagementSent" if not with_link else "EX_EngagementClick")
    j.decision("SPLIT-1", "MP email", [
        ("path_a", "true", criteria(pkg.cond("DE_Master", "MarketingPermission", "Is", "true")), "STO-1",
         "MarketingPermission is True"),
        ("remainder_path", "false", None, "UPDATE-4", None)])
    j.sto_wait("STO-1", "EMAIL-1")
    j.email("EMAIL-1", "900101", "WAITATTR-1")
    j.wait_attribute("WAITATTR-1", "DE_Config", "WAIT1", "ENGAGE-1")
    j.engagement("ENGAGE-1", "Open Welcome", "open", "EMAIL-1", "Welcome", yes_next="ENGAGE-2", no_next="UPDATE-1")
    j.engagement("ENGAGE-2", "Click Welcome", "click", "EMAIL-1", "Welcome", yes_next="UPDATE-2", no_next="UPDATE-3")
    log_update(j, "UPDATE-1", "Not opened", "NotOpened", None)
    log_update(j, "UPDATE-2", "Clicked", "Clicked", None)
    log_update(j, "UPDATE-3", "Opened not clicked", "OpenedNotClicked", None)
    log_update(j, "UPDATE-4", "No permission", "NoPermission", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_%s_Entry" % j.name, sql_master_config(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_engagement_not_sent() -> dict:
    pkg = Package("engagement_not_sent", "EX engagement not sent")
    _engagement_tables(pkg)
    add_email_asset(pkg, "900102", "Welcome", "WELCOME", with_link=False)
    j = Journey(pkg, "EX_EngagementNotSent")
    j.decision("SPLIT-1", "MP email", [
        ("path_a", "true", criteria(pkg.cond("DE_Master", "MarketingPermission", "Is", "true")), "EMAIL-1",
         "MarketingPermission is True"),
        ("remainder_path", "false", None, "WAITATTR-1", None)])
    j.email("EMAIL-1", "900102", "UPDATE-1")
    log_update(j, "UPDATE-1", "Email sent", "EmailSent", None)
    j.wait_attribute("WAITATTR-1", "DE_Config", "WAIT1", "ENGAGE-1")
    j.engagement("ENGAGE-1", "Open Welcome", "open", "EMAIL-1", "Welcome", yes_next="UPDATE-2", no_next="UPDATE-3")
    log_update(j, "UPDATE-2", "Opened", "Opened", None)
    log_update(j, "UPDATE-3", "Not opened", "NotOpened", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_EngagementNotSent_Entry", sql_master_config(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_random_split() -> dict:
    pkg = Package("random_split", "EX random split")
    setup_with_config(pkg, [boolean("IsMailOffers")],
                      [boolean("Control Group Required", nullable=False, default="False")])
    j = Journey(pkg, "EX_RandomSplit")
    j.decision("SPLIT-1", "Control group required", [
        ("path_a", "true", criteria(pkg.cond("DE_Config", "Control Group Required", "Is", "true")), "RANDOM-1",
         "Control Group Required is True"),
        ("remainder_path", "false", None, "SPLIT-2", None)])
    j.random_split("RANDOM-1", [("branchResult-1", 90, "UPDATE-1"), ("branchResult-2", 10, "UPDATE-2")])
    log_update(j, "UPDATE-1", "Treatment group", "Treatment", "SPLIT-2")
    log_update(j, "UPDATE-2", "Control group", "Control", "WAIT-1")
    j.wait_duration("WAIT-1", None)
    j.decision("SPLIT-2", "Mail offers", [
        ("path_a", "true", criteria(pkg.cond("DE_Master", "IsMailOffers", "Is", "true")), "UPDATE-3",
         "IsMailOffers is True"),
        ("remainder_path", "false", None, "UPDATE-4", None)])
    log_update(j, "UPDATE-3", "Offer sent", "OfferSent", None)
    log_update(j, "UPDATE-4", "No offer", "NoOffer", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_RandomSplit_Entry", sql_master_config(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_unsupported_element() -> dict:
    pkg = Package("unsupported_element", "EX unsupported element")
    setup_contact_only(pkg, [boolean("IsMailOffers"), boolean("PhoneConsent")])
    m = "DE_Master"
    j = Journey(pkg, "EX_UnsupportedElement")
    j.decision("SPLIT-1", "Mail offers", [
        ("path_a", "true", criteria(pkg.cond(m, "IsMailOffers", "Is", "true")), "PUSH-1", "IsMailOffers is True"),
        ("remainder_path", "false", None, "UPDATE-1", None)])
    j.other("PUSH-1", "PUSHNOTIFICATIONACTIVITY", "Send push notification", "SPLIT-2")
    j.decision("SPLIT-2", "Phone consent", [
        ("path_a", "true", criteria(pkg.cond(m, "PhoneConsent", "Is", "true")), "UPDATE-2", "PhoneConsent is True"),
        ("remainder_path", "false", None, "UPDATE-3", None)])
    log_update(j, "UPDATE-1", "No offers", "NoOffers", None)
    log_update(j, "UPDATE-2", "SMS consent", "SmsConsent", None)
    log_update(j, "UPDATE-3", "No SMS consent", "NoSmsConsent", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_UnsupportedElement_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_update_then_split() -> dict:
    pkg = Package("update_then_split", "EX update then split")
    setup_contact_only(pkg, [text("Segment", 20), date("SegmentChangedOn")])
    m = "DE_Master"

    def segment(value):
        return criteria(pkg.cond(m, "Segment", "Equal", value))

    j = Journey(pkg, "EX_UpdateThenSplit")
    j.decision("SPLIT-1", "Gold segment?", [
        ("path_a", "Gold", segment("Gold"), "UPDATE-1", "Segment equal Gold"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    j.update("UPDATE-1", "Downgrade to Silver", m, [("Segment", "Silver"), ("SegmentChangedOn", "{{DateTime.Now}}")],
             "SPLIT-2")
    log_update(j, "UPDATE-2", "Not gold", "NotGold", None)
    j.decision("SPLIT-2", "Segment after downgrade", [
        ("path_a", "Gold", segment("Gold"), "UPDATE-3", "Segment equal Gold"),
        ("path_b", "Silver", segment("Silver"), "UPDATE-4", "Segment equal Silver"),
        ("remainder_path", "Remainder", None, "UPDATE-5", None)])
    log_update(j, "UPDATE-3", "Still gold", "StillGold", None)
    log_update(j, "UPDATE-4", "Now silver", "NowSilver", None)
    log_update(j, "UPDATE-5", "Other segment", "OtherSegment", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_UpdateThenSplit_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_case_mismatch() -> dict:
    pkg = Package("case_mismatch", "EX case mismatch")
    setup_with_config(pkg, [], [text("CHANNEL", 50)])
    c = "DE_Config"
    j = Journey(pkg, "EX_CaseMismatch")
    j.decision("SPLIT-1", "Channel", [
        ("path_a", "Email", criteria(pkg.cond(c, "CHANNEL", "Equal", "Email")), "UPDATE-1", "CHANNEL equal Email"),
        ("path_b", "DM", criteria(pkg.cond(c, "CHANNEL", "Equal", "DM")), "UPDATE-2", "CHANNEL equal DM"),
        ("remainder_path", "Remainder", None, "WAIT-1", None)])
    log_update(j, "UPDATE-1", "Email channel", "EmailChannel", "SPLIT-2")
    log_update(j, "UPDATE-2", "DM channel", "DMChannel", None)
    j.wait_duration("WAIT-1", None)
    j.decision("SPLIT-2", "channel email?", [
        ("path_a", "true", criteria(pkg.cond(c, "CHANNEL", "Equal", "EMAIL")), "UPDATE-3", "CHANNEL equal EMAIL"),
        ("remainder_path", "false", None, "UPDATE-4", None)])
    log_update(j, "UPDATE-3", "Email confirmed", "EmailConfirmed", None)
    log_update(j, "UPDATE-4", "Email not confirmed", "EmailNotConfirmed", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_CaseMismatch_Entry", sql_master_config(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_unsupported_operator() -> dict:
    pkg = Package("unsupported_operator", "EX unsupported operator")
    setup_contact_only(pkg, [boolean("IsMailOffers"), text("PostalCode", 10)])
    m = "DE_Master"
    j = Journey(pkg, "EX_UnsupportedOperator")
    j.decision("SPLIT-1", "Mail offers", [
        ("path_a", "true", criteria(pkg.cond(m, "IsMailOffers", "Is", "true")), "SPLIT-2", "IsMailOffers is True"),
        ("remainder_path", "false", None, "UPDATE-1", None)])
    # "Between" was not in the spec's operator list when this example was written; the extension section
    # added it with the value "low|high". '10000' is not written that way, so the branch stays unsupported.
    j.decision("SPLIT-2", "Postal code range", [
        ("path_a", "North", criteria(pkg.cond(m, "PostalCode", "Between", "10000")), "UPDATE-2",
         "PostalCode between 10000"),
        ("remainder_path", "Remainder", None, "UPDATE-3", None)])
    log_update(j, "UPDATE-1", "No offers", "NoOffers", None)
    log_update(j, "UPDATE-2", "North", "North", None)
    log_update(j, "UPDATE-3", "Elsewhere", "Elsewhere", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_UnsupportedOperator_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


def build_boolean_exhaustive() -> dict:
    pkg = Package("boolean_exhaustive", "EX boolean exhaustive")
    setup_contact_only(pkg, [boolean("StrictFlag", nullable=False, default="False"), boolean("LooseFlag")])
    m = "DE_Master"
    j = Journey(pkg, "EX_BooleanExhaustive")
    j.decision("SPLIT-1", "Strict flag", [
        ("path_a", "true", criteria(pkg.cond(m, "StrictFlag", "Is", "true")), "SPLIT-2", "StrictFlag is True"),
        ("path_b", "false", criteria(pkg.cond(m, "StrictFlag", "Is", "false")), "SPLIT-2", "StrictFlag is False"),
        ("remainder_path", "Remainder", None, "UPDATE-1", None)])
    j.decision("SPLIT-2", "Loose flag", [
        ("path_a", "true", criteria(pkg.cond(m, "LooseFlag", "Is", "true")), "UPDATE-2", "LooseFlag is True"),
        ("path_b", "false", criteria(pkg.cond(m, "LooseFlag", "Is", "false")), "UPDATE-3", "LooseFlag is False"),
        ("remainder_path", "Remainder", None, "UPDATE-4", None)])
    log_update(j, "UPDATE-1", "Strict flag empty", "StrictEmpty", None)
    log_update(j, "UPDATE-2", "Loose true", "LooseTrue", None)
    log_update(j, "UPDATE-3", "Loose false", "LooseFalse", None)
    log_update(j, "UPDATE-4", "Loose empty", "LooseEmpty", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_BooleanExhaustive_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


# Example directory -> builder. Directories sharing a builder get the same package.json.
EXAMPLES = {
    "one_split": build_one_split,
    "repeated_splits": build_repeated_splits,
    "and_or_null": build_and_or_null,
    "two_levels": build_two_levels,
    "engagement_sent": build_engagement_sent,
    "engagement_no_inbox": build_engagement_sent,          # same package, no real inbox in settings
    "engagement_click": lambda: build_engagement_sent(with_link=True, example="engagement_click"),
    "engagement_not_sent": build_engagement_not_sent,
    "random_split": build_random_split,
    "unsupported_element": build_unsupported_element,
    "update_then_split": build_update_then_split,
    "case_mismatch": build_case_mismatch,
    "case_mismatch_insensitive": build_case_mismatch,      # same package, case-insensitive setting
    "unsupported_operator": build_unsupported_operator,
    "boolean_exhaustive": build_boolean_exhaustive,
}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

REF_RE = re.compile(r"\{\{mcpm#/entities/([^/]+)/([^/]+)/data/([^}]*)\}\}")
SPLIT_TYPES = ("MULTICRITERIADECISION", "ENGAGEMENTDECISION", "RANDOMSPLIT")


def validate_package(path: Path) -> list[tuple[str, str]]:
    """Check one package.json; return its split branches as (activity key, outcome key)."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    entities = doc["entities"]
    journeys = doc["selectedEntities"]["journeys"]
    assert len(journeys) == 1 and journeys[0] in entities["journeys"], "%s: selected journey" % path
    for m in REF_RE.finditer(path.read_text(encoding="utf-8")):
        kind, eid = m.group(1), m.group(2)
        assert eid in entities.get(kind, {}), "%s: dangling reference %s" % (path, m.group(0))
    journey = entities["journeys"][journeys[0]]["data"]
    keys = [a["key"] for a in journey["activities"]]
    assert len(keys) == len(set(keys)), "%s: duplicate activity keys" % path
    targets = set()
    branches = []
    for a in journey["activities"]:
        okeys = [o["key"] for o in a["outcomes"]]
        assert len(okeys) == len(set(okeys)), "%s: duplicate outcome keys in %s" % (path, a["key"])
        for o in a["outcomes"]:
            nxt = o.get("next")
            if nxt is not None:
                assert nxt in keys, "%s: %s/%s -> %s does not exist" % (path, a["key"], o["key"], nxt)
                targets.add(nxt)
            if a["type"] in SPLIT_TYPES:
                branches.append((a["key"], o["key"]))
        if a["type"] == "MULTICRITERIADECISION":
            crit = a["configurationArguments"]["criteria"]
            for o in a["outcomes"]:
                if o["key"] == "remainder_path":
                    assert o["key"] not in crit, "%s: remainder with criteria in %s" % (path, a["key"])
                else:
                    ET.fromstring(crit[o["key"]])          # raises on malformed XML
        if a["type"] == "ENGAGEMENTDECISION":
            assert a["configurationArguments"]["refActivityCustomerKey"] in keys
    starts = [k for k in keys if k not in targets]
    assert len(starts) == 1, "%s: expected one start activity, found %s" % (path, starts)
    return branches


STATUSES = ("covered", "covered with manual action", "not covered")


def validate_expected(path: Path, branches: list[tuple[str, str]]) -> None:
    """Every split outcome is listed once, in `branches` or (pilot only) in `branches_any_of`."""
    expected = json.loads(path.read_text(encoding="utf-8"))
    listed = set(expected["branches"])
    any_of = set(expected.get("branches_any_of", {}))
    assert not listed & any_of, "%s: listed twice %s" % (path, sorted(listed & any_of))
    actual = {"%s/%s" % b for b in branches}
    missing = actual - listed - any_of
    extra = (listed | any_of) - actual
    assert not missing and not extra, "%s: branches missing %s, unknown %s" % (path, sorted(missing), sorted(extra))
    for key, value in expected["branches"].items():
        assert value["status"] in STATUSES, (path, key)
    for key, values in expected.get("branches_any_of", {}).items():
        assert values and all(v in STATUSES for v in values), (path, key)


def package_path(directory: Path) -> Path:
    """package.json next to expected.json, or the file named by expected.json's "package" (the pilot)."""
    expected = directory / "expected.json"
    if expected.exists():
        other = json.loads(expected.read_text(encoding="utf-8")).get("package")
        if other:
            return (directory / other).resolve()
    return directory / "package.json"


def validate(example_dirs: list[str]) -> None:
    for name in example_dirs:
        directory = HERE / name
        branches = validate_package(package_path(directory))
        expected = directory / "expected.json"
        if expected.exists():
            validate_expected(expected, branches)
        print("ok  %-28s %3d split branches" % (name, len(branches)))


# Examples that use an existing export instead of a built package.json (expected.json has "package").
EXTERNAL_EXAMPLES = ["pilot", "pilot_case_insensitive"]


def main() -> int:
    for name, builder in EXAMPLES.items():
        directory = HERE / name
        directory.mkdir(exist_ok=True)
        text_out = json.dumps(builder(), indent=1, ensure_ascii=False) + "\n"
        (directory / "package.json").write_text(text_out, encoding="utf-8")
    validate(list(EXAMPLES) + [n for n in EXTERNAL_EXAMPLES if (HERE / n / "expected.json").exists()])
    return 0


if __name__ == "__main__":
    sys.exit(main())
