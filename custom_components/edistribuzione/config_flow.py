"""edistribuzione config flow.

Setup flow: user (email+password) -> otp -> pod (multi-select among the
account's PODs). Reauth: the same user/otp steps (told apart by an internal
branch on self._reauth_entry), without going back to choosing the PODs - only
the existing entry's refresh_token is updated.

No distributor or municipality selection: a single distributor, a single flow.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)

from .api import ApiClient
from .auth import (
    AccessoBloccato,
    AccountBloccato,
    AppBloccata,
    AuthClient,
    ErroreTecnicoPortale,
    InvalidBrowserRedirect,
    InvalidCredentials,
    InvalidOtp,
    LoginRifiutato,
    ParsingError,
    PasswordScaduta,
    TokenNonValido,
    TroppeSessioni,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_INSTANCE_URL,
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    ORA_MINIMA_RICHIESTA,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)
from .testi import testo

_LOGGER = logging.getLogger(__name__)

# The OTP code is Optional (not Required) because the same form also serves to
# request a new code without having one to enter: whoever received nothing ticks
# the box and submits the empty form.
STEP_OTP_SCHEMA = vol.Schema({
    vol.Optional("otp", default=""): str,
    vol.Optional("richiedi_nuovo_codice", default=False): bool,
})


# Error of the user step for each kind of login refusal. Each one also shows
# the portal's own message ({messaggio}): an unknown refusal still says what
# E-Distribuzione answered (see auth.LoginRifiutato).
_ERRORI_RIFIUTO_LOGIN: dict[type[LoginRifiutato], str] = {
    InvalidCredentials: "invalid_auth",
    ErroreTecnicoPortale: "errore_tecnico_portale",
    AccountBloccato: "account_bloccato",
    PasswordScaduta: "password_scaduta",
}


# Error of the browser_login step for each reason the pasted text is rejected
# (see auth.InvalidBrowserRedirect).
_ERRORI_LOGIN_BROWSER = {
    "mancante": "browser_url_mancante",
    "negato": "browser_negato",
    "altro_tentativo": "browser_altro_link",
    "token_mancanti": "browser_token_mancanti",
}


def _descrivi_eccezione(err: Exception) -> str:
    """Short technical detail for the "unexpected error" messages."""
    testo = str(err).strip()
    return f"{type(err).__name__}: {testo}"[:200] if testo else type(err).__name__


def _etichetta_pod(pod_info: dict) -> str:
    """'IT001E12345678 - Via Roma 1, Milano (MI)' instead of just the POD code
    in the selector, so you can tell at a glance which property it is without
    looking elsewhere."""
    indirizzo = (
        f"{pod_info.get('PointOfMeasureStreetPrefix', '')} "
        f"{pod_info.get('PointOfMeasureStreet', '')} "
        f"{pod_info.get('PointOfMeasureStreetNumber', '')}, "
        f"{pod_info.get('PointOfMeasureMunicipality', '')} "
        f"({pod_info.get('PointOfMeasureProvince', '')})"
    ).strip()
    return f"{pod_info['IdPod']} - {indirizzo}"


class EdistribuzioneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the edistribuzione integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._session = None
        self._auth: AuthClient | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._instance_url = ""
        # The browser login has a client of its own: the password login
        # replaces self._auth on every attempt, and with it the OAuth state
        # the pasted redirect is checked against.
        self._auth_browser: AuthClient | None = None
        self._browser_auth_url: str | None = None
        self._pods_disponibili: list[dict] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    def _testo(self, chiave: str, **segnaposto: str) -> str:
        """Text for the description_placeholders, which HA does not translate: see testi.py."""
        return testo(self.hass.config.language, chiave, **segnaposto)

    # ------------------------------------------------------------------
    # Login email/password -> OTP
    # ------------------------------------------------------------------

    async def _reinvia_otp(self) -> tuple[str | None, str | None]:
        """Request a new OTP within the current login session.

        It is the only way to get a code valid for Home Assistant when the first
        one does not arrive: an OTP generated on the website or in the app
        belongs to another login session and cannot be validated here. Returns
        (error_key, notice) to pass to the form.
        """
        try:
            confermato = await self._auth.async_resend_otp()
        except TroppeSessioni:
            _LOGGER.warning("Reinvio OTP rifiutato: troppe sessioni aperte sull'account")
            return "troppe_sessioni", None
        except AccessoBloccato:
            _LOGGER.warning("Reinvio OTP: E-Distribuzione ha risposto con la verifica antibot")
            return "accesso_bloccato", None
        except Exception:  # noqa: BLE001 - see comment in async_step_password_login
            _LOGGER.exception("Reinvio del codice OTP fallito")
            return "cannot_connect", None
        return None, self._testo("otp_reinviato" if confermato else "otp_invio_non_confermato")

    def _form_otp(self, errors: dict[str, str], avviso: str):
        return self.async_show_form(
            step_id="otp",
            data_schema=STEP_OTP_SCHEMA,
            errors=errors,
            description_placeholders={"avviso": avviso},
        )

    async def _otp_senza_codice(self, user_input: dict[str, Any], avviso: str):
        """Handle OTP form submits that carry no code to validate: a request for
        a new code, or an empty field.

        Returns the form to show, or None if there is a code and we can proceed
        with async_submit_otp.
        """
        if user_input.get("richiedi_nuovo_codice"):
            errore, avviso_reinvio = await self._reinvia_otp()
            return self._form_otp({"base": errore} if errore else {}, avviso_reinvio or avviso)
        if not user_input.get("otp"):
            return self._form_otp({"base": "otp_mancante"}, avviso)
        return None

    def _avviso_iniziale(self) -> str:
        """Notice to show the first time the OTP form is reached: flags the case
        where the portal did not confirm that the code was sent (otherwise the
        user would wait for an OTP that never arrives, with nothing telling
        them)."""
        if getattr(self._auth, "otp_invio_confermato", None) is False:
            return self._testo("otp_invio_non_confermato")
        return self._testo("otp_solo_da_qui")

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Offer browser OAuth first, while retaining the password/OTP route."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["browser_login", "password_login"],
            description_placeholders={"pod_correnti": self._nota_reauth()},
        )

    async def async_step_password_login(
        self, user_input: dict[str, Any] | None = None
    ):
        errors: dict[str, str] = {}
        messaggio = ""

        if user_input is not None:
            # Dedicated session (not Home Assistant's shared one): this login
            # goes through a Salesforce redirect chain that depends on a session
            # cookie set halfway through, which the shared session does not
            # reliably persist for this domain - the symptom is an infinite
            # redirect loop because the server never sees the cookie come back.
            # Created once and reused across retries of this same flow.
            if self._session is None:
                self._session = async_create_clientsession(self.hass)
            self._auth = AuthClient(self._session)

            try:
                # A space copied along with the address is never part of it.
                await self._auth.async_begin_login(
                    user_input["email"].strip(), user_input["password"]
                )
            except LoginRifiutato as err:
                errors["base"] = _ERRORI_RIFIUTO_LOGIN.get(type(err), "login_rifiutato")
                messaggio = err.messaggio
            except TroppeSessioni:
                # Right credentials, but the account has too many open sessions:
                # no OTP is sent, so there is no point moving to the next step to
                # ask for it.
                _LOGGER.warning("Login rifiutato: troppe sessioni aperte sull'account")
                errors["base"] = "troppe_sessioni"
            except AccessoBloccato:
                _LOGGER.warning("Login: E-Distribuzione ha risposto con la verifica antibot")
                return await self.async_step_antibot()
            except AppBloccata as err:
                _LOGGER.warning("Login: E-Distribuzione blocca il client OAuth (%s)", err)
                return await self.async_step_antibot()
            except ParsingError as err:
                # A page or answer different from the expected one: maintenance,
                # or a change on the portal. Not the credentials.
                _LOGGER.exception("Parsing della pagina di login fallito")
                errors["base"] = "risposta_inattesa"
                messaggio = _descrivi_eccezione(err)
            except (aiohttp.ClientError, TimeoutError):
                _LOGGER.exception("E-Distribuzione non raggiungibile durante il login")
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 - any other unexpected error
                # Anything auth.py does not wrap in its dedicated exceptions:
                # better an error in the form (with the traceback in the logs)
                # than blowing up the step.
                _LOGGER.exception("Errore imprevisto durante il login")
                errors["base"] = "errore_imprevisto"
                messaggio = _descrivi_eccezione(err)
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="password_login",
            data_schema=vol.Schema({vol.Required("email"): str, vol.Required("password"): str}),
            errors=errors,
            description_placeholders={
                "pod_correnti": self._nota_reauth(),
                "messaggio": messaggio,
            },
        )

    async def async_step_antibot(self, user_input: dict[str, Any] | None = None):
        """Offer the interactive browser login after an automated block."""
        return self.async_show_menu(
            step_id="antibot", menu_options=["browser_login", "riprova_login"]
        )

    async def async_step_riprova_login(
        self, user_input: dict[str, Any] | None = None
    ):
        """Return to password login without losing the in-progress flow."""
        return await self.async_step_password_login()

    async def async_step_browser_login(
        self, user_input: dict[str, Any] | None = None
    ):
        """Sign-in in the user's browser.

        The user opens the official portal from the link, signs in there and
        pastes back the address the browser could not open (eneldist://...),
        which carries the tokens: see AuthClient.tokens_from_browser_redirect.
        """
        if self._auth_browser is None:
            if self._session is None:
                self._session = async_create_clientsession(self.hass)
            self._auth_browser = AuthClient(self._session)
            self._browser_auth_url = self._auth_browser.prepare_browser_token_authorization_url()
            _LOGGER.info(
                "Config flow: login nel browser avviato%s",
                " (riautenticazione)" if self._reauth_entry else "",
            )

        errors: dict[str, str] = {}
        messaggio = ""
        if user_input is not None:
            try:
                tokens = self._auth_browser.tokens_from_browser_redirect(
                    user_input["redirect_url"]
                )
            except InvalidBrowserRedirect as err:
                errors["base"] = _ERRORI_LOGIN_BROWSER.get(err.motivo, "browser_url_mancante")
                messaggio = err.dettaglio
            except AppBloccata:
                errors["base"] = "app_bloccata"
            else:
                return await self._autenticazione_completata(tokens)

        return self.async_show_form(
            step_id="browser_login",
            data_schema=vol.Schema({vol.Required("redirect_url"): str}),
            errors=errors,
            description_placeholders={
                "authorization_url": self._browser_auth_url,
                "pod_correnti": self._nota_reauth(),
                "messaggio": messaggio,
            },
        )

    def _nota_reauth(self) -> str:
        """Extra sentence shown only during a reauth, to recall which PODs will
        be re-authenticated - empty during the initial setup."""
        if self._reauth_entry is None:
            return ""
        pods = ", ".join(self._reauth_entry.data.get(CONF_PODS, []))
        return self._testo("nota_reauth", pods=pods)

    async def async_step_otp(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        avviso = self._avviso_iniziale()

        if user_input is not None:
            form_senza_codice = await self._otp_senza_codice(user_input, avviso)
            if form_senza_codice is not None:
                return form_senza_codice

            # Checked here, before using up an attempt on the portal: the code
            # is made of digits only, often pasted with spaces in between.
            codice = user_input["otp"].replace(" ", "")
            if not codice.isdigit():
                return self._form_otp({"base": "otp_formato"}, avviso)

            try:
                tokens = await self._auth.async_submit_otp(codice)
            except InvalidOtp:
                errors["base"] = "invalid_otp"
            except TroppeSessioni:
                errors["base"] = "troppe_sessioni"
            except AccessoBloccato:
                _LOGGER.warning("Convalida OTP: E-Distribuzione ha risposto con la verifica antibot")
                return self.async_abort(reason="accesso_bloccato")
            except AppBloccata as err:
                _LOGGER.warning("Convalida OTP: E-Distribuzione blocca il client OAuth (%s)", err)
                return self.async_abort(reason="app_bloccata")
            except ParsingError:
                # At this point the OTP has already been accepted by Salesforce
                # (otherwise we would have caught InvalidOtp above): the failure
                # is in parsing a later step, not in the code entered. Showing
                # the OTP form again does not help - the OTP is single-use and
                # the ViewState has already advanced, a retry with the same code
                # would fail again the same way.
                _LOGGER.exception("Parsing della pagina OTP fallito")
                return self.async_abort(reason="otp_exchange_failed")
            except Exception:  # noqa: BLE001 - see comment in async_step_password_login
                _LOGGER.exception("Errore imprevisto durante lo scambio del codice OTP")
                return self.async_abort(reason="otp_exchange_failed")
            else:
                return await self._autenticazione_completata(tokens)

        return self._form_otp(errors, avviso)

    async def _autenticazione_completata(self, tokens):
        """Continue identically after browser OAuth or password/OTP login."""
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token
        self._instance_url = getattr(tokens, "instance_url", "")
        if self._reauth_entry is not None:
            if not await self._stesso_account():
                return self.async_abort(
                    reason="account_diverso",
                    description_placeholders={
                        "pods": ", ".join(self._reauth_entry.data.get(CONF_PODS, []))
                    },
                )
            nuovi_dati = {
                **self._reauth_entry.data,
                CONF_ACCESS_TOKEN: self._access_token,
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_INSTANCE_URL: self._instance_url,
            }
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data=nuovi_dati
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        return await self.async_step_pod()

    async def _stesso_account(self) -> bool:
        """On a reauth, whether the new login is for the account of the
        configured PODs.

        The browser login reuses the portal session the browser already has,
        whichever account it belongs to: tokens of another account would leave
        the integration asking for PODs it cannot see. When the PODs cannot be
        read the login is accepted, rather than blocked on a check.
        """
        configurati = set(self._reauth_entry.data.get(CONF_PODS, []))
        api = ApiClient(async_get_clientsession(self.hass), self._access_token)
        try:
            dell_account = {p["IdPod"] for p in await api.async_get_supplies()}
        except Exception:  # noqa: BLE001 - a check, it must not block the reauth
            _LOGGER.warning("Riautenticazione: impossibile verificare i POD dell'account", exc_info=True)
            return True
        if not dell_account or configurati & dell_account:
            return True
        _LOGGER.warning(
            "Riautenticazione con un altro account: %d POD sull'account, nessuno di quelli configurati",
            len(dell_account),
        )
        return False

    # ------------------------------------------------------------------
    # POD selection (initial setup only, not reauth)
    # ------------------------------------------------------------------

    async def async_step_pod(self, user_input: dict[str, Any] | None = None):
        session = async_get_clientsession(self.hass)
        api = ApiClient(session, self._access_token)

        if not self._pods_disponibili:
            try:
                self._pods_disponibili = await api.async_get_supplies()
            except Exception:  # noqa: BLE001 - see comment in async_step_password_login
                # Login and OTP already succeeded: an OTP is single-use, so a
                # retry of this step would not fix anything without redoing
                # login+OTP from scratch.
                _LOGGER.exception("Errore imprevisto nel recupero dei POD")
                return self.async_abort(reason="supplies_failed")

        if not self._pods_disponibili:
            return self.async_abort(reason="no_pods_found")

        def crea_entry(pods: list[str]):
            titolo = pods[0] if len(pods) == 1 else f"{len(pods)} POD"
            return self.async_create_entry(
                title=titolo,
                data={
                    CONF_PODS: pods,
                    CONF_ACCESS_TOKEN: self._access_token,
                    CONF_REFRESH_TOKEN: self._refresh_token,
                    CONF_INSTANCE_URL: self._instance_url,
                },
            )

        # With a single POD on the account there is nothing to choose.
        if len(self._pods_disponibili) == 1:
            return crea_entry([self._pods_disponibili[0]["IdPod"]])

        if user_input is not None:
            scelti = user_input[CONF_PODS]
            if not scelti:
                return self.async_show_form(
                    step_id="pod",
                    data_schema=self._schema_multi_pod(),
                    errors={"pods": "nessun_pod_selezionato"},
                )
            return crea_entry(scelti)

        return self.async_show_form(step_id="pod", data_schema=self._schema_multi_pod())

    def _schema_multi_pod(self) -> vol.Schema:
        pod_ids = [p["IdPod"] for p in self._pods_disponibili]
        opzioni = [{"value": p["IdPod"], "label": _etichetta_pod(p)} for p in self._pods_disponibili]
        return vol.Schema({
            vol.Required(CONF_PODS, default=pod_ids): selector.SelectSelector(
                selector.SelectSelectorConfig(options=opzioni, multiple=True)
            )
        })

    # ------------------------------------------------------------------
    # Reauth: same login+OTP, no POD selection
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        self._reauth_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        _LOGGER.info("Config flow: riautenticazione richiesta")
        # Same choice as the setup: browser (recommended) or password and OTP.
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EdistribuzioneOptionsFlow:
        return EdistribuzioneOptionsFlow()


class EdistribuzioneOptionsFlow(config_entries.OptionsFlow):
    """POD role, add/remove POD, request time - after the initial setup."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=["tipo_pod", "aggiungi_pod", "rimuovi_pod", "orario"],
        )

    async def async_step_tipo_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        tipi_attuali = dict(self.config_entry.options.get(CONF_TIPO_POD, {}))

        if user_input is not None:
            nuovi_tipi = {pod: user_input[f"tipo_{pod}"] for pod in pods}
            nuove_opzioni = {**self.config_entry.options, CONF_TIPO_POD: nuovi_tipi}
            # Updated BEFORE the reload, so the sensors are rebuilt with the new
            # role. async_create_entry(data=...) below REPLACES entry.options
            # entirely with 'data' when the options flow ends: a data={} would
            # also wipe a previously set CONF_ORA_RICHIESTA, so the same complete
            # dict already applied is passed again (no different second write,
            # just the final confirmation the flow requires).
            self.hass.config_entries.async_update_entry(self.config_entry, options=nuove_opzioni)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=nuove_opzioni)

        # Labels in translations/*.json, under selector.tipo_pod.
        opzioni_ruolo = [TIPO_POD_SCAMBIO, TIPO_POD_PRODUZIONE]
        schema = {
            vol.Required(
                f"tipo_{pod}", default=tipi_attuali.get(pod, TIPO_POD_DEFAULT)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(options=opzioni_ruolo, translation_key="tipo_pod")
            )
            for pod in pods
        }
        return self.async_show_form(step_id="tipo_pod", data_schema=vol.Schema(schema))

    async def async_step_orario(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            ora = int(user_input[CONF_ORA_RICHIESTA])
            return self.async_create_entry(
                title="", data={**self.config_entry.options, CONF_ORA_RICHIESTA: ora}
            )

        attuale = self.config_entry.options.get(CONF_ORA_RICHIESTA, ORA_MINIMA_RICHIESTA)
        return self.async_show_form(
            step_id="orario",
            data_schema=vol.Schema({
                vol.Required(CONF_ORA_RICHIESTA, default=attuale): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                )
            }),
            description_placeholders={"ora_attuale": str(attuale)},
        )

    async def async_step_aggiungi_pod(self, user_input: dict[str, Any] | None = None):
        pods_attuali = list(self.config_entry.data.get(CONF_PODS, []))

        if user_input is not None:
            nuovi = user_input.get("pods_da_aggiungere", [])
            if not nuovi:
                return self.async_abort(reason="nessun_pod_selezionato")
            pods_finali = pods_attuali + [p for p in nuovi if p not in pods_attuali]
            new_data = {**self.config_entry.data, CONF_PODS: pods_finali}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # data=... here is the OPTIONS dict (not data) the options flow ends
            # with: pass the current options unchanged, not {}, otherwise it
            # would wipe the already set CONF_TIPO_POD/CONF_ORA_RICHIESTA.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        session = async_get_clientsession(self.hass)
        tutti_pod = await self._pod_con_token_salvato(session)
        if tutti_pod is not None:
            return self._form_aggiungi_pod(pods_attuali, tutti_pod)

        auth = AuthClient(session)
        try:
            tokens = await auth.async_refresh_access_token(
                self.config_entry.data[CONF_REFRESH_TOKEN]
            )
        except AccessoBloccato:
            _LOGGER.warning("Opzioni: E-Distribuzione ha risposto con la verifica antibot")
            return self.async_abort(reason="accesso_bloccato")
        except AppBloccata as err:
            _LOGGER.warning("Opzioni: E-Distribuzione blocca il client OAuth (%s)", err)
            return self.async_abort(reason="app_bloccata")
        except TokenNonValido:
            # Same reaction as the coordinator: ask for a new login, which
            # keeps PODs, options and history.
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="accesso_scaduto")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Refresh del token fallito nelle opzioni")
            return self.async_abort(
                reason="refresh_failed",
                description_placeholders={"errore": _descrivi_eccezione(err)},
            )

        # Kept, as the coordinator does: a rotated refresh_token would
        # otherwise be lost with this flow.
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_INSTANCE_URL: getattr(tokens, "instance_url", ""),
            },
        )
        api = ApiClient(session, tokens.access_token)
        try:
            tutti_pod = await api.async_get_supplies()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recupero POD fallito nelle opzioni")
            return self.async_abort(reason="supplies_failed")
        return self._form_aggiungi_pod(pods_attuali, tutti_pod)

    async def _pod_con_token_salvato(self, session) -> list[dict] | None:
        """The account's PODs with the saved access token, or None when there
        is none or it no longer works.

        Tried before a refresh: after a login in the browser the saved token
        works while the refresh from Home Assistant may be blocked, and adding
        a POD must not depend on it.
        """
        access_token = self.config_entry.data.get(CONF_ACCESS_TOKEN)
        if not access_token:
            return None
        try:
            return await ApiClient(session, access_token).async_get_supplies()
        except Exception as err:  # noqa: BLE001 - the refresh below is the fallback
            _LOGGER.info(
                "Opzioni: access token salvato non utilizzabile (%s), provo il rinnovo",
                _descrivi_eccezione(err),
            )
            return None

    def _form_aggiungi_pod(self, pods_attuali: list[str], tutti_pod: list[dict]):
        pods_disponibili = [p for p in tutti_pod if p["IdPod"] not in pods_attuali]
        if not pods_disponibili:
            return self.async_abort(reason="nessun_pod_da_aggiungere")

        opzioni = [{"value": p["IdPod"], "label": _etichetta_pod(p)} for p in pods_disponibili]

        return self.async_show_form(
            step_id="aggiungi_pod",
            data_schema=vol.Schema({
                vol.Required("pods_da_aggiungere", default=[]): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=opzioni, multiple=True)
                )
            }),
            description_placeholders={
                "pod_correnti": ", ".join(pods_attuali) or testo(self.hass.config.language, "nessun_pod")
            },
        )

    async def async_step_rimuovi_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        if not pods:
            return self.async_abort(reason="nessun_pod")

        if user_input is not None:
            da_rimuovere = set(user_input.get("pods_da_rimuovere", []))
            if len(da_rimuovere) >= len(pods):
                return self.async_show_form(
                    step_id="rimuovi_pod",
                    data_schema=self._schema_rimuovi(pods),
                    errors={"pods_da_rimuovere": "non_puoi_rimuoverli_tutti"},
                )
            pods_rimasti = [p for p in pods if p not in da_rimuovere]
            new_data = {**self.config_entry.data, CONF_PODS: pods_rimasti}
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            # See the comment in async_step_aggiungi_pod: 'data' here is the
            # options the flow ends with, not {}.
            return self.async_create_entry(title="", data=dict(self.config_entry.options))

        return self.async_show_form(step_id="rimuovi_pod", data_schema=self._schema_rimuovi(pods))

    @staticmethod
    def _schema_rimuovi(pods: list[str]) -> vol.Schema:
        return vol.Schema({
            vol.Required("pods_da_rimuovere", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=pods, multiple=True)
            )
        })
