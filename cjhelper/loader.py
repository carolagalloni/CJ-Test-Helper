"""Read a Salesforce Marketing Cloud package export into a ``Package``.

Spec, "Input: the package export": "The only input is the JSON file exported from
Package Manager, which already contains everything the generator needs." This module
reads, from ``entities``: ``journeys``, ``eventDefinitions``, ``dataExtensions`` (and
``sharedDataExtensions``), ``attributeSets``, ``attributeRelationships``,
``automations``, ``queryActivities`` and ``assets`` (docs/export-format.md).

Spec rules applied here:

- "References written as {{mcpm#/entities/...}} are resolved to the real names before
  anything else." -> ``resolve_references`` runs on the whole export first.
- "If the export holds more than one journey, the app uses the one listed in
  selectedEntities.journeys." -> ``_choose_journey``.
- "Split criteria are XML (FilterDefinition) with nested AND/OR groups." ->
  ``criteria.parse_criteria``; condition tables are attribute-set names, mapped to
  table names through the attribute sets.
- "New activity types and operators are added by extending one lookup table each." ->
  ``model.ACTIVITY_KINDS`` maps the export type to a kind; ``WAIT_UI_TYPES`` tells the
  two supported waits apart; ``ENGAGEMENT_KINDS`` maps engagement statistics types.
- "Anything else is not an error: it is listed in the report, and the branches behind
  it are marked as not covered." -> an activity the generator does not understand gets
  ``KIND_UNSUPPORTED`` and its key goes to ``Package.unsupported_activities``.
- "A file that is not valid JSON, or holds no journey, gives a plain error message and
  nothing else." -> ``LoadError``; also raised when the entry table cannot be found.

Rules of the spec section "Extensions beyond the pilot (agreed 12 October 2026)" applied here:

- Journey Data: a condition ``Event.<eventDefinitionKey>.<Field>`` reads column ``Field`` of the
  trigger's entry table; the condition keeps ``source`` SOURCE_JOURNEY. A key naming another event
  definition is unsupported. The same mapping applies to a wait by attribute written that way.
- Attribute compared with an attribute: supported only as "the field linked to the contact key of a
  contact-level table Equal the contact key", normalised so that the condition names that field and
  reads "the contact has a row in that table"; any other form is unsupported, with its reason.
- ``Between``: a value not written ``low|high`` makes the branch unsupported.
- Missing relationship: a Contact Data condition on an attribute set that no relationship of the
  export names is unsupported, "relazione mancante nell'export" (``Package.missing_relationships``).
- Package Manager ``issues`` of level warning or error (top level and per entity) become warnings; the
  level is the most severe of the issue and its cause. On content the journey uses only indirectly (an
  image inside a sent email, a landing page the journey reaches through references) only the errors are
  kept (team decision, 13 October 2026).
- Entry source: the event definition's type and ``metaData.scheduleFlowMode`` give
  ``EntryEvent.kind`` (``ENTRY_SOURCE_TYPES`` and ``SCHEDULE_FLOW_MODES``, one lookup table each). An
  ``automationId`` is a feeding automation only for an automation entry: on a data extension entry it
  is the scheduler Journey Builder creates (assumption). Entry filters and goals are not modelled: when
  present they are reported (exit criteria: see step 3 below). ``entryMode`` is kept on the journey.
- Warnings about assets and landing pages that no Email (or SMS) activity of the journey sends are dropped.
- A journey default (email, mobile number) naming a column the entry table lacks is reported.

Rules of the spec section "Extensions for time-dependent journeys (step 3, 14 October 2026)" applied here:

- SMS sends: ``SMSSYNC`` is ``KIND_SMS`` (no branching); the journey's default mobile number
  (``defaults.mobileNumber``) is read as the default email is.
- The clock: a wait by attribute keeps its offset (``waitDuration`` and ``waitUnit``, possibly negative)
  and its time of day (``specifiedTime``); a WAIT without ``metaData.uiType`` is told apart by its date
  expression or its key.
- Tables reached through other tables: the path from the contact to the table a condition or a wait by
  attribute reads (the relationship ids of the condition, the segments of the wait's expression) is kept
  as table names, nearest the contact first (``Condition.path``, ``Activity.wait_path``).
- Relative dates: a relative value on a field that is not a Date, in a unit criteria.py does not list or
  with an operator that does not take one (``criteria.RELATIVE_OPERATORS``) is unsupported.
- Exit criteria and goals are parsed like decision split criteria (``Journey.exits``, ``Journey.goals``);
  the exit criteria the generator can evaluate are reported as evaluated at the end of every wait, the
  others and the goals as not modelled.
- Synchronized data extensions: a table Package Manager marks NO_DEPLOY / ManagedDataExtension is
  ``Table.managed`` and reported.

Everything odd met while reading, and every reading the spec does not fix, goes to
``Package.warnings`` in plain words. Nothing here knows the pilot: every name is read
from the export. Field names are written with the schema's spelling (docs/plan.md:
"Field lookups are case-insensitive; the schema spelling is what gets written").
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Optional

from . import criteria as criteria_rules
from .criteria import OPERATORS, CriteriaError, between_bounds, parse_criteria, split_attribute
from .model import (
    ACTIVITY_KINDS,
    ENTRY_AUTOMATION,
    ENTRY_OTHER,
    ENTRY_RUN_ONCE,
    ENTRY_SCHEDULED,
    FIELD_TYPES,
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
    Activity,
    Condition,
    ConditionGroup,
    EmailAsset,
    EntryEvent,
    Field,
    Formula,
    Journey,
    JourneyFilter,
    Outcome,
    Package,
    Query,
    Relationship,
    Table,
    Value,
    Write,
    formula_conditions,
    formula_text,
)


class LoadError(Exception):
    """The export cannot be used: not valid JSON, no journey, or no entry table. The message is plain words."""


# Engagement split statsTypeId -> the action the tester must take (docs/export-format.md:
# "statsTypeId 2 = open, 3 = click"). Any other value is reported, never guessed.
ENGAGEMENT_KINDS: dict[int, str] = {2: "open", 3: "click"}

# model.ACTIVITY_KINDS maps the export type WAIT to a wait by duration; the export tells
# the kinds of wait apart by metaData.uiType. Spec, "Supported in the PoC": wait by
# duration and wait by attribute. Any other uiType is unsupported (reported, not guessed).
WAIT_UI_TYPES: dict[str, str] = {
    "WAITBYDURATION": KIND_WAIT_DURATION,
    "WAITBYATTRIBUTE": KIND_WAIT_ATTRIBUTE,
}

# Spec, step 3, "The clock": the units a wait by attribute's offset (waitUnit) can be written in. Any other unit
# is kept as written and reported: the end of that wait cannot be computed.
WAIT_UNITS = frozenset({"MINUTES", "HOURS", "DAYS", "WEEKS", "MONTHS", "YEARS"})

QUERY_OBJECT_TYPE = 300     # automation step activity that runs a query (1000 = data verification)

# Entry source (spec, "Entry source and procedure"): the event definition's type gives the kind of source.
# "EmailAudience" is the data extension entry source; its metaData.scheduleFlowMode then says how it is
# scheduled (SCHEDULE_FLOW_MODES). Any type not listed is ENTRY_OTHER (CloudPages form, API event, ...).
DATA_EXTENSION_SOURCE = "data extension"
ENTRY_SOURCE_TYPES: dict[str, str] = {
    "AutomationAudience": ENTRY_AUTOMATION,
    "EmailAudience": DATA_EXTENSION_SOURCE,
}
# metaData.scheduleFlowMode of a data extension entry source ("automation" in the pilot, "runOnce" in the
# Run Once exports). Any other value, or none, is read as a recurring schedule, the spec's "procedure to
# verify", with a warning: no export with a recurring schedule has been seen.
SCHEDULE_FLOW_MODES: dict[str, str] = {
    "automation": ENTRY_AUTOMATION,
    "runOnce": ENTRY_RUN_ONCE,
}
ZERO_GUID = "00000000-0000-0000-0000-000000000000"     # filterDefinitionId of a trigger without entry filter

# Kinds of content entities. A warning about one of them is dropped unless an Email activity of the
# journey sends it (spec: "Warnings about assets the journey does not use are dropped").
CONTENT_KINDS = frozenset({"assets", "landingPages", "primaryLandingPages", "cloudPageCollections"})

# Package Manager issue levels kept as warnings (spec: "Package Manager issues of level warning or error").
ISSUE_LEVELS = ("WARNING", "ERROR")

# Spec, step 3, "Synchronized data extensions": the Package Manager issue fields, and their values, that mark a
# managed data extension (functionality NO_DEPLOY, objectType ManagedDataExtension); the entity kinds that hold tables.
MANAGED_MARKS: dict[str, frozenset[str]] = {
    "functionality": frozenset({"NO_DEPLOY"}),
    "objectType": frozenset({"ManagedDataExtension"}),
}
TABLE_KINDS = ("dataExtensions", "sharedDataExtensions")

# Spec, step 3, "Exit criteria and goals": the reason given for an exit criterion or a goal without criteria.
NO_CRITERIA = "nessun criterio"

REFERENCE = re.compile(r"\{\{mcpm#/entities/([^/{}]+)/([^/{}]+)/data/([^{}]*)\}\}")
FIELD_ID_PATH = re.compile(r"^fields/name:(.+)/id$")
LINK = re.compile(r"href\s*=|<a\s", re.IGNORECASE)
MAX_REFERENCE_ROUNDS = 10   # a value that holds a reference is resolved again, this many times at most


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def load_package(source: Any) -> Package:
    """Read a package export and return its journey, tables, relationships and queries.

    ``source`` is the export as bytes, as JSON text, as a path (``str`` or
    ``pathlib.Path``) or as an already parsed dict (left unchanged). Raises LoadError
    with a plain message when it is not valid JSON, holds no journey, or the journey's
    entry table cannot be found.
    """
    export = _read_export(source)
    entities = export.get("entities")
    if not isinstance(entities, dict):
        raise LoadError("Il file non è un export di un package: manca la sezione 'entities', quindi non contiene "
                        "alcun journey.")
    warnings: list[str] = []
    journey_id = _choose_journey(export, entities, warnings)
    sent = _sent_assets(entities, _entity_data(entities, "journeys", journey_id))
    # Every reference is resolved before anything is read; journeys other than the chosen one
    # are left out, so that their oddities do not reach the warnings. Warnings about assets and
    # landing pages the journey does not send are dropped.
    wanted = {**entities, "journeys": {journey_id: entities["journeys"][journey_id]}}
    resolved: dict[str, Any] = {}
    for kind, group in wanted.items():
        if not isinstance(group, dict):
            resolved[kind] = _resolve(group, entities, _filtered_sink(warnings, sent, None))
            continue
        resolved[kind] = {entity_id: _resolve(entity, entities, _filtered_sink(warnings, sent, (kind, entity_id)))
                          for entity_id, entity in group.items()}
    for text in _package_issues(export, entities, journey_id, sent, _indirect_content(entities, journey_id, sent)):
        _warn(warnings, text)
    reader = _ExportReader(resolved, warnings, _list(export.get("issues")))
    return reader.read_package(str(export.get("name") or ""), journey_id)


def resolve_references(value: Any, entities: dict, warnings: Optional[list[str]] = None) -> Any:
    """Return a copy of ``value`` with every ``{{mcpm#/entities/<kind>/<id>/data/<path>}}`` replaced.

    Spec: "References written as {{mcpm#/entities/...}} are resolved to the real names
    before anything else." Works on strings, wherever the reference sits inside them
    (criteria XML, SQL, ``{{Event.{{mcpm#...}}."Field"}}``), and recursively on dict
    values and list items; other values come back unchanged. ``entities`` is the export's
    ``entities`` map. Rules (docs/plan.md, "loader.py"):

    - ``.../data/id`` -> the entity id (its key in ``entities``): the export carries no real ids;
    - ``.../data/fields/name:<F>/id`` -> the field name ``<F>``;
    - ``.../data/namePrefix`` -> the value when present, else ``""`` (so SQL ``prefix[name]`` becomes ``[name]``);
    - any other path (``name``, ``eventDefinitionKey``, ...) -> the value found at that path;
    - a path or an entity missing from the export -> the entity id, plus a warning.

    A value that itself holds a reference (an attribute set's ``storageName``) is
    resolved in turn. ``{{mcpm:...}}`` placeholders (sender profile, delivery profile)
    and personalisation strings stay as they are. Warnings go to ``warnings`` when a
    list is given.
    """
    sink = None if warnings is None else (lambda text, about: _warn(warnings, text))
    return _resolve(value, entities, sink)


# A warning met while resolving, with the entity it is about ((kind, entity id), or None).
WarningSink = Optional[Callable[[str, Optional[tuple[str, str]]], None]]


def _resolve(value: Any, entities: dict, sink: WarningSink) -> Any:
    if isinstance(value, str):
        return _resolve_text(value, entities, sink)
    if isinstance(value, dict):
        return {key: _resolve(item, entities, sink) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, entities, sink) for item in value]
    return value


# ---------------------------------------------------------------------------
# Reading the source
# ---------------------------------------------------------------------------

def _read_export(source: Any) -> dict:
    if isinstance(source, dict):
        export = source
    elif isinstance(source, (bytes, bytearray)):
        export = _parse_json(_decode(bytes(source)))
    elif isinstance(source, Path):
        export = _parse_json(_decode(_read_file(source)))
    elif isinstance(source, str):
        text = source.lstrip("﻿ \t\r\n")
        if not text or text[0] in "{[":
            export = _parse_json(text)
        else:
            export = _parse_json(_decode(_read_file(Path(source))))
    else:
        raise LoadError(f"Un export di un package non si può leggere da un valore di tipo {type(source).__name__}.")
    if not isinstance(export, dict):
        raise LoadError("Il file è un JSON valido, ma non è un export di un package: deve contenere un oggetto JSON.")
    return export


def _read_file(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise LoadError(f"Non è possibile leggere il file {path} ({error.strerror or error}).") from None


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise LoadError("Il file non è testo UTF-8, quindi non è un export JSON valido.") from None


def _parse_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise LoadError(
            f"Il file non è un JSON valido ({error.msg} alla riga {error.lineno}, colonna {error.colno})."
        ) from None


def _choose_journey(export: dict, entities: dict, warnings: list[str]) -> str:
    """Plan: "selectedEntities.journeys[0] when present, else the only journey, else the first with a warning."

    Spec: "If the export holds more than one journey, the app uses the one listed in
    selectedEntities.journeys." No journey at all -> LoadError.
    """
    journeys = [journey_id for journey_id, _ in _entity_items(entities, "journeys")]
    if not journeys:
        raise LoadError("L'export non contiene alcun journey.")
    selection = export.get("selectedEntities")
    selected = selection.get("journeys") if isinstance(selection, dict) else None
    selected = [journey_id for journey_id in selected or [] if isinstance(journey_id, str)]
    if selected:
        if len(selected) > 1:
            _warn(warnings, f"L'export seleziona {len(selected)} journey: viene letto solo il primo, {selected[0]}.")
        if selected[0] in journeys:
            return selected[0]
        _warn(warnings, f"Il journey selezionato {selected[0]} non è nell'export.")
    if len(journeys) == 1:
        return journeys[0]
    first_name = _entity_data(entities, "journeys", journeys[0]).get("name") or journeys[0]
    _warn(warnings, f"L'export contiene {len(journeys)} journey e nessuno è selezionato: viene letto il primo, "
                    f"{first_name}.")
    return journeys[0]


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------

def _resolve_text(text: str, entities: dict, sink: WarningSink) -> str:
    if "{{mcpm#" not in text:
        return text
    for _ in range(MAX_REFERENCE_ROUNDS):
        resolved = REFERENCE.sub(lambda match: _reference_value(match, entities, sink), text)
        if resolved == text:
            break
        text = resolved
    if "{{mcpm#" in text and sink is not None:
        sink(f"Un riferimento non si può risolvere e resta com'è scritto: {text[:160]}", None)
    return text


def _reference_value(match: re.Match, entities: dict, sink: WarningSink) -> str:
    kind, entity_id, path = match.groups()
    if path == "id":
        return entity_id
    field_name = FIELD_ID_PATH.match(path)
    if field_name:
        return field_name.group(1)
    data = _entity_data(entities, kind, entity_id)
    found = _value_at(data, path)
    if path == "namePrefix":
        return "" if found is None else str(found)
    if isinstance(found, (str, int, float, bool)):
        return str(found)
    if sink is None:
        return entity_id
    if not data:
        sink(f"L'export non contiene {kind}/{entity_id}; il riferimento è sostituito con l'id {entity_id}.",
             (kind, entity_id))
    else:
        sink(f"{kind}/{entity_id} non ha un valore in '{path}'; il riferimento è sostituito con l'id {entity_id}.",
             (kind, entity_id))
    return entity_id


def _sent_assets(entities: dict, journey_data: dict) -> set[str]:
    """Ids and legacy ids of the assets the journey's Email and SMS activities send, read from the raw journey.

    ``triggeredSend.emailId`` (email) and ``configurationArguments.assetId`` (SMS) are a reference to the
    asset (its id is in the reference) or a plain id, which may be the asset's legacy id. Both spellings of
    each sent asset are in the set.
    """
    sent: set[str] = set()
    for raw in _list(journey_data.get("activities")):
        asset_ref = _sent_asset_ref(raw)
        if asset_ref is None:
            continue
        found = [m.group(2) for m in REFERENCE.finditer(str(asset_ref)) if m.group(1) == "assets"]
        sent.update(found or [str(asset_ref)])
    for asset_id, data in _entity_items(entities, "assets"):
        legacy_id = _value_at(data, "data/email/legacy/legacyId")
        if legacy_id is None:
            continue
        if asset_id in sent:
            sent.add(str(legacy_id))
        elif str(legacy_id) in sent:
            sent.add(asset_id)
    return sent


def _sent_asset_ref(raw: Any) -> Any:
    """The asset an Email or SMS activity of the raw journey sends, as written; None for any other activity."""
    if not isinstance(raw, dict):
        return None
    kind = ACTIVITY_KINDS.get(str(raw.get("type") or ""))
    config = _dict(raw.get("configurationArguments"))
    if kind == KIND_EMAIL:
        return _dict(config.get("triggeredSend")).get("emailId")
    if kind == KIND_SMS:
        return config.get("assetId")
    return None


def _unused_content(kind: Optional[str], entity_id: Any, sent: set[str]) -> bool:
    """An asset or landing page that no Email or SMS activity of the journey sends."""
    return kind in CONTENT_KINDS and str(entity_id) not in sent


def _indirect_content(entities: dict, journey_id: str, sent: set[str]) -> set[str]:
    """Ids of the assets and landing pages the journey uses only indirectly: reached through references
    (``{{mcpm#/entities/...}}``) from the journey, its sent emails and whatever they reference in turn, other
    journeys left out, but not sent by an Email activity. An image inside a sent email, or a landing page an
    email or the entry event links to, is one."""
    seen: set[tuple[str, str]] = set()
    queue = [("journeys", journey_id)] + [("assets", asset_id) for asset_id in sent]
    found: set[str] = set()
    while queue:
        kind, entity_id = queue.pop()
        if (kind, entity_id) in seen:
            continue
        seen.add((kind, entity_id))
        group = entities.get(kind)
        entity = group.get(entity_id) if isinstance(group, dict) else None
        if entity is None:
            continue
        if kind in CONTENT_KINDS and entity_id not in sent:
            found.add(entity_id)
        for match in REFERENCE.finditer(json.dumps(entity)):
            ref_kind, ref_id = match.group(1), match.group(2)
            if ref_kind != "journeys" and (ref_kind, ref_id) not in seen:
                queue.append((ref_kind, ref_id))
    return found


def _filtered_sink(warnings: list[str], sent: set[str], resolving: Optional[tuple[str, str]]) -> WarningSink:
    """Collect the warnings met while resolving one entity, except those about unused assets.

    Spec: "Warnings about assets the journey does not use are dropped": a warning is dropped when
    the entity being resolved, or the entity the reference names, is an asset or landing page that
    the journey does not send.
    """
    def sink(text: str, about: Optional[tuple[str, str]]) -> None:
        if resolving is not None and _unused_content(resolving[0], resolving[1], sent):
            return
        if about is not None and _unused_content(about[0], about[1], sent):
            return
        _warn(warnings, text)
    return sink


# ---------------------------------------------------------------------------
# Package Manager issues
# ---------------------------------------------------------------------------

# How report texts name the entity an issue sits on: kind -> Italian phrase before the entity name.
_ISSUE_HOSTS = {
    "journeys": "sul journey",
    "dataExtensions": "sulla data extension",
    "sharedDataExtensions": "sulla data extension",
    "eventDefinitions": "sull'entry event",
    "automations": "sull'automation",
    "queryActivities": "sulla query",
    "attributeSets": "sull'attribute set",
    "attributeRelationships": "sulla relazione",
    "assets": "sull'asset",
    "landingPages": "sulla landing page",
    "primaryLandingPages": "sulla landing page",
    "cloudPageCollections": "sulla collection CloudPages",
}


def _package_issues(export: dict, entities: dict, journey_id: str, sent: set[str],
                    indirect: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """Spec: "Package Manager issues of level warning or error are listed in the report".

    Issues are read at the top level of the export and on every entity (other journeys left out). The
    level of an issue is the most severe of the issue and its cause. An issue sitting on, or naming, an
    asset or landing page the journey does not use is dropped, like the other warnings about assets the
    journey does not use. On content the journey uses only indirectly (``indirect``, or content named by an
    issue of an email or page the journey uses: an image inside a sent email) only the errors are kept
    (team decision, 13 October 2026). Returned as plain Italian sentences.
    """
    found: list[tuple[Optional[str], str, dict]] = [(None, "", issue) for issue in _list(export.get("issues"))]
    for kind, group in entities.items():
        if not isinstance(group, dict):
            continue
        for entity_id, entity in group.items():
            if not isinstance(entity, dict) or (kind == "journeys" and entity_id != journey_id):
                continue
            found.extend((kind, entity_id, issue) for issue in _list(entity.get("issues")))
    texts: list[str] = []
    for kind, entity_id, issue in found:
        if not isinstance(issue, dict):
            continue
        if (kind in TABLE_KINDS or (kind is None and _about_table(issue))) and _managed_marks(issue):
            continue    # a managed data extension: reported once by _ExportReader.read_managed
        indirectly_used = _unused_content(kind, entity_id, sent)
        if indirectly_used and str(entity_id) not in indirect:
            continue
        levels = {str(issue.get("level") or "").upper(), str(_dict(issue.get("cause")).get("level") or "").upper()}
        if not levels & set(ISSUE_LEVELS):
            continue
        missing = _dict(issue.get("entity"))
        location = _dict(missing.get("locationInformation"))
        sent_by_journey = ACTIVITY_KINDS.get(str(location.get("activityType") or "")) in (KIND_EMAIL, KIND_SMS)
        if _unused_content(missing.get("entityType"), missing.get("value"), sent) and not sent_by_journey:
            if kind not in CONTENT_KINDS:
                continue
            indirectly_used = True   # named by an email or page the journey uses: an image inside it
        if indirectly_used and "ERROR" not in levels:
            continue
        level = "un errore" if "ERROR" in levels else "un warning"
        texts.append(f"Package Manager segnala {level} {_issue_host(entities, kind, entity_id)}: "
                     f"{_issue_description(issue)}.")
    return texts


def _managed_marks(issue: Any) -> list[str]:
    """The values of a Package Manager issue that mark a managed data extension (MANAGED_MARKS), as written."""
    issue = _dict(issue)
    return [str(issue.get(name)) for name, values in MANAGED_MARKS.items() if str(issue.get(name) or "") in values]


def _about_table(issue: Any) -> bool:
    """An export-level issue whose entity is a data extension."""
    return _dict(_dict(issue).get("entity")).get("entityType") in TABLE_KINDS


def _issue_host(entities: dict, kind: Optional[str], entity_id: str) -> str:
    if kind is None:
        return "sul package"
    name = _entity_data(entities, kind, entity_id).get("name")
    phrase = _ISSUE_HOSTS.get(kind)
    if phrase is None:
        return f"su {kind}/{entity_id}"
    if kind == "assets":
        return f"{phrase} {name} ({entity_id})" if name else f"{phrase} {entity_id}"
    return f"{phrase} {name or entity_id}"


def _issue_description(issue: dict) -> str:
    """What the issue says, in plain words; Package Manager's own terms and messages stay as written."""
    issue_type = str(issue.get("type") or "")
    missing = _dict(issue.get("entity"))
    if issue_type == "ENTITY_NOT_FOUND" and missing:
        text = (f"non trova {missing.get('entityType')} con {missing.get('retrievalProperty')} "
                f"{missing.get('value')}")
        location = _dict(missing.get("locationInformation"))
        if location.get("activityName"):
            text += f", usato dall'attività '{location['activityName']}' ({location.get('activityType')})"
    elif issue_type == "UNSUPPORTED_FUNCTIONALITY":
        text = f"funzionalità non supportata {issue.get('functionality')}"
        details = [str(issue[name]) for name in ("blockType", "objectType") if issue.get(name)]
        if details:
            text += f" ({', '.join(details)})"
    else:
        text = " ".join(str(part) for part in (issue_type, issue.get("issue"), issue.get("functionality")) if part)
    message = _dict(issue.get("cause")).get("message") or issue.get("message")
    if message:
        text += f". Messaggio di Package Manager: {message}"
    return text


def _value_at(data: Any, path: str) -> Any:
    """The value at a ``/``-separated path inside an entity's data, or None when the path is missing."""
    current = data
    for part in path.split("/"):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _warn(warnings: Optional[list[str]], text: str) -> None:
    """Add a warning once; the same fact met many times is reported once."""
    if warnings is not None and text not in warnings:
        warnings.append(text)


def _entity_items(entities: dict, kind: str) -> list[tuple[str, dict]]:
    """(entity id, data) of every entity of one kind, in export order."""
    group = entities.get(kind)
    if not isinstance(group, dict):
        return []
    return [(entity_id, entity["data"]) for entity_id, entity in group.items()
            if isinstance(entity, dict) and isinstance(entity.get("data"), dict)]


def _entity_data(entities: dict, kind: str, entity_id: str) -> dict:
    """The ``data`` of one entity, or an empty dict when the export does not hold it."""
    group = entities.get(kind)
    entity = group.get(entity_id) if isinstance(group, dict) else None
    data = entity.get("data") if isinstance(entity, dict) else None
    return data if isinstance(data, dict) else {}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _text_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _written_value(value: Any) -> Value:
    """Plan: an update-contact value is "kept as written (str(value) for numbers)"; null stays None."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


def _strings(value: Any) -> Iterator[str]:
    """Every string inside a nested dict / list."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _has_link(view: Any) -> bool:
    """Plan: has_link = an ``href=`` or ``<a `` appears in the HTML or text view.

    The whole view is searched: its ``content`` and, for template-based emails, the
    content of its slots and blocks.
    """
    return any(LINK.search(text) for text in _strings(view))


def _dotted_segments(text: str) -> list[str]:
    """Split ``A.B."C D"`` on the dots outside double quotes and drop the quotes: ``[A, B, C D]``."""
    segments: list[str] = []
    current: list[str] = []
    quoted = False
    for char in text:
        if char == '"':
            quoted = not quoted
        elif char == "." and not quoted:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    segments.append("".join(current))
    return segments


def _personalisation_segments(expression: Any) -> list[str]:
    """Segments of a ``{{A.B."C"}}`` personalisation string; empty when it is not one."""
    if not isinstance(expression, str) or not expression.strip():
        return []
    text = expression.strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2]
    return _dotted_segments(text)


def _expression_field(expression: Any) -> Optional[tuple[str, str]]:
    """(attribute set, field) of a wait-by-attribute expression.

    Plan: "last quoted segment = field, segment before it = attribute set", e.g.
    ``{{Contact.Attribute.Contacts.Master.Entry.Config."WAIT1"}}`` -> ``(Config, WAIT1)``.
    """
    segments = _personalisation_segments(expression)
    if len(segments) < 2 or not segments[-1] or not segments[-2]:
        return None
    return segments[-2], segments[-1]


def _event_field(text: str) -> Optional[str]:
    """The field name in ``{{Event.<key>."Field"}}``: the entry table field the journey sends to."""
    match = re.search(r'\."([^"]+)"\s*\}\}', str(text))
    return match.group(1) if match else None


def _event_key(expression: Any) -> Optional[str]:
    """The event key of ``{{Event.<key>."Field"}}``, or None when the expression reads no entry event."""
    segments = _personalisation_segments(expression)
    if len(segments) >= 3 and segments[0] == "Event" and segments[1]:
        return segments[1]
    return None


def _path_tables(tables: list[Optional[str]], own_table: str) -> tuple[str, ...]:
    """The tables of a path from the contact, nearest the contact first, without repeats, without empty names and
    without the table the path leads to (spec, step 3, "Tables reached through other tables")."""
    path: list[str] = []
    for table in tables:
        if table and table != own_table and table not in path:
            path.append(table)
    return tuple(path)


def _one_line(text: str) -> str:
    """A text on one line with single spaces (criteria descriptions hold double spaces)."""
    return " ".join(text.split())


def _named(activity: Activity) -> str:
    """An activity as named in warnings: its key, and its name when it has one."""
    return f"{activity.key} ({activity.name})" if activity.name else activity.key


def _outcome(raw: dict) -> Outcome:
    """One outcome: key, label, next activity. The label falls back to the criteria description, then the key."""
    meta = _dict(raw.get("metaData"))
    key = str(raw.get("key") or "")
    description = str(meta.get("criteriaDescription") or "")
    next_key = raw.get("next")
    return Outcome(
        key=key,
        label=str(meta.get("label") or description or key),
        next=str(next_key) if next_key else None,
        description=description,
    )


# ---------------------------------------------------------------------------
# Building the package
# ---------------------------------------------------------------------------

class _Link(NamedTuple):
    """One attributeRelationships entity, as the path of a condition reads it: a group-to-set link (the contact
    model pointing at ``right_table``) or a link from ``left_table`` to ``right_table``."""
    group_to_set: bool
    left_table: Optional[str]
    right_table: str


class _ExportReader:
    """Builds the Package from the resolved ``entities`` of one export, collecting warnings."""

    def __init__(self, entities: dict, warnings: list[str], top_issues: Optional[list] = None):
        self.entities = entities
        self.warnings = warnings
        self.top_issues = [issue for issue in top_issues or [] if isinstance(issue, dict)]   # export-level issues
        self.tables: dict[str, Table] = {}
        self.table_aliases: dict[str, str] = {}     # entity id or customer key -> table name
        self.set_tables: dict[str, str] = {}        # attribute set id or name -> table name
        self.queries_by_id: dict[str, Query] = {}
        self.automation_names: dict[str, str] = {}  # automation entity id -> automation name
        self.emails: dict[str, EmailAsset] = {}
        self.legacy_email_ids: dict[str, str] = {}  # email legacyId -> asset id
        self.relationships: list[Relationship] = []
        self.relationship_links: dict[str, _Link] = {}   # attributeRelationships entity id -> its tables
        self.related_tables: set[str] = set()       # tables named by a relationship of the export
        self.missing_relationships: list[str] = []  # tables read through a set that no relationship names
        self.entry: Optional[EntryEvent] = None

    def warn(self, text: str) -> None:
        _warn(self.warnings, text)

    def items(self, kind: str) -> list[tuple[str, dict]]:
        return _entity_items(self.entities, kind)

    def read_package(self, package_name: str, journey_id: str) -> Package:
        self.read_tables()
        self.read_attribute_sets()
        relationships = self.read_relationships()
        queries = self.read_queries()
        automation_queries = self.read_automations()
        self.read_emails()
        journey_data = _entity_data(self.entities, "journeys", journey_id)
        self.relationships = relationships
        entry_event = self.read_entry_event(journey_data)
        self.entry = entry_event
        journey = self.read_journey(journey_id, journey_data, entry_event)
        self.check_constant_writes_into_keys(journey)
        self.check_one_to_one_joins(relationships)
        return Package(
            name=package_name,
            journey=journey,
            tables=self.tables,
            relationships=relationships,
            queries=queries,
            entry_event=entry_event,
            emails=self.emails,
            automation_queries=automation_queries,
            warnings=self.warnings,
            unsupported_activities=[a.key for a in journey.activities.values() if a.kind == KIND_UNSUPPORTED],
            missing_relationships=list(self.missing_relationships),
        )

    # -- checks carried over from the earlier tool (spec, "Outputs") ----------

    def check_constant_writes_into_keys(self, journey: Journey) -> None:
        """Spec check: "An update contact step that writes a constant into a primary-key field"."""
        found: dict[tuple[str, str, str], list[str]] = {}
        for activity in journey.activities.values():
            for write in activity.writes:
                table = self.tables.get(write.table)
                field = table.field(write.field) if table is not None else None
                if field is None or not field.is_primary_key or write.value is None:
                    continue
                if "{{" in write.value:
                    continue   # a placeholder such as {{DateTime.Now}} is not a constant
                found.setdefault((table.name, field.name, write.value), []).append(activity.key)
        for (table, field, value), keys in found.items():
            self.warn(f"pattern sospetto: la costante {value!r} è scritta nel campo di chiave primaria "
                      f"{table}.{field} da {len(keys)} attività Update contact, quindi tutti i contatti hanno lo "
                      f"stesso valore di chiave in quel campo ({', '.join(keys)}).")

    def check_one_to_one_joins(self, relationships: list[Relationship]) -> None:
        """Spec check: "A one-to-one relationship to a table whose primary key is wider than the join"."""
        by_name: dict[str, list[Relationship]] = {}
        for rel in relationships:
            if rel.left_table is not None:
                by_name.setdefault(rel.name, []).append(rel)
        for name, rels in by_name.items():
            first = rels[0]
            if first.left_cardinality != "One" or first.right_cardinality != "One":
                continue
            table = self.tables.get(first.right_table)
            if table is None:
                continue
            join_fields = {r.right_field.lower() for r in rels}
            pk = [f.name for f in table.fields if f.is_primary_key]
            extra = [f for f in pk if f.lower() not in join_fields]
            if extra:
                self.warn(f"pattern sospetto: la relazione uno a uno '{name}' collega {table.name} tramite "
                          f"{', '.join(sorted(r.right_field for r in rels))}, ma la chiave primaria della tabella "
                          f"comprende anche {', '.join(extra)}: a un contatto possono corrispondere più righe.")

    # -- tables -------------------------------------------------------------

    def read_tables(self) -> None:
        """Plan: "Tables: all dataExtensions (and sharedDataExtensions), fields in ordinal order"."""
        for kind in TABLE_KINDS:
            for entity_id, data in self.items(kind):
                table = self.table_from(entity_id, data)
                self.read_managed(kind, entity_id, table)
                if table.name in self.tables:
                    self.warn(f"Due tabelle si chiamano {table.name}: si usa la prima dell'export.")
                else:
                    self.tables[table.name] = table
                self.table_aliases[entity_id] = table.name
                if table.customer_key:
                    self.table_aliases.setdefault(table.customer_key, table.name)

    def table_from(self, entity_id: str, data: dict) -> Table:
        name = str(data.get("name") or entity_id)
        raw_fields = [raw for raw in _list(data.get("fields")) if isinstance(raw, dict) and raw.get("name")]
        fields = [self.field_from(name, raw, position) for position, raw in enumerate(raw_fields)]
        fields.sort(key=lambda f: f.ordinal)
        table = Table(
            id=entity_id,
            name=name,
            customer_key=_text_or_none(data.get("customerKey")),
            fields=fields,
            is_sendable=bool(data.get("isSendable")),
        )
        sendable_field = data.get("sendableCustomObjectField")
        if sendable_field:
            table.sendable_field = self.field_spelling(table, str(sendable_field))
        return table

    def read_managed(self, kind: str, entity_id: str, table: Table) -> None:
        """Spec, step 3, "Synchronized data extensions": a source table that Package Manager marks as a managed
        data extension (NO_DEPLOY, ManagedDataExtension ...) is imported like every other CSV; its rows also arrive
        from the CRM sync.

        The marks are read in the issues of the table's entity and in the export-level issues that name the
        table (by id, name or customer key). A marked table is ``managed``, with one warning, which also stands
        for the Package Manager issue that marks it (``_package_issues`` leaves that issue out).
        """
        names = {entity_id, table.name, table.customer_key}
        issues = _list(_dict(_dict(self.entities.get(kind)).get(entity_id)).get("issues")) + [
            issue for issue in self.top_issues
            if _about_table(issue) and str(_dict(issue.get("entity")).get("value")) in names]
        marks = [mark for issue in issues for mark in _managed_marks(issue)]
        if not marks:
            return
        table.managed = True
        self.warn(f"La data extension {table.name} è sincronizzata (managed data extension; Package Manager la "
                  f"segnala come {', '.join(dict.fromkeys(marks))}): le sue righe arrivano anche dalla "
                  "sincronizzazione.")

    def field_from(self, table_name: str, raw: dict, position: int) -> Field:
        name = str(raw["name"])
        field_type = str(raw.get("type") or "")
        if field_type not in FIELD_TYPES:
            self.warn(f"Il campo {table_name}.{name} è di tipo {field_type!r}, che il generatore non conosce.")
        is_primary_key = bool(raw.get("isPrimaryKey"))
        nullable = raw.get("isNullable")
        if nullable is None:
            nullable = not is_primary_key
            self.warn(f"Il campo {table_name}.{name} non indica se ammette null: si considera che "
                      f"{'ammetta null' if nullable else 'non ammetta null'} perché "
                      f"{'non ' if nullable else ''}fa parte della chiave primaria.")
        ordinal = _int_or_none(raw.get("ordinal"))
        return Field(
            name=name,
            type=field_type,
            length=_int_or_none(raw.get("length")),
            is_primary_key=is_primary_key,
            is_nullable=bool(nullable),
            default=_text_or_none(raw.get("defaultValue")),
            ordinal=position if ordinal is None else ordinal,
            scale=_int_or_none(raw.get("scale")) if field_type == "Decimal" else None,
        )

    def known_table(self, value: Any) -> Optional[str]:
        """The table a resolved reference names (a table name, entity id or customer key), or None."""
        if not isinstance(value, str) or not value:
            return None
        if value in self.tables:
            return value
        return self.table_aliases.get(value)

    def table_ref(self, value: Any, what: str) -> str:
        """The table named by a resolved reference; an unknown one is kept as written, with a warning."""
        table = self.known_table(value)
        if table is None:
            table = "" if value is None else str(value)
            self.warn(f"{what}: la tabella {table!r} non è nell'export.")
        return table

    def field_spelling(self, table: Table, field_name: str) -> str:
        """The field's spelling in the table schema; a field missing from the schema is kept, with a warning."""
        field = table.field(field_name)
        if field is None:
            self.warn(f"Il campo {field_name!r} non è nello schema della tabella {table.name}.")
            return field_name
        if field.name != field_name:
            self.warn(f"Il campo {table.name}.{field_name} è scritto {field.name} nello schema della tabella: "
                      f"si usa la grafia dello schema.")
        return field.name

    def schema_field(self, table_name: str, field_name: str) -> str:
        table = self.tables.get(table_name)
        if table is None:       # the unknown table has already been reported
            return field_name
        return self.field_spelling(table, field_name)

    # -- attribute sets and relationships -----------------------------------

    def read_attribute_sets(self) -> None:
        """Each attribute set names a table through ``storageName`` (fallback: ``storageReferenceID``)."""
        for set_id, data in self.items("attributeSets"):
            name = str(data.get("name") or set_id)
            storage = data.get("storageName")
            table = self.known_table(storage) or self.known_table(_dict(data.get("storageReferenceID")).get("value"))
            if table is None:
                table = str(storage or name)
                self.warn(f"L'attribute set {name} salva i dati in {table}, che non è una tabella dell'export.")
            self.set_tables[set_id] = table
            self.set_tables.setdefault(name, table)

    def table_for_set(self, set_name: str) -> str:
        """Plan: map the attribute-set name to the table through ``attributeSets[*].storageName``.

        "Fall back to the name itself when no set matches, with a warning."
        """
        table = self.set_tables.get(set_name)
        if table is None:
            table = set_name
            known = "" if set_name in self.tables else ", ma nessuna tabella ha questo nome"
            self.warn(f"Nessun attribute set si chiama {set_name}: il nome è preso come nome di tabella{known}.")
        return table

    def item_table(self, item: dict, relationship_name: str) -> str:
        """The table of one side of a relationship (``leftItem.identifier`` / ``rightItem.name``)."""
        for ref in (item.get("identifier"), item.get("name")):
            if isinstance(ref, str) and ref in self.set_tables:
                return self.set_tables[ref]
        fallback = str(item.get("name") or item.get("identifier") or "")
        self.warn(f"La relazione {relationship_name} collega {fallback!r}, che non è un attribute set "
                  f"dell'export: è preso come nome di tabella.")
        return fallback

    def read_relationships(self) -> list[Relationship]:
        """Plan: one Relationship per ``relationshipAttributes`` entry; group-to-set links have no left table."""
        relationships: list[Relationship] = []
        for relationship_id, data in self.items("attributeRelationships"):
            name = str(data.get("name") or relationship_id)
            left_item, right_item = _dict(data.get("leftItem")), _dict(data.get("rightItem"))
            group_to_set = bool(data.get("isGroupToSetRelationship"))
            left_table = None if group_to_set else self.item_table(left_item, name)
            right_table = self.item_table(right_item, name)
            self.related_tables.update(table for table in (left_table, right_table) if table)
            self.relationship_links[relationship_id] = _Link(group_to_set, left_table, right_table)
            pairs = [pair for pair in _list(data.get("relationshipAttributes")) if isinstance(pair, dict)]
            if not pairs:
                self.warn(f"La relazione {name} non indica alcun campo, quindi non collega nulla.")
            for pair in pairs:
                left_field = _text_or_none(pair.get("leftAttributeName")) or None
                if left_field and left_table is not None:
                    left_field = self.schema_field(left_table, left_field)
                right_field = str(pair.get("rightAttributeName") or pair.get("rightAttributeID") or "")
                relationships.append(Relationship(
                    name=name,
                    left_table=left_table,
                    left_field=left_field,
                    right_table=right_table,
                    right_field=self.schema_field(right_table, right_field),
                    left_cardinality=str(left_item.get("cardinality") or ""),
                    right_cardinality=str(right_item.get("cardinality") or ""),
                ))
        return relationships

    # -- queries, automations, emails ---------------------------------------

    def read_queries(self) -> list[Query]:
        """Plan: "Queries: every queryActivities entry" (SQL with references already resolved)."""
        queries: list[Query] = []
        for query_id, data in self.items("queryActivities"):
            name = str(data.get("name") or query_id)
            target = data.get("targetId") or data.get("targetKey") or data.get("targetName")
            query = Query(
                id=query_id,
                name=name,
                target_table=self.table_ref(target, f"Query {name}"),
                sql=str(data.get("queryText") or ""),
                update_type=str(data.get("targetUpdateTypeName") or ""),
            )
            queries.append(query)
            self.queries_by_id[query_id] = query
        return queries

    def read_automations(self) -> dict[str, list[str]]:
        """Plan: automation name -> query names in step order (``steps[*].activities[*]`` with objectTypeId 300)."""
        automation_queries: dict[str, list[str]] = {}
        for automation_id, data in self.items("automations"):
            name = str(data.get("name") or automation_id)
            self.automation_names[automation_id] = name
            names: list[str] = []
            for step in _list(data.get("steps")):
                for step_activity in _list(_dict(step).get("activities")):
                    if _int_or_none(_dict(step_activity).get("objectTypeId")) == QUERY_OBJECT_TYPE:
                        names.append(self.step_query_name(step_activity, name))
            if name in automation_queries:
                self.warn(f"Due automation si chiamano {name}: si tengono le query dell'ultima.")
            automation_queries[name] = names
        return automation_queries

    def step_query_name(self, step_activity: dict, automation_name: str) -> str:
        query = self.queries_by_id.get(str(step_activity.get("activityObjectId") or ""))
        if query is not None:
            return query.name
        name = str(step_activity.get("name") or step_activity.get("activityObjectId") or "")
        self.warn(f"L'automation {automation_name} esegue la query {name}, che non è nell'export.")
        return name

    def read_emails(self) -> None:
        """Every asset of the export, by asset id. Plan: has_link = ``href=`` or ``<a `` in the HTML or text view."""
        for asset_id, data in self.items("assets"):
            views = _dict(data.get("views"))
            self.emails[asset_id] = EmailAsset(
                id=asset_id,
                name=str(data.get("name") or asset_id),
                subject=str(_dict(views.get("subjectline")).get("content") or ""),
                has_link=_has_link(views.get("html")) or _has_link(views.get("text")),
            )
            legacy_id = _value_at(data, "data/email/legacy/legacyId")
            if legacy_id is not None:
                self.legacy_email_ids[str(legacy_id)] = asset_id

    # -- entry ----------------------------------------------------------------

    def read_entry_event(self, journey_data: dict) -> EntryEvent:
        """Plan: the trigger's ``metaData.eventDefinitionId`` -> eventDefinition -> ``dataExtensionId`` -> table;
        ``arguments.automationId`` -> automation name (automation entry only).

        Spec, "Table selection": the entry table is "the table bound to the journey's
        entry trigger". Without it nothing can be generated, so it is a LoadError.
        Spec, "Entry source and procedure": the entry source is read from the event definition
        (type and ``metaData.scheduleFlowMode``), see ``entry_kind``. Entry filters are not modelled:
        a non-empty one is kept and reported.
        """
        triggers = [trigger for trigger in _list(journey_data.get("triggers")) if isinstance(trigger, dict)]
        if not triggers:
            raise LoadError("Il journey non ha un trigger di ingresso, quindi non è possibile trovare la sua "
                            "tabella di ingresso.")
        if len(triggers) > 1:
            self.warn(f"Il journey ha {len(triggers)} trigger di ingresso: viene letto solo il primo.")
        meta = _dict(triggers[0].get("metaData"))
        event_id, event = self.find_event(meta)
        if event is None:
            raise LoadError("L'entry event del journey non è nell'export, quindi non è possibile trovare la sua "
                            "tabella di ingresso.")
        event_name = str(event.get("name") or event_id)
        arguments = _dict(event.get("arguments"))
        table_ref = event.get("dataExtensionId") or arguments.get("dataExtensionId")
        table = self.known_table(table_ref)
        if table is None:
            raise LoadError(f"La tabella di ingresso dell'entry event {event_name} ({table_ref}) non è nell'export.")
        event_type = str(event.get("type") or triggers[0].get("type") or "")
        event_meta = _dict(event.get("metaData"))
        schedule_mode = _text_or_none(event_meta.get("scheduleFlowMode")) or None
        kind = self.entry_kind(event_name, event_type, schedule_mode)
        automation_ref = arguments.get("automationId")
        automation_name = None
        if kind == ENTRY_AUTOMATION:
            automation_name = self.automation_names.get(str(automation_ref)) if automation_ref else None
            if automation_ref and automation_name is None:
                self.warn(f"L'entry event {event_name} è alimentato dall'automation {automation_ref}, "
                          f"che non è nell'export.")
        # On a data extension entry with its own schedule, automationId names the scheduler Journey
        # Builder creates, not an automation that fills the table (assumption): nothing to report.
        entry_filter = self.entry_filter(event, triggers[0])
        if entry_filter:
            self.warn(f"L'entry event {event_name} ha un filtro di ingresso ({entry_filter[:200]}): il generatore "
                      "non lo modella, quindi alcuni contatti generati potrebbero non entrare nel journey.")
        return EntryEvent(
            key=str(event.get("eventDefinitionKey") or meta.get("eventDefinitionKey") or ""),
            name=event_name,
            table=table,
            automation_name=automation_name,
            kind=kind,
            event_type=event_type,
            schedule_mode=schedule_mode,
            run_once_mode=_text_or_none(event_meta.get("runOnceScheduleMode")) or None,
            entry_filter=entry_filter,
        )

    def entry_kind(self, event_name: str, event_type: str, schedule_mode: Optional[str]) -> str:
        """Spec, "Entry source and procedure": the entry source, read from the event definition (type and
        ``metaData.scheduleFlowMode``), through ENTRY_SOURCE_TYPES and SCHEDULE_FLOW_MODES.

        A data extension entry whose schedule mode is missing or not in SCHEDULE_FLOW_MODES is read as a
        recurring schedule, the procedure the spec leaves to verify, with a warning.
        """
        source = ENTRY_SOURCE_TYPES.get(event_type, ENTRY_OTHER)
        if source != DATA_EXTENSION_SOURCE:
            return source
        kind = SCHEDULE_FLOW_MODES.get(schedule_mode or "")
        if kind is None:
            kind = ENTRY_SCHEDULED
            schedule = f"la pianificazione {schedule_mode!r}" if schedule_mode else "una pianificazione non indicata"
            self.warn(f"L'entry event {event_name} è una data extension con {schedule}: è trattata come una "
                      "pianificazione ricorrente, in cui i contatti entrano alla valutazione successiva della "
                      "data extension. La procedura di ingresso è da verificare.")
        return kind

    def entry_filter(self, event: dict, trigger: dict) -> Optional[str]:
        """The entry filter criteria, when the event or the trigger holds one; None when all are empty."""
        trigger_config = _dict(trigger.get("configurationArguments"))
        texts = [
            _dict(event.get("arguments")).get("criteria"),
            trigger_config.get("criteria"),
            _dict(event.get("metaData")).get("criteriaDescription"),
        ]
        for text in texts:
            if isinstance(text, str) and text.strip():
                return text.strip()
        filter_id = trigger_config.get("filterDefinitionId")
        if isinstance(filter_id, str) and filter_id.strip() and filter_id.strip() != ZERO_GUID:
            return f"filterDefinitionId {filter_id.strip()}"
        return None

    def find_event(self, trigger_meta: dict) -> tuple[str, Optional[dict]]:
        events = dict(self.items("eventDefinitions"))
        event_id = str(trigger_meta.get("eventDefinitionId") or "")
        if event_id in events:
            return event_id, events[event_id]
        event_key = trigger_meta.get("eventDefinitionKey")
        for other_id, data in events.items():
            if event_key and data.get("eventDefinitionKey") == event_key:
                return other_id, data
        return event_id, None

    def event_name(self, event_key: str) -> Optional[str]:
        for _, data in self.items("eventDefinitions"):
            if data.get("eventDefinitionKey") == event_key:
                return str(data.get("name") or "")
        return None

    def check_defaults(self, defaults: dict, entry: EntryEvent) -> None:
        """Spec, "Outputs" checks: "A journey default that reads from a different entry event".

        Spec, "Honest outcomes": "A journey default (email, mobile number) that names a column the entry
        data extension lacks is reported."
        """
        entry_table = self.tables.get(entry.table)
        for default_name, expressions in defaults.items():
            for expression in _list(expressions):
                event_key = _event_key(expression)
                if event_key is None:
                    continue
                if event_key == entry.key:
                    column = _event_field(expression)
                    if column and entry_table is not None and entry_table.field(column) is None:
                        self.warn(f"Il valore di default {default_name} del journey legge il campo {column} "
                                  f"dell'entry event, ma la tabella di ingresso {entry_table.name} non ha questo "
                                  "campo.")
                    continue
                other_name = self.event_name(event_key)
                other = f"{event_key} ({other_name})" if other_name else event_key
                self.warn(f"Il valore di default {default_name} del journey legge {expression} dall'entry event "
                          f"{other}, non dall'entry event del journey, {entry.key} ({entry.name}).")

    # -- journey ----------------------------------------------------------------

    def read_journey(self, journey_id: str, data: dict, entry: EntryEvent) -> Journey:
        activities: dict[str, Activity] = {}
        for raw in _list(data.get("activities")):
            if not isinstance(raw, dict):
                continue
            activity = self.read_activity(raw)
            if not activity.key:
                self.warn(f"L'attività {activity.name!r} non ha una chiave, quindi nessun branch può portarvi: "
                          f"è ignorata.")
            elif activity.key in activities:
                self.warn(f"Due attività hanno la chiave {activity.key}: si usa la prima.")
            else:
                activities[activity.key] = activity
        self.check_links(activities)
        defaults = _dict(data.get("defaults"))
        self.check_defaults(defaults, entry)
        email_defaults = _list(defaults.get("email"))
        mobile_defaults = _list(defaults.get("mobileNumber"))
        goals = self.read_filters(_list(data.get("goals")), "goal")
        exits = self.read_filters(_list(data.get("exits")), "exit")
        if goals:
            self.warn(f"Il journey ha {len(goals)} goal: il generatore non li modella, quindi i contatti che "
                      "raggiungono un goal potrebbero uscire prima della fine del percorso previsto.")
        self.report_exits(exits)
        return Journey(
            name=str(data.get("name") or journey_id),
            key=str(data.get("key") or ""),
            activities=activities,
            start_key=self.start_key(activities),
            entry_event_key=entry.key or None,
            entry_table=entry.table,
            default_email_event_key=_event_key(email_defaults[0]) if email_defaults else None,
            default_email_field=_event_field(email_defaults[0]) if email_defaults else None,
            activity_count=len(activities),
            entry_mode=_text_or_none(data.get("entryMode")) or None,
            goal_count=len(goals),
            exit_count=len(exits),
            exits=exits,
            goals=goals,
            default_mobile_field=_event_field(mobile_defaults[0]) if mobile_defaults else None,
        )

    # -- exit criteria and goals --------------------------------------------------

    def read_filters(self, raw_filters: list, kind: str) -> list[JourneyFilter]:
        """Spec, step 3, "Exit criteria and goals": every exit criterion (``kind`` "exit") or goal ("goal") of the
        journey, in export order. See ``read_filter``."""
        return [self.read_filter(raw, kind) for raw in raw_filters if isinstance(raw, dict)]

    def read_filter(self, raw: dict, kind: str) -> JourneyFilter:
        """One exit criterion or goal. Its criteria (``configurationArguments.criteria``) are read and mapped
        like a decision split's ("the same operators, data sources and relative dates as a decision split"),
        and ``unsupported_reason`` says why the generator cannot evaluate them: no criteria ("nessun criterio"),
        criteria that cannot be read, or a condition the rules of ``unsupported_formula`` reject."""
        key = str(raw.get("key") or raw.get("id") or "")
        found = JourneyFilter(
            key=key,
            name=str(raw.get("name") or key),
            kind=kind,
            formula=None,
            description=str(_dict(raw.get("metaData")).get("criteriaDescription") or ""),
        )
        xml = _dict(raw.get("configurationArguments")).get("criteria")
        if not isinstance(xml, str) or not xml.strip():
            found.unsupported_reason = NO_CRITERIA
            return found
        try:
            formula = parse_criteria(xml)
        except CriteriaError as error:
            found.unsupported_reason = f"i criteri non si possono leggere ({error})"
            return found
        found.formula = self.map_formula(formula)
        found.unsupported_reason = self.unsupported_formula(found.formula, "il criterio" if kind == "exit"
                                                            else "il goal")
        return found

    def report_exits(self, exits: list[JourneyFilter]) -> None:
        """Spec, step 3: "Journey Builder evaluates the exit criteria of a journey for each contact when the
        contact's time in a wait activity expires (documented)" and "Exit criteria with a condition the
        generator cannot evaluate are reported as not modelled". The exit criteria the generator evaluates are
        reported once, with their description; each of the others gets its own warning, with the reason."""
        modelled = [exit_ for exit_ in exits if exit_.unsupported_reason is None]
        if modelled:
            one = len(modelled) == 1
            quoted = "; ".join(f'"{_one_line(exit_.description) or formula_text(exit_.formula)}"' for exit_ in modelled)
            self.warn(f"Il journey ha {len(modelled)} {'criterio' if one else 'criteri'} di uscita (exit criteria) "
                      f"che il generatore valuta alla fine di ogni wait, come Journey Builder: il contatto che "
                      f"{'lo' if one else 'ne'} soddisfa {'' if one else 'uno '}esce dal journey in quel punto. "
                      f"{'Criterio' if one else 'Criteri'}: {quoted}.")
        for exit_ in exits:
            if exit_.unsupported_reason is not None:
                self.warn(f"Il criterio di uscita (exit criteria) {exit_.name!r} non è modellato: "
                          f"{exit_.unsupported_reason}. Alcuni contatti potrebbero uscire dal journey alla fine di un "
                          "wait, prima della fine del percorso previsto.")

    def start_key(self, activities: dict[str, Activity]) -> Optional[str]:
        """Spec: "The start is the activity no other activity points to." Several -> the first, with a warning."""
        targets = {outcome.next for activity in activities.values() for outcome in activity.outcomes if outcome.next}
        starts = [key for key in activities if key not in targets]
        if not starts:
            self.warn("Ogni attività del journey è la destinazione di un'altra, quindi non è possibile trovare "
                      "l'attività iniziale." if activities else "Il journey non ha attività.")
            return None
        if len(starts) > 1:
            self.warn(f"{len(starts)} attività non hanno collegamenti in entrata ({', '.join(starts)}): come inizio "
                      f"si prende la prima, {starts[0]}.")
        return starts[0]

    def check_links(self, activities: dict[str, Activity]) -> None:
        for activity in activities.values():
            for outcome in activity.outcomes:
                if outcome.next and outcome.next not in activities:
                    self.warn(f"Il branch {outcome.label!r} dell'attività {activity.key} porta a {outcome.next}, "
                              f"che non è nel journey.")
            if activity.kind == KIND_ENGAGEMENT and activity.engagement_ref:
                referenced = activities.get(activity.engagement_ref)
                if referenced is None or referenced.kind != KIND_EMAIL:
                    self.warn(f"Engagement split {_named(activity)}: fa riferimento a "
                              f"{activity.engagement_ref}, che non è un'attività Email del journey.")

    # -- activities -------------------------------------------------------------

    def read_activity(self, raw: dict) -> Activity:
        raw_outcomes = [outcome for outcome in _list(raw.get("outcomes")) if isinstance(outcome, dict)]
        activity = Activity(
            key=str(raw.get("key") or ""),
            raw_type=str(raw.get("type") or ""),
            kind=KIND_UNSUPPORTED,
            name=str(raw.get("name") or ""),
            outcomes=[_outcome(outcome) for outcome in raw_outcomes],
        )
        activity.kind = self.activity_kind(activity, raw)
        if activity.kind == KIND_DECISION:
            self.read_decision(activity, raw)
        elif activity.kind == KIND_ENGAGEMENT:
            self.read_engagement(activity, raw, raw_outcomes)
        elif activity.kind == KIND_RANDOM:
            self.read_random(activity, raw_outcomes)
        elif activity.kind == KIND_WAIT_DURATION:
            self.read_wait_duration(activity, raw)
        elif activity.kind == KIND_WAIT_ATTRIBUTE:
            self.read_wait_attribute(activity, raw)
        elif activity.kind == KIND_WAIT_STO:
            self.read_wait_sto(activity, raw)
        elif activity.kind == KIND_EMAIL:
            self.read_email(activity, raw)
        elif activity.kind == KIND_UPDATE:
            self.read_update(activity, raw)
        elif activity.kind == KIND_SMS:
            self.read_sms(activity, raw)
        return activity

    def activity_kind(self, activity: Activity, raw: dict) -> str:
        """Plan: map ``type`` through ``model.ACTIVITY_KINDS``; unknown types are unsupported.

        A WAIT is refined by ``wait_kind``.
        """
        if activity.raw_type == "WAIT":
            return self.wait_kind(activity, raw)
        return ACTIVITY_KINDS.get(activity.raw_type, KIND_UNSUPPORTED)

    def wait_kind(self, activity: Activity, raw: dict) -> str:
        """A WAIT is refined by ``metaData.uiType`` through WAIT_UI_TYPES; a uiType not listed is unsupported.

        Without a uiType the wait is told apart by what it holds: a non-empty
        ``waitEndDateAttributeExpression`` is a wait by attribute, else the key prefix (``WAITBYATTRIBUTE-1``,
        ``WAITBYDURATION-8``) names the kind. A wait nothing identifies is unsupported, with a warning.
        """
        ui_type = _dict(raw.get("metaData")).get("uiType")
        if ui_type:
            kind = WAIT_UI_TYPES.get(str(ui_type), KIND_UNSUPPORTED)
            if kind == KIND_UNSUPPORTED:
                self.warn(f"Attività {_named(activity)}: è un wait di tipo {ui_type!r}; sono supportati solo "
                          f"Wait by duration e Wait by attribute, quindi è segnalata come non supportata.")
            return kind
        expression = _dict(raw.get("configurationArguments")).get("waitEndDateAttributeExpression")
        if isinstance(expression, str) and expression.strip():
            return KIND_WAIT_ATTRIBUTE
        for prefix, kind in WAIT_UI_TYPES.items():
            if activity.key.upper().startswith(prefix):
                return kind
        self.warn(f"Attività {_named(activity)}: è un wait che non indica il suo tipo (metaData.uiType), non attende "
                  "un attributo data e non ha una chiave che lo identifichi, quindi è segnalata come non supportata.")
        return KIND_UNSUPPORTED

    def make_unsupported(self, activity: Activity, reason: str) -> None:
        """Spec: "Anything else is not an error: it is listed in the report, and the branches behind it
        are marked as not covered." The activity keeps its outcomes, so the graph stays whole."""
        activity.kind = KIND_UNSUPPORTED
        activity.unsupported_reason = reason
        for outcome in activity.outcomes:
            outcome.formula = None
            outcome.is_remainder = False
        self.warn(f"Attività {_named(activity)}: è segnalata come non supportata, perché {reason}.")

    def read_decision(self, activity: Activity, raw: dict) -> None:
        """Plan: ``configurationArguments.criteria[outcome.key]`` is parsed; the outcome without criteria is the
        remainder. Spec: "Outcomes are tried in order, the first true one wins, and the remainder branch means
        none matched" -> the outcomes keep their export order.
        """
        criteria = _dict(_dict(raw.get("configurationArguments")).get("criteria"))
        for outcome in activity.outcomes:
            xml = criteria.get(outcome.key)
            if not xml:
                outcome.is_remainder = True
                continue
            try:
                formula = parse_criteria(xml)
            except CriteriaError as error:
                self.make_unsupported(activity, f"i criteri del branch {outcome.label!r} non si possono leggere "
                                                f"({error})")
                return
            outcome.formula = self.map_formula(formula)
            problem = self.unsupported_condition(outcome)
            if problem is not None:
                # Spec: outcomes are tried in order, so the outcomes before this one can still be
                # covered; this one and the ones after it are reported as not covered.
                outcome.unsupported_reason = problem
                self.warn(f"Decision split {_named(activity)}: {problem}; questo branch e quelli successivi "
                          "sono segnalati come non coperti.")
        remainders = [position for position, outcome in enumerate(activity.outcomes, 1) if outcome.is_remainder]
        if remainders != [len(activity.outcomes)]:
            self.warn(f"Decision split {_named(activity)}: dovrebbe avere un solo branch senza criteri "
                      f"(il remainder), in ultima posizione; branch senza criteri: "
                      f"{remainders or 'nessuno'} su {len(activity.outcomes)}.")

    def map_formula(self, formula: Formula) -> Formula:
        """Condition tables: attribute-set name (Contact Data) or the trigger's event key (Journey Data) ->
        table name; fields in the schema's spelling. See ``map_condition``."""
        if isinstance(formula, Condition):
            return self.map_condition(formula)
        return ConditionGroup(formula.operator, tuple(self.map_formula(item) for item in formula.items))

    def map_condition(self, condition: Condition) -> Condition:
        """One condition mapped to its table.

        Spec, "Data sources of a condition": ``Event.<eventDefinitionKey>.<Field>`` reads column ``Field``
        of the data extension of the trigger's entry event; a key naming another event definition is
        unsupported, so that condition is kept as written (``unsupported_condition`` reports it). A
        Contact Data condition goes through its attribute set; a set no relationship names is recorded
        in ``missing_relationships``. An attribute comparison is normalised by ``normalise_comparison``.
        """
        mapped = self.map_attribute(condition.source, condition.table, condition.field)
        result = condition if mapped is None else replace(condition, table=mapped[0], field=mapped[1])
        path = self.condition_path(result.table, condition.path) if condition.source == SOURCE_CONTACT else ()
        result = replace(result, path=path)
        if condition.value_attribute is not None:
            result = self.normalise_comparison(result, condition)
        return result

    def condition_path(self, own_table: str, relationship_ids: tuple[str, ...]) -> tuple[str, ...]:
        """Spec, step 3, "Tables reached through other tables": "The export names the path: the attribute path of
        the condition (the relationship ids in AttributePath and UiMetaData)".

        ``relationship_ids`` (criteria.py: the ``groupToSetRelationshipId`` of ``UiMetaData``, then the
        ``RelationshipID`` of each ``<AttributePath>``) become the tables between the contact and ``own_table``,
        nearest the contact first: a group-to-set relationship gives the table linked to the contact (its right
        item), a relationship between two tables gives its left table, following the chain from the contact. An
        id that is not an attributeRelationships entity of the export is dropped, with a warning.
        """
        links: list[_Link] = []
        for relationship_id in relationship_ids:
            link = self.relationship_links.get(relationship_id)
            if link is None:
                self.warn(f"Una condizione legge la sua tabella attraverso la relazione {relationship_id}, che non è "
                          "nell'export: la relazione è ignorata nel percorso dal contatto alla tabella.")
            else:
                links.append(link)
        tables: list[Optional[str]] = [link.right_table for link in links if link.group_to_set]
        current = tables[-1] if tables else None
        chain = [link for link in links if not link.group_to_set]
        while chain:
            link = next((item for item in chain if item.left_table == current), chain[0])
            chain.remove(link)
            tables.append(link.left_table)
            current = link.right_table
        return _path_tables(tables, own_table)

    def map_attribute(self, source: str, owner: str, field_name: str, quiet: bool = False) -> Optional[tuple[str, str]]:
        """``(table, field)`` of an attribute as written: ``owner`` is the attribute set (Contact Data) or the
        event definition key (Journey Data). None for Journey Data of an event other than the trigger's.
        ``quiet`` reads without warnings (the other side of an attribute comparison)."""
        if source == SOURCE_JOURNEY:
            if self.entry is None or owner != self.entry.key:
                return None
            table = self.entry.table
        elif quiet:
            table = self.set_tables.get(owner, owner)
        else:
            table = self.table_for_set(owner)
        if source == SOURCE_CONTACT and owner in self.set_tables and table not in self.related_tables:
            if table not in self.missing_relationships:
                self.missing_relationships.append(table)
        if quiet:
            found = self.tables[table].field(field_name) if table in self.tables else None
            return table, found.name if found else field_name
        return table, self.schema_field(table, field_name)

    def normalise_comparison(self, mapped: Condition, raw: Condition) -> Condition:
        """Spec, "Attribute compared with an attribute": supported only as "key field of a contact-level
        linked table Equal the contact key". When the condition's own key is the contact key and its value
        attribute is a Contact Data field (``ValueIsReference``, the mirror of ``ValueParameterName``), the
        two sides are swapped, so that the condition always names the linked table's field and keeps the
        contact key as ``value_attribute``. Any other form is left as it is and reported as unsupported."""
        other = split_attribute(raw.value_attribute or "")
        if other is None or other[0] != SOURCE_CONTACT:
            return mapped
        if not self.is_contact_key(raw.source, raw.table, (mapped.table, mapped.field)):
            return mapped
        other_ref = self.map_attribute(*other, quiet=True)
        if other_ref is None:
            return mapped
        return replace(mapped, table=other_ref[0], field=other_ref[1], source=SOURCE_CONTACT,
                       value_attribute=f"Event.{raw.table}.{raw.field}")

    def entry_contact_key(self) -> Optional[str]:
        """The entry table's field holding the contact key, in the order tables.py uses (contract in
        docs/plan.md): the sendable field, the field a group-to-set relationship links on the entry table,
        a single-field primary key. None when none of them exists."""
        table = self.tables.get(self.entry.table) if self.entry is not None else None
        if table is None:
            return None
        if table.sendable_field and table.field(table.sendable_field):
            return table.field(table.sendable_field).name
        for rel in self.relationships:
            if rel.left_table is None and rel.right_table == table.name and table.field(rel.right_field):
                return table.field(rel.right_field).name
        primary_key = table.primary_key
        return primary_key[0] if len(primary_key) == 1 else None

    def is_contact_key(self, source: str, owner: str, ref: tuple[str, str]) -> bool:
        """Is this attribute the contact key: Journey Data of the trigger's event on the entry contact key field?"""
        key = self.entry_contact_key()
        return (source == SOURCE_JOURNEY and self.entry is not None and owner == self.entry.key
                and key is not None and ref[0] == self.entry.table and ref[1].lower() == key.lower())

    def contact_linked_fields(self) -> set[tuple[str, str]]:
        """``(table, lower-cased field)`` of every field the export links to the contact key: the entry contact
        key field, the fields of group-to-set relationships, and the fields joined to one of them by a
        relationship between two tables (the same links tables.py follows for the contact key group)."""
        key = self.entry_contact_key()
        linked = {(self.entry.table, key.lower())} if key and self.entry is not None else set()
        linked.update((rel.right_table, rel.right_field.lower()) for rel in self.relationships if rel.left_table is None)
        changed = True
        while changed:
            changed = False
            for rel in self.relationships:
                if rel.left_table is None or not rel.left_field:
                    continue
                pair = ((rel.left_table, rel.left_field.lower()), (rel.right_table, rel.right_field.lower()))
                if (pair[0] in linked) != (pair[1] in linked):
                    linked.update(pair)
                    changed = True
        return linked

    def unsupported_condition(self, outcome: Outcome) -> Optional[str]:
        """Why a mapped outcome formula cannot be evaluated, or None (see ``unsupported_formula``)."""
        return self.unsupported_formula(outcome.formula, f"il branch {outcome.label!r}")

    def unsupported_formula(self, formula: Optional[Formula], subject: str) -> Optional[str]:
        """Why a mapped formula cannot be evaluated, or None; ``subject`` names it in the reason ("il branch 'X'").

        Spec, operators: only the listed ones are understood, "the rest is reported, not guessed"
        (``criteria.OPERATORS``). A condition on a table or field that is not in the export's schemas cannot
        be given a type, a length or a nullability, so it is reported as unsupported rather than guessed.
        Spec, "Extensions beyond the pilot": Journey Data of an event other than the trigger's, an
        attribute set with no relationship in the export ("relationship missing in the export"), a
        Between value not written ``low|high`` and an attribute comparison in any form other than the
        supported one are unsupported. Spec, step 3: a relative date value is checked by ``unsupported_relative``.
        """
        for condition in formula_conditions(formula):
            if condition.operator not in OPERATORS:
                return f"{subject} usa l'operatore {condition.operator!r}, che il PoC non supporta"
            if condition.source == SOURCE_JOURNEY and (self.entry is None or condition.table != self.entry.table):
                name = self.event_name(condition.table)
                event = f"{condition.table} ({name})" if name else condition.table
                trigger = self.entry.key if self.entry is not None else ""
                return (f"{subject} legge Event.{condition.table}.{condition.field}, cioè i Journey Data "
                        f"dell'entry event {event}, che non è l'entry event del trigger del journey ({trigger})")
            if condition.source == SOURCE_CONTACT and condition.table in self.missing_relationships:
                return (f"{subject} legge {condition.table}.{condition.field}, ma l'attribute set di "
                        f"{condition.table} non ha alcuna relazione nell'export, quindi non si sa come la tabella è "
                        "collegata al contatto (relazione mancante nell'export)")
            table = self.tables.get(condition.table)
            if table is None:
                return (f"{subject} legge {condition.table}.{condition.field}, ma la tabella "
                        f"{condition.table!r} non è nell'export")
            field = table.field(condition.field)
            if field is None:
                return (f"{subject} legge {condition.table}.{condition.field}, ma il campo "
                        f"{condition.field!r} non è nello schema di {table.name}")
            if condition.operator == "Between" and condition.value_attribute is None \
                    and between_bounds(condition.value) is None:
                return (f"{subject} usa Between con il valore {condition.value!r}, che non è nella forma "
                        "basso|alto")
            if condition.value_attribute is not None and not self.supported_comparison(condition):
                return (f"{subject} confronta {condition.table}.{condition.field} con l'attributo "
                        f"{condition.value_attribute}: è supportato solo il confronto con Equal tra il campo che "
                        "collega una tabella al contatto e la chiave del contatto, letto come \"il contatto ha "
                        "una riga nella tabella\"")
            problem = self.unsupported_relative(condition, field, subject)
            if problem is not None:
                return problem
        return None

    def unsupported_relative(self, condition: Condition, field: Field, subject: str) -> Optional[str]:
        """Spec, step 3, "Relative dates in conditions": a value written ``;<sign>;<n>;<unit>`` "compares the field
        with the clock's date moved by n units" and "Any other value form, unit or operator is unsupported".

        A relative value (``Condition.relative``, set by criteria.py) is unsupported on a field whose type is not
        Date, in a unit that ``criteria.relative_units()`` does not list, and with an operator that
        ``criteria.RELATIVE_OPERATORS`` does not list ("The operators that take such a value are After,
        AtOrAfter, Before, AtOrBefore"). None when the condition has no relative value or is supported.
        """
        if condition.relative is None:
            return None
        written = f"{condition.table}.{condition.field} {condition.operator} {condition.value!r}"
        if field.type != "Date":
            return f"{subject} usa un valore relativo a oggi su un campo di tipo {field.type} ({written})"
        units = {str(unit).lower() for unit in criteria_rules.relative_units()}
        if condition.relative.unit.lower() not in units:
            return (f"{subject} usa un valore relativo a oggi in {condition.relative.unit!r}, un'unità che il "
                    f"generatore non supporta ({written})")
        if condition.operator not in criteria_rules.RELATIVE_OPERATORS:
            return (f"{subject} usa un valore relativo a oggi con l'operatore {condition.operator!r}, che non lo "
                    f"accetta ({written})")
        return None

    def supported_comparison(self, condition: Condition) -> bool:
        """Spec: an attribute comparison is supported only in the form "key field of a contact-level linked
        table Equal the contact key" (after ``normalise_comparison``, the condition names the linked field)."""
        other = split_attribute(condition.value_attribute or "")
        if condition.operator != "Equal" or condition.source != SOURCE_CONTACT or other is None:
            return False
        if (condition.table, condition.field.lower()) not in self.contact_linked_fields():
            return False
        other_ref = self.map_attribute(*other, quiet=True)
        return other_ref is not None and self.is_contact_key(other[0], other[1], other_ref)

    def read_engagement(self, activity: Activity, raw: dict, raw_outcomes: list[dict]) -> None:
        """Plan: ``refActivityCustomerKey`` -> engagement_ref; ``statsTypeId`` through ENGAGEMENT_KINDS;
        outcomes carry ``when`` from ``arguments.when``."""
        config = _dict(raw.get("configurationArguments"))
        activity.engagement_ref = _text_or_none(config.get("refActivityCustomerKey")) or None
        activity.engagement_kind = ENGAGEMENT_KINDS.get(_int_or_none(config.get("statsTypeId")))
        if activity.engagement_kind is None:
            self.warn(f"Engagement split {_named(activity)}: misura il tipo di statistica "
                      f"{config.get('statsTypeId')!r}, che non è né un open (2) né un click (3); i suoi branch "
                      f"saranno segnalati come non coperti.")
        for outcome, raw_outcome in zip(activity.outcomes, raw_outcomes):
            when = _dict(raw_outcome.get("arguments")).get("when")
            outcome.when = when if isinstance(when, bool) else None
        if activity.outcomes and all(o.when is None for o in activity.outcomes):
            self.make_unsupported(activity, "nessuno dei suoi branch indica se è il branch Yes o il branch No")
            return
        for outcome in activity.outcomes:
            if outcome.when is None:
                self.warn(f"Engagement split {_named(activity)}: il branch {outcome.label!r} non indica "
                          f"se è il branch Yes o il branch No.")
        urls = _list(_dict(config.get("engagementUrls")).get("urls"))
        if urls:
            self.warn(f"Engagement split {_named(activity)}: conta solo i click su questi link: "
                      f"{', '.join(str(url) for url in urls)}.")

    def read_random(self, activity: Activity, raw_outcomes: list[dict]) -> None:
        """Plan: ``percentage`` from ``outcomes[*].arguments.percentage``."""
        for outcome, raw_outcome in zip(activity.outcomes, raw_outcomes):
            outcome.percentage = _int_or_none(_dict(raw_outcome.get("arguments")).get("percentage"))
        percentages = [outcome.percentage for outcome in activity.outcomes]
        if None in percentages or sum(p for p in percentages if p is not None) != 100:
            self.warn(f"Random split {_named(activity)}: la somma delle percentuali {percentages} "
                      f"non è 100.")

    def read_wait_duration(self, activity: Activity, raw: dict) -> None:
        config = _dict(raw.get("configurationArguments"))
        activity.wait_amount = _int_or_none(config.get("waitDuration"))
        activity.wait_unit = _text_or_none(config.get("waitUnit")) or None
        activity.wait_timezone = _text_or_none(config.get("timeZone")) or None

    def read_wait_attribute(self, activity: Activity, raw: dict) -> None:
        """Plan: the field comes from ``configurationArguments.waitEndDateAttributeExpression`` (last quoted
        segment = field, segment before it = attribute set -> table); ``metaData.attribute.id`` is the fallback.

        Spec, step 3: the offset and the time of day are read by ``read_wait_offset``; the attribute sets the
        expression names between the contact and the field's set are the wait's path (``expression_path``).
        """
        config = _dict(raw.get("configurationArguments"))
        activity.wait_timezone = _text_or_none(config.get("timeZone")) or None
        self.read_wait_offset(activity, config)
        expression = config.get("waitEndDateAttributeExpression")
        found = _expression_field(expression)
        event_key = _event_key(expression)
        from_expression = found is not None
        if found is None:
            attribute_id = _dict(_dict(raw.get("metaData")).get("attribute")).get("id")
            parts = split_attribute(attribute_id) if isinstance(attribute_id, str) else None
            found = (parts[1], parts[2]) if parts else None
            event_key = parts[1] if parts and parts[0] == SOURCE_JOURNEY else None
        if found is None:
            self.warn(f"Wait {_named(activity)}: non è possibile identificare l'attributo data che il wait attende.")
            return
        set_name, field_name = found
        if event_key is not None:
            # Journey Data, as for split conditions: the column of the trigger's entry table.
            if self.entry is None or event_key != self.entry.key:
                self.warn(f"Wait {_named(activity)}: attende la data {field_name} dei Journey Data dell'entry event "
                          f"{event_key}, che non è l'entry event del trigger del journey; non è possibile "
                          "identificare l'attributo data.")
                return
            table = self.entry.table
        else:
            table = self.table_for_set(set_name)
            if from_expression:
                activity.wait_path = self.expression_path(expression, table)
        activity.wait_field = (table, self.schema_field(table, field_name))

    def read_wait_offset(self, activity: Activity, config: dict) -> None:
        """Spec, step 3, "The clock": "A wait by attribute holds the contact until the date in its field plus its
        offset (waitDuration and waitUnit, which may be negative ...), at the time of day given by specifiedTime
        when it has one, otherwise at the time of day held in the field."

        An offset of 0, or none, is no offset (None). An offset in a unit not in WAIT_UNITS is kept as written and
        reported, because the end of the wait cannot be computed.
        """
        amount = _int_or_none(config.get("waitDuration"))
        if amount:
            activity.wait_offset_amount = amount
            activity.wait_offset_unit = _text_or_none(config.get("waitUnit")) or None
            if (activity.wait_offset_unit or "").strip().upper() not in WAIT_UNITS:
                self.warn(f"Wait {_named(activity)}: termina {amount} {activity.wait_offset_unit!r} rispetto alla data "
                          "del suo campo, un'unità che il generatore non conosce, quindi non è possibile calcolare "
                          "quando il wait termina.")
        activity.wait_time_of_day = (_text_or_none(config.get("specifiedTime")) or "").strip() or None

    def expression_path(self, expression: Any, own_table: str) -> tuple[str, ...]:
        """Spec, step 3, "Tables reached through other tables": the tables the wait's expression names between the
        contact and the table of its field, nearest the contact first.

        ``{{Contact.Attribute.Contacts.PublicHoliday_Entry.PublicHoliday_DE."PHEnd"}}`` -> ``(PublicHoliday_Entry,)``:
        the segments after ``Contact.Attribute``, without a first segment that is not an attribute set
        (``Contacts``, or an attribute group), up to the set of the field (excluded); set names are mapped to
        tables as a condition's are. Any other kind of expression gives no path.
        """
        segments = _personalisation_segments(expression)
        if segments[:2] != ["Contact", "Attribute"]:
            return ()
        sets = segments[2:-2]
        if sets and sets[0] not in self.set_tables:
            sets = sets[1:]
        return _path_tables([self.table_for_set(set_name) for set_name in sets], own_table)

    def read_sms(self, activity: Activity, raw: dict) -> None:
        """Spec, step 3, "SMS sends": "SMSSYNC is a send to the phone number of the journey's default mobile
        number ... It has no branching." ``sms_asset_id`` is ``configurationArguments.assetId`` as written (a
        resolved reference gives the asset id); ``sms_name`` is the activity name."""
        asset_id = _dict(raw.get("configurationArguments")).get("assetId")
        activity.sms_asset_id = None if asset_id is None or asset_id == "" else str(asset_id)
        activity.sms_name = activity.name

    def read_wait_sto(self, activity: Activity, raw: dict) -> None:
        """Send-time wait: the email goes out within ``params.slidingWindowHours``.

        Recorded as ``wait_amount`` hours (the longest delay), because the spec notes the
        expected delay of a send-time wait in the testbook.
        """
        params = _dict(_dict(raw.get("configurationArguments")).get("params"))
        hours = _int_or_none(params.get("slidingWindowHours"))
        if hours is not None:
            activity.wait_amount = hours
            activity.wait_unit = "HOURS"

    def read_email(self, activity: Activity, raw: dict) -> None:
        """Plan: ``triggeredSend.emailId`` resolves to the asset id; ``email_name`` is the asset name, else the
        activity name. An emailId holding the asset's legacy id is matched to that asset too."""
        send = _dict(_dict(raw.get("configurationArguments")).get("triggeredSend"))
        email_ref = send.get("emailId")
        asset_id = None if email_ref is None else str(email_ref)
        if asset_id is not None and asset_id not in self.emails:
            asset_id = self.legacy_email_ids.get(asset_id, asset_id)
        activity.email_asset_id = asset_id
        asset = self.emails.get(asset_id) if asset_id else None
        activity.email_name = asset.name if asset else activity.name
        if asset is None:
            self.warn(f"Email {_named(activity)}: invia l'email {asset_id!r}, che non è nell'export; "
                      f"non si sa se contiene un link.")

    def read_update(self, activity: Activity, raw: dict) -> None:
        """Plan: ``arguments.activityData.updateContactFields[*]`` -> Write(table, field, value); the value is
        kept as written (``str(value)`` for numbers), ``{{DateTime.Now}}`` literally."""
        activity_data = _dict(_dict(raw.get("arguments")).get("activityData"))
        for item in _list(activity_data.get("updateContactFields")):
            if not isinstance(item, dict):
                continue
            table = self.table_ref(item.get("dataExtensionId"), f"Update contact {_named(activity)}")
            field_name = self.schema_field(table, str(item.get("field") or ""))
            activity.writes.append(Write(table=table, field=field_name, value=_written_value(item.get("value"))))
        if not activity.writes:
            self.warn(f"Update contact {_named(activity)}: non scrive alcun campo.")
