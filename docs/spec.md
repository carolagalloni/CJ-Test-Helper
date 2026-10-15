# CJ Test Helper PoC: Specification

11 October 2026

## Purpose and scope

The PoC is a one-page app that takes a Salesforce Marketing Cloud package export (JSON) and returns ready-to-import CSV test data covering every branch of the customer journey. It replaces the manual work of designing test contacts by hand, and it uses no LLM: the logic is a deterministic Python script.

In scope:

- Parse one journey and its related tables, relationships and queries from the export.
- Work out which combinations of field values send a contact down each branch.
- Generate one CSV per table, with consistent keys across tables.
- Generate a testbook in the team's layout (expected result per case) and a coverage report.

Out of scope for the PoC:

- Login, user management, storage of past runs.
- Connecting to Salesforce: no import, no journey execution, no collection of evidence.
- Journeys from platforms other than Salesforce Marketing Cloud.

## Development priorities

The main goal is trusted test data generated from a customer journey JSON; everything else serves it and is built after it. Work proceeds in the order below, and a lower level never delays or complicates a higher one.

| Priority | What | Done when |
| --- | --- | --- |
| 1. Core | Read the JSON, select the tables, find the cases that cover every branch, write the CSVs for the entry table and the linked tables | The CSVs import cleanly and send contacts down the intended branches |
| 1. Core | Trust checks on the generated data | Every check below passes, or the failure is shown in the report |
| 2. Needed to use the data | `scenarios.csv` and `report.md`: which scenario is which, what is covered, what is not and why | A tester can run the scenarios without reading the code |
| 3. Special branches | Engagement splits (manual action, real inbox) and random splits (statistical case) | Both branches of each have contacts and are flagged |
| 4. Bonus | `testbook.xlsx` in the team's layout | One test per case with the expected result |
| 5. Convenience | The one-page app, its settings, the anomaly checks, the extra operators | Upload, generate, download works end to end |

"Trusted" has a precise meaning here. After writing the files, the generator reads them back and checks them:

- Every value respects the type, length, nullability and primary key of its schema.
- Every key joins: each contact row finds its scenario row and its rows in the linked tables.
- Each generated contact, simulated again through the journey from the written values, takes exactly the path it was created for.
- Every reachable branch is crossed by at least one case, and every branch not covered is listed with its reason.
- The same JSON and settings give the same data.

Until priority 5 is built, the generator runs from the command line with the default settings. The core must be complete and checked on the pilot before work starts on the testbook or the page.

## Decisions taken

| Topic | Decision |
| --- | --- |
| Relation to earlier work | Fresh, simplified rebuild with the same goal |
| Input | Always a Salesforce Marketing Cloud package export |
| Coverage | Every branch of every split at least once |
| Contacts per case | 3 |
| Engagement and random splits | Data for both branches; contacts needing a manual action are flagged |
| Tables | Derived from the JSON by a general rule, using schemas only (no table data) |
| File layout | One CSV per table, holding all scenarios |
| Real contact details | The tester supplies a real inbox and a real phone number in the app |
| Dates and waits | Configurable in the app, with defaults |
| Deliverable | Test data is required; expected outcomes are a bonus |
| App | One page: upload JSON, download CSVs |
| Execution | None; the app only generates files |
| Expected outcomes format | The team's testbook layout, one test per case, as an Excel file |

## Input: the package export

The only input is the JSON file exported from Package Manager, which already contains everything the generator needs. The app reads these parts of `entities`:

| Part of the JSON | What the app takes from it |
| --- | --- |
| `journeys` | Activities, their outcomes and `next` links, split criteria, the entry trigger |
| `eventDefinitions` | Which table is the entry table, and which automation feeds it |
| `dataExtensions` | Table names and schemas: field name, type, length, primary key, nullable, default value |
| `attributeSets`, `attributeRelationships` | How tables are linked, and on which fields |
| `automations`, `queryActivities` | The SQL that fills the entry table: source tables, joins, filters |

Parsing rules:

- References written as `{{mcpm#/entities/...}}` are resolved to the real names before anything else.
- If the export holds more than one journey, the app uses the one listed in `selectedEntities.journeys`.
- Split criteria are XML (`FilterDefinition`) with nested AND/OR groups. Each condition names a table, a field, an operator and a value.

Supported in the PoC, based on the pilot journey:

| Kind | Supported values |
| --- | --- |
| Activity types | Decision split, engagement split, random split, wait by duration, wait by attribute, send-time wait, email, update contact |
| Condition operators | `Is`, `Equal`, `IsNull` |
| Field types | Text, Boolean, Date, EmailAddress, Phone |

The earlier tool's condition parser also handled not-equal, numeric comparisons, contains, begins-with, ends-with and is-not-null. The pilot does not use them, but they can be carried over at low cost.

Anything else is not an error: it is listed in the report, and the branches behind it are marked as not covered. New activity types and operators are added by extending one lookup table each.

## Table selection

A table gets a CSV when something reads it; tables that are only written to are skipped. The rule is general and applied from the JSON, not hard-coded per journey.

| Role | How the app finds it | CSV |
| --- | --- | --- |
| Entry table | The table bound to the journey's entry trigger | Yes |
| Split table | Any table named in a split condition or in a wait-by-attribute | Yes |
| Source table | Any table in the FROM or JOIN of a query that fills the entry table, followed back until no query writes it | Yes |
| Written-only table | Target of an update contact step or of a query, never read by a split or by the entry query | No |

Applied to the pilot journey:

| Table | Role | CSV |
| --- | --- | --- |
| `DE_NCM_AdHoc_Entry` | Entry | Yes |
| `SF_Master_DataExtension_DE` | Split and source | Yes |
| `DE_NCRM_AD_HOC_CONFIG` | Split and source | Yes |
| `DE_NCRM_AdHoc_Target` | Source | Yes |
| `DE_SkyDE_AdHoc_Log_partial` | Written by the journey | No |
| `DE_NCRM_AdHoc_Entry_Backup` | Written by a query | No |
| `DE_Check_Active_Campaigns` | Written by a query | No |

The entry CSV is kept consistent with the source CSVs: its rows are what the entry query would produce from them. The tester imports the entry CSV directly, together with the CSVs of the linked tables: this is the normal way in the test environment, where the source tables hold no data. Importing the sources and running the automation is the alternative. The two ways are not combined, because doing both would duplicate the entry rows.

SQL handling is deliberately limited in the PoC. The app reads table names, join equalities, simple WHERE filters (equality and date range) and simple select expressions (column, literal, `CONCAT`, current date). Anything more complex is reported, and the affected entry column gets a placeholder.

## Journey model and coverage

The journey is treated as a graph, and test design becomes a search for the smallest set of paths that crosses every branch. The generator runs these steps in order:

1. **Build the graph.** Each activity is a node; each outcome is a branch pointing to the next activity. The start is the activity no other activity points to.
2. **Translate conditions.** Each decision split outcome becomes a logical formula over table fields. Outcomes are tried in order, the first true one wins, and the remainder branch means none matched.
3. **Classify fields by level.** A field is contact-level when its table has one row per contact (joined to the entry on the contact key). It is scenario-level when many contacts share one row (joined on any other field).
4. **Enumerate values, not paths.** Paths are not listed one by one: the earlier tool counted about 775,000 structural paths in the pilot, almost all impossible. The generator instead simulates the journey for each combination of scenario-level values and each contact class, where a class groups the contact-level combinations that satisfy the same conditions (8 classes in the pilot). Each field takes the values named in conditions, one extra "other" value, and null when the schema allows it.
5. **Choose the cover.** A greedy set cover with a fixed random seed picks the fewest scenarios that cross every reachable branch.
6. **Group into scenarios and cases.** Paths sharing the same scenario-level values form one scenario. Inside a scenario, each distinct combination of contact-level values and manual actions is one case, with 3 contacts.

In the pilot, the contact key is `ContactId`, Master is contact-level, and Config is scenario-level because it is joined on `CampaignName`.

Special activities:

| Activity | Treatment |
| --- | --- |
| Engagement split | Both branches get contacts. The Yes branch needs a manual action (open or click), recorded in the testbook, and uses the real inbox. |
| Engagement split, reachability | The Yes branch counts as reachable only if the path sent the referenced email earlier. Otherwise it is reported as unreachable. |
| Random split | Cannot be forced. The cover prefers paths that avoid it; branches reachable only through it get a statistical case of 50 contacts (configurable). |
| Wait by duration, send-time wait | No branching. The expected delay is noted in the testbook. |
| Wait by attribute | No branching. The date field is filled according to the date settings. |
| Update contact | The written values are tracked, so a later split that reads the same field sees the updated value. |

With a 90/10 random split, 50 contacts reach the 10% side at least once with about 99.5% probability. The testbook states the accepted range: 1 to 9 contacts on the 10% side, which holds in about 97% of runs.

Every branch ends in one of three states in the report: covered, covered with manual action, or not covered (unreachable, or behind an unsupported element), each with its reason.

Rules that follow from how Salesforce reads data (checked against its documentation on 11 October 2026):

- **Data is read live.** The pilot's splits and waits use contact data, which Salesforce looks up when the contact reaches the activity, not at entry. The rows of a scenario must not be changed or re-imported until its contacts have left the journey. To move to the next scenario, the tester edits only its activation field.
- **Any matching row counts.** When a contact has several related rows, a condition is true if any of them matches. Each contact therefore belongs to exactly one scenario and is never reused, and each generation uses a new run identifier.
- **A past wait date lets the contact through at once.** Scenarios with a manual action come first in the run order, the testbook gives the deadline as a date and time, and a run that slips past it is regenerated.
- **A click needs a link.** When the email referenced by a click split has no link, as in the pilot, the Yes branch is reported as not covered instead of asking the tester for a click.

## Data generation rules

Every generated value comes from one of five sources, applied in this priority order:

| Priority | Field kind | Value |
| --- | --- | --- |
| 1 | Key or join field | Generated key, identical in every table that joins on it |
| 2 | Field read by a split | The value the case requires, written exactly as in the condition (same letter case) |
| 3 | Field filtered by the entry query | A value that passes the filter, so the contact really enters the journey |
| 4 | Email, phone, date | See the rules below |
| 5 | Any other field | Placeholder by type: text `TST_<field>_<n>` cut to the field length, Boolean set to its default or false |

Keys:

- Contact keys are `TST_C0001`, `TST_C0002`, and so on.
- Scenario keys are `TST_S01`, `TST_S02`, and so on, written into the field that joins contacts to the scenario-level table.
- Primary keys are unique within each table.

A run identifier, set in the app, is added to every key, giving `TST_R01_C0001` and `TST_R01_S01`. Without it, a second generation would reuse keys already imported into the real tables, and the evidence of two runs would mix.

Email:

- Contacts that must open or click get the tester's real inbox with plus-addressing, for example `name+TST_C0042@domain`.
- All other contacts get an undeliverable address, `<contact key>@example.invalid`.

Phone:

- In each case whose path sends a message to a phone, one of the 3 contacts gets the tester's real number so reception can be checked.
- All other contacts get a clearly fake number, configurable in the app.
- The pilot journey has no real SMS send (its SMS steps are update contact placeholders), so the real number is not used there.

Dates (all configurable, defaults shown):

| Date field | How it is recognised | Default |
| --- | --- | --- |
| Validity window | Date-range filter in the entry query | Starts yesterday, ends in 7 days |
| Wait date | Field used by a wait-by-attribute | Generation time, so the wait ends at once |
| Wait date before an engagement split | Wait-by-attribute on the path to an engagement split | Generation time plus 24 hours, leaving time to open the email |
| Other date fields | None of the above | Generation time |

The window starts a day early because the pilot's entry query compares dates in UTC. A window starting today could miss a run made just after midnight Italian time.

One scenario at a time. Some journeys allow a single active scenario per run; the pilot stops its automation if two campaigns are active. The app therefore has an optional "one active scenario" setting with an activation field, suggested from the entry query filters (`ACTIVE` in the pilot). When on, only the first scenario is active in the CSV, and the testbook tells the tester which row to switch before each run.

File format: UTF-8, comma-separated, header row with the exact field names in schema order, `True`/`False` for Booleans, empty cell for null.

The output is reproducible: the same JSON and settings always produce the same files, apart from values derived from the generation time.

## Outputs

The app returns one zip file with the data CSVs plus three supporting files.

| File | Content | Status |
| --- | --- | --- |
| `<TableName>.csv` | One per selected table, all scenarios together, ready to import | Required |
| `scenarios.csv` | One row per scenario: its key, its scenario-level values, run order, what to activate | Required |
| `testbook.xlsx` | One test per case with the expected result, in the team's testbook layout | Bonus |
| `report.md` | Coverage, unreachable branches, unsupported elements, warnings, settings used | Required |

Columns of `testbook.xlsx`:

| Column | What the app writes |
| --- | --- |
| ID | Progressive test number |
| Owner system | `SFMC` |
| Test name | Case identifier and a short summary of the path, for example "S07-C02 - Email sent on channel 1, no phone consent" |
| Description | Scenario values, contact profile values, and the expected path as activity names in order |
| Pre conditions | Which CSVs must be imported, which scenario must be active, and the keys of the test contacts |
| Detailed steps | Numbered steps: activate the scenario, start the entry, wait (with expected delays), any manual action with its deadline, collect evidence |
| Expected result | For each contact: the emails or messages received, the values written by the journey, and the last activity reached |
| Status | `TO START` |
| Note | Manual engagement required, or the accepted range for a random split case |

The testbook has one row per case, not per contact: the 3 contacts of a case share the same expected result and are listed in Pre conditions. The 50 contacts of a random split case are given as a key range.

The report opens with a summary: number of scenarios, cases and contacts, and branches covered, covered with manual action, and not covered. It also lists suspicious patterns found while parsing, such as the same value written in two letter cases (`Email` and `EMAIL` in the pilot).

Checks carried over from the earlier tool, all generic:

- An engagement split that references an email never sent on the paths reaching it.
- Splits with the same name but different conditions.
- An update contact step that writes a constant into a primary-key field.
- A journey default that reads from a different entry event.
- A one-to-one relationship to a table whose primary key is wider than the join.

## The app

The app is a single Streamlit page run locally, with no login and nothing stored: the file is processed in memory and discarded. The tester goes through four steps on the same page:

1. **Upload** the package export JSON.
2. **Review** what was detected: journey name, number of activities by type, the tables with their role, and any warnings.
3. **Adjust settings** if needed; every setting has a default.
4. **Generate**, look at the summary and a preview of each CSV, then download the zip.

Settings:

| Setting | Default |
| --- | --- |
| Contacts per case | 3 |
| Contacts per random-split case | 50 |
| Real inbox for open and click tests | Empty |
| Real phone number | Empty |
| Fake phone number | `333333333333` |
| Validity window length | From yesterday to 7 days ahead |
| Wait date offset | 0 hours |
| Wait date offset before an engagement split | 24 hours |
| Date format in the CSVs | `YYYY-MM-DD HH:MM` |
| One active scenario at a time | On when an activation field is detected |
| Activation field | Suggested from the entry query filters |
| Run identifier in keys | R01 |
| Text comparisons ignore letter case | Off |

Behaviour at the edges:

- A file that is not valid JSON, or holds no journey, gives a plain error message and nothing else.
- If the real inbox is empty, generation still works. The open and click contacts get undeliverable addresses, and those branches are reported as not covered.
- The same applies to the real phone number.

## Code structure

The generator is a plain Python package with no Salesforce or web dependency, and the Streamlit page is a thin layer on top. That keeps the logic testable and reusable from a command line later.

| Module | Responsibility |
| --- | --- |
| `loader.py` | Read the JSON, resolve `{{mcpm#...}}` references, return journey, tables, relationships, queries |
| `criteria.py` | Parse the split criteria XML into condition formulas |
| `sqlparse_lite.py` | Extract source tables, joins, filters and select expressions from the entry queries |
| `tables.py` | Apply the table selection rule and classify fields by level |
| `graph.py` | Build the journey graph and simulate a contact along a path, including update contact writes |
| `coverage.py` | Enumerate value combinations, simulate them, run the set cover, group into scenarios and cases |
| `datagen.py` | Turn cases into rows per table, following the data generation rules |
| `outputs.py` | Write the CSVs, `scenarios.csv`, `testbook.xlsx`, `report.md` and the zip |
| `app.py` | The Streamlit page |

Dependencies: Python 3.11 or later, `streamlit`, and the standard library (`json`, `xml.etree`, `csv`, `zipfile`). The testbook is written with openpyxl. SQL parsing uses `sqlglot` in T-SQL mode.

Tests: unit tests per module on small hand-made journeys, plus one end-to-end test on the pilot export.

## Assumptions to verify and known limits

The generator encodes six assumptions about how Salesforce behaves; each should be checked once with a small real run, because the expected results depend on them.

- [ ] Decision split outcomes are evaluated in order and the first match wins.
- [ ] Text comparisons: whether `Email` and `EMAIL` are treated as equal. The PoC assumes they are different by default; the earlier tool assumed they are equal, so this is a setting.
- [ ] A wait-by-attribute whose date is already past ends immediately.
- [ ] The import accepts the chosen date format and `True`/`False` for Booleans.
- [ ] An engagement split is evaluated when the contact reaches it, not when the email was sent.
- [ ] The time zone applied to a wait date. The pilot's waits are set to W. Europe time, so the 24-hour deadline could shift by some hours.

Status after the documentation check: a secondary source supports first-match evaluation from top to bottom ([SalesforceTutorial](https://www.salesforcetutorial.com/decision-split/)), and the Journey Builder interface text quoted in a [community thread](https://trailhead.salesforce.com/trailblazer-community/feed/0D53A00004uu1W3SAI) says a past wait date lets the contact proceed. Nothing was found on letter case, on import formats, or on how the time zone of a wait date is applied.

Known limits of the PoC:

- Branch coverage, not path coverage: every branch is crossed at least once, but not every combination of branches.
- Random splits are covered statistically, never with certainty.
- Only the listed activity types, operators and SQL patterns are understood; the rest is reported, not guessed.
- Entry criteria defined outside the JSON (for example contact filters set in the interface and not exported) are not seen.
- Email content and personalisation are not tested; the tool tests the routing of contacts.

## Acceptance criteria on the pilot journey

The PoC is done when uploading `CJ template complesso-v1.json` produces a zip that passes all of these checks:

- [ ] The journey `CJ_SkYDE_NCRM_Demo_final` is parsed with all 178 activities and no crash.
- [ ] Exactly four CSVs are produced: Entry, Master, Target, Config.
- [ ] Each CSV has the exact columns of its schema, unique primary keys, and values that respect type and length.
- [ ] Every Target row joins to a Master row and to a Config row; every Entry row matches what the entry query would return.
- [ ] Every reachable branch of the 106 decision splits is covered by at least one case of 3 contacts.
- [ ] The four engagement Yes branches after channels 2 and 3 are reported as unreachable, because all six engagement splits reference the first email.
- [ ] The 90/10 random split has a statistical case of 50 contacts.
- [ ] Contacts needing an open or click use the real inbox; all others use `@example.invalid`.
- [ ] `testbook.xlsx` has one test per case, names every generated contact, and `report.md` totals match the files.
- [ ] Running twice with the same settings gives the same scenarios, cases and keys.

For reference, the earlier hand-built tool reported 40 path scenarios plus 1 statistical, 148 cases and 494 contacts on this journey; the PoC should land in the same range.

## Extensions beyond the pilot (agreed 12 October 2026)

Four further exports, kept outside the repository because they belong to clients, use constructs the pilot does not. They may be test journeys that are not fully working: they are sources of structures and cases, not numeric targets. The rules below extend the spec; where they differ from an earlier section, they win. Facts marked "confirmed" were confirmed by the team from Salesforce practice.

### Data sources of a condition

- **Journey Data.** A condition key `Event.<eventDefinitionKey>.<Field>` (marked `IsEphemeralAttribute="true"`) reads column `Field` of the data extension of the trigger's entry event. Its value is the snapshot taken at entry: later Update contact writes do not change it. A key naming an event definition other than the trigger's is unsupported.
- **Contact Data.** `<AttributeSet>.<Field>` as before, read live when the contact reaches the activity. The rule "a later split that reads the same field sees the updated value" applies to Contact Data only.
- **Attribute compared with an attribute.** A condition whose value is another attribute (`ValueParameterName`, or `ValueIsReference`) is supported only in the form "key field of a contact-level linked table `Equal` the contact key", read as "the contact has a row in that table". Any other form is unsupported and reported; it is never read as a comparison with an empty value.
- **Missing relationship.** An attribute set read by a split with no relationship in the export: its conditions are unsupported, with the reason "relationship missing in the export". Package Manager `issues` of level warning or error are listed in the report.
- Table levels keep the rule of "Classify fields by level" (join field), not the cardinality declared in Contact Builder.

### Row present or absent

- A contact-level table other than the entry table, whose existence a split tests (`IsNull` or `IsNotNull` on its field linked to the contact key, or the attribute comparison above), gets a contact-level variable "row present / row absent". No such variable for tables the entry query joins, or that no split tests for existence.
- A contact whose row is absent has no row in that table's CSV. This is the only exception to "each contact row finds its rows in the linked tables".
- On an absent row, `IsNull` on any of its fields is true and every other operator is false (confirmed: a contact with no row reads its key as null).
- An Update contact on that table inserts the row: later reads see it present.

### Operators, types and values

- Supported operators: `Equal`, `Is`, `NotEqual`, `IsNull`, `IsNotNull`, `GreaterThan`, `GreaterThanOrEqual`, `LessThan`, `LessThanOrEqual`, `Between`, `Contains`, `BeginsWith`, `EndsWith`. `Between` takes the value `low|high` and includes both bounds (confirmed). Field types add Number and Decimal.
- Coverage values are chosen strictly inside the ranges the conditions define, never on a bound. Among values that cover the same branches, the generator prefers those whose path rests on no unverified assumption: not null, row present.
- A null value is an empty CSV cell, which Salesforce imports as null (confirmed). A cell holding a space would be a value, so the generator never writes one.

### Entry source and procedure

The CSVs are still imported by hand. The procedure depends on the entry source, read from the event definition (type and `metaData.scheduleFlowMode`):

| Entry source | Procedure in report.md and the testbook |
| --- | --- |
| Automation (the pilot) | As before |
| Data extension, Run Once | Import every CSV, then activate the journey. Only rows present at activation enter; rows imported later never do (confirmed). A new attempt needs a new journey version and a new run identifier. All scenarios enter together, so "one active scenario at a time" is not offered. |
| Data extension, recurring schedule | Reported as a procedure to verify: contacts enter at the next evaluation |
| CloudPages form, API event or any other | Not driven by a CSV import. The team tests a copy of the journey whose entry source is a data extension with the same columns (confirmed); the report says so, and the entry CSV is for that data extension |

- Entry modes `OnceAndDone` (no re-entry) and `SingleEntryAcrossAllVersions`: the testbook says every attempt needs a new run identifier.
- Entry filters, goals and exit criteria are not modelled; when present they are reported, because contacts may not enter or may leave early.
- The entry data extension must hold only test rows when the journey is activated; report.md says so for Run Once entries.

### Honest outcomes

- Past an unsupported element the expected result says the path is unknown from that activity on; it never names the unsupported activity as the last one reached.
- report.md and the page open with a warning when unsupported elements leave branches not covered.
- The deadline of a manual open or click is computed from the waits between the email and the engagement split, with a warning when the window is under one hour. The testbook gives each case's minimum duration.
- report.md lists only the assumptions the export triggers. Warnings about assets the journey does not use are dropped. A journey default (email, mobile number) that names a column the entry data extension lacks is reported.

### Acceptance beyond the pilot

- On the pilot, every CSV and `scenarios.csv` stay byte-identical to the outputs before these extensions (same time, run identifier and seed); differences in report.md and the testbook are listed for the team.
- The four further exports run without errors, and each construct above has a synthetic example in `examples/` with hand-derived expected answers.

## Extensions for time-dependent journeys (step 3, 14 October 2026)

Six further exports of one retention family (two weeks, four weeks, six weeks, three months, five days and
hot churn before a contract end date) use constructs that neither the pilot nor the step 2 exports had. They
are client exports and stay outside the repository. The rules below extend the spec; where they differ from an
earlier section, they win. Facts marked "documented" were read in the Salesforce documentation on 14 October
2026; everything else marked "assumption" stays visible in the report until a real run confirms it.

### The clock: a contact's path depends on when it reaches an activity

The pilot's paths depended on data only. In these journeys a condition can compare a date with "Today", a wait
can end a number of days before a date held in a field, and the exit criteria are checked when a wait expires.
The simulation therefore carries a clock:

- The clock starts at the **entry time**, a setting whose default is the generation time (the tester imports
  the files and activates the journey right after generating them; the report gives the deadline by which this
  must happen, see "Validity of the data").
- A **wait by duration** advances the clock by its amount (a month counts 28 days, as before).
- A **wait by attribute** holds the contact until the date in its field plus its offset (`waitDuration` and
  `waitUnit`, which may be negative: "13 days before Entitle_RequestCancellationDate" is an offset of -13 days),
  at the time of day given by `specifiedTime` when it has one, otherwise at the time of day held in the field.
  When that moment is already past, the contact goes on at once (as before). When the field is null, the
  contact goes on at once (assumption: the vendor documentation says a contact with no value proceeds).
- A **send-time wait** advances the clock by nothing (the email may go out at once); its sliding window is noted
  in the testbook as before.
- An email and an SMS are sent at the clock time; `{{DateTime.Now}}` written by an Update contact is the clock
  time; an engagement split is evaluated at the clock time, which is the deadline of its manual action.
- Every case carries its **timeline**: the clock at every step of the expected path. The testbook writes it
  as days from the entry time with the absolute date and time.

Time zones: the clock is naive local time, the time zone of the waits (`W. Europe Standard Time` in these
exports) is reported, and the assumption on the time zone applied to dates in the CSVs stays as before.

### Relative dates in conditions

A condition value written `;<sign>;<n>;<unit>` (for example `;+;0;days` or `;-;7;days`, shown in the
interface as "Today", "Today Plus 90 days", "Today Minus 7 days") compares the field with the clock's date
moved by `n` units. The operators that take such a value are `After`, `AtOrAfter`, `Before`, `AtOrBefore`;
`IsNot` is the negation of `Is` (on a Boolean `IsNot true` is true for `false` and for null, assumption: the
rule "any other operator on null is false" does not apply to a negated operator; `NotEqual` keeps its
earlier reading, since the step 2 example `operators_text` fixes it).

- Comparison level: with the unit `days`, `weeks`, `months` or `years` the field's date part is compared with
  the clock's date part, as the interface says "Today" (confirmed by the team on 14 October 2026); with `hours`
  or `minutes` the full date and time are compared with the clock ("Now") (assumption). Both bounds are
  inclusive for `AtOrAfter` and `AtOrBefore`.
- The same operators with an absolute date value keep the earlier reading (a value without a time compares
  on the date part).
- Any other value form, unit or operator is unsupported and reported as before.

### Values of a date field read with relative dates or by a wait with an offset

The step 2 rule "coverage values are chosen strictly inside the ranges the conditions define" is kept, but
the ranges now move with the clock, so the values are chosen as offsets from the entry time:

- For a field with at least one relative condition or one wait by attribute with an offset, the generator
  lists the **cut points** in days: for each relative condition, `n` plus every fixed delay with which a
  contact can reach that split (the sum of the waits by duration on each path from the start, waits by
  attribute counting zero); for each wait by attribute on the field, minus its offset plus the same delays
  (the value at which the wait ends exactly when the contact reaches it).
- The candidate values are the entry time moved by every cut point and by every cut point plus or minus
  one and two days, plus the absolute values named by the other conditions, plus null when the schema
  allows it. Candidates that take the same branches at every split that reads the field, whatever the delay,
  are one class; the generator keeps one value per class, chosen in this order: (1) among the candidates whose
  validity (see "Validity of the data") reaches the setting "minimum validity of the data", or all of them when
  none does; (2) the one that makes the waits by attribute on the field end soonest (so the test runs as early
  as the margin allows; a setting of 0 lets them end at once); (3) not on a cut point; (4) the longest validity;
  (5) the earliest date; null never represents a class that has a non-null member. It tries the classes like
  any other domain. A case whose values are all not perishable has no validity (nothing in it depends on the
  clock) and sets no deadline.
- A wait by attribute with an offset no longer writes "the generation time plus the wait offset" into its
  field when the field also has conditions: the field takes the candidate the case needs, and the
  testbook says when the wait ends. A wait by attribute on a field with no condition keeps the earlier rule.
- Some classes compare on a bound at the split whatever their value (the journey waits until "13 days before
  the date" and then asks "is the date on or after Today plus 13 days": the wait makes the comparison fall on
  the bound for every contact that arrives in time). The branch is still covered, by a value of that class
  chosen with the rules above, and the testbook and the report say that the branch is decided on the bound of a
  day-level comparison (confirmed by the team on 14 October 2026).

### Validity of the data

A value chosen relative to the entry time stops working when the test starts late: a holiday that "starts
after Today" becomes current. The validity of a value is the number of whole days the entry time can slip
before any condition on the field, at any delay a contact can reach it with, reads differently; the generator
computes it by re-evaluating the conditions with the entry time moved by one day at a time (so an exclusive
operator gives one day less than the distance from the cut point). A value that no slip of up to a year
changes is not perishable and sets no deadline. The report and the testbook give the earliest validity over
the cases as the deadline: "import the files and activate the journey by <date and time>" (the entry time plus
that many days). Values are chosen so that this margin is at least the setting "minimum validity of the data"
(default 1 day) wherever a class of values allows it, and no larger than a wait on the field needs, so that
the test runs as early as the margin allows (a setting of 0 gives values on which every wait ends at once and
the data must be activated the same day); a value with a smaller margin is reported.

### Exit criteria and goals

Journey Builder evaluates the exit criteria of a journey for each contact when the contact's time in a wait
activity expires (documented), goals before exit criteria. The generator models exit criteria whose
conditions it can evaluate (the same operators, data sources and relative dates as a decision split):

- At the end of every wait on a path the exit criteria are evaluated with the clock; when they are true the
  contact leaves the journey there. The expected path of such a case ends with "leaves the journey at the
  end of <wait>, because the exit criteria are met", and the testbook says so.
- Every exit criterion is one extra coverage target, "exit criteria met at the end of a wait", so that the
  cover includes at least one case that leaves the journey this way when it is reachable; the summary
  counts it apart from the branches of the splits.
- A branch no contact can reach because every contact that gets to the wait before it meets the exit
  criteria is reported as unreachable with that reason. (One of the six exports has `Entitle_RequestCancellationDate
  is not null` as an exit criterion, which every contact its splits accept satisfies: the report must say that
  every branch behind the first wait is unreachable for this reason, as a finding on the journey.)
- Exit criteria with a condition the generator cannot evaluate are reported as not modelled, as in step 2;
  goals are still reported, not modelled (no export of the cohort has one).

### SMS sends

`SMSSYNC` is a send to the phone number of the journey's default mobile number (`defaults.mobileNumber`,
a column of the entry table, as `defaults.email` for emails). It has no branching. The spec rule "in each
case whose path sends a message to a phone, one of the 3 contacts gets the tester's real number" applies:
that contact's row holds the real number in the default mobile column (and in every column the entry query
copies it from); the testbook's expected result names the SMS and the contact that receives it. Without a
real number the SMS cannot be checked and the report says so; the send still happens on the path.

### Tables reached through other tables

A split condition or a wait by attribute can read a table that the contact model reaches through another
table: in these exports `PublicHoliday_DE` is reached through `PublicHoliday_Entry` (contact key in
`SubscriberKey`, link on `PHKey`). The export names the path: the attribute path of the condition (the
relationship ids in `AttributePath` and `UiMetaData`) and the segments of the wait's attribute expression
(`Contacts.PublicHoliday_Entry.PublicHoliday_DE."PHEnd"`).

- Every table on the path from the contact to a read table is a **link table**: it gets a CSV, with the role
  "link", because without its rows the journey cannot read the table at the end of the path.
- Levels keep the rule of "Classify fields by level": the link table joined on the contact key is
  contact-level, the table it points to through another field is scenario-level (one holiday row shared by
  the contacts of a scenario), and the scenario key is written into the linking field of the link table.
- When the export gives no path for a read table that no selected table joins, the generator looks for the
  shortest chain of relationships from the contact to it and uses it with a warning; with no chain at all,
  the earlier rule applies (one row shared by all contacts, assumption).

### Columns the entry query derives and columns it copies

- A column of the entry table copied as a plain column from a source table (`M.Entitle_RequestCancellationDate`)
  holds the same value as its source: the two fields are **one variable** for the coverage (as the key
  groups are for keys), so a Journey Data condition on the copy and a Contact Data condition on the source read
  the same value, and the CSVs agree. The same holds through a chain of followed queries.
- A column of the entry table that a split reads and that the entry query computes with an expression the
  generator does not understand (a `CASE WHEN ... THEN 'True' ELSE 'False' END` on the timestamps of an
  earlier run) takes the value the case needs (data rule 2), not a placeholder. The report says that the
  automation would compute it from rows the generator does not reproduce, so the direct import of the CSVs
  is the only procedure for which the expected results hold.
- A filter comparing the date part of a column with the current date moved by a number of days
  (`CAST(CM.CreatedDate AS DATE) = CAST(DATEADD(day, -1, GETDATE()) AS DATE)`) is understood: the column takes the
  generation time moved by that number of days. The spelling with the current date cast first
  (`DATEADD(day, -2, CAST(GETDATE() AS DATE))`) is the same filter.
- A `LEFT JOIN` source still gets one row per contact (read as an inner join, as before); the report says so.
- A column of the entry table that the query does not populate and whose schema default is the current date
  (`GetDate()`) holds the generation time, as the other date fields; the trust check accepts it.

### Synchronized data extensions

A source table that Package Manager marks as a managed data extension (`NO_DEPLOY`,
`ManagedDataExtension`: the Salesforce objects synchronized from the CRM, named like
`CampaignMember_Salesforce_9`) is imported like every other CSV: its CSV is produced and is among the files to
import, in the report and in the testbook. The report notes that the table is synchronized from the CRM
(`NO_DEPLOY`, `ManagedDataExtension`), so the tester knows the rows will also arrive from the sync. Confirmed by
the team on 14 October 2026.

### Honest checks

- The trust check on the entry query reports a column it cannot verify (an expression the generator does not
  understand, a filter it does not understand, a LEFT JOIN read as an inner join) as "not verified", with the
  reason, and fails only on what it can verify.
- The simulation from the written files runs with the same clock and the same exit criteria as the coverage.
- The report opens with the deadline for activation when the data is perishable, next to the warning on
  unsupported elements.

### Acceptance for step 3

- On the pilot, every CSV and `scenarios.csv` stay byte-identical to the step 2 outputs (same time, run
  identifier and seed); differences in report.md and the testbook are listed for the team.
- The six exports run without errors; every decision, engagement and exit-criteria branch that the rules above
  make reachable is covered; each construct above has a synthetic example in `examples/` with hand-derived
  expected answers; the hand-derived answers for the six exports (kept outside the repository with the exports)
  match the generator's output.
