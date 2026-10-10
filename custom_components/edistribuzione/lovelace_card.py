"""POD Lovelace card, served by the integration itself.

The JS file lives in frontend/ inside the integration package: it is exposed as
a static path and registered as an extra frontend module, so the card shows up
in the card picker without adding a Lovelace resource by hand (nor installing a
second "plugin" HACS repository).

The file is served with cache_headers=True, i.e. cached for 31 days: the
?v=<content hash> parameter changes as soon as the file changes, so browsers and
the companion app download the new card. Tying it to the manifest version was
not enough: a commit without a version bump (the fork publishes no releases)
left the previous card cached.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NOME_FILE = "edistribuzione-pod-card.js"
URL_STATICO = f"/{DOMAIN}_static/{NOME_FILE}"
PERCORSO_FILE = Path(__file__).parent / "frontend" / NOME_FILE

_CHIAVE_REGISTRATA = f"{DOMAIN}_card_registrata"


def impronta_file(percorso: Path = PERCORSO_FILE) -> str:
    """First 12 hex digits of the card file's SHA-256."""
    return hashlib.sha256(percorso.read_bytes()).hexdigest()[:12]


async def async_registra_card(hass: HomeAssistant) -> None:
    """Register the static path and frontend module, once per start.

    Does nothing if http or frontend are not active (e.g. in tests, or in a
    headless install): the card is a bonus, it must never block the integration
    setup.
    """
    if hass.data.get(_CHIAVE_REGISTRATA):
        return
    if hass.http is None or "frontend" not in hass.config.components:
        _LOGGER.debug("http/frontend non attivi: card Lovelace non registrata")
        return

    # Local imports: frontend/http are after_dependencies, not hard
    # dependencies, and importing them at the top would pull in the frontend
    # package even where it is not needed.
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    impronta = await hass.async_add_executor_job(impronta_file)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_STATICO, str(PERCORSO_FILE), cache_headers=True)]
    )
    add_extra_js_url(hass, f"{URL_STATICO}?v={impronta}")
    hass.data[_CHIAVE_REGISTRATA] = True
    _LOGGER.debug("Card Lovelace registrata su %s", URL_STATICO)
