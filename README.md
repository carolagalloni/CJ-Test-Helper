# CJ Test Helper

CJ Test Helper legge l'export di un package di Salesforce Marketing Cloud, cioè il file JSON prodotto da Package Manager, e scrive dati di test che portano i contatti lungo ogni branch del customer journey. Scrive un CSV per ogni tabella letta dal journey, un elenco di scenari, un report di coverage e un testbook con il risultato atteso di ogni test. Non si collega mai a Salesforce: genera solo file e li controlla prima che vengano usati.

`docs/spec.md` è il riferimento per il comportamento dello strumento. Questo file spiega come avviarlo e come usare i file che produce.

## Requisiti e installazione

- Python 3.11 o successivo.
- I pacchetti elencati in `requirements.txt`:

```
pip install -r requirements.txt
```

## Avvio

Dalla cartella del repository:

```
python -m cjhelper "fixtures/CJ template complesso-v1.json" --out output --inbox name@example.com
```

Il comando stampa un riepilogo e l'esito di ogni trust check. Quando elementi non supportati lasciano branch non coperti, il riepilogo si apre con un avviso, come `report.md`. Scrive tutti i file nella cartella di output, insieme a `test_data.zip`, che li contiene tutti. Sull'export del pilot impiega circa 30 secondi.

| Opzione | Cosa fa |
| --- | --- |
| `export` | L'export JSON del package. Obbligatoria. |
| `--out FOLDER` | Dove scrivere i file. Default `output`. |
| `--run-id R01` | Run identifier aggiunto a ogni chiave, come in `TST_R01_C0001`. Default `R01`. |
| `--inbox ADDRESS` | Inbox reale per i contatti che devono aprire o cliccare un'email. Default vuota. |
| `--phone NUMBER` | Numero di telefono reale. Lo riceve un contatto per ogni caso il cui percorso invia un SMS, così il tester può controllarlo. Default vuoto: l'SMS non si può verificare. |
| `--contacts N` | Contatti per caso. Default 3. |
| `--random-contacts N` | Contatti nel caso statistico di un random split. Default 50. |
| `--case-insensitive` | I confronti di testo ignorano maiuscole e minuscole, quindi `Email` corrisponde a `EMAIL`. Default disattivata. |
| `--no-single-active` | Rende attivi tutti gli scenari nei CSV. Di default è attivo solo il primo quando viene rilevato un campo di attivazione. Con un'entry source Run Once tutti gli scenari sono sempre attivi, perché entrano insieme quando il journey viene attivato. |
| `--time "YYYY-MM-DD HH:MM"` | Ora di generazione. Default l'ora corrente. Con la stessa ora si ottengono di nuovo gli stessi file. |
| `--no-testbook` | Non scrive `testbook.xlsx`. |

Il comando termina con codice 0 quando tutti i trust check sono superati e con codice 2 quando un check non è superato. In caso di errore, per esempio un file che non è un JSON valido o che non contiene un journey, stampa un solo messaggio, non scrive nulla e termina con codice 1.

## I file prodotti

| File | Contenuto |
| --- | --- |
| `<TableName>.csv` | Un file per ogni tabella letta dal journey o dalla entry query, comprese le tabelle di collegamento attraverso cui il journey raggiunge una tabella, con tutti gli scenari insieme, pronto da importare. UTF-8, separato da virgole, con i nomi esatti dei campi dello schema come intestazione, `True` e `False` per i Boolean e una cella vuota per il null. Il CSV di una data extension sincronizzata dal CRM (managed) si importa come gli altri; il report dice solo che la tabella è sincronizzata e che le sue righe arriveranno anche dalla sincronizzazione. |
| `scenarios.csv` | Una riga per scenario: la chiave, l'ordine di esecuzione, quale scenario è attivo all'inizio, i casi e i contatti, e i valori a livello di scenario. |
| `report.md` | Da leggere per primo. Riepilogo, trust check, tabelle, come eseguire i test, scenari e casi, branch non coperti e perché, criteri di uscita (sezione "Criteri di uscita"), elementi non supportati, warning e pattern sospetti, assunzioni, impostazioni usate. Si apre con un avviso quando elementi non supportati lasciano branch non coperti e, quando i dati dipendono dalla data di oggi, con la scadenza di attivazione: l'ora entro cui importare i file e attivare il journey. |
| `testbook.xlsx` | Un test per caso nel formato del team: ID, Owner system, Test name, Description, Pre conditions, Detailed steps, Expected result, Status, Note. I passi danno anche la durata minima di ogni test e la timeline del caso (il giorno e l'ora di ogni passo, contati dall'ora di ingresso), e le note riportano la scadenza di attivazione dei suoi dati. |
| `test_data.zip` | Tutti i file precedenti in un solo archivio. |

Termini usati in questi file:

- Un **branch** è un esito di uno split: decision split, engagement split o random split.
- Uno **scenario** è un insieme di valori a livello di scenario, per esempio una riga di una tabella di configurazione della campagna. Tutti i suoi contatti lo condividono.
- Un **caso** è un gruppo di contatti con gli stessi valori di contatto e le stesse azioni manuali, che quindi seguono lo stesso percorso. Ogni caso è un test del testbook. Di default ha 3 contatti.
- Un **caso statistico** copre i branch che seguono un random split. I suoi contatti non possono essere indirizzati su un branch preciso, quindi di default ha 50 contatti.

## Come un tester usa i file

1. Apri `report.md`. Verifica che tutti i trust check siano superati, poi leggi la sezione "Come eseguire i test", che segue la entry source del journey (vedi "La procedura di ingresso" qui sotto).
2. Popola la tabella di ingresso a mano, con l'import diretto dei CSV: importa prima i CSV delle tabelle collegate e per ultimo il CSV di ingresso. Durante il test non si esegue nessuna automation. Importa anche il CSV di una data extension sincronizzata dal CRM, come gli altri: il report ricorda che le sue righe arriveranno anche dalla sincronizzazione. Se il report indica una scadenza di attivazione, importa i file e attiva il journey entro quell'ora, altrimenti i valori che dipendono dalla data di oggi cambiano percorso.
3. Quando il report lo indica, esegui uno scenario alla volta, in ordine di esecuzione. I CSV partono con il primo scenario attivo. Per rendere attivo uno scenario, imposta il campo di attivazione a True sulla riga dello scenario e a False su tutte le altre righe. Il report indica il campo: nel pilot è `ACTIVE` di `DE_NCRM_AD_HOC_CONFIG`. Il campo di attivazione è un filtro della entry query e nessuno split lo legge, quindi con l'import diretto non trattiene alcuno scenario.
4. Prima di passare allo scenario successivo, aspetta che i contatti dello scenario corrente siano usciti dal journey. Non cambiare altro e non reimportare nulla, perché il journey legge i dati degli split nel momento in cui un contatto raggiunge lo split.
5. Segui il testbook, una riga per caso. I passi indicano le attese, le eventuali azioni manuali con la loro scadenza e le evidenze da raccogliere. Confronta quello che vedi con l'Expected result.
6. Per una nuova generazione usa un nuovo run identifier. Le chiavi di un run precedente sono già nelle tabelle e un contatto di test non viene mai riutilizzato.

Assunzione da verificare al primo run reale: quando la entry source del journey è una data extension pianificata da un'automation, come nel pilot, Salesforce potrebbe richiedere il passo di ingresso nel journey di quell'automation perché i contatti importati entrino. Le righe da 23 a 27 di `docs/assumptions.md` spiegano il motivo.

### La procedura di ingresso

La procedura dipende dalla entry source del journey, letta dall'export. Il report e il testbook la scrivono per esteso.

| Entry source | Procedura |
| --- | --- |
| Data extension alimentata da un'automation (il pilot) | Quella descritta sopra: import diretto, uno scenario alla volta quando c'è un campo di attivazione. |
| Data extension pianificata "Run Once" | Importa tutti i CSV, poi attiva il journey. Entrano solo le righe presenti all'attivazione: le righe importate dopo non entrano mai. La data extension di ingresso deve contenere solo righe di test. Tutti gli scenari entrano insieme, quindi l'impostazione "Un solo scenario attivo alla volta" non è disponibile. Un nuovo tentativo richiede una nuova versione del journey e un nuovo run identifier. |
| Data extension con pianificazione ricorrente | Procedura da verificare: i contatti dovrebbero entrare alla valutazione successiva della data extension. |
| Form CloudPages, API event o altro | I contatti non entrano con l'import di un CSV. Il test si fa su una copia del journey la cui entry source è una data extension con le stesse colonne, e il CSV di ingresso va in quella data extension. |

Con le modalità di ingresso `OnceAndDone` e `SingleEntryAcrossAllVersions` ogni tentativo richiede un nuovo run identifier. Filtri di ingresso e goal non sono modellati: il report li segnala, perché alcuni contatti potrebbero non entrare o uscire prima. Gli exit criteria che il generatore sa valutare sono modellati: alla fine di ogni wait, se sono veri, il contatto esce dal journey (sezione "Criteri di uscita" di `report.md`); gli altri sono segnalati come non modellati.

Quando uno split controlla se il contatto ha una riga in una tabella collegata (per esempio se ha compilato un form), alcuni casi hanno la riga assente: i loro contatti non hanno righe nel CSV di quella tabella, e il testbook lo dice nelle Pre conditions. È voluto.

### L'inbox reale

I contatti che devono aprire o cliccare un'email ricevono la tua inbox reale con plus-addressing. Con `--inbox name@example.com`, il contatto `TST_R01_C0004` riceve `name+TST_R01_C0004@example.com` e le sue email arrivano in `name@example.com`. Tutti gli altri contatti ricevono un indirizzo non recapitabile, `<contact key>@example.invalid`.

- Apri o clicca solo le email indicate dal testbook, e solo per i contatti di quel test.
- Fallo prima della scadenza indicata nel testbook. La scadenza viene dai wait tra l'email e l'engagement split: la data dell'ultimo wait by attribute, di default 24 ore dopo l'ora di generazione, più i wait a durata fissa che lo seguono. Senza wait by attribute la scadenza è un tempo dall'arrivo dell'email, per esempio "entro 1 giorno". Una finestra sotto l'ora ha un warning. Per questo motivo gli scenari con un'azione manuale vengono per primi nell'ordine di esecuzione.
- Se un run parte troppo tardi per rispettare una scadenza, genera di nuovo i dati.
- Senza `--inbox` la generazione funziona comunque. Quei contatti ricevono indirizzi non recapitabili e i branch che richiedono un open o un click risultano non coperti.

Un click si può chiedere solo se l'email contiene un link. Nel pilot l'email non ne ha, quindi i branch del click risultano non coperti.

### Il caso del random split

Un random split non si può forzare. I suoi branch ricevono un caso statistico, di default con 50 contatti, e il testbook indica l'intervallo accettato per il lato più piccolo. Per uno split 90/10, da 1 a 9 dei 50 contatti dovrebbero prendere il lato del 10%. Questo vale in circa il 97% dei run, quindi un conteggio fuori dall'intervallo può capitare per caso.

## Trust check

Dopo aver scritto i file, il generatore li rilegge e li controlla. Il report riporta l'esito di ogni check e i relativi dettagli.

| Check | Se non è superato |
| --- | --- |
| I valori rispettano tipo, lunghezza, nullability e primary key | L'import potrebbe scartare righe o troncare valori. |
| Ogni chiave trova corrispondenza tra le tabelle | Alcuni contatti non troverebbero la riga del loro scenario o le loro righe in una tabella collegata, quindi i loro split non vedrebbero i valori previsti. L'unica eccezione accettata sono le righe assenti volute dal caso, e solo quelle. |
| Le righe di ingresso corrispondono alla entry query | Il CSV di ingresso non contiene quello che la entry query costruirebbe dagli altri CSV. Per una entry source che nessuna query popola (Run Once, form CloudPages) il check non si applica e lo dice. |
| Ogni contatto, ri-simulato a partire dai file, segue il percorso previsto | Il risultato atteso nel testbook potrebbe essere sbagliato per alcuni contatti. |
| Ogni branch raggiungibile è coperto e ogni branch non coperto ha un motivo | La coverage indicata nel report non è affidabile. |
| Solo i contatti con un'azione manuale usano l'inbox reale; tutti gli altri sono non recapitabili | Alcune email potrebbero arrivare all'inbox reale quando non dovrebbero. |
| Due run con lo stesso input producono gli stessi file | Lo stesso export con le stesse impostazioni non darebbe gli stessi dati. |

Quando un check non è superato, il comando termina con codice 2 e il report elenca i problemi. Non importare i file finché il problema non è chiarito.

## Impostazioni e default

Queste sono le impostazioni della spec. La riga di comando usa i default e le opzioni descritte sopra ne modificano alcuni. Il report termina con le impostazioni usate per il run.

| Impostazione | Default | Opzione |
| --- | --- | --- |
| Contatti per caso | 3 | `--contacts` |
| Contatti per caso di random split | 50 | `--random-contacts` |
| Inbox reale per i test di open e click | Vuota | `--inbox` |
| Numero di telefono reale | Vuoto | `--phone` |
| Numero di telefono fittizio | `333333333333` | nessuna |
| Durata della finestra di validità | Da ieri a 7 giorni dopo | nessuna |
| Ora di ingresso (da cui parte l'orologio dei wait e delle date relative a oggi) | L'ora di generazione | nessuna: la riga di comando usa sempre l'ora di generazione |
| Validità minima dei dati (giorni) | 1 | nessuna |
| Offset della data di wait | 0 ore | nessuna |
| Offset della data di wait prima di un engagement split | 24 ore | nessuna |
| Formato delle date nei CSV | `YYYY-MM-DD HH:MM` | nessuna |
| Un solo scenario attivo alla volta | Attivo quando viene rilevato un campo di attivazione, mai con un'entry source Run Once | `--no-single-active` lo disattiva |
| Campo di attivazione | Suggerito dai filtri della entry query | nessuna |
| Run identifier nelle chiavi | R01 | `--run-id` |
| I confronti di testo ignorano maiuscole e minuscole | Disattivato | `--case-insensitive` |

## Come vengono scelti i valori

Ogni valore viene dalla prima di queste regole che si applica:

1. Un campo chiave o di join riceve una chiave generata, uguale in tutte le tabelle che fanno join su quel campo: `TST_R01_C0001` per i contatti, `TST_R01_S01` per gli scenari.
2. Un campo letto da uno split riceve il valore che serve al caso, scritto esattamente come nella condizione, con le stesse maiuscole e minuscole. Per gli intervalli (`GreaterThan`, `Between` e simili su Number, Decimal e Date) il valore sta strettamente dentro l'intervallo, mai su un estremo. Un null è sempre una cella vuota, mai uno spazio.
3. Un campo filtrato dalla entry query riceve un valore che supera il filtro, così il contatto entra davvero nel journey.
4. Un indirizzo email riceve l'inbox reale o `@example.invalid`, come descritto sopra. Un telefono riceve il numero fittizio. La finestra di validità va da ieri a 7 giorni dopo. Una data di wait è l'ora di generazione, per far terminare subito il wait, oppure 24 ore dopo quando i contatti devono aprire un'email dopo quel wait.
5. Ogni altro campo riceve un segnaposto: per il testo `TST_<field>_<n>` troncato alla lunghezza del campo, per i Boolean il loro default o False.

## Limiti noti

- Coverage dei branch, non dei percorsi: ogni branch viene attraversato almeno una volta, ma non ogni combinazione di branch.
- I random split sono coperti in modo statistico, mai con certezza.
- Vengono interpretati solo i tipi di attività, gli operatori e i pattern SQL supportati. Tutto il resto viene elencato nel report e i branch che ne dipendono risultano non coperti. Nulla viene dedotto per supposizione.
- I criteri di ingresso definiti fuori dal JSON, come i filtri sui contatti impostati nell'interfaccia e non esportati, non sono visibili.
- Il contenuto delle email e la personalizzazione non vengono testati. Lo strumento testa l'instradamento dei contatti.

Supportato nel PoC:

- Attività: decision split, engagement split, random split, wait by duration, wait by attribute (anche con offset rispetto alla data del campo, per esempio "13 days before"), send-time wait, email, SMS, update contact.
- Operatori nelle condizioni: `Is`, `Equal` e `IsNull`, più `NotEqual`, `IsNotNull`, `GreaterThan`, `GreaterThanOrEqual`, `LessThan`, `LessThanOrEqual`, `Contains`, `BeginsWith` e `EndsWith`, ripresi dallo strumento precedente, e `Between` con il valore `basso|alto`, estremi compresi. Per le date: `After`, `AtOrAfter`, `Before` e `AtOrBefore`, anche con una data relativa a oggi ("Today", "Today Plus 90 days", "Today Minus 7 days"), e `IsNot`.
- Sorgenti dei dati: Contact Data (`<attribute set>.<campo>`, letti quando il contatto arriva allo split) e Journey Data (`Event.<chiave>.<campo>`, la riga di ingresso com'era all'ingresso). Un confronto tra due attributi è supportato solo nella forma "campo collegato alla chiave del contatto uguale alla chiave del contatto", letta come "il contatto ha una riga in quella tabella"; le altre forme sono segnalate come non supportate. Una tabella raggiunta attraverso un'altra tabella (per esempio una tabella di festività collegata al contatto da una tabella di collegamento) riceve anche il CSV della tabella di collegamento.
- Exit criteria: valutati alla fine di ogni wait con gli stessi operatori, sorgenti e date relative degli split. Un contatto che li soddisfa esce dal journey in quel punto.
- Entry source: data extension alimentata da un'automation, data extension "Run Once" o con pianificazione ricorrente, form CloudPages e API event, ciascuna con la sua procedura (vedi "La procedura di ingresso").
- SQL nelle entry query: nomi di tabella, uguaglianze di join, filtri WHERE semplici su uguaglianza, null e intervalli di date, ed espressioni semplici nella select, tra cui il confronto della data di una colonna con la data corrente spostata di alcuni giorni. Una colonna della select copiata da un'altra tabella ha lo stesso valore della sorgente. Una colonna che la query calcola con un'espressione che il generatore non capisce (per esempio un `CASE`) riceve il valore che serve al caso, valido solo con l'import diretto dei CSV. Un `LEFT JOIN` è letto come un join interno. Un filtro o un'espressione più complessi vengono segnalati, e una colonna di ingresso che ne dipende riceve un segnaposto.

## Assunzioni da verificare

In alcuni punti i risultati attesi dipendono da comportamenti di Salesforce che la spec non chiarisce: le maiuscole e minuscole nei confronti di testo, come viene letta una data di wait senza fuso orario, come entrano i contatti quando l'ingresso è pianificato da un'automation, e altro. Ognuno è un'assunzione da verificare una volta con un piccolo run reale, mai un dato di fatto.

- `docs/assumptions.md` le elenca tutte, con la documentazione trovata e come verificare ciascuna, e pianifica un run di verifica di un giorno. Inizia dalla sezione "Check these first".
- La sezione "Assunzioni" di `report.md` elenca solo quelle che l'export eseguito attiva.

## L'app a pagina singola

L'app a pagina singola è un passo successivo, la priorità 5 della spec: carica l'export, verifica cosa è stato rilevato, modifica le impostazioni, genera e scarica lo zip. Una prima versione è in `cjhelper/app.py` e si avvia con `streamlit run cjhelper/app.py` dalla cartella del repository. Finché non viene confermata, usa la riga di comando.

L'interfaccia, i messaggi del generatore (warning, trust check, errori di lettura) e i testi dei file scaricati sono in italiano; restano in inglese i termini d'uso comune del team, i nomi dei file e le intestazioni di colonna di scenarios.csv e del testbook (vedi docs/brand.md, "Glossario").

## Per gli sviluppatori

| Percorso | Contenuto |
| --- | --- |
| `docs/spec.md` | La specifica, il riferimento |
| `docs/plan.md` | Come la spec è suddivisa in moduli |
| `docs/progress.md` | Stato del lavoro e domande aperte |
| `docs/assumptions.md` | La review Salesforce: assunzioni da verificare e pattern sospetti |
| `cjhelper/` | Il generatore, un modulo per ogni passo della spec |
| `tests/` | Test unitari per modulo, gli esempi e il test end-to-end sul pilot |
| `examples/` | Piccoli journey costruiti a mano con le risposte attese |
| `fixtures/` | L'export del pilot |
| `tools/` | Script di diagnosi per gli export dei clienti, che restano fuori dalla repository: `dump_cj.py` (dump strutturale di un export), `graph_cj.py` (grafo compatto del journey e inventario dei costrutti), `run_cohort.py` (esegue il generatore su un gruppo di export e confronta con le risposte derivate a mano). Non fanno parte del generatore. |

Esegui i test con `python -m pytest`. I test sul pilot impiegano circa un minuto.
