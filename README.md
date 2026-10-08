# HomeAssistant-EDistribuzione

Integrazione custom per Home Assistant, dedicata a **E-Distribuzione**.

Per ogni POD configurato importa **entrambe** le direzioni dell'energia come
statistiche esterne, visibili nella Energy Dashboard:

- **prelevata** (consumo dalla rete)
- **immessa** (immissione in rete / produzione fotovoltaica)

Ogni POD ha un **ruolo** configurabile (contatore normale/scambio, oppure
fotovoltaico/produzione): influenza solo i nomi mostrati, non quali dati
vengono scaricati - entrambe le direzioni si acquisiscono sempre, per ogni
POD, a prescindere dal ruolo.

## Installazione

### Tramite HACS (consigliato)

Non è nello store predefinito di HACS: va aggiunto come repository custom.

1. HACS → menu (⋮ in alto a destra) → **Repository personalizzati**
2. URL: `https://github.com/maurobraggio/HomeAssistant-EDistribuzione`,
   categoria **Integrazione**
3. Cerca "**E-Distribuzione**" in HACS → **Scarica**
4. Riavvia Home Assistant

### Manuale (alternativa)

Copia `custom_components/edistribuzione/` nella cartella
`custom_components/` della tua istanza Home Assistant, poi riavvia.

### Configurazione

*Impostazioni → Dispositivi e servizi → Aggiungi integrazione → E-Distribuzione.*

Serve: email e password dell'area clienti E-Distribuzione, e il codice OTP
che ricevi via email o SMS durante la configurazione.

## Configurazione del ruolo POD

*Impostazioni → Dispositivi e servizi → E-Distribuzione → Configura → Tipo
di contatore per POD.* Modificabile in qualunque momento.

## Energy Dashboard

Per un impianto con un contatore di scambio (M1) e uno di produzione (M2):

| Sezione | Statistica |
|---|---|
| Rete → Consumo dalla rete | `edistribuzione:<pod_m1>_energia` |
| Rete → Ritorno alla rete | `edistribuzione:<pod_m1>_energia_immessa` |
| Pannelli solari → Produzione | `edistribuzione:<pod_m2>_energia_immessa` |

`edistribuzione:<pod_m2>_energia` (la prelevata del contatore di
produzione, tipicamente lo stand-by dell'inverter, qualche decimo di kWh al
mese) resta disponibile ma non va in nessuna sezione della dashboard.

Autoconsumo e consumo totale casa sono calcolati automaticamente da Home
Assistant a partire da produzione + immissione + prelievo - non servono
sensori aggiuntivi.

## Fasce orarie F1 / F2 / F3

Per la direzione **prelevata** l'integrazione scrive anche tre serie per
fascia ARERA, calcolate dagli stessi campioni a 15 minuti:

| Statistica                       | Fascia                                                         |
| -------------------------------- | -------------------------------------------------------------- |
| `edistribuzione:<pod>_energia_f1` | lun-ven 8-19                                                   |
| `edistribuzione:<pod>_energia_f2` | lun-ven 7-8 e 19-23, sabato 7-23                               |
| `edistribuzione:<pod>_energia_f3` | notte (23-7), domenica e festivi nazionali tutto il giorno     |

Le tre serie hanno gli stessi timestamp orari della serie totale e ora per
ora F1 + F2 + F3 = totale. Si ricalcolano da `edistribuzione_curve.db` come
la totale: al primo import dopo l'aggiornamento compaiono già con tutto lo
storico scaricato in precedenza, e seguono da sole le rettifiche.

La classificazione usa sempre l'ora italiana (Europe/Rome), indipendentemente
dal fuso configurato in Home Assistant. Le festività sono le 11 storiche
(Pasquetta compresa, patroni locali esclusi) più il 4 ottobre dal 2026
(L. 151/2025): se le letture ufficiali di ottobre 2027 dicessero il
contrario, basta `SAN_FRANCESCO_FESTIVO = False` in `fasce.py`.

**Grafico per fascia** (scheda statistiche):

```yaml
type: statistics-graph
title: Prelievo per fascia
chart_type: bar
period: day
days_to_show: 30
stat_types:
  - change
entities:
  - edistribuzione:<pod>_energia_f1
  - edistribuzione:<pod>_energia_f2
  - edistribuzione:<pod>_energia_f3
```

**Energy Dashboard:** in alternativa alla serie totale si possono aggiungere
le tre fasce come tre consumi dalla rete distinti, ciascuno col proprio
prezzo. Mai la totale *e* le fasce insieme: il prelievo verrebbe contato due
volte. `configura_energy_dashboard` continua ad aggiungere solo la totale.

## Card Lovelace del POD

L'integrazione porta con sé una card (`custom:edistribuzione-pod-card`) e
la registra da sola nel frontend: niente risorse Lovelace da aggiungere a
mano, niente secondo repository HACS. Compare nel selettore delle card come
"E-Distribuzione · POD" e ha un editor visuale.

```yaml
type: custom:edistribuzione-pod-card
pod: it001e12345678     # opzionale: senza, usa il primo POD trovato
name: Contatore         # opzionale
icon: mdi:transmission-tower
period: month           # day | week | month | year
show_injection: true
```

Mostra prelievo e immissione del periodo, la ripartizione F1/F2/F3 e un
grafico a barre impilate per fascia (ore nella vista giorno, giorni in
settimana e mese, mesi nell'anno), con navigazione limitata al periodo in
cui esistono dati. Usa solo i token del tema di Home Assistant (colori
energia, tipografia, raggi, tile icon, control select, tema dei grafici),
quindi segue tema chiaro/scuro e temi personalizzati, oltre a lingua,
formato numeri, formato orario e primo giorno della settimana del profilo.

## Architettura: 15 minuti come source of truth

I campioni a 15 minuti restituiti da E-Distribuzione (96/giorno) non vengono
aggregati e scartati: finiscono per primi in un database SQLite proprio
dell'integrazione (`edistribuzione_curve.db`, nella cartella di
configurazione di Home Assistant - un file indipendente, mai lo stesso
database del Recorder). Solo dopo, dai campioni realmente memorizzati, si
ricalcolano i bucket orari e la somma cumulativa che vanno nella Energy
Dashboard:

```
API E-Distribuzione (15')  ->  raw storage (upsert)  ->  bucket orari + sum  ->  Energy Dashboard
```

Questo rende ogni import **idempotente e capace di autocorreggersi**: se
E-Distribuzione rettifica in un secondo momento un campione già scaricato
(succede), un nuovo `recupera_storico` sullo stesso periodo sovrascrive
quel campione (stesso POD, stessa direzione, stesso istante) invece di
duplicarlo, e ricalcola da zero sia l'ora toccata sia tutte le somme
cumulative successive - il risultato finale non dipende dall'ordine in cui
storico, retry e rettifiche sono arrivati.

Per le stesse ragioni, il ciclo automatico giornaliero non richiede più solo
il giorno precedente: ricontrolla sempre gli ultimi `GIORNI_RICONTROLLO`
giorni (3 di default, in una sola richiesta per direzione, non una per
giorno), così una rettifica recente viene vista da sola senza dover lanciare
`recupera_storico` a mano.

## Recupero storico

Azione `edistribuzione.recupera_storico(device_id, data_da, data_a)`: una
sola richiesta per direzione per l'intero periodo (confermato funzionante
fino a 181 giorni in un'unica risposta). Scegliendo il dispositivo di un
singolo POD il recupero si limita a quello; scegliendo il dispositivo
"E-Distribuzione" (account) copre tutti i POD configurati.

Rilanciarlo sullo stesso periodo è sempre sicuro: aggiorna/corregge invece
di duplicare (vedi sopra).

## Verificare il protocollo prima di fidarsi dei dati

```bash
pip install -r requirements_test.txt
python scripts/verify_login.py
```

Lo script fa login (email/password/OTP), elenca i POD dell'account e sonda
diversi candidati per `magnitude` sull'endpoint dati, confrontando i totali
per scoprire quale restituisce l'energia immessa. Se un candidato risulta
corretto, aggiorna **solo** `MAGNITUDE_IMMESSA` in
`custom_components/edistribuzione/const.py`.

## Sviluppo

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt ruff
.venv/bin/python -m pytest
.venv/bin/ruff check custom_components/ tests/ scripts/
```

## Origine

Il protocollo (login OAuth2+PKCE+OTP via Salesforce, client REST MuleSoft
per i dati) è stato reverse-engineered per l'integrazione multi-distributore
[HomeAssistant-Contatore](https://github.com/riccardorossi92/HomeAssistant-Contatore),
che resta la scelta giusta per chi ha anche Duereti, Unareti o Areti. Questo
repository è dedicato solo a E-Distribuzione, con supporto nativo per
prelevata/immessa separate e un ruolo configurabile per POD.
