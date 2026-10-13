"""Italian/English translations: translations/*.json and testi.py.

A key present in one language and not in the other breaks nothing at runtime
(HA falls back to English, or shows the raw key), so it is exactly the kind of
mistake nobody notices: these tests catch it first.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.helpers.translation import async_get_translations

from custom_components.edistribuzione import config_flow, sensor, testi
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
    assert not re.search("[\u2013\u2014]", testo)


def test_ogni_translation_key_del_codice_esiste():
    """The keys used in the code (sensors, exceptions, selectors) must exist
    in the translation files, otherwise the UI shows the key."""
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
        set(it["entity"]["sensor"])
        | set(it["exceptions"])
        | set(it["selector"])
        | set(it["issues"])
    )
    assert usate, "nessuna translation_key trovata nel codice: regex da aggiornare"
    assert usate <= definite, usate - definite


def test_ogni_errore_e_motivo_dei_flussi_esiste():
    """Errors and abort reasons of the config and options flows: a missing
    key shows the raw key in the form instead of the message."""
    sorgente = (CARTELLA / "config_flow.py").read_text(encoding="utf-8")
    usate = set(re.findall(r'errors\["base"\] = "(\w+)"', sorgente))
    usate |= set(re.findall(r'"base": "(\w+)"', sorgente))
    usate |= set(re.findall(r'reason="(\w+)"', sorgente))
    usate |= set(config_flow._ERRORI_RIFIUTO_LOGIN.values()) | {"login_rifiutato"}
    usate |= set(config_flow._ERRORI_LOGIN_BROWSER.values())
    it = _carica("it")
    definite = (
        set(it["config"]["error"])
        | set(it["config"]["abort"])
        | set(it["options"]["error"])
        | set(it["options"]["abort"])
    )
    assert len(usate) > 20, "regex to update: too few keys found in config_flow.py"
    assert usate <= definite, usate - definite


def test_ogni_errore_dell_azione_esiste():
    """Errors of the recupera_storico action raised through _errore() and the
    coordinator's period check, including the keys built from the field name."""
    sorgente = "\n".join(
        (CARTELLA / nome).read_text(encoding="utf-8") for nome in ("__init__.py", "coordinator.py")
    )
    usate = set(re.findall(r'_?errore\(\s*"(\w+)"', sorgente))
    usate |= set(re.findall(r'"(dispositivo_\w+)"', sorgente))
    usate |= {f"{campo}_{esito}" for campo in ("data_da", "data_a") for esito in ("mancante", "non_valida")}
    definite = set(_carica("it")["exceptions"])
    assert {"pod_non_configurato", "intervallo_troppo_ampio"} <= usate, "regex to update"
    assert usate <= definite, usate - definite


def test_gli_avvisi_correggibili_hanno_il_passo_di_conferma():
    """repairs.py shows the "confirm" step for both blocked-access notices."""
    for lingua in LINGUE:
        avvisi = _carica(lingua)["issues"]
        for chiave in ("accesso_bloccato", "app_bloccata"):
            passo = avvisi[chiave]["fix_flow"]["step"]["confirm"]
            assert passo["title"] and passo["description"], (lingua, chiave)


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
    """HA's real loader, not just the JSON: checks path and structure of the
    files as Home Assistant reads them."""
    traduzioni = await async_get_translations(hass, lingua, "entity", {DOMAIN})
    assert traduzioni[f"component.{DOMAIN}.entity.sensor.prelievo_ultimo_giorno.name"] == nome
