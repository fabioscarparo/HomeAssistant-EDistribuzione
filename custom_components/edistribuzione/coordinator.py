"""DataUpdateCoordinator for E-Distribuzione.

Supports multiple PODs on the same config entry. On each daily window it fetches
both directions (consumption and injection) over the last GIORNI_RICONTROLLO
days plus any queued backlog, incrementally updating Recorder statistics.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import date, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import raw_storage
from .api import ApiClient, ApiError
from .auth import (
    AccessoBloccato,
    AppBloccata,
    AuthClient,
    AuthError,
    ClientNonValido,
    TokenNonValido,
)
from .const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CHUNK_GIORNI_STORICO,
    CONF_ACCESS_TOKEN,
    CONF_ENERGIA_REATTIVA,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_INSTANCE_URL,
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    CONF_TOKEN_AGGIORNATO_IL,
    CONF_TOKEN_ORIGINE,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    FINESTRA_LETTURE_GIORNI,
    GIORNI_RICONTROLLO,
    ISSUE_ACCESSO_BLOCCATO,
    ISSUE_APP_BLOCCATA,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    MAGNITUDE_REATTIVE,
    MAGNITUDE_TUTTE,
    MAX_EVENTI_CRONOLOGIA,
    MAX_GIORNI_IN_CODA,
    MAX_GIORNI_RECUPERO_STORICO,
    OAUTH_CLIENT_ID_PRECEDENTE,
    ORA_MINIMA_RICHIESTA,
    RITARDO_DATI_GIORNI,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
    client_id_della_entry,
    issue_id_per_entry,
)
from .dati_api import (
    calcola_reattiva_e_cos_phi,
    estrai_fornitura,
    estrai_mensili,
    estrai_ultima_lettura,
)
from .fasce import FUSO_ITALIA
from .redaction import mask_pod, mask_pods_in_text, redact_secrets
from .statistics import (
    async_get_ultima_data_disponibile,
    async_import_curva_giornaliera,
    async_ricostruisci_serie_mancanti,
)
from .testi import testo

_LOGGER = logging.getLogger(__name__)


def _giorni_nel_periodo(data_da: date, data_a: date) -> list[date]:
    """List of days in [data_da, data_a], both ends included."""
    giorni, cursore = [], data_da
    while cursore <= data_a:
        giorni.append(cursore)
        cursore += timedelta(days=1)
    return giorni


def _giorni_ricevuti(curva: list[dict]) -> set[date]:
    """Days present in the response with non-empty sampleValues."""
    giorni: set[date] = set()
    for elemento in curva:
        readings = elemento.get("readings") or {}
        if not readings.get("sampleValues"):
            continue
        grezzo = readings.get("sampleDate")
        try:
            giorni.add(date(int(grezzo[:4]), int(grezzo[4:6]), int(grezzo[6:8])))
        except (TypeError, ValueError, IndexError):
            _LOGGER.warning("sampleDate non interpretabile, giorno ignorato: %r", grezzo)
    return giorni


def _curva_ha_dati(curva: list[dict]) -> bool:
    """True if at least one day in the response carries samples."""
    return any((elemento.get("readings") or {}).get("sampleValues") for elemento in curva)


def _magnitude_onorata(curva: list[dict], magnitude_richiesta: str) -> bool:
    """True if energyType matches the requested magnitude (or is absent)."""
    for elemento in curva:
        energy_type = (elemento.get("readings") or {}).get("energyType")
        if energy_type is not None and energy_type != magnitude_richiesta:
            return False
    return True


def _riepilogo_pod(
    ultima_data: date | None,
    ultimi_giorni: dict[str, tuple[date, float] | None],
    picchi: dict[str, tuple[date, float, str] | None] | None = None,
) -> dict:
    """Summary values for a POD's diagnostic sensors."""
    giorni_presenti = [giorno for giorno, _ in filter(None, ultimi_giorni.values())]
    per_direzione: dict[str, dict | None] = {}
    for direzione, valore in ultimi_giorni.items():
        if valore is None and giorni_presenti:
            valore = (max(giorni_presenti), 0.0)
        per_direzione[direzione] = (
            None if valore is None else {"giorno": valore[0].isoformat(), "kwh": round(valore[1], 3)}
        )
    risultato: dict[str, Any] = {
        "ultima_data_disponibile": ultima_data.isoformat() if ultima_data else None,
        "ultimo_giorno": per_direzione,
    }
    if picchi is not None:
        risultato["picco_ultimo_giorno"] = {
            direzione: (
                None
                if valore is None
                else {"giorno": valore[0].isoformat(), "kw": valore[1], "ora": valore[2]}
            )
            for direzione, valore in picchi.items()
        }
    return risultato


def _suddividi_intervallo(data_da: date, data_a: date, passo: int) -> list[tuple[date, date]]:
    """Split [data_da, data_a] into inclusive chunks of at most 'passo' days."""
    chunks: list[tuple[date, date]] = []
    cursore = data_da
    while cursore <= data_a:
        fine = min(cursore + timedelta(days=passo - 1), data_a)
        chunks.append((cursore, fine))
        cursore = fine + timedelta(days=1)
    return chunks


def _descrivi_errore_api(err: ApiError, lingua: str) -> str:
    """User-facing sentence describing a data API failure."""
    if err.rete:
        return testo(lingua, "api_non_raggiungibile")
    if err.status in (401, 403):
        return testo(lingua, "api_non_autorizzata", stato=str(err.status))
    if err.status is not None and err.status >= 500:
        return testo(lingua, "api_non_disponibile", stato=str(err.status))
    if err.status is not None:
        return testo(lingua, "api_rifiutata", stato=str(err.status))
    return testo(lingua, "api_errore", dettaglio=str(err))


class EdistribuzioneCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({entry.title})",
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
            config_entry=entry,
        )
        self.entry = entry
        self.pods: list[str] = list(entry.data[CONF_PODS])
        self.accesso_bloccato = False
        self.ultimo_errore_accesso: str | None = None
        self.ultimo_rinnovo: str | None = None
        self.riepilogo: dict[str, dict] = {}
        self.ultime_richieste: dict[str, dict] = {}
        self.forniture: dict[str, dict[str, Any]] = {}
        self.mensili: dict[str, dict[str, dict[str, Any] | None]] = {}
        self.reattiva: dict[str, dict[str, dict[str, Any] | None]] = {}
        self.letture_ufficiali: dict[str, dict[str, Any] | None] = {}

        self._cronologia_eventi: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTI_CRONOLOGIA)
        # Serialize token refreshes and data imports across polling and manual actions.
        self._token_lock = asyncio.Lock()
        self._import_lock = asyncio.Lock()
        # Prevent hourly hammering when a POD has no published data yet or backlog stays empty.
        self._bootstrap_tentato: set[str] = set()
        self._ultimo_tentativo_senza_dati: dict[str, date] = {}
        self._ultimo_tentativo_coda: dict[str, date] = {}
        self._forniture_giorno: date | None = None
        self._mensili_giorno: dict[str, date] = {}
        self._letture_giorno: dict[str, date] = {}
        self._ausiliari_avvio_tentati = False

        session = async_get_clientsession(hass)
        self._auth = AuthClient(session, instance_url=entry.data.get(CONF_INSTANCE_URL))
        self._api = ApiClient(session, access_token="")

    @property
    def energia_reattiva_attiva(self) -> bool:
        """True when opt-in monthly reactive energy and power factor polling is enabled."""
        return bool(self.entry.options.get(CONF_ENERGIA_REATTIVA, False))

    @property
    def host_ultimo_accesso(self) -> str | None:
        """Salesforce host used by the last login or refresh, or None."""
        return self._auth.host_attivo

    @property
    def cronologia_eventi(self) -> list[dict[str, Any]]:
        """Recent ring buffer of refresh, polling and history events for diagnostics."""
        return list(self._cronologia_eventi)

    @property
    def ora_richiesta_effettiva(self) -> int:
        """Effective local hour (0-23) used to trigger daily requests."""
        return self._ora_richiesta

    @property
    def origine_token(self) -> str | None:
        return self.entry.data.get(CONF_TOKEN_ORIGINE)

    @property
    def token_aggiornato_il(self) -> str | None:
        return self.entry.data.get(CONF_TOKEN_AGGIORNATO_IL)

    def _registra_evento(self, tipo: str, esito: str, durata_ms: int, **extra: Any) -> None:
        evento: dict[str, Any] = {
            "quando": dt_util.now().isoformat(timespec="seconds"),
            "tipo": tipo,
            "esito": esito,
            "durata_ms": durata_ms,
        }
        evento.update(extra)
        self._cronologia_eventi.append(evento)

    def _issue_id(self, issue_base: str) -> str:
        return issue_id_per_entry(issue_base, self.entry.entry_id)

    def tipo_pod(self, pod: str) -> str:
        """User-assigned POD role (exchange or production)."""
        tipi = self.entry.options.get(CONF_TIPO_POD, {})
        return tipi.get(pod, TIPO_POD_DEFAULT)

    @property
    def lingua(self) -> str:
        """Server language for strings HA does not translate."""
        return self.hass.config.language

    def _nome_serie(self, pod: str, immessa: bool) -> str:
        if self.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            chiave = "serie_produzione" if immessa else "serie_prelievo_tecnico"
        else:
            chiave = "serie_immissione" if immessa else "serie_prelievo"
        return f"E-Distribuzione {pod} - {testo(self.lingua, chiave)}"

    def _segnala_blocco(self, issue: str) -> None:
        """Create a per-entry Repairs notice when automated refresh is blocked."""
        self.accesso_bloccato = True
        _LOGGER.warning("Accesso a E-Distribuzione bloccato: avviso %s in Riparazioni", issue)
        for altro in (ISSUE_ACCESSO_BLOCCATO, ISSUE_APP_BLOCCATA):
            if altro != issue:
                ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(altro))
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id(issue),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue,
            data={"entry_id": self.entry.entry_id},
        )

    def _registra_errore_accesso(self, err: Exception) -> None:
        safe_msg = mask_pods_in_text(redact_secrets(str(err)))
        self.ultimo_errore_accesso = f"{dt_util.now().isoformat()} {type(err).__name__}: {safe_msg}"

    def _accesso_riuscito(self) -> None:
        """Clear blocked-access Repairs notices for this config entry."""
        if self.accesso_bloccato:
            _LOGGER.info("Accesso a E-Distribuzione di nuovo disponibile")
        self.accesso_bloccato = False
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_ACCESSO_BLOCCATO))
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id(ISSUE_APP_BLOCCATA))

    def registra_rinnovo_esterno(self, access_token: str) -> None:
        """Sync in-memory state after options flow refreshed the token."""
        self.ultimo_rinnovo = dt_util.now().isoformat()
        self._accesso_riuscito()
        self._api.update_token(access_token)
        self._registra_evento("refresh_opzioni", "ok", 0)

    async def _async_ensure_token(
        self, *, force_refresh: bool = False, failed_token: str | None = None
    ) -> None:
        """Ensure ApiClient holds a usable access token, serialized via _token_lock."""
        async with self._token_lock:
            access_token = self.entry.data.get(CONF_ACCESS_TOKEN)
            if access_token and not force_refresh:
                _LOGGER.debug("Uso l'access token salvato, senza rinnovarlo")
                self._api.update_token(access_token)
                return

            # Another coroutine already rotated the token while we waited for the lock.
            if (
                force_refresh
                and failed_token is not None
                and access_token
                and access_token != failed_token
            ):
                _LOGGER.debug("Token già rinnovato da un'altra richiesta concorrente")
                self._api.update_token(access_token)
                return

            _LOGGER.debug(
                "Rinnovo dell'accesso%s",
                " dopo un token rifiutato dal servizio dati" if force_refresh else "",
            )
            refresh_token = self.entry.data.get(CONF_REFRESH_TOKEN)
            if not refresh_token:
                err = TokenNonValido("refresh_token mancante nella configurazione")
                self._registra_errore_accesso(err)
                raise self._errore_rinnovo(err) from err

            client_id = client_id_della_entry(self.entry.data)
            t0 = time.monotonic()
            try:
                tokens = await self._auth.async_refresh_access_token(refresh_token, client_id)
            except AuthError as err:
                durata_ms = round((time.monotonic() - t0) * 1000)
                self._registra_errore_accesso(err)
                self._registra_evento("refresh", type(err).__name__, durata_ms)
                raise self._errore_rinnovo(err, client_id) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                durata_ms = round((time.monotonic() - t0) * 1000)
                self._registra_errore_accesso(err)
                self._registra_evento("refresh", type(err).__name__, durata_ms)
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="portale_non_raggiungibile",
                    translation_placeholders={"errore": type(err).__name__},
                ) from err

            durata_ms = round((time.monotonic() - t0) * 1000)
            _LOGGER.info("Accesso a E-Distribuzione rinnovato via %s", self._auth.host_attivo)
            ora_iso = dt_util.now().isoformat(timespec="seconds")
            self.ultimo_rinnovo = ora_iso
            self._accesso_riuscito()
            self._api.update_token(tokens.access_token)
            self._registra_evento("refresh", "ok", durata_ms, host=self._auth.host_attivo)

            new_data = {
                **self.entry.data,
                CONF_ACCESS_TOKEN: tokens.access_token,
                CONF_REFRESH_TOKEN: tokens.refresh_token,
                CONF_INSTANCE_URL: getattr(tokens, "instance_url", ""),
                CONF_TOKEN_ORIGINE: "refresh",
                CONF_TOKEN_AGGIORNATO_IL: ora_iso,
            }
            if new_data != self.entry.data:
                self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    def _errore_rinnovo(
        self, err: AuthError, client_id: str | None = None
    ) -> HomeAssistantError:
        """Convert a refresh AuthError into the matching HA error and Repairs notice."""
        if client_id is None:
            client_id = client_id_della_entry(self.entry.data)
        if isinstance(err, AccessoBloccato):
            self._segnala_blocco(ISSUE_ACCESSO_BLOCCATO)
            return UpdateFailed(translation_domain=DOMAIN, translation_key="accesso_bloccato")
        if (
            isinstance(err, (ClientNonValido, AppBloccata))
            and client_id == OAUTH_CLIENT_ID_PRECEDENTE
        ):
            # Entry still bound to the previous OAuth client: signing in again
            # migrates it to the current client while keeping PODs and history.
            _LOGGER.warning("Client OAuth precedente rifiutato, richiesta riautenticazione: %s", err)
            self._accesso_riuscito()
            return ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="client_precedente_rifiutato"
            )
        if isinstance(err, ClientNonValido):
            _LOGGER.warning("Client OAuth attuale rifiutato da E-Distribuzione: %s", err)
            return UpdateFailed(translation_domain=DOMAIN, translation_key="client_non_valido")
        if isinstance(err, AppBloccata):
            _LOGGER.warning("Rinnovo dell'accesso rifiutato: %s", err)
            self._segnala_blocco(ISSUE_APP_BLOCCATA)
            return UpdateFailed(translation_domain=DOMAIN, translation_key="app_bloccata")
        if isinstance(err, TokenNonValido):
            _LOGGER.warning("Accesso a E-Distribuzione scaduto: %s", err)
            return ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="accesso_scaduto"
            )
        _LOGGER.warning("Rinnovo dell'accesso fallito: %s", err)
        return UpdateFailed(
            translation_domain=DOMAIN,
            translation_key="refresh_token_fallito",
            translation_placeholders={"errore": str(err)},
        )

    async def _async_chiama_api(self, chiamata):
        """Run one ApiClient call, refreshing once on HTTP 401/403."""
        token_usato = self.entry.data.get(CONF_ACCESS_TOKEN)
        try:
            risultato = await chiamata()
        except ApiError as err:
            if err.status not in (401, 403):
                raise
            _LOGGER.info(
                "Il servizio dati ha rifiutato l'access token (HTTP %s): lo rinnovo", err.status
            )
            await self._async_ensure_token(force_refresh=True, failed_token=token_usato)
            risultato = await chiamata()
        self._accesso_riuscito()
        return risultato

    async def _async_get_daily_load_profile(
        self, pod: str, data_da: date, data_a: date, magnitude: str
    ) -> list[dict]:
        """Fetch a load curve, refreshing once if the data API returns 401/403."""
        pod_log = mask_pod(pod)
        _LOGGER.debug("POD %s: richiedo %s dal %s al %s", pod_log, magnitude, data_da, data_a)
        curva = await self._async_chiama_api(
            lambda: self._api.async_get_daily_load_profile(
                pod, data_da, data_a, magnitude=magnitude
            )
        )
        _LOGGER.debug("POD %s: %s, %d giorni nella risposta", pod_log, magnitude, len(curva))
        return curva

    async def _async_aggiorna_forniture(self, oggi: date) -> None:
        """Fetch technical supply metadata from getSupplies at most once per day."""
        if self._forniture_giorno == oggi:
            return
        try:
            supplies = await self._async_chiama_api(self._api.async_get_supplies)
        except ApiError as err:
            _LOGGER.warning("Aggiornamento metadati forniture fallito: %s", err)
            return
        self._forniture_giorno = oggi
        for item in supplies:
            if not isinstance(item, dict):
                continue
            pod_id = item.get("IdPod")
            if isinstance(pod_id, str) and pod_id in self.pods:
                self.forniture[pod_id] = estrai_fornitura(item)

    async def _async_aggiorna_mensili(self, pod: str, oggi: date) -> None:
        """Fetch official monthly totals/peaks (and opt-in reactive) via querymonthlytimeofuse."""
        ieri = oggi - timedelta(days=1)
        fine_mese_prec = oggi.replace(day=1) - timedelta(days=1)
        inizio = fine_mese_prec.replace(day=1)
        per_dir: dict[str, dict[str, Any] | None] = dict(self.mensili.get(pod) or {})
        successo = False
        for magnitude in self._magnitudes_attive(pod):
            try:
                righe = await self._async_chiama_api(
                    lambda m=magnitude: self._api.async_get_monthly_time_of_use(
                        pod, inizio, ieri, magnitude=m
                    )
                )
            except ApiError as err:
                _LOGGER.warning(
                    "POD %s: riepilogo mensile (%s) non disponibile: %s",
                    mask_pod(pod),
                    magnitude,
                    err,
                )
                continue
            per_dir[magnitude] = estrai_mensili(righe, oggi=oggi, magnitude_attesa=magnitude)
            successo = True

        if successo:
            self.mensili[pod] = per_dir
            self._mensili_giorno[pod] = oggi

        if not self.energia_reattiva_attiva:
            self.reattiva.pop(pod, None)
            return

        per_quadrante_corr: dict[str, dict[str, Any] | None] = {}
        per_quadrante_prec: dict[str, dict[str, Any] | None] = {}
        for magnitude in MAGNITUDE_REATTIVE:
            try:
                righe = await self._async_chiama_api(
                    lambda m=magnitude: self._api.async_get_monthly_time_of_use(
                        pod, inizio, ieri, magnitude=m
                    )
                )
            except ApiError as err:
                _LOGGER.warning(
                    "POD %s: riepilogo reattiva (%s) non disponibile: %s",
                    mask_pod(pod),
                    magnitude,
                    err,
                )
                continue
            estratto = estrai_mensili(righe, oggi=oggi, magnitude_attesa=magnitude)
            per_quadrante_corr[magnitude] = estratto["mese_corrente"]
            per_quadrante_prec[magnitude] = estratto["mese_precedente"]

        a1_mensili = per_dir.get(MAGNITUDE_PRELEVATA) or {}
        corr = calcola_reattiva_e_cos_phi(a1_mensili.get("mese_corrente"), per_quadrante_corr)
        prec = calcola_reattiva_e_cos_phi(a1_mensili.get("mese_precedente"), per_quadrante_prec)
        if corr is not None or prec is not None:
            self.reattiva[pod] = {"mese_corrente": corr, "mese_precedente": prec}

    async def _async_aggiorna_letture(self, pod: str, oggi: date) -> None:
        """Fetch latest official monthly meter reading at most once per month once published."""
        if self._letture_giorno.get(pod) == oggi:
            return
        primo_del_mese = oggi.replace(day=1).isoformat()
        esistente = self.letture_ufficiali.get(pod)
        if isinstance(esistente, dict) and (esistente.get("data_lettura") or "") >= primo_del_mese:
            self._letture_giorno[pod] = oggi
            return
        inizio = oggi - timedelta(days=FINESTRA_LETTURE_GIORNI)
        try:
            righe = await self._async_chiama_api(
                lambda: self._api.async_get_reading(pod, inizio, oggi)
            )
        except ApiError as err:
            _LOGGER.warning(
                "POD %s: letture ufficiali non disponibili: %s", mask_pod(pod), err
            )
            return
        self._letture_giorno[pod] = oggi
        estratta = estrai_ultima_lettura(righe)
        if estratta is not None:
            self.letture_ufficiali[pod] = estratta

    def _servono_ausiliari_avvio(self) -> bool:
        """True until the startup auxiliary attempt runs without an access/refresh error."""
        if self._ausiliari_avvio_tentati:
            return False
        if not self.forniture and self._forniture_giorno is None:
            return True
        for pod in self.pods:
            if pod not in self.mensili and pod not in self._mensili_giorno:
                return True
            if pod not in self.letture_ufficiali and pod not in self._letture_giorno:
                return True
        return False

    async def _async_aggiorna_ausiliari(
        self, pod_aggiornati: set[str], *, includi_forniture: bool = True
    ) -> None:
        """Best-effort refresh of supply metadata, monthly TOU and official readings.

        Stops immediately on an access/refresh UpdateFailed so a blocked token
        endpoint is never hammered repeatedly across magnitudes and PODs. Marks
        startup auxiliaries as attempted once the run finishes without an access
        error, preventing hourly retries outside the evening window on persistent
        API errors.
        """
        if not self._api.has_token:
            return
        oggi = dt_util.now(FUSO_ITALIA).date()
        try:
            if includi_forniture and (
                pod_aggiornati or (not self.forniture and self._forniture_giorno is None)
            ):
                await self._async_aggiorna_forniture(oggi)
            for pod in self.pods:
                if pod in pod_aggiornati or (
                    pod not in self.mensili and pod not in self._mensili_giorno
                ):
                    await self._async_aggiorna_mensili(pod, oggi)
                if pod in pod_aggiornati or (
                    pod not in self.letture_ufficiali and pod not in self._letture_giorno
                ):
                    await self._async_aggiorna_letture(pod, oggi)
            self._ausiliari_avvio_tentati = True
        except UpdateFailed as err:
            _LOGGER.warning(
                "Aggiornamento dati ausiliari interrotto per errore di accesso: %s", err
            )

    async def async_ricostruisci_serie_mancanti(self) -> None:
        """Rebuild missing or lagging statistics from raw_storage without network calls."""
        if "recorder" not in self.hass.config.components:
            return
        for pod in self.pods:
            for immessa in (False, True):
                try:
                    await async_ricostruisci_serie_mancanti(
                        self.hass, pod, immessa=immessa, nome=self._nome_serie(pod, immessa)
                    )
                except Exception:  # noqa: BLE001 - repair step must not block setup
                    _LOGGER.exception(
                        "POD %s: ricostruzione delle serie dal database locale fallita",
                        mask_pod(pod),
                    )

    # ------------------------------------------------------------------
    # Diagnostic sensors
    # ------------------------------------------------------------------

    async def async_aggiorna_riepilogo(self) -> dict[str, dict]:
        """Refresh diagnostic sensor values using a single executor job."""
        db_path = raw_storage.percorso_predefinito(self.hass)

        def _leggi_locale():
            totali = raw_storage.riepilogo_db(db_path, self.pods, MAGNITUDE_TUTTE, FUSO_ITALIA)
            picchi = raw_storage.picchi_ultimo_giorno_db(
                db_path, self.pods, MAGNITUDE_TUTTE, FUSO_ITALIA
            )
            return totali, picchi

        try:
            per_pod_db, picchi_db = await self.hass.async_add_executor_job(_leggi_locale)
        except Exception:  # noqa: BLE001 - diagnostics only, never fail an update
            _LOGGER.exception("Valori dei sensori diagnostici non aggiornati (database locale)")
            return self.riepilogo

        riepilogo: dict[str, dict] = {}
        recorder_attivo = "recorder" in self.hass.config.components
        for pod in self.pods:
            try:
                ultima_data = (
                    await async_get_ultima_data_disponibile(self.hass, pod)
                    if recorder_attivo
                    else None
                )
            except Exception:  # noqa: BLE001 - diagnostics only, never fail an update
                _LOGGER.exception(
                    "POD %s: valori dei sensori diagnostici non aggiornati", mask_pod(pod)
                )
                riepilogo[pod] = self.riepilogo.get(pod, {})
                continue
            ultimi_giorni = per_pod_db.get(pod) or dict.fromkeys(MAGNITUDE_TUTTE)
            picchi_pod = picchi_db.get(pod) or dict.fromkeys(MAGNITUDE_TUTTE)
            riepilogo[pod] = _riepilogo_pod(ultima_data, ultimi_giorni, picchi_pod)
        self.riepilogo = riepilogo
        return riepilogo

    # ------------------------------------------------------------------
    # Configurable hour + retry queue per POD
    # ------------------------------------------------------------------

    @property
    def _ora_richiesta(self) -> int:
        valore = self.entry.options.get(CONF_ORA_RICHIESTA)
        if valore is None:
            return ORA_MINIMA_RICHIESTA
        try:
            ora = int(float(valore))
        except (TypeError, ValueError):
            _LOGGER.warning(
                "Ora richiesta non valida nelle opzioni (%r): uso le %d:00",
                valore,
                ORA_MINIMA_RICHIESTA,
            )
            return ORA_MINIMA_RICHIESTA
        if not 0 <= ora <= 23:
            _LOGGER.warning(
                "Ora richiesta fuori intervallo (%r): uso le %d:00", valore, ORA_MINIMA_RICHIESTA
            )
            return ORA_MINIMA_RICHIESTA
        return ora

    def _leggi_code(self) -> dict[str, dict[str, date]]:
        """Per-POD retry queues: {pod: {iso_day: first_queued_date}}."""
        grezzo = self.entry.data.get(CONF_GIORNI_DA_RIPROVARE) or {}
        oggi = dt_util.now(FUSO_ITALIA).date()

        def _con_date(coda: dict) -> dict[str, date]:
            risultato: dict[str, date] = {}
            for giorno, valore in coda.items():
                try:
                    date.fromisoformat(giorno)
                except (TypeError, ValueError):
                    continue
                try:
                    risultato[giorno] = date.fromisoformat(valore)
                except (TypeError, ValueError):
                    risultato[giorno] = oggi
            return risultato

        return {pod: _con_date(coda) for pod, coda in grezzo.items() if isinstance(coda, dict)}

    def _scrivi_code(self, code: dict[str, dict[str, date]]) -> None:
        """Persist queues in a single write, expiring old entries and capping length."""
        oggi = dt_util.now(FUSO_ITALIA).date()
        pulite: dict[str, dict[str, str]] = {}
        for pod, coda in code.items():
            pulita = {
                giorno: da
                for giorno, da in coda.items()
                if (oggi - da).days < ABBANDONO_CODA_DOPO_GIORNI
            }
            abbandonati = set(coda) - set(pulita)
            if abbandonati:
                _LOGGER.warning(
                    "POD %s: giorni abbandonati dopo %d giorni in coda senza dati da "
                    "E-Distribuzione: %s. Se servono, richiedili con l'azione "
                    "edistribuzione.recupera_storico.",
                    mask_pod(pod),
                    ABBANDONO_CODA_DOPO_GIORNI,
                    ", ".join(sorted(abbandonati)),
                )

            if len(pulita) > MAX_GIORNI_IN_CODA:
                tenuti = sorted(pulita, reverse=True)[:MAX_GIORNI_IN_CODA]
                scartati = set(pulita) - set(tenuti)
                _LOGGER.warning(
                    "POD %s: coda dei giorni da riprovare oltre %d elementi: scarto i "
                    "più vecchi (%s)",
                    mask_pod(pod),
                    MAX_GIORNI_IN_CODA,
                    ", ".join(sorted(scartati)),
                )
                pulita = {g: pulita[g] for g in tenuti}

            if pulita:
                pulite[pod] = {g: da.isoformat() for g, da in pulita.items()}

        if pulite != self.entry.data.get(CONF_GIORNI_DA_RIPROVARE):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_GIORNI_DA_RIPROVARE: pulite},
            )

    def _accoda_giorni(self, pod: str, giorni: list[date]) -> None:
        """Queue multiple days in one config-entry write."""
        if not giorni:
            return
        code = self._leggi_code()
        coda = code.setdefault(pod, {})
        oggi = dt_util.now(FUSO_ITALIA).date()
        pod_log = mask_pod(pod)
        for giorno in giorni:
            chiave = giorno.isoformat()
            if chiave in coda:
                _LOGGER.info(
                    "POD %s: giorno %s ancora senza dati da E-Distribuzione, in coda da "
                    "%d giorni (max %d)",
                    pod_log,
                    chiave,
                    (oggi - coda[chiave]).days,
                    ABBANDONO_CODA_DOPO_GIORNI,
                )
            else:
                coda[chiave] = oggi
                _LOGGER.info(
                    "POD %s: giorno %s senza dati da E-Distribuzione, messo in coda per "
                    "riprovare (max %d giorni)",
                    pod_log,
                    chiave,
                    ABBANDONO_CODA_DOPO_GIORNI,
                )
        self._scrivi_code(code)

    def _accoda_giorno(self, pod: str, giorno: date) -> None:
        self._accoda_giorni(pod, [giorno])

    def _rimuovi_dalla_coda(self, pod: str, giorni: list[date]) -> None:
        code = self._leggi_code()
        coda = code.get(pod, {})
        rimossi = [g.isoformat() for g in giorni if g.isoformat() in coda]
        if not rimossi:
            return
        for chiave in rimossi:
            del coda[chiave]
        _LOGGER.info(
            "POD %s: dati ricevuti per %s, rimossi dalla coda", mask_pod(pod), ", ".join(rimossi)
        )
        self._scrivi_code(code)

    def _pod_ha_immessa_storica(self, pod: str) -> bool:
        """True if this POD is known to produce/inject energy."""
        if self.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            return True
        immessa = (self.riepilogo.get(pod) or {}).get("ultimo_giorno", {}).get(MAGNITUDE_IMMESSA)
        return bool(immessa and (immessa.get("kwh") or 0) > 0)

    def _pod_supporta_immissione(self, pod: str) -> bool:
        """True unless getSupplies explicitly confirmed HasPlant=False on a non-production POD."""
        if self._pod_ha_immessa_storica(pod):
            return True
        fornitura = self.forniture.get(pod)
        return not (isinstance(fornitura, dict) and fornitura.get("ha_impianto") is False)

    def _magnitudes_attive(self, pod: str) -> tuple[str, ...]:
        """Active energy magnitudes to request for this POD."""
        if self._pod_supporta_immissione(pod):
            return MAGNITUDE_TUTTE
        return (MAGNITUDE_PRELEVATA,)

    async def async_stato_prossima_richiesta(self, pod: str) -> dict[str, Any]:
        """Read-only summary of why/when the next automatic fetch will run for diagnostics."""
        adesso = dt_util.now(FUSO_ITALIA)
        oggi = adesso.date()
        atteso = oggi - timedelta(days=RITARDO_DATI_GIORNI)
        inizio_ricontrollo = atteso - timedelta(days=GIORNI_RICONTROLLO - 1)
        ultima_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)
        coda = self._leggi_code().get(pod, {})
        arretrati = sorted(
            date.fromisoformat(g) for g in coda if date.fromisoformat(g) < inizio_ricontrollo
        )

        if ultima_disponibile is None:
            if pod not in self._bootstrap_tentato:
                stato = "pending_bootstrap"
            elif self._ultimo_tentativo_senza_dati.get(pod) == oggi:
                stato = "no_data_already_tried_today"
            elif adesso.hour < self._ora_richiesta:
                stato = "waiting_for_hour"
            else:
                stato = "ready"
        elif adesso.hour < self._ora_richiesta:
            stato = "waiting_for_hour"
        elif ultima_disponibile >= atteso:
            if arretrati and self._ultimo_tentativo_coda.get(pod) != oggi:
                stato = "queued_backlog_ready"
            else:
                stato = "up_to_date"
        else:
            stato = "ready"

        return {
            "stato": stato,
            "giorno_atteso": atteso.isoformat(),
            "giorni_in_coda": len(coda),
            "arretrati_fuori_finestra": len(arretrati),
        }

    async def _prossima_richiesta(self, pod: str) -> tuple[date, date] | None:
        """Compute the date range to request for a POD in this cycle, or None."""
        adesso = dt_util.now(FUSO_ITALIA)
        oggi = adesso.date()
        atteso = oggi - timedelta(days=RITARDO_DATI_GIORNI)
        inizio_ricontrollo = atteso - timedelta(days=GIORNI_RICONTROLLO - 1)

        ultima_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)
        code = self._leggi_code()
        coda = code.get(pod, {})
        arretrati = sorted(
            date.fromisoformat(g) for g in coda if date.fromisoformat(g) < inizio_ricontrollo
        )

        if ultima_disponibile is None:
            if pod not in self._bootstrap_tentato:
                self._bootstrap_tentato.add(pod)
                self._ultimo_tentativo_senza_dati[pod] = oggi
                _LOGGER.info(
                    "POD %s: nessun dato ancora importato, richiedo subito gli ultimi %d "
                    "giorni per verificare POD e token. Le richieste successive partiranno "
                    "dopo le %d:00 (modificabile dalle opzioni); per lo storico usa l'azione "
                    "edistribuzione.recupera_storico.",
                    mask_pod(pod),
                    GIORNI_RICONTROLLO,
                    self._ora_richiesta,
                )
            else:
                # Avoid hourly 24/7 polling for meters that have no published curves yet.
                if (
                    adesso.hour < self._ora_richiesta
                    or self._ultimo_tentativo_senza_dati.get(pod) == oggi
                ):
                    return None
                self._ultimo_tentativo_senza_dati[pod] = oggi
        else:
            if adesso.hour < self._ora_richiesta:
                return None
            if ultima_disponibile >= atteso:
                if arretrati and self._ultimo_tentativo_coda.get(pod) != oggi:
                    self._ultimo_tentativo_coda[pod] = oggi
                    inizio = max(arretrati[0], atteso - timedelta(days=150))
                    return inizio, atteso
                return None

        inizio = (
            max(arretrati[0], atteso - timedelta(days=150)) if arretrati else inizio_ricontrollo
        )
        return inizio, atteso

    # ------------------------------------------------------------------
    # Fetch + import shared by polling and history action
    # ------------------------------------------------------------------

    async def _async_importa_periodo(self, pod: str, data_da: date, data_a: date) -> dict[str, dict]:
        """Request and import active energy directions for a POD, chunking over 180 days."""
        risultati: dict[str, dict] = {}
        pod_log = mask_pod(pod)
        chunks = _suddividi_intervallo(data_da, data_a, CHUNK_GIORNI_STORICO)
        for magnitude in self._magnitudes_attive(pod):
            giorni_totali: set[date] = set()
            for c_da, c_a in chunks:
                curva = await self._async_get_daily_load_profile(
                    pod, c_da, c_a, magnitude=magnitude
                )
                giorni_ricevuti = _giorni_ricevuti(curva)
                _LOGGER.debug(
                    "POD %s, magnitude %s, %s - %s: %d giorni nella risposta, %d con campioni",
                    pod_log,
                    magnitude,
                    c_da,
                    c_a,
                    len(curva),
                    len(giorni_ricevuti),
                )
                if not _curva_ha_dati(curva):
                    continue
                if not _magnitude_onorata(curva, magnitude):
                    _LOGGER.warning(
                        "POD %s: risposta per magnitude=%r con energyType diverso da quello "
                        "richiesto (probabile parametro ignorato dal server): scartata",
                        pod_log,
                        magnitude,
                    )
                    continue

                immessa = magnitude == MAGNITUDE_IMMESSA
                await async_import_curva_giornaliera(
                    self.hass, pod, curva, immessa=immessa, nome=self._nome_serie(pod, immessa)
                )
                giorni_totali.update(giorni_ricevuti)
            if giorni_totali:
                risultati[magnitude] = {"giorni_ricevuti": giorni_totali}
        return risultati

    # ------------------------------------------------------------------
    # Automatic polling cycle
    # ------------------------------------------------------------------

    def _registra_richiesta(
        self,
        pod: str,
        data_da: date,
        data_a: date,
        *,
        risultati: dict[str, dict] | None = None,
        errore: str | None = None,
    ) -> None:
        richiesta: dict = {
            "quando": dt_util.now().isoformat(timespec="seconds"),
            "periodo": f"{data_da} - {data_a}",
        }
        if errore is not None:
            richiesta["errore"] = errore
        else:
            richiesta["giorni_ricevuti"] = {
                magnitude: len((risultati or {}).get(magnitude, {}).get("giorni_ricevuti", ()))
                for magnitude in MAGNITUDE_TUTTE
            }
        self.ultime_richieste[pod] = richiesta

    async def _async_update_data(self) -> dict:
        t0 = time.monotonic()
        async with self._import_lock:
            oggi = dt_util.now(FUSO_ITALIA).date()
            richieste_per_pod: list[tuple[str, date, date]] = []
            for pod in self.pods:
                richiesta = await self._prossima_richiesta(pod)
                if richiesta is not None:
                    richieste_per_pod.append((pod, richiesta[0], richiesta[1]))

            servono_ausiliari = bool(richieste_per_pod) or self._servono_ausiliari_avvio()
            if richieste_per_pod or servono_ausiliari:
                await self._async_ensure_token()

            if richieste_per_pod and self._api.has_token:
                await self._async_aggiorna_forniture(oggi)

            errori: dict[str, str] = {}
            richiesti_count = len(richieste_per_pod)
            pod_aggiornati: set[str] = set()
            for pod, data_da, data_a in richieste_per_pod:
                try:
                    risultati = await self._async_importa_periodo(pod, data_da, data_a)
                except ApiError as err:
                    _LOGGER.warning(
                        "POD %s: errore recuperando il periodo %s - %s: %s",
                        mask_pod(pod),
                        data_da,
                        data_a,
                        err,
                    )
                    self._accoda_giorni(pod, _giorni_nel_periodo(data_da, data_a))
                    errori[pod] = _descrivi_errore_api(err, self.lingua)
                    self._registra_richiesta(pod, data_da, data_a, errore=errori[pod])
                    continue
                self._registra_richiesta(pod, data_da, data_a, risultati=risultati)
                pod_aggiornati.add(pod)

                ricevuti_prelevata = risultati.get(MAGNITUDE_PRELEVATA, {}).get(
                    "giorni_ricevuti", set()
                )
                ricevuti_immessa = risultati.get(MAGNITUDE_IMMESSA, {}).get(
                    "giorni_ricevuti", set()
                )
                if self._pod_ha_immessa_storica(pod):
                    ricevuti_completi = ricevuti_prelevata & ricevuti_immessa
                else:
                    ricevuti_completi = ricevuti_prelevata | ricevuti_immessa

                richiesti = _giorni_nel_periodo(data_da, data_a)
                if ricevuti_completi:
                    self._rimuovi_dalla_coda(pod, [g for g in richiesti if g in ricevuti_completi])
                mancanti = [g for g in richiesti if g not in ricevuti_completi]
                if mancanti:
                    self._accoda_giorni(pod, mancanti)

                if self._pod_supporta_immissione(pod) and ricevuti_prelevata != ricevuti_immessa:
                    _LOGGER.info(
                        "POD %s: prelevata e immessa non coprono esattamente gli stessi "
                        "giorni in questo ciclo (prelevata=%s, immessa=%s)",
                        mask_pod(pod),
                        sorted(g.isoformat() for g in ricevuti_prelevata),
                        sorted(g.isoformat() for g in ricevuti_immessa),
                    )

            if servono_ausiliari:
                await self._async_aggiorna_ausiliari(
                    pod_aggiornati, includi_forniture=not richieste_per_pod
                )
            riepilogo = await self.async_aggiorna_riepilogo()
            durata_ms = round((time.monotonic() - t0) * 1000)
            if richiesti_count or errori:
                self._registra_evento(
                    "polling",
                    "error" if errori else "ok",
                    durata_ms,
                    pod_richiesti=richiesti_count,
                    pod_in_errore=len(errori),
                )
            if errori:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="errore_import_pod",
                    translation_placeholders={
                        "pod": ", ".join(errori),
                        "errore": "; ".join(dict.fromkeys(errori.values())),
                    },
                )
            return {"by_pod": riepilogo}

    # ------------------------------------------------------------------
    # Manual history fetch (edistribuzione.recupera_storico)
    # ------------------------------------------------------------------

    async def async_recupera_storico(
        self, data_da: date, data_a: date, pod: str | None = None
    ) -> dict[str, Any]:
        """Fetch and import both directions for [data_da, data_a] and return a summary."""
        if pod is not None and pod not in self.pods:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="pod_non_configurato",
                translation_placeholders={"pod": pod, "pods": ", ".join(self.pods)},
            )
        pod_da_recuperare = [pod] if pod else list(self.pods)

        self._verifica_periodo(data_da, data_a)

        t0 = time.monotonic()
        async with self._import_lock:
            try:
                await self._async_ensure_token()
            except ConfigEntryAuthFailed:
                self.entry.async_start_reauth(self.hass)
                raise

            _LOGGER.info(
                "Recupero storico avviato: %s - %s (POD: %s)",
                data_da,
                data_a,
                ", ".join(mask_pod(p) for p in pod_da_recuperare),
            )

            fallimenti: list[str] = []
            report_pod: dict[str, dict[str, Any]] = {}
            pod_con_dati = 0
            giorni_attesi = _giorni_nel_periodo(data_da, data_a)

            try:
                for pod_corrente in pod_da_recuperare:
                    pod_log = mask_pod(pod_corrente)
                    try:
                        risultati = await self._async_importa_periodo(pod_corrente, data_da, data_a)
                    except ConfigEntryAuthFailed:
                        self.entry.async_start_reauth(self.hass)
                        raise
                    except ApiError as err:
                        _LOGGER.warning(
                            "POD %s: errore recuperando il periodo %s - %s: %s",
                            pod_log,
                            data_da,
                            data_a,
                            err,
                        )
                        desc = _descrivi_errore_api(err, self.lingua)
                        fallimenti.append(f"{pod_corrente}: {desc}")
                        report_pod[pod_corrente] = {"errore": desc}
                        continue

                    ricevuti_prelevata = risultati.get(MAGNITUDE_PRELEVATA, {}).get(
                        "giorni_ricevuti", set()
                    )
                    ricevuti_immessa = risultati.get(MAGNITUDE_IMMESSA, {}).get(
                        "giorni_ricevuti", set()
                    )
                    ricevuti_unione = ricevuti_prelevata | ricevuti_immessa

                    if not ricevuti_unione:
                        _LOGGER.warning(
                            "POD %s: nessun dato (né prelevata né immessa) per il periodo %s - %s",
                            pod_log,
                            data_da,
                            data_a,
                        )
                        desc = testo(self.lingua, "nessun_dato_pod")
                        fallimenti.append(f"{pod_corrente}: {desc}")
                        report_pod[pod_corrente] = {
                            "giorni_attesi": len(giorni_attesi),
                            "giorni_ricevuti": 0,
                            "prelevata": 0,
                            "immessa": 0,
                            "mancanti": [g.isoformat() for g in giorni_attesi],
                        }
                        continue

                    self._rimuovi_dalla_coda(
                        pod_corrente, [g for g in giorni_attesi if g in ricevuti_unione]
                    )
                    pod_con_dati += 1

                    mancanti = [g for g in giorni_attesi if g not in ricevuti_unione]
                    report_pod[pod_corrente] = {
                        "giorni_attesi": len(giorni_attesi),
                        "giorni_ricevuti": len(ricevuti_unione),
                        "prelevata": len(ricevuti_prelevata),
                        "immessa": len(ricevuti_immessa),
                        "mancanti": [g.isoformat() for g in mancanti],
                    }

                    dettaglio_mancanti = ""
                    if mancanti:
                        elenco = ", ".join(g.isoformat() for g in mancanti[:10])
                        if len(mancanti) > 10:
                            elenco += f", e altri {len(mancanti) - 10}"
                        dettaglio_mancanti = f" ({elenco})"

                    descrizione_direzioni = (
                        "unione delle due direzioni"
                        if self._pod_supporta_immissione(pod_corrente)
                        else "solo prelevata"
                    )
                    _LOGGER.info(
                        "POD %s: recupero storico %s - %s completato, %d/%d giorni ricevuti "
                        "(%s)%s",
                        pod_log,
                        data_da,
                        data_a,
                        len(ricevuti_unione),
                        len(giorni_attesi),
                        descrizione_direzioni,
                        dettaglio_mancanti,
                    )
            finally:
                if pod_con_dati > 0:
                    await self.async_aggiorna_riepilogo()
                    self.async_update_listeners()
                durata_ms = round((time.monotonic() - t0) * 1000)
                self._registra_evento(
                    "storico",
                    "ok" if pod_con_dati > 0 else "error",
                    durata_ms,
                    periodo=f"{data_da} - {data_a}",
                    pod_con_dati=pod_con_dati,
                    pod_falliti=len(fallimenti),
                )

            if pod_con_dati == 0:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="nessun_dato_importato",
                    translation_placeholders={
                        "data_da": str(data_da),
                        "data_a": str(data_a),
                        "dettagli": "; ".join(fallimenti),
                    },
                )

            return {
                "data_da": data_da.isoformat(),
                "data_a": data_a.isoformat(),
                "pod": report_pod,
            }

    @staticmethod
    def _verifica_periodo(data_da: date, data_a: date) -> None:
        """Validate requested range using Italian calendar dates."""
        ultimo_utile = dt_util.now(FUSO_ITALIA).date() - timedelta(days=RITARDO_DATI_GIORNI)

        def errore(chiave: str, **segnaposto: str) -> ServiceValidationError:
            return ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=chiave,
                translation_placeholders={
                    "data_da": data_da.isoformat(),
                    "data_a": data_a.isoformat(),
                    "ultimo_utile": ultimo_utile.isoformat(),
                    **segnaposto,
                },
            )

        if data_da > data_a:
            raise errore("date_invertite")
        if data_da > ultimo_utile:
            raise errore("data_inizio_troppo_recente")
        if data_a > ultimo_utile:
            raise errore("data_fine_troppo_recente")

        giorni_totali = (data_a - data_da).days + 1
        if giorni_totali > MAX_GIORNI_RECUPERO_STORICO:
            fine_prima_parte = data_da + timedelta(days=MAX_GIORNI_RECUPERO_STORICO - 1)
            raise errore(
                "intervallo_troppo_ampio",
                giorni=str(giorni_totali),
                massimo=str(MAX_GIORNI_RECUPERO_STORICO),
                fine_prima_parte=fine_prima_parte.isoformat(),
            )

    @property
    def api(self) -> ApiClient:
        """Expose the API client for on-demand calls."""
        return self._api
