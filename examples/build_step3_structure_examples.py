"""Builders for the structural examples of the spec section "Extensions for time-dependent journeys (step 3, 14
October 2026)": tables reached through other tables, columns the entry query copies or derives, filters on the
current date moved by days, LEFT JOIN sources, synchronized (managed) data extensions, and `IsNot` on a nullable
Boolean.

package.json only; expected.json and README.md are written by hand from docs/spec.md (examples/README.md, "The
rule"). Reuses the helpers of examples/build_examples.py and examples/build_extension_examples.py, so every package
keeps the shape of the real exports, and adds the raw shapes step 3 introduces (copied in form only from the real
exports, with invented names):

- a condition through a path: Key="<set>.<field>", UiMetaData with the groupToSetRelationshipId of the contact link
  and the value wrapped in <AttributePath RelationshipID="<set-to-set relationship id>"> (Package.cond does it for
  every table after the first of the chain);
- a wait by attribute through a path: waitEndDateAttributeExpression
  {{Contact.Attribute.Contacts.<LinkSet>.<Set>."<Field>"}} (Journey.wait_attribute);
- relative date values ";+;0;days" with the operators After / AtOrBefore, and the operator IsNot;
- entry queries with a CASE WHEN column, a LEFT JOIN, CAST(... AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE);
- a managed data extension: entity issue UNSUPPORTED_FUNCTIONALITY / NO_DEPLOY / ManagedDataExtension.

Run:  python examples/build_step3_structure_examples.py [name ...]
(writes examples/<name>/package.json, then validates package and expected.json like build_examples.py, and checks
that every table and field named in expected.json exists in the package)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(EXAMPLES_DIR))
import build_examples as B  # noqa: E402
import build_extension_examples as E  # noqa: E402
from build_examples import (Journey, Package, add_email_asset, add_entry_source, add_journey, boolean,  # noqa: E402
                            criteria, date, email_field, export, log_update, phone, setup_contact_only,
                            sql_master_only, table_sql, text)

RELATIVE_TODAY = ";+;0;days"      # shown in the interface as "Today"


def contact_tables(pkg: Package, entry_extra: list[dict], master_extra: list[dict]) -> None:
    """Entry + contact-level Master (+ written-only DE_Log), Master -> Entry on ContactId, like setup_contact_only
    but with extra columns on the entry table too."""
    pkg.table("DE_Entry", [text("ContactId", 50, pk=True, nullable=False), email_field()] + entry_extra,
              sendable_field="ContactId")
    pkg.table("DE_Master", [text("ContactId", 50, pk=True, nullable=False), text("FirstName"), email_field(),
                            phone()] + master_extra, sendable_field="ContactId")
    B.log_table(pkg)
    pkg.contact_model([("DE_Master", "ContactId", ""), ("DE_Entry", "ContactId", "ContactId")])


# ---------------------------------------------------------------------------
# 1. A table read through a link table (condition and wait by attribute through a path)
# ---------------------------------------------------------------------------

def build_link_table_path() -> dict:
    pkg = Package("link_table_path", "EX link table path")
    E.entry_table(pkg, "DE_HL_Entry", [])
    # The link table: contact key in SubscriberKey, link field HKey with a schema default 'A' (as in the real export).
    pkg.table("Holiday_Entry", [E.key_field(), text("HKey", 20, nullable=False, default="A")])
    # The table at the end of the path: no primary key, joined on HKey.
    pkg.table("Holiday_DE", [text("HKey", 20, nullable=False), date("HStart"), date("HEnd")])
    pkg.contact_model([("Holiday_Entry", "SubscriberKey", ""), ("Holiday_DE", "HKey", "HKey")])
    add_email_asset(pkg, "900951", "Before You Leave", "BEFORE YOU LEAVE", with_link=False)
    add_email_asset(pkg, "900952", "Welcome Back", "WELCOME BACK", with_link=False)
    add_email_asset(pkg, "900953", "Regular News", "REGULAR NEWS", with_link=False)
    h = "Holiday_DE"
    j = Journey(pkg, "EX_LinkTablePath")
    event_id, _ = E.add_de_entry(pkg, j.name, "DE_HL_Entry")
    j.decision("SPLIT-1", "Holiday period", [
        ("path_a", "Holiday ahead", criteria(pkg.cond(h, "HStart", "After", RELATIVE_TODAY)), "EMAIL-1",
         "HStart after Today"),
        ("path_b", "On holiday", criteria(pkg.cond(h, "HStart", "AtOrBefore", RELATIVE_TODAY)), "WAIT-1",
         "HStart at or before Today"),
        ("remainder_path", "No holiday", None, "EMAIL-3", None)])
    j.email("EMAIL-1", "900951", None)
    j.wait_attribute("WAIT-1", h, "HEnd", "EMAIL-2")       # {{Contact.Attribute.Contacts.Holiday_Entry.Holiday_DE."HEnd"}}
    j.email("EMAIL-2", "900952", None)
    j.email("EMAIL-3", "900953", None)
    E.add_journey_ext(pkg, j, event_id, "EmailAudience")
    return export(pkg)


# ---------------------------------------------------------------------------
# 2. An entry column copied from a source column: one variable for Journey Data and Contact Data
# ---------------------------------------------------------------------------

def build_entry_copy_alias() -> dict:
    pkg = Package("entry_copy_alias", "EX entry copy alias")
    contact_tables(pkg, [text("Tier", 20)], [text("Tier", 20)])
    j = Journey(pkg, "EX_EntryCopyAlias")
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress,\n    M.Tier AS Tier\nFROM %s M"
           % table_sql(pkg.tables["DE_Master"]))
    event_id = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_EntryCopyAlias_Entry", sql)
    m = "DE_Master"
    j.decision("SPLIT-1", "Tier at entry", [
        ("path_a", "Gold", criteria(E.jd_cond(event_id, "Tier", "Equal", "Gold")), "SPLIT-2", "Tier equal Gold"),
        ("remainder_path", "Remainder", None, "SPLIT-3", None)])
    j.decision("SPLIT-2", "Tier now, after Gold", [
        ("path_a", "Gold", criteria(pkg.cond(m, "Tier", "Equal", "Gold")), "UPDATE-1", "Tier equal Gold"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    j.decision("SPLIT-3", "Tier now, after not Gold", [
        ("path_a", "Gold", criteria(pkg.cond(m, "Tier", "Equal", "Gold")), "UPDATE-3", "Tier equal Gold"),
        ("remainder_path", "Remainder", None, "UPDATE-4", None)])
    log_update(j, "UPDATE-1", "Gold confirmed", "GoldConfirmed", None)
    log_update(j, "UPDATE-2", "Gold lost", "GoldLost", None)
    log_update(j, "UPDATE-3", "Gold gained", "GoldGained", None)
    log_update(j, "UPDATE-4", "Never gold", "NeverGold", None)
    add_journey(pkg, j, event_id)
    return export(pkg)


# ---------------------------------------------------------------------------
# 3. An entry column computed by CASE WHEN from a LEFT JOINed table, read by IsNot
# ---------------------------------------------------------------------------

def build_case_when_identifier() -> dict:
    pkg = Package("case_when_identifier", "EX case when identifier")
    contact_tables(pkg, [boolean("Email_Identifier")], [])
    pkg.table("DE_Backup", [text("ContactId", 50, pk=True, nullable=False), date("Email_Timestamp")])
    j = Journey(pkg, "EX_CaseWhenIdentifier")
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress,\n"
           "    CASE WHEN B.Email_Timestamp IS NOT NULL AND B.Email_Timestamp >= DATEADD(day, -7, GETDATE())\n"
           "         THEN 'True' ELSE 'False' END AS Email_Identifier\n"
           "FROM %s M\nLEFT JOIN %s B ON M.ContactId = B.ContactId"
           % (table_sql(pkg.tables["DE_Master"]), table_sql(pkg.tables["DE_Backup"])))
    event_id = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_CaseWhenIdentifier_Entry", sql)
    j.decision("SPLIT-1", "Emailed this week?", [
        ("path_a", "N", criteria(E.jd_cond(event_id, "Email_Identifier", "IsNot", "true")), "UPDATE-1",
         "Email_Identifier is not True"),
        ("remainder_path", "Y", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "Send reminder", "Reminder", None)
    log_update(j, "UPDATE-2", "Already emailed", "AlreadyEmailed", None)
    add_journey(pkg, j, event_id)
    return export(pkg)


# ---------------------------------------------------------------------------
# 4. Filters on the date part of a column = the current date moved by days (both spellings of the spec)
# ---------------------------------------------------------------------------

def build_date_offset_filter() -> dict:
    pkg = Package("date_offset_filter", "EX date offset filter")
    contact_tables(pkg, [], [boolean("Perm"), date("LastVisit")])
    pkg.table("DE_Member", [text("ContactId", 50, pk=True, nullable=False), date("CreatedDate", nullable=False)])
    j = Journey(pkg, "EX_DateOffsetFilter")
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress\nFROM %s M\nJOIN %s CM ON M.ContactId = CM.ContactId\n"
           "WHERE CAST(CM.CreatedDate AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)\n"
           "AND CAST(M.LastVisit AS DATE) = DATEADD(day, -2, CAST(GETDATE() AS DATE))"
           % (table_sql(pkg.tables["DE_Master"]), table_sql(pkg.tables["DE_Member"])))
    event_id = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_DateOffsetFilter_Entry", sql)
    j.decision("SPLIT-1", "Permission", [
        ("path_a", "true", criteria(pkg.cond("DE_Master", "Perm", "Is", "true")), "UPDATE-1", "Perm is True"),
        ("remainder_path", "false", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "Allowed", "Allowed", None)
    log_update(j, "UPDATE-2", "Not allowed", "NotAllowed", None)
    add_journey(pkg, j, event_id)
    return export(pkg)


# ---------------------------------------------------------------------------
# 5. A source table that Package Manager marks as a managed (synchronized) data extension
# ---------------------------------------------------------------------------

MANAGED_TABLE = "CampaignMember_Salesforce_3"
MANAGED_ISSUE = {"type": "UNSUPPORTED_FUNCTIONALITY", "level": "WARNING", "sourceJobType": "PACKAGE",
                 "functionality": "NO_DEPLOY", "objectType": "ManagedDataExtension"}


def build_managed_source_table() -> dict:
    pkg = Package("managed_source_table", "EX managed source table")
    contact_tables(pkg, [], [boolean("Perm")])
    de_id = pkg.table(MANAGED_TABLE, [text("Id", 18, pk=True, nullable=False), text("ContactId", 18),
                                      text("Status", 40)])
    pkg.entities["dataExtensions"][de_id]["issues"] = [dict(MANAGED_ISSUE)]
    j = Journey(pkg, "EX_ManagedSourceTable")
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress\nFROM %s M\nJOIN %s CM ON M.ContactId = CM.ContactId\n"
           "WHERE CM.Status = 'Responded'" % (table_sql(pkg.tables["DE_Master"]), table_sql(de_id)))
    event_id = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_ManagedSourceTable_Entry", sql)
    j.decision("SPLIT-1", "Permission", [
        ("path_a", "true", criteria(pkg.cond("DE_Master", "Perm", "Is", "true")), "UPDATE-1", "Perm is True"),
        ("remainder_path", "false", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "Allowed", "Allowed", None)
    log_update(j, "UPDATE-2", "Not allowed", "NotAllowed", None)
    add_journey(pkg, j, event_id)
    return export(pkg)


# ---------------------------------------------------------------------------
# 6. IsNot 'true' on a nullable Boolean, then IsNull on the same field
# ---------------------------------------------------------------------------

def build_is_not_on_null_boolean() -> dict:
    pkg = Package("is_not_on_null_boolean", "EX is not on null boolean")
    setup_contact_only(pkg, [boolean("Flag")])
    m = "DE_Master"
    j = Journey(pkg, "EX_IsNotOnNullBoolean")
    j.decision("SPLIT-1", "Flag not true", [
        ("path_a", "Not true", criteria(pkg.cond(m, "Flag", "IsNot", "true")), "SPLIT-2", "Flag is not True"),
        ("remainder_path", "True", None, "UPDATE-3", None)])
    j.decision("SPLIT-2", "Flag empty", [
        ("path_a", "Empty", criteria(pkg.cond(m, "Flag", "IsNull", "")), "UPDATE-1", "Flag is null"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "Flag empty", "FlagEmpty", None)
    log_update(j, "UPDATE-2", "Flag false", "FlagFalse", None)
    log_update(j, "UPDATE-3", "Flag true", "FlagTrue", None)
    event = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_IsNotOnNullBoolean_Entry", sql_master_only(pkg))
    add_journey(pkg, j, event)
    return export(pkg)


# ---------------------------------------------------------------------------
# 7. A LEFT JOIN source whose column the entry copies and a Journey Data split reads
# ---------------------------------------------------------------------------

def build_left_join_source_row() -> dict:
    pkg = Package("left_join_source_row", "EX left join source row")
    contact_tables(pkg, [E.number("Score")], [])
    pkg.table("DE_Score", [text("ContactId", 50, pk=True, nullable=False), E.number("Score")])
    j = Journey(pkg, "EX_LeftJoinSourceRow")
    sql = ("SELECT\n    M.ContactId,\n    M.Emailaddress,\n    S.Score\nFROM %s M\n"
           "LEFT JOIN %s S ON M.ContactId = S.ContactId"
           % (table_sql(pkg.tables["DE_Master"]), table_sql(pkg.tables["DE_Score"])))
    event_id = add_entry_source(pkg, j.name, "DE_Entry", "Q_EX_LeftJoinSourceRow_Entry", sql)
    j.decision("SPLIT-1", "Score above 10", [
        ("path_a", "High", criteria(E.jd_cond(event_id, "Score", "GreaterThan", "10")), "UPDATE-1",
         "Score greater than 10"),
        ("remainder_path", "Remainder", None, "UPDATE-2", None)])
    log_update(j, "UPDATE-1", "High score", "High", None)
    log_update(j, "UPDATE-2", "Low score", "Low", None)
    add_journey(pkg, j, event_id)
    return export(pkg)


STRUCTURE = {
    "link_table_path": build_link_table_path,
    "entry_copy_alias": build_entry_copy_alias,
    "case_when_identifier": build_case_when_identifier,
    "date_offset_filter": build_date_offset_filter,
    "managed_source_table": build_managed_source_table,
    "is_not_on_null_boolean": build_is_not_on_null_boolean,
    "left_join_source_row": build_left_join_source_row,
}


# ---------------------------------------------------------------------------
# Validation of the names used in expected.json
# ---------------------------------------------------------------------------

def _schemas(package: dict) -> dict[str, set[str]]:
    return {e["data"]["name"]: {f["name"] for f in e["data"]["fields"]}
            for e in package["entities"]["dataExtensions"].values()}


def validate_names(expected_path: Path, package_path: Path) -> None:
    """Every table and Table.Field that expected.json names exists in the package (a typo would make a case or a
    proposed key silently match nothing), and csv_tables names every table the other keys say has a CSV."""
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    package = json.loads(package_path.read_text(encoding="utf-8"))
    schemas = _schemas(package)
    journey = package["entities"]["journeys"][package["selectedEntities"]["journeys"][0]]["data"]
    activity_keys = {a["key"] for a in journey["activities"]}

    def activity(key) -> None:
        assert key is None or key in activity_keys, "%s: unknown activity %s" % (expected_path, key)

    def table(name: str) -> None:
        assert name in schemas, "%s: unknown table %s" % (expected_path, name)

    def field(ref_: str) -> None:
        t, f = ref_.split(".", 1)
        table(t)
        assert f in schemas[t], "%s: unknown field %s" % (expected_path, ref_)

    csv_tables = set(expected.get("csv_tables", []))
    for t in csv_tables | set(expected.get("table_levels", {})) | set(expected.get("table_roles", {})):
        table(t)
    for t in expected.get("csv_rows", {}):
        table(t)
        assert t in csv_tables, "%s: csv_rows names %s, not in csv_tables" % (expected_path, t)
    for ref_ in expected.get("csv_values", {}):
        field(ref_)
        assert ref_.split(".", 1)[0] in csv_tables, "%s: csv_values names %s" % (expected_path, ref_)
    for case in expected.get("cases", []):
        for key in ("scenario_values", "contact_values", "contact_constraints"):
            for ref_ in case.get(key, {}):
                field(ref_)
        for t in case.get("has_row", {}):
            table(t)
            assert t in csv_tables, "%s: has_row names %s, not in csv_tables" % (expected_path, t)
        for key in [case.get("end_activity")] + list(case.get("timeline", {})) + case.get("emails_sent", []):
            activity(key)


def main(names=None) -> int:
    names = names or list(STRUCTURE)
    for name in names:
        d = EXAMPLES_DIR / name
        d.mkdir(exist_ok=True)
        (d / "package.json").write_text(json.dumps(STRUCTURE[name](), indent=1, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    for name in names:
        d = EXAMPLES_DIR / name
        branches = B.validate_package(d / "package.json")
        if (d / "expected.json").exists():
            B.validate_expected(d / "expected.json", branches)
            validate_names(d / "expected.json", d / "package.json")
        print("ok  %-26s %3d split branches %s" % (name, len(branches), branches))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
