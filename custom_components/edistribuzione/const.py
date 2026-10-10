"""E-Distribuzione protocol constants: login via Salesforce, data from MuleSoft."""
from __future__ import annotations

DOMAIN = "edistribuzione"

# --- Salesforce Experience Cloud (login / OAuth2 + PKCE) --------------------
# The same Salesforce org/community (PortaleClienti) answers on two hostnames:
# - private.e-distribuzione.it is the portal's custom domain, behind Imperva
#   (Incapsula). Since October 2026 it answers non-browser clients with the
#   "Pardon Our Interruption" anti-bot check, blocking both login and token
#   refresh.
# - edistribuzione.my.site.com is the canonical domain Salesforce assigns to
#   the org: same login page, same OAuth client, served by Salesforce's CDN
#   without Imperva in front.
# The integration uses the custom domain as PRIMARY and falls back to the
# direct one only when the primary is blocked, so it returns to the official
# domain by itself once Enel lifts the block (see auth.AuthClient).
SF_HOST_PRINCIPALE = "private.e-distribuzione.it"
SF_HOST_DIRETTO = "edistribuzione.my.site.com"
SF_HOSTS = (SF_HOST_PRINCIPALE, SF_HOST_DIRETTO)

SF_BASE = f"https://{SF_HOST_PRINCIPALE}/PortaleClienti"

OAUTH_AUTHORIZE_URL = f"{SF_BASE}/services/oauth2/authorize"
OAUTH_TOKEN_URL = f"{SF_BASE}/services/oauth2/token"
OAUTH_USERINFO_URL = f"{SF_BASE}/services/oauth2/userinfo"

LOGIN_PAGE_URL = f"{SF_BASE}/s/login/"
AURA_ENDPOINT = f"{SF_BASE}/s/sfsites/aura"
LOGINFLOW_URL = f"{SF_BASE}/loginflow/loginFlow.apexp"

# Client ID of the official E-Distribuzione app: public, native-app style, no
# client secret (PKCE). It is a third-party client on Enel's infrastructure,
# so treat it as subject to change or revocation without notice.
OAUTH_CLIENT_ID = (
    "3MVG9Rd3qC6oMalUVUXDfNEIi52RXSZMsndjnAnbka4R4Amhq6QrTj2U2Zw0sjyqOlFViLC"
    ".cTpau8fPEBV0V"
)
OAUTH_REDIRECT_URI = "eneldist://redirect"
OAUTH_SCOPE = "web api openid id profile email address phone refresh_token offline_access"

# --- Data backend (MuleSoft) -------------------------------------------------
MISURE_BASE_URL = "https://xs-misura-p.de-c1.eu1.cloudhub.io/xs/misure"

MISURE_READING_URL = f"{MISURE_BASE_URL}/reading"
MISURE_DAILY_LOAD_PROFILE_URL = f"{MISURE_BASE_URL}/querydailyloadprofile"
MISURE_MONTHLY_LOAD_PROFILE_URL = f"{MISURE_BASE_URL}/querymonthlyloadprofile"
MISURE_MONTHLY_TIME_OF_USE_URL = f"{MISURE_BASE_URL}/querymonthlytimeofuse"
MISURE_GET_SUPPLIES_URL = f"{MISURE_BASE_URL}/getSupplies"

# Application-level header the backend uses to pick permissions per endpoint,
# independent of the HTTP verb.
METHOD_USER_ELENCO_POD = "ELENCO_POD"
METHOD_USER_LETTURE = "LETTURE"
METHOD_USER_CURVA_GIORNO = "CURVE_DI_CARICO-GIORNO"
METHOD_USER_CURVA_MESE = "CURVE_DI_CARICO-MESE"
METHOD_USER_CURVA_PERIODO = "CURVE_DI_CARICO-PERIODO"

# --- Load curve magnitude ----------------------------------------------------
#
# These are the codes of the MuleSoft REST backend (querydailyloadprofile),
# not those of the web portal ("A+"/"A-"): two different backends with
# different vocabularies, linked only by the OAuth token.
MAGNITUDE_PRELEVATA = "A1"  # energy drawn from the grid
MAGNITUDE_IMMESSA = "A2"  # energy fed into the grid

MAGNITUDE_TUTTE = (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA)

# Directions for which the ARERA time-band series (F1/F2/F3, see fasce.py) are
# also generated. Consumption only: it is the energy billed by time band;
# injected energy has no per-band price in residential contracts.
DIREZIONI_CON_FASCE = (MAGNITUDE_PRELEVATA,)

# --- Stored configuration ----------------------------------------------------
CONF_PODS = "pods"  # list of POD codes, all on the same authenticated account
CONF_REFRESH_TOKEN = "refresh_token"

CONF_TIPO_POD = "tipo_pod"  # {pod: "scambio"|"produzione"}, in entry.options
TIPO_POD_SCAMBIO = "scambio"
TIPO_POD_PRODUZIONE = "produzione"
TIPO_POD_DEFAULT = TIPO_POD_SCAMBIO

# The role is always chosen by hand (options flow, tipo_pod step): it cannot be
# inferred reliably. 'HasPlant' from getSupplies is not usable (it is True on
# the exchange POD and null on the production one), and the POD code prefix
# does not generalize across DSOs and customers.

# --- Automatic daily curve import + retry queue ------------------------------
DEFAULT_UPDATE_INTERVAL_MINUTES = 60

# Data is published with a one-day delay: the day just ended is not available
# yet, the previous one is. The queue below keeps this robust even if the real
# delay were longer.
RITARDO_DATI_GIORNI = 1

# E-Distribuzione may correct data already published: a day arrives with one
# value and days later comes back corrected. Without a periodic recheck that
# correction would never be seen, so every automatic cycle always requests the
# last GIORNI_RICONTROLLO days (not only the expected day), in a single request
# per direction.
GIORNI_RICONTROLLO = 3

# Notice in Settings > Repairs when E-Distribuzione answers with the anti-bot
# check instead of the token (see auth.AccessoBloccato).
ISSUE_ACCESSO_BLOCCATO = "accesso_bloccato"

CONF_DATA_INSTALLAZIONE = "data_installazione"
CONF_GIORNI_DA_RIPROVARE = "giorni_da_riprovare"
CONF_ORA_RICHIESTA = "ora_richiesta"

# A day stays in the queue and is retried on later cycles, then dropped after
# this many real days from when it was first queued (not after N attempts).
# About a week covers publishing delays without hammering dates that will
# never arrive.
ABBANDONO_CODA_DOPO_GIORNI = 7

MAX_GIORNI_IN_CODA = 30

# 19:00 is a safe margin (the previous day is usually available by 18:00), not
# a hard constraint. Configurable from the integration options.
ORA_MINIMA_RICHIESTA = 19

# Self-imposed courtesy limit for the recupera_storico action (not a known
# E-Distribuzione API constraint): async_get_daily_load_profile supports a
# multi-day range in a single call, up to 181 days. This only prevents a typo
# in the dates from requesting years of data by mistake in one response.
MAX_GIORNI_RECUPERO_STORICO = 190  # ~6 months
