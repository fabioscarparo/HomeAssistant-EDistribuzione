"""Authentication client for E-Distribuzione (Salesforce OAuth2 + PKCE).

The portal answers on two hostnames of the same Salesforce org: the custom
domain (private.e-distribuzione.it, behind Imperva) and the canonical domain
(edistribuzione.my.site.com). Login and refresh try the custom domain first
and fall back to the direct host on an anti-bot block.

Automated login scrapes Aura and Visualforce Login Flow pages. Once a
refresh_token is obtained, renewal uses the standard OAuth2 token endpoint.
Browser login bypasses automated scraping via OAuth user-agent flow.
"""
from __future__ import annotations

import base64
import hashlib
import html as html_lib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

import aiohttp

from .const import (
    AURA_ENDPOINT,
    HOST_LOGIN_AMMESSI,
    HOST_SALESFORCE_PREFISSI,
    HOST_SALESFORCE_SUFFISSI,
    HTTP_CONNECT_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    LOGINFLOW_URL,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_REVOKE_URL,
    OAUTH_SCOPE,
    OAUTH_TOKEN_URL,
    SF_HOST_DIRETTO,
    SF_HOST_PRINCIPALE,
    SF_HOST_TOKEN,
    SF_HOSTS,
)
from .redaction import redact_preview, redact_url

_LOGGER = logging.getLogger(__name__)

_MOBILE_USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
_HEADERS_TOKEN = {"Accept": "application/json"}

_MAX_REDIRECT_HOPS = 15

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=HTTP_TIMEOUT_SECONDS,
    connect=HTTP_CONNECT_TIMEOUT_SECONDS,
)

# Fallback RichFaces form field names if dynamic discovery misses them.
_FORM_ID_DEFAULT = "thePage:j_id2:i:f"
_CAMPO_NAV_TYPE_DEFAULT = "thePage:j_id2:i:f:pb:d:navigationType"
_CAMPO_OTP_INPUT_DEFAULT = "thePage:j_id2:i:f:pb:d:element___input____OTP_Input"
_CAMPO_RICHIEDI_NUOVO_OTP_DEFAULT = "thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"
_CAMPO_NEXT_AJAX_DEFAULT = "thePage:j_id2:i:f:pb:pbb:nextAjax"

_MARCATORI_TROPPE_SESSIONI = (
    "sessioni simultanee",
    "sessioni contemporanee",
    "numero di sessioni",
    "sessioni consentite",
)

# Refusal categories for Aura loginUser ("KO: ..."), checked in order.
_MARCATORI_ERRORE_TECNICO = ("errore tecnico", "technical error")
_MARCATORI_ACCOUNT_BLOCCATO = ("bloccat", "locked", "disattivat", "disabilitat")
_MARCATORI_PASSWORD_SCADUTA = ("scadut", "expired")
_MARCATORI_CREDENZIALI_ERRATE = (
    "non valid",
    "errat",
    "invalid",
    "incorrect",
    "credenziali",
)

# Imperva/Incapsula anti-bot block page markers.
_MARCATORI_ANTIBOT = (
    "pardon our interruption",
    "incapsula incident id",
)

# Text confirming the OTP send on the initial/resend form reply.
_MARCATORI_OTP_INVIATO = (
    "abbiamo inviato",
    "inviato un codice",
    "codice a 5 cifre",
    "codice è stato inviato",
    "nuovo codice",
)

# Markers for an invalid or expired OTP code (Italian and English).
_MARCATORI_OTP_CONTESTO = ("codice otp", "otp", "codice di verifica", "verification code")
_MARCATORI_OTP_ERRORE = ("errat", "scadut", "non valid", "invalid", "expired", "incorrect")

# Approval button labels on the Salesforce OAuth consent screen.
_ETICHETTE_CONSENSO = ("consenti", "allow", "approve", "autorizza", "accetta")

# Match the app redirect alone or inside a browser console log / JS / JSON string.
_RE_REDIRECT_APP = re.compile(re.escape(OAUTH_REDIRECT_URI) + r"[^\s'\"<>\\]*")
_TRAILING_PUNCTUATION = ".,;)]}"

# HTML attribute helpers (word-boundary safe, supports single or double quotes).
_RE_ATTR_TYPE = re.compile(r"""(?:^|\s)type=(["'])([^"']*)\1""", re.IGNORECASE)
_RE_ATTR_NAME = re.compile(r"""(?:^|\s)name=(["'])([^"']*)\1""", re.IGNORECASE)
_RE_ATTR_VALUE = re.compile(r"""(?:^|\s)value=(["'])([^"']*)\1""", re.IGNORECASE)
_RE_FORM_ACTION = re.compile(r"""<form\b[^>]*?(?:\s)action=(["'])([^"']*)\1""", re.IGNORECASE)
_RE_FORM_NAME_OR_ID = re.compile(
    r"""<form\b[^>]*?(?:\s)(?:name|id)=(["'])([^"']*)\1""", re.IGNORECASE
)


def _per_log(url) -> str:
    """URL without query and fragment, safe for logs and exceptions."""
    return redact_url(url)


def _contiene(testo: str, marcatori: tuple[str, ...]) -> bool:
    minuscolo = testo.lower()
    return any(marcatore in minuscolo for marcatore in marcatori)


def _log_parsing_failure_context(html: str, campo_cercato: str) -> None:
    """Log page title, length and a scrubbed preview without writing files to disk."""
    title_match = re.search(r"<title>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    title = title_match.group(1).strip() if title_match else "(nessun <title> trovato)"
    _LOGGER.error(
        "Campo '%s' non trovato. Titolo pagina: %r. Lunghezza: %d caratteri. Anteprima: %r",
        campo_cercato,
        redact_preview(title, 120),
        len(html),
        redact_preview(html, 200),
    )


class AuthError(Exception):
    """Generic authentication error."""


class LoginRifiutato(AuthError):
    """The portal refused the login with a message of its own."""

    def __init__(self, messaggio: str) -> None:
        super().__init__(messaggio)
        self.messaggio = messaggio


class InvalidCredentials(LoginRifiutato):
    """Wrong email or password."""


class ErroreTecnicoPortale(LoginRifiutato):
    """Generic technical error from the portal during automated login."""


class AccountBloccato(LoginRifiutato):
    """Account locked or disabled on the portal."""


class PasswordScaduta(LoginRifiutato):
    """Password expired on the portal."""


class InvalidOtp(AuthError):
    """Wrong or expired OTP code."""


class TroppeSessioni(AuthError):
    """Account has too many open sessions; no OTP is sent."""


class AccessoBloccato(AuthError):
    """Imperva anti-bot page returned instead of the expected endpoint."""


class AppBloccata(AuthError):
    """Salesforce Transaction Security rejected the OAuth client (app_blocked)."""


class TokenNonValido(AuthError):
    """Refresh token or authorization code expired or revoked (invalid_grant)."""


class InvalidBrowserRedirect(AuthError):
    """Pasted redirect URL from browser login is missing or invalid."""

    def __init__(self, motivo: str, dettaglio: str = "") -> None:
        super().__init__(f"{motivo}: {dettaglio}" if dettaglio else motivo)
        self.motivo = motivo
        self.dettaglio = dettaglio


class ParsingError(AuthError):
    """Scraped Salesforce page did not contain the expected markup."""


class HostNonAmmesso(ParsingError):
    """Login redirect, bridge page or form pointed to an unexpected or non-HTTPS host."""


_SCHEMA_REDIRECT_APP = urlsplit(OAUTH_REDIRECT_URI).scheme.lower()


def _host_ammesso(host: str) -> bool:
    """True if host belongs to E-Distribuzione or its Salesforce org."""
    if any(
        (host.endswith(voce) and len(host) > len(voce)) if voce.startswith(".") else host == voce
        for voce in HOST_LOGIN_AMMESSI
    ):
        return True
    return host.startswith(HOST_SALESFORCE_PREFISSI) and host.endswith(HOST_SALESFORCE_SUFFISSI)


def _verifica_host(url: object, passo: str) -> None:
    """Raise HostNonAmmesso on unexpected schemes, protocol-relative hosts or untrusted domains."""
    parti = urlsplit(str(url))
    schema = parti.scheme.lower()
    if schema == "":
        if not parti.netloc:
            return
        host_ref = (parti.hostname or parti.netloc).lower()
        messaggio = (
            f'Accesso E-Distribuzione interrotto al passo "{passo}": il portale '
            f"ha indicato //{host_ref}, che non usa https."
        )
        _LOGGER.error("%s", messaggio)
        raise HostNonAmmesso(messaggio)
    if schema == _SCHEMA_REDIRECT_APP:
        return
    host = (parti.hostname or "").lower()
    if schema == "https" and _host_ammesso(host):
        return
    if schema == "https":
        motivo = "non è tra quelli ammessi"
    elif schema == "http":
        motivo = "non usa https"
    else:
        motivo = "usa uno schema non ammesso"
    messaggio = (
        f'Accesso E-Distribuzione interrotto al passo "{passo}": il portale '
        f"ha indicato {schema}://{host}, che {motivo}."
    )
    _LOGGER.error("%s", messaggio)
    raise HostNonAmmesso(messaggio)


def _parametri_redirect(url: str) -> dict[str, str]:
    """Decode OAuth redirect fragment or query without turning '+' into spaces."""
    parti = urlsplit(url)
    parametri: dict[str, str] = {}
    for coppia in (parti.fragment or parti.query).split("&"):
        chiave, _, valore = coppia.partition("=")
        if chiave:
            parametri[unquote(chiave)] = unquote(valore)
    return parametri


def _errore_oauth(codice: str | None, descrizione: str | None, contesto: str) -> AuthError:
    """Map an OAuth error code to the matching exception."""
    dettaglio = f"{contesto}: {codice}" + (f" ({descrizione})" if descrizione else "")
    if codice == "app_blocked":
        return AppBloccata(dettaglio)
    if codice == "invalid_grant":
        return TokenNonValido(dettaglio)
    return AuthError(dettaglio)


def _verifica_non_bloccato(testo: str) -> None:
    """Raise AccessoBloccato if the response is an Imperva anti-bot page."""
    if _contiene(testo, _MARCATORI_ANTIBOT):
        raise AccessoBloccato("E-Distribuzione ha risposto con la pagina antibot (Imperva)")


async def _leggi_testo(resp: aiohttp.ClientResponse) -> str:
    """Read response text after ruling out the anti-bot page."""
    testo = await resp.text()
    _verifica_non_bloccato(testo)
    return testo


def _identita(url: str) -> str:
    return url


def _riscrivi_su_host(url: str, target_host: str) -> str:
    """Rewrite any known Salesforce host prefix onto target_host."""
    for host in SF_HOSTS:
        prefisso = f"https://{host}"
        if url.startswith(prefisso):
            return f"https://{target_host}" + url[len(prefisso) :]
    return url


async def _get_following_redirects(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    riscrivi=_identita,
    client_timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
    stop_on_app_redirect: bool = False,
    passo: str = "redirect",
) -> aiohttp.ClientResponse | str:
    """Follow redirects manually with encoded=True to avoid double-encoding startURL.

    If stop_on_app_redirect is True and a Location targets OAUTH_REDIRECT_URI
    without an OAuth error, returns that Location string directly.
    """
    _verifica_host(url, passo)
    resp = await session.get(
        url, params=params, headers=headers, allow_redirects=False, timeout=client_timeout
    )

    for numero_hop in range(1, _MAX_REDIRECT_HOPS + 1):
        location = resp.headers.get("Location")
        _LOGGER.debug(
            "GET %s: HTTP %s%s",
            _per_log(getattr(resp, "url", url)),
            getattr(resp, "status", "?"),
            f" -> {_per_log(location)}" if location else "",
        )
        if location is None:
            return resp

        resp.close()
        if location.startswith(OAUTH_REDIRECT_URI):
            query = parse_qs(urlsplit(location).query)
            if "error" in query:
                raise _errore_oauth(
                    query["error"][0],
                    query.get("error_description", [None])[0],
                    "Autorizzazione OAuth rifiutata",
                )
            if stop_on_app_redirect:
                return location
        location_url = aiohttp.client.URL(location, encoded=True)
        next_url = location_url if location_url.is_absolute() else resp.url.join(location_url)
        next_url = aiohttp.client.URL(riscrivi(str(next_url)), encoded=True)
        _verifica_host(next_url, f"{passo}, redirect {numero_hop}")
        resp = await session.get(
            next_url, headers=headers, allow_redirects=False, timeout=client_timeout
        )

    resp.close()
    raise AuthError(
        f"Troppi redirect (>{_MAX_REDIRECT_HOPS}) seguendo {_per_log(url)}: possibile "
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
    """State carried from the OTP page to its submission."""

    fwuid: str | None = None
    aura_token: str | None = None
    view_state: str | None = None
    view_state_version: str | None = None
    view_state_mac: str | None = None
    view_state_csrf: str | None = None
    form_action_url: str | None = None
    form_id: str | None = None
    nav_field_name: str | None = None
    otp_field_name: str | None = None
    resend_field_name: str | None = None
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
    """Drives the login -> OTP -> OAuth code -> token exchange sequence."""

    def __init__(
        self,
        session: aiohttp.ClientSession | None,
        *,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
        instance_url: str | None = None,
    ) -> None:
        self._session = session
        self._timeout = timeout
        self._instance_url = (instance_url or "").strip()
        self._code_verifier: str | None = None
        self._oauth_state: str | None = None
        self._flow = _LoginFlowState()
        # None until an actual login or refresh completes on a host.
        self._host: str | None = None
        self.otp_invio_confermato: bool | None = None

    def _host_token_diretto(self) -> str:
        """Return validated Salesforce org host for direct OAuth token calls."""
        if self._instance_url:
            parts = urlsplit(self._instance_url)
            host = (parts.hostname or "").lower()
            if parts.scheme == "https" and (
                host == SF_HOST_TOKEN
                or (
                    host.startswith(HOST_SALESFORCE_PREFISSI)
                    and host.endswith(".salesforce.com")
                )
            ):
                return host
        return SF_HOST_TOKEN

    @property
    def host_attivo(self) -> str | None:
        """Host the last login or refresh went through, or None if not used yet."""
        return self._host

    def prepare_browser_token_authorization_url(self) -> str:
        """Build the OAuth user-agent authorization URL for browser login."""
        self._code_verifier = None
        self._oauth_state = _b64url(secrets.token_bytes(16))
        params = {
            "nonce": _b64url(secrets.token_bytes(16)),
            "display": "touch",
            "response_type": "token",
            "scope": OAUTH_SCOPE,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "state": self._oauth_state,
        }
        _LOGGER.debug("Login nel browser: link di autorizzazione preparato")
        return f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def tokens_from_browser_redirect(self, testo: str) -> OAuthTokens:
        """Extract tokens from the redirect URL pasted after browser login."""
        if self._oauth_state is None:
            raise InvalidBrowserRedirect("altro_tentativo", "nessun link generato")

        corrispondenza = _RE_REDIRECT_APP.search(testo)
        if corrispondenza is None:
            _LOGGER.warning(
                "Login nel browser: nessun indirizzo %s nel testo incollato (%d caratteri)",
                OAUTH_REDIRECT_URI,
                len(testo),
            )
            raise InvalidBrowserRedirect("mancante")

        url_estratto = html_lib.unescape(corrispondenza.group(0)).rstrip(_TRAILING_PUNCTUATION)
        parametri = _parametri_redirect(url_estratto)
        _LOGGER.debug("Login nel browser: parametri ricevuti %s", sorted(parametri))

        if "error" in parametri:
            descrizione = parametri.get("error_description", "").replace("+", " ")
            _LOGGER.warning(
                "Login nel browser rifiutato da E-Distribuzione: %s (%s)",
                parametri["error"],
                descrizione,
            )
            if parametri["error"] == "app_blocked":
                raise AppBloccata(f"Login nel browser: app_blocked ({descrizione})")
            raise InvalidBrowserRedirect("negato", descrizione or parametri["error"])

        if parametri.get("state") != self._oauth_state:
            _LOGGER.warning(
                "Login nel browser: l'indirizzo incollato appartiene a un altro link"
            )
            raise InvalidBrowserRedirect("altro_tentativo")

        mancanti = [nome for nome in ("access_token", "refresh_token") if not parametri.get(nome)]
        if mancanti:
            _LOGGER.warning("Login nel browser: nel redirect mancano %s", ", ".join(mancanti))
            raise InvalidBrowserRedirect("token_mancanti", ", ".join(mancanti))

        _LOGGER.info("Login nel browser riuscito: token ricevuti")
        return self._tokens_from_payload(parametri)

    @property
    def _login_host(self) -> str:
        return self._host or SF_HOST_PRINCIPALE

    @property
    def _origin(self) -> str:
        return f"https://{self._login_host}"

    def _su_host(self, url: str) -> str:
        """Rewrite a Salesforce URL onto the active login host."""
        return _riscrivi_su_host(url, self._login_host)

    # -- Steps 1-4: login + OTP -------------------------------------------

    async def async_begin_login(self, email: str, password: str) -> None:
        """Submit credentials and reach the OTP page, with fallback to direct host."""
        self._host = SF_HOST_PRINCIPALE
        _LOGGER.debug("Login con password: inizio su %s", self._host)
        try:
            await self._async_begin_login_su_host(email, password)
        except AccessoBloccato:
            _LOGGER.warning(
                "Login bloccato su %s, riprovo via %s", SF_HOST_PRINCIPALE, SF_HOST_DIRETTO
            )
            self._host = SF_HOST_DIRETTO
            self._flow = _LoginFlowState()
            self.otp_invio_confermato = None
            await self._async_begin_login_su_host(email, password)
        _LOGGER.info(
            "Login con password: credenziali accettate su %s, invio OTP %s",
            self._host,
            "confermato" if self.otp_invio_confermato else "non confermato",
        )

    async def _async_begin_login_su_host(self, email: str, password: str) -> None:
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
        """Step 1: GET /oauth2/authorize with PKCE up to the login page."""
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
            self._session,
            self._su_host(OAUTH_AUTHORIZE_URL),
            params=params,
            headers=headers,
            riscrivi=self._su_host,
            client_timeout=self._timeout,
            passo="authorize OAuth verso la pagina di login",
        )
        try:
            login_page_html = await _leggi_testo(resp)
            _LOGGER.debug(
                "Pagina di login: %s, HTTP %s, %d caratteri",
                _per_log(resp.url),
                resp.status,
                len(login_page_html),
            )
            start_url_value = resp.url.query.get(
                "startURL", "/PortaleClienti/setup/secur/RemoteAccessAuthorizationPage.apexp"
            )
            aura_page_uri = resp.url.path_qs
            referer = str(resp.url)
        finally:
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
        """Step 2: submit Aura loginUser action and return the frontdoor.jsp URL."""
        headers = {"User-Agent": _MOBILE_USER_AGENT}

        self._flow.fwuid = self._estrai_fwuid(login_page_html)
        loaded = self._estrai_loaded(login_page_html)
        self._flow.aura_token = "null"

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
        login_headers = {
            **headers,
            "X-SFDC-Page-Scope-Id": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": self._origin,
            "Referer": referer,
        }

        async with self._session.post(
            f"{self._su_host(AURA_ENDPOINT)}?r=2&other.PED_Login.loginUser=1",
            data=data,
            headers=login_headers,
            timeout=self._timeout,
        ) as resp:
            raw_text = await _leggi_testo(resp)
            try:
                payload = json.loads(raw_text)
            except json.JSONDecodeError as err:
                _LOGGER.error(
                    "Risposta non-JSON da loginUser (status %s, %d caratteri). Anteprima: %r",
                    resp.status,
                    len(raw_text),
                    redact_preview(raw_text, 200),
                )
                raise ParsingError(
                    f"Risposta non-JSON da loginUser (status {resp.status}, {len(raw_text)} caratteri)"
                ) from err

        try:
            azione = payload["actions"][0]
            return_value = azione["returnValue"]
        except (KeyError, IndexError, TypeError) as err:
            raise ParsingError("Forma inattesa della risposta di loginUser") from err

        _LOGGER.debug(
            "loginUser su %s: state %r, esito %s",
            self._login_host,
            azione.get("state"),
            return_value[:3] if isinstance(return_value, str) else type(return_value).__name__,
        )

        if not isinstance(return_value, str):
            errori = azione.get("error") or [{}]
            dettaglio = errori[0].get("message") if isinstance(errori[0], dict) else None
            _LOGGER.error(
                "loginUser su %s senza esito (state %r): %r",
                self._login_host,
                azione.get("state"),
                dettaglio,
            )
            raise ParsingError(
                f"loginUser senza esito (state {azione.get('state')!r}): {dettaglio!r}"
            )

        if not return_value.startswith("OK:"):
            self._verifica_non_troppe_sessioni(return_value)
            _LOGGER.warning(
                "E-Distribuzione ha rifiutato il login su %s con il messaggio %r",
                self._login_host,
                redact_preview(return_value, 200),
            )
            raise self._rifiuto_login(return_value)

        return self._su_host(return_value[len("OK:") :])

    @staticmethod
    def _rifiuto_login(return_value: str) -> LoginRifiutato:
        """Classify a loginUser 'KO: ...' refusal by meaning."""
        messaggio = return_value.removeprefix("KO:").strip() or return_value
        for marcatori, eccezione in (
            (_MARCATORI_ERRORE_TECNICO, ErroreTecnicoPortale),
            (_MARCATORI_ACCOUNT_BLOCCATO, AccountBloccato),
            (_MARCATORI_PASSWORD_SCADUTA, PasswordScaduta),
            (_MARCATORI_CREDENZIALI_ERRATE, InvalidCredentials),
        ):
            if _contiene(messaggio, marcatori):
                return eccezione(messaggio)
        return LoginRifiutato(messaggio)

    async def _segui_a_pagina_otp(self, frontdoor_url: str) -> str:
        """Step 3: follow frontdoor.jsp and its JS bridge redirect to the OTP form."""
        headers = {"User-Agent": _MOBILE_USER_AGENT}

        resp = await _get_following_redirects(
            self._session,
            frontdoor_url,
            headers=headers,
            riscrivi=self._su_host,
            client_timeout=self._timeout,
            passo="frontdoor dopo le credenziali",
        )
        try:
            bridge_html = await _leggi_testo(resp)
        finally:
            resp.close()

        otp_form_url = self._su_host(self._estrai_url_redirect_js(bridge_html))
        resp = await _get_following_redirects(
            self._session,
            otp_form_url,
            headers=headers,
            riscrivi=self._su_host,
            client_timeout=self._timeout,
            passo="pagina del codice OTP",
        )
        try:
            return await _leggi_testo(resp)
        finally:
            resp.close()

    async def _async_avvia_invio_otp(self) -> None:
        """Step 4: submit the empty OTP form to trigger sending the code."""
        await self._async_post_form_otp(self._form_invio_iniziale())

    async def async_resend_otp(self) -> bool:
        """Request a new OTP within the already-started login session."""
        if self._flow.view_state is None:
            raise AuthError("async_begin_login() deve riuscire prima di async_resend_otp()")
        await self._async_post_form_otp(self._form_richiedi_nuovo())
        return bool(self.otp_invio_confermato)

    def _campi_comuni_form_otp(self) -> dict[str, str]:
        """RichFaces fields shared by initial send, resend and OTP validation."""
        form_id = self._flow.form_id or _FORM_ID_DEFAULT
        nav_field = self._flow.nav_field_name or _CAMPO_NAV_TYPE_DEFAULT
        next_field = self._flow.next_field_name or _CAMPO_NEXT_AJAX_DEFAULT
        return {
            "AJAXREQUEST": "_viewRoot",
            form_id: form_id,
            nav_field: "",
            "com.salesforce.visualforce.ViewState": self._flow.view_state,
            "com.salesforce.visualforce.ViewStateVersion": self._flow.view_state_version,
            "com.salesforce.visualforce.ViewStateMAC": self._flow.view_state_mac,
            "com.salesforce.visualforce.ViewStateCSRF": self._flow.view_state_csrf,
            next_field: next_field,
        }

    def _form_invio_iniziale(self) -> dict[str, str]:
        return self._campi_comuni_form_otp()

    def _form_richiedi_nuovo(self) -> dict[str, str]:
        dati = self._campi_comuni_form_otp()
        dati[self._flow.resend_field_name or _CAMPO_RICHIEDI_NUOVO_OTP_DEFAULT] = "true"
        if self._flow.resend_checkbox_field_name:
            dati[self._flow.resend_checkbox_field_name] = "true"
        return dati

    def _form_convalida(self, otp_code: str) -> dict[str, str]:
        dati = self._campi_comuni_form_otp()
        dati[self._flow.otp_field_name or _CAMPO_OTP_INPUT_DEFAULT] = otp_code
        dati[self._flow.resend_field_name or _CAMPO_RICHIEDI_NUOVO_OTP_DEFAULT] = "false"
        return dati

    async def _async_post_form_otp(self, data: dict[str, str]) -> None:
        """POST the OTP form for initial send or resend and rotate ViewState."""
        headers = {"User-Agent": _MOBILE_USER_AGENT, "Faces-Request": "partial/ajax"}
        url = self._su_host(self._flow.form_action_url or LOGINFLOW_URL)
        async with self._session.post(
            url, data=data, headers=headers, timeout=self._timeout
        ) as resp:
            body = await _leggi_testo(resp)
        _LOGGER.debug(
            "Form OTP (invio codice) su %s: HTTP %s, %d caratteri",
            self._login_host,
            getattr(resp, "status", "?"),
            len(body),
        )

        self._verifica_non_troppe_sessioni(body)

        self.otp_invio_confermato = _contiene(body, _MARCATORI_OTP_INVIATO)
        if not self.otp_invio_confermato:
            _LOGGER.warning(
                "E-Distribuzione non ha confermato l'invio del codice OTP "
                "(risposta lunga %d caratteri)",
                len(body),
            )

        self._analizza_pagina_otp(body)

    # -- Steps 5-8: OTP validation + token exchange -----------------------

    async def async_submit_otp(self, otp_code: str) -> OAuthTokens:
        """Submit the OTP and complete the OAuth2 code exchange."""
        if self._flow.view_state is None:
            raise AuthError("async_begin_login() deve riuscire prima di async_submit_otp()")

        next_url = await self._sottometti_codice_otp(otp_code)
        _LOGGER.debug("Codice OTP accettato su %s", self._login_host)
        consent_html = await self._scarica_pagina_dopo_otp(next_url)
        auth_code = await self._estrai_codice_autorizzazione(consent_html)
        tokens = await self._async_exchange_code(auth_code)
        _LOGGER.info("Login con password e OTP riuscito su %s", self._login_host)
        return tokens

    async def _sottometti_codice_otp(self, otp_code: str) -> str:
        """Step 5: submit the OTP code and extract the redirect meta Location."""
        headers = {"User-Agent": _MOBILE_USER_AGENT, "Faces-Request": "partial/ajax"}
        data = self._form_convalida(otp_code)

        url = self._su_host(self._flow.form_action_url or LOGINFLOW_URL)
        async with self._session.post(
            url, data=data, headers=headers, timeout=self._timeout
        ) as resp:
            body = await _leggi_testo(resp)

        self._verifica_non_troppe_sessioni(body)

        if _contiene(body, _MARCATORI_OTP_CONTESTO) and _contiene(body, _MARCATORI_OTP_ERRORE):
            _LOGGER.warning("Codice OTP rifiutato da E-Distribuzione (errato o scaduto)")
            raise InvalidOtp("Codice OTP errato o scaduto")

        loc_match = re.search(
            r"""name=(["'])Location\1\s+content=(["'])([^"']+)\2""", body, re.IGNORECASE
        )
        if not loc_match:
            _log_parsing_failure_context(body, "meta Location dopo l'invio dell'OTP")
            raise ParsingError(
                "Redirect (meta Location) non trovato nella risposta dopo "
                f"l'invio dell'OTP (risposta lunga {len(body)} caratteri)"
            )
        next_url = unquote(html_lib.unescape(loc_match.group(3)))
        next_url = self._su_host(urljoin(f"{self._origin}/", next_url))
        _verifica_host(next_url, "redirect dopo il codice OTP")
        return next_url

    async def _scarica_pagina_dopo_otp(self, next_url: str) -> str:
        """Step 6: follow redirects on the active host until the consent/code page."""
        headers = {"User-Agent": _MOBILE_USER_AGENT}
        result = await _get_following_redirects(
            self._session,
            next_url,
            headers=headers,
            riscrivi=self._su_host,
            client_timeout=self._timeout,
            stop_on_app_redirect=True,
            passo="pagina di autorizzazione dopo l'OTP",
        )
        if isinstance(result, str):
            return result
        try:
            return await _leggi_testo(result)
        finally:
            result.close()

    async def _estrai_codice_autorizzazione(self, consent_html: str) -> str:
        """Step 7: extract OAuth authorization code, approving consent on first login."""
        code_match = re.search(r"[?&]code=([^&'\"\s<>;]+)", consent_html)
        state_match = re.search(r"[?&]state=([^&'\"\s<>;]+)", consent_html)

        if not code_match:
            risposta_consenso = await self._async_approva_consenso(consent_html)
            if risposta_consenso is not None:
                consent_html = risposta_consenso
                code_match = re.search(r"[?&]code=([^&'\"\s<>;]+)", consent_html)
                state_match = re.search(r"[?&]state=([^&'\"\s<>;]+)", consent_html)

        if not code_match:
            _log_parsing_failure_context(
                consent_html, "authorization code sulla pagina di consenso"
            )
            raise ParsingError(
                "Authorization code non trovato nella risposta della pagina "
                f"di consenso (pagina lunga {len(consent_html)} caratteri)"
            )

        self._valida_state(state_match)
        return unquote(code_match.group(1))

    def _valida_state(self, state_match: re.Match[str] | None) -> None:
        """Enforce OAuth CSRF state match when a state was issued."""
        if self._oauth_state is None:
            return
        if state_match is None or unquote(state_match.group(1)) != self._oauth_state:
            _LOGGER.warning("State OAuth nella risposta mancante o non corrispondente")
            raise AuthError("State OAuth non valido nella risposta di autorizzazione")

    async def _async_approva_consenso(self, html: str) -> str | None:
        """Submit the 'Consenti' button on Salesforce's first-time OAuth consent page."""
        form = self._estrai_form_consenso(html)
        if form is None:
            return None

        action_url, dati = form
        action_url = self._su_host(action_url)
        _verifica_host(action_url, "form di consenso OAuth")
        _LOGGER.info(
            "Schermata di consenso OAuth rilevata: confermo con %r",
            dati.get("save"),
        )
        headers = {
            "User-Agent": _MOBILE_USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._origin,
            "Referer": f"{self._origin}/PortaleClienti/",
        }
        async with self._session.post(
            action_url,
            data=dati,
            headers=headers,
            allow_redirects=False,
            timeout=self._timeout,
        ) as resp:
            location = resp.headers.get("Location")
            body = await _leggi_testo(resp)
        return location or body

    @staticmethod
    def _estrai_form_consenso(html: str) -> tuple[str, dict[str, str]] | None:
        """Find the consent form and build its hidden fields plus approval button."""
        for form_match in re.finditer(r"<form\b[^>]*>.*?</form>", html, re.IGNORECASE | re.DOTALL):
            blocco = form_match.group(0)
            approva: tuple[str, str] | None = None
            for input_match in re.finditer(r"<input\b[^>]*>", blocco, re.IGNORECASE):
                tag = input_match.group(0)
                tipo = _RE_ATTR_TYPE.search(tag)
                if not tipo or tipo.group(2).lower() != "submit":
                    continue
                nome = _RE_ATTR_NAME.search(tag)
                valore = _RE_ATTR_VALUE.search(tag)
                if not nome or not valore:
                    continue
                etichetta = html_lib.unescape(valore.group(2))
                if etichetta.strip().lower() in _ETICHETTE_CONSENSO:
                    approva = (nome.group(2), etichetta)
                    break
            if approva is None:
                continue

            dati = {}
            for input_match in re.finditer(r"<input\b[^>]*>", blocco, re.IGNORECASE):
                tag = input_match.group(0)
                tipo = _RE_ATTR_TYPE.search(tag)
                if not tipo or tipo.group(2).lower() != "hidden":
                    continue
                nome = _RE_ATTR_NAME.search(tag)
                if not nome:
                    continue
                valore = _RE_ATTR_VALUE.search(tag)
                dati[nome.group(2)] = html_lib.unescape(valore.group(2)) if valore else ""
            dati[approva[0]] = approva[1]

            action = _RE_FORM_ACTION.search(blocco)
            action_url = html_lib.unescape(action.group(2)) if action else ""
            if not action_url:
                return None
            action_url = urljoin(f"https://{SF_HOST_PRINCIPALE}/", action_url)
            return action_url, dati

        return None

    @staticmethod
    def _verifica_non_troppe_sessioni(testo: str) -> None:
        """Raise TroppeSessioni if the response reports the concurrent session limit."""
        if _contiene(testo, _MARCATORI_TROPPE_SESSIONI):
            raise TroppeSessioni("Troppi accessi contemporanei sull'account")

    # -- Token exchange/refresh ---------------------------------------------

    async def _async_exchange_code(self, code: str) -> OAuthTokens:
        """Step 8: exchange authorization code for tokens."""
        data = {
            "code": code,
            "code_verifier": self._code_verifier,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "client_id": OAUTH_CLIENT_ID,
            "grant_type": "authorization_code",
        }
        async with self._session.post(
            self._su_host(OAUTH_TOKEN_URL),
            data=data,
            headers=_HEADERS_TOKEN,
            timeout=self._timeout,
        ) as resp:
            payload = self._json_token(
                await _leggi_testo(resp), resp.status, "Scambio del codice OAuth fallito"
            )
        return self._tokens_from_payload(payload)

    def _candidati_refresh(self) -> list[tuple[str, str]]:
        """Ordered (host, token_url) candidates for token refresh.

        Try the direct Salesforce org host first (no Imperva anti-bot, no
        my.site.com app_blocked), then fall back to the portal's custom domain.
        """
        org_host = self._host_token_diretto()
        return [
            (org_host, f"https://{org_host}/services/oauth2/token"),
            (SF_HOST_PRINCIPALE, OAUTH_TOKEN_URL),
        ]

    async def async_refresh_access_token(
        self, refresh_token: str, client_id: str = OAUTH_CLIENT_ID
    ) -> OAuthTokens:
        """Refresh the access token, trying the direct Salesforce org host first."""
        primo_errore: AccessoBloccato | AppBloccata | aiohttp.ClientError | TimeoutError | None = (
            None
        )
        candidati = self._candidati_refresh()
        for indice, (host, token_url) in enumerate(candidati):
            _LOGGER.debug("Rinnovo dell'accesso: tentativo su %s", host)
            try:
                tokens = await self._async_refresh_su_url(refresh_token, token_url, client_id)
            except (AccessoBloccato, AppBloccata, aiohttp.ClientError, TimeoutError) as err:
                if primo_errore is None:
                    primo_errore = err
                if indice + 1 < len(candidati):
                    prossimo_host = candidati[indice + 1][0]
                    _LOGGER.warning(
                        "Refresh fallito su %s (%s), riprovo via %s",
                        host,
                        type(err).__name__,
                        prossimo_host,
                    )
                    continue
                raise primo_errore from None
            except AuthError as err:
                # If the fallback host fails generically after the primary host already
                # failed, preserve the primary reason (except explicit invalid_grant).
                if primo_errore is not None and not isinstance(err, TokenNonValido):
                    raise primo_errore from None
                raise
            self._host = host
            _LOGGER.debug(
                "Accesso rinnovato su %s (nuovo refresh_token: %s)",
                host,
                "sì" if tokens.refresh_token != refresh_token else "no",
            )
            return tokens
        assert primo_errore is not None
        raise primo_errore

    async def _async_refresh_su_url(
        self, refresh_token: str, token_url: str, client_id: str = OAUTH_CLIENT_ID
    ) -> OAuthTokens:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        async with self._session.post(
            token_url, data=data, headers=_HEADERS_TOKEN, timeout=self._timeout
        ) as resp:
            payload = self._json_token(
                await _leggi_testo(resp), resp.status, "Scambio del refresh_token fallito"
            )
        # Keep the existing refresh_token if Salesforce omits it or sends null/empty.
        if not payload.get("refresh_token"):
            payload["refresh_token"] = refresh_token
        return self._tokens_from_payload(payload)

    async def async_revoke_token(self, token: str) -> bool:
        """Best-effort OAuth token revocation to free the portal session slot."""
        if not token or self._session is None:
            return False
        org_host = self._host_token_diretto()
        urls = (f"https://{org_host}/services/oauth2/revoke", OAUTH_REVOKE_URL)
        for revoke_url in urls:
            try:
                async with self._session.post(
                    revoke_url, data={"token": token}, timeout=self._timeout
                ) as resp:
                    if resp.status == 200:
                        _LOGGER.debug("Token OAuth revocato su %s", _per_log(revoke_url))
                        return True
                    _LOGGER.debug(
                        "Revoca OAuth su %s ha risposto HTTP %s",
                        _per_log(revoke_url),
                        resp.status,
                    )
            except Exception as err:  # noqa: BLE001 - revocation is best-effort
                _LOGGER.debug(
                    "Revoca OAuth su %s non riuscita: %s",
                    _per_log(revoke_url),
                    type(err).__name__,
                )
        return False

    @staticmethod
    def _json_token(testo: str, status: int, contesto: str) -> dict:
        """Parse token endpoint JSON and map non-200 OAuth errors."""
        try:
            payload = json.loads(testo)
        except json.JSONDecodeError as err:
            _LOGGER.error(
                "%s: risposta non-JSON (status %s, %d caratteri). Anteprima: %r",
                contesto,
                status,
                len(testo),
                redact_preview(testo, 200),
            )
            raise ParsingError(
                f"Risposta non-JSON dal token endpoint (status {status}, {len(testo)} caratteri)"
            ) from err
        if status != 200:
            errore = payload if isinstance(payload, dict) else {}
            _LOGGER.warning(
                "%s: HTTP %s, errore OAuth %r (%s)",
                contesto,
                status,
                errore.get("error"),
                errore.get("error_description"),
            )
            raise _errore_oauth(
                errore.get("error"), errore.get("error_description"), f"{contesto} ({status})"
            )
        if not isinstance(payload, dict):
            raise ParsingError("Risposta JSON inattesa dal token endpoint")
        return payload

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
            raise ParsingError(
                f"Campo atteso mancante nella risposta del token endpoint: {err}"
            ) from err

    # -- HTML/JSON scraping --------------------------------------------------

    @staticmethod
    def _estrai_fwuid(html: str) -> str:
        match = re.search(r'"fwuid"\s*:\s*"([^"]+)"', html)
        if match:
            return match.group(1)

        match = re.search(r"%22fwuid%22%3A%22([^%]+)%22", html)
        if match:
            return match.group(1)

        raise ParsingError("fwuid non trovato sulla pagina di login")

    @staticmethod
    def _estrai_loaded(html: str) -> str:
        match = re.search(r'"loaded"\s*:\s*(\{[^}]*\})', html)
        if match:
            return match.group(1)

        match = re.search(r"%22loaded%22%3A(%7B.*?%7D)", html)
        if match:
            return unquote(match.group(1))

        return "{}"

    @staticmethod
    def _estrai_url_redirect_js(html: str) -> str:
        match = re.search(r"window\.location\.replace\('([^']+)'\)", html)
        if match:
            return match.group(1)

        match = re.search(r"window\.location\.href\s*=\s*'([^']+)'", html)
        if match:
            return match.group(1)

        _log_parsing_failure_context(html, "window.location.replace(...) su pagina frontdoor")
        raise ParsingError(
            "Redirect JavaScript verso il form OTP non trovato sulla pagina "
            f"di frontdoor (pagina lunga {len(html)} caratteri)"
        )

    def _analizza_pagina_otp(self, html: str) -> None:
        """Extract ViewState/CSRF tokens and dynamic RichFaces field names."""

        def trova(nome_campo: str) -> str:
            pattern = rf"""<input\b[^>]*?(?:\s)name=(["']){re.escape(nome_campo)}\1[^>]*>"""
            tag_match = re.search(pattern, html, re.IGNORECASE)
            if not tag_match:
                _log_parsing_failure_context(html, nome_campo)
                raise ParsingError(
                    f"Campo '{nome_campo}' non trovato sulla pagina OTP "
                    f"(pagina lunga {len(html)} caratteri)"
                )
            value_match = _RE_ATTR_VALUE.search(tag_match.group(0))
            if not value_match:
                raise ParsingError(
                    f"Campo '{nome_campo}' trovato ma senza attributo 'value' leggibile"
                )
            return value_match.group(2)

        self._flow.view_state = trova("com.salesforce.visualforce.ViewState")
        self._flow.view_state_version = trova("com.salesforce.visualforce.ViewStateVersion")
        self._flow.view_state_mac = trova("com.salesforce.visualforce.ViewStateMAC")
        self._flow.view_state_csrf = trova("com.salesforce.visualforce.ViewStateCSRF")

        form_name = _RE_FORM_NAME_OR_ID.search(html)
        if form_name and ":f" in form_name.group(2):
            self._flow.form_id = form_name.group(2)

        nav_field = re.search(r"""(?:\s)name=(["'])([^"']*navigationType[^"']*)\1""", html)
        if nav_field:
            self._flow.nav_field_name = nav_field.group(2)

        otp_field = re.search(r"""(?:\s)name=(["'])([^"']*OTP_Input[^"']*)\1""", html)
        if otp_field:
            self._flow.otp_field_name = otp_field.group(2)

        resend_field = re.search(
            r"""(?:\s)name=(["'])([^"']*element___hidden____Richiedi_nuovo_OTP[^"']*)\1""",
            html,
        )
        if resend_field:
            self._flow.resend_field_name = resend_field.group(2)

        resend_checkbox = re.search(
            r"""(?:\s)name=(["'])([^"']*element___input____Richiedi_nuovo_OTP[^"']*)\1""",
            html,
        )
        if resend_checkbox:
            self._flow.resend_checkbox_field_name = resend_checkbox.group(2)

        next_field = re.search(r"""(?:\s)name=(["'])([^"']*nextAjax[^"']*)\1""", html)
        if next_field:
            self._flow.next_field_name = next_field.group(2)

        form_action = _RE_FORM_ACTION.search(html)
        if form_action:
            action = html_lib.unescape(form_action.group(2))
            self._flow.form_action_url = self._su_host(urljoin(f"{self._origin}/", action))
            _verifica_host(self._flow.form_action_url, "form del codice OTP")
