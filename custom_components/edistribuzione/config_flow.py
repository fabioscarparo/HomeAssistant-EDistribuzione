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
    AuthClient,
    InvalidCredentials,
    InvalidOtp,
    ParsingError,
    TroppeSessioni,
)
from .const import (
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
        except Exception:  # noqa: BLE001 - see comment in async_step_user
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
        errors: dict[str, str] = {}

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
                await self._auth.async_begin_login(user_input["email"], user_input["password"])
            except InvalidCredentials:
                errors["base"] = "invalid_auth"
            except TroppeSessioni:
                # Right credentials, but the account has too many open sessions:
                # no OTP is sent, so there is no point moving to the next step to
                # ask for it.
                _LOGGER.warning("Login rifiutato: troppe sessioni aperte sull'account")
                errors["base"] = "troppe_sessioni"
            except AccessoBloccato:
                # Before ParsingError: the anti-bot page does not contain the
                # expected fields, but the problem is not a markup change.
                _LOGGER.warning("Login: E-Distribuzione ha risposto con la verifica antibot")
                errors["base"] = "accesso_bloccato"
            except ParsingError:
                _LOGGER.exception("Parsing della pagina di login fallito")
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - any other unexpected error
                # aiohttp.ClientError, timeout, a non-JSON response where one was
                # expected, or anything else auth.py does not wrap in its
                # dedicated exceptions: better an error in the form (with the
                # traceback in the logs) than blowing up the step.
                _LOGGER.exception("Errore imprevisto durante il login")
                errors["base"] = "cannot_connect"
            else:
                return await self.async_step_otp()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("email"): str, vol.Required("password"): str}),
            errors=errors,
            description_placeholders={"pod_correnti": self._nota_reauth()},
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

            try:
                tokens = await self._auth.async_submit_otp(user_input["otp"])
            except InvalidOtp:
                errors["base"] = "invalid_otp"
            except TroppeSessioni:
                errors["base"] = "troppe_sessioni"
            except AccessoBloccato:
                _LOGGER.warning("Convalida OTP: E-Distribuzione ha risposto con la verifica antibot")
                return self.async_abort(reason="accesso_bloccato")
            except ParsingError:
                # At this point the OTP has already been accepted by Salesforce
                # (otherwise we would have caught InvalidOtp above): the failure
                # is in parsing a later step, not in the code entered. Showing
                # the OTP form again does not help - the OTP is single-use and
                # the ViewState has already advanced, a retry with the same code
                # would fail again the same way.
                _LOGGER.exception("Parsing della pagina OTP fallito")
                return self.async_abort(reason="otp_exchange_failed")
            except Exception:  # noqa: BLE001 - see comment in async_step_user
                _LOGGER.exception("Errore imprevisto durante lo scambio del codice OTP")
                return self.async_abort(reason="otp_exchange_failed")
            else:
                self._access_token = tokens.access_token
                self._refresh_token = tokens.refresh_token
                if self._reauth_entry is not None:
                    nuovi_dati = {
                        **self._reauth_entry.data,
                        CONF_REFRESH_TOKEN: tokens.refresh_token,
                    }
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry, data=nuovi_dati
                    )
                    await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                    return self.async_abort(reason="reauth_successful")
                return await self.async_step_pod()

        return self._form_otp(errors, avviso)

    # ------------------------------------------------------------------
    # POD selection (initial setup only, not reauth)
    # ------------------------------------------------------------------

    async def async_step_pod(self, user_input: dict[str, Any] | None = None):
        session = async_get_clientsession(self.hass)
        api = ApiClient(session, self._access_token)

        if not self._pods_disponibili:
            try:
                self._pods_disponibili = await api.async_get_supplies()
            except Exception:  # noqa: BLE001 - see comment in async_step_user
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
                data={CONF_PODS: pods, CONF_REFRESH_TOKEN: self._refresh_token},
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
        auth = AuthClient(session)
        try:
            tokens = await auth.async_refresh_access_token(
                self.config_entry.data[CONF_REFRESH_TOKEN]
            )
        except AccessoBloccato:
            _LOGGER.warning("Opzioni: E-Distribuzione ha risposto con la verifica antibot")
            return self.async_abort(reason="accesso_bloccato")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Refresh del token fallito nelle opzioni")
            return self.async_abort(reason="refresh_failed")

        api = ApiClient(session, tokens.access_token)
        try:
            tutti_pod = await api.async_get_supplies()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Recupero POD fallito nelle opzioni")
            return self.async_abort(reason="supplies_failed")

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
