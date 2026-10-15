# Piano di costruzione e contratto dei moduli

Questo documento è il contratto di lavoro tra i moduli di `cjhelper/`.
`docs/spec.md` è la fonte di verità; questo file fissa solo *come* la spec
è divisa in codice. Se i due documenti non coincidono, vince la spec e questo file è sbagliato.

## Struttura del package

```
cjhelper/
  model.py          shared dataclasses: the only file every module imports (frozen contract)
  loader.py         export JSON -> Package           (resolves {{mcpm#...}}, builds Journey, Tables, Queries)
  criteria.py       FilterDefinition XML -> Formula; operator lookup table and condition evaluation
  sqlparse_lite.py  Query -> ParsedQuery              (sqlglot, T-SQL)
  tables.py         Package -> TableModel             (table selection rule, field levels, key groups)
  graph.py          journey graph walk and simulation (lead)
  coverage.py       value enumeration, set cover, scenarios and cases (lead)
  datagen.py        CoverageResult -> GeneratedData   (data generation rules)
  checks.py         trust checks on the written files (lead)
  outputs.py        CSV, scenarios.csv, report.md, testbook.xlsx, zip
  pipeline.py       generate(source, settings) -> GenerationResult (lead)
  __main__.py       command line: python -m cjhelper export.json --out dir
  app.py            Streamlit page (priority 5, later)
tests/              pytest, one file per module, plus test_examples.py and test_pilot.py
examples/           small hand-made journeys with expected answers (see "Examples")
fixtures/           the pilot export
```

Regole per tutti:

- Python 3.11+, libreria standard più `sqlglot`, `openpyxl`, in seguito `streamlit`. Nient'altro.
- Codice semplice e leggibile. Funzioni brevi, nomi espliciti, nessuna astuzia. I docstring dicono *qual è la regola della spec*.
- `model.py` è congelato. Se serve una modifica, dillo nel report finale; non modificarlo.
- Crea o modifica solo il tuo modulo e il suo file di test. Non toccare mai un altro modulo.
- Non inventare mai il comportamento di Salesforce. Quando la spec non dice nulla, scegli la lettura più semplice, rendila visibile (una voce in `warnings` o una riga del report) ed elencala sotto "Assumptions" nel report finale.
- Nulla è scritto in modo fisso sul journey pilota. I nomi di tabelle e campi vengono dall'export.
- Ogni valore è una `str` in forma CSV, oppure `None` per null. I Boolean sono `"True"`/`"False"`. Le date sono in `Settings.date_format`.
- La ricerca dei campi non distingue maiuscole e minuscole (`Table.field(name)`); viene scritta l'ortografia dello schema.
- Esegui `python -m pytest tests/test_<yourmodule>.py -q` prima di finire.

## Contratti dei moduli

### loader.py

```python
class LoadError(Exception): ...          # invalid JSON, no journey, missing entry table
def load_package(source) -> Package      # source: bytes, str (JSON text or path), dict, or pathlib.Path
```

- Risolvi prima ogni riferimento `{{mcpm#/entities/<kind>/<id>/data/<path>}}`, ovunque (anche le stringhe dentro
  le stringhe, per esempio dentro l'XML dei criteria e dentro l'SQL). Regole: `.../data/name` -> il nome dell'entità;
  `.../data/id` -> l'id dell'entità (la chiave dell'export); `.../data/fields/name:<F>/id` -> il nome del campo `<F>`;
  `.../data/namePrefix` -> il valore se presente, altrimenti `""`; `.../data/eventDefinitionKey` e gli altri
  path esistenti -> il valore; un path mancante -> l'id dell'entità più un warning. I placeholder `{{mcpm:...}}`
  (sender profile, delivery profile) restano come sono. In SQL `prefix[name]` diventa `[name]`.
- Scelta del journey: `selectedEntities.journeys[0]` se presente, altrimenti l'unico journey, altrimenti il primo con un warning.
  Nessun journey -> `LoadError`.
- Activity: mappa `type` tramite `model.ACTIVITY_KINDS`; `WAIT` con `metaData.uiType == "WAITBYATTRIBUTE"`
  è `KIND_WAIT_ATTRIBUTE` (il campo viene da `configurationArguments.waitEndDateAttributeExpression`, l'ultimo
  segmento tra virgolette = campo, il segmento prima = attribute set -> tabella; `metaData.attribute.id` è il ripiego).
  I tipi sconosciuti -> `KIND_UNSUPPORTED` e la chiave va in `Package.unsupported_activities`.
- Decision split: per ogni outcome, `configurationArguments.criteria[outcome.key]` viene letto con
  `criteria.parse_criteria`; l'outcome senza criteria è il remainder (`is_remainder=True`, formula None).
  Il `table` della condizione è il nome dell'attribute set; mappalo al nome della data extension tramite
  `attributeSets[*].storageName` (se nessun set corrisponde, usa il nome stesso, con un warning).
  Tieni gli outcome nell'ordine dell'export (è l'ordine di valutazione).
- Engagement split: `configurationArguments.refActivityCustomerKey` -> `engagement_ref`;
  `statsTypeId` tramite una lookup `ENGAGEMENT_KINDS = {2: "open", 3: "click"}`; ogni altro valore ->
  `engagement_kind = None` e un warning (il branch sarà segnalato come non coperto).
  Gli outcome portano `when` da `arguments.when`.
- Random split: `percentage` da `outcomes[*].arguments.percentage`.
- Email: `configurationArguments.triggeredSend.emailId` si risolve nell'id dell'asset; `email_name` dal nome
  dell'asset, altrimenti dal nome dell'activity. `EmailAsset.has_link` = un `href=` o un `<a ` compare in `views.html.content`
  o in `views.text.content`.
- Update contact: `arguments.activityData.updateContactFields[*]` -> `Write(table, field, value)`; il valore resta
  com'è scritto (`str(value)` per i numeri), `{{DateTime.Now}}` resta letterale.
- Start activity: quella a cui non punta nessun outcome (se ne attende una; se sono più d'una, la prima nell'ordine dell'export più un warning).
- Entry: il `metaData.eventDefinitionId` del trigger -> eventDefinition -> `dataExtensionId` -> tabella;
  `arguments.automationId` -> nome dell'automation. `Journey.default_email_event_key` = la chiave dell'evento dentro
  `defaults.email[0]` (per l'anomalia "il default legge un entry event diverso").
- Tabelle: tutte le `dataExtensions` (e `sharedDataExtensions`), con i campi in ordine `ordinal`.
- Relazioni: `attributeRelationships` -> una `Relationship` per ogni voce di `relationshipAttributes`, con le tabelle
  risolte tramite gli attribute set (`leftItem.identifier` / `rightItem.name`) nei nomi delle data extension.
  `isGroupToSetRelationship` -> `left_table=None`.
- Query: ogni voce di `queryActivities`; `Package.automation_queries[automation name]` elenca i nomi delle query
  in ordine di step (`steps[*].activities[*]` con `objectTypeId == 300`).

Regole aggiunte dalla sezione della spec "Extensions beyond the pilot" (12 ottobre 2026):

- Journey Data: una condizione con chiave `Event.<eventDefinitionKey>.<campo>` (segnata `IsEphemeralAttribute="true"`)
  legge la colonna `<campo>` della tabella di ingresso dell'entry event del trigger: `Condition.table` diventa quella
  tabella e `Condition.source` è `SOURCE_JOURNEY`, così il passo successivo può applicare la regola della fotografia
  all'ingresso. Una chiave che nomina un altro event definition (il pilota ne ha due: vale quello del trigger) rende il
  branch non supportato, con il motivo. Lo stesso per un wait by attribute scritto `{{Event.<key>."Campo"}}`.
- Confronto tra due attributi (`ValueParameterName`, oppure `ValueIsReference`): supportato solo nella forma "campo
  collegato alla chiave del contatto di una tabella a livello di contatto `Equal` la chiave del contatto" (la chiave del
  contatto è il Journey Data dell'entry event del trigger sul campo che contiene la chiave del contatto; la stessa
  chiave scritta come Contact Data, dall'attribute set della tabella di ingresso, non è riconosciuta e il confronto è
  segnalato come non supportato: una lettura più stretta della spec, prudente). La condizione
  viene normalizzata in modo che `table.field` sia il campo collegato e `value_attribute` la chiave del contatto, e si legge
  "il contatto ha una riga in quella tabella". Ogni altra forma rende il branch non supportato, con il motivo; non viene
  mai letta come confronto con un valore vuoto. Un campo è collegato alla chiave del contatto quando lo è per una relazione
  group-to-set, o tramite relazioni tra tabelle a partire da uno di questi campi o dalla chiave del contatto della
  tabella di ingresso.
- `Between` con un valore che non è nella forma `basso|alto` rende il branch non supportato.
- Relazione mancante: una condizione Contact Data su un attribute set che nessuna relazione dell'export nomina rende il
  branch non supportato con il motivo "relazione mancante nell'export"; la tabella va in `Package.missing_relationships`.
- `issues` di Package Manager di livello warning o error (anche nella `cause`), sia in cima all'export sia su ogni
  entità: diventano warning in italiano, tranne quelli su asset e landing page che il journey non usa.
- I warning sugli asset e sulle landing page che nessuna attività Email del journey invia vengono scartati, sia quelli
  della risoluzione dei riferimenti sia le `issues` (anche quando l'asset mancante è un'immagine di un'email inviata).
- Entry source: `EntryEvent.kind` viene dal `type` dell'event definition (`ENTRY_SOURCE_TYPES`) e, per una data
  extension, da `metaData.scheduleFlowMode` (`SCHEDULE_FLOW_MODES`): `ENTRY_AUTOMATION` (il pilota), `ENTRY_RUN_ONCE`,
  `ENTRY_SCHEDULED` (pianificazione ricorrente o non riconosciuta, con un warning), `ENTRY_OTHER` (form CloudPages,
  API event e ogni altro tipo). Restano anche `event_type`, `schedule_mode`, `run_once_mode` ed `entry_filter`.
  `arguments.automationId` diventa `automation_name` solo per un ingresso da automation: su una data extension con la sua
  pianificazione è lo scheduler di Journey Builder (assunzione) e non si segnala nulla.
- Criteri di ingresso (`arguments.criteria` dell'evento, `configurationArguments.criteria` e `filterDefinitionId` del
  trigger, `metaData.criteriaDescription`), goal ed exit criteria non sono modellati: se presenti, un warning.
  `Journey.entry_mode` (`entryMode`), `Journey.goal_count` e `Journey.exit_count` li riportano.
- Un valore predefinito del journey (`defaults.email`, `defaults.mobileNumber`) che legge una colonna che la tabella
  di ingresso non ha -> warning.
- `Field.scale` è letto per i campi Decimal.

### criteria.py

```python
class CriteriaError(Exception): ...
def parse_criteria(xml: str) -> Formula
OPERATORS: dict[str, Callable]            # the one lookup table for operators
def evaluate_condition(cond: Condition, value: Value, field_type: str, case_insensitive: bool) -> bool
class UnsupportedOperator(Exception): ...
```

- XML: `<FilterDefinition><ConditionSet Operator="AND|OR">` che annida `<Condition Key="Set.Field" Operator="..">`
  con il valore in `<Value>` (CDATA, eventualmente vuoto), a volte avvolto in uno o più `<AttributePath>`.
  `Key` si divide al primo `.` in set e campo (i nomi dei campi possono contenere spazi). Un set con un solo elemento
  si riduce a quell'elemento. Un set vuoto o un elemento sconosciuto -> `CriteriaError`.
- Operatori, tutti dalla spec: `Is`, `Equal`, `IsNull` (pilota) più `NotEqual`, `IsNotNull`, `GreaterThan`,
  `LessThan`, `GreaterThanOrEqual`, `LessThanOrEqual`, `Contains`, `BeginsWith`, `EndsWith` (ripresi dal vecchio tool)
  e `Between` (sezione "Extensions beyond the pilot": valore `basso|alto`, estremi inclusi).
- Chiavi: `Event.<eventDefinitionKey>.<campo>` è Journey Data (`source = SOURCE_JOURNEY`, `table` = la chiave
  dell'evento finché il loader non la mappa); `IsEphemeralAttribute="true"` su una chiave in un'altra forma ->
  `CriteriaError`. `ValueParameterName="<attributo>"` (con `<Value>` vuoto) o `ValueIsReference="true"` (l'attributo
  nel `<Value>`) -> `value_attribute`, nessun valore letterale. Funzioni di supporto: `split_attribute(text)` e
  `between_bounds(value)`.
  Semantica in `evaluate_condition` (il valore è sempre la stringa CSV oppure None):
  - Boolean: confronto dopo la conversione in minuscolo, accettando `true/false/1/0`.
  - Text, EmailAddress, Phone, Locale: corrispondenza esatta; non distingue maiuscole e minuscole quando lo dice l'impostazione.
  - Number/Decimal: confronto numerico. Date: leggi entrambi i lati (`YYYY-MM-DD[ HH:MM[:SS]]`, ISO); i valori
    di condizione con la sola data si confrontano sulla parte della data.
  - `IsNull`: None oppure `""`. `IsNotNull`: l'opposto. Ogni altro operatore su None -> False (assunzione, da elencare).
  - `Between`: `basso <= valore <= alto` con il confronto del tipo del campo; un valore non `basso|alto` -> `UnsupportedOperator`.
  - Confronto tra attributi (`value_attribute`, solo `Equal`, forma controllata dal loader): vero quando il campo ha un
    valore, cioè quando il contatto ha la riga.
  - Operatore sconosciuto -> `UnsupportedOperator`.

### sqlparse_lite.py

```python
def parse_query(query: Query, tables: dict[str, Table]) -> ParsedQuery
```

- `sqlglot` con `read="tsql"`. I nomi delle tabelle arrivano come `[Name]` o `ENT.[Name]`; gli alias sono frequenti.
- `sources`: la tabella del FROM e di ogni JOIN. `joins`: le uguaglianze delle clausole ON (concatenate con AND); tutto il resto -> warning.
- `filters`: il WHERE diviso sugli AND di primo livello. `col = 'lit'` -> `EqualityFilter`; `col IS [NOT] NULL` -> `IsNullFilter`;
  un confronto tra la data corrente (`GETDATE()`, `GETUTCDATE()`, `SYSDATETIME()`, con o senza `CAST(... AS DATE)`)
  e una colonna -> `DateBoundFilter` con `bound="start"` quando il filtro significa *now >= column* e `"end"` quando
  significa *now <= column*, da qualunque lato stia la colonna. Tutto il resto, compreso qualsiasi gruppo OR -> `UnsupportedFilter(text)`.
- `select`: colonna -> `SelectColumn`; letterale stringa o numero -> `SelectLiteral`; `FORMAT(GETUTCDATE(), 'fmt')` ->
  `SelectCurrentDate(fmt)`; una chiamata nuda alla data corrente, anche avvolta in `AT TIME ZONE` -> `SelectCurrentDate(None)`;
  `CONCAT(...)` -> `SelectConcat`; tutto il resto -> `SelectUnsupported(text)`. `target_column` = alias, altrimenti il nome della colonna.
  `SELECT *` -> `select_all=True`, select vuota.
- Risoluzione delle colonne: alias -> nome della tabella; non qualificata -> l'unica tabella sorgente che ha quella colonna
  (senza distinguere maiuscole e minuscole, tramite `Table.field`); altrimenti `ColumnRef("", col)` più un warning.
- Non sollevare mai un'eccezione su SQL anomalo: restituisci ciò che è stato compreso e metti il resto in `warnings`.

### tables.py

```python
def build_table_model(package: Package) -> TableModel
```

Chiama `sqlparse_lite.parse_query` da sé. Regole (spec "Table selection" e "Classify fields by level"):

- Tabella di ingresso: `journey.entry_table`. Entry query: la query dell'automation di ingresso il cui target è la tabella
  di ingresso (l'ultima in ordine di step se sono più d'una, con un warning); nessuna -> `entry_query=None` e un warning.
- Tabelle degli split: le tabelle citate in una condizione di un decision split o nel campo di un wait by attribute.
- Tabelle sorgente: FROM/JOIN della entry query, poi per ogni sorgente qualsiasi query il cui target è quella sorgente
  (prima l'automation di ingresso, poi il resto del package), in modo ricorsivo, con un insieme dei già visitati. Registra le query
  lette in `source_queries`.
- Solo scritte: i target degli update contact o delle query che non sono di ingresso, di split o sorgente -> non selezionate.
  Le tabelle senza alcun ruolo -> non selezionate, motivo "not read or written by this journey".
- Chiave del contatto: `(entry table, entry table's sendable_field)`; ripiego sul campo destro della relazione
  group-to-set sulla tabella di ingresso, poi su una chiave primaria di un solo campo; altrimenti un warning e il primo campo della PK.
- Gruppi di chiavi: union-find su ogni uguaglianza di join (join delle query e relazioni); il gruppo che contiene il campo
  della chiave del contatto è `kind="contact"`; un gruppo che contiene un campo di una tabella a livello di scenario è `kind="scenario"`; gli altri `"other"`.
- Livelli: visita in ampiezza dalla tabella di ingresso lungo gli archi di join tra le tabelle selezionate. Una tabella raggiunta tramite
  un campo del gruppo del contatto è a livello di contatto; raggiunta tramite qualsiasi altro campo è a livello di scenario; i vicini
  di una tabella a livello di scenario restano a livello di scenario. I livelli dei campi seguono la loro tabella, tranne i campi di una tabella
  a livello di contatto che appartengono a un gruppo di chiavi dello scenario (contengono la chiave dello scenario).
- `activation_field`: dai filtri della entry query, il primo `EqualityFilter` su un campo Boolean di una tabella
  a livello di scenario con valore `True`/`true`; altrimenti None.
- Ogni decisione finisce in `TableRole.reason` o in `warnings`, in parole semplici.
- Riga presente o assente (sezione "Extensions beyond the pilot"): `TableModel.existence_tested` = tabella -> campo
  collegato alla chiave del contatto, per ogni tabella a livello di contatto, diversa dalla tabella di ingresso, di cui
  uno split valutato verifica l'esistenza (`IsNull` / `IsNotNull` su quel campo, o il confronto tra attributi supportato).
  Mai per le tabelle lette dalle query che popolano la tabella di ingresso (la entry query e quelle che ne alimentano le
  sorgenti). Il motivo della tabella lo dice.
- I livelli restano quelli del campo di join, non la cardinalità dichiarata in Contact Builder.
- Un ingresso non alimentato da un'automation (Run Once, pianificazione ricorrente, form, API event) non ha di norma una
  entry query: non si segnala. Un ingresso da automation la cui automation manca dall'export lo dice così.
- Una tabella in `Package.missing_relationships` resta selezionata (è letta da uno split), a livello di scenario, con
  il motivo "relazione mancante nell'export" al posto dell'assunzione "una riga condivisa da tutti i contatti".
- Le condizioni Journey Data di un altro entry event non nominano alcuna tabella e non producono warning qui.

### Campi del modello aggiunti per le estensioni

| Campo | Significato |
| --- | --- |
| `Condition.source` | `SOURCE_CONTACT` (Contact Data, letto quando il contatto arriva allo split) o `SOURCE_JOURNEY` (Journey Data, fotografia all'ingresso) |
| `Condition.value_attribute` | L'attributo con cui il campo è confrontato, com'è scritto; nella forma supportata la chiave del contatto |
| `Field.scale` | Cifre decimali di un campo Decimal |
| `EntryEvent.kind`, `event_type`, `schedule_mode`, `run_once_mode`, `entry_filter` | Entry source e pianificazione; `ENTRY_*` in `model.py` |
| `Journey.entry_mode`, `goal_count`, `exit_count` | `entryMode` del journey; goal ed exit criteria presenti (non modellati) |
| `Package.missing_relationships` | Tabelle lette da uno split tramite un attribute set senza relazioni nell'export |
| `TableModel.existence_tested` | Tabelle con la variabile "riga presente / riga assente" -> campo collegato alla chiave del contatto |
| `CoverageResult.row_fields`, `row_absent(case, table)`, `GENERATED_KEY` | Le stesse tabelle come le usa la coverage; nei valori del caso il campo collegato vale `GENERATED_KEY` (riga presente) o None (riga assente) |
| `Case.unknown_from`, `Case.unknown_reason`, `RandomBranch.unknown_from`, `RandomBranch.unknown_reason` | Il percorso atteso non è noto da questa attività in poi, e perché |
| `ActionWindow`, `Case.action_windows` | Finestra di ogni azione manuale: wait prima dell'email e tra email e split, durata fissa, ultimo wait by attribute, arrivo dell'email, scadenza, sotto un'ora |
| `CoverageResult.unsupported_reasons` | Elementi di tipo supportato che il generatore non sa trattare per una parte dei contatti (un Update contact il cui inserimento può fallire, un branch che ordina un campo né numerico né data) -> motivo |
| `Case.min_duration_minutes`, `Case.not_before` | Durata minima del caso (wait a durata fissa) e data prima della quale non può finire (wait by attribute) |

### graph.py e coverage.py (lead)

Producono `CoverageResult`. Ciò su cui i consumatori possono contare:

- `Scenario.values` ha ogni campo di `CoverageResult.scenario_fields`; `Case.contact_values` ha ogni campo di
  `contact_fields`. I valori sono stringhe CSV oppure None. I valori di testo "other" hanno la forma `OTHER`.
- `Case.steps` è il percorso deterministico completo; per il caso statistico si ferma al random split
  e `Case.random.branches` contiene la continuazione per ogni outcome.
- `Scenario.run_order` parte da 1; gli scenari con azioni manuali vengono per primi. `Case.id` è `S01-C01`.
- `Case.contact_count` è 3 oppure il numero di contatti del caso statistico. `Case.waits` elenca, in ordine, le chiavi delle activity wait attraversate.
- `ManualAction` dice a datagen quali contatti hanno bisogno dell'inbox reale.

Regole aggiunte dalla sezione della spec "Extensions beyond the pilot" (12 ottobre 2026):

- Journey Data: una condizione `SOURCE_JOURNEY` legge la riga di ingresso com'era all'ingresso (la fotografia). Gli
  Update contact cambiano solo ciò che leggono le condizioni Contact Data (`graph.evaluate(..., snapshot)`); per il
  resto un campo Journey Data è un campo a livello di contatto della tabella di ingresso.
- Riga presente o assente: per ogni tabella di `TableModel.existence_tested` il campo collegato alla chiave del
  contatto ha il dominio `[GENERATED_KEY, None]`: `GENERATED_KEY` è "riga presente", None è "riga assente".
  `CoverageResult.row_fields` elenca queste tabelle (tabella -> campo collegato) e `CoverageResult.row_absent(case,
  tabella)` dice se i contatti del caso non hanno la riga. Su una riga assente ogni campo vale None: `IsNull` è vero,
  ogni altro operatore è falso, e nei `Case.contact_values` gli altri campi di quella tabella sono None. Un Update
  contact che scrive in quella tabella inserisce la riga: dopo, il campo collegato vale `GENERATED_KEY`, i campi
  scritti il valore scritto, gli altri il loro valore di default (un default `GETDATE()` vale l'ora della
  simulazione), None se non ne hanno (assunzione, righe 20, 29 e 46 di docs/assumptions.md). Se l'inserimento lascia
  vuoto un campo che non ammette null e non ha un default, può fallire (righe 19 e 46): il percorso si ferma
  sull'Update contact (`STOP_UNSUPPORTED`, `Case.unknown_from`), la chiave dell'attività entra in
  `CoverageResult.unsupported` e `unsupported_reasons`, e i branch che solo quei contatti raggiungerebbero sono "a
  valle di un elemento non supportato". `graph.Walker(..., row_fields=...)` applica queste regole: checks.py, che
  simula i contatti dai file, deve passargli `TableModel.existence_tested`.
- Valori: su un campo Number, Decimal o Date confrontato con `GreaterThan`, `GreaterThanOrEqual`, `LessThan`,
  `LessThanOrEqual` o `Between`, i valori delle condizioni tagliano i valori in intervalli e il dominio prende un
  valore strettamente dentro ciascun intervallo (sotto il più basso, in mezzo a due vicini, sopra il più alto: per un
  Decimal anche alla sua scala), poi i valori citati dagli altri operatori; un estremo non è mai provato. Un campo
  di testo non riceve valori dagli intervalli, e un warning lo dice; un branch che resta non coperto per questo è
  "branch non supportato" (non "non raggiungibile"), con la chiave `<split>/<outcome>` in
  `CoverageResult.unsupported` e il motivo in `unsupported_reasons`, così report.md e la pagina si aprono con
  l'avviso degli elementi non supportati.
- Preferenza: tra valori che coprono gli stessi branch vince quello con meno null e righe assenti, poi meno righe
  assenti, poi meno valori citati al posto di valori interni a un intervallo. Decide il rappresentante di ogni classe
  di contatti e lo scenario tenuto per ogni firma. Tra scenari diversi che coprono gli stessi branch conta solo il
  terzo criterio: il criterio dei null cambierebbe lo scenario S18 del pilota, che deve restare identico. Le due
  regole della spec qui sono in conflitto: la scelta spetta al team (docs/progress.md, "Estensioni oltre il
  pilota: da decidere con il team").
- `Case.unknown_from` e `Case.unknown_reason` (anche su `RandomBranch`): l'attività in cui il percorso simulato si è
  fermato senza uscire dal journey (elemento non supportato, operatore non valutabile, ...), e perché; da lì in poi il
  percorso atteso non è noto.
- `Case.action_windows`: una `ActionWindow` per ogni azione manuale, con i wait tra l'email e l'engagement split, la
  loro durata fissa in minuti (`graph.fixed_wait_minutes`: un send-time wait vale 0, un mese 28 giorni), l'ultimo
  wait by attribute tra loro e i wait del percorso prima dell'email (`wait_keys_before`), che ne ritardano l'arrivo. `Case.min_duration_minutes`: la durata fissa dei wait del percorso più lungo del caso
  (None se una durata non è leggibile).

### datagen.py

```python
def generate_data(package: Package, model: TableModel, coverage: CoverageResult,
                  settings: Settings, generation_time: datetime) -> GeneratedData
```

Implementa esattamente le "Data generation rules" della spec, in ordine di priorità. Compila anche `Scenario.key` e
`Case.contact_keys` sul posto, e i record `Contact`.

- Righe: una riga di contatto per ogni contatto in ogni tabella selezionata a livello di contatto; una riga per scenario in ogni
  tabella a livello di scenario; le righe della tabella di ingresso sono calcolate dalle espressioni `select` della entry query sulle
  righe sorgente (`SelectColumn` -> valore della riga sorgente collegata; `SelectLiteral`; `SelectCurrentDate` ->
  ora di generazione nel formato .NET convertito in strftime, formato `None` -> `Settings.date_format`;
  `SelectConcat`; `SelectUnsupported` -> placeholder e una voce in `placeholders`). Le colonne di ingresso che non sono nella
  select ricevono il default dello schema (Boolean `False` quando il default lo dice), altrimenti None; una colonna che non ammette null riceve
  un placeholder e una voce in `placeholders`. `select_all` copia le colonne con lo stesso nome.
- Gruppi di chiavi: gruppo del contatto -> `settings.contact_key(n)`, gruppo dello scenario -> `settings.scenario_key(run_order)`,
  altri gruppi -> `TST_<run>_K<n>` condiviso nel gruppo.
- Campi letti dagli split: il valore del caso o dello scenario, esattamente com'è. Null -> None.
- Filtri della entry query: `EqualityFilter` -> quel valore (Boolean scritto `True`/`False`); `DateBoundFilter`
  start -> inizio della finestra, end -> fine della finestra; `IsNullFilter` -> None. Il campo di attivazione sotto
  "one active scenario" è `True` solo per `run_order == 1`.
- Campi dei wait by attribute: ora di generazione + `wait_offset_hours`, oppure + `wait_offset_before_engagement_hours`
  quando un caso di quello scenario attraversa il wait e poi ha un'azione manuale (per scenario per i campi
  a livello di scenario, per contatto per quelli a livello di contatto).
- Campi email (tipo `EmailAddress`, oppure con nome `Email*`): inbox reale con plus-addressing
  `local+<contact key>@domain` per i contatti di un caso con un'azione manuale quando `settings.real_inbox` è impostato,
  altrimenti `<contact key>@example.invalid`. Campi phone: `fake_phone` (hook per il telefono reale: nessuna attività supportata invia
  a un telefono, quindi lascia `uses_real_phone=False` e dillo nel docstring del modulo).
- Altri campi: Text `TST_<field>_<n>` tagliato alla lunghezza (`n` = numero di riga in quella tabella), Boolean default o
  `False`, Date ora di generazione, Number/Decimal `str(n)`, Locale `en-US`.
- `scenarios_rows`: una per scenario: `scenario_key, run_order, has_manual_action, is_statistical, case_count,
  contact_count, first_contact_key, last_contact_key, activate` più una colonna per ogni campo dello scenario `Table.Field`.
- Riga assente (sezione "Extensions beyond the pilot"): un contatto il cui caso ha la riga assente in una tabella di
  `CoverageResult.row_fields` non ha righe nel CSV di quella tabella; le righe rimaste sono numerate di nuovo. Le
  righe di ingresso e le altre tabelle non cambiano. Il valore `GENERATED_KEY` della coverage è la chiave generata.
- Un ingresso che nessuna automation alimenta (Run Once, pianificazione ricorrente, form, API event) non ha una entry
  query per costruzione: le righe di ingresso vengono dalle regole generali senza warning.
- Finestre delle azioni manuali: la finestra si apre quando l'email può arrivare, cioè l'ora di generazione più i
  wait prima dell'email (`email_not_before`, quando la ritardano). Per ogni `ActionWindow` con un wait by attribute,
  `deadline` = il momento in cui i contatti raggiungono lo split al più presto: dall'arrivo dell'email, un wait by
  duration aggiunge la sua durata, un send-time wait niente, un wait by attribute trattiene fino alla data scritta
  nelle righe del caso, o lascia passare subito se quella data è già passata. Senza ritardi è la data scritta più i
  wait a durata fissa dopo di essa, come prima. Senza wait by attribute la finestra è `minutes` minuti dall'arrivo
  dell'email. `short` quando la finestra è sotto un'ora (scadenza a meno di un'ora dall'arrivo dell'email, o
  `minutes` < 60), con un warning per email ed engagement split che, quando l'email è ritardata, dice quando
  arriva.
  `Case.not_before` = la stessa data per l'intero percorso del caso.

### outputs.py

```python
def write_table_csv(data: TableData, table: Table) -> bytes      # UTF-8, comma, header in schema order, \n
def write_scenarios_csv(rows: list[dict]) -> bytes
def write_report(result: GenerationResult) -> str                # markdown, see spec "Outputs"
def write_testbook(result: GenerationResult) -> bytes            # openpyxl, spec columns; priority 4
def build_zip(files: dict[str, bytes]) -> bytes
```

CSV: `csv.writer` con quoting di default, `lineterminator="\n"`, None -> cella vuota. Sezioni del report, in ordine:
riepilogo (scenari, casi, contatti; branch coperti / coperti con azione manuale / non coperti), trust check
(ognuno con superato/non superato e dettagli), tabelle (nome, ruolo, livello, CSV sì/no, motivo), scenari (chiave, valori, casi),
branch non coperti (activity, outcome, motivo), elementi non supportati, warning e pattern sospetti,
assunzioni, impostazioni usate.

Regole aggiunte dalla sezione della spec "Extensions beyond the pilot" (12 ottobre 2026):

- Procedura di ingresso secondo `EntryEvent.kind` (`entry_kind(package)`), nella sezione "Come eseguire i test" del
  report e nelle colonne Pre conditions e Detailed steps del testbook:

  | Entry source | Testi |
  | --- | --- |
  | `ENTRY_AUTOMATION` (il pilota) | Invariati |
  | `ENTRY_RUN_ONCE` | Importa ogni CSV, poi attiva il journey; entrano solo le righe presenti all'attivazione; la data extension di ingresso deve contenere solo righe di test; un nuovo tentativo richiede una nuova versione del journey e un nuovo run identifier; tutti gli scenari entrano insieme. Se "un solo scenario attivo" è comunque attivo, il report dice di generare di nuovo i dati senza |
  | `ENTRY_SCHEDULED` | Procedura da verificare: i contatti entrano alla valutazione successiva della data extension |
  | `ENTRY_OTHER` | Il test si fa su una copia del journey con entry source una data extension con le stesse colonne; il CSV di ingresso va in quella data extension (`entry_source_text(package)` nomina la entry source) |

- `Journey.entry_mode` `OnceAndDone` o `SingleEntryAcrossAllVersions`: il report e le Pre conditions dicono che ogni
  tentativo richiede un nuovo run identifier. Filtro di ingresso, goal ed exit criteria: una frase nel report e
  nella colonna Note.
- Riga presente o assente: nella Description il profilo dice "riga presente in T" o "nessuna riga in T" al posto del
  campo collegato (`GENERATED_KEY` o None) e dei campi di una riga assente; le Pre conditions dicono "Nessuna riga in
  T per questi contatti".
- Percorso non noto (`Case.unknown_from`, `RandomBranch.unknown_from`): il percorso atteso si ferma all'attività, il
  risultato atteso dice "Percorso non noto da <attività> in poi, perché <motivo>" e non nomina mai quell'attività come
  ultima raggiunta; nel report la colonna Ultima attività dice "non nota da <attività> in poi".
- `unsupported_warning(result)`: la frase con cui report.md (in un blocco "Attenzione" prima del Riepilogo), la
  pagina e la riga di comando si aprono quando branch non coperti hanno un motivo che viene da un elemento non
  supportato (`attività non supportata`, `branch non supportato`, `a valle di un elemento non supportato`). None
  altrimenti.
- Scadenza delle azioni manuali da `Case.action_windows`: con un wait by attribute, `ActionWindow.deadline`
  ("Prima del ..."); senza, "Entro N dall'arrivo dell'email" quando i wait hanno una durata fissa; altrimenti "Appena
  arriva l'email". Con `ActionWindow.short` la Note dice che la finestra è sotto l'ora (e, con
  `email_not_before`, quando arriva l'email). Un caso senza finestre (un
  `Case` costruito a mano) usa ancora l'ultimo wait by attribute tra email e split.
- Durata minima: il passo dei wait del testbook aggiunge "Durata minima del test: ..." da
  `Case.min_duration_minutes` e da `Case.not_before` quando questa data è dopo l'ora di generazione.
- Assunzioni: il report elenca solo quelle che l'export attiva, ciascuna con il suo segnale (un decision split con
  due condizioni, un confronto di testo, un wait by attribute, campi Date o Boolean nei CSV, un engagement split,
  l'automation del pilota, colonne ignorate della entry query, la pianificazione della data extension, `OnceAndDone`,
  `IsNull` o `IsNotNull` sulla chiave del contatto, una tabella collegata al contatto solo da una relazione
  group-to-set, un confronto tra attributi, una tabella dal lato "Many" di una relazione, un Update contact che
  scrive in una tabella con la variabile "riga presente / riga assente"). Righe di docs/assumptions.md: da 1 a 6, da
  23 a 28 e da 40 a 46.
- Un branch non supportato (`<split>/<outcome>` in `CoverageResult.unsupported`) è descritto come branch dello split,
  non come attività. Un elemento di `CoverageResult.unsupported_reasons` è descritto con il suo motivo: un Update
  contact come "non supportato per una parte dei contatti", un branch che ordina un campo di testo come branch non
  supportato i cui branch vicini restano valutati.
- Nel passo dei wait del testbook, per un caso il cui percorso non è noto da un'attività in poi, la durata vale solo
  per la parte nota del percorso, e il testo lo dice.

### checks.py (lead)

Rilegge i byte dei CSV scritti ed esegue i cinque trust check della spec. Restituisce `list[CheckResult]`.

Regole aggiunte dalla sezione della spec "Extensions beyond the pilot":

- Join: un contatto il cui caso ha la riga assente (`CoverageResult.row_absent`) non deve avere righe in quella
  tabella, e solo questi contatti possono non averne. Il confronto tra i membri del gruppo di chiavi del contatto
  ignora le loro chiavi mancanti in quella tabella.
- Simulazione dai file: `graph.Walker(..., row_fields=TableModel.existence_tested)`; la lookup legge i file, cioè i
  valori all'ingresso, quindi i Journey Data restano la fotografia e un Update contact su una tabella con la riga
  assente la inserisce.
- Entry query: senza entry query il check passa con una sola nota. Per una entry source che nessuna query popola (Run
  Once, pianificazione ricorrente, form, API event) la nota comincia con `NOT_APPLICABLE` ("non si applica").

## Esempi

`examples/<name>/package.json` è un export di package con la stessa forma del pilota (vedi
`docs/export-format.md`), abbastanza piccolo da poterlo ragionare a mano. `examples/<name>/expected.json` è la
risposta ricavata a mano dalla spec. `examples/<name>/README.md` dice cosa verifica l'esempio e come è stata ricavata la
risposta attesa, passo per passo, citando la regola della spec usata.

Chiavi di `expected.json` (tutte facoltative tranne `branches`):

```json
{
  "journey_name": "...",
  "activity_count": 5,
  "csv_tables": ["DE_Entry", "DE_Master"],
  "table_levels": {"DE_Master": "contact", "DE_Config": "scenario"},
  "branches": {
    "SPLIT-1/path_a": {"status": "covered"},
    "SPLIT-1/remainder_path": {"status": "not covered", "reason_contains": "unreachable"}
  },
  "scenario_count": {"min": 1, "max": 2},
  "case_count": {"min": 2, "max": 2},
  "contact_count": 6,
  "cases": [
    {"scenario_values": {"DE_Config.CHANNEL": "Email"},
     "contact_values": {"DE_Master.Perm": "True"},
     "end_activity": "EMAIL-1",
     "manual_actions": ["open"],
     "contact_count": 3}
  ],
  "unsupported": ["SOMETHING-1"],
  "warnings_contain": ["Email", "EMAIL"],
  "checks_pass": true,
  "settings": {"real_inbox": "tester@example.com", "case_insensitive_text": false}
}
```

`branches` deve elencare ogni outcome di ogni decision, engagement e random split (`"<activity key>/<outcome key>"`).
Un caso elencato deve esistere (valori confrontati come stringhe CSV; `end_activity` è l'ultima chiave di activity;
`manual_actions` è la lista ordinata dei tipi). `tests/test_examples.py` esegue ogni esempio nella
pipeline e confronta. Il pilota è `examples/pilot/` con `"package": "../../fixtures/CJ template complesso-v1.json"`.

Chiavi aggiunte per le estensioni oltre il pilota (spec, "Extensions beyond the pilot"), tutte facoltative:

| Chiave | Dove | Significato |
| --- | --- | --- |
| `cases[i].contact_constraints` | caso | `{"Tabella.Campo": {"not_null": true, "not_in": [...], "gt": n, "lt": n}}`: il valore del caso non è vuoto, non è tra quelli elencati, ed è un numero strettamente maggiore di `gt` e minore di `lt` (mai sull'estremo). Per i valori che la spec non fissa, solo l'intervallo. |
| `cases[i].has_row` | caso | `{"Tabella": true/false}`: ogni contatto del caso ha una riga in quella tabella (`true`), oppure nessuno ne ha (`false`). Una riga appartiene al contatto quando una sua cella contiene la chiave del contatto. Serve per la riga assente. |
| `report_contains` | esempio | testi che report.md contiene, senza distinguere maiuscole e minuscole |
| `report_not_contains` | esempio | testi che report.md non contiene |
| `report_head_contains` | esempio | testi presenti nell'apertura di report.md, cioè prima della seconda sezione `##` |
| `testbook_contains` | esempio | testi presenti in una cella di testbook.xlsx (il runner lo scrive con `outputs.write_testbook`) |
| `warnings_not_contain` | esempio | testi che nessun warning contiene (stessi warning di `warnings_contain`) |
| `csv_no_space_cells` | esempio | `true`: nessuna cella dei CSV contiene solo spazi (un null è una cella vuota) |

Il valore di `reason_contains`, e ogni elemento di `warnings_contain`, `report_contains`, `report_head_contains` e
`testbook_contains`, può essere una lista: basta che compaia uno dei suoi testi. Si usa quando la spec fissa il
concetto ma non la parola (per esempio il nome di un'attività oppure la sua chiave).

## Estensioni del passo 3: journey che dipendono dal tempo (14 ottobre 2026)

Regole della sezione della spec "Extensions for time-dependent journeys (step 3, 14 October 2026)". `model.py` è stato
esteso in modo additivo (nuovi campi con valore di default, nessuna firma cambiata): `RelativeDate`, `Condition.relative`
e `Condition.path`, `KIND_SMS`, `Activity.wait_offset_amount/wait_offset_unit/wait_time_of_day/wait_path/sms_asset_id/
sms_name`, `JourneyFilter` e `Journey.exits/goals/default_mobile_field`, `Table.managed`, `TableRef.optional`,
`DateOffsetFilter`, `ROLE_LINK`, `TableModel.aliases`, `TimelinePoint`, `Case.timeline/exit_by_criteria/validity_days/
sms_sent`, `RandomBranch.timeline/exit_by_criteria`, `CoverageResult.exit_targets/validity_days/perishable`,
`Settings.entry_time/min_validity_days`.

### criteria.py

- Un valore scritto `;<segno>;<n>;<unità>` diventa `Condition.relative` (`RelativeDate`), `value` resta com'è scritto.
- Operatori aggiunti alla tabella `OPERATORS`: `After`, `AtOrAfter`, `Before`, `AtOrBefore` (come gli operatori di
  ordinamento) e `IsNot` (negazione di `Is`: vero anche su null). `NotEqual` resta falso su null.
- `evaluate_condition(cond, value, field_type, case_insensitive, now=None)`: con `relative` il riferimento è `now`
  spostato dal valore relativo (`shift_date`); confronto sulla data per days/weeks/months/years, su data e ora per
  hours/minutes. `now` mancante con un valore relativo -> `UnsupportedOperator`. Helper pubblici: `shift_date(moment,
  rel)`, `relative_units()`, `is_day_level(rel)`.
- `Condition.path` come lo scrive criteria.py contiene gli ID delle relazioni (`groupToSetRelationshipId` di `UiMetaData`,
  poi i `RelationshipID` degli `<AttributePath>`); loader.py li sostituisce con i nomi delle tabelle sul percorso.

### loader.py

- `SMSSYNC` -> `KIND_SMS` (`sms_asset_id`, `sms_name`); non è più un tipo sconosciuto.
- Wait by attribute: `wait_offset_amount/unit` da `waitDuration/waitUnit` (anche negativi), `wait_time_of_day` da
  `specifiedTime`, `wait_path` dai segmenti dell'espressione tra `Contact.Attribute` e il set del campo. Un WAIT senza
  `metaData.uiType` viene riconosciuto dall'espressione o dal prefisso della chiave.
- `Condition.path`: dagli ID delle relazioni ai nomi delle tabelle tra il contatto e la tabella della condizione (dalla
  più vicina al contatto, esclusa la tabella della condizione).
- Exit criteria e goal: `Journey.exits` e `Journey.goals` (`JourneyFilter` con la formula mappata come quella di uno
  split e `unsupported_reason` con le stesse regole). Warning: criteri valutati alla fine di ogni wait quando sono tutti
  supportati; "non modellati, perché ..." altrimenti. I goal restano non modellati.
- `Journey.default_mobile_field` da `defaults.mobileNumber`, come `default_email_field`.
- Condizione con valore relativo su un campo non Date, o con unità non supportata -> branch non supportato.
- `Table.managed` dalle issue `NO_DEPLOY` / `ManagedDataExtension`, con un warning.

### sqlparse_lite.py

- `DateOffsetFilter(column, days)` per `CAST(col AS DATE) = CAST(DATEADD(day, N, GETDATE()) AS DATE)` e le varianti
  della spec; solo l'uguaglianza. `TableRef.optional` per LEFT/RIGHT/FULL JOIN (letti come inner join, con warning).
- Pattern sospetto: due colonne della stessa tabella scambiate di nome nella SELECT.

### tables.py

- Tabelle di collegamento (`ROLE_LINK`): ogni tabella di `Condition.path` e di `Activity.wait_path` è selezionata, con
  il motivo "sul percorso tra il contatto e <tabella> letta da <attività>". Senza percorso nell'export, una tabella
  letta e non collegata prende la catena di relazioni più corta dalle tabelle collegate al contatto (warning); senza
  catena vale la regola di prima (riga condivisa, assunzione).
- `TableModel.aliases`: gruppi di campi non chiave che una query seguita copia come colonna semplice (`SELECT M.X` nella
  tabella di ingresso): stessa variabile per la coverage. Le copie di campi chiave restano nei `key_groups`.
- Una sorgente `optional` resta selezionata; il motivo cita il LEFT JOIN. Una tabella `managed` resta selezionata; il
  motivo dice che la tabella è sincronizzata dal CRM e che il CSV si importa come gli altri. `DateOffsetFilter` non è un campo di attivazione.

### graph.py

- `WalkState.clock` (parte da `entry_time`, che `Walker` riceve; default l'ora di generazione), `WalkState.timeline`
  (un `TimelinePoint` per `Step`, stesso ordine), `WalkState.sms_sent`, `WalkState.exit_by_criteria`,
  `STOP_EXIT_CRITERIA`.
- Decision split ed exit criteria valutano con `now = state.clock` (`graph.evaluate(..., now=)`).
- Wait by duration: `clock += durata` (`WAIT_UNIT_MINUTES`). Wait by attribute: valore del campo dal lookup (Contact
  Data con le scritture, Journey Data dalla fotografia); se UNKNOWN e il campo è nei domini della coverage ->
  `STOP_UNDETERMINED` con `pending_field`; se UNKNOWN e nessuna condizione lo legge (campo "solo wait") il wait finisce
  subito (il valore lo scrive datagen con la regola delle date dei wait); null -> finisce subito (assunzione); altrimenti
  `clock = max(clock, wait_end(activity, data))` dove `wait_end` applica l'offset (`shift_date` con l'unità del wait) e
  `wait_time_of_day` quando c'è. Send-time wait: `+0`.
- Alla fine di ogni wait: gli exit criteria supportati vengono valutati con il clock; se uno è vero il percorso finisce
  lì (`Step(wait, None)`, `exit_by_criteria = wait`, `STOP_EXIT_CRITERIA`); UNKNOWN -> `STOP_UNDETERMINED` sul campo.
- Email e SMS registrati con il clock (`emails_sent`, `sms_sent`); `{{DateTime.Now}}` scritto da un Update contact
  vale il clock.
- Helper pubblici: `wait_end(activity, value: datetime) -> datetime`, `fixed_delays(journey) -> dict[str, set[float]]`
  (per ogni attività, le somme in giorni dei wait by duration su ogni percorso dall'inizio; i wait by attribute e STO
  contano 0; al massimo 32 valori per attività, con warning).

### coverage.py

- Campi: anche quelli letti dagli exit criteria supportati e i campi dei wait by attribute che una condizione legge.
  Alias (`TableModel.aliases`): ogni gruppo è una variabile, rappresentata dal campo della tabella sorgente (non la
  copia di ingresso); il lookup del walker risolve gli alias.
- Dominio di un campo Date con condizioni relative o con un wait con offset: punti di taglio in giorni = `{n + W}` per
  ogni condizione relativa e `{-O + W}` per ogni wait con offset `O`, con `W` i ritardi fissi con cui si arriva
  all'attività (`graph.fixed_delays`); candidati = `T0` spostato di ogni punto di taglio e di ogni punto ±1 e ±2 giorni
  (ora del giorno di `T0`), più i valori assoluti nominati, più null se ammesso; i candidati con lo stesso vettore di
  verità su ogni (condizione, W) sono una classe. Validità di un candidato: il numero di giorni interi di cui si
  può spostare in avanti l'ora di ingresso prima che un vettore di verità cambi, calcolata rivalutando le
  condizioni con l'ingresso spostato di 1, 2, ... giorni (fino a 366: oltre, il valore non è deperibile e la
  validità è None). Per ogni classe resta un candidato, scelto in quest'ordine: (1) validità ≥
  `Settings.min_validity_days` (tutti, se nessuno la raggiunge); (2) il wait by attribute sul campo finisce prima
  (solo quando un wait con offset legge il campo); (3) non su un punto di taglio; (4) validità più lunga;
  (5) data più vicina; null non rappresenta mai una classe che ha un membro non null. `Case.validity_days` è None
  quando nessun valore del caso è deperibile. I campi "solo wait" senza condizioni restano fuori dalla coverage,
  come prima.
- Target "exit criteria met": un bit per ogni exit criterion supportato nel set cover (`exit_targets`,
  `activity_key` = chiave del criterio, `outcome_key` = `"met"`, `activity_kind` = `"exit"`), coperto dai casi con
  `exit_by_criteria`.
- `Case.timeline`, `exit_by_criteria`, `sms_sent` dalla foglia; `Case.validity_days` = minimo dei margini dei valori
  del caso che ne hanno uno; `CoverageResult.validity_days` = minimo sui casi; `perishable` lo spiega.
- Motivi dei branch non coperti: nuova classe "tutti i contatti che arrivano alla fine di <wait> escono per i criteri
  di uscita (<descrizione>)".

### datagen.py

- Alias: il valore del rappresentante viene scritto in ogni membro (le copie di ingresso arrivano comunque dalla
  SELECT). Una colonna di ingresso letta da uno split e calcolata da una `SelectUnsupported` prende il valore del
  caso, con una nota invece di un placeholder.
- `DateOffsetFilter`: ora di generazione spostata di `days`. Campi "solo wait": regola di prima; le scadenze delle
  finestre manuali usano `graph.wait_end`.
- SMS: nei casi con `sms_sent`, il primo contatto riceve `settings.real_phone` nella colonna
  `journey.default_mobile_field` della tabella di ingresso e nel campo sorgente da cui la SELECT la copia
  (`Contact.uses_real_phone`); senza numero reale, `fake_phone` e un warning.
- Le tabelle `managed` ricevono comunque il CSV; `GeneratedData.warnings` lo dice.

### checks.py

- Il walker della simulazione riceve lo stesso `entry_time` e valuta gli exit criteria: un percorso che esce per i
  criteri finisce con `Step(wait, None)`, come nella coverage.
- Check della entry query: `DateOffsetFilter` verificato sulla parte data; una colonna letta da uno split e calcolata da
  un'espressione non compresa è "non verificata" (nota, non errore); un default `GetDate()` ammette qualsiasi data;
  una sorgente `optional` è una nota.

### outputs.py e app.py

- report.md: nel blocco "Attenzione" la scadenza di attivazione quando `validity_days` non è None; sezione "Criteri di
  uscita" (descrizione, modellati o no, casi che escono, stato del target); le tabelle di collegamento e la nota
  sulle data extension sincronizzate (il loro CSV è tra quelli da importare) nella sezione Tabelle e in "Come eseguire i test"; i valori deperibili tra i warning.
- testbook.xlsx: nei passi la timeline ("Giorno N (data e ora): ..."), il passo dell'SMS e il suo risultato atteso,
  l'uscita per i criteri nel risultato atteso, la scadenza di attivazione nelle Note, la nota che dice che un branch è
  deciso sull'estremo di un confronto sul giorno.
- app.py: impostazione "Validità minima dei dati (giorni)"; la scadenza di attivazione nel riepilogo; gli exit
  criteria nel passo di revisione. `scenarios.csv` non cambia.

### Esempi del passo 3: chiavi aggiunte a expected.json

Lette da `tests/test_examples.py` (`compare_step3`, `case_matches_step3`): `table_roles` (tabella -> ruoli che deve
avere), `csv_rows` (tabella -> numero di righe del CSV), `csv_values` ("Tabella.Campo" -> insieme dei valori distinti,
null compreso), `exit_targets` ("CHIAVE/met" -> {status, reason_contains}), `validity_days` (numero o null); per
caso: `exit_by_criteria`, `sms_sent`, `validity_days`, `timeline` (attività -> "YYYY-MM-DD HH:MM"),
`contact_values_any_of`, `contact_rows` (una voce per contatto, in ordine di chiave: "Tabella.Campo" -> valore).
