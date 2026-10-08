"""Test dei sensori diagnostici: leggono coordinator.data nella forma
{"by_pod": {pod: {...}}}.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import sensor as s
from custom_components.edistribuzione.const import DOMAIN, TIPO_POD_DEFAULT

POD = "IT001E00000009"


def _entry(hass=None, pods=(POD,)):
    entry = MockConfigEntry(domain=DOMAIN, data={"pods": list(pods)}, title="E-Distribuzione")
    if hass is not None:
        entry.add_to_hass(hass)
    return entry


def _coord(by_pod=None, pods=(POD,), tipo_pod=TIPO_POD_DEFAULT, lingua="it"):
    return SimpleNamespace(
        data={"by_pod": by_pod} if by_pod is not None else None,
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


def test_consumo_giorno_prelevata_arrotonda_a_tre_decimali():
    coord = _coord({
        POD: {
            "kwh_prelevata_ultimo_giorno": 1.23456,
            "ultimo_giorno_curva_richiesto": "2026-09-02",
        }
    })
    sensore = s.ConsumoGiornoSensor(coord, _entry(), POD, immessa=False)
    assert sensore.native_value == 1.235
    assert sensore.extra_state_attributes["giorno"] == "2026-09-02"


def test_consumo_giorno_immessa_legge_la_chiave_propria():
    coord = _coord({
        POD: {
            "kwh_prelevata_ultimo_giorno": 1.0,
            "kwh_immessa_ultimo_giorno": 9.876,
        }
    })
    sensore = s.ConsumoGiornoSensor(coord, _entry(), POD, immessa=True)
    assert sensore.native_value == 9.876


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


async def test_build_entities_conta_account_piu_tre_per_pod(hass):
    entry = _entry(hass, pods=[POD, "IT002"])
    coord = SimpleNamespace(
        data=None,
        pods=[POD, "IT002"],
        entry=entry,
        tipo_pod=lambda pod: TIPO_POD_DEFAULT,
        lingua="it",
    )
    entità = s.build_entities(hass, coord)
    # 1 sull'account + 3 per ciascuno dei 2 POD (ultima data + prelevata + immessa)
    assert len(entità) == 1 + 3 * 2
    assert "PodConfiguratiSensor" in {type(e).__name__ for e in entità}
