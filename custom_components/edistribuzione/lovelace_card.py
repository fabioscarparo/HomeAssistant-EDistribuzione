"""POD Lovelace card served directly by the integration."""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NOME_FILE = "edistribuzione-pod-card.js"
URL_STATICO = f"/{DOMAIN}_static/{NOME_FILE}"
PERCORSO_FILE = Path(__file__).parent / "frontend" / NOME_FILE

_CHIAVE_REGISTRATA = f"{DOMAIN}_card_registrata"
_CHIAVE_IMPRONTA = f"{DOMAIN}_card_impronta"
_CHIAVE_LOCK = f"{DOMAIN}_card_lock"


def impronta_file(percorso: Path = PERCORSO_FILE) -> str:
    """First 12 hex digits of the card file's SHA-256."""
    return hashlib.sha256(percorso.read_bytes()).hexdigest()[:12]


def stato_card(hass: HomeAssistant) -> dict[str, Any]:
    """Current Lovelace card registration state for diagnostics."""
    return {
        "registrata": bool(hass.data.get(_CHIAVE_REGISTRATA)),
        "url": URL_STATICO,
        "impronta": hass.data.get(_CHIAVE_IMPRONTA),
    }


async def async_registra_card(hass: HomeAssistant) -> None:
    """Register the static path and frontend module once per HA startup."""
    if hass.data.get(_CHIAVE_REGISTRATA):
        return
    if hass.http is None or "frontend" not in hass.config.components:
        _LOGGER.debug("http/frontend non attivi: card Lovelace non registrata")
        return

    lock: asyncio.Lock = hass.data.setdefault(_CHIAVE_LOCK, asyncio.Lock())
    async with lock:
        if hass.data.get(_CHIAVE_REGISTRATA):
            return

        # Local imports: frontend/http are after_dependencies, not hard requirements.
        from homeassistant.components.frontend import add_extra_js_url
        from homeassistant.components.http import StaticPathConfig

        impronta = await hass.async_add_executor_job(impronta_file)
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_STATICO, str(PERCORSO_FILE), cache_headers=True)]
        )
        add_extra_js_url(hass, f"{URL_STATICO}?v={impronta}")
        hass.data[_CHIAVE_IMPRONTA] = impronta
        hass.data[_CHIAVE_REGISTRATA] = True
        _LOGGER.debug("Card Lovelace registrata su %s", URL_STATICO)
