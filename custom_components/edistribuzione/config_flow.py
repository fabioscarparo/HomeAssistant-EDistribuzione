"""Config and options flows for the edistribuzione integration."""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.util import dt as dt_util

from .api import ApiClient
from .auth import (
    AccessoBloccato,
    AccountBloccato,
    AppBloccata,
    AuthClient,
    ClientNonValido,
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
    CONF_CLIENT_ID_OAUTH,
    CONF_ENERGIA_REATTIVA,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_INSTANCE_URL,
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    CONF_TOKEN_AGGIORNATO_IL,
    CONF_TOKEN_ORIGINE,
    DOMAIN,
    LOGIN_PAGE_URL,
    LOGIN_PASSWORD_DISPONIBILE,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_ID_PRECEDENTE,
    ORA_MINIMA_RICHIESTA,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
    client_id_della_entry,
)
from .dati_api import ruolo_suggerito_da_fornitura
from .redaction import mask_pods_in_text, redact_secrets
from .testi import testo

_LOGGER = logging.getLogger(__name__)

# OTP is Optional so the user can tick the resend box without entering a code.
STEP_OTP_SCHEMA = vol.Schema({
    vol.Optional("otp", default=""): str,
    vol.Optional("richiedi_nuovo_codice", default=False): bool,
})

STEP_PASSWORD_LOGIN_SCHEMA = vol.Schema({
    vol.Required("email"): selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL, autocomplete="username")
    ),
    vol.Required("password"): selector.TextSelector(
        selector.TextSelectorConfig(
            type=selector.TextSelectorType.PASSWORD, autocomplete="current-password"
        )
    ),
})

STEP_BROWSER_LOGIN_SCHEMA = vol.Schema({
    vol.Required("redirect_url"): selector.TextSelector(
        selector.TextSelectorConfig(multiline=True, autocomplete="off")
    ),
})

_ERRORI_RIFIUTO_LOGIN: dict[type[LoginRifiutato], str] = {
    InvalidCredentials: "invalid_auth",
    AccountBloccato: "account_bloccato",
    PasswordScaduta: "password_scaduta",
}

_ERRORI_LOGIN_BROWSER = {
    "mancante": "browser_url_mancante",
    "negato": "browser_negato",
    "altro_tentativo": "browser_altro_link",
    "token_mancanti": "browser_token_mancanti",
}


def _descrivi_eccezione(err: Exception) -> str:
    """Short, scrubbed technical detail for unexpected error messages."""
    raw = str(err).strip()
    if not raw:
        return type(err).__name__
    safe = mask_pods_in_text(redact_secrets(raw))
    return f"{type(err).__name__}: {safe}"[:200]


def _titolo_per_pods(pods: list[str]) -> str:
    return pods[0] if len(pods) == 1 else f"{len(pods)} POD"


def _calcola_unique_id(pods_disponibili: list[dict]) -> str:
    """Derive a stable, privacy-safe unique_id from the account's tax code or PODs."""
    for pod_info in pods_disponibili:
        for chiave in ("TaxCode", "VatNumber"):
            valore = (pod_info.get(chiave) or "").strip().upper()
            if valore:
                return hashlib.sha256(f"{chiave}:{valore}".encode()).hexdigest()[:32]
    pod_ids = sorted(
        p["IdPod"].strip().upper() for p in pods_disponibili if isinstance(p.get("IdPod"), str)
    )
    return hashlib.sha256(",".join(pod_ids).encode()).hexdigest()[:32]


def _etichetta_pod(pod_info: dict) -> str:
    """Build 'IT001E12345678 - Via Roma 1, Milano (MI)' without None or stray punctuation."""
    via = " ".join(
        parte
        for chiave in (
            "PointOfMeasureStreetPrefix",
            "PointOfMeasureStreet",
            "PointOfMeasureStreetNumber",
        )
        if (parte := str(pod_info.get(chiave) or "").strip())
    )
    comune = str(pod_info.get("PointOfMeasureMunicipality") or "").strip()
    provincia = str(pod_info.get("PointOfMeasureProvince") or "").strip()
    localita = f"{comune} ({provincia})".strip() if provincia else comune
    indirizzo = ", ".join(parte for parte in (via, localita) if parte)
    pod_id = pod_info["IdPod"]
    return f"{pod_id} - {indirizzo}" if indirizzo else pod_id


class EdistribuzioneConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for the edistribuzione integration."""

    VERSION = 1

    def __init__(self) -> None:
        self._session = None
        self._auth: AuthClient | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._instance_url = ""
        self._token_origine = "password_otp"
        self._auth_browser: AuthClient | None = None
        self._browser_auth_url: str | None = None
        self._pods_disponibili: list[dict] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    def async_remove(self) -> None:
        """Detach the temporary login session when the flow finishes or is cancelled."""
        if self._session is not None:
            detach = getattr(self._session, "detach", None)
            if callable(detach):
                detach()
            self._session = None

    def _testo(self, chiave: str, **segnaposto: str) -> str:
        return testo(self.hass.config.language, chiave, **segnaposto)

    # ------------------------------------------------------------------
    # Login email/password -> OTP
    # ------------------------------------------------------------------

    async def _reinvia_otp(self) -> tuple[str | None, str | None]:
        try:
            confermato = await self._auth.async_resend_otp()
        except TroppeSessioni:
            _LOGGER.warning("Reinvio OTP rifiutato: troppe sessioni aperte sull'account")
            return "troppe_sessioni", None
        except AccessoBloccato:
            _LOGGER.warning("Reinvio OTP: E-Distribuzione ha risposto con la verifica antibot")
            return "accesso_bloccato", None
        except Exception:  # noqa: BLE001 - unexpected error shown in form
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
        if user_input.get("richiedi_nuovo_codice"):
            errore, avviso_reinvio = await self._reinvia_otp()
            return self._form_otp({"base": errore} if errore else {}, avviso_reinvio or avviso)
        if not user_input.get("otp"):
            return self._form_otp({"base": "otp_mancante"}, avviso)
        return None

    def _avviso_iniziale(self) -> str:
        if getattr(self._auth, "otp_invio_confermato", None) is False:
            return self._testo("otp_invio_non_confermato")
        return self._testo("otp_solo_da_qui")

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """Go straight to browser sign-in while automated password login is blocked."""
        if not LOGIN_PASSWORD_DISPONIBILE:
            return await self.async_step_browser_login()
        return self.async_show_menu(
            step_id="user",
            menu_options=["browser_login", "password_login"],
            description_placeholders={"pod_correnti": self._nota_reauth()},
        )

    async def async_step_password_login(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        messaggio = ""

        if user_input is not None:
            # Dedicated session for Salesforce's redirect cookie chain; detached in async_remove.
            if self._session is None:
                self._session = async_create_clientsession(self.hass, auto_cleanup=False)
            self._auth = AuthClient(self._session)

            try:
                await self._auth.async_begin_login(
                    user_input["email"].strip(), user_input["password"]
                )
            except ErroreTecnicoPortale as err:
                _LOGGER.warning(
                    "Login: E-Distribuzione ha risposto %r, propongo il browser", err.messaggio
                )
                return await self.async_step_antibot()
            except LoginRifiutato as err:
                errors["base"] = _ERRORI_RIFIUTO_LOGIN.get(type(err), "login_rifiutato")
                messaggio = err.messaggio
            except TroppeSessioni:
                _LOGGER.warning("Login rifiutato: troppe sessioni aperte sull'account")
                errors["base"] = "troppe_sessioni"
            except AccessoBloccato:
                _LOGGER.warning("Login: E-Distribuzione ha risposto con la verifica antibot")
                return await self.async_step_antibot()
            except AppBloccata as err:
                _LOGGER.warning("Login: E-Distribuzione blocca il client OAuth (%s)", err)
                return await self.async_step_antibot()
            except ParsingError as err:
                _LOGGER.exception("Parsing della pagina di login fallito")
                errors["base"] = "risposta_inattesa"
                messaggio = _descrivi_eccezione(err)
            except (aiohttp.ClientError, TimeoutError):
                _LOGGER.exception("E-Distribuzione non raggiungibile durante il login")
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 - catch-all to show error in form
                _LOGGER.exception("Errore imprevisto durante il login")
                errors["base"] = "errore_imprevisto"
                messaggio = _descrivi_eccezione(err)
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="password_login",
            data_schema=STEP_PASSWORD_LOGIN_SCHEMA,
            errors=errors,
            description_placeholders={
                "pod_correnti": self._nota_reauth(),
                "messaggio": messaggio,
            },
        )

    async def async_step_antibot(self, user_input: dict[str, Any] | None = None):
        """Offer interactive browser login after an automated block."""
        return self.async_show_menu(
            step_id="antibot", menu_options=["browser_login", "riprova_login"]
        )

    async def async_step_riprova_login(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_password_login()

    async def async_step_browser_login(self, user_input: dict[str, Any] | None = None):
        """Interactive OAuth user-agent sign-in in the user's browser (no HTTP calls here)."""
        if self._auth_browser is None:
            self._auth_browser = AuthClient(None)
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
                return await self._autenticazione_completata(tokens, origine="browser")

        return self.async_show_form(
            step_id="browser_login",
            data_schema=STEP_BROWSER_LOGIN_SCHEMA,
            errors=errors,
            description_placeholders={
                "login_url": LOGIN_PAGE_URL,
                "authorization_url": self._browser_auth_url,
                "pod_correnti": self._nota_reauth(),
                "messaggio": messaggio,
            },
        )

    def _nota_reauth(self) -> str:
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
                _LOGGER.warning(
                    "Convalida OTP: E-Distribuzione ha risposto con la verifica antibot, "
                    "propongo il browser"
                )
                return await self.async_step_antibot()
            except AppBloccata as err:
                _LOGGER.warning("Convalida OTP: E-Distribuzione blocca il client OAuth (%s)", err)
                return self.async_abort(reason="app_bloccata")
            except ParsingError:
                _LOGGER.exception("Parsing della pagina OTP fallito")
                return self.async_abort(reason="otp_exchange_failed")
            except Exception:  # noqa: BLE001 - catch-all to abort cleanly
                _LOGGER.exception("Errore imprevisto durante lo scambio del codice OTP")
                return self.async_abort(reason="otp_exchange_failed")
            else:
                return await self._autenticazione_completata(tokens, origine="password_otp")

        return self._form_otp(errors, avviso)

    async def _autenticazione_completata(self, tokens, *, origine: str = "password_otp"):
        self._access_token = tokens.access_token
        self._refresh_token = tokens.refresh_token
        self._instance_url = getattr(tokens, "instance_url", "")
        self._token_origine = origine
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
                CONF_CLIENT_ID_OAUTH: OAUTH_CLIENT_ID,
                CONF_INSTANCE_URL: self._instance_url,
                CONF_TOKEN_ORIGINE: origine,
                CONF_TOKEN_AGGIORNATO_IL: dt_util.now().isoformat(timespec="seconds"),
            }
            return self.async_update_reload_and_abort(
                self._reauth_entry, data=nuovi_dati, reason="reauth_successful"
            )
        return await self.async_step_pod()

    async def _stesso_account(self) -> bool:
        """Verify on reauth that the new tokens belong to the account of the configured PODs."""
        configurati = set(self._reauth_entry.data.get(CONF_PODS, []))
        api = ApiClient(async_get_clientsession(self.hass), self._access_token)
        try:
            dell_account = {p["IdPod"] for p in await api.async_get_supplies()}
        except Exception:  # noqa: BLE001 - advisory check, must not block reauth
            _LOGGER.warning(
                "Riautenticazione: impossibile verificare i POD dell'account", exc_info=True
            )
            return True
        if not dell_account or configurati & dell_account:
            return True
        _LOGGER.warning(
            "Riautenticazione con un altro account: %d POD sull'account, nessuno di quelli configurati",
            len(dell_account),
        )
        return False

    # ------------------------------------------------------------------
    # POD selection (initial setup only)
    # ------------------------------------------------------------------

    async def async_step_pod(self, user_input: dict[str, Any] | None = None):
        session = async_get_clientsession(self.hass)
        api = ApiClient(session, self._access_token)

        if not self._pods_disponibili:
            try:
                self._pods_disponibili = await api.async_get_supplies()
            except Exception:  # noqa: BLE001 - abort cleanly on supplies failure
                _LOGGER.exception("Errore imprevisto nel recupero dei POD")
                return self.async_abort(reason="supplies_failed")

        if not self._pods_disponibili:
            return self.async_abort(reason="no_pods_found")

        await self.async_set_unique_id(_calcola_unique_id(self._pods_disponibili))
        self._abort_if_unique_id_configured()

        def crea_entry(pods: list[str]):
            ruoli_suggeriti = {
                p["IdPod"]: ruolo_suggerito_da_fornitura(p)
                for p in self._pods_disponibili
                if p.get("IdPod") in pods and p.get("MeteringPointType")
            }
            entry_kwargs: dict[str, Any] = {
                "title": _titolo_per_pods(pods),
                "data": {
                    CONF_PODS: pods,
                    CONF_ACCESS_TOKEN: self._access_token,
                    CONF_REFRESH_TOKEN: self._refresh_token,
                    CONF_CLIENT_ID_OAUTH: OAUTH_CLIENT_ID,
                    CONF_INSTANCE_URL: self._instance_url,
                    CONF_TOKEN_ORIGINE: self._token_origine,
                    CONF_TOKEN_AGGIORNATO_IL: dt_util.now().isoformat(timespec="seconds"),
                },
            }
            if ruoli_suggeriti:
                entry_kwargs["options"] = {CONF_TIPO_POD: ruoli_suggeriti}
            return self.async_create_entry(**entry_kwargs)

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
    # Reauth
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]):
        self._reauth_entry = self._get_reauth_entry()
        _LOGGER.info("Config flow: riautenticazione richiesta")
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> EdistribuzioneOptionsFlow:
        return EdistribuzioneOptionsFlow()


class EdistribuzioneOptionsFlow(config_entries.OptionsFlow):
    """POD role, add/remove POD and request hour options."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "tipo_pod",
                "aggiungi_pod",
                "rimuovi_pod",
                "orario",
                "energia_reattiva",
            ],
        )

    async def async_step_tipo_pod(self, user_input: dict[str, Any] | None = None):
        pods = list(self.config_entry.data.get(CONF_PODS, []))
        tipi_attuali = dict(self.config_entry.options.get(CONF_TIPO_POD, {}))

        if user_input is not None:
            nuovi_tipi = {pod: user_input[f"tipo_{pod}"] for pod in pods}
            nuove_opzioni = {**self.config_entry.options, CONF_TIPO_POD: nuovi_tipi}
            self.hass.config_entries.async_update_entry(self.config_entry, options=nuove_opzioni)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=nuove_opzioni)

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

    async def async_step_energia_reattiva(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            attiva = bool(user_input[CONF_ENERGIA_REATTIVA])
            nuove_opzioni = {**self.config_entry.options, CONF_ENERGIA_REATTIVA: attiva}
            self.hass.config_entries.async_update_entry(self.config_entry, options=nuove_opzioni)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=nuove_opzioni)

        attuale = bool(self.config_entry.options.get(CONF_ENERGIA_REATTIVA, False))
        return self.async_show_form(
            step_id="energia_reattiva",
            data_schema=vol.Schema({
                vol.Required(CONF_ENERGIA_REATTIVA, default=attuale): selector.BooleanSelector()
            }),
        )

    async def async_step_aggiungi_pod(self, user_input: dict[str, Any] | None = None):
        pods_attuali = list(self.config_entry.data.get(CONF_PODS, []))

        if user_input is not None:
            nuovi = user_input.get("pods_da_aggiungere", [])
            if not nuovi:
                return self.async_abort(reason="nessun_pod_selezionato")
            pods_finali = pods_attuali + [p for p in nuovi if p not in pods_attuali]
            new_data = {**self.config_entry.data, CONF_PODS: pods_finali}
            new_options = dict(self.config_entry.options)
            tutti_salvati = getattr(self, "_tutti_pod_disponibili", [])
            ruoli_aggiunti = {
                p["IdPod"]: ruolo_suggerito_da_fornitura(p)
                for p in tutti_salvati
                if p.get("IdPod") in nuovi and p.get("MeteringPointType")
            }
            if ruoli_aggiunti:
                tipi = dict(new_options.get(CONF_TIPO_POD, {}))
                for pod_id, ruolo in ruoli_aggiunti.items():
                    tipi.setdefault(pod_id, ruolo)
                new_options[CONF_TIPO_POD] = tipi
            kwargs: dict[str, Any] = {"data": new_data, "options": new_options}
            if self.config_entry.title == _titolo_per_pods(pods_attuali):
                kwargs["title"] = _titolo_per_pods(pods_finali)
            self.hass.config_entries.async_update_entry(self.config_entry, **kwargs)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=new_options)

        session = async_get_clientsession(self.hass)
        tutti_pod = await self._pod_con_token_salvato(session)
        if tutti_pod is not None:
            return self._form_aggiungi_pod(pods_attuali, tutti_pod)

        refresh_token = self.config_entry.data.get(CONF_REFRESH_TOKEN)
        if not refresh_token:
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="accesso_scaduto")

        client_id = client_id_della_entry(self.config_entry.data)
        auth = AuthClient(session, instance_url=self.config_entry.data.get(CONF_INSTANCE_URL))
        try:
            tokens = await auth.async_refresh_access_token(refresh_token, client_id)
        except AccessoBloccato:
            _LOGGER.warning("Opzioni: E-Distribuzione ha risposto con la verifica antibot")
            return self.async_abort(reason="accesso_bloccato")
        except (ClientNonValido, AppBloccata) as err:
            if client_id == OAUTH_CLIENT_ID_PRECEDENTE:
                _LOGGER.warning(
                    "Opzioni: client OAuth precedente rifiutato (%s), avvio riautenticazione",
                    err,
                )
                self.config_entry.async_start_reauth(self.hass)
                return self.async_abort(reason="client_precedente_rifiutato")
            if isinstance(err, ClientNonValido):
                _LOGGER.warning("Opzioni: client OAuth attuale rifiutato (%s)", err)
                return self.async_abort(reason="client_non_valido")
            _LOGGER.warning("Opzioni: E-Distribuzione blocca il client OAuth (%s)", err)
            return self.async_abort(reason="app_bloccata")
        except TokenNonValido:
            self.config_entry.async_start_reauth(self.hass)
            return self.async_abort(reason="accesso_scaduto")
        except Exception as err:  # noqa: BLE001
            _LOGGER.exception("Refresh del token fallito nelle opzioni")
            return self.async_abort(
                reason="refresh_failed",
                description_placeholders={"errore": _descrivi_eccezione(err)},
            )

        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **self.config_entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_INSTANCE_URL: getattr(tokens, "instance_url", ""),
                CONF_TOKEN_ORIGINE: "refresh",
                CONF_TOKEN_AGGIORNATO_IL: dt_util.now().isoformat(timespec="seconds"),
            },
        )
        # Keep the running coordinator's token and Repairs state in sync.
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if coordinator is not None and hasattr(coordinator, "registra_rinnovo_esterno"):
            coordinator.registra_rinnovo_esterno(tokens.access_token)

        api = ApiClient(session, tokens.access_token)
        try:
            tutti_pod = await api.async_get_supplies()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recupero POD fallito nelle opzioni")
            return self.async_abort(reason="supplies_failed")
        return self._form_aggiungi_pod(pods_attuali, tutti_pod)

    async def _pod_con_token_salvato(self, session) -> list[dict] | None:
        access_token = self.config_entry.data.get(CONF_ACCESS_TOKEN)
        if not access_token:
            return None
        try:
            return await ApiClient(session, access_token).async_get_supplies()
        except Exception as err:  # noqa: BLE001 - fall back to token refresh
            _LOGGER.info(
                "Opzioni: access token salvato non utilizzabile (%s), provo il rinnovo",
                _descrivi_eccezione(err),
            )
            return None

    def _form_aggiungi_pod(self, pods_attuali: list[str], tutti_pod: list[dict]):
        self._tutti_pod_disponibili = list(tutti_pod)
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
                "pod_correnti": ", ".join(pods_attuali)
                or testo(self.hass.config.language, "nessun_pod")
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

            # Scrub removed PODs from retry queue, options and Device Registry.
            new_data = {**self.config_entry.data, CONF_PODS: pods_rimasti}
            if CONF_GIORNI_DA_RIPROVARE in new_data and isinstance(
                new_data[CONF_GIORNI_DA_RIPROVARE], dict
            ):
                new_data[CONF_GIORNI_DA_RIPROVARE] = {
                    p: coda
                    for p, coda in new_data[CONF_GIORNI_DA_RIPROVARE].items()
                    if p not in da_rimuovere
                }

            new_options = dict(self.config_entry.options)
            if CONF_TIPO_POD in new_options and isinstance(new_options[CONF_TIPO_POD], dict):
                new_options[CONF_TIPO_POD] = {
                    p: ruolo
                    for p, ruolo in new_options[CONF_TIPO_POD].items()
                    if p not in da_rimuovere
                }

            kwargs: dict[str, Any] = {"data": new_data, "options": new_options}
            if self.config_entry.title == _titolo_per_pods(pods):
                kwargs["title"] = _titolo_per_pods(pods_rimasti)
            self.hass.config_entries.async_update_entry(self.config_entry, **kwargs)

            dev_reg = dr.async_get(self.hass)
            entry_id = self.config_entry.entry_id
            for device in list(dr.async_entries_for_config_entry(dev_reg, entry_id)):
                if any(
                    dominio == DOMAIN and ident in {f"{entry_id}_{p}" for p in da_rimuovere}
                    for dominio, ident in device.identifiers
                ):
                    dev_reg.async_remove_device(device.id)

            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(step_id="rimuovi_pod", data_schema=self._schema_rimuovi(pods))

    @staticmethod
    def _schema_rimuovi(pods: list[str]) -> vol.Schema:
        return vol.Schema({
            vol.Required("pods_da_rimuovere", default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=pods, multiple=True)
            )
        })
