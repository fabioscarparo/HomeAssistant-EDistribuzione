"""Tests for incremental Recorder writes, extended diagnostics, binary sensor and HA services."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml
from freezegun import freeze_time
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata, statistics_during_period
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from custom_components.edistribuzione import (
    SERVICE_RECUPERA_STORICO,
    async_remove_entry,
    async_setup,
    binary_sensor,
    diagnostics,
    fasce,
    raw_storage,
    statistics,
)
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_AGGIORNATO_IL,
    CONF_TOKEN_ORIGINE,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    MAGNITUDE_TUTTE,
    MAX_EVENTI_CRONOLOGIA,
    issue_id_per_entry,
)

from .conftest import POD_A, POD_B


def _giorno(sample_date: str, initial_sample: str, valori: list[float]) -> dict:
    return {
        "readings": {
            "energyType": "A1",
            "sampleDate": sample_date,
            "sampleValues": [
                {"id": str(i), "val": str(v)} for i, v in enumerate(valori, start=1)
            ],
        },
        "sampleFrequency": 15,
        "timeType": "CONS",
        "initialSample": initial_sample,
    }


async def _leggi_statistiche(hass, statistic_id: str) -> dict[datetime, dict]:
    risultato = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return {
        dt_util.utc_from_timestamp(riga["start"]): riga for riga in risultato.get(statistic_id, [])
    }


# ---------------------------------------------------------------------------
# Section 5: incremental Recorder writes & SQLite batching
# ---------------------------------------------------------------------------


async def test_incremental_import_writes_only_changed_hours(recorder_mock, hass, tmp_path):
    """Importing day 2 only writes day 2 (24h); reimporting identical data writes 0 rows."""
    pod = "IT001E00000090"
    d1 = _giorno("20260801", "2026-07-31T22:00:00.000+00:00", [0.25] * 96)
    d2 = _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [0.5] * 96)
    d3 = _giorno("20260803", "2026-08-02T22:00:00.000+00:00", [0.25] * 96)
    db_file = str(tmp_path / "curve.db")

    with patch.object(raw_storage, "percorso_predefinito", return_value=db_file):
        await statistics.async_import_curva_giornaliera(hass, pod, [d1], nome="POD test")
        await async_recorder_block_till_done(hass)

        # Day 2: only 24 rows per series (4 series = 96 rows total), not 48 per series.
        with patch.object(
            statistics,
            "async_add_external_statistics",
            wraps=statistics.async_add_external_statistics,
        ) as spy_add:
            await statistics.async_import_curva_giornaliera(hass, pod, [d1, d2], nome="POD test")
            await async_recorder_block_till_done(hass)
            righe_per_serie = [len(call.args[2]) for call in spy_add.call_args_list]
            assert righe_per_serie == [24, 24, 24, 24]

        # Identical reimport: 0 writes to Recorder.
        with patch.object(
            statistics,
            "async_add_external_statistics",
            wraps=statistics.async_add_external_statistics,
        ) as spy_add:
            await statistics.async_import_curva_giornaliera(hass, pod, [d1, d2], nome="POD test")
            assert spy_add.call_count == 0

        # Name change only (e.g. POD role changed) updates metadata with a 1-row write per series.
        with patch.object(
            statistics,
            "async_add_external_statistics",
            wraps=statistics.async_add_external_statistics,
        ) as spy_add:
            await statistics.async_import_curva_giornaliera(hass, pod, [d2], nome="POD nuovo")
            await async_recorder_block_till_done(hass)
            assert [len(call.args[2]) for call in spy_add.call_args_list] == [1, 1, 1, 1]

        sid_totale = statistics._sanitize_statistic_id(pod)
        meta = await get_instance(hass).async_add_executor_job(
            partial(get_metadata, hass, statistic_ids={sid_totale})
        )
        assert next(iter(meta.values()))[1]["name"] == "POD nuovo"

        # Add day 3, then revise day 2: must rewrite from day 2 onward (48h) and keep sums exact.
        await statistics.async_import_curva_giornaliera(hass, pod, [d3], nome="POD nuovo")
        await async_recorder_block_till_done(hass)

        d2_corretto = _giorno("20260802", "2026-08-01T22:00:00.000+00:00", [0.25] * 96)
        with patch.object(
            statistics,
            "async_add_external_statistics",
            wraps=statistics.async_add_external_statistics,
        ) as spy_add:
            await statistics.async_import_curva_giornaliera(
                hass, pod, [d2_corretto], nome="POD nuovo"
            )
            await async_recorder_block_till_done(hass)
            assert [len(call.args[2]) for call in spy_add.call_args_list] == [48, 48, 48, 48]

        ore = await _leggi_statistiche(hass, sid_totale)
        assert len(ore) == 72
        assert ore[max(ore)]["sum"] == pytest.approx(72.0)


def test_raw_storage_upsert_delta_and_batch_helpers(tmp_path):
    db_path = str(tmp_path / "batch.db")
    t1 = datetime(2026, 8, 1, 22, 0, tzinfo=UTC)
    t2 = datetime(2026, 8, 1, 22, 15, tzinfo=UTC)

    assert raw_storage.upsert_campioni(db_path, POD_A, MAGNITUDE_PRELEVATA, [(t1, 1.0), (t2, 2.0)]) == int(
        t1.timestamp()
    )
    # Identical samples -> None (no delta).
    assert raw_storage.upsert_campioni(db_path, POD_A, MAGNITUDE_PRELEVATA, [(t1, 1.0), (t2, 2.0)]) is None
    # Change only t2 -> returns t2 timestamp.
    assert raw_storage.upsert_campioni(db_path, POD_A, MAGNITUDE_PRELEVATA, [(t1, 1.0), (t2, 3.5)]) == int(
        t2.timestamp()
    )

    riepilogo = raw_storage.riepilogo_db(db_path, [POD_A, POD_B], MAGNITUDE_TUTTE)
    assert riepilogo[POD_A][MAGNITUDE_PRELEVATA] == (date(2026, 8, 2), pytest.approx(4.5))
    assert riepilogo[POD_A][MAGNITUDE_IMMESSA] is None
    assert riepilogo[POD_B][MAGNITUDE_PRELEVATA] is None

    stats_db = raw_storage.statistiche_db(db_path, [POD_A], MAGNITUDE_TUTTE)
    assert stats_db["esiste"] is True
    assert stats_db["dimensione_byte"] > 0
    assert stats_db["per_pod"][POD_A][MAGNITUDE_PRELEVATA]["campioni"] == 2
    assert stats_db["per_pod"][POD_A][MAGNITUDE_PRELEVATA]["prima_data"] == "2026-08-02"


def test_fasce_classifica_ore_matches_aggrega_ore_per_fascia():
    campioni = [
        (datetime(2026, 8, 3, 6, m, tzinfo=UTC), 0.25) for m in (0, 15, 30, 45)
    ] + [
        (datetime(2026, 8, 3, 8, m, tzinfo=UTC), 0.5) for m in (0, 15, 30, 45)
    ]
    da_campioni = fasce.aggrega_ore_per_fascia(campioni)
    ore = statistics._aggrega_ore(campioni)
    da_ore = fasce.classifica_ore(ore)
    assert da_campioni == da_ore


# ---------------------------------------------------------------------------
# Section 6: extended diagnostics & diagnostic binary sensor
# ---------------------------------------------------------------------------


async def test_extended_diagnostics_contains_observability_fields_and_is_redacted(
    hass, make_edist_coordinator, tmp_path
):
    db_file = str(tmp_path / "diag.db")
    t1 = datetime(2026, 8, 1, 22, 0, tzinfo=UTC)
    raw_storage.upsert_campioni(db_file, POD_A, MAGNITUDE_PRELEVATA, [(t1, 1.25)])

    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={
            CONF_ACCESS_TOKEN: "TOK_SECRET",
            CONF_REFRESH_TOKEN: "REF_SECRET",
            CONF_TOKEN_ORIGINE: "browser",
            CONF_TOKEN_AGGIORNATO_IL: "2026-10-01T12:00:00+02:00",
        },
    )
    coordinator._registra_evento("polling", "ok", 42, pod_richiesti=1)
    coordinator.entry.runtime_data = coordinator
    hass.data[DOMAIN] = {coordinator.entry.entry_id: coordinator}

    with (
        patch.object(raw_storage, "percorso_predefinito", return_value=db_file),
        patch(
            "custom_components.edistribuzione.diagnostics.async_get_integration",
            AsyncMock(return_value=Mock(version="1.0.0")),
        ),
    ):
        data = await diagnostics.async_get_config_entry_diagnostics(hass, coordinator.entry)

    dumped = json.dumps(data, default=str)
    assert POD_A not in dumped
    assert "TOK_SECRET" not in dumped
    assert "REF_SECRET" not in dumped
    assert data["database_locale"]["esiste"] is True
    assert data["database_locale"]["per_pod"]["POD_1"][MAGNITUDE_PRELEVATA]["campioni"] == 1
    assert data["card_lovelace"]["registrata"] is False
    assert data["fascia_corrente"] in fasce.FASCE
    assert data["accesso"]["origine_token"] == "browser"
    assert data["coordinator"]["ora_richiesta_effettiva"] == 19
    assert "POD_1" in data["coordinator"]["prossima_azione"]
    assert data["coordinator"]["cronologia_recente"][-1]["tipo"] == "polling"


def test_access_problem_binary_sensor_reflects_coordinator():
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PODS: [POD_A], CONF_REFRESH_TOKEN: "rt"})
    coord = SimpleNamespace(
        accesso_bloccato=False,
        host_ultimo_accesso="private.e-distribuzione.it",
        ultimo_rinnovo="2026-10-01T10:00:00+02:00",
        ultimo_errore_accesso=None,
        lingua="it",
    )
    ent = binary_sensor.AccessoBloccatoBinarySensor(coord, entry)
    assert ent.device_class == BinarySensorDeviceClass.PROBLEM
    assert ent.available is True
    assert ent.is_on is False
    coord.accesso_bloccato = True
    assert ent.is_on is True
    assert ent.extra_state_attributes["host_ultimo_accesso"] == "private.e-distribuzione.it"


async def test_event_ring_buffer_caps_length(make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])
    for i in range(MAX_EVENTI_CRONOLOGIA + 5):
        coordinator._registra_evento("polling", "ok", i)
    assert len(coordinator.cronologia_eventi) == MAX_EVENTI_CRONOLOGIA
    assert coordinator.cronologia_eventi[-1]["durata_ms"] == MAX_EVENTI_CRONOLOGIA + 4


# ---------------------------------------------------------------------------
# Section 7: async_setup, SupportsResponse, async_remove_entry, services.yaml
# ---------------------------------------------------------------------------


async def test_recupera_storico_returns_structured_response(hass, make_edist_coordinator):
    assert await async_setup(hass, {}) is True
    coordinator = make_edist_coordinator(pods=[POD_A])
    entry = coordinator.entry
    entry.runtime_data = coordinator
    entry._async_set_state(hass, entry.state.LOADED, None)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    dev_reg = dr.async_get(hass)
    dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, entry.entry_id)}
    )
    coordinator._async_ensure_token = AsyncMock()
    coordinator.async_aggiorna_riepilogo = AsyncMock(return_value={})
    coordinator._async_importa_periodo = AsyncMock(
        return_value={
            MAGNITUDE_PRELEVATA: {"giorni_ricevuti": {date(2026, 9, 10)}},
            MAGNITUDE_IMMESSA: {"giorni_ricevuti": {date(2026, 9, 10)}},
        }
    )

    with freeze_time("2026-09-15 12:00:00+02:00"):
        resp = await hass.services.async_call(
            DOMAIN,
            SERVICE_RECUPERA_STORICO,
            {"device_id": dev.id, "data_da": "2026-09-10", "data_a": "2026-09-10"},
            blocking=True,
            return_response=True,
        )

    assert resp == {
        "data_da": "2026-09-10",
        "data_a": "2026-09-10",
        "pod": {
            POD_A: {
                "giorni_attesi": 1,
                "giorni_ricevuti": 1,
                "prelevata": 1,
                "immessa": 1,
                "mancanti": [],
            }
        },
    }


async def test_async_remove_entry_clears_entry_repairs_issue(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._segnala_blocco(ISSUE_ACCESSO_BLOCCATO)
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, coordinator.entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    await async_remove_entry(hass, coordinator.entry)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


def test_services_yaml_is_valid_english_and_has_no_long_dashes():
    raw = (
        Path(__file__).parent.parent
        / "custom_components"
        / "edistribuzione"
        / "services.yaml"
    ).read_text(encoding="utf-8")
    assert "\u2013" not in raw and "\u2014" not in raw
    parsed = yaml.safe_load(raw)
    assert set(parsed) == {"recupera_storico", "configura_energy_dashboard"}
    assert parsed["recupera_storico"]["name"] == "Fetch history"
