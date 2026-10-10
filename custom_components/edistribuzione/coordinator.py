"""DataUpdateCoordinator for E-Distribuzione.

Supports multiple PODs on the same config entry (same credential, same
authenticated account). Each cycle, for each POD, when it is time (queue +
hour, at most once a day): it requests BOTH energy directions (consumption and
injection, see MAGNITUDE_TUTTE in const.py) for the last GIORNI_RICONTROLLO
days (not only the most recent, to recheck any corrections E-Distribuzione made
to days already imported), in a single request per direction. Writing into the
external statistics always recomputes the whole series from the 15-minute raw
storage (see statistics.py), so a correction also fixes the following cumulative
sums automatically.

A day leaves the retry queue if at least one of the two directions returned it:
a POD without one of the two (e.g. a regular meter that never measures
injection) must not stay in the queue forever waiting for data that will not
arrive.

async_recupera_storico takes an optional 'pod' parameter: if omitted, it
fetches the history for ALL the PODs of the entry.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiClient, ApiError
from .auth import AccessoBloccato, AuthClient, AuthError
from .const import (
    ABBANDONO_CODA_DOPO_GIORNI,
    CONF_DATA_INSTALLAZIONE,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
    GIORNI_RICONTROLLO,
    ISSUE_ACCESSO_BLOCCATO,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    MAGNITUDE_TUTTE,
    MAX_GIORNI_IN_CODA,
    MAX_GIORNI_RECUPERO_STORICO,
    ORA_MINIMA_RICHIESTA,
    RITARDO_DATI_GIORNI,
    TIPO_POD_DEFAULT,
    TIPO_POD_PRODUZIONE,
)
from .statistics import async_get_ultima_data_disponibile, async_import_curva_giornaliera
from .testi import testo

_LOGGER = logging.getLogger(__name__)


def _giorni_nel_periodo(data_da: date, data_a: date) -> list[date]:
    """List of days in the range, both ends included."""
    giorni, cursore = [], data_da
    while cursore <= data_a:
        giorni.append(cursore)
        cursore += timedelta(days=1)
    return giorni


def _giorni_ricevuti(curva: list[dict]) -> set[date]:
    """Days actually present in the response (sampleDate field, YYYYMMDD
    format), dropping those without samples."""
    giorni: set[date] = set()
    for elemento in curva:
        readings = elemento.get("readings", {})
        if not readings.get("sampleValues"):
            continue
        grezzo = readings.get("sampleDate")
        try:
            giorni.add(date(int(grezzo[:4]), int(grezzo[4:6]), int(grezzo[6:8])))
        except (TypeError, ValueError, IndexError):
            _LOGGER.warning("sampleDate non interpretabile, giorno ignorato: %r", grezzo)
    return giorni


def _kwh_del_giorno(curva: list[dict], giorno: date) -> float | None:
    """Total kWh of a single day inside a multi-day response."""
    atteso = giorno.strftime("%Y%m%d")
    for elemento in curva:
        readings = elemento.get("readings", {})
        if readings.get("sampleDate") == atteso:
            return sum(float(c.get("val", 0)) for c in readings.get("sampleValues", []))
    return None


def _curva_ha_dati(curva: list[dict]) -> bool:
    """True if the async_get_daily_load_profile response really contains
    samples, not just an empty structure."""
    return bool(curva) and bool(curva[0].get("readings", {}).get("sampleValues"))


def _magnitude_onorata(curva: list[dict], magnitude_richiesta: str) -> bool:
    """True if the server actually served the requested magnitude.

    'energyType' in the response echoes back the magnitude actually served: if
    the server silently ignores an unknown parameter and replies with
    consumption anyway, this field reveals it. Without this check, an ignored
    magnitude would look like a success and end up duplicating consumption into
    the injection series - the main risk of this integration until
    MAGNITUDE_IMMESSA is confirmed.

    An element without 'energyType' does not fail the check: it is assumed
    honoured rather than discarding good data for a missing field.
    """
    for elemento in curva:
        energy_type = elemento.get("readings", {}).get("energyType")
        if energy_type is not None and energy_type != magnitude_richiesta:
            return False
    return True


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
        # True while the token refresh hits E-Distribuzione's anti-bot check
        # (see _async_ensure_token and async_setup_entry).
        self.accesso_bloccato = False
        session = async_get_clientsession(hass)
        self._auth = AuthClient(session)
        self._api = ApiClient(session, access_token="")

    def tipo_pod(self, pod: str) -> str:
        """Role assigned by the user to the POD (exchange/production), from the
        config entry options. Only affects the visible labels (see
        _nome_serie), never which directions are requested."""
        tipi = self.entry.options.get(CONF_TIPO_POD, {})
        return tipi.get(pod, TIPO_POD_DEFAULT)

    @property
    def lingua(self) -> str:
        """Server language, for the texts HA does not translate (see testi.py)."""
        return self.hass.config.language

    def _nome_serie(self, pod: str, immessa: bool) -> str:
        """External statistic label per POD/direction, depending on the role
        chosen by the user. The name ends up in the Recorder metadata, with no
        translation: it follows the server language and updates on the first
        import after a language change."""
        if self.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            chiave = "serie_produzione" if immessa else "serie_prelievo_tecnico"
        else:
            chiave = "serie_immissione" if immessa else "serie_prelievo"
        return f"E-Distribuzione {pod} - {testo(self.lingua, chiave)}"

    async def _async_ensure_token(self) -> None:
        refresh_token = self.entry.data[CONF_REFRESH_TOKEN]
        try:
            tokens = await self._auth.async_refresh_access_token(refresh_token)
        except AccessoBloccato as err:
            # The refresh_token stays valid and saved: we retry on the next
            # cycle, and the Repairs notice explains why the data is stuck
            # instead of leaving it to be inferred from the logs.
            self.accesso_bloccato = True
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                ISSUE_ACCESSO_BLOCCATO,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_ACCESSO_BLOCCATO,
            )
            raise UpdateFailed(
                translation_domain=DOMAIN, translation_key="accesso_bloccato"
            ) from err
        except AuthError as err:
            # A failed refresh almost certainly means the refresh_token was
            # revoked (password change, Enel-side session cleanup, ...) and the
            # user must log in again through the config_flow reauth.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="refresh_token_fallito",
                translation_placeholders={"errore": str(err)},
            ) from err

        # Access possible again: the notice, if any, is no longer needed.
        self.accesso_bloccato = False
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ACCESSO_BLOCCATO)
        self._api.update_token(tokens.access_token)

        if tokens.refresh_token != refresh_token:
            new_data = dict(self.entry.data)
            new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # Configurable hour + queue of days to retry, PER POD
    # ------------------------------------------------------------------

    @property
    def _ora_richiesta(self) -> int:
        """Hour (local) from which to request the previous day's curve.
        Configurable from the options: if requests often come back empty it is
        worth moving it later."""
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
        """Queues of days to retry, ONE PER POD:
        {pod: {ISO day: date first queued}}.

        The first-queued date acts as a timer: a day is dropped after
        ABBANDONO_CODA_DOPO_GIORNI regardless of the number of attempts (see
        _scrivi_code).
        """
        grezzo = self.entry.data.get(CONF_GIORNI_DA_RIPROVARE) or {}
        oggi = dt_util.now().date()

        def _con_date(coda: dict) -> dict[str, date]:
            risultato: dict[str, date] = {}
            for giorno, valore in coda.items():
                try:
                    risultato[giorno] = date.fromisoformat(valore)
                except (TypeError, ValueError):
                    risultato[giorno] = oggi
            return risultato

        return {pod: _con_date(coda) for pod, coda in grezzo.items()}

    def _scrivi_code(self, code: dict[str, dict[str, date]]) -> None:
        """Save the queues, dropping days that are too old and capping their number, per POD."""
        oggi = dt_util.now().date()
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
                    pod,
                    ABBANDONO_CODA_DOPO_GIORNI,
                    ", ".join(sorted(abbandonati)),
                )

            if len(pulita) > MAX_GIORNI_IN_CODA:
                tenuti = sorted(pulita, reverse=True)[:MAX_GIORNI_IN_CODA]
                scartati = set(pulita) - set(tenuti)
                _LOGGER.warning(
                    "POD %s: coda dei giorni da riprovare oltre %d elementi: scarto i "
                    "più vecchi (%s)",
                    pod,
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

    def _accoda_giorno(self, pod: str, giorno: date) -> None:
        code = self._leggi_code()
        coda = code.setdefault(pod, {})
        chiave = giorno.isoformat()
        if chiave in coda:
            _LOGGER.info(
                "POD %s: giorno %s ancora senza dati da E-Distribuzione, in coda da "
                "%d giorni (max %d)",
                pod,
                chiave,
                (dt_util.now().date() - coda[chiave]).days,
                ABBANDONO_CODA_DOPO_GIORNI,
            )
        else:
            coda[chiave] = dt_util.now().date()
            _LOGGER.info(
                "POD %s: giorno %s senza dati da E-Distribuzione, messo in coda per "
                "riprovare (max %d giorni)",
                pod,
                chiave,
                ABBANDONO_CODA_DOPO_GIORNI,
            )
        self._scrivi_code(code)

    def _rimuovi_dalla_coda(self, pod: str, giorni: list[date]) -> None:
        code = self._leggi_code()
        coda = code.get(pod, {})
        rimossi = [g.isoformat() for g in giorni if g.isoformat() in coda]
        if not rimossi:
            return
        for chiave in rimossi:
            del coda[chiave]
        _LOGGER.info("POD %s: dati ricevuti per %s, rimossi dalla coda", pod, ", ".join(rimossi))
        self._scrivi_code(code)

    async def _prossima_richiesta(self, pod: str) -> tuple[date, date] | None:
        """Decide which range to request for this POD in this cycle.

        A POD with NO data imported yet (the entry's first start, or a POD added
        later from the options) requests immediately, regardless of the
        configured hour - this checks right away that the POD and token are
        valid, instead of finding out only in the evening. On later cycles it
        waits for the configured hour, then at most once a day (the same
        'atteso' stays unchanged until the day changes) requests in a SINGLE
        request the last GIORNI_RICONTROLLO days up to the expected day - not
        only the most recent: E-Distribuzione may correct a day already
        published, and without this periodic recheck that correction would never
        be seen automatically (it stays recoverable by hand with
        recupera_storico).

        If there are backlog days OLDER than the recheck window (stuck in the
        queue from a previous error), the range widens backwards to include
        them, still in a single request.

        PAST BUG (fixed here): the previous version made the immediate fetch
        depend on CONF_DATA_INSTALLAZIONE, a flag SHARED by the whole config
        entry instead of per-POD. With several PODs configured from the first
        start, only the first in the list was checked immediately - the second
        (and later) ones stayed without any data until the configured hour,
        because by the time the cycle reached them the flag had already been set
        by the first. The correct condition is "does this POD already have
        data?", not "has the entry's first start already happened?".
        """
        oggi = dt_util.now().date()
        atteso = oggi - timedelta(days=RITARDO_DATI_GIORNI)

        if not self.entry.data.get(CONF_DATA_INSTALLAZIONE):
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, CONF_DATA_INSTALLAZIONE: oggi.isoformat()},
            )

        ultima_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)

        if ultima_disponibile is None:
            _LOGGER.info(
                "POD %s: nessun dato ancora importato, richiedo subito gli ultimi %d "
                "giorni per verificare POD e token. Le richieste successive partiranno "
                "dopo le %d:00 (modificabile dalle opzioni); per lo storico usa l'azione "
                "edistribuzione.recupera_storico.",
                pod,
                GIORNI_RICONTROLLO,
                self._ora_richiesta,
            )
        else:
            adesso = dt_util.now()
            if adesso.hour < self._ora_richiesta:
                return None
            if ultima_disponibile >= atteso:
                # Already rechecked today ('atteso' stays the same until
                # midnight): at most one request per day per POD, not one on
                # every hourly cycle after the configured hour.
                return None

        inizio_ricontrollo = atteso - timedelta(days=GIORNI_RICONTROLLO - 1)

        code = self._leggi_code()
        coda = code.get(pod, {})
        arretrati = sorted(
            date.fromisoformat(g) for g in coda if date.fromisoformat(g) < inizio_ricontrollo
        )
        inizio = (
            max(arretrati[0], atteso - timedelta(days=150)) if arretrati else inizio_ricontrollo
        )

        return inizio, atteso

    # ------------------------------------------------------------------
    # Fetch + import, shared between the automatic cycle and the history fetch
    # ------------------------------------------------------------------

    async def _async_importa_periodo(self, pod: str, data_da: date, data_a: date) -> dict[str, dict]:
        """Request and import BOTH directions for a POD over the period
        [data_da, data_a] (both ends included), in two separate API calls.

        A single request per direction, not one per day: the endpoint accepts a
        real multi-day range in a single response (up to 181 days).

        Returns {magnitude: {"giorni_ricevuti": set[date], "kwh_ultimo_giorno":
        float|None}} for the directions that returned valid, honoured data (see
        _magnitude_onorata) - a direction with no data for this POD/period
        simply does not appear in the result.
        """
        risultati: dict[str, dict] = {}
        for magnitude in MAGNITUDE_TUTTE:
            curva = await self._api.async_get_daily_load_profile(
                pod, data_da, data_a, magnitude=magnitude
            )
            if not _curva_ha_dati(curva):
                continue
            if not _magnitude_onorata(curva, magnitude):
                _LOGGER.warning(
                    "POD %s: risposta per magnitude=%r con energyType diverso da quello "
                    "richiesto (probabile parametro ignorato dal server): scartata, non "
                    "importata per evitare di duplicare un'altra direzione.",
                    pod,
                    magnitude,
                )
                continue

            immessa = magnitude == MAGNITUDE_IMMESSA
            await async_import_curva_giornaliera(
                self.hass, pod, curva, immessa=immessa, nome=self._nome_serie(pod, immessa)
            )

            giorni_ricevuti = _giorni_ricevuti(curva)
            risultati[magnitude] = {
                "giorni_ricevuti": giorni_ricevuti,
                "kwh_ultimo_giorno": (
                    _kwh_del_giorno(curva, max(giorni_ricevuti)) if giorni_ricevuti else None
                ),
            }
        return risultati

    # ------------------------------------------------------------------
    # Automatic polling cycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict:
        await self._async_ensure_token()

        by_pod: dict[str, dict] = {}
        for pod in self.pods:
            richiesta = await self._prossima_richiesta(pod)
            kwh_prelevata = None
            kwh_immessa = None
            ultimo_giorno_richiesto = None

            if richiesta is not None:
                data_da, data_a = richiesta
                ultimo_giorno_richiesto = data_a
                try:
                    risultati = await self._async_importa_periodo(pod, data_da, data_a)
                except ApiError as err:
                    # The requested days go into the queue instead of being
                    # lost: on the next cycle 'atteso' would already have moved.
                    for giorno in _giorni_nel_periodo(data_da, data_a):
                        self._accoda_giorno(pod, giorno)
                    raise UpdateFailed(
                        translation_domain=DOMAIN,
                        translation_key="errore_import_pod",
                        translation_placeholders={"pod": pod, "errore": str(err)},
                    ) from err

                ricevuti_prelevata = risultati.get(MAGNITUDE_PRELEVATA, {}).get(
                    "giorni_ricevuti", set()
                )
                ricevuti_immessa = risultati.get(MAGNITUDE_IMMESSA, {}).get(
                    "giorni_ricevuti", set()
                )
                ricevuti_unione = ricevuti_prelevata | ricevuti_immessa

                # A day leaves the queue if AT LEAST ONE direction returned it:
                # a POD without one of the two must not stay in the queue
                # forever waiting for data that will not arrive.
                richiesti = _giorni_nel_periodo(data_da, data_a)
                if ricevuti_unione:
                    self._rimuovi_dalla_coda(pod, [g for g in richiesti if g in ricevuti_unione])
                for giorno in richiesti:
                    if giorno not in ricevuti_unione:
                        self._accoda_giorno(pod, giorno)

                if ricevuti_prelevata != ricevuti_immessa:
                    _LOGGER.info(
                        "POD %s: prelevata e immessa non coprono esattamente gli stessi "
                        "giorni in questo ciclo (prelevata=%s, immessa=%s) - possibile "
                        "sfasamento nella pubblicazione dei due registri.",
                        pod,
                        sorted(g.isoformat() for g in ricevuti_prelevata),
                        sorted(g.isoformat() for g in ricevuti_immessa),
                    )

                kwh_prelevata = risultati.get(MAGNITUDE_PRELEVATA, {}).get("kwh_ultimo_giorno")
                kwh_immessa = risultati.get(MAGNITUDE_IMMESSA, {}).get("kwh_ultimo_giorno")

            ultima_data_disponibile = await async_get_ultima_data_disponibile(self.hass, pod)

            by_pod[pod] = {
                "ultimo_giorno_curva_richiesto": (
                    ultimo_giorno_richiesto.isoformat() if ultimo_giorno_richiesto else None
                ),
                "ultima_data_disponibile": (
                    ultima_data_disponibile.isoformat() if ultima_data_disponibile else None
                ),
                "kwh_prelevata_ultimo_giorno": kwh_prelevata,
                "kwh_immessa_ultimo_giorno": kwh_immessa,
            }

        return {"by_pod": by_pod}

    # ------------------------------------------------------------------
    # Manual history fetch (edistribuzione.recupera_storico action)
    # ------------------------------------------------------------------

    async def async_recupera_storico(
        self, data_da: date, data_a: date, pod: str | None = None
    ) -> None:
        """Fetch and import both directions for the range [data_da, data_a].

        If 'pod' is omitted, it does so for ALL the PODs configured on the
        entry; if specified, only for that one.

        Raises HomeAssistantError if nothing was imported for any POD (neither
        consumption nor injection): the action is manual and launched from the
        UI, where a silent failure is indistinguishable from a success. With
        several PODs and only a partial failure the action succeeds - something
        was imported - and the failed PODs stay in the logs.
        """
        if pod is not None and pod not in self.pods:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="pod_non_configurato",
                translation_placeholders={"pod": pod, "pods": ", ".join(self.pods)},
            )
        pod_da_recuperare = [pod] if pod else list(self.pods)

        if data_da > data_a:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="date_invertite",
                translation_placeholders={"data_da": str(data_da), "data_a": str(data_a)},
            )

        ultimo_utile = dt_util.now().date() - timedelta(days=RITARDO_DATI_GIORNI)
        if data_a > ultimo_utile:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="data_fine_troppo_recente",
                translation_placeholders={"data_a": str(data_a), "ultimo_utile": str(ultimo_utile)},
            )

        giorni_totali = (data_a - data_da).days + 1
        if giorni_totali > MAX_GIORNI_RECUPERO_STORICO:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="intervallo_troppo_ampio",
                translation_placeholders={
                    "giorni": str(giorni_totali),
                    "massimo": str(MAX_GIORNI_RECUPERO_STORICO),
                },
            )

        await self._async_ensure_token()

        _LOGGER.info(
            "Recupero storico avviato: %s - %s (POD: %s)",
            data_da,
            data_a,
            ", ".join(pod_da_recuperare),
        )

        fallimenti: list[str] = []
        pod_con_dati = 0

        for pod_corrente in pod_da_recuperare:
            try:
                risultati = await self._async_importa_periodo(pod_corrente, data_da, data_a)
            except ApiError as err:
                _LOGGER.warning(
                    "POD %s: errore recuperando il periodo %s - %s: %s",
                    pod_corrente,
                    data_da,
                    data_a,
                    err,
                )
                fallimenti.append(f"{pod_corrente}: {err}")
                continue

            ricevuti_unione: set[date] = set()
            for info in risultati.values():
                ricevuti_unione |= info["giorni_ricevuti"]

            if not ricevuti_unione:
                _LOGGER.warning(
                    "POD %s: nessun dato (né prelevata né immessa) per il periodo %s - %s",
                    pod_corrente,
                    data_da,
                    data_a,
                )
                fallimenti.append(f"{pod_corrente}: {testo(self.lingua, 'nessun_dato_pod')}")
                continue

            giorni_attesi = _giorni_nel_periodo(data_da, data_a)
            self._rimuovi_dalla_coda(
                pod_corrente, [g for g in giorni_attesi if g in ricevuti_unione]
            )
            pod_con_dati += 1

            mancanti = [g for g in giorni_attesi if g not in ricevuti_unione]
            dettaglio_mancanti = ""
            if mancanti:
                elenco = ", ".join(g.isoformat() for g in mancanti[:10])
                if len(mancanti) > 10:
                    elenco += f", e altri {len(mancanti) - 10}"
                dettaglio_mancanti = f" ({elenco})"

            _LOGGER.info(
                "POD %s: recupero storico %s - %s completato, %d/%d giorni ricevuti "
                "(unione delle due direzioni)%s",
                pod_corrente,
                data_da,
                data_a,
                len(ricevuti_unione),
                len(giorni_attesi),
                dettaglio_mancanti,
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

    @property
    def api(self) -> ApiClient:
        """Expose the API client for on-demand calls."""
        return self._api
