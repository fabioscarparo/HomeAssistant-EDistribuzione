"""E-Distribuzione protocol constants: login via Salesforce, data from MuleSoft."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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
# Direct Salesforce org host (instance_url): bypasses both Imperva anti-bot on
# private.e-distribuzione.it and Transaction Security app_blocked on my.site.com.
SF_HOST_TOKEN = "edistribuzione.my.salesforce.com"
SF_HOSTS = (SF_HOST_PRINCIPALE, SF_HOST_DIRETTO)

# Allowed HTTPS hosts when following login redirects, bridge pages and forms.
# Exact hosts and .e-distribuzione.it subdomains are listed here; Salesforce
# org domains are additionally scoped to E-Distribuzione's own org prefixes
# (HOST_SALESFORCE_PREFISSI + HOST_SALESFORCE_SUFFISSI) so arbitrary third-party
# Salesforce orgs on *.my.site.com / *.salesforce.com / *.force.com are rejected.
HOST_LOGIN_AMMESSI = (
    *SF_HOSTS,
    SF_HOST_TOKEN,
    ".e-distribuzione.it",
)
HOST_SALESFORCE_PREFISSI = ("edistribuzione.", "edistribuzione--")
HOST_SALESFORCE_SUFFISSI = (
    ".salesforce.com",
    ".force.com",
    ".my.site.com",
    ".salesforce-sites.com",
)

SF_BASE = f"https://{SF_HOST_PRINCIPALE}/PortaleClienti"

OAUTH_AUTHORIZE_URL = f"{SF_BASE}/services/oauth2/authorize"
OAUTH_TOKEN_URL = f"{SF_BASE}/services/oauth2/token"
OAUTH_TOKEN_URL_DIRETTO = f"https://{SF_HOST_TOKEN}/services/oauth2/token"
OAUTH_REVOKE_URL = f"{SF_BASE}/services/oauth2/revoke"
OAUTH_REVOKE_URL_DIRETTO = f"https://{SF_HOST_TOKEN}/services/oauth2/revoke"
OAUTH_USERINFO_URL = f"{SF_BASE}/services/oauth2/userinfo"

LOGIN_PAGE_URL = f"{SF_BASE}/s/login/"
AURA_ENDPOINT = f"{SF_BASE}/s/sfsites/aura"
LOGINFLOW_URL = f"{SF_BASE}/loginflow/loginFlow.apexp"

# Client ID of the updated official E-Distribuzione app (iOS 1.5.4 / Android 1.5.8,
# November 2026 ForceUpdate): public, native-app style, no client secret (PKCE).
# Treat it as subject to change or revocation without notice.
OAUTH_CLIENT_ID = (
    "3MVG9Rd3qC6oMalUVUXDfNEIi5wmqcb6MVZMb_WQdp_fCJ1zvnx6ufi2J4KW7ikvzlMVVbdwYjeEMAZrdrYoP"
)
# Previous app client (used until October 2026). A refresh_token only works with
# the client_id that issued it, so entries created before saving CONF_CLIENT_ID_OAUTH
# keep refreshing with this client until their next sign-in or reauth.
OAUTH_CLIENT_ID_PRECEDENTE = (
    "3MVG9Rd3qC6oMalUVUXDfNEIi52RXSZMsndjnAnbka4R4Amhq6QrTj2U2Zw0sjyqOlFViLC"
    ".cTpau8fPEBV0V"
)
OAUTH_REDIRECT_URI = "eneldist://redirect"
OAUTH_SCOPE = "web api openid id profile email address phone refresh_token offline_access"

# Automated password+OTP login has been blocked by E-Distribuzione since October 2026
# (Imperva on private.e-distribuzione.it, "Errore Tecnico" on my.site.com). While
# False, setup and reauth go straight to browser sign-in. Set back to True if
# automated login starts working again.
LOGIN_PASSWORD_DISPONIBILE = False

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
MAGNITUDE_PRELEVATA = "A1"  # active energy drawn from the grid (kWh)
MAGNITUDE_IMMESSA = "A2"  # active energy fed into the grid (kWh)

# IEC four-quadrant reactive energy magnitudes (kvarh), opt-in via options.
MAGNITUDE_REATTIVA_R1 = "R1"  # reactive inductive withdrawn (Q1)
MAGNITUDE_REATTIVA_R2 = "R2"  # reactive capacitive injected (Q2)
MAGNITUDE_REATTIVA_R3 = "R3"  # reactive inductive injected (Q3)
MAGNITUDE_REATTIVA_R4 = "R4"  # reactive capacitive withdrawn (Q4)

MAGNITUDE_TUTTE = (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA)
MAGNITUDE_REATTIVE = (
    MAGNITUDE_REATTIVA_R1,
    MAGNITUDE_REATTIVA_R2,
    MAGNITUDE_REATTIVA_R3,
    MAGNITUDE_REATTIVA_R4,
)

# Directions for which the ARERA time-band series (F1/F2/F3, see fasce.py) are
# also generated: both, as E-Distribuzione accounts consumption and injection
# by band (issue #7).
DIREZIONI_CON_FASCE = (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA)

# --- Stored configuration ----------------------------------------------------
CONF_PODS = "pods"  # list of POD codes, all on the same authenticated account
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_CLIENT_ID_OAUTH = "client_id_oauth"
CONF_INSTANCE_URL = "instance_url"
CONF_TOKEN_ORIGINE = "token_origine"
CONF_TOKEN_AGGIORNATO_IL = "token_aggiornato_il"


def client_id_della_entry(dati: Mapping[str, Any]) -> str:
    """OAuth client_id to use when refreshing an entry's refresh_token."""
    return dati.get(CONF_CLIENT_ID_OAUTH) or OAUTH_CLIENT_ID_PRECEDENTE

MAX_EVENTI_CRONOLOGIA = 15

CONF_TIPO_POD = "tipo_pod"  # {pod: "scambio"|"produzione"}, in entry.options
CONF_ENERGIA_REATTIVA = "energia_reattiva"  # bool in entry.options, opt-in
TIPO_POD_SCAMBIO = "scambio"
TIPO_POD_PRODUZIONE = "produzione"
TIPO_POD_DEFAULT = TIPO_POD_SCAMBIO

# MeteringPointType from getSupplies ("M1" exchange, "M2" production) is used
# to suggest the initial role during setup; the user can always override it.

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

# Notices in Settings > Repairs when E-Distribuzione answers with the anti-bot
# check instead of the token (see auth.AccessoBloccato), or rejects the OAuth
# client the integration uses (see auth.AppBloccata).
ISSUE_ACCESSO_BLOCCATO = "accesso_bloccato"
ISSUE_APP_BLOCCATA = "app_bloccata"

CONF_DATA_INSTALLAZIONE = "data_installazione"
CONF_GIORNI_DA_RIPROVARE = "giorni_da_riprovare"
CONF_ORA_RICHIESTA = "ora_richiesta"

# A day stays in the queue and is retried on later cycles, then dropped after
# this many real days from when it was first queued (not after N attempts).
# Two weeks: some meters have their curves published several days late
# (issue #11), and the queued days go in the same daily request as the
# others, so keeping them longer costs no extra request.
ABBANDONO_CODA_DOPO_GIORNI = 14

MAX_GIORNI_IN_CODA = 30

# 19:00 is a safe margin (the previous day is usually available by 18:00), not
# a hard constraint. Configurable from the integration options.
ORA_MINIMA_RICHIESTA = 19

# Maximum span per recupera_storico invocation (confirmed 200 OK up to 365 days;
# internally split into CHUNK_GIORNI_STORICO chunks to keep request latency safe).
MAX_GIORNI_RECUPERO_STORICO = 365
CHUNK_GIORNI_STORICO = 180

# Official monthly readings (/xs/misure/reading) are reliable up to ~90 days.
FINESTRA_LETTURE_GIORNI = 65

# Explicit HTTP timeouts so a stalled backend cannot block the coordinator.
HTTP_TIMEOUT_SECONDS = 30
HTTP_CONNECT_TIMEOUT_SECONDS = 10


def issue_id_per_entry(issue_base: str, entry_id: str) -> str:
    """Per-entry Repairs issue id so multiple accounts do not overwrite each other."""
    return f"{issue_base}_{entry_id}"
