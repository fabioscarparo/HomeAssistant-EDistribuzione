"""Card Lovelace del POD, servita dall'integrazione stessa.

Il file JS sta in frontend/ dentro il pacchetto dell'integrazione: lo si
espone come percorso statico e lo si registra come modulo extra del
frontend, così la card compare nel selettore delle card senza dover
aggiungere a mano una risorsa Lovelace (né installare un secondo repository
HACS di tipo "plugin").

Il parametro ?v=<versione del manifest> cambia a ogni release: i browser
(e l'app companion) scaricano la nuova card invece di tenere quella in cache.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NOME_FILE = "edistribuzione-pod-card.js"
URL_STATICO = f"/{DOMAIN}_static/{NOME_FILE}"
PERCORSO_FILE = Path(__file__).parent / "frontend" / NOME_FILE

_CHIAVE_REGISTRATA = f"{DOMAIN}_card_registrata"


async def async_registra_card(hass: HomeAssistant) -> None:
    """Registra percorso statico e modulo frontend, una sola volta per avvio.

    Non fa nulla se http o frontend non sono attivi (es. nei test, o in
    un'installazione senza interfaccia): la card è un di più, non deve mai
    impedire il setup dell'integrazione.
    """
    if hass.data.get(_CHIAVE_REGISTRATA):
        return
    if hass.http is None or "frontend" not in hass.config.components:
        _LOGGER.debug("http/frontend non attivi: card Lovelace non registrata")
        return

    # Import locali: frontend/http sono after_dependencies, non dipendenze
    # dure, e importarli in cima trascinerebbe il pacchetto del frontend
    # anche dove non serve.
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    integrazione = await async_get_integration(hass, DOMAIN)
    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_STATICO, str(PERCORSO_FILE), cache_headers=True)]
    )
    add_extra_js_url(hass, f"{URL_STATICO}?v={integrazione.version}")
    hass.data[_CHIAVE_REGISTRATA] = True
    _LOGGER.debug("Card Lovelace registrata su %s", URL_STATICO)
