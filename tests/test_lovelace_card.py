"""Registration of the Lovelace card served by the integration."""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.edistribuzione import lovelace_card


def test_il_file_della_card_esiste_e_definisce_la_card():
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert 'const CARD_TYPE = "edistribuzione-pod-card"' in contenuto
    assert "registro.define(CARD_TYPE" in contenuto


def test_la_card_aspetta_il_registro_definitivo_di_home_assistant():
    """Regression seen on HA 2026.2: extra modules can run before the
    frontend installs its custom element registry. An immediate define ends
    up in the wrong registry and the card is unknown ("Configuration
    error")."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert 'whenDefined("home-assistant").then(registraElementi)' in contenuto
    assert "customElements.define(CARD_TYPE" not in contenuto


async def test_senza_http_non_registra_nulla(hass):
    hass.http = None
    await lovelace_card.async_registra_card(hass)
    assert not hass.data.get(lovelace_card._CHIAVE_REGISTRATA)


async def test_registra_percorso_statico_e_modulo_una_sola_volta(hass):
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    hass.config.components.add("frontend")

    with patch("homeassistant.components.frontend.add_extra_js_url") as add_url:
        await lovelace_card.async_registra_card(hass)
        await lovelace_card.async_registra_card(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    (config,) = hass.http.async_register_static_paths.await_args.args[0]
    assert config.url_path == lovelace_card.URL_STATICO
    assert config.path == str(lovelace_card.PERCORSO_FILE)
    add_url.assert_called_once_with(
        hass, f"{lovelace_card.URL_STATICO}?v={lovelace_card.impronta_file()}"
    )


def test_l_impronta_cambia_col_contenuto_della_card(tmp_path):
    """The card is cached for 31 days: ?v= must change on every change to
    the file, even without bumping the manifest version."""
    card = tmp_path / "card.js"
    card.write_text("console.log(1);", encoding="utf-8")
    prima = lovelace_card.impronta_file(card)
    card.write_text("console.log(2);", encoding="utf-8")
    assert lovelace_card.impronta_file(card) != prima
    assert len(prima) == 12


def test_testi_della_card_nelle_due_lingue():
    """Every card string exists in Italian and English, as in translations/."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    blocco = contenuto[contenuto.index("const STRINGS = {") : contenuto.index("\n};\n")]
    it, en = blocco.split("\n  en: {")
    chiavi = lambda testo: set(re.findall(r"^    (\w+):", testo, re.MULTILINE))  # noqa: E731
    assert chiavi(it) == chiavi(en)
    assert {"empty_title", "history_text", "fetch_history"} <= chiavi(it)


def test_la_card_porta_all_azione_recupera_storico():
    """The buttons go through the "My Home Assistant" redirects, which point
    to the right page on every version."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert "/_my_redirect/developer_call_service?service=edistribuzione.recupera_storico" in contenuto
    assert "/_my_redirect/config_flow_start?domain=edistribuzione" in contenuto


def test_la_card_legge_l_immissione_per_fascia():
    """Issue #7: the card reads the injection band series written by
    statistics.py, with the same naming."""
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    for fascia in ("f1", "f2", "f3"):
        assert f"_energia_immessa_{fascia}`" in contenuto
