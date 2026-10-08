"""Rinnovo del token bloccato dalla verifica antibot di E-Distribuzione:
errore tradotto e avviso in Impostazioni > Riparazioni, che sparisce da solo
quando l'accesso torna."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione.auth import AccessoBloccato, AuthError
from custom_components.edistribuzione.const import (
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
)

from .conftest import POD_A


async def test_token_bloccato_avvisa_e_l_avviso_sparisce_quando_torna(
    hass, make_edist_coordinator
):
    coordinator = make_edist_coordinator()
    registro = ir.async_get(hass)

    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=AccessoBloccato("antibot")
    )
    with pytest.raises(UpdateFailed) as errore:
        await coordinator._async_ensure_token()
    assert errore.value.translation_key == "accesso_bloccato"
    avviso = registro.async_get_issue(DOMAIN, ISSUE_ACCESSO_BLOCCATO)
    assert avviso is not None
    assert avviso.translation_key == ISSUE_ACCESSO_BLOCCATO
    assert not avviso.is_fixable

    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="nuovo", refresh_token="rt")
    )
    await coordinator._async_ensure_token()
    assert registro.async_get_issue(DOMAIN, ISSUE_ACCESSO_BLOCCATO) is None
    assert coordinator.entry.data["refresh_token"] == "rt"


async def _setup_con_rinnovo(hass, effetto):
    """Setup completo dell'integrazione con il rinnovo del token simulato."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: [POD_A]}, title=POD_A
    )
    entry.add_to_hass(hass)
    auth = Mock()
    auth.async_refresh_access_token = AsyncMock(side_effect=effetto)
    with patch("custom_components.edistribuzione.coordinator.AuthClient", return_value=auth):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, auth


async def test_setup_bloccato_si_completa_senza_tentativi_ravvicinati(hass):
    """Con il blocco antibot all'avvio il setup non va in SETUP_RETRY, che
    ripeterebbe la richiesta fino a ogni 10 minuti: si completa e il
    coordinator riprova al suo ritmo orario."""
    entry, auth = await _setup_con_rinnovo(hass, AccessoBloccato("antibot"))
    assert entry.state is ConfigEntryState.LOADED
    assert auth.async_refresh_access_token.await_count == 1
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ACCESSO_BLOCCATO) is not None
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.accesso_bloccato
    assert coordinator.update_interval.total_seconds() == 3600


async def test_altri_errori_all_avvio_restano_un_nuovo_tentativo_di_setup(hass):
    entry, _ = await _setup_con_rinnovo(hass, AuthError("refresh_token revocato"))
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert ir.async_get(hass).async_get_issue(DOMAIN, ISSUE_ACCESSO_BLOCCATO) is None
