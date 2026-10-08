"""Traduzioni italiano/inglese: translations/*.json e testi.py.

Una chiave presente in una lingua e non nell'altra non rompe niente a
runtime (HA ricade sull'inglese, o mostra la chiave grezza), quindi è
proprio il tipo di errore che nessuno nota: questi test lo bloccano prima.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers.translation import async_get_translations

from custom_components.edistribuzione import sensor, testi
from custom_components.edistribuzione.const import DOMAIN, TIPO_POD_PRODUZIONE, TIPO_POD_SCAMBIO

CARTELLA = Path(__file__).parent.parent / "custom_components" / DOMAIN
LINGUE = ("it", "en")


def _carica(lingua: str) -> dict:
    return json.loads((CARTELLA / "translations" / f"{lingua}.json").read_text(encoding="utf-8"))


def _foglie(albero: dict, prefisso: str = "") -> dict[str, str]:
    foglie = {}
    for chiave, valore in albero.items():
        if isinstance(valore, dict):
            foglie |= _foglie(valore, f"{prefisso}{chiave}.")
        else:
            foglie[f"{prefisso}{chiave}"] = valore
    return foglie


def _segnaposto(testo: str) -> set[str]:
    return set(re.findall(r"\{(\w+)\}", testo))


def test_italiano_e_inglese_hanno_le_stesse_chiavi_e_gli_stessi_segnaposto():
    it, en = _foglie(_carica("it")), _foglie(_carica("en"))
    assert it.keys() == en.keys()
    for chiave in it:
        assert _segnaposto(it[chiave]) == _segnaposto(en[chiave]), chiave


@pytest.mark.parametrize("lingua", LINGUE)
def test_nessuna_lineetta_lunga(lingua):
    testo = (CARTELLA / "translations" / f"{lingua}.json").read_text(encoding="utf-8")
    assert not re.search("[–—]", testo)


def test_ogni_translation_key_del_codice_esiste():
    """Le chiavi usate nel codice (sensori, eccezioni, selettori) devono
    esistere nei file di traduzione, altrimenti la UI mostra la chiave."""
    sorgente = "\n".join(p.read_text(encoding="utf-8") for p in CARTELLA.glob("*.py"))
    usate = set(re.findall(r'translation_key="(\w+)"', sorgente))
    usate |= set(re.findall(r'_attr_translation_key = "(\w+)"', sorgente))
    usate |= {
        sensor.ConsumoGiornoSensor._chiave_traduzione(
            SimpleNamespace(tipo_pod=lambda pod, r=ruolo: r), "POD", immessa
        )
        for ruolo in (TIPO_POD_SCAMBIO, TIPO_POD_PRODUZIONE)
        for immessa in (False, True)
    }
    it = _carica("it")
    definite = (
        set(it["entity"]["sensor"]) | set(it["exceptions"]) | set(it["selector"])
    )
    assert usate, "nessuna translation_key trovata nel codice: regex da aggiornare"
    assert usate <= definite, usate - definite


def test_testi_stesse_chiavi_e_segnaposto_in_ogni_lingua():
    it, en = testi.TESTI["it"], testi.TESTI["en"]
    assert it.keys() == en.keys()
    for chiave in it:
        assert _segnaposto(it[chiave]) == _segnaposto(en[chiave]), chiave


@pytest.mark.parametrize(
    ("lingua", "attesa"),
    [("it", "it"), ("it-IT", "it"), ("en", "en"), ("en-GB", "en"), ("de", "en"), (None, "en")],
)
def test_lingua_supportata(lingua, attesa):
    assert testi.lingua_supportata(lingua) == attesa


@pytest.mark.parametrize(
    ("lingua", "nome"), [("it", "Prelievo ultimo giorno"), ("en", "Last day consumption")]
)
async def test_home_assistant_carica_i_nomi_tradotti(hass, lingua, nome):
    """Il caricatore vero di HA, non solo il JSON: verifica percorso e
    struttura dei file come li legge Home Assistant."""
    traduzioni = await async_get_translations(hass, lingua, "entity", {DOMAIN})
    assert traduzioni[f"component.{DOMAIN}.entity.sensor.prelievo_ultimo_giorno.name"] == nome
