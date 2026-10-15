# Avanzamento

Si riparte da questo file. Spec: `docs/spec.md`. Contratto: `docs/plan.md`. Disegno del passo 3: `docs/design-passo3.md`.

## Ripartenza del passo 3 (journey che dipendono dal tempo): stato al checkpoint del 14 ottobre 2026, sera

Passo 3 consegnato: il generatore gira sui sei export della famiglia RET_99 (2W, 3m, 4W, HC, 5d, 6w; dati del cliente,
MAI nel repository), sul pilota (CSV e `scenarios.csv` identici byte per byte) e sui quattro export del passo 2 (stessi
numeri e stessi stati di prima). Report finale del passo: `docs/final-report-passo3.md`. Regole: sezione della spec
"Extensions for time-dependent journeys (step 3, 14 October 2026)"; contratti: `docs/plan.md`, "Estensioni del passo
3"; perché: `docs/design-passo3.md`; assunzioni 47-61: `docs/assumptions.md`.

Materiale fuori dal repository (da ricaricare in una sessione nuova): i sei export RET_99 e i quattro del passo 2, le
risposte attese derivate a mano per cinque dei sei export (`target_<nome>.json`, derivate da un agente, senza il passo
di confutazione per restare nel budget). Gli script `tools/dump_cj.py`, `tools/graph_cj.py` e `tools/run_cohort.py`
ricostruiscono dumps, grafi, baseline e confronto (`CJ_SCRATCH`, `CJ_RUNS`, `CJ_REPO` nell'ambiente).

Risposte del team del 14 ottobre sera applicate (`docs/final-report-passo3.md`, "Risposte del team"): date relative sul giorno confermate, data extension sincronizzate importate come gli altri CSV, validità sul valore, target dei criteri di uscita coperto, errore del journey 4 settimane segnalato al cliente.

Suite: ~1000 test, 79 esempi (64 dei passi 1 e 2, 15 del passo 3), pilota: tutto verde. Prossimi passi possibili, da
decidere con l'utente: le domande in `docs/final-report-passo3.md`; la validità misurata sul percorso del caso (oggi
è misurata sul valore, lettura prudente); il set cover che sui sei export usa da 1 a 2 casi più del minimo derivato
a mano; la verifica delle assunzioni 47-61 con un run reale.

## Fatto

- Repository impostato: spec copiata in `docs/spec.md`, export pilota in `fixtures/`, vecchio tool conservato solo nella storia di git (commit 83f0f65) come riferimento.
- `cjhelper/model.py`: il modello dati condiviso e il contratto dei moduli.
- `docs/plan.md`: responsabilità dei moduli e firme delle funzioni. `docs/export-format.md`: forma dell'export per gli esempi fatti a mano.

- `loader.py` + `criteria.py` (197 test), `sqlparse_lite.py` (144 test), `tables.py` (27 test): consegnati e rivisti.
- `graph.py`, `coverage.py`, `checks.py`, `pipeline.py`, `__main__.py` (lead): la prima esecuzione integrata sul pilota supera tutti i 7 trust check.
  `python -m cjhelper "fixtures/CJ template complesso-v1.json" --out output --inbox tester@example.com` richiede circa 30 s.

- `datagen.py` + `outputs.py` (88 test): consegnati. L'intera suite (562 test) passa.
- `examples/`: 17 esempi con risposte attese ricavate a mano (15 piccoli, 2 varianti del pilota con conteggi minimi dimostrati a mano);
  tutti passano in `tests/test_examples.py`. Problemi trovati nel ciclo e corretti: il controllo di coverage leggeva male lo stato voluto "nessuna inbox reale";
  un operatore di condizione sconosciuto bloccava la run (ora lo split è non supportato e i suoi branch non coperti); una condizione su un campo
  assente dallo schema veniva interpretata come testo (ora è non supportata); un campo fissato da un filtro di ingresso non viene più provato con altri valori.
- `docs/assumptions.md`: revisione Salesforce, 35 assunzioni da verificare, 16 pattern sospetti, un piano di run di verifica di un giorno.
  Recepito: i filtri delle query dell'automation seguita sono applicati alle righe generate (RUN_TIME resta vuoto).

- Technical writer: README.md, report.md con una sezione su come eseguire i test e il raggruppamento per cause a monte, testo del testbook. Fatto.
- Pagina Streamlit `cjhelper/app.py` (28 test), screenshot giudicati dal lead; revisione UX fatta.
- Ciclo di revisione di fine livello, parte 1 (revisori di coverage e di generazione dei dati, 34 finding): corretti in tre lotti, vedi i
  commit "Review fixes, batch 1/2/3". In sintesi: valori 'other' che rispettano la lunghezza, campi email e phone letti dagli split, campi
  chiave letti dagli split, outcome non supportati invece di split interi, remainder valutato per ultimo, random split annidati,
  casi statistici con azioni manuali, branch prima di un random split, SELECT * con elementi extra, NULL nella select,
  token di data .NET non supportati, colonne di ingresso copiate da colonne a livello di scenario, i wait prima dell'email non ricevono tempo extra,
  placeholder univoci per le chiavi primarie, chiavi del contatto di tipo email, colonne `active_at_start` e `activation_field` in scenarios.csv.

## Piano residuo

Niente in programma. Possibili passi successivi, da decidere con l'utente: verificare le assunzioni su Salesforce con una piccola run reale
(docs/assumptions.md), i finding rimandati qui sotto, le letture di design ancora aperte (docs/final-report.md).

Decisioni prese dall'utente la sera dell'11 ottobre:

- Il conteggio minimo dei casi è confermato: prima il minor numero di scenari, poi il minor numero di casi.
- Il default per maiuscole e minuscole resta quello che le distingue (case-sensitive), finché l'utente non ha letto la documentazione di Salesforce.
- Tutto è in italiano, tranne i termini inglesi del team e gli identificatori. `docs/spec.md` resta in inglese per ora.
- La procedura di ingresso è l'import diretto dei CSV (durante il test non si esegue nessuna automation). Un altro agente la sta applicando a report e testbook.

Fatto la sera dell'11 ottobre: traduzione in italiano dei messaggi del generatore e dei file prodotti (quattro agenti Opus su moduli
disgiunti, glossario in docs/brand.md "Glossario"; pattern in outputs.py riallineati ai nuovi testi; un unico
prefisso "pattern sospetto:"), tests/test_checks.py (18 test), RB-5, RB-15 (prefisso unico delle query). La suite veloce (~700 test),
i 39 esempi e il test di accettazione del pilota passano; la run da riga di comando sul pilota dà 7/7 check, 23/33/146 come prima.

Finding rimandati (non nel pilota): RB-1/RB-10 (mappatura di Journey Data wait e condizioni), RB-2 (warning sui criteria della entry source),
CK-9 (la parte locale dell'inbox non è controllata); tutti i finding bassi del triage.
Instradamento: Opus per il codice e il testo italiano, Sonnet per i controlli e il lavoro meccanico, Haiku per i compiti banali.

## Stato dopo il merge della PR #1 (upstream main)

- PR #1 unita; upstream ha aggiunto su main: brand Lutech (tema, Montserrat, logo, docs/brand.md), pagina in italiano con
  stepper e card dei step comprimibili (cjhelper/app.css), README in italiano, sezione di CLAUDE.md su aspetto e lingua.
  Sono cambiati solo cjhelper/app.py, app.css, config.toml, README.md, tests/test_app.py, docs/brand.md e gli asset: nessuna sovrapposizione con
  il generatore. Il branch è stato portato avanti in fast-forward su main; l'aspetto della pagina non va cambiato.
- Correzioni del triage committate sopra (4e5e01a): ogni query che popola la tabella di ingresso viene seguita (RB-4/CP-4), forma di esclusione
  con LEFT JOIN nel controllo di ingresso (RB-13), i trust check leggono i file e seguono i collegamenti degli scenari (CK-1, CK-4, CK-5, CK-6, CK-8,
  CK-11, CK-12). CK-18 (tests/test_checks.py) e RB-5 (statsTypeId sconosciuto: entrambi i branch non coperti) fatti con la traduzione.

## Regole di lavoro dell'utente

- Subagent su Opus con effort massimo per il lavoro che richiede giudizio; Sonnet per compiti più semplici, anche lunghi, supervisionati, e per i controlli semplici.

## Letture di design del lead (non assunzioni su Salesforce)

- Casi dentro uno scenario: la spec chiede "the smallest set of paths that crosses every branch" e "the fewest scenarios".
  Il generatore sceglie il minor numero di scenari, poi il minor numero di casi al loro interno (un secondo passo greedy). Non aggiunge ogni
  classe di contatti a ogni scenario. Sul pilota dà 23 scenari / 33 casi / 146 contatti (distingue maiuscole e minuscole) e
  41 / 59 / 224 (non distingue maiuscole e minuscole); il tool precedente riportava 41 / 148 / 494. Il numero di scenari coincide, il numero di casi è inferiore
  per scelta di design. Da confermare con l'utente.
- Le classi di contatti raggruppano le combinazioni di valori a livello di contatto in base alla verità di ogni formula di outcome puramente a livello di contatto (e di ogni
  condizione a livello di contatto di una formula mista): 8 classi sul pilota, come dice la spec.
- Il valore di testo "other" è `OTHER` (oppure `OTHER_2`, ... quando è già preso); i Boolean prendono sempre entrambi i valori; null viene aggiunto quando lo schema lo ammette.
- Caso statistico: gli engagement split a valle del random split prendono il branch No (in un caso statistico non è richiesta nessuna azione manuale).

## Prossimi passi

- Vedi "Piano residuo" sopra. Dopo: consegna con il report finale; le assunzioni in docs/assumptions.md restano da
  verificare con una piccola run reale.

## Da riferire all'utente (correzioni alla spec suggerite dalla revisione Salesforce, non cambiate nel codice)

- Procedura di ingresso: la revisione dice che importare solo il CSV di ingresso non fa entrare nessuno; l'automation (con il suo step di ingresso nel journey) deve girare,
  e una run completa esegue anche la entry query (duplicando le righe di ingresso) e lo step di verifica. La spec presenta l'import diretto
  come via normale. Il generatore scrive comunque tutti e quattro i CSV, quindi entrambe le procedure sono possibili; il report le elenca come alternative.
- Date dei wait: il default della spec (ora di generazione, "so the wait ends at once") non è garantito se Salesforce legge la data del CSV in
  un altro fuso orario (CST); un'attesa con offset negativo sarebbe più sicura. L'impostazione resta al default della spec.
- Le maiuscole e minuscole spostano 46 branch tra coperti e non coperti sul pilota; il default dell'impostazione (distingue maiuscole e minuscole) è quello della spec.

## Domande aperte

- Pilota con il confronto di default che distingue maiuscole e minuscole: 137 dei 259 branch degli split non sono raggiungibili perché `CHANNEL2 Equal 'EMAIL'` /
  `CHANNEL3 Equal 'EMAIL'` non possono mai essere veri sui percorsi che hanno già superato `CHANNEL2 Equal 'Email'`. La riga di accettazione della spec dice che i
  quattro branch Yes degli engagement split dopo i channel 2 e 3 non sono raggiungibili "because all six engagement splits reference the first email";
  questo è il motivo solo quando i confronti di testo non distinguono maiuscole e minuscole (allora 91 branch restano non raggiungibili). Le due letture danno lo stesso stato.
- `RUN_TIME` (Date che ammette null, senza condizione né filtro della entry query) resta vuoto, e non riceve l'ora di generazione prevista dalla regola della spec per gli "altri campi data";
  la query di aggiornamento della config filtra `WHERE RUN_TIME IS NULL`, quindi il generatore lo lascia vuoto. Questo differisce dalla regola della spec.

## Fatti verificati sull'export pilota

- 178 attività: 106 decision split, 34 update contact, 13 wait (3 by attribute su Config.WAIT1-3, 10 by duration di 1 minuto), 11 email (tutte lo stesso asset "Demo Sky", senza link), 7 send-time wait, 6 engagement split (tutti riferiti a EMAILV2-1), 1 random split 90/10.
- Attività iniziale: `MULTICRITERIADECISIONV2-1` (Control Group Required). Tutte le 178 attività sono raggiungibili per struttura.
- La entry query collega con join Master -> Target su `ContactId = SalesforceId` e Target -> Config su `Description = CampaignName`; filtri `C.active = 'True'`, `HAS_RUN = 'False'`, intervallo di date su `START_DATE`/`END_DATE`.
- Con il confronto di default che distingue maiuscole e minuscole, `CHANNEL2 Equal 'EMAIL'` (controllo email del channel 2) non può mai essere vero su un percorso che l'ha raggiunto tramite `CHANNEL2 Equal 'Email'`, quindi gli engagement split dei channel 2 e 3 non sono raggiungibili del tutto; i loro branch Yes non sono comunque raggiungibili perché EMAILV2-1 viene inviata solo quando CHANNEL1 è Email, che instrada verso l'engagement split del channel 1.

## Estensioni oltre il pilota: differenze sul pilota e decisioni per il team

Spec, "Acceptance beyond the pilot": sul pilota CSV e `scenarios.csv` restano identici byte per byte, e le differenze di
report.md e del testbook vanno elencate per il team. Confronto con gli output di prima delle estensioni, stessa ora di
generazione (2026-10-11 12:00), stesso run identifier e stesso seed.

### Differenze sul pilota (prima delle decisioni del 13 ottobre)

- I quattro CSV e `scenarios.csv`: identici.
- report.md: identico.
- testbook.xlsx: 32 dei 33 test cambiano, solo nella colonna "Detailed steps". Il passo 3 (i wait) aggiunge la durata
  minima del caso, dalla regola "The testbook gives each case's minimum duration": "Durata minima del test: nessuna,
  perché i wait del percorso possono terminare subito." in 25 test; "Durata minima del test: i contatti non finiscono
  il percorso prima del 2026-10-12 12:00." in 3; "Durata minima del test: 1 minuto; i contatti non finiscono il
  percorso prima del 2026-10-11 12:01." in 3; "Durata minima del test: 1 minuto." in 1. Il test che non cambia
  (S19-C01) non ha wait sul percorso.

### Decisioni del team (13 ottobre)

Il team ha preso il 13 ottobre le cinque decisioni che restavano aperte sulle estensioni; sono applicate in `cjhelper/`
e nei test. La prima cambia il pilota in un punto, accettato in deroga alla regola "CSV e `scenarios.csv` identici".

1. **Preferenza "not null" anche tra scenari diversi (scenario S18): accettata.** La preferenza completa (prima not null
   e row present, poi i valori interni agli intervalli) vale anche tra scenari diversi che coprono gli stessi branch
   (`coverage.py`, `_preferred_per_mask`). Differenza approvata sul pilota: solo lo scenario `TST_R01_S18`, dove
   `DE_NCRM_AD_HOC_CONFIG.CHANNEL2` passa da vuoto a `OTHER`. Cambia una riga di `DE_NCRM_AD_HOC_CONFIG.csv` e una di
   `scenarios.csv` (colonna `DE_NCRM_AD_HOC_CONFIG.CHANNEL2`), la riga di S18 nella tabella degli scenari di report.md
   e la colonna "Description" del test S18-C01 nel testbook. Gli altri tre CSV e tutti gli altri scenari sono identici.
2. **Data del wait prima di un engagement split: aggiunge i wait prima dell'email.** La data scritta è l'ora di
   generazione più i wait a durata fissa che precedono l'email sul percorso più l'offset delle impostazioni (per il
   secondo engagement split dello stesso percorso, due volte l'offset), così la finestra manuale non viene assorbita
   (`datagen.py`, `manual_wait_delay`). Il warning resta solo quando la finestra è ancora sotto l'ora (per esempio con
   offset 0, o quando a ritardare l'email è un wait by attribute o un wait di durata sconosciuta). Il pilota non ha wait
   prima delle email con engagement split: i suoi CSV non cambiano per questo.
3. **Issue di Package Manager su contenuti usati in modo indiretto: restano solo gli errori.** Un contenuto è usato in
   modo indiretto quando il journey lo raggiunge attraverso i riferimenti `{{mcpm#/entities/...}}` (dal journey, dalle
   email inviate e da ciò che queste nominano) senza inviarlo, oppure quando è nominato da un'issue di un'email o di
   una pagina usata (un'immagine dentro un'email inviata). Su questi contenuti le issue restano nel report solo se il
   loro livello è error; le altre vengono tolte, come le issue sugli asset non usati (`loader.py`,
   `_indirect_content`). Sui quattro export dei clienti tutte le issue di questo tipo sono di livello warning: nessuna
   differenza.
4. **Livello delle issue: il più grave tra l'issue e la sua causa.** Comportamento invariato; lo fissa
   `tests/test_loader.py::test_package_manager_issues_of_level_warning_or_error_are_reported`.
5. **"valore di default".** I due messaggi del loader usano ora "valore di default", come il glossario di
   docs/brand.md. Cambia una riga di report.md del pilota (il warning su `mobileNumber`) e una del report di uno dei
   quattro export dei clienti.

Corretto nello stesso passo, dall'esempio `run_once_one_active`: con un ingresso Run Once l'impostazione "Un solo
scenario attivo alla volta" viene ignorata anche quando è forzata (spec, riga Run Once: "'one active scenario at a
time' is not offered"). Tutti gli scenari sono attivi, il report non dà la procedura a una riga attiva alla volta,
un warning spiega perché e la tabella delle impostazioni dice che non si applica. Il pilota non cambia.

Nuovo riferimento per il pilota (CSV, `scenarios.csv`, report.md e testbook), da usare nei prossimi confronti al posto
degli output di prima delle estensioni:
`python -m cjhelper "fixtures/CJ template complesso-v1.json" --out <cartella> --inbox tester@example.com --time "2026-10-11 12:00"`
con il codice del commit che applica queste decisioni. Il pilota resta a 7/7 trust check, 23 scenari, 33 casi, 146
contatti.

### Lasciato agli esempi

La spec chiede un esempio sintetico per ogni costrutto. Mancano ancora: entry da data extension con pianificazione
ricorrente, entry da API event, segnalazione di entry filter, goal ed exit criteria, "report.md elenca solo le
assunzioni che l'export attiva", "un solo scenario attivo" non offerto con Run Once, e gli operatori `NotEqual`,
`GreaterThanOrEqual`, `LessThan`, `LessThanOrEqual`, `Contains`, `BeginsWith` ed `EndsWith`. Gli esempi li scrive chi
ricava le risposte attese dalla spec senza leggere il codice.
