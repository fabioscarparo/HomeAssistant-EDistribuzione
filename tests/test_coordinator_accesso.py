"""Token refresh: blocked by E-Distribuzione (anti-bot check or OAuth client
rejected), with a translated error and a notice in Settings > Repairs that goes
away on its own once access is back; expired, with a request to sign in
again."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione.api import ApiError
from custom_components.edistribuzione.auth import (
    AccessoBloccato,
    AppBloccata,
    AuthError,
    TokenNonValido,
)
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_CLIENT_ID_OAUTH,
    CONF_INSTANCE_URL,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
    ISSUE_APP_BLOCCATA,
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_ID_PRECEDENTE,
    issue_id_per_entry,
)

from .conftest import POD_A


async def test_access_token_browser_non_richiede_refresh(make_edist_coordinator):
    coordinator = make_edist_coordinator(
        data={
            CONF_ACCESS_TOKEN: "access-browser",
            CONF_INSTANCE_URL: "https://example.my.salesforce.com",
        }
    )
    coordinator._auth.async_refresh_access_token = AsyncMock()
    coordinator._api.update_token = Mock()

    await coordinator._async_ensure_token()

    coordinator._auth.async_refresh_access_token.assert_not_awaited()
    coordinator._api.update_token.assert_called_once_with("access-browser")


async def test_401_rinnova_token_e_riprova_la_richiesta(make_edist_coordinator):
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "access-scaduto"})
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=[ApiError("401", status=401), [{"sample": "lettura"}]]
    )
    coordinator._api.update_token = Mock()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="access-nuovo", refresh_token="rt")
    )

    risultato = await coordinator._async_get_daily_load_profile(
        POD_A, date(2026, 10, 1), date(2026, 10, 1), "A1"
    )

    assert risultato == [{"sample": "lettura"}]
    coordinator._auth.async_refresh_access_token.assert_awaited_once_with(
        "rt", OAUTH_CLIENT_ID_PRECEDENTE
    )
    coordinator._api.update_token.assert_called_once_with("access-nuovo")
    assert coordinator._api.async_get_daily_load_profile.await_count == 2


async def test_refresh_usa_client_id_salvato_sulla_entry(make_edist_coordinator):
    coordinator = make_edist_coordinator(data={CONF_CLIENT_ID_OAUTH: OAUTH_CLIENT_ID})
    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="access-nuovo", refresh_token="rt")
    )
    await coordinator._async_ensure_token()
    coordinator._auth.async_refresh_access_token.assert_awaited_once_with("rt", OAUTH_CLIENT_ID)


async def test_il_token_salvato_non_cancella_l_avviso_di_blocco(hass, make_edist_coordinator):
    """Reusing the saved token says nothing about whether access works: the
    notice must stay until a refresh or a data request succeeds."""
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "salvato"})
    coordinator._segnala_blocco(ISSUE_ACCESSO_BLOCCATO)
    await coordinator._async_ensure_token()
    assert coordinator.accesso_bloccato
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, coordinator.entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None


async def test_una_richiesta_dati_riuscita_toglie_l_avviso(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "salvato"})
    coordinator._segnala_blocco(ISSUE_APP_BLOCCATA)
    coordinator._api.async_get_daily_load_profile = AsyncMock(return_value=[])
    await coordinator._async_get_daily_load_profile(POD_A, date(2026, 10, 1), date(2026, 10, 1), "A1")
    assert not coordinator.accesso_bloccato
    issue_id = issue_id_per_entry(ISSUE_APP_BLOCCATA, coordinator.entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_anche_il_403_rinnova_il_token(make_edist_coordinator):
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "access-scaduto"})
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=[ApiError("403", status=403), []]
    )
    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="nuovo", refresh_token="rt")
    )
    await coordinator._async_get_daily_load_profile(POD_A, date(2026, 10, 1), date(2026, 10, 1), "A1")
    coordinator._auth.async_refresh_access_token.assert_awaited_once()
    assert coordinator.entry.data[CONF_ACCESS_TOKEN] == "nuovo"


async def test_token_salvato_scaduto_e_rinnovo_bloccato(hass, make_edist_coordinator):
    """The case the browser login leaves behind once its access token expires
    while E-Distribuzione still blocks the refresh: notice, data kept."""
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "scaduto"})
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=ApiError("401", status=401)
    )
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=AppBloccata("app_blocked")
    )
    with pytest.raises(UpdateFailed) as errore:
        await coordinator._async_get_daily_load_profile(
            POD_A, date(2026, 10, 1), date(2026, 10, 1), "A1"
        )
    assert errore.value.translation_key == "app_bloccata"
    assert coordinator.accesso_bloccato
    issue_id = issue_id_per_entry(ISSUE_APP_BLOCCATA, coordinator.entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id).is_fixable
    assert coordinator.entry.data[CONF_ACCESS_TOKEN] == "scaduto"


async def test_token_bloccato_avvisa_e_l_avviso_sparisce_quando_torna(
    hass, make_edist_coordinator
):
    coordinator = make_edist_coordinator()
    registro = ir.async_get(hass)
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, coordinator.entry.entry_id)

    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=AccessoBloccato("antibot")
    )
    with pytest.raises(UpdateFailed) as errore:
        await coordinator._async_ensure_token()
    assert errore.value.translation_key == "accesso_bloccato"
    avviso = registro.async_get_issue(DOMAIN, issue_id)
    assert avviso is not None
    assert avviso.translation_key == ISSUE_ACCESSO_BLOCCATO
    assert avviso.is_fixable
    assert coordinator.ultimo_errore_accesso.endswith("AccessoBloccato: antibot")

    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="nuovo", refresh_token="rt")
    )
    await coordinator._async_ensure_token()
    assert registro.async_get_issue(DOMAIN, issue_id) is None
    assert coordinator.entry.data["refresh_token"] == "rt"


async def test_client_rifiutato_avvisa_senza_chiedere_un_nuovo_login(
    hass, make_edist_coordinator
):
    """Issue #4: 'app_blocked' on refresh creates a dedicated notice without reauth."""
    coordinator = make_edist_coordinator()
    registro = ir.async_get(hass)
    issue_app = issue_id_per_entry(ISSUE_APP_BLOCCATA, coordinator.entry.entry_id)
    issue_acc = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, coordinator.entry.entry_id)

    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=AppBloccata("app_blocked")
    )
    with pytest.raises(UpdateFailed) as errore:
        await coordinator._async_ensure_token()
    assert errore.value.translation_key == "app_bloccata"
    assert coordinator.accesso_bloccato
    assert registro.async_get_issue(DOMAIN, issue_app) is not None
    assert coordinator.entry.data[CONF_REFRESH_TOKEN] == "rt"

    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=AccessoBloccato("antibot")
    )
    with pytest.raises(UpdateFailed):
        await coordinator._async_ensure_token()
    assert registro.async_get_issue(DOMAIN, issue_app) is None
    assert registro.async_get_issue(DOMAIN, issue_acc) is not None

    coordinator._auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="nuovo", refresh_token="rt")
    )
    await coordinator._async_ensure_token()
    assert not coordinator.accesso_bloccato
    assert registro.async_get_issue(DOMAIN, issue_acc) is None
    assert registro.async_get_issue(DOMAIN, issue_app) is None


async def test_token_scaduto_chiede_un_nuovo_login(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=TokenNonValido("invalid_grant")
    )
    with pytest.raises(ConfigEntryAuthFailed) as errore:
        await coordinator._async_ensure_token()
    assert errore.value.translation_key == "accesso_scaduto"


async def test_portale_non_raggiungibile_ha_un_messaggio_suo(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator()
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=aiohttp.ClientConnectionError("dns")
    )
    with pytest.raises(UpdateFailed) as errore:
        await coordinator._async_ensure_token()
    assert errore.value.translation_key == "portale_non_raggiungibile"
    assert errore.value.translation_placeholders == {"errore": "ClientConnectionError"}


async def _setup_con_rinnovo(hass, effetto):
    """Full setup of the integration with a simulated token refresh."""
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
    """With the anti-bot block at startup the setup completes and retries hourly."""
    entry, auth = await _setup_con_rinnovo(hass, AccessoBloccato("antibot"))
    assert entry.state is ConfigEntryState.LOADED
    assert auth.async_refresh_access_token.await_count == 1
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.accesso_bloccato
    assert coordinator.update_interval.total_seconds() == 3600


async def test_client_rifiutato_all_avvio_completa_il_setup(hass):
    entry, auth = await _setup_con_rinnovo(hass, AppBloccata("app_blocked"))
    assert entry.state is ConfigEntryState.LOADED
    assert auth.async_refresh_access_token.await_count == 1
    issue_id = issue_id_per_entry(ISSUE_APP_BLOCCATA, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


async def test_token_scaduto_all_avvio_apre_la_riautenticazione(hass):
    entry, _ = await _setup_con_rinnovo(hass, TokenNonValido("invalid_grant"))
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flussi = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flussi] == ["reauth"]
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_altri_errori_all_avvio_restano_un_nuovo_tentativo_di_setup(hass):
    entry, _ = await _setup_con_rinnovo(hass, AuthError("errore 500"))
    assert entry.state is ConfigEntryState.SETUP_RETRY
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_avviso_ignorato_ricompare_al_ricaricamento_se_il_blocco_continua(hass):
    """After a reload with access still blocked, an ignored notice reappears."""
    entry, auth = await _setup_con_rinnovo(hass, AccessoBloccato("antibot"))
    registro = ir.async_get(hass)
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, entry.entry_id)
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    assert registro.async_get_issue(DOMAIN, issue_id).dismissed_version

    with patch("custom_components.edistribuzione.coordinator.AuthClient", return_value=auth):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    avviso = registro.async_get_issue(DOMAIN, issue_id)
    assert avviso is not None
    assert avviso.dismissed_version is None


async def test_rimuovendo_l_integrazione_sparisce_anche_l_avviso(hass):
    entry, _ = await _setup_con_rinnovo(hass, AccessoBloccato("antibot"))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, entry.entry_id)
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_all_avvio_ricostruisce_le_serie_anche_con_l_accesso_bloccato(hass):
    """The rebuild comes from the local database: it runs before the first
    refresh, so the new series appear even while E-Distribuzione is blocked."""
    with patch(
        "custom_components.edistribuzione.coordinator.EdistribuzioneCoordinator"
        ".async_ricostruisci_serie_mancanti",
        AsyncMock(),
    ) as ricostruisci:
        entry, _ = await _setup_con_rinnovo(hass, AccessoBloccato("antibot"))
    assert entry.state is ConfigEntryState.LOADED
    ricostruisci.assert_awaited_once()
