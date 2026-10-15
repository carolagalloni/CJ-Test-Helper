# Report finale del passo 3: journey che dipendono dal tempo

14 ottobre 2026, sera. Branch `claude/quirky-bohr-24fr8j`. Per riprendere il lavoro: `docs/progress.md`.

## Cosa passa

- **I sei export RET_99** (2 settimane, 3 mesi, 4 settimane, hot churn, 5 giorni, 6 settimane) girano in pochi
  secondi ciascuno, con tutti e sette i trust check superati. Prima del passo 3 il generatore copriva 0 branch su
  cinque di essi (4 su 20 sul sesto).
- **Confronto con le risposte attese derivate a mano** (cinque export; per il sesto, 5 giorni, la risposta attesa non è
  stata derivata per restare nel budget): lo stato di ogni branch coincide su tutti e cinque; sui 3 mesi due branch
  risultano "coperti" invece di "coperti con azione manuale" perché il generatore ha trovato un percorso che non
  richiede l'open (una risposta migliore, non un errore). Il target "criteri di uscita soddisfatti" è coperto su tutti.

| Export | Scenari / casi / contatti | Branch coperti (di cui con azione manuale) / non coperti | Minimo derivato a mano | Validità dei dati |
| --- | --- | --- | --- | --- |
| HC | 3 / 9 / 27 | 19 (3) / 1 | 3 / 7 / 21 | 1 giorno |
| 2W | 3 / 8 / 24 | 21 (0) / 0 | 3 / 7 / 21 | 1 giorno |
| 3m | 3 / 11 / 33 | 29 (9) / 1 | 3 / 9 / 27 | 1 giorno |
| 6w | 3 / 11 / 33 | 35 (3) / 1 | 3 / 9 / 27 | 0 giorni (vedi sotto) |
| 4W | 1 / 3 / 9 | 6 (0) / 35 | 1 / 3 / 9 | nessun valore deperibile |
| 5d | 3 / 13 / 39 | 41 (0) / 1 | non derivato | 0 giorni |

- **Il pilota** resta identico byte per byte nei quattro CSV e in `scenarios.csv`. Differenze da elencare al team:
  `report.md` cambia in quattro righe (due motivi nella sezione Tabelle, che ora dicono che Master e Entry sono sul
  percorso verso Config letta da 62 split e 3 Wait by attribute; due righe nuove nella tabella delle impostazioni,
  "Ora di ingresso" e "Validità minima dei dati"); `testbook.xlsx` cambia in tutti i 33 test nella sola colonna
  "Detailed steps", che ora ha un passo con la timeline del caso (nel pilota ogni wait finisce subito, quindi la
  timeline è una riga "Giorno 0" lunga).
- **I quattro export del passo 2** danno gli stessi scenari, casi, contatti e stati dei branch di prima, con i check
  superati.
- **79 esempi** con risposte derivate a mano passano, di cui 15 nuovi per i costrutti del passo 3 (date relative,
  wait con offset, criteri di uscita, SMS, tabelle di collegamento, colonne copiate e calcolate, filtro data con
  offset, data extension sincronizzate, LEFT JOIN, `IsNot` su null).

## Cosa è cambiato nel generatore

Le classi di errore trovate e la risposta a ciascuna sono in `docs/design-passo3.md`. In breve: il walker porta un
orologio (wait by duration e by attribute con offset e ora del giorno, criteri di uscita valutati alla fine di ogni
wait come documenta Salesforce, SMS, timeline di ogni caso); gli operatori di data relativi a "oggi" e `IsNot`; i
domini dei campi data costruiti dai punti di taglio con la validità calcolata per simulazione e le regole di scelta
della spec; le classi di contatti costruite per percorso e per componenti di campi (i sei export hanno fino a 435
milioni di combinazioni, che la versione precedente troncava alla prima milionesima parte); le colonne copiate dalla
entry query come una sola variabile; le tabelle di collegamento sui percorsi degli attribute set; il parser SQL con il
filtro "data = oggi ± N giorni", le sorgenti opzionali e le colonne scambiate; i trust check che dicono "non
verificato" invece di fallire sui propri limiti; report e testbook con la scadenza di attivazione, la sezione dei
criteri di uscita, la nota sulle data extension sincronizzate (il loro CSV si importa come gli altri), la
timeline e gli SMS.

## Rilievi sui journey, che il report ora porta al cliente

- **4 settimane**: l'exit criterion dice `Entitle_RequestCancellationDate is not null` (negli altri cinque è `is
  null`): ogni contatto accettato dagli split esce alla fine del primo wait; 35 branch su 41 non sono raggiungibili.
- In tutti e sei: la entry query scambia le colonne `Phone` e `MobilePhone`; `CampaignMember_Salesforce_9` è una data
  extension sincronizzata dal CRM, il cui CSV si importa come gli altri (risposta del team); lo split
  "Booked?/MP/LI/DOI/MCL" compare in versioni con condizioni diverse e, ripetuto dopo l'email senza che nulla cambi i campi letti, ha il remainder non
  raggiungibile.

## Cosa si discosta e cosa resta aperto

- **Numero di casi**: il set cover greedy usa da 1 a 2 casi più del minimo derivato a mano (per esempio 9 contro 7 su
  HC). Gli stati dei branch coincidono; la minimalità esatta richiederebbe una ricerca più costosa.
- **Validità dei dati**: il generatore la misura sul valore ("di quanti giorni può slittare l'ingresso prima che una
  condizione sul campo, a qualunque ritardo raggiungibile, legga il valore diversamente"), lettura prudente della spec.
  Sui 6 settimane dà 0 giorni perché il solo valore che raggiunge un branch (`ED btwn 42 to 28` remainder) cambia
  lettura su un altro percorso; misurata sul percorso del caso darebbe 1 giorno, come la derivazione a mano. Da
  decidere se passare alla misura sul percorso.
- **Risposte attese**: derivate da un agente per cinque export, senza il passo di confutazione previsto (fermato per
  restare nel budget); le differenze di validità (HC 1 contro 2, 2W 1 contro 2) vanno a favore del generatore, che
  applica la regola della spec con l'operatore esclusivo.
- **Assunzioni 48-61** (`docs/assumptions.md`; le righe 47 e 57 sono state confermate dal team) da verificare con un
  run reale: in testa `IsNot` su null, il wait by attribute con campo null, il fuso orario dell'orologio.

## Risposte del team (14 ottobre 2026, sera)

1. **Date relative confrontate sul giorno**, non sull'ora: confermato. Non è più un'assunzione (`docs/assumptions.md`).
2. **Data extension sincronizzate** (`CampaignMember_Salesforce_9`): se servono dentro il journey vanno importate come
   gli altri CSV. Il report le tiene nell'elenco dei file da importare e dice solo che la tabella è sincronizzata dal CRM.
3. **Validità dei dati misurata sul valore**, come oggi: i CSV vengono usati in giornata, quindi la lettura prudente
   basta. La misura sul percorso del caso resta un'idea, non un passo da fare.
4. **Target "criteri di uscita soddisfatti"** coperto da un caso che esce: va bene.
5. **Journey 4 settimane**: l'exit criterion `Entitle_RequestCancellationDate is not null` (gli altri cinque dicono `is
   null`) è un errore di configurazione da segnalare al cliente. Il report lo dice così ("probabile errore di
   configurazione del journey, da segnalare al cliente").
