"""Costanti del protocollo E-Distribuzione.

Reverse-engineered dal traffico reale dell'app iOS (login) e dal portale web
(dati) - vedi documentation/protocol.md per i dettagli e le fonti.
"""
from __future__ import annotations

DOMAIN = "edistribuzione"

# --- Salesforce Experience Cloud (login / OAuth2 + PKCE) --------------------
SF_BASE = "https://private.e-distribuzione.it/PortaleClienti"

OAUTH_AUTHORIZE_URL = f"{SF_BASE}/services/oauth2/authorize"
OAUTH_TOKEN_URL = f"{SF_BASE}/services/oauth2/token"
OAUTH_USERINFO_URL = f"{SF_BASE}/services/oauth2/userinfo"

LOGIN_PAGE_URL = f"{SF_BASE}/s/login/"
AURA_ENDPOINT = f"{SF_BASE}/s/sfsites/aura"
LOGINFLOW_URL = f"{SF_BASE}/loginflow/loginFlow.apexp"

# Client ID dell'app ufficiale E-Distribuzione (pubblico, stile app nativa -
# nessun client_secret, coerente con PKCE). Riusare il client_id di Enel è
# ciò che rende possibile un login headless, ma è un client di terze parti
# che si appoggia all'infrastruttura di Enel - fragile, va trattato come
# soggetto a revoca/cambio senza preavviso.
OAUTH_CLIENT_ID = (
    "3MVG9Rd3qC6oMalUVUXDfNEIi52RXSZMsndjnAnbka4R4Amhq6QrTj2U2Zw0sjyqOlFViLC"
    ".cTpau8fPEBV0V"
)
OAUTH_REDIRECT_URI = "eneldist://redirect"
OAUTH_SCOPE = "web api openid id profile email address phone refresh_token offline_access"

# --- Backend dati (MuleSoft) -------------------------------------------------
MISURE_BASE_URL = "https://xs-misura-p.de-c1.eu1.cloudhub.io/xs/misure"

MISURE_READING_URL = f"{MISURE_BASE_URL}/reading"
MISURE_DAILY_LOAD_PROFILE_URL = f"{MISURE_BASE_URL}/querydailyloadprofile"
MISURE_MONTHLY_LOAD_PROFILE_URL = f"{MISURE_BASE_URL}/querymonthlyloadprofile"
MISURE_MONTHLY_TIME_OF_USE_URL = f"{MISURE_BASE_URL}/querymonthlytimeofuse"
MISURE_GET_SUPPLIES_URL = f"{MISURE_BASE_URL}/getSupplies"

# Method_User: header applicativo che il backend usa come discriminatore di
# permessi per endpoint, indipendente dal verbo HTTP.
METHOD_USER_ELENCO_POD = "ELENCO_POD"
METHOD_USER_LETTURE = "LETTURE"
METHOD_USER_CURVA_GIORNO = "CURVE_DI_CARICO-GIORNO"
METHOD_USER_CURVA_MESE = "CURVE_DI_CARICO-MESE"
METHOD_USER_CURVA_PERIODO = "CURVE_DI_CARICO-PERIODO"

# --- Magnitude della curva di carico -----------------------------------------
#
# ATTENZIONE al backend: questi valori sono quelli del layer REST MuleSoft
# (querydailyloadprofile), NON quelli del portale web (Aura,
# PED_CurveDiCaricoController, che usa "A+"/"A-") - sono due backend diversi
# con vocabolari diversi, collegati solo dal token OAuth. Non dare per
# scontato che valgano le stesse stringhe.
MAGNITUDE_PRELEVATA = "A1"  # CONFERMATO: energia prelevata dalla rete.

# CONFERMATO su account reale (23/09/2026, recupera_storico su agosto 2026):
# il totale del POD di produzione con magnitude="A2" torna 626.551 kWh,
# esattamente il valore noto di energia immessa per quel mese - non un
# duplicato della prelevata (0.437 kWh sulla stessa serie con "A1").
MAGNITUDE_IMMESSA = "A2"

MAGNITUDE_TUTTE = (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA)

# Direzioni per cui si generano anche le serie per fascia ARERA (F1/F2/F3,
# vedi fasce.py). Solo la prelevata: è l'energia che il venditore fattura
# per fascia; l'immessa non ha un prezzo per fascia nei contratti domestici.
DIREZIONI_CON_FASCE = (MAGNITUDE_PRELEVATA,)

# --- Configurazione persistita ------------------------------------------------
CONF_PODS = "pods"  # lista di codici POD: tutti sulla stessa utenza già autenticata
CONF_REFRESH_TOKEN = "refresh_token"

CONF_TIPO_POD = "tipo_pod"  # {pod: "scambio"|"produzione"}, in entry.options
TIPO_POD_SCAMBIO = "scambio"
TIPO_POD_PRODUZIONE = "produzione"
TIPO_POD_DEFAULT = TIPO_POD_SCAMBIO

# Nessun suggerimento automatico del ruolo, va scelto sempre a mano
# (options flow, step tipo_pod): verificato su un account reale (23/09/2026)
# che 'HasPlant' da getSupplies NON e' un segnale utilizzabile - risulta
# True sul POD di SCAMBIO (l'utenza che ha un impianto associato) e null
# (non False, assente) su quello di PRODUZIONE, cioe' l'opposto di quello
# che servirebbe per dedurre automaticamente il ruolo. Il prefisso del
# codice POD (es. "ITP0A" vs "IT001") e' un altro indizio disponibile ma
# deliberatamente escluso: non generalizza ad altri DSO/clienti.

# --- Import automatico curva giornaliera + coda di retry ---------------------
DEFAULT_UPDATE_INTERVAL_MINUTES = 60

# Osservazioni reali: a mezzanotte/01:00 il giorno appena finito non è ancora
# disponibile (404), quello di 2 giorni prima sì; alle 18:00 il giorno
# precedente risulta già disponibile. Coerente con un ritardo di un giorno
# intero - dedotto dall'uso, non garantito. Il meccanismo di coda sotto lo
# rende comunque robusto anche se il ritardo vero fosse maggiore.
RITARDO_DATI_GIORNI = 1

# E-Distribuzione può rettificare dati già pubblicati (osservato: un giorno
# arriva con un valore, giorni dopo torna corretto). Senza un ricontrollo
# periodico quella correzione non verrebbe mai vista. Ogni ciclo automatico
# richiede quindi sempre gli ultimi GIORNI_RICONTROLLO giorni (non solo il
# giorno atteso), in un'unica richiesta per direzione - non una per giorno,
# l'endpoint accetta già un intervallo multi-giorno.
GIORNI_RICONTROLLO = 3

CONF_DATA_INSTALLAZIONE = "data_installazione"
CONF_GIORNI_DA_RIPROVARE = "giorni_da_riprovare"
CONF_ORA_RICHIESTA = "ora_richiesta"

# Un giorno resta in coda e viene riprovato ai cicli successivi, abbandonato
# dopo questo numero di giorni REALI dal primo inserimento (non dopo N
# tentativi). ~1 settimana copre eventuali ritardi di pubblicazione senza
# accanirsi su date che non arriveranno mai.
ABBANDONO_CODA_DOPO_GIORNI = 7

MAX_GIORNI_IN_CODA = 30

# Le 19:00 sono una scelta prudente con margine (i dati del giorno prima sono
# risultati disponibili già alle 18:00), non un vincolo stretto - modificabile
# dalle opzioni dell'integrazione.
ORA_MINIMA_RICHIESTA = 19

# Limite di cortesia auto-imposto per l'azione recupera_storico (non un
# vincolo noto delle API E-Distribuzione): async_get_daily_load_profile
# supporta un intervallo multi-giorno in un'unica chiamata, confermato fino a
# 181 giorni - resta solo per evitare che un errore di battitura nella data
# richieda anni di dati per sbaglio in una singola risposta.
MAX_GIORNI_RECUPERO_STORICO = 190  # ~6 mesi
