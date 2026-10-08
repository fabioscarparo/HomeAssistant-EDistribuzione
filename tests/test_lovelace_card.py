"""Registrazione della card Lovelace servita dall'integrazione."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.edistribuzione import lovelace_card


def test_il_file_della_card_esiste_e_definisce_la_card():
    contenuto = lovelace_card.PERCORSO_FILE.read_text(encoding="utf-8")
    assert 'const CARD_TYPE = "edistribuzione-pod-card"' in contenuto
    assert "registro.define(CARD_TYPE" in contenuto


def test_la_card_aspetta_il_registro_definitivo_di_home_assistant():
    """Regressione verificata su HA 2026.2: i moduli extra possono girare
    prima che il frontend installi il suo registro dei custom element. Un
    define immediato finisce nel registro sbagliato e la card risulta
    sconosciuta ("Errore di configurazione")."""
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
    integrazione = MagicMock(version="9.9.9")

    with (
        patch.object(lovelace_card, "async_get_integration", AsyncMock(return_value=integrazione)),
        patch("homeassistant.components.frontend.add_extra_js_url") as add_url,
    ):
        await lovelace_card.async_registra_card(hass)
        await lovelace_card.async_registra_card(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    (config,) = hass.http.async_register_static_paths.await_args.args[0]
    assert config.url_path == lovelace_card.URL_STATICO
    assert config.path == str(lovelace_card.PERCORSO_FILE)
    add_url.assert_called_once_with(hass, f"{lovelace_card.URL_STATICO}?v=9.9.9")
