"""Sensors: per-POD sensors read coordinator caches; current time band uses calendar."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.helpers.entity import EntityCategory
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.edistribuzione import sensor as s
from custom_components.edistribuzione.const import DOMAIN, TIPO_POD_DEFAULT
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


def test_sensori_pod_disponibili_anche_se_l_aggiornamento_fallisce():
    """The values come from data already imported: they stay valid while
    E-Distribuzione does not answer (last_update_success False)."""
    coord = _coord({POD: {"ultima_data_disponibile": "2026-09-03"}})
    assert s.UltimaDataDisponibileSensor(coord, _entry(), POD).available
    assert s.UltimaLetturaUfficialeSensor(coord, _entry(), POD).available
    assert s.PotenzaContrattualeSensor(coord, _entry(), POD).available
    assert s.PiccoPotenzaGiornoSensor(coord, _entry(), POD).available


@pytest.mark.parametrize(
    ("lingua", "account", "pod"),
    [("it", "Account API", "Punto di prelievo"), ("en", "API account", "Withdrawal point")],
)
def test_modello_dispositivo_segue_la_lingua_del_server(lingua, account, pod):
    coord = _coord(lingua=lingua)
    assert s.PodConfiguratiSensor(coord, _entry()).device_info["model"] == account
    assert s.UltimaDataDisponibileSensor(coord, _entry(), POD).device_info["model"] == pod


async def test_build_entities_conta_account_piu_sette_per_pod(hass):
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
    # 2 on the account + 7 per POD (reactive opt-in disabled by default)
    assert len(entità) == 2 + 7 * 2
    assert {"PodConfiguratiSensor", "FasciaCorrenteSensor"} <= {type(e).__name__ for e in entità}


async def test_categorie_entita_solo_date_diagnostiche_sui_pod(hass):
    entry = _entry(hass, pods=[POD])
    coord = SimpleNamespace(
        data=None,
        pods=[POD],
        entry=entry,
        tipo_pod=lambda pod: TIPO_POD_DEFAULT,
        lingua="it",
        energia_reattiva_attiva=True,
    )
    by_key = {e.translation_key: e for e in s.build_entities(hass, coord)}
    diagnostiche = {
        k for k, ent in by_key.items() if ent.entity_category == EntityCategory.DIAGNOSTIC
    }
    primarie = {k for k, ent in by_key.items() if ent.entity_category is None}
    assert diagnostiche == {
        "pod_configurati",
        "ultima_data_disponibile",
        "ultima_lettura_ufficiale",
    }
    assert primarie == {
        "fascia_corrente",
        "potenza_contrattuale",
        "potenza_disponibile",
        "picco_potenza_ultimo_giorno",
        "picco_potenza_mese_corrente",
        "picco_potenza_mese_precedente",
        "energia_reattiva_mese_corrente",
        "fattore_potenza_mese_corrente",
    }


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
