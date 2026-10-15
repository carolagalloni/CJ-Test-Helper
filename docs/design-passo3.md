# Disegno della versione per molti journey (passo 3)

14 ottobre 2026. Questo documento spiega *perché* il generatore cambia come cambia nel passo 3, quali classi di errore di
generalizzazione sono emerse passando da un journey (passo 1) a quattro (passo 2) a dieci (passo 3), e il processo con
cui si arriva a uno strumento che regge il prossimo journey senza sorprese. La fonte di verità resta `docs/spec.md`
(sezione "Extensions for time-dependent journeys"); il contratto dei moduli è in `docs/plan.md`.

## 1. Da dove veniamo: tre passi, tre famiglie di errori

| Passo | Journey | Cosa ha insegnato |
| --- | --- | --- |
| 1 | il pilota (178 attività, 106 split, 1 random, 6 engagement) | Il modello base: grafo, condizioni come formule, livelli contatto/scenario, set cover, trust check |
| 2 | quattro export di clienti | Journey Data (fotografia all'ingresso), riga presente/assente, operatori e tipi, entry source diverse, esiti onesti |
| 3 | sei journey di retention (famiglia RET_99) | **Il tempo**: date relative a "oggi", wait con offset sul campo, exit criteria valutati alla fine dei wait; SMS; tabelle raggiunte attraverso altre tabelle; query con CASE e LEFT JOIN; data extension sincronizzate |

Sui sei export del passo 3 il generatore del passo 2 non si rompe, ma risponde zero: 0 branch coperti su 21, 30, 41, 20,
42 e 36. Ogni split dipende da un costrutto che il generatore non conosce, e per onestà segnala tutto come non supportato.
Le cause, raggruppate per classe:

1. **Vocabolario mancante** (la classe più facile): operatori `After`, `AtOrAfter`, `Before`, `AtOrBefore`, `IsNot`;
   la forma del valore `;+;N;days`; il tipo di attività `SMSSYNC`; i filtri SQL `CAST(... AS DATE) = CAST(DATEADD(...))`.
   Rimedio: una voce in una tabella di lookup, come la spec prevede. È la stessa classe di `Between` nel passo 2.
2. **Dimensione mancante dello stato simulato** (la classe costosa): nel pilota il percorso dipendeva solo dai dati.
   Qui dipende da *quando* il contatto arriva a un'attività: "oggi più 13 giorni", "13 giorni prima della data nel
   campo", "esci se alla fine di un wait la data è passata". Lo stato della simulazione era (valori, scritture, righe);
   ora deve essere (valori, scritture, righe, **orologio**). È l'equivalente, nel passo 3, dell'errore "sessione attiva"
   del passo 1 → 2: un'ipotesi implicita del pilota (le condizioni sono statiche) presa per regola generale.
3. **Fonti di vincolo mancanti**: gli exit criteria limitano i contatti che restano nel journey (nel passo 2 erano solo
   "segnalati"); la entry query *calcola* colonne (`CASE WHEN`) che gli split leggono come Journey Data, e *copia*
   colonne (`M.X`) che gli split leggono due volte, come Contact Data e come Journey Data. Un campo è una variabile sola,
   con tutti i suoi vincoli: split, wait, exit criteria, query, schema.
4. **Percorsi tra tabelle**: `PublicHoliday_DE` è raggiunta dal contatto attraverso `PublicHoliday_Entry`. La selezione
   delle tabelle leggeva solo "chi legge" e "chi scrive", non "attraverso chi si legge". La tabella di collegamento
   senza CSV rende illeggibile la tabella letta. È l'analogo della tabella Target del pilota (sorgente ma non letta).
5. **Fiducia dei check**: il check sulla entry query falliva su colonne che non può verificare (default `GetDate()`,
   espressioni non comprese) invece di dire "non verificato". Un check che fallisce per i propri limiti insegna al
   tester a ignorarlo.
6. **Procedura**: una data extension sincronizzata dal CRM si importa come gli altri CSV (risposta del team, 14 ottobre
   2026) e le sue righe arrivano anche dalla sincronizzazione; una tabella scritta da un LEFT JOIN può
   non avere la riga. Il report deve dire cosa importare davvero.

La lezione metodologica: ogni volta che una condizione di Salesforce può dipendere da *qualcosa*, quel qualcosa deve
essere una dimensione esplicita dello stato simulato, con una regola scritta su come si evolve e su come la coverage ne
sceglie i valori. Oggi le dimensioni sono: dati del contatto (live), dati dello scenario, Journey Data (fotografia),
scritture, righe presenti/assenti, orologio, azioni manuali (open/click), random. Restano fuori, e dichiarati: goal
(nessun export ne ha), finestre dei send-time wait (contano zero), cardinalità "Many" delle relazioni, contenuto delle email.

## 2. Principi del disegno

1. **Il vocabolario è dato, non codice.** Operatori, tipi di attività, forme di valore, forme di filtro, tipi di entry
   source: una tabella di lookup ciascuno. L'ignoto viene segnalato, mai indovinato.
2. **Una variabile, tutti i suoi vincoli.** Prima di scegliere un valore, il generatore raccoglie per ogni variabile ogni
   vincolo: condizioni degli split (assolute e relative), wait by attribute (con offset), exit criteria, filtri e
   SELECT della entry query (copie = alias della stessa variabile), schema (tipo, lunghezza, null, default), gruppi di
   chiavi. Il dominio nasce da tutti insieme; la simulazione decide.
3. **Il tempo è simulato, non stimato.** Il walker porta un orologio; wait, condizioni relative, exit criteria, email,
   SMS e scadenze delle azioni manuali lo leggono. La timeline di ogni caso è un prodotto della simulazione, non un
   calcolo a parte.
4. **Valori robusti, non solo corretti.** Un valore relativo a oggi scade: il generatore sceglie, tra i valori che
   prendono gli stessi branch, quello più lontano da ogni soglia, e dichiara per quanti giorni il dataset resta valido.
   Quando un branch esiste solo sulla soglia (wait "13 giorni prima della data", poi "data ≥ oggi + 13"), lo dice.
5. **Tre gradi di onestà.** Verificato (un trust check lo ha letto dai file), assunto (elencato in
   `docs/assumptions.md` e nel report, con il segnale che lo attiva), ignoto (il percorso atteso si ferma). Un check non
   fallisce mai per i propri limiti: dice "non verificato" e perché.
6. **Il pilota è la regressione.** CSV e `scenarios.csv` del pilota restano identici byte per byte; i 68 esempi e le
   risposte derivate a mano per i sei export sono la definizione di "funziona".

## 3. Il modello teorico, in breve

Un journey è un automa deterministico sui contatti, con tre eccezioni esplicite (engagement: azione manuale; random:
statistica; elementi non supportati: percorso ignoto da lì in poi). Lo stato di un contatto è
`(valori live, fotografia all'ingresso, scritture, righe presenti, orologio)`. Ogni attività è una transizione:

- decision split: la prima formula vera, valutata su `(valori ∘ scritture, fotografia, orologio)`;
- wait: `orologio := orologio + durata`, oppure `max(orologio, data_del_campo + offset)`; poi, se gli exit criteria sono
  veri, il contatto esce;
- email / SMS: registrati con l'orologio; engagement split: scelta manuale con scadenza = orologio;
- update contact: scrittura nei valori live (mai nella fotografia); inserimento della riga se assente.

La generazione dei dati è una ricerca: trovare il minor numero di scenari (combinazioni di valori a livello di
scenario) e poi di casi (combinazioni di valori a livello di contatto e azioni manuali) tali che ogni branch
raggiungibile di ogni split, e ogni exit criterion, sia attraversato almeno una volta. Le variabili sono classi di
equivalenza di campi (alias per copia, gruppi di chiavi); il dominio di ogni variabile è un insieme finito di
rappresentanti, uno per ogni modo distinto in cui la variabile può decidere i branch (valori nominati, "altro", null,
un valore dentro ogni intervallo; per le date relative, un valore per ogni vettore di verità sulle soglie, il più
lontano dalle soglie). L'esplorazione è simbolica: il walker si ferma quando legge una variabile non ancora decisa e la
coverage dirama sui suoi rappresentanti; le classi di contatti raggruppano le combinazioni che decidono gli stessi
predicati. Il set cover greedy con seme fisso sceglie; datagen materializza le righe (chiavi, copie, regole per tipo);
i trust check rileggono i file e risimulano ogni contatto con lo stesso walker e lo stesso orologio.

## 4. Cosa cambia nei moduli

Il contratto per modulo è in `docs/plan.md`, "Estensioni del passo 3". In sintesi:

| Modulo | Cambiamento |
| --- | --- |
| model | campi additivi: data relativa, percorso di una condizione, offset e ora dei wait, SMS, exit criteria, alias, timeline, validità |
| criteria | valori relativi, quattro operatori di data e `IsNot`, valutazione con `now`, ID delle relazioni del percorso |
| loader | SMSSYNC, offset dei wait, percorsi, exit criteria e goal, default del numero mobile, data extension sincronizzate |
| sqlparse_lite | filtro "data = oggi ± N giorni", sorgenti opzionali (LEFT JOIN), pattern delle colonne scambiate |
| tables | tabelle di collegamento, alias delle copie, motivi per sorgenti opzionali e sincronizzate |
| graph | orologio, wait con offset, exit criteria alla fine dei wait, SMS, timeline |
| coverage | domini delle date relative per vettore di verità, alias come variabili, target degli exit criteria, validità |
| datagen | alias, colonne calcolate lette dagli split, numero reale per gli SMS, filtri con offset |
| checks | stesso orologio ed exit criteria nella simulazione; check della entry query onesto |
| outputs, app | scadenza di attivazione, sezione exit criteria, timeline nel testbook, SMS, nota sulle data extension sincronizzate |

## 5. Cosa resta fuori, e perché

- Goal: nessun export ne ha; vengono letti e segnalati, non valutati.
- Valutazione delle espressioni `CASE WHEN` della entry query: la colonna prende il valore del caso (import diretto);
  riprodurre i timestamp della tabella di Backup che la renderebbero vera servirebbe solo a chi esegue l'automation.
- Fuso orario delle date e livello di confronto (giorno) delle date relative: assunzioni, elencate; un run reale le
  chiude.
- Send-time wait: finestra contata zero, come prima (la scadenza del testbook resta prudente).
- Journey con cicli o con più di 400.000 percorsi: come prima, l'esplorazione si ferma e lo dice.

## 6. Il processo, e come si ripete al prossimo journey

1. **Diagnosi**: eseguire il generatore sui nuovi export; estrarre l'inventario dei costrutti (script
   `graph_cj.py` e `dump_cj.py`, fuori dal repository: operatori, forme di valore, tipi di attività, wait, SQL,
   relazioni, issue). Classificare ogni costrutto sconosciuto in una delle sei classi del paragrafo 1.
2. **Risposta attesa a mano**: per ogni journey, un test engineer deriva dalla spec (senza leggere il codice) scenari,
   casi, stato di ogni branch con la prova, timeline, tabelle e CSV importabili, validità, rilievi sul journey; un
   secondo cerca di confutarlo; un terzo riconcilia. Le risposte dei sei export del passo 3 restano fuori dal
   repository con gli export (dati dei clienti).
3. **Spec prima del codice**: una sezione nuova della spec per i costrutti nuovi; dove Salesforce non è documentato,
   la lettura più semplice, marcata come assunzione e visibile nel report.
4. **Implementazione per moduli disgiunti**, con unit test ed esempi sintetici (`examples/`) per ogni costrutto, un
   commit per passo; il pilota byte-identico come guardia.
5. **Coorte completa**: pilota, 68 esempi, sei export contro le risposte derivate a mano; ogni divergenza è o un
   difetto del generatore o una prova scritta nel README dell'esempio che la risposta era sbagliata.
6. **Checklist di generalizzazione** per il prossimo export, da farsi prima di scrivere codice:
   - c'è un operatore, un tipo di attività, una forma di valore o di SQL non in tabella? → classe 1, una voce;
   - una condizione può dipendere da qualcosa che lo stato simulato non ha (ora, posizione, eventi esterni, contatori,
     dati di altri contatti)? → classe 2, una dimensione nuova con regola di evoluzione e di scelta dei valori;
   - un campo è letto o vincolato da un posto nuovo (filtro, goal, query, default dello schema)? → classe 3, un
     vincolo in più sulla stessa variabile;
   - una tabella è raggiunta attraverso un'altra, o scritta da chi non la legge? → classe 4, un ruolo di tabella;
   - un check fallisce su ciò che non sa verificare? → classe 5, "non verificato" con motivo;
   - il tester può davvero importare, attivare, osservare? → classe 6, la procedura nel report.

## 7. Rilievi sui sei journey che il generatore deve far emergere

- `RET_99_4W`: l'exit criterion dice `Entitle_RequestCancellationDate is not null` (negli altri cinque è `is null`):
  ogni contatto che gli split accettano esce alla fine del primo wait. Probabile errore nel journey.
- In tutti e sei: `Booked?/MP/LI/DOI/MCL` compare in più versioni con condizioni diverse (già segnalato come pattern
  sospetto); lo stesso split ripetuto dopo un'email senza che nulla cambi i campi letti rende il suo remainder non
  raggiungibile; `M.Phone AS MobilePhone, M.MobilePhone AS Phone` scambia due colonne nella entry query;
  `CampaignMember_Salesforce_9` è sincronizzata (il suo CSV si importa come gli altri).
- I journey "ED on or after Today + N days" subito dopo "N days before ED": il branch sì vale solo sull'estremo (contatto
  puntuale), il remainder serve ai contatti in ritardo; entrambi sono test utili e il testbook deve dire quale è quale.
