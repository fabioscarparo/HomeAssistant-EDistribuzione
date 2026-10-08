"""DataUpdateCoordinator per E-Distribuzione.

Supporta più POD sulla stessa config entry (stessa credenziale, stessa
utenza autenticata). Ogni ciclo, per ciascun POD, se è il momento (coda +
orario, al massimo una volta al giorno): richiede ENTRAMBE le direzioni
dell'energia (prelevata e immessa, vedi MAGNITUDE_TUTTE in const.py) per gli
ultimi GIORNI_RICONTROLLO giorni (non solo il più recente, per ricontrollare
eventuali rettifiche di E-Distribuzione su giorni già importati), in
un'unica richiesta per direzione. La scrittura effettiva nelle external
statistics ricalcola sempre l'intera serie dal raw storage a 15 minuti (vedi
statistics.py), quindi una rettifica corregge automaticamente anche le sum
cumulative successive.

Un giorno esce dalla coda di retry se almeno una delle due direzioni lo ha
restituito: un POD senza una delle due (es. un contatore normale che non
misura mai immissione) non deve restare in coda per sempre in attesa di un
dato che non arriverà.

async_recupera_storico accetta un parametro 'pod' opzionale: se omesso,
recupera lo storico per TUTTI i POD della entry.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiClient, ApiError
from .auth import AuthClient, AuthError
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
    """Elenco dei giorni compresi nell'intervallo, estremi inclusi."""
    giorni, cursore = [], data_da
    while cursore <= data_a:
        giorni.append(cursore)
        cursore += timedelta(days=1)
    return giorni


def _giorni_ricevuti(curva: list[dict]) -> set[date]:
    """Giorni effettivamente presenti nella risposta (campo sampleDate,
    formato YYYYMMDD), scartando quelli senza campioni."""
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
    """Totale kWh di un singolo giorno dentro una risposta multi-giorno."""
    atteso = giorno.strftime("%Y%m%d")
    for elemento in curva:
        readings = elemento.get("readings", {})
        if readings.get("sampleDate") == atteso:
            return sum(float(c.get("val", 0)) for c in readings.get("sampleValues", []))
    return None


def _curva_ha_dati(curva: list[dict]) -> bool:
    """True se la risposta di async_get_daily_load_profile contiene davvero
    dei campioni, non solo una struttura vuota."""
    return bool(curva) and bool(curva[0].get("readings", {}).get("sampleValues"))


def _magnitude_onorata(curva: list[dict], magnitude_richiesta: str) -> bool:
    """True se il server ha davvero servito la magnitude richiesta.

    'energyType' nella risposta rimanda indietro la magnitude effettivamente
    servita: se il server ignora silenziosamente un parametro sconosciuto e
    risponde comunque con la prelevata, questo campo lo rivela. Senza questo
    controllo, una magnitude ignorata sembrerebbe un successo e finirebbe a
    duplicare la prelevata nella serie immessa - è il rischio principale di
    questa integrazione finché MAGNITUDE_IMMESSA non è confermata.

    Un elemento senza 'energyType' (mai osservato, ma non escluso da nessuna
    documentazione) non fa fallire il controllo: si presume onorato piuttosto
    che scartare dati buoni per un campo assente.
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
        session = async_get_clientsession(hass)
        self._auth = AuthClient(session)
        self._api = ApiClient(session, access_token="")

    def tipo_pod(self, pod: str) -> str:
        """Ruolo assegnato dall'utente al POD (scambio/produzione), dalle
        opzioni della config entry. Influenza solo le etichette visibili
        (vedi _nome_serie), mai quali direzioni vengono richieste."""
        tipi = self.entry.options.get(CONF_TIPO_POD, {})
        return tipi.get(pod, TIPO_POD_DEFAULT)

    @property
    def lingua(self) -> str:
        """Lingua del server, per i testi che HA non traduce (vedi testi.py)."""
        return self.hass.config.language

    def _nome_serie(self, pod: str, immessa: bool) -> str:
        """Etichetta della external statistic per POD/direzione, dipendente
        dal ruolo scelto dall'utente. Il nome finisce nei metadati del
        Recorder, senza traduzione: segue la lingua del server e si aggiorna
        al primo import dopo un cambio di lingua."""
        if self.tipo_pod(pod) == TIPO_POD_PRODUZIONE:
            chiave = "serie_produzione" if immessa else "serie_prelievo_tecnico"
        else:
            chiave = "serie_immissione" if immessa else "serie_prelievo"
        return f"E-Distribuzione {pod} - {testo(self.lingua, chiave)}"

    async def _async_ensure_token(self) -> None:
        refresh_token = self.entry.data[CONF_REFRESH_TOKEN]
        try:
            tokens = await self._auth.async_refresh_access_token(refresh_token)
        except AuthError as err:
            # Un refresh fallito significa quasi certamente che il
            # refresh_token è stato revocato (cambio password, pulizia
            # sessioni lato Enel, ...) e l'utente deve rifare il login
            # tramite il reauth del config_flow.
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="refresh_token_fallito",
                translation_placeholders={"errore": str(err)},
            ) from err

        self._api.update_token(tokens.access_token)

        if tokens.refresh_token != refresh_token:
            new_data = dict(self.entry.data)
            new_data[CONF_REFRESH_TOKEN] = tokens.refresh_token
            self.hass.config_entries.async_update_entry(self.entry, data=new_data)

    # ------------------------------------------------------------------
    # Orario configurabile + coda dei giorni da riprovare, PER POD
    # ------------------------------------------------------------------

    @property
    def _ora_richiesta(self) -> int:
        """Ora (locale) a partire dalla quale chiedere la curva del giorno
        prima. Configurabile dalle opzioni: se capitano richieste spesso
        vuote conviene spostarla più avanti."""
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
        """Code dei giorni da riprovare, UNA PER POD:
        {pod: {giorno ISO: data di primo inserimento}}.

        La data di primo inserimento fa da timer: un giorno viene abbandonato
        dopo ABBANDONO_CODA_DOPO_GIORNI a prescindere dal numero di
        tentativi (vedi _scrivi_code).
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
        """Salva le code, scartando i giorni troppo vecchi e limitandone il numero, per ciascun POD."""
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
        """Decide che intervallo chiedere per questo POD in questo ciclo.

        Un POD senza NESSUN dato ancora importato (primo avvio della entry,
        o un POD aggiunto in seguito dalle opzioni) chiede subito, a
        prescindere dall'orario configurato - serve a verificare da subito
        che POD e token siano validi, invece di scoprirlo solo a sera. Nei
        cicli successivi aspetta l'orario configurato, poi al massimo una
        volta al giorno (la stessa 'atteso' resta invariata finché non
        cambia il giorno) chiede in UNA SOLA richiesta gli ultimi
        GIORNI_RICONTROLLO giorni fino al giorno atteso - non solo il più
        recente: E-Distribuzione può rettificare un giorno già pubblicato, e
        senza questo ricontrollo periodico quella correzione non verrebbe
        mai vista in automatico (resta comunque recuperabile a mano con
        recupera_storico).

        Se ci sono giorni arretrati PIU' VECCHI della finestra di
        ricontrollo (bloccati in coda da un errore precedente), l'intervallo
        si allarga all'indietro per includerli, sempre in un'unica
        richiesta.

        BUG STORICO (corretto qui): la versione precedente subordinava il
        fetch immediato a CONF_DATA_INSTALLAZIONE, un flag CONDIVISO da tutta
        la config entry invece che per-POD. Con più POD configurati fin dal
        primo avvio, solo il primo della lista veniva verificato subito - il
        secondo (e successivi) restava senza nessun dato fino all'orario
        configurato, perché quando il ciclo arrivava a lui il flag era già
        stato impostato dal primo. La condizione corretta è "questo POD ha
        già dei dati?", non "è già passato il primo avvio della entry?".
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
                # Già ricontrollato oggi ('atteso' resta lo stesso fino a
                # mezzanotte): al massimo una richiesta al giorno per POD,
                # non una ad ogni ciclo orario dopo l'orario configurato.
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
    # Fetch + import, condiviso tra ciclo automatico e recupero storico
    # ------------------------------------------------------------------

    async def _async_importa_periodo(self, pod: str, data_da: date, data_a: date) -> dict[str, dict]:
        """Richiede e importa ENTRAMBE le direzioni per un POD sul periodo
        [data_da, data_a] (estremi inclusi), in due chiamate API separate.

        Una sola richiesta per direzione, non una per giorno: l'endpoint
        accetta un intervallo multi-giorno vero in un'unica risposta
        (confermato funzionante fino a 181 giorni).

        Ritorna {magnitude: {"giorni_ricevuti": set[date], "kwh_ultimo_giorno":
        float|None}} per le sole direzioni che hanno restituito dati validi
        e onorati (vedi _magnitude_onorata) - una direzione senza dati per
        questo POD/periodo semplicemente non compare nel risultato.
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
    # Ciclo di polling automatico
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
                    # I giorni richiesti vanno in coda invece di andare
                    # persi: al ciclo successivo 'atteso' sarebbe già avanzato.
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

                # Un giorno esce dalla coda se ALMENO UNA direzione lo ha
                # restituito: un POD senza una delle due non deve restare in
                # coda per sempre in attesa di un dato che non arriverà.
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
    # Recupero storico manuale (azione edistribuzione.recupera_storico)
    # ------------------------------------------------------------------

    async def async_recupera_storico(
        self, data_da: date, data_a: date, pod: str | None = None
    ) -> None:
        """Recupera e importa entrambe le direzioni per l'intervallo
        [data_da, data_a].

        Se 'pod' è omesso, lo fa per TUTTI i POD configurati sulla entry; se
        specificato, solo per quello.

        Solleva HomeAssistantError se al termine non è stato importato
        nulla per nessun POD (né prelevata né immessa): l'azione è manuale e
        lanciata dall'interfaccia, dove un fallimento silenzioso è
        indistinguibile da un successo. Con più POD e un fallimento solo
        parziale l'azione riesce - qualcosa è stato importato - e i POD
        falliti restano nei log.
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
        """Espone il client API per chiamate on-demand."""
        return self._api
