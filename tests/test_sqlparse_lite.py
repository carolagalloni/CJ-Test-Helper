"""Unit tests for cjhelper.sqlparse_lite (docs/plan.md, "sqlparse_lite.py").

The four pilot queries are copied from fixtures/CJ template complesso-v1.json
(entities.queryActivities[*].data.queryText) in the form the loader hands them over:
``{{mcpm#.../namePrefix}}[{{mcpm#.../name}}]`` resolved to ``[DE_NAME]`` (no table in the
pilot has a namePrefix). The four pilot tables are written by hand from the same export
(entities.dataExtensions[*].data.fields).
"""

from datetime import datetime

import pytest
import sqlglot
from sqlglot import exp

from cjhelper.model import (
    ColumnRef,
    DateBoundFilter,
    DateOffsetFilter,
    EqualityFilter,
    Field,
    IsNullFilter,
    JoinEquality,
    ParsedQuery,
    Query,
    SelectColumn,
    SelectConcat,
    SelectCurrentDate,
    SelectItem,
    SelectLiteral,
    SelectUnsupported,
    Table,
    TableRef,
    UnsupportedFilter,
)
from cjhelper.sqlparse_lite import dotnet_to_strftime, parse_query

MASTER = "SF_Master_DataExtension_DE"
TARGET = "DE_NCRM_AdHoc_Target"
CONFIG = "DE_NCRM_AD_HOC_CONFIG"
ENTRY = "DE_NCM_AdHoc_Entry"


# ---------------------------------------------------------------------------
# Pilot queries, resolved
# ---------------------------------------------------------------------------

BACKUP_SQL = (
    "select\n"
    "*\n"
    "from [DE_NCM_AdHoc_Entry]"
)

CONFIG_UPDATE_SQL = (
    "SELECT\n"
    "a.CampaignName,\n"
    "a.Channel1,\n"
    "a.START_DATE,\n"
    "a.END_DATE,\n"
    "'True' AS HAS_RUN,\n"
    "'False' AS ACTIVE,\n"
    "GETUTCDATE()\n"
    "AT TIME ZONE 'UTC'\n"
    "AT TIME ZONE 'Central European Standard Time' AS RUN_TIME\n"
    "FROM [DE_NCRM_AD_HOC_CONFIG] a\n"
    "JOIN [DE_NCM_AdHoc_Entry] b \n"
    "ON a.CampaignName = b.CampaignName\n"
    "WHERE RUN_TIME IS NULL"
)

ENTRY_SQL = (
    "SELECT \n"
    "    M.ContactId,\n"
    "    M.FirstName,\n"
    "    M.LastName,\n"
    "    M.Emailaddress,\n"
    "    M.AccountId, \n"
    "    M.AccountStatus, \n"
    "    M.TypeOfCustomer, \n"
    "    M.CustomerNumber, \n"
    "    M.Salutation, \n"
    "    M.Country, \n"
    "    M.CustomerRegistrationType, \n"
    "    M.SiebelCustomerNumber, \n"
    "    M.Phone, \n"
    "    M.MobilePhone, \n"
    "    M.Platform, \n"
    "    M.Entitle_RequestCancellationReason, \n"
    "    M.ReceptionType,\n"
    "    M.BookedPackages, \n"
    "    M.Entitle_RequestCancellationDate, \n"
    "    M.Entitle_CommitmentStartDate,\n"
    "    M.MasterContactLock, \n"
    "    M.EmailOptOut, \n"
    "    M.IsMailOffers, \n"
    "    M.Entitle_ContractId,\n"
    "    M.Entitle_ContractStatus,\n"
    "    M.Entitle_ContractNumber, \n"
    "    M.City, \n"
    "    M.PostalCode,\n"
    "    M.Entitle_ContractTerm, \n"
    "    M.Entitle_ActivatedDate, \n"
    "    M.IsDunning,\n"
    "    C.CampaignName,\n"
    "CONCAT(\n"
    "    M.ContactId, '_', \n"
    "    C.CampaignName, '_', \n"
    "    FORMAT(GETUTCDATE(), 'yyyyMMdd_HHmm')\n"
    ") AS ActionId\n"
    "\n"
    "    \n"
    "FROM [SF_Master_DataExtension_DE] M\n"
    "JOIN [DE_NCRM_AdHoc_Target] T ON M.ContactId = T.SalesforceId\n"
    "JOIN [DE_NCRM_AD_HOC_CONFIG] C on T.Description = C.CampaignName\n"
    "/*LEFT JOIN \t[DE_NCM_AdHoc_Entry] E ON T.SalesforceId = E.ContactId\n"
    "WHERE E.ContactId is null*/\n"
    "where C.active = 'True'\n"
    "and (CAST(GETUTCDATE() AS DATE) >= CAST(START_DATE AS DATE) AND CAST(GETUTCDATE() AS DATE) <= CAST(END_DATE AS DATE))\n"
    "AND C.HAS_RUN = 'False'"
)

CHECK_ACTIVE_SQL = (
    "SELECT\n"
    "CampaignName,\n"
    "'True' AS Active\n"
    "FROM [DE_NCRM_AD_HOC_CONFIG]\n"
    "WHERE\n"
    "ACTIVE = 'True'"
)


# ---------------------------------------------------------------------------
# Pilot tables, from entities.dataExtensions
# Each row: name, type, length, is_primary_key, is_nullable, default, ordinal (as Field).
# ---------------------------------------------------------------------------

MASTER_FIELDS = [  # SF_Master_DataExtension_DE
    ("ContactId", "Text", 250, True, False, None, 0),
    ("FirstName", "Text", 50, False, True, None, 1),
    ("LastName", "Text", 50, False, True, None, 2),
    ("Emailaddress", "EmailAddress", 254, False, True, None, 3),
    ("AccountId", "Text", 50, False, True, None, 4),
    ("AccountStatus", "Text", 50, False, True, None, 5),
    ("TypeOfCustomer", "Text", 50, False, True, None, 6),
    ("CustomerNumber", "Text", 50, False, True, None, 7),
    ("Salutation", "Text", 50, False, True, None, 8),
    ("Country", "Text", 50, False, True, None, 9),
    ("CustomerRegistrationType", "Text", 50, False, True, None, 10),
    ("SiebelCustomerNumber", "Text", 50, False, True, None, 11),
    ("Phone", "Phone", 50, False, True, None, 12),
    ("MobilePhone", "Phone", 50, False, True, None, 13),
    ("Platform", "Text", 50, False, True, None, 14),
    ("Entitle_RequestCancellationReason", "Text", 50, False, True, None, 15),
    ("ReceptionType", "Text", 50, False, True, None, 16),
    ("BookedPackages", "Text", 50, False, True, None, 17),
    ("Entitle_RequestCancellationDate", "Date", None, False, True, None, 18),
    ("Entitle_CommitmentStartDate", "Date", None, False, True, None, 19),
    ("MasterContactLock", "Boolean", None, False, True, None, 20),
    ("EmailOptOut", "Boolean", None, False, True, None, 21),
    ("IsMailOffers", "Boolean", None, False, True, None, 22),
    ("Entitle_ContractId", "Text", 50, False, True, None, 23),
    ("Entitle_ContractStatus", "Text", 50, False, True, None, 24),
    ("Entitle_ContractNumber", "Text", 50, False, True, None, 25),
    ("City", "Text", 50, False, True, None, 26),
    ("PostalCode", "Text", 50, False, True, None, 27),
    ("Entitle_ContractTerm", "Text", 50, False, True, None, 28),
    ("Entitle_ActivatedDate", "Text", 50, False, True, None, 29),
    ("IsDunning", "Boolean", None, False, True, None, 30),
    ("MarketingPermission", "Boolean", None, False, True, "False", 31),
    ("PhoneConsent", "Boolean", None, False, True, None, 32),
]

TARGET_FIELDS = [  # DE_NCRM_AdHoc_Target
    ("SalesforceId", "Text", 50, True, False, None, 0),
    ("LastSyncedAt", "Date", None, False, True, None, 1),
    ("Description", "Text", 255, True, False, None, 2),
    ("CreatedDate", "Date", None, False, False, "GetDate()", 3),
]

CONFIG_FIELDS = [  # DE_NCRM_AD_HOC_CONFIG
    ("CampaignName", "Text", 250, True, False, None, 0),
    ("START_DATE", "Date", None, False, False, "GETDATE()", 1),
    ("END_DATE", "Date", None, False, False, "GETDATE()", 2),
    ("CHANNEL1", "Text", 50, False, False, None, 3),
    ("CHANNEL2", "Text", 50, False, True, None, 4),
    ("CHANNEL3", "Text", 50, False, True, None, 5),
    ("CHANNEL4", "Text", 50, False, True, None, 6),
    ("IGNOREPERM1", "Boolean", None, False, False, "False", 7),
    ("IGNOREPERM2", "Boolean", None, False, False, "False", 8),
    ("IGNOREPERM3", "Boolean", None, False, False, "False", 9),
    ("WAIT1", "Date", None, False, False, "GETDATE()", 10),
    ("WAIT2", "Date", None, False, False, "GETDATE()", 11),
    ("WAIT3", "Date", None, False, False, "GETDATE()", 12),
    ("old_not_used", "Date", None, False, False, "GETDATE()", 13),
    ("Control Group Required", "Boolean", None, False, False, "False", 14),
    ("ACTIVE", "Boolean", None, False, False, "False", 15),
    ("HAS_RUN", "Boolean", None, False, False, "False", 16),
    ("RUN_TIME", "Date", None, False, True, None, 17),
    ("Open_Click_Strategy", "Boolean", None, False, False, "False", 18),
    ("IGNOREPERM4", "Boolean", None, False, False, "False", 19),
]

ENTRY_FIELDS = [  # DE_NCM_AdHoc_Entry
    ("ContactId", "Text", 255, True, False, None, 0),
    ("Emailaddress", "EmailAddress", 254, False, True, None, 1),
    ("AccountId", "Text", 100, False, True, None, 2),
    ("AccountStatus", "Text", 50, False, True, None, 3),
    ("Mailing CG", "Text", 50, False, True, None, 4),
    ("Outbound_Timestamp", "Date", None, False, True, None, 5),
    ("Outbound_Identifier", "Boolean", None, False, True, None, 6),
    ("MobilePhone", "Phone", 50, False, True, None, 7),
    ("Mailing_Identifier", "Boolean", None, False, True, None, 8),
    ("Mailing_Timestamp", "Date", None, False, True, None, 9),
    ("OutboundAudience", "Text", 50, False, True, None, 10),
    ("ActionId", "Text", 255, True, False, None, 11),
    ("CampaignName", "Text", 255, True, False, None, 12),
    ("Control Group", "Boolean", None, False, True, "False", 13),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_table(name: str, rows: list[tuple], sendable_field: str | None = None) -> Table:
    return Table(
        id=name,
        name=name,
        customer_key=None,
        fields=[Field(*row) for row in rows],
        is_sendable=sendable_field is not None,
        sendable_field=sendable_field,
    )


def pilot_tables() -> dict[str, Table]:
    tables = [
        make_table(MASTER, MASTER_FIELDS, "ContactId"),
        make_table(TARGET, TARGET_FIELDS, "SalesforceId"),
        make_table(CONFIG, CONFIG_FIELDS),
        make_table(ENTRY, ENTRY_FIELDS, "ContactId"),
    ]
    return {table.name: table for table in tables}


def parse(sql: str, name: str = "Q_TEST", target: str = ENTRY,
          tables: dict[str, Table] | None = None) -> ParsedQuery:
    query = Query(id=f"id-{name}", name=name, target_table=target, sql=sql, update_type="Overwrite")
    return parse_query(query, pilot_tables() if tables is None else tables)


def parse_entry() -> ParsedQuery:
    return parse(ENTRY_SQL, name="Q_SKYDE_AdHoc_Target", target=ENTRY)


def warned(parsed: ParsedQuery, *fragments: str) -> bool:
    """True when one warning holds every fragment."""
    return any(all(fragment in warning for fragment in fragments) for warning in parsed.warnings)


# Short FROM clauses on the pilot tables, for the edge cases.
FROM_MASTER = f"FROM [{MASTER}] M"
FROM_CONFIG = f"FROM [{CONFIG}] C"
FROM_PILOT = (
    f"FROM [{MASTER}] M "
    f"JOIN [{TARGET}] T ON M.ContactId = T.SalesforceId "
    f"JOIN [{CONFIG}] C ON T.Description = C.CampaignName"
)


# ---------------------------------------------------------------------------
# Pilot: entry query Q_SKYDE_AdHoc_Target
# ---------------------------------------------------------------------------

# Every plain column of the entry query's SELECT, in order: (table, column as written).
ENTRY_PLAIN_COLUMNS = [
    (MASTER, "ContactId"),
    (MASTER, "FirstName"),
    (MASTER, "LastName"),
    (MASTER, "Emailaddress"),
    (MASTER, "AccountId"),
    (MASTER, "AccountStatus"),
    (MASTER, "TypeOfCustomer"),
    (MASTER, "CustomerNumber"),
    (MASTER, "Salutation"),
    (MASTER, "Country"),
    (MASTER, "CustomerRegistrationType"),
    (MASTER, "SiebelCustomerNumber"),
    (MASTER, "Phone"),
    (MASTER, "MobilePhone"),
    (MASTER, "Platform"),
    (MASTER, "Entitle_RequestCancellationReason"),
    (MASTER, "ReceptionType"),
    (MASTER, "BookedPackages"),
    (MASTER, "Entitle_RequestCancellationDate"),
    (MASTER, "Entitle_CommitmentStartDate"),
    (MASTER, "MasterContactLock"),
    (MASTER, "EmailOptOut"),
    (MASTER, "IsMailOffers"),
    (MASTER, "Entitle_ContractId"),
    (MASTER, "Entitle_ContractStatus"),
    (MASTER, "Entitle_ContractNumber"),
    (MASTER, "City"),
    (MASTER, "PostalCode"),
    (MASTER, "Entitle_ContractTerm"),
    (MASTER, "Entitle_ActivatedDate"),
    (MASTER, "IsDunning"),
    (CONFIG, "CampaignName"),
]


def test_entry_query_sources_keep_their_aliases():
    assert parse_entry().sources == [
        TableRef(MASTER, "M"),
        TableRef(TARGET, "T"),
        TableRef(CONFIG, "C"),
    ]


def test_entry_query_joins():
    assert parse_entry().joins == [
        JoinEquality(ColumnRef(MASTER, "ContactId"), ColumnRef(TARGET, "SalesforceId")),
        JoinEquality(ColumnRef(TARGET, "Description"), ColumnRef(CONFIG, "CampaignName")),
    ]


def test_entry_query_filters():
    # Three conditions in the WHERE; the parenthesised date range gives two bounds.
    # START_DATE and END_DATE are unqualified: only the Config table has them.
    # `active` keeps the spelling of the SQL (the schema says ACTIVE).
    assert parse_entry().filters == [
        EqualityFilter(ColumnRef(CONFIG, "active"), "True"),
        DateBoundFilter(ColumnRef(CONFIG, "START_DATE"), "start"),
        DateBoundFilter(ColumnRef(CONFIG, "END_DATE"), "end"),
        EqualityFilter(ColumnRef(CONFIG, "HAS_RUN"), "False"),
    ]


def test_entry_query_plain_columns():
    expected = [
        SelectItem(column, SelectColumn(ColumnRef(table, column)))
        for table, column in ENTRY_PLAIN_COLUMNS
    ]
    assert len(expected) == 32
    assert parse_entry().select[:32] == expected


def test_entry_query_action_id_is_a_concat():
    parsed = parse_entry()
    assert len(parsed.select) == 33
    assert parsed.select[32] == SelectItem(
        "ActionId",
        SelectConcat((
            SelectColumn(ColumnRef(MASTER, "ContactId")),
            SelectLiteral("_"),
            SelectColumn(ColumnRef(CONFIG, "CampaignName")),
            SelectLiteral("_"),
            SelectCurrentDate("yyyyMMdd_HHmm"),
        )),
    )


def test_entry_query_is_fully_understood():
    parsed = parse_entry()
    assert parsed.name == "Q_SKYDE_AdHoc_Target"
    assert parsed.target_table == ENTRY
    assert parsed.select_all is False
    assert parsed.warnings == []


# ---------------------------------------------------------------------------
# Pilot: the three other queries
# ---------------------------------------------------------------------------

def test_config_update_query():
    parsed = parse(CONFIG_UPDATE_SQL, name="Q_SKYDE_NCRM_AD_HOC_CONFIGURATION_UPDATE", target=CONFIG)
    assert parsed.sources == [TableRef(CONFIG, "a"), TableRef(ENTRY, "b")]
    assert parsed.joins == [
        JoinEquality(ColumnRef(CONFIG, "CampaignName"), ColumnRef(ENTRY, "CampaignName")),
    ]
    # RUN_TIME is unqualified; of the two sources only the Config table has it.
    assert parsed.filters == [IsNullFilter(ColumnRef(CONFIG, "RUN_TIME"), negated=False)]
    assert parsed.select == [
        SelectItem("CampaignName", SelectColumn(ColumnRef(CONFIG, "CampaignName"))),
        SelectItem("Channel1", SelectColumn(ColumnRef(CONFIG, "Channel1"))),
        SelectItem("START_DATE", SelectColumn(ColumnRef(CONFIG, "START_DATE"))),
        SelectItem("END_DATE", SelectColumn(ColumnRef(CONFIG, "END_DATE"))),
        SelectItem("HAS_RUN", SelectLiteral("True")),
        SelectItem("ACTIVE", SelectLiteral("False")),
        # GETUTCDATE() AT TIME ZONE 'UTC' AT TIME ZONE 'Central European Standard Time'
        SelectItem("RUN_TIME", SelectCurrentDate(None)),
    ]
    assert parsed.select_all is False
    assert parsed.warnings == []


def test_backup_query_is_select_star():
    parsed = parse(BACKUP_SQL, name="Q_SkyDE_AdHoc_Backup", target="DE_NCRM_AdHoc_Entry_Backup")
    assert parsed.select_all is True
    assert parsed.select == []
    assert parsed.sources == [TableRef(ENTRY, None)]
    assert parsed.joins == []
    assert parsed.filters == []
    assert parsed.warnings == []


def test_check_active_campaigns_query():
    parsed = parse(CHECK_ACTIVE_SQL, name="Q_Check_Active_campaigns", target="DE_Check_Active_Campaigns")
    assert parsed.sources == [TableRef(CONFIG, None)]
    assert parsed.joins == []
    assert parsed.filters == [EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True")]
    assert parsed.select == [
        SelectItem("CampaignName", SelectColumn(ColumnRef(CONFIG, "CampaignName"))),
        SelectItem("Active", SelectLiteral("True")),
    ]
    assert parsed.warnings == []


# ---------------------------------------------------------------------------
# Step 3: an entry query shaped like those of the retention exports
# (invented tables and columns, the same constructs: renamed and swapped columns, a CASE on a LEFT JOIN
# table, a JOIN and a LEFT JOIN, an equality and a date-offset filter, comments in the WHERE clause)
# ---------------------------------------------------------------------------

RET_PROFILE = "Customer_Profile_DE"
RET_MEMBER = "CampaignMember_Sync_3"
RET_BACKUP = "Entry_Retention_Test_Backup"
RET_ENTRY = "Entry_Retention_Test"

RET_PROFILE_FIELDS = [
    ("ContactId", "Text", 250, True, False, None, 0),
    ("Emailaddress", "EmailAddress", 254, False, True, None, 1),
    ("FirstName", "Text", 50, False, True, None, 2),
    ("Phone", "Phone", 50, False, True, None, 3),
    ("MobilePhone", "Phone", 50, False, True, None, 4),
    ("City", "Text", 50, False, True, None, 5),
    ("Contract_EndDate", "Date", None, False, True, None, 6),
    ("Request_SubmittedDate", "Date", None, False, True, None, 7),
]
RET_MEMBER_FIELDS = [
    ("Id", "Text", 18, True, False, None, 0),
    ("ContactId", "Text", 18, False, True, None, 1),
    ("Campaign_Label__c", "Text", 255, False, True, None, 2),
    ("Offer_Code__c", "Text", 50, False, True, None, 3),
    ("CreatedDate", "Date", None, False, True, None, 4),
]
RET_BACKUP_FIELDS = [
    ("ContactId", "Text", 250, True, False, None, 0),
    ("Letter_Timestamp", "Date", None, False, True, None, 1),
    ("Call_Timestamp", "Date", None, False, True, None, 2),
]

RET_ENTRY_SQL = (
    "SELECT\n"
    "M.ContactId,\n"
    "M.Emailaddress,\n"
    "M.FirstName,\n"
    "M.Phone as MobilePhone,\n"
    "M.MobilePhone as Phone,\n"
    "M.City,\n"
    "M.Contract_EndDate,\n"
    "CM.Campaign_Label__c as CampaignName,\n"
    "CM.Offer_Code__c as OfferCode,\n"
    "CM.Id AS CMId,\n"
    "B.Letter_Timestamp,\n"
    "CASE WHEN B.Letter_Timestamp IS NOT NULL\n"
    "      AND CAST(B.Letter_Timestamp AS DATE) BETWEEN\n"
    "          CAST(DATEADD(day, -60, M.Contract_EndDate) AS DATE)\n"
    "          AND CAST(DATEADD(day, -10, M.Contract_EndDate) AS DATE)\n"
    "      AND B.Letter_Timestamp >= M.Request_SubmittedDate\n"
    "     THEN 'True' ELSE 'False' END AS Letter_Identifier,\n"
    "B.Call_Timestamp\n"
    "\n"
    f"FROM [{RET_PROFILE}] M\n"
    f"JOIN [{RET_MEMBER}] CM on CM.ContactId = M.ContactId\n"
    f"LEFT JOIN [{RET_BACKUP}] B on CM.ContactId = B.ContactId\n"
    "WHERE  \n"
    "       CM.Campaign_Label__c = 'RETENTION_TEST_CAMPAIGN'         \n"
    "/*and cast(CM.CreatedDate as Date) = cast(dateadd(dd,-1,getdate()) as Date)*/\n"
    "and {date_filter}\n"
    "/*and cast(CM.CreatedDate as Date) = cast(getdate() as Date)*/"
)

RET_CASE_TEXT = (
    "CASE WHEN NOT B.Letter_Timestamp IS NULL AND CAST(B.Letter_Timestamp AS DATE) BETWEEN "
    "CAST(DATEADD(DAY, -60, M.Contract_EndDate) AS DATE) AND CAST(DATEADD(DAY, -10, M.Contract_EndDate) AS DATE) "
    "AND B.Letter_Timestamp >= M.Request_SubmittedDate THEN 'True' ELSE 'False' END"
)


def retention_tables() -> dict[str, Table]:
    tables = [
        make_table(RET_PROFILE, RET_PROFILE_FIELDS, "ContactId"),
        make_table(RET_MEMBER, RET_MEMBER_FIELDS),
        make_table(RET_BACKUP, RET_BACKUP_FIELDS, "ContactId"),
    ]
    return {table.name: table for table in tables}


@pytest.mark.parametrize("date_filter, days", [
    ("cast(CM.CreatedDate as Date) = cast(dateadd(dd,-1,getdate()) as Date)", -1),
    ("cast(CM.CreatedDate as Date) = cast(getdate() as Date)", 0),
    ("cast(CM.CreatedDate as Date) = dateadd(day,-2,cast(getdate() as Date))", -2),
])
def test_retention_entry_query_is_read_in_full(date_filter, days):
    parsed = parse(RET_ENTRY_SQL.format(date_filter=date_filter), name="Q_RETENTION_ENTRY", target=RET_ENTRY,
                   tables=retention_tables())
    assert parsed.name == "Q_RETENTION_ENTRY"
    assert parsed.target_table == RET_ENTRY
    assert parsed.select_all is False
    assert parsed.sources == [
        TableRef(RET_PROFILE, "M", optional=False),
        TableRef(RET_MEMBER, "CM", optional=False),
        TableRef(RET_BACKUP, "B", optional=True),
    ]
    assert parsed.joins == [
        JoinEquality(ColumnRef(RET_MEMBER, "ContactId"), ColumnRef(RET_PROFILE, "ContactId")),
        JoinEquality(ColumnRef(RET_MEMBER, "ContactId"), ColumnRef(RET_BACKUP, "ContactId")),
    ]
    assert parsed.filters == [
        EqualityFilter(ColumnRef(RET_MEMBER, "Campaign_Label__c"), "RETENTION_TEST_CAMPAIGN"),
        DateOffsetFilter(ColumnRef(RET_MEMBER, "CreatedDate"), days),
    ]
    assert parsed.select == [
        SelectItem("ContactId", SelectColumn(ColumnRef(RET_PROFILE, "ContactId"))),
        SelectItem("Emailaddress", SelectColumn(ColumnRef(RET_PROFILE, "Emailaddress"))),
        SelectItem("FirstName", SelectColumn(ColumnRef(RET_PROFILE, "FirstName"))),
        SelectItem("MobilePhone", SelectColumn(ColumnRef(RET_PROFILE, "Phone"))),
        SelectItem("Phone", SelectColumn(ColumnRef(RET_PROFILE, "MobilePhone"))),
        SelectItem("City", SelectColumn(ColumnRef(RET_PROFILE, "City"))),
        SelectItem("Contract_EndDate", SelectColumn(ColumnRef(RET_PROFILE, "Contract_EndDate"))),
        SelectItem("CampaignName", SelectColumn(ColumnRef(RET_MEMBER, "Campaign_Label__c"))),
        SelectItem("OfferCode", SelectColumn(ColumnRef(RET_MEMBER, "Offer_Code__c"))),
        SelectItem("CMId", SelectColumn(ColumnRef(RET_MEMBER, "Id"))),
        SelectItem("Letter_Timestamp", SelectColumn(ColumnRef(RET_BACKUP, "Letter_Timestamp"))),
        SelectItem("Letter_Identifier", SelectUnsupported(RET_CASE_TEXT)),
        SelectItem("Call_Timestamp", SelectColumn(ColumnRef(RET_BACKUP, "Call_Timestamp"))),
    ]
    assert parsed.warnings == [
        f"Query Q_RETENTION_ENTRY: la tabella {RET_BACKUP} è unita con un LEFT JOIN; viene letta come inner join",
        f"Query Q_RETENTION_ENTRY: colonna Letter_Identifier: espressione non compresa: {RET_CASE_TEXT}",
        "Query Q_RETENTION_ENTRY: la query copia Phone in MobilePhone e MobilePhone in Phone: "
        "verifica che lo scambio sia voluto",
    ]


def test_retention_backup_query_is_select_star():
    tables = retention_tables()
    tables[RET_ENTRY] = make_table(RET_ENTRY, [("ContactId", "Text", 250, True, False, None, 0)], "ContactId")
    parsed = parse(f"SELECT * \nFROM [{RET_ENTRY}]", name="Q_RETENTION_BACKUP", target=RET_BACKUP, tables=tables)
    assert parsed.select_all is True
    assert parsed.sources == [TableRef(RET_ENTRY, None)]
    assert (parsed.select, parsed.joins, parsed.filters, parsed.warnings) == ([], [], [], [])


# ---------------------------------------------------------------------------
# WHERE filters
# ---------------------------------------------------------------------------

def test_or_group_becomes_unsupported_filter():
    parsed = parse(
        f"SELECT M.ContactId {FROM_PILOT} "
        f"WHERE C.ACTIVE = 'True' AND (C.CHANNEL1 = 'Email' OR C.CHANNEL1 = 'SMS')"
    )
    assert parsed.filters == [
        EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True"),
        UnsupportedFilter("C.CHANNEL1 = 'Email' OR C.CHANNEL1 = 'SMS'"),
    ]
    assert warned(parsed, "filtro non compreso", "C.CHANNEL1 = 'Email' OR C.CHANNEL1 = 'SMS'")


def test_top_level_or_is_one_unsupported_filter():
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE C.ACTIVE = 'True' OR C.HAS_RUN = 'False'")
    assert parsed.filters == [UnsupportedFilter("C.ACTIVE = 'True' OR C.HAS_RUN = 'False'")]


def test_parentheses_around_and_groups_are_flattened():
    parsed = parse(
        f"SELECT C.CampaignName {FROM_CONFIG} "
        f"WHERE ((C.ACTIVE = 'True') AND (C.HAS_RUN = 'False' AND (C.CHANNEL1 = 'Email')))"
    )
    assert parsed.filters == [
        EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True"),
        EqualityFilter(ColumnRef(CONFIG, "HAS_RUN"), "False"),
        EqualityFilter(ColumnRef(CONFIG, "CHANNEL1"), "Email"),
    ]
    assert parsed.warnings == []


def test_equality_literal_on_either_side_and_literal_kinds():
    parsed = parse(
        f"SELECT C.CampaignName {FROM_CONFIG} "
        f"WHERE 'True' = C.ACTIVE AND C.CHANNEL1 = N'Email' AND C.CHANNEL2 = 'O''Brien' "
        f"AND C.CHANNEL3 = 5 AND -1 = C.CHANNEL4"
    )
    assert parsed.filters == [
        EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True"),
        EqualityFilter(ColumnRef(CONFIG, "CHANNEL1"), "Email"),
        EqualityFilter(ColumnRef(CONFIG, "CHANNEL2"), "O'Brien"),
        EqualityFilter(ColumnRef(CONFIG, "CHANNEL3"), "5"),
        EqualityFilter(ColumnRef(CONFIG, "CHANNEL4"), "-1"),
    ]


def test_is_null_and_is_not_null():
    parsed = parse(
        f"SELECT C.CampaignName {FROM_CONFIG} "
        f"WHERE C.CHANNEL2 IS NULL AND C.CHANNEL3 IS NOT NULL AND NOT C.CHANNEL4 IS NULL"
    )
    assert parsed.filters == [
        IsNullFilter(ColumnRef(CONFIG, "CHANNEL2"), negated=False),
        IsNullFilter(ColumnRef(CONFIG, "CHANNEL3"), negated=True),
        IsNullFilter(ColumnRef(CONFIG, "CHANNEL4"), negated=True),
    ]


def test_getdate_range_with_the_column_on_the_left():
    parsed = parse(f"SELECT M.ContactId {FROM_PILOT} WHERE START_DATE <= GETDATE() AND END_DATE >= GETDATE()")
    assert parsed.filters == [
        DateBoundFilter(ColumnRef(CONFIG, "START_DATE"), "start"),
        DateBoundFilter(ColumnRef(CONFIG, "END_DATE"), "end"),
    ]
    assert parsed.warnings == []


@pytest.mark.parametrize("condition, column, bound", [
    ("GETDATE() >= START_DATE", "START_DATE", "start"),
    ("GETDATE() > START_DATE", "START_DATE", "start"),
    ("START_DATE < SYSDATETIME()", "START_DATE", "start"),
    ("CAST(GETDATE() AS DATE) <= CAST(END_DATE AS DATE)", "END_DATE", "end"),
    ("END_DATE > GETUTCDATE()", "END_DATE", "end"),
    ("CONVERT(DATE, GETUTCDATE()) >= CONVERT(DATE, C.START_DATE)", "START_DATE", "start"),
    ("GETUTCDATE() AT TIME ZONE 'UTC' <= C.END_DATE", "END_DATE", "end"),
    ("CAST(GETUTCDATE() AT TIME ZONE 'UTC' AS DATE) >= C.START_DATE", "START_DATE", "start"),
    ("(GETDATE()) >= (C.START_DATE)", "START_DATE", "start"),
    ("CAST(GETDATE() AS DATETIME) <= END_DATE", "END_DATE", "end"),
    ("CURRENT_TIMESTAMP <= END_DATE", "END_DATE", "end"),
])
def test_date_bound_forms(condition, column, bound):
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE {condition}")
    assert parsed.filters == [DateBoundFilter(ColumnRef(CONFIG, column), bound)]
    assert parsed.warnings == []


def test_current_date_between_two_columns_gives_both_bounds():
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE GETDATE() BETWEEN C.START_DATE AND C.END_DATE")
    assert parsed.filters == [
        DateBoundFilter(ColumnRef(CONFIG, "START_DATE"), "start"),
        DateBoundFilter(ColumnRef(CONFIG, "END_DATE"), "end"),
    ]


@pytest.mark.parametrize("condition", [
    "C.START_DATE >= '2026-01-01'",
    "DATEADD(day, -1, GETDATE()) >= C.START_DATE",
    "GETDATE() = C.START_DATE",
    "C.START_DATE <= C.END_DATE",
    "CAST(GETDATE() AS VARCHAR(10)) >= C.START_DATE",
    "C.START_DATE BETWEEN '2026-01-01' AND '2026-12-31'",
    "C.CHANNEL1 <> 'Email'",
    "C.CHANNEL1 IN ('Email', 'SMS')",
    "C.CHANNEL1 LIKE 'E%'",
    "NOT C.CHANNEL1 = 'Email'",
    "UPPER(C.CHANNEL1) = 'EMAIL'",
    "C.CHANNEL1 = C.CHANNEL2",
    "C.CHANNEL1 = NULL",
    "1 = 1",
    "EXISTS (SELECT 1 FROM [DE_Check_Active_Campaigns])",
])
def test_other_conditions_are_unsupported_filters(condition):
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE C.ACTIVE = 'True' AND {condition}")
    assert parsed.filters[0] == EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True")
    assert len(parsed.filters) == 2
    assert isinstance(parsed.filters[1], UnsupportedFilter)
    assert warned(parsed, "filtro non compreso", parsed.filters[1].text)


def test_unsupported_filter_text_leaves_comments_out():
    parsed = parse(
        f"SELECT C.CampaignName {FROM_CONFIG} "
        f"WHERE /* note */ (C.ACTIVE = 'True' /* x */ OR C.HAS_RUN = 'False')"
    )
    assert parsed.filters == [UnsupportedFilter("C.ACTIVE = 'True' OR C.HAS_RUN = 'False'")]


# ---------------------------------------------------------------------------
# WHERE filters: the date part of a column against the current date moved by days (spec, step 3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("condition, days", [
    # The three spellings of the spec and of the retention exports.
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)", -1),
    ("cast(C.START_DATE as Date) = cast(dateadd(dd,-1,getdate()) as Date)", -1),
    ("CAST(C.START_DATE AS DATE) = CAST(GETDATE() AS DATE)", 0),
    ("cast(C.START_DATE as Date) = cast(getdate() as Date)", 0),
    ("CAST(C.START_DATE AS DATE) = DATEADD(day, -2, CAST(GETDATE() AS DATE))", -2),
    ("cast(C.START_DATE as Date) = dateadd(day,-2,cast(getdate() as Date))", -2),
    # Either side, the other day units and the other current-date calls.
    ("CAST(DATEADD(d, -3, GETDATE()) AS DATE) = CAST(C.START_DATE AS DATE)", -3),
    ("DATEADD(DD, 2, CAST(GETDATE() AS DATE)) = CAST(START_DATE AS DATE)", 2),
    ("CAST(C.START_DATE AS DATE) = CAST(GETUTCDATE() AS DATE)", 0),
    ("CAST(C.START_DATE AS DATE) = CAST(SYSDATETIME() AS DATE)", 0),
    ("CAST(C.START_DATE AS DATE) = CAST(CURRENT_TIMESTAMP AS DATE)", 0),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(Day, +1, GETDATE()) AS DATE)", 1),
    # CONVERT(DATE, ...) and TRY_CAST, as the date bounds accept them.
    ("CONVERT(DATE, C.START_DATE) = CONVERT(DATE, DATEADD(day, -1, GETDATE()))", -1),
    ("CONVERT(DATE, C.START_DATE, 104) = DATEADD(day, 5, CONVERT(DATE, GETUTCDATE()))", 5),
    ("TRY_CAST(C.START_DATE AS DATE) = CAST(GETDATE() AS DATE)", 0),
    # Parentheses and AT TIME ZONE pass through, as for the date bounds.
    ("(CAST(C.START_DATE AS DATE)) = (CAST(GETUTCDATE() AT TIME ZONE 'UTC' AS DATE))", 0),
    # Other units and amounts whose meaning is still a whole number of days.
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(week, -1, GETDATE()) AS DATE)", -7),
    ("CAST(C.START_DATE AS DATE) = DATEADD(wk, 2, CAST(GETDATE() AS DATE))", 14),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(dayofyear, -1, GETDATE()) AS DATE)", -1),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(dw, 1, GETDATE()) AS DATE)", 1),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(hour, -48, GETDATE()) AS DATE)", -2),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(minute, 1440, GETDATE()) AS DATE)", 1),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(hh, 12, DATEADD(hh, 12, GETDATE())) AS DATE)", 1),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, (-1) * 7, GETDATE()) AS DATE)", -7),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, -(2), GETDATE()) AS DATE)", -2),
    ("CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, 10 - 11, GETDATE()) AS DATE)", -1),
    ("CAST(C.START_DATE AS DATE) = DATEADD(day, -1, DATEADD(day, -1, CAST(GETDATE() AS DATE)))", -2),
    # A date part made a DATETIME again is still midnight of that day.
    ("CAST(C.START_DATE AS DATE) = CAST(CAST(GETDATE() AS DATE) AS DATETIME)", 0),
    # The column moved by days instead of the current date.
    ("DATEADD(day, 1, CAST(C.START_DATE AS DATE)) = CAST(GETDATE() AS DATE)", -1),
    ("CAST(DATEADD(day, 1, C.START_DATE) AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)", -2),
])
def test_date_offset_forms(condition, days):
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE C.ACTIVE = 'True' AND {condition}")
    assert parsed.filters == [
        EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True"),
        DateOffsetFilter(ColumnRef(CONFIG, "START_DATE"), days),
    ]
    assert parsed.warnings == []


@pytest.mark.parametrize("condition", [
    # Not an equality: an inequality against a moved date stays unsupported.
    "CAST(C.START_DATE AS DATE) >= CAST(DATEADD(day, -1, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) < DATEADD(day, -2, CAST(GETDATE() AS DATE))",
    "CAST(C.START_DATE AS DATE) <> CAST(GETDATE() AS DATE)",
    "CAST(C.START_DATE AS DATE) BETWEEN CAST(DATEADD(day, -7, GETDATE()) AS DATE) AND CAST(GETDATE() AS DATE)",
    # One side keeps its time: T-SQL compares the full date and time, true only at midnight.
    "C.START_DATE = CAST(GETDATE() AS DATE)",
    "C.START_DATE = CAST(DATEADD(day, -1, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = DATEADD(day, -1, GETDATE())",
    "CAST(C.START_DATE AS DATE) = GETDATE()",
    "CAST(C.START_DATE AS DATETIME) = CAST(GETDATE() AS DATE)",
    "C.START_DATE = DATEADD(day, -1, GETDATE())",
    # A move that is not a whole number of days, or whose length depends on the date.
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(hour, -12, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(month, -1, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(yy, -1, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(quarter, 1, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(ms, 86400000, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = DATEADD(hour, 24, CAST(GETDATE() AS DATE))",   # T-SQL refuses hours on a DATE
    "CAST(DATEADD(hour, 1, C.START_DATE) AS DATE) = CAST(GETDATE() AS DATE)",
    # An amount that is not a whole-number constant.
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, C.CHANNEL1, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, 1.5, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, '-1', GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, 2147483648, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(DATEADD(day, 4000000, GETDATE()) AS DATE)",
    # Not a column against the current date.
    "CAST(C.START_DATE AS DATE) = CAST(C.END_DATE AS DATE)",
    "CAST(GETDATE() AS DATE) = CAST(DATEADD(day, 0, GETDATE()) AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST('2026-10-14' AS DATE)",
    "CAST(C.START_DATE AS VARCHAR(10)) = CAST(GETDATE() AS VARCHAR(10))",
    "CAST(UPPER(C.START_DATE) AS DATE) = CAST(GETDATE() AS DATE)",
    "CAST(C.START_DATE AS DATE) = CAST(GETDATE() - 1 AS DATE)",
])
def test_other_comparisons_with_a_moved_date_are_unsupported(condition):
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE C.ACTIVE = 'True' AND {condition}")
    assert parsed.filters[0] == EqualityFilter(ColumnRef(CONFIG, "ACTIVE"), "True")
    assert len(parsed.filters) == 2
    assert isinstance(parsed.filters[1], UnsupportedFilter)
    assert warned(parsed, "filtro non compreso", parsed.filters[1].text)


def test_date_offset_on_an_unresolved_column_keeps_the_filter():
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE CAST(CreatedDate AS DATE) = CAST(GETDATE() AS DATE)")
    assert parsed.filters == [DateOffsetFilter(ColumnRef("", "CreatedDate"), 0)]
    assert warned(parsed, "colonna CreatedDate", "nessuna tabella nota")


# ---------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------

def test_non_equality_join_condition_gives_a_warning():
    parsed = parse(
        f"SELECT M.ContactId FROM [{MASTER}] M "
        f"JOIN [{TARGET}] T ON M.ContactId = T.SalesforceId "
        f"AND T.LastSyncedAt > M.Entitle_CommitmentStartDate"
    )
    assert parsed.joins == [JoinEquality(ColumnRef(MASTER, "ContactId"), ColumnRef(TARGET, "SalesforceId"))]
    assert warned(parsed, "condizione di join non compresa", "T.LastSyncedAt > M.Entitle_CommitmentStartDate")


@pytest.mark.parametrize("on", [
    "M.ContactId = T.SalesforceId OR M.AccountId = T.SalesforceId",
    "M.ContactId <> T.SalesforceId",
    "UPPER(M.ContactId) = T.SalesforceId",
])
def test_join_conditions_that_are_not_equalities_are_left_out(on):
    parsed = parse(f"SELECT M.ContactId FROM [{MASTER}] M JOIN [{TARGET}] T ON {on}")
    assert parsed.sources == [TableRef(MASTER, "M"), TableRef(TARGET, "T")]
    assert parsed.joins == []
    assert warned(parsed, "condizione di join non compresa")


def test_join_condition_with_a_literal_is_not_a_join():
    parsed = parse(
        f"SELECT M.ContactId FROM [{MASTER}] M "
        f"JOIN [{TARGET}] T ON (M.ContactId = T.SalesforceId) AND T.Description = 'Campaign A'"
    )
    assert parsed.joins == [JoinEquality(ColumnRef(MASTER, "ContactId"), ColumnRef(TARGET, "SalesforceId"))]
    assert warned(parsed, "condizione di join non compresa", "T.Description = 'Campaign A'")


def test_left_join_is_read_as_an_inner_join_with_a_warning():
    parsed = parse(
        f"SELECT M.ContactId FROM [{MASTER}] M LEFT JOIN [{TARGET}] T ON M.ContactId = T.SalesforceId"
    )
    assert parsed.joins == [JoinEquality(ColumnRef(MASTER, "ContactId"), ColumnRef(TARGET, "SalesforceId"))]
    # Spec, step 3: "A LEFT JOIN source still gets one row per contact (read as an inner join, as before)".
    assert parsed.sources == [TableRef(MASTER, "M"), TableRef(TARGET, "T", optional=True)]
    assert parsed.warnings == [
        f"Query Q_TEST: la tabella {TARGET} è unita con un LEFT JOIN; viene letta come inner join",
    ]


# Each join of a three-table chain: which sources may lack their row (optional), in FROM/JOIN order.
@pytest.mark.parametrize("join, optional", [
    ("JOIN", [False, False, False]),
    ("INNER JOIN", [False, False, False]),
    ("LEFT JOIN", [False, False, True]),
    ("LEFT OUTER JOIN", [False, False, True]),
    ("RIGHT JOIN", [True, True, False]),        # the tables before a RIGHT JOIN may lack their row
    ("RIGHT OUTER JOIN", [True, True, False]),
    ("FULL OUTER JOIN", [True, True, True]),
    ("FULL JOIN", [True, True, True]),
])
def test_outer_joins_make_sources_optional(join, optional):
    parsed = parse(
        f"SELECT M.ContactId FROM [{MASTER}] M "
        f"JOIN [{TARGET}] T ON M.ContactId = T.SalesforceId "
        f"{join} [{CONFIG}] C ON T.Description = C.CampaignName"
    )
    assert [source.table for source in parsed.sources] == [MASTER, TARGET, CONFIG]
    assert [source.optional for source in parsed.sources] == optional
    assert len(parsed.joins) == 2   # still read as equalities, whatever the side
    if join.split()[0] in ("LEFT", "RIGHT", "FULL"):
        assert parsed.warnings == [
            f"Query Q_TEST: la tabella {CONFIG} è unita con un {join.split()[0]} JOIN; viene letta come inner join",
        ]
    else:
        assert parsed.warnings == []


def test_left_join_after_a_right_join_keeps_every_optional_flag():
    parsed = parse(
        f"SELECT M.ContactId FROM [{MASTER}] M "
        f"RIGHT JOIN [{TARGET}] T ON M.ContactId = T.SalesforceId "
        f"LEFT JOIN [{CONFIG}] C ON T.Description = C.CampaignName"
    )
    assert parsed.sources == [
        TableRef(MASTER, "M", optional=True),
        TableRef(TARGET, "T", optional=False),
        TableRef(CONFIG, "C", optional=True),
    ]
    assert all("letta come inner join" in warning for warning in parsed.warnings)
    assert len(parsed.warnings) == 2


@pytest.mark.parametrize("from_clause", [
    f"FROM [{MASTER}] M, [{TARGET}] T",
    f"FROM [{MASTER}] M CROSS JOIN [{TARGET}] T",
])
def test_join_without_on_condition_is_reported(from_clause):
    parsed = parse(f"SELECT M.ContactId {from_clause}")
    assert parsed.sources == [TableRef(MASTER, "M"), TableRef(TARGET, "T")]
    assert parsed.joins == []
    assert warned(parsed, TARGET, "senza una condizione ON")


def test_join_on_a_column_of_unknown_table_is_left_out():
    parsed = parse(f"SELECT M.ContactId FROM [{MASTER}] M JOIN [{TARGET}] T ON M.ContactId = X.SalesforceId")
    assert parsed.joins == []
    assert warned(parsed, "X.SalesforceId", "non è una tabella né un alias")
    assert warned(parsed, "condizione di join ignorata", "M.ContactId = X.SalesforceId")


# ---------------------------------------------------------------------------
# Column resolution
# ---------------------------------------------------------------------------

def test_unresolvable_column_gives_empty_table_and_a_warning():
    parsed = parse(f"SELECT M.ContactId, NoSuchField {FROM_MASTER}")
    assert parsed.select[1] == SelectItem("NoSuchField", SelectColumn(ColumnRef("", "NoSuchField")))
    assert warned(parsed, "colonna NoSuchField", "nessuna tabella nota del FROM/JOIN")


def test_column_found_in_several_tables_is_not_resolved():
    parsed = parse(
        f"SELECT CampaignName FROM [{CONFIG}] a JOIN [{ENTRY}] b ON a.CampaignName = b.CampaignName"
    )
    assert parsed.select == [SelectItem("CampaignName", SelectColumn(ColumnRef("", "CampaignName")))]
    assert warned(parsed, "colonna CampaignName", "più tabelle", CONFIG, ENTRY)


def test_unknown_qualifier_gives_empty_table_and_a_warning():
    parsed = parse(f"SELECT X.ContactId {FROM_MASTER}")
    assert parsed.select == [SelectItem("ContactId", SelectColumn(ColumnRef("", "ContactId")))]
    assert warned(parsed, "X.ContactId", "non è una tabella né un alias")


def test_bare_table_name_as_qualifier():
    parsed = parse(f"SELECT [{CONFIG}].CampaignName, {CONFIG}.CHANNEL1 FROM [{CONFIG}]")
    assert parsed.select == [
        SelectItem("CampaignName", SelectColumn(ColumnRef(CONFIG, "CampaignName"))),
        SelectItem("CHANNEL1", SelectColumn(ColumnRef(CONFIG, "CHANNEL1"))),
    ]
    assert parsed.warnings == []


def test_aliases_and_table_names_ignore_letter_case():
    parsed = parse(f"SELECT m.ContactId FROM [{MASTER.lower()}] M WHERE m.emailoptout = 'False'")
    assert parsed.sources == [TableRef(MASTER, "M")]  # the export's spelling of the table
    assert parsed.select == [SelectItem("ContactId", SelectColumn(ColumnRef(MASTER, "ContactId")))]
    assert parsed.filters == [EqualityFilter(ColumnRef(MASTER, "emailoptout"), "False")]
    assert parsed.warnings == []


def test_qualified_column_missing_from_the_schema_is_reported():
    parsed = parse(f"SELECT M.NoSuchField {FROM_MASTER}")
    assert parsed.select == [SelectItem("NoSuchField", SelectColumn(ColumnRef(MASTER, "NoSuchField")))]
    assert warned(parsed, f"la tabella {MASTER} non ha un campo NoSuchField")


def test_shared_table_with_ent_prefix():
    tables = pilot_tables()
    tables["Shared_DE"] = make_table("Shared_DE", [
        ("SubscriberKey", "Text", 254, True, False, None, 0),
        ("Flag", "Boolean", None, False, True, None, 1),
    ])
    parsed = parse(
        f"SELECT S.SubscriberKey, Flag FROM ENT.[Shared_DE] S "
        f"JOIN [{MASTER}] M ON M.ContactId = S.SubscriberKey WHERE S.Flag = 'True'",
        tables=tables,
    )
    assert parsed.sources == [TableRef("Shared_DE", "S"), TableRef(MASTER, "M")]
    assert parsed.joins == [
        JoinEquality(ColumnRef(MASTER, "ContactId"), ColumnRef("Shared_DE", "SubscriberKey")),
    ]
    assert parsed.filters == [EqualityFilter(ColumnRef("Shared_DE", "Flag"), "True")]
    assert parsed.select == [
        SelectItem("SubscriberKey", SelectColumn(ColumnRef("Shared_DE", "SubscriberKey"))),
        SelectItem("Flag", SelectColumn(ColumnRef("Shared_DE", "Flag"))),
    ]
    assert parsed.warnings == []


def test_shared_table_named_in_full_as_qualifier():
    tables = pilot_tables()
    tables["Shared_DE"] = make_table("Shared_DE", [("SubscriberKey", "Text", 254, True, False, None, 0)])
    parsed = parse("SELECT ENT.[Shared_DE].SubscriberKey FROM ENT.[Shared_DE]", tables=tables)
    assert parsed.sources == [TableRef("Shared_DE", None)]
    assert parsed.select == [
        SelectItem("SubscriberKey", SelectColumn(ColumnRef("Shared_DE", "SubscriberKey"))),
    ]
    assert parsed.warnings == []


def test_other_table_prefix_is_ignored_with_a_warning():
    parsed = parse(f"SELECT M.ContactId FROM dbo.[{MASTER}] M")
    assert parsed.sources == [TableRef(MASTER, "M")]
    assert warned(parsed, "prefisso", MASTER)


def test_table_missing_from_the_export_is_reported():
    parsed = parse("SELECT s.SubscriberKey, EmailAddress FROM [_Subscribers] s")
    assert parsed.sources == [TableRef("_Subscribers", "s")]
    assert parsed.select == [
        SelectItem("SubscriberKey", SelectColumn(ColumnRef("_Subscribers", "SubscriberKey"))),
        SelectItem("EmailAddress", SelectColumn(ColumnRef("", "EmailAddress"))),
    ]
    assert warned(parsed, "la tabella _Subscribers non è nell'export")
    assert warned(parsed, "colonna EmailAddress")


def test_warnings_name_the_query_and_are_not_repeated():
    parsed = parse(f"SELECT NoSuchField, NoSuchField AS Again {FROM_MASTER} WHERE NoSuchField = 'x'")
    assert parsed.warnings == [
        "Query Q_TEST: colonna NoSuchField: nessuna tabella nota del FROM/JOIN la contiene, "
        "quindi la sua tabella è sconosciuta",
    ]
    assert parsed.filters == [EqualityFilter(ColumnRef("", "NoSuchField"), "x")]


# ---------------------------------------------------------------------------
# Select list
# ---------------------------------------------------------------------------

def test_functions_in_select_are_unsupported():
    parsed = parse(
        f"SELECT UPPER(M.FirstName) AS FirstName, ISNULL(M.City, 'n/a') AS City, "
        f"M.FirstName + ' ' + M.LastName AS FullName {FROM_MASTER}"
    )
    assert parsed.select == [
        SelectItem("FirstName", SelectUnsupported("UPPER(M.FirstName)")),
        SelectItem("City", SelectUnsupported("ISNULL(M.City, 'n/a')")),
        SelectItem("FullName", SelectUnsupported("M.FirstName + ' ' + M.LastName")),
    ]
    assert warned(parsed, "colonna FirstName", "espressione non compresa", "UPPER(M.FirstName)")
    assert warned(parsed, "colonna City", "ISNULL(M.City, 'n/a')")
    assert warned(parsed, "colonna FullName", "M.FirstName + ' ' + M.LastName")


def test_select_literals():
    parsed = parse(
        f"SELECT 'True' AS A, 1 AS B, -2 AS C, 1.50 AS D, N'x' AS E, 'O''Brien' AS F, '' AS G {FROM_MASTER}"
    )
    assert parsed.select == [
        SelectItem("A", SelectLiteral("True")),
        SelectItem("B", SelectLiteral("1")),
        SelectItem("C", SelectLiteral("-2")),
        SelectItem("D", SelectLiteral("1.50")),
        SelectItem("E", SelectLiteral("x")),
        SelectItem("F", SelectLiteral("O'Brien")),
        SelectItem("G", SelectLiteral("")),
    ]
    assert parsed.warnings == []


def test_current_date_forms_in_select():
    parsed = parse(
        "SELECT GETDATE() AS A, SYSDATETIME() AS B, GETUTCDATE() AT TIME ZONE 'UTC' AS C, "
        "FORMAT(GETDATE(), 'dd/MM/yyyy') AS D, "
        "FORMAT(GETUTCDATE() AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time', "
        "'yyyy-MM-dd HH:mm') AS E, "
        f"(GETUTCDATE()) AS F {FROM_MASTER}"
    )
    assert parsed.select == [
        SelectItem("A", SelectCurrentDate(None)),
        SelectItem("B", SelectCurrentDate(None)),
        SelectItem("C", SelectCurrentDate(None)),
        SelectItem("D", SelectCurrentDate("dd/MM/yyyy")),
        SelectItem("E", SelectCurrentDate("yyyy-MM-dd HH:mm")),
        SelectItem("F", SelectCurrentDate(None)),
    ]
    assert parsed.warnings == []


def test_format_keeps_the_dotnet_format_as_written():
    # sqlglot alone would turn this into its own strftime form; the module keeps the original.
    parsed = parse(f"SELECT FORMAT(GETUTCDATE(), 'YYYY ''at'' hh:mm tt') AS X {FROM_MASTER}")
    assert parsed.select == [SelectItem("X", SelectCurrentDate("YYYY 'at' hh:mm tt"))]


@pytest.mark.parametrize("expression", [
    "CAST(GETDATE() AS DATE)",
    "FORMAT(GETDATE(), 'yyyy', 'it-IT')",
    "FORMAT(M.Entitle_CommitmentStartDate, 'yyyy')",
    "FORMAT(GETDATE(), M.City)",
    "CONVERT(VARCHAR(8), GETDATE(), 112)",
    "DATEADD(day, 1, GETDATE())",
    "CASE WHEN M.City = 'Rome' THEN 'Y' ELSE 'N' END",
    "(SELECT MAX(C.CampaignName) FROM [DE_NCRM_AD_HOC_CONFIG] C)",
])
def test_other_select_expressions_are_unsupported(expression):
    parsed = parse(f"SELECT {expression} AS X {FROM_MASTER}")
    assert len(parsed.select) == 1
    item = parsed.select[0]
    assert item.target_column == "X"
    assert isinstance(item.expr, SelectUnsupported)
    assert warned(parsed, "colonna X", "espressione non compresa", item.expr.text)


def test_concat_keeps_its_parts_even_when_one_is_unsupported():
    parsed = parse(f"SELECT CONCAT(M.ContactId, '-', UPPER(M.City), CONCAT('a', 1)) AS X {FROM_MASTER}")
    assert parsed.select == [SelectItem("X", SelectConcat((
        SelectColumn(ColumnRef(MASTER, "ContactId")),
        SelectLiteral("-"),
        SelectUnsupported("UPPER(M.City)"),
        SelectConcat((SelectLiteral("a"), SelectLiteral("1"))),
    )))]
    assert warned(parsed, "colonna X", "UPPER(M.City)")


def test_alias_forms():
    parsed = parse(
        f"SELECT ActionId = CONCAT(M.ContactId, '_'), 'x' AS 'Quoted', M.City AS [Mailing CG], "
        f"M.Country Nation, [M].[PostalCode] {FROM_MASTER}"
    )
    assert [item.target_column for item in parsed.select] == [
        "ActionId", "Quoted", "Mailing CG", "Nation", "PostalCode",
    ]
    assert parsed.select[4] == SelectItem("PostalCode", SelectColumn(ColumnRef(MASTER, "PostalCode")))
    assert parsed.warnings == []


def test_columns_swapped_into_each_other_names_give_one_warning():
    parsed = parse(f"SELECT M.ContactId, M.Phone AS MobilePhone, M.City, M.MobilePhone AS Phone {FROM_MASTER}")
    assert parsed.select == [
        SelectItem("ContactId", SelectColumn(ColumnRef(MASTER, "ContactId"))),
        SelectItem("MobilePhone", SelectColumn(ColumnRef(MASTER, "Phone"))),
        SelectItem("City", SelectColumn(ColumnRef(MASTER, "City"))),
        SelectItem("Phone", SelectColumn(ColumnRef(MASTER, "MobilePhone"))),
    ]
    assert parsed.warnings == [
        "Query Q_TEST: la query copia Phone in MobilePhone e MobilePhone in Phone: "
        "verifica che lo scambio sia voluto",
    ]


def test_swap_is_found_ignoring_letter_case_and_named_as_the_first_item_writes_it():
    parsed = parse(f"SELECT M.mobilephone AS PHONE, m.Phone AS MobilePhone {FROM_MASTER}")
    assert parsed.warnings == [
        "Query Q_TEST: la query copia mobilephone in PHONE e PHONE in mobilephone: "
        "verifica che lo scambio sia voluto",
    ]


def test_each_swapped_pair_is_reported_once():
    parsed = parse(
        f"SELECT M.Phone AS MobilePhone, M.MobilePhone AS Phone, M.MobilePhone AS Phone, "
        f"M.City AS Country, M.Country AS City {FROM_MASTER}"
    )
    assert parsed.warnings == [
        "Query Q_TEST: la query copia Phone in MobilePhone e MobilePhone in Phone: verifica che lo scambio sia voluto",
        "Query Q_TEST: la query copia City in Country e Country in City: verifica che lo scambio sia voluto",
    ]


@pytest.mark.parametrize("select", [
    "M.Phone AS MobilePhone",                                   # a rename alone
    "M.Phone AS MobilePhone, M.MobilePhone",                    # the other column keeps its name
    "M.Phone AS MobilePhone, T.MobilePhone AS Phone",           # columns of two tables
    "M.Phone AS Phone, M.MobilePhone AS MobilePhone",           # no rename
    "M.Phone AS MobilePhone, UPPER(M.MobilePhone) AS Phone",    # not a plain column
    "M.Phone AS MobilePhone, X.MobilePhone AS Phone",           # table unknown
])
def test_renames_that_are_not_a_swap_give_no_warning(select):
    tables = pilot_tables()
    tables[TARGET].fields.append(Field("MobilePhone", "Phone", 50, False, True, None, 4))
    parsed = parse(f"SELECT {select} FROM [{MASTER}] M JOIN [{TARGET}] T ON M.ContactId = T.SalesforceId",
                   tables=tables)
    assert not warned(parsed, "scambio")


def test_select_item_without_a_name_is_reported():
    parsed = parse(f"SELECT 'x', GETDATE() {FROM_MASTER}")
    assert parsed.select == [
        SelectItem("", SelectLiteral("x")),
        SelectItem("", SelectCurrentDate(None)),
    ]
    assert warned(parsed, "l'elemento della SELECT 'x' non ha un nome di colonna")


def test_table_star_is_read_as_select_star_with_a_warning():
    parsed = parse(f"SELECT M.* {FROM_MASTER}")
    assert parsed.select_all is True
    assert parsed.select == []
    assert warned(parsed, "M.*", "SELECT *")


def test_star_with_extra_columns():
    parsed = parse(f"SELECT *, 'x' AS Extra {FROM_MASTER}")
    assert parsed.select_all is True
    assert parsed.select == [SelectItem("Extra", SelectLiteral("x"))]
    assert parsed.warnings == []


# ---------------------------------------------------------------------------
# Statements the module does not fully read, and SQL it cannot parse
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT FROM WHERE",
    "SELEC M.ContactId FROM [X] M",
    "SELECT M.ContactId FROM [SF_Master_DataExtension_DE",
    "SELECT (M.ContactId FROM [X] M",
    "SELECT a FROM T WHERE",
    "SELECT * FROM {{mcpm#/entities/dataExtensions/abc/data/name}}",
])
def test_unparseable_sql_gives_empty_result_and_one_warning(sql):
    parsed = parse(sql)
    assert parsed.select_all is False
    assert parsed.sources == []
    assert parsed.joins == []
    assert parsed.filters == []
    assert parsed.select == []
    assert len(parsed.warnings) == 1
    assert parsed.warnings[0].startswith("Query Q_TEST: non è stato possibile analizzare il codice SQL: ")


def test_parse_error_names_the_problem_in_plain_text():
    parsed = parse("SELECT FROM WHERE")
    assert parsed.warnings == [
        "Query Q_TEST: non è stato possibile analizzare il codice SQL: Expected table name but got 'WHERE', "
        "vicino a 'WHERE' alla riga 1, colonna 17"
    ]


@pytest.mark.parametrize("sql", ["", "   \n", "-- only a comment", ";"])
def test_empty_sql_gives_one_warning(sql):
    parsed = parse(sql)
    assert (parsed.sources, parsed.select, parsed.select_all) == ([], [], False)
    assert parsed.warnings == ["Query Q_TEST: il codice SQL è vuoto"]


def test_select_without_columns_is_reported():
    parsed = parse("SELECT")
    assert parsed.select == []
    assert parsed.warnings == ["Query Q_TEST: la SELECT non elenca alcuna colonna"]


def test_statement_that_is_not_a_select_is_not_read():
    parsed = parse(f"UPDATE [{CONFIG}] SET HAS_RUN = 'True'")
    assert parsed.sources == []
    assert parsed.warnings == ["Query Q_TEST: il codice SQL non è una SELECT (istruzione UPDATE); non è stato letto nulla"]


def test_union_reads_the_first_select():
    parsed = parse(f"SELECT M.ContactId {FROM_MASTER} UNION SELECT T.SalesforceId FROM [{TARGET}] T")
    assert parsed.sources == [TableRef(MASTER, "M")]
    assert parsed.select == [SelectItem("ContactId", SelectColumn(ColumnRef(MASTER, "ContactId")))]
    assert warned(parsed, "UNION", "viene letta solo la prima SELECT")


def test_several_statements_read_the_first():
    parsed = parse(f"SELECT M.ContactId {FROM_MASTER}; SELECT C.CampaignName {FROM_CONFIG}")
    assert parsed.sources == [TableRef(MASTER, "M")]
    assert warned(parsed, "2 istruzioni", "viene letta solo la prima")


@pytest.mark.parametrize("sql, part", [
    (f"SELECT TOP 10 M.ContactId {FROM_MASTER}", "TOP"),
    (f"SELECT DISTINCT M.ContactId {FROM_MASTER}", "DISTINCT"),
    (f"SELECT M.City {FROM_MASTER} GROUP BY M.City", "GROUP BY"),
    (f"SELECT M.City {FROM_MASTER} GROUP BY M.City HAVING COUNT(*) > 1", "HAVING"),
    (f"SELECT TOP 1 M.City {FROM_MASTER} ORDER BY M.City", "ORDER BY"),
    (f"WITH X AS (SELECT M.City {FROM_MASTER}) SELECT City FROM X", "WITH"),
])
def test_parts_that_are_not_read_are_reported(sql, part):
    parsed = parse(sql)
    assert warned(parsed, f"la parte {part} della query è ignorata")


def test_subquery_in_from_is_reported():
    parsed = parse(f"SELECT s.ContactId FROM (SELECT M.ContactId {FROM_MASTER}) s")
    assert parsed.sources == []
    assert warned(parsed, "nel FROM/JOIN non è una tabella")
    assert parsed.select == [SelectItem("ContactId", SelectColumn(ColumnRef("", "ContactId")))]


def test_joined_subquery_and_apply_are_reported_once_and_not_joined():
    parsed = parse(
        f"SELECT M.ContactId {FROM_MASTER} "
        f"JOIN (SELECT T.SalesforceId FROM [{TARGET}] T) s ON s.SalesforceId = M.ContactId "
        f"CROSS APPLY OPENJSON(M.City)"
    )
    assert parsed.sources == [TableRef(MASTER, "M")]
    assert parsed.joins == []
    assert len(parsed.warnings) == 2
    assert warned(parsed, "SELECT T.SalesforceId", "non è una tabella e non è stato letto")
    assert warned(parsed, "OPENJSON(M.City)", "non è una tabella e non è stato letto")


def test_pivot_on_a_table_is_reported():
    parsed = parse(f"SELECT C.CampaignName FROM [{CONFIG}] PIVOT (MAX(v) FOR k IN ([x], [y])) p")
    assert parsed.sources == [TableRef(CONFIG, None)]
    assert warned(parsed, f"il PIVOT sulla tabella {CONFIG} è ignorato")


def test_long_and_chain_is_read_in_full():
    conditions = " AND ".join(f"C.CHANNEL{1 + i % 4} = 'v{i}'" for i in range(3000))
    parsed = parse(f"SELECT C.CampaignName {FROM_CONFIG} WHERE {conditions}")
    assert len(parsed.filters) == 3000
    assert parsed.filters[2999] == EqualityFilter(ColumnRef(CONFIG, "CHANNEL4"), "v2999")
    assert parsed.warnings == []


def test_unexpected_error_keeps_what_was_read(monkeypatch):
    # Plan: "Never raise on odd SQL: return what was understood and put the rest in warnings".
    import cjhelper.sqlparse_lite as module

    def broken(*args):
        raise ValueError("boom")

    monkeypatch.setattr(module, "_read_select_list", broken)
    parsed = parse_entry()
    assert len(parsed.sources) == 3
    assert len(parsed.joins) == 2
    assert len(parsed.filters) == 4
    assert parsed.select == []
    assert parsed.warnings == [
        "Query Q_SKYDE_AdHoc_Target: il codice SQL è stato letto solo in parte a causa di un errore imprevisto "
        "(ValueError: boom)"
    ]


@pytest.mark.parametrize("sql", [
    "SELECT",
    "SELECT 1",
    "SELECT * FROM",
    ";",
    "SELECT a FROM T CROSS APPLY OPENJSON(T.j)",
    "SELECT a FROM OPENJSON('[]')",
    "SELECT a FROM T WHERE a = (SELECT MAX(b) FROM U) AND b IN (SELECT c FROM V)",
    "EXEC sp_who",
    "SELECT a FROM T PIVOT (MAX(v) FOR k IN ([x], [y])) p",
    "SELECT a FROM T WITH (NOLOCK) WHERE T.a = 'x'",
    "SELECT " + "(" * 200 + "1" + ")" * 200 + " AS deep",
    "SELECT a FROM T WHERE " + " AND ".join(f"c{i} = 'v'" for i in range(3000)),
    "SELECT CONCAT() AS x",
    "SELECT FORMAT() AS x",
    "SELECT [a] FROM [T] AS [ ] WHERE [ ].[a] IS NULL",
    "SELECT a FROM T WHERE CAST(a AS DATE) = CAST(DATEADD(day, " + " + ".join(["1"] * 3000) + ", GETDATE()) AS DATE)",
    "SELECT a FROM T WHERE CAST(a AS DATE) = CAST(DATEADD(day, " + " * ".join(["99999"] * 300) + ", GETDATE()) AS DATE)",
    "SELECT a FROM T WHERE CAST(a AS DATE) = DATEADD(day, 1)",
    "SELECT a FROM T WHERE CAST(a AS DATE) = CONVERT(DATE)",
    "SELECT a FROM T RIGHT JOIN (SELECT 1 AS b) s ON s.b = T.a FULL JOIN OPENJSON('[]') j ON 1 = 1",
    "SELECT a AS b, a AS b, b AS a, T.a AS b, T.b AS a FROM T",
])
def test_odd_sql_never_raises(sql):
    parsed = parse(sql)
    assert isinstance(parsed, ParsedQuery)
    assert not warned(parsed, "errore imprevisto")  # the module's own safety net was not needed


def test_the_standard_tsql_dialect_is_left_unchanged():
    # The module parses FORMAT(...) as written through its own dialect class only.
    node = sqlglot.parse_one("SELECT FORMAT(GETDATE(), 'yyyy') AS y", read="tsql")
    assert isinstance(node.selects[0].this, exp.TimeToStr)


# ---------------------------------------------------------------------------
# dotnet_to_strftime
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dotnet, expected", [
    ("yyyyMMdd_HHmm", "%Y%m%d_%H%M"),
    ("yyyy-MM-dd HH:mm:ss", "%Y-%m-%d %H:%M:%S"),
    ("dd/MM/yy hh:mm tt", "%d/%m/%y %I:%M %p"),
    ("ddd, dd MMM yyyy", "%a, %d %b %Y"),
    ("dddd dd MMMM", "%A %d %B"),
    ("'Year' yyyy", "Year %Y"),
    ('"dd" dd', "dd %d"),
    ("\\d dd", "d %d"),
    ("100% yyyy", "100%% %Y"),
    ("yyyy 'unclosed", "%Y unclosed"),
    ("yyyy\\", "%Y\\"),
    ("", ""),
])
def test_dotnet_to_strftime(dotnet, expected):
    assert dotnet_to_strftime(dotnet) == expected


@pytest.mark.parametrize("unknown", ["yyy", "fff", "YYYY-DD", "H:m:s", "d", "MMMMM", "g"])
def test_dotnet_to_strftime_passes_unknown_tokens_through(unknown):
    assert dotnet_to_strftime(unknown) == unknown


def test_dotnet_to_strftime_gives_what_format_would_write():
    moment = datetime(2026, 10, 11, 9, 5, 7)
    assert moment.strftime(dotnet_to_strftime("yyyyMMdd_HHmm")) == "20261011_0905"
    assert moment.strftime(dotnet_to_strftime("dd/MM/yyyy hh:mm:ss tt")) == "11/10/2026 09:05:07 AM"


def test_null_select_gives_an_empty_entry_column():
    from cjhelper.model import SelectNull
    parsed = parse(f"SELECT NULL AS X {FROM_MASTER}")
    assert parsed.select == [SelectItem("X", SelectNull())]
    assert not parsed.warnings


def test_format_with_tokens_the_poc_cannot_write_is_unsupported():
    from cjhelper.sqlparse_lite import unsupported_dotnet_tokens
    assert unsupported_dotnet_tokens("yyyyMMdd_HHmm") == []
    assert unsupported_dotnet_tokens("d/M/yyyy HH:mm:ss.fff") == ["d", "M", "fff"]
    parsed = parse(f"SELECT FORMAT(GETUTCDATE(), 'd/M/yyyy') AS X {FROM_MASTER}")
    assert isinstance(parsed.select[0].expr, SelectUnsupported)
    assert warned(parsed, "colonna X", "token che il PoC non sa scrivere")
