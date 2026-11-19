"""Sensors: the diagnostic ones read coordinator.riepilogo, shaped as
{pod: {"ultima_data_disponibile": ..., "ultimo_giorno": {direzione: ...}}};
the current time band is computed from the calendar.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.edistribuzione import sensor as s
from custom_components.edistribuzione.const import (
    DOMAIN,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    TIPO_POD_DEFAULT,
)
from custom_components.edistribuzione.fasce import FUSO_ITALIA

POD = "IT001E00000009"


def _entry(hass=None, pods=(POD,)):
    entry = MockConfigEntry(domain=DOMAIN, data={"pods": list(pods)}, title="E-Distribuzione")
    if hass is not None:
        entry.add_to_hass(hass)
    return entry


def _coord(riepilogo=None, pods=(POD,), tipo_pod=TIPO_POD_DEFAULT, lingua="it"):
    return SimpleNamespace(
        riepilogo=riepilogo or {},
        last_update_success=False,
        pods=list(pods),
        entry=None,
        tipo_pod=lambda pod: tipo_pod,
        lingua=lingua,
    )


def _ultimo_giorno(prelevata=None, immessa=None):
    return {MAGNITUDE_PRELEVATA: prelevata, MAGNITUDE_IMMESSA: immessa}


def test_pod_configurati():
    sensore = s.PodConfiguratiSensor(_coord(pods=[POD, "IT002"]), _entry())
    assert sensore.native_value == 2
    assert sensore.extra_state_attributes["pods"] == [POD, "IT002"]


def test_ultima_data_disponibile():
    coord = _coord({POD: {"ultima_data_disponibile": "2026-09-03"}})
    sensore = s.UltimaDataDisponibileSensor(coord, _entry(), POD)
    assert sensore.native_value == date(2026, 9, 3)


def test_ultima_data_disponibile_senza_dati():
    sensore = s.UltimaDataDisponibileSensor(_coord(), _entry(), POD)
    assert sensore.native_value is None


def test_consumo_giorno_legge_valore_e_giorno_della_direzione():
    coord = _coord({
        POD: {
            "ultimo_giorno": _ultimo_giorno(
                prelevata={"giorno": "2026-09-02", "kwh": 1.235},
                immessa={"giorno": "2026-09-01", "kwh": 9.876},
            )
        }
    })
    prelevata = s.ConsumoGiornoSensor(coord, _entry(), POD, immessa=False)
    immessa = s.ConsumoGiornoSensor(coord, _entry(), POD, immessa=True)
    assert prelevata.native_value == 1.235
    assert prelevata.extra_state_attributes["giorno"] == "2026-09-02"
    assert immessa.native_value == 9.876
    assert immessa.extra_state_attributes["giorno"] == "2026-09-01"


def test_consumo_giorno_senza_dati_resta_sconosciuto():
    sensore = s.ConsumoGiornoSensor(_coord(), _entry(), POD, immessa=False)
    assert sensore.native_value is None
    assert sensore.extra_state_attributes["giorno"] is None


def test_sensori_diagnostici_disponibili_anche_se_l_aggiornamento_fallisce():
    """The values come from data already imported: they stay valid while
    E-Distribuzione does not answer (last_update_success False)."""
    coord = _coord({POD: {"ultima_data_disponibile": "2026-09-03"}})
    assert s.UltimaDataDisponibileSensor(coord, _entry(), POD).available
    assert s.ConsumoGiornoSensor(coord, _entry(), POD).available


@pytest.mark.parametrize(
    ("tipo_pod", "immessa", "chiave"),
    [
        ("scambio", False, "prelievo_ultimo_giorno"),
        ("scambio", True, "immissione_ultimo_giorno"),
        ("produzione", False, "prelievo_tecnico_ultimo_giorno"),
        ("produzione", True, "produzione_ultimo_giorno"),
    ],
)
def test_consumo_giorno_nome_dipende_dal_ruolo(tipo_pod, immessa, chiave):
    sensore = s.ConsumoGiornoSensor(_coord(tipo_pod=tipo_pod), _entry(), POD, immessa=immessa)
    assert sensore.translation_key == chiave
    assert sensore._attr_has_entity_name


@pytest.mark.parametrize(
    ("lingua", "account", "pod"),
    [("it", "Account API", "Punto di prelievo"), ("en", "API account", "Withdrawal point")],
)
def test_modello_dispositivo_segue_la_lingua_del_server(lingua, account, pod):
    coord = _coord(lingua=lingua)
    assert s.PodConfiguratiSensor(coord, _entry()).device_info["model"] == account
    assert s.UltimaDataDisponibileSensor(coord, _entry(), POD).device_info["model"] == pod


async def test_build_entities_conta_account_piu_tredici_per_pod(hass):
    entry = _entry(hass, pods=[POD, "IT002"])
    coord = SimpleNamespace(
        data=None,
        pods=[POD, "IT002"],
        entry=entry,
        tipo_pod=lambda pod: TIPO_POD_DEFAULT,
        lingua="it",
        energia_reattiva_attiva=False,
    )
    entità = s.build_entities(hass, coord)
    # 2 on the account + 13 per POD (reactive opt-in disabled by default)
    assert len(entità) == 2 + 13 * 2
    assert {"PodConfiguratiSensor", "FasciaCorrenteSensor"} <= {type(e).__name__ for e in entità}


async def test_fascia_corrente_cambia_al_confine_senza_polling(
    hass, freezer: FrozenDateTimeFactory
):
    """Monday October 12, 2026, 07:30 in Italy: F2 until 08:00, then F1. The
    state changes exactly at the boundary, through the scheduled timer."""
    await hass.config.async_set_time_zone("Europe/Rome")
    freezer.move_to(datetime(2026, 10, 12, 7, 30, tzinfo=FUSO_ITALIA))
    entry = _entry(hass)
    sensore = s.FasciaCorrenteSensor(_coord(), entry)
    sensore.hass = hass
    # The state written to the state machine is checked by the full setup
    # test (test_azione_recupera_storico.py): here the timer is what matters.
    sensore.async_write_ha_state = Mock()
    await sensore.async_added_to_hass()

    assert sensore.native_value == "f2"
    attributi = sensore.extra_state_attributes
    assert attributi["prossima_fascia"] == "f1"
    assert attributi["prossimo_cambio"] == datetime(2026, 10, 12, 8, 0, tzinfo=FUSO_ITALIA)

    freezer.move_to(datetime(2026, 10, 12, 8, 0, 1, tzinfo=FUSO_ITALIA))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert sensore.native_value == "f1"
    assert sensore.async_write_ha_state.call_count == 2
    assert sensore.extra_state_attributes["prossimo_cambio"] == datetime(
        2026, 10, 12, 19, 0, tzinfo=FUSO_ITALIA
    )

    sensore._annulla()
    assert sensore._annulla_timer is None


def test_fascia_corrente_e_un_enum_sull_account():
    sensore = s.FasciaCorrenteSensor(_coord(), _entry())
    assert sensore.options == ["f1", "f2", "f3"]
    assert sensore.translation_key == "fascia_corrente"
    assert sensore.device_info["identifiers"] == {(DOMAIN, sensore.unique_id.rsplit("_", 2)[0])}
