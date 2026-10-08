"""Client di autenticazione per E-Distribuzione (private.e-distribuzione.it).

Login OAuth2 Authorization Code + PKCE standard, ma la parte difficile sta nel
mezzo: le credenziali si sottomettono via un'azione Aura (Salesforce
Lightning), e l'OTP passa da una classica pagina Visualforce "Login Flow"
(ViewState/RichFaces). Sono dettagli di implementazione della UI, non un'API
pubblica stabile: questo modulo scrapa HTML/JSON con regex, e SI ROMPERA' se
Enel cambia il template Experience Cloud o la versione di RichFaces. I punti
più fragili sono le funzioni statiche di scraping in fondo al file - il primo
posto da controllare se il login inizia a fallire.

Una volta ottenuto un refresh_token, questa fragilità non serve più:
AuthClient.async_refresh_access_token() parla solo con l'endpoint OAuth2
standard.

async_begin_login esegue in sequenza:
    1. _carica_pagina_login       GET /oauth2/authorize (PKCE) -> pagina di login
    2. _invia_credenziali         POST Aura loginUser -> URL di frontdoor.jsp
    3. _segui_a_pagina_otp        GET frontdoor -> bridge JS -> pagina OTP
    4. _async_avvia_invio_otp     submit vuoto: fa DAVVERO partire l'invio dell'OTP

async_submit_otp esegue poi:
    5. _sottometti_codice_otp          POST del form con l'OTP -> meta Location
    6. _scarica_pagina_dopo_otp        segue quel redirect
    7. _estrai_codice_autorizzazione   trova code/state (gestendo il consenso
                                        OAuth alla primissima autorizzazione)
    8. _async_exchange_code            POST /oauth2/token
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_lib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

import aiohttp

from .const import (
    AURA_ENDPOINT,
    LOGINFLOW_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
)

_LOGGER = logging.getLogger(__name__)

_MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"

_MAX_REDIRECT_HOPS = 15

# Nomi di campo di fallback del form RichFaces del login flow, usati solo se
# _analizza_pagina_otp non riesce a scoprirli dinamicamente dalla pagina reale
# (i prefissi "thePage:j_id2:..." sono generati da Salesforce e possono
# cambiare tra deploy).
_CAMPO_OTP_INPUT_DEFAULT = "thePage:j_id2:i:f:pb:d:element___input____OTP_Input"
_CAMPO_RICHIEDI_NUOVO_OTP_DEFAULT = "thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"
_CAMPO_NEXT_AJAX_DEFAULT = "thePage:j_id2:i:f:pb:pbb:nextAjax"

# Frammenti con cui il portale segnala che l'account ha troppe sessioni
# aperte contemporaneamente ("Hai superato il numero di sessioni simultanee
# consentite", osservato sul sito reale). Va distinto da credenziali sbagliate
# e da un cambio di markup: in questo stato il login non prosegue e l'OTP non
# viene nemmeno inviato, quindi l'utente resterebbe ad aspettare un codice che
# non arriva.
_MARCATORI_TROPPE_SESSIONI = (
    "sessioni simultanee",
    "sessioni contemporanee",
    "numero di sessioni",
    "sessioni consentite",
)

# Pagine della protezione antibot di Imperva (Incapsula), che risponde al
# posto di login e token endpoint quando classifica il traffico come
# automatico: la verifica JavaScript "Pardon Our Interruption" (osservata
# l'8 ottobre 2026, status 200) e il blocco "Incapsula incident ID". Solo
# marcatori di queste due pagine: lo script _Incapsula_Resource può
# comparire anche nelle pagine normali di un sito protetto.
_MARCATORI_ANTIBOT = (
    "pardon our interruption",
    "incapsula incident id",
)

# Testo con cui la risposta al primo submit del form conferma di avere
# spedito il codice (confermato su HAR reale: "Abbiamo inviato un codice a 5
# cifre al tuo indirizzo email"). Più varianti perché il canale (email/SMS) e
# la formulazione cambiano da account ad account: l'assenza di conferma NON
# viene trattata come errore fatale (romperebbe login altrimenti validi con
# una formulazione diversa), solo segnalata.
_MARCATORI_OTP_INVIATO = (
    "abbiamo inviato",
    "inviato un codice",
    "codice a 5 cifre",
    "codice è stato inviato",
    "nuovo codice",
)

# Etichette del pulsante che APPROVA sulla schermata di consenso OAuth. Sulla
# pagina reale i due pulsanti hanno lo STESSO name="save" e si distinguono
# solo per il valore: "Consenti" approva, " Nega " (con gli spazi) rifiuta -
# sbagliare pulsante significa negare l'autorizzazione all'integrazione,
# quindi il valore va confrontato per intero, non cercato come sottostringa.
_ETICHETTE_CONSENSO = ("consenti", "allow", "approve", "autorizza", "accetta")


def _contiene(testo: str, marcatori: tuple[str, ...]) -> bool:
    minuscolo = testo.lower()
    return any(marcatore in minuscolo for marcatore in marcatori)


def _scrivi_pagina_debug(html: str, nome_file: str) -> None:
    try:
        percorso = Path(nome_file)
        percorso.write_text(html, encoding="utf-8")
    except OSError as exc:
        _LOGGER.debug("Impossibile salvare %s su disco: %s", nome_file, exc)
        return
    _LOGGER.error("Pagina completa salvata in %s", percorso.resolve())


def _salva_pagina_debug(html: str, nome_file: str) -> None:
    """Scrive la pagina su disco (best-effort) e ne logga il percorso, così è
    richiedibile in una segnalazione senza dover far catturare una HAR.

    La scrittura è delegata a un executor quando siamo dentro l'event loop di
    Home Assistant, che segnala l'I/O sincrono come "blocking call". Fuori da
    un event loop (test, scripts/verify_login.py) scrive direttamente.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _scrivi_pagina_debug(html, nome_file)
        return
    # Deliberatamente non atteso: è un dump diagnostico best-effort, non deve
    # rallentare né far fallire il login se il disco è lento o pieno.
    loop.run_in_executor(None, _scrivi_pagina_debug, html, nome_file)


def _log_parsing_failure_context(html: str, campo_cercato: str) -> None:
    """Logga titolo + anteprima della pagina quando un campo atteso non si
    trova - distingue "regex sbagliato" (il campo c'è ma in forma diversa) da
    "pagina completamente diversa da quella attesa" senza dover chiedere
    un'altra cattura per scoprirlo. Salva anche la pagina intera su disco."""
    title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else "(nessun <title> trovato)"
    _LOGGER.error(
        "Campo '%s' non trovato. Titolo pagina: %r. Lunghezza: %d caratteri. "
        "Primi 300 caratteri: %r",
        campo_cercato,
        title,
        len(html),
        html[:300],
    )
    _salva_pagina_debug(html, "otp_page_debug.html")


class AuthError(Exception):
    """Errore generico di autenticazione."""


class InvalidCredentials(AuthError):
    """Email o password errate."""


class InvalidOtp(AuthError):
    """Codice OTP errato o scaduto."""


class TroppeSessioni(AuthError):
    """L'account ha già troppe sessioni aperte: il portale rifiuta il login
    prima di inviare qualunque OTP.

    Non è un problema di credenziali né di markup cambiato: finché le altre
    sessioni non vengono chiuse o non scadono (app ufficiale, sito, tentativi
    precedenti di questa stessa integrazione) non c'è niente da reinserire
    nel form, va liberata una sessione e riprovato.
    """


class AccessoBloccato(AuthError):
    """E-Distribuzione ha risposto con la pagina antibot di Imperva invece
    che con quella attesa: l'accesso automatico è bloccato lato server.

    Non è un problema di credenziali né di markup cambiato, e qui non c'è
    niente da correggere: va segnalato in modo chiaro e riprovato più tardi,
    senza insistere.
    """


def _verifica_non_bloccato(testo: str) -> None:
    """Alza AccessoBloccato se la risposta è la pagina antibot."""
    if _contiene(testo, _MARCATORI_ANTIBOT):
        raise AccessoBloccato("E-Distribuzione ha risposto con la pagina antibot (Imperva)")


async def _leggi_testo(resp: aiohttp.ClientResponse) -> str:
    """Corpo della risposta, dopo aver escluso la pagina antibot."""
    testo = await resp.text()
    _verifica_non_bloccato(testo)
    return testo


class ParsingError(AuthError):
    """La pagina scrapata non conteneva quanto ci si aspettava.

    Causa più probabile: Enel ha cambiato qualcosa nel loro sito Salesforce e
    le regex sotto vanno aggiornate. Cattura una HAR fresca e confronta.
    """


async def _get_following_redirects(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
) -> aiohttp.ClientResponse:
    """GET seguendo i redirect a mano invece di allow_redirects=True.

    Necessario perché la catena di redirect di /services/oauth2/authorize
    passa per un parametro 'startURL' che contiene un URL annidato
    percent-encoded. Lasciando che aiohttp gestisca i redirect da solo, il
    comportamento dipende da come la libreria ri-codifica un URL già
    codificato ad ogni hop e può entrare in un loop infinito
    (TooManyRedirects, riprodotto e confermato con un HAR reale). Seguendo i
    redirect esplicitamente con encoded=True sull'URL del prossimo hop, il
    valore del Location non viene mai ri-processato: la stessa catena che con
    allow_redirects=True va in loop qui si risolve in pochi hop.

    Ritorna la risposta finale (status < 300, nessun altro Location); il
    chiamante è responsabile di chiudere/consumare 'resp' come al solito.
    """
    resp = await session.get(url, params=params, headers=headers, allow_redirects=False)

    for _ in range(_MAX_REDIRECT_HOPS):
        location = resp.headers.get("Location")
        if location is None:
            return resp

        resp.close()
        location_url = aiohttp.client.URL(location, encoded=True)
        next_url = location_url if location_url.is_absolute() else resp.url.join(location_url)
        resp = await session.get(next_url, headers=headers, allow_redirects=False)

    resp.close()
    raise AuthError(
        f"Troppi redirect (>{_MAX_REDIRECT_HOPS}) seguendo {url}: possibile "
        "cambiamento lato Salesforce nella struttura dei redirect di login."
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


@dataclass
class _LoginFlowState:
    """Tutto ciò che serve portare dalla pagina OTP alla sua sottomissione."""

    fwuid: str | None = None
    aura_token: str | None = None
    view_state: str | None = None
    view_state_version: str | None = None
    view_state_mac: str | None = None
    view_state_csrf: str | None = None
    form_action_url: str | None = None
    # I prefissi dei campi sotto 'thePage:j_id2:i:f:...' sono generati da
    # Salesforce e variano tra deploy: si conserva il nome scoperto sulla
    # pagina reale invece di dare per scontati quelli hardcoded (usati solo
    # come fallback nei rispettivi costruttori di form).
    otp_field_name: str | None = None
    resend_field_name: str | None = None
    # La checkbox visibile "Richiedi nuovo OTP", gemella del campo nascosto
    # sopra: serve solo per il reinvio esplicito, dove riproduciamo il submit
    # di un browser con la casella spuntata.
    resend_checkbox_field_name: str | None = None
    next_field_name: str | None = None


@dataclass
class OAuthTokens:
    access_token: str
    refresh_token: str
    instance_url: str
    id_token: str | None = None
    raw: dict = field(default_factory=dict)


class AuthClient:
    """Guida la sequenza login -> OTP -> code OAuth -> scambio token."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._code_verifier: str | None = None
        self._oauth_state: str | None = None
        self._flow = _LoginFlowState()
        # True/False dopo async_begin_login()/async_resend_otp() a seconda
        # che il portale abbia confermato l'invio del codice; None finché non
        # ci si è arrivati. Il config flow lo usa per avvisare l'utente
        # invece di lasciarlo aspettare un OTP che non arriverà mai.
        self.otp_invio_confermato: bool | None = None

    # -- Passo 1-4: login + OTP -------------------------------------------

    async def async_begin_login(self, email: str, password: str) -> None:
        """Sottomette email/password e arriva alla pagina OTP pronta per la
        convalida. Alza un'eccezione se le credenziali vengono rifiutate.

        Assume sempre che segua un passo OTP, coerente con ogni cattura vista
        finora.
        """
        login_page_html, start_url_value, aura_page_uri, referer = (
            await self._carica_pagina_login(email)
        )
        frontdoor_url = await self._invia_credenziali(
            email, password, login_page_html, start_url_value, aura_page_uri, referer
        )
        otp_page_html = await self._segui_a_pagina_otp(frontdoor_url)
        self._analizza_pagina_otp(otp_page_html)
        await self._async_avvia_invio_otp()

    async def _carica_pagina_login(self, email: str) -> tuple[str, str, str, str]:
        """Passo 1: GET /oauth2/authorize con PKCE, seguendo i redirect a
        mano (vedi _get_following_redirects) fino alla pagina di login
        Salesforce. Genera anche code_verifier/oauth_state usati più avanti.

        Ritorna (login_page_html, start_url_value, aura_page_uri, referer).
        """
        self._code_verifier, code_challenge = _make_pkce_pair()
        self._oauth_state = _b64url(secrets.token_bytes(16))

        headers = {"User-Agent": _MOBILE_USER_AGENT}
        params = {
            "prompt": "login",
            "nonce": _b64url(secrets.token_bytes(16)),
            "display": "touch",
            "response_type": "code",
            "scope": OAUTH_SCOPE,
            "login_hint": email,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "state": self._oauth_state,
        }

        resp = await _get_following_redirects(
            self._session, OAUTH_AUTHORIZE_URL, params=params, headers=headers
        )
        login_page_html = await _leggi_testo(resp)
        # Il parametro 'startURL' della pagina su cui atterriamo contiene un
        # token 'source=...' generato dal server al primo hop, che il server
        # pretende di riavere indietro nel campo 'startUrl' del passo
        # successivo per validare che il login appartenga allo stesso flusso
        # di autorizzazione - confermato confrontando con una richiesta reale
        # riuscita: mandare un path nudo (senza questo token) produce un
        # AuraClientInputException generico.
        start_url_value = resp.url.query.get(
            "startURL", "/PortaleClienti/setup/secur/RemoteAccessAuthorizationPage.apexp"
        )
        aura_page_uri = resp.url.path_qs
        referer = str(resp.url)
        resp.close()
        return login_page_html, start_url_value, aura_page_uri, referer

    async def _invia_credenziali(
        self,
        email: str,
        password: str,
        login_page_html: str,
        start_url_value: str,
        aura_page_uri: str,
        referer: str,
    ) -> str:
        """Passo 2: estrae fwuid/loaded dalla pagina di login, sottomette
        l'azione Aura loginUser, e ritorna l'URL di frontdoor.jsp da
        seguire."""
        headers = {"User-Agent": _MOBILE_USER_AGENT}

        self._flow.fwuid = self._estrai_fwuid(login_page_html)
        loaded = self._estrai_loaded(login_page_html)
        # aura.token: confermato su una HAR reale con login riuscito che il
        # client invia letteralmente la stringa "null" (non un token vero)
        # per questa azione specifica, e il server la accetta
        # ("state":"SUCCESS"). Verosimilmente non c'è ancora una sessione
        # autenticata da proteggere via CSRF a questo punto del flusso.
        self._flow.aura_token = "null"

        # json.dumps invece di concatenazione di stringhe: evita JSON
        # malformato se email/password contenessero virgolette o backslash.
        message = json.dumps({
            "actions": [{
                "id": "1;a",
                "descriptor": "apex://PED_LoginController/ACTION$loginUser",
                "callingDescriptor": "markup://c:PED_Login",
                "params": {
                    "username": email,
                    "password": password,
                    "startUrl": start_url_value,
                },
            }]
        })
        # 'loaded' NON può essere {} (vuoto): il server risponde con un
        # generico AuraClientInputException se non corrisponde a quanto si
        # aspetta - confermato confrontando con una richiesta reale riuscita,
        # dove conteneva un riferimento alla versione del componente
        # caricato (es. {"APPLICATION@markup://siteforce:loginApp2":"..."}).
        aura_context = json.dumps({
            "mode": "PROD",
            "fwuid": self._flow.fwuid,
            "app": "siteforce:loginApp2",
            "loaded": json.loads(loaded),
            "dn": [],
            "globals": {},
            "uad": True,
        })
        data = {
            "message": message,
            "aura.context": aura_context,
            "aura.pageURI": aura_page_uri,
            "aura.token": self._flow.aura_token,
        }

        # X-SFDC-Page-Scope-Id: mai visto in nessuna risposta del server,
        # solo nelle richieste - verosimilmente generato lato client (un ID
        # di correlazione riusato identico su tutte le chiamate Aura).
        # Origin/Referer/Content-Type: presenti nella richiesta reale
        # riuscita, non individualmente confermati come necessari ma a
        # rischio zero da aggiungere.
        login_headers = {
            **headers,
            "X-SFDC-Page-Scope-Id": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://private.e-distribuzione.it",
            "Referer": referer,
        }

        async with self._session.post(
            f"{AURA_ENDPOINT}?r=2&other.PED_Login.loginUser=1",
            data=data,
            headers=login_headers,
        ) as resp:
            raw_text = await _leggi_testo(resp)
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as err:
                _LOGGER.error(
                    "Risposta non-JSON da loginUser (status %s). Primi 500 "
                    "caratteri del corpo: %r",
                    resp.status,
                    raw_text[:500],
                )
                raise ParsingError(
                    f"Risposta non-JSON da loginUser (status {resp.status}): "
                    f"{raw_text[:200]!r}"
                ) from err

        try:
            return_value = payload["actions"][0]["returnValue"]
        except (KeyError, IndexError) as err:
            raise ParsingError("Forma inattesa della risposta di loginUser") from err

        if not isinstance(return_value, str) or not return_value.startswith("OK:"):
            # Il limite di sessioni contemporanee arriva qui come messaggio
            # di errore della stessa action del login: trattarlo come
            # "credenziali non valide" manderebbe l'utente a ricontrollare
            # email e password mentre il problema è altrove.
            self._verifica_non_troppe_sessioni(str(return_value))
            raise InvalidCredentials(str(return_value))

        # Seguirlo stabilisce il cookie di sessione 'sid' e atterra su una
        # pagina-ponte che fa un redirect via JAVASCRIPT (non HTTP) verso il
        # vero form OTP - vedi _segui_a_pagina_otp.
        return return_value[len("OK:") :]

    async def _segui_a_pagina_otp(self, frontdoor_url: str) -> str:
        """Passo 3: frontdoor.jsp stabilisce il cookie di sessione 'sid' e
        atterra su una pagina-ponte che fa un redirect via JAVASCRIPT
        (window.location.replace(...), non un redirect HTTP) verso il vero
        form OTP - un browser lo segue automaticamente, qui va estratto e
        seguito a mano.
        """
        headers = {"User-Agent": _MOBILE_USER_AGENT}

        resp = await _get_following_redirects(self._session, frontdoor_url, headers=headers)
        bridge_html = await _leggi_testo(resp)
        resp.close()

        otp_form_url = self._estrai_url_redirect_js(bridge_html)
        resp = await _get_following_redirects(self._session, otp_form_url, headers=headers)
        otp_page_html = await _leggi_testo(resp)
        resp.close()
        return otp_page_html

    async def _async_avvia_invio_otp(self) -> None:
        """Passo 4: il form OTP non parte da solo al caricamento della
        pagina - serve un primo submit del bottone, SENZA codice, per farlo
        davvero inviare (confermato su HAR reale: la risposta contiene
        testualmente "Abbiamo inviato un codice a 5 cifre al tuo indirizzo
        email", non solo SMS nonostante il nome del campo)."""
        await self._async_post_form_otp(self._form_invio_iniziale())

    async def async_resend_otp(self) -> bool:
        """Fa rimandare un OTP nuovo DENTRO la sessione di login già avviata.

        Serve perché un OTP generato altrove (app ufficiale o sito) appartiene
        a un'altra "interview" del login flow di Salesforce e non può essere
        convalidato qui: se il codice non arriva, l'unico modo di ottenerne
        uno valido è farlo rispedire da questa stessa sessione.

        Ritorna True se il portale ha confermato l'invio.
        """
        if self._flow.view_state is None:
            raise AuthError("async_begin_login() deve riuscire prima di async_resend_otp()")
        await self._async_post_form_otp(self._form_richiedi_nuovo())
        return bool(self.otp_invio_confermato)

    def _campi_comuni_form_otp(self) -> dict[str, str]:
        """Campi RichFaces comuni a tutti i submit del login flow (invio
        iniziale, reinvio, convalida): cambiano solo il campo OTP e il flag
        di reinvio, gestiti dai tre costruttori sotto."""
        next_field = self._flow.next_field_name or _CAMPO_NEXT_AJAX_DEFAULT
        return {
            "AJAXREQUEST": "_viewRoot",
            "thePage:j_id2:i:f": "thePage:j_id2:i:f",
            "thePage:j_id2:i:f:pb:d:navigationType": "",
            "com.salesforce.visualforce.ViewState": self._flow.view_state,
            "com.salesforce.visualforce.ViewStateVersion": self._flow.view_state_version,
            "com.salesforce.visualforce.ViewStateMAC": self._flow.view_state_mac,
            "com.salesforce.visualforce.ViewStateCSRF": self._flow.view_state_csrf,
            next_field: next_field,
        }

    def _form_invio_iniziale(self) -> dict[str, str]:
        """Primo submit, vuoto: niente campo OTP né flag di reinvio - un
        submit "neutro" del form appena caricato, che però è ciò che fa
        davvero partire l'invio del codice (vedi _async_avvia_invio_otp)."""
        return self._campi_comuni_form_otp()

    def _form_richiedi_nuovo(self) -> dict[str, str]:
        """Submit con la richiesta di un nuovo codice: imposta il flag di
        reinvio (campo nascosto) più la checkbox visibile che lo specchia,
        se conosciuta - vedi il commento sui due campi omonimi in
        _analizza_pagina_otp."""
        dati = self._campi_comuni_form_otp()
        dati[self._flow.resend_field_name or _CAMPO_RICHIEDI_NUOVO_OTP_DEFAULT] = "true"
        if self._flow.resend_checkbox_field_name:
            dati[self._flow.resend_checkbox_field_name] = "true"
        return dati

    def _form_convalida(self, otp_code: str) -> dict[str, str]:
        """Submit con il codice da convalidare."""
        dati = self._campi_comuni_form_otp()
        dati[self._flow.otp_field_name or _CAMPO_OTP_INPUT_DEFAULT] = otp_code
        dati[self._flow.resend_field_name or _CAMPO_RICHIEDI_NUOVO_OTP_DEFAULT] = "false"
        return dati

    async def _async_post_form_otp(self, data: dict[str, str]) -> None:
        """Submit del form OTP che NON porta un codice da convalidare (invio
        iniziale o reinvio): controlla che il portale abbia davvero spedito
        l'OTP e aggiorna ViewState/CSRF dalla risposta.

        I token nella risposta di QUESTA chiamata sono quelli da usare per
        l'invio effettivo del codice (async_submit_otp), non quelli della
        pagina di atterraggio: ruotano ad ogni submit del form.
        """
        headers = {"User-Agent": _MOBILE_USER_AGENT, "Faces-Request": "partial/ajax"}
        url = self._flow.form_action_url or LOGINFLOW_URL
        async with self._session.post(url, data=data, headers=headers) as resp:
            body = await _leggi_testo(resp)

        self._verifica_non_troppe_sessioni(body, "otp_send_debug.html")

        self.otp_invio_confermato = _contiene(body, _MARCATORI_OTP_INVIATO)
        if not self.otp_invio_confermato:
            # Non fatale di proposito: la pagina potrebbe confermare l'invio
            # con parole che non conosciamo ancora, e abortire qui
            # romperebbe login altrimenti validi. Ma va segnalato, perché
            # l'altra possibilità è che il codice non sia MAI stato spedito.
            _LOGGER.error(
                "E-Distribuzione non ha confermato l'invio del codice OTP "
                "(nessuno dei messaggi attesi %r nella risposta, lunga %d "
                "caratteri). Se il codice non arriva né via email né via "
                "SMS, allega otp_send_debug.html alla segnalazione.",
                _MARCATORI_OTP_INVIATO,
                len(body),
            )
            _salva_pagina_debug(body, "otp_send_debug.html")

        self._analizza_pagina_otp(body)

    # -- Passi 5-8: convalida OTP + scambio token -------------------------

    async def async_submit_otp(self, otp_code: str) -> OAuthTokens:
        """Sottomette l'OTP e completa lo scambio del code OAuth2."""
        if self._flow.view_state is None:
            raise AuthError("async_begin_login() deve riuscire prima di async_submit_otp()")

        next_url = await self._sottometti_codice_otp(otp_code)
        consent_html = await self._scarica_pagina_dopo_otp(next_url)
        auth_code = await self._estrai_codice_autorizzazione(consent_html)
        return await self._async_exchange_code(auth_code)

    async def _sottometti_codice_otp(self, otp_code: str) -> str:
        """Passo 5: POST del form con il codice; ritorna l'URL scoperto nel
        meta tag Location della risposta."""
        headers = {"User-Agent": _MOBILE_USER_AGENT, "Faces-Request": "partial/ajax"}
        data = self._form_convalida(otp_code)

        url = self._flow.form_action_url or LOGINFLOW_URL
        async with self._session.post(url, data=data, headers=headers) as resp:
            body = await _leggi_testo(resp)

        self._verifica_non_troppe_sessioni(body, "otp_submit_response_debug.html")

        if "Codice OTP" in body and "errato" in body.lower():
            raise InvalidOtp(body[:500])

        loc_match = re.search(r'name="Location"\s+content="([^"]+)"', body)
        if not loc_match:
            _log_parsing_failure_context(body, "meta Location dopo l'invio dell'OTP")
            _salva_pagina_debug(body, "otp_submit_response_debug.html")
            raise ParsingError(
                "Redirect (meta Location) non trovato nella risposta dopo "
                f"l'invio dell'OTP (risposta lunga {len(body)} caratteri - "
                "vedi log per un'anteprima)"
            )
        next_url = unquote(loc_match.group(1))
        if next_url.startswith("/"):
            next_url = "https://private.e-distribuzione.it" + next_url
        return next_url

    async def _scarica_pagina_dopo_otp(self, next_url: str) -> str:
        """Passo 6: GET della pagina a cui punta il redirect - di norma
        innesca poi un redirect JS verso eneldist://redirect?code=..., che
        aiohttp non può seguire (schema custom, non HTTP): il codice si
        scrapa dal testo qui sotto, non navigando."""
        headers = {"User-Agent": _MOBILE_USER_AGENT}
        async with self._session.get(next_url, headers=headers) as resp:
            return await _leggi_testo(resp)

    async def _estrai_codice_autorizzazione(self, consent_html: str) -> str:
        """Passo 7: trova code/state nella pagina. Se assenti, è la prima
        autorizzazione di questo account: Salesforce mostra una schermata di
        consenso esplicita ("Consentire l'accesso?") invece di rimandare
        subito il codice - va approvata al posto dell'utente, poi si ricerca
        di nuovo. Dalla seconda volta in poi il consenso resta memorizzato
        lato Salesforce e questo ramo non viene più eseguito."""
        code_match = re.search(r"[?&]code=([^&'\"]+)", consent_html)
        state_match = re.search(r"[?&]state=([^&'\"]+)", consent_html)

        if not code_match:
            risposta_consenso = await self._async_approva_consenso(consent_html)
            if risposta_consenso is not None:
                consent_html = risposta_consenso
                code_match = re.search(r"[?&]code=([^&'\"]+)", consent_html)
                state_match = re.search(r"[?&]state=([^&'\"]+)", consent_html)

        if not code_match:
            _log_parsing_failure_context(
                consent_html, "authorization code sulla pagina di consenso"
            )
            _salva_pagina_debug(consent_html, "consent_page_debug.html")
            raise ParsingError(
                "Authorization code non trovato nella risposta della pagina "
                f"di consenso (pagina lunga {len(consent_html)} caratteri - "
                "vedi log per un'anteprima)"
            )

        self._valida_state(state_match)
        return unquote(code_match.group(1))

    def _valida_state(self, state_match: re.Match[str] | None) -> None:
        """Il parametro 'state' è la protezione CSRF standard di OAuth:
        generato e mandato da noi in _carica_pagina_login, dovrebbe tornare
        identico. Solo un warning, non un errore bloccante: non verificato
        dal vivo se il confronto regge sempre (es. differenze di codifica) -
        un falso positivo qui romperebbe un login altrimenti valido."""
        if state_match and unquote(state_match.group(1)) != self._oauth_state:
            _LOGGER.warning(
                "State OAuth nella risposta non corrisponde a quello inviato "
                "(atteso %r, ricevuto %r) - procedo comunque, ma segnalalo se "
                "il login inizia a fallire qui",
                self._oauth_state,
                state_match.group(1),
            )

    async def _async_approva_consenso(self, html: str) -> str | None:
        """Preme "Consenti" sulla schermata di consenso OAuth di Salesforce.

        Ritorna il testo con cui cercare il codice di autorizzazione: il
        Location della risposta se c'è un redirect (tipicamente verso
        eneldist://redirect?code=..., che aiohttp non può seguire perché non
        è uno schema HTTP), altrimenti il corpo della risposta. Ritorna None
        se questa pagina non è una schermata di consenso riconoscibile, così
        il chiamante prosegue con il suo errore di parsing abituale.
        """
        form = self._estrai_form_consenso(html)
        if form is None:
            return None

        action_url, dati = form
        _LOGGER.info(
            "Schermata di consenso OAuth rilevata (prima autorizzazione di "
            "questo account): confermo con %r",
            dati.get("save"),
        )
        headers = {
            "User-Agent": _MOBILE_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://private.e-distribuzione.it",
            "Referer": "https://private.e-distribuzione.it/PortaleClienti/",
        }
        # allow_redirects=False: il redirect punta allo schema custom
        # dell'app (eneldist://), che aiohttp non sa seguire - il codice sta
        # nel Location, che qui possiamo leggere direttamente.
        async with self._session.post(
            action_url, data=dati, headers=headers, allow_redirects=False
        ) as resp:
            location = resp.headers.get("Location")
            body = await _leggi_testo(resp)
        return location or body

    @staticmethod
    def _estrai_form_consenso(html: str) -> tuple[str, dict[str, str]] | None:
        """Trova il form della schermata di consenso e costruisce il
        payload da rimandare: tutti i campi hidden così come sono, più il
        pulsante di approvazione.

        Ritorna (url_action, dati) oppure None se non c'è un form con un
        pulsante di approvazione riconoscibile.
        """
        for form_match in re.finditer(r"<form[^>]*>.*?</form>", html, re.IGNORECASE | re.DOTALL):
            blocco = form_match.group(0)
            approva: tuple[str, str] | None = None
            for input_match in re.finditer(r"<input[^>]*>", blocco, re.IGNORECASE):
                tag = input_match.group(0)
                if not re.search(r'type="submit"', tag, re.IGNORECASE):
                    continue
                nome = re.search(r'name="([^"]*)"', tag)
                valore = re.search(r'value="([^"]*)"', tag)
                if not nome or not valore:
                    continue
                etichetta = html_lib.unescape(valore.group(1))
                if etichetta.strip().lower() in _ETICHETTE_CONSENSO:
                    approva = (nome.group(1), etichetta)
                    break
            if approva is None:
                continue

            dati = {}
            for input_match in re.finditer(r"<input[^>]*>", blocco, re.IGNORECASE):
                tag = input_match.group(0)
                if not re.search(r'type="hidden"', tag, re.IGNORECASE):
                    continue
                nome = re.search(r'name="([^"]*)"', tag)
                if not nome:
                    continue
                valore = re.search(r'value="([^"]*)"', tag)
                # I valori nell'HTML sono escapati (&amp;, &#39;): vanno
                # riportati in chiaro, altrimenti il server riceve campi
                # diversi da quelli che ha generato (save_new_url e source
                # sono URL pieni di parametri).
                dati[nome.group(1)] = html_lib.unescape(valore.group(1)) if valore else ""
            dati[approva[0]] = approva[1]

            action = re.search(r'<form[^>]*action="([^"]*)"', blocco, re.IGNORECASE)
            action_url = html_lib.unescape(action.group(1)) if action else ""
            if not action_url:
                return None
            if action_url.startswith("/"):
                action_url = "https://private.e-distribuzione.it" + action_url
            return action_url, dati

        return None

    @staticmethod
    def _verifica_non_troppe_sessioni(testo: str, nome_file_debug: str | None = None) -> None:
        """Alza TroppeSessioni se il testo contiene uno dei marcatori noti.

        Centralizza un controllo che nel protocollo originale compare in tre
        punti diversi del flusso (risposta di loginUser, submit senza
        codice, submit con codice) con lo stesso identico significato."""
        if not _contiene(testo, _MARCATORI_TROPPE_SESSIONI):
            return
        if nome_file_debug:
            _salva_pagina_debug(testo, nome_file_debug)
        raise TroppeSessioni(testo[:500])

    # -- Scambio/refresh del token ------------------------------------------

    async def _async_exchange_code(self, code: str) -> OAuthTokens:
        """Passo 8: scambia l'authorization code per una coppia di token."""
        data = {
            "code": code,
            "code_verifier": self._code_verifier,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "authorization_code",
        }
        async with self._session.post(OAUTH_TOKEN_URL, data=data) as resp:
            payload = self._json_token(await _leggi_testo(resp), resp.status)
        return self._tokens_from_payload(payload)

    async def async_refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Ottiene un access_token fresco. Nessun Aura/OTP coinvolto - è il
        percorso che l'integrazione usa a ogni avvio/rinnovo normale."""
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": OAUTH_CLIENT_ID,
        }
        async with self._session.post(OAUTH_TOKEN_URL, data=data) as resp:
            text = await _leggi_testo(resp)
            if resp.status != 200:
                raise AuthError(f"Scambio del refresh_token fallito ({resp.status}): {text[:300]}")
            payload = self._json_token(text, resp.status)
        # Salesforce non restituisce sempre un nuovo refresh_token al
        # refresh - si mantiene il vecchio se non ne arriva uno nuovo.
        payload.setdefault("refresh_token", refresh_token)
        return self._tokens_from_payload(payload)

    @staticmethod
    def _json_token(testo: str, status: int) -> dict:
        """JSON del token endpoint; una pagina HTML al suo posto è un
        ParsingError (quindi un AuthError), non un ValueError generico."""
        try:
            return json.loads(testo)
        except json.JSONDecodeError as err:
            raise ParsingError(
                f"Risposta non-JSON dal token endpoint (status {status}): {testo[:200]!r}"
            ) from err

    @staticmethod
    def _tokens_from_payload(payload: dict) -> OAuthTokens:
        try:
            return OAuthTokens(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                instance_url=payload.get("instance_url", ""),
                id_token=payload.get("id_token"),
                raw=payload,
            )
        except KeyError as err:
            raise ParsingError(f"Campo atteso mancante nella risposta del token endpoint: {err}") from err

    # -- Scraping HTML/JSON --------------------------------------------------
    # Le parti più probabilmente da correggere contro una HAR fresca.

    @staticmethod
    def _estrai_fwuid(html: str) -> str:
        # Forma diretta: "fwuid":"<value>" letterale in JSON/JS incorporato.
        match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', html)
        if match:
            return match.group(1)

        # Forma percent-encoded: confermato su cattura reale, il blob di
        # contesto Aura è incorporato direttamente nel PATH dell'URL di uno
        # <script src="...">, non in una variabile JS:
        #   <script src="/PortaleClienti/s/sfsites/l/%7B%22mode%22...
        #     %22fwuid%22%3A%22<value>%22...%7D/app.js">
        # Deve essere così perché virgolette letterali dentro un attributo
        # src="..." romperebbero il parsing HTML.
        match = re.search(r"%22fwuid%22%3A%22([^%]+)%22", html)
        if match:
            return match.group(1)

        raise ParsingError("fwuid non trovato sulla pagina di login")

    @staticmethod
    def _estrai_loaded(html: str) -> str:
        """Estrae il valore grezzo (JSON, come stringa) del campo 'loaded'
        dallo stesso blob di bootstrap da cui si estrae fwuid - es.
        '{"APPLICATION@markup://siteforce:loginApp2":"1628_TW-..."}'.

        Necessario perché aura.context con 'loaded':{} (vuoto) viene
        rifiutato dal server con un AuraClientInputException generico,
        confermato confrontando con una richiesta loginUser reale riuscita:
        il valore reale di 'loaded' non è vuoto e va riportato identico.

        Ritorna '{}' (stringa) se non trovato, così il chiamante degrada al
        comportamento precedente invece di fallire qui - il fallimento vero
        arriverà comunque dal server sulla loginUser, con l'errore già
        diagnosticato da lì.
        """
        match = re.search(r'"loaded"\s*:\s*(\{[^}]*\})', html)
        if match:
            return match.group(1)

        match = re.search(r"%22loaded%22%3A(%7B.*?%7D)", html)
        if match:
            return unquote(match.group(1))

        return "{}"

    @staticmethod
    def _estrai_url_redirect_js(html: str) -> str:
        """La pagina di frontdoor.jsp è un ponte che rimanda al vero form
        OTP con window.location.replace('URL') (JavaScript, non un redirect
        HTTP) - un browser lo segue da solo, qui va estratto a mano e
        richiesto con una GET esplicita.

        L'URL dentro replace() è già assoluto e correttamente
        percent-encoded (è dentro una stringa JS, non un attributo HTML),
        quindi nessun problema di doppia codifica come altrove in questo
        file - va usato così com'è.
        """
        match = re.search(r"window\.location\.replace\('([^']+)'\)", html)
        if match:
            return match.group(1)

        # Fallback: alcune varianti di questa pagina Salesforce usano
        # window.location.href invece di .replace(...).
        match = re.search(r"window\.location\.href\s*=\s*'([^']+)'", html)
        if match:
            return match.group(1)

        _log_parsing_failure_context(html, "window.location.replace(...) su pagina frontdoor")
        raise ParsingError(
            "Redirect JavaScript verso il form OTP non trovato sulla pagina "
            f"di frontdoor (pagina lunga {len(html)} caratteri - vedi log "
            "per un'anteprima)"
        )

    def _analizza_pagina_otp(self, html: str) -> None:
        """Estrae dalla pagina Visualforce del form OTP tutto ciò che serve
        per sottometterlo (ViewState/CSRF + nomi dinamici dei campi),
        salvandolo in self._flow."""

        def trova(nome_campo: str) -> str:
            # Cerca l'intero tag <input ...> che contiene questo attributo
            # 'name', poi 'value' AL SUO INTERNO - indipendente dall'ordine
            # in cui i due attributi compaiono nel tag.
            tag_match = re.search(rf'<input[^>]*name="{re.escape(nome_campo)}"[^>]*>', html)
            if not tag_match:
                _log_parsing_failure_context(html, nome_campo)
                raise ParsingError(
                    f"Campo '{nome_campo}' non trovato sulla pagina OTP "
                    f"(pagina lunga {len(html)} caratteri - vedi log per un'anteprima)"
                )
            value_match = re.search(r'value="([^"]*)"', tag_match.group(0))
            if not value_match:
                raise ParsingError(
                    f"Campo '{nome_campo}' trovato ma senza attributo 'value' "
                    f"leggibile: {tag_match.group(0)!r}"
                )
            return value_match.group(1)

        self._flow.view_state = trova("com.salesforce.visualforce.ViewState")
        self._flow.view_state_version = trova("com.salesforce.visualforce.ViewStateVersion")
        self._flow.view_state_mac = trova("com.salesforce.visualforce.ViewStateMAC")
        self._flow.view_state_csrf = trova("com.salesforce.visualforce.ViewStateCSRF")

        # I prefissi dei campi sotto sono generati da Salesforce e possono
        # cambiare tra deploy: si tenta di scoprirli dinamicamente invece di
        # fidarsi solo dei default hardcoded (usati come fallback sopra).
        otp_field = re.search(r'name="([^"]*OTP_Input[^"]*)"', html)
        if otp_field:
            self._flow.otp_field_name = otp_field.group(1)

        # La pagina ha DUE campi con "Richiedi_nuovo_OTP" nel nome: una
        # checkbox visibile (element___input____...) e un campo nascosto che
        # la specchia (element___hidden____...), aggiornato da un onclick
        # JS - è quest'ultimo che va sottomesso. Un regex generico trova per
        # primo la checkbox (appare prima nell'HTML), causando l'invio del
        # campo sbagliato: confermato su una risposta reale, dove il server
        # trattava la sottomissione del codice come un'ennesima richiesta di
        # reinvio invece che una convalida.
        resend_field = re.search(
            r'name="([^"]*element___hidden____Richiedi_nuovo_OTP[^"]*)"', html
        )
        if resend_field:
            self._flow.resend_field_name = resend_field.group(1)
        resend_checkbox = re.search(
            r'name="([^"]*element___input____Richiedi_nuovo_OTP[^"]*)"', html
        )
        if resend_checkbox:
            self._flow.resend_checkbox_field_name = resend_checkbox.group(1)
        next_field = re.search(r'name="([^"]*nextAjax[^"]*)"', html)
        if next_field:
            self._flow.next_field_name = next_field.group(1)

        form_action = re.search(r'<form[^>]+action="([^"]+)"', html)
        if form_action:
            action = form_action.group(1)
            self._flow.form_action_url = (
                action
                if action.startswith("http")
                else f"https://private.e-distribuzione.it{action}"
            )
