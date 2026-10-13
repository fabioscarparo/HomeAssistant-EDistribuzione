"""Fix of the blocked-access notices, and the diagnostics attached to bug
reports."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import diagnostics, repairs
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    ISSUE_APP_BLOCCATA,
)

from .conftest import POD_A, POD_B


def _entry(hass, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_REFRESH_TOKEN: "rt", CONF_PODS: [POD_A], **data},
        title=POD_A,
    )
    entry.add_to_hass(hass)
    return entry


async def test_la_correzione_avvia_la_riautenticazione_dei_soli_bloccati(hass):
    bloccata, libera = _entry(hass), _entry(hass)
    hass.data[DOMAIN] = {
        bloccata.entry_id: Mock(accesso_bloccato=True),
        libera.entry_id: Mock(accesso_bloccato=False),
    }
    flusso = await repairs.async_create_fix_flow(hass, ISSUE_APP_BLOCCATA, None)
    flusso.hass = hass

    with patch.object(MockConfigEntry, "async_start_reauth") as reauth:
        res = await flusso.async_step_init()
        assert res["type"] == FlowResultType.FORM
        assert res["step_id"] == "confirm"
        reauth.assert_not_called()

        res = await flusso.async_step_confirm({})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    reauth.assert_called_once_with(hass)


async def test_diagnostica_senza_token_e_con_i_pod_anonimizzati(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(
        pods=[POD_A, POD_B],
        data={
            CONF_ACCESS_TOKEN: "SEGRETO-ACCESSO",
            CONF_REFRESH_TOKEN: "SEGRETO-RINNOVO",
            CONF_GIORNI_DA_RIPROVARE: {POD_B: {"2026-10-01": "2026-10-02"}},
        },
        options={CONF_TIPO_POD: {POD_A: "scambio"}},
    )
    coordinator.ultimo_errore_accesso = f"AppBloccata: app_blocked ({POD_A})"
    coordinator.riepilogo = {POD_A: {"ultima_data_disponibile": "2026-10-12"}}
    hass.data[DOMAIN] = {coordinator.entry.entry_id: coordinator}

    with patch(
        "custom_components.edistribuzione.diagnostics.async_get_integration",
        AsyncMock(return_value=Mock(version="0.8.0")),
    ):
        dati = await diagnostics.async_get_config_entry_diagnostics(hass, coordinator.entry)

    testo = json.dumps(dati, default=str)
    assert "SEGRETO" not in testo
    assert POD_A not in testo and POD_B not in testo
    assert dati["entry"]["data"][CONF_PODS] == ["POD_1", "POD_2"]
    assert dati["entry"]["data"][CONF_GIORNI_DA_RIPROVARE] == {"POD_2": {"2026-10-01": "2026-10-02"}}
    assert dati["entry"]["options"][CONF_TIPO_POD] == {"POD_1": "scambio"}
    assert dati["accesso"]["access_token_salvato"] is True
    assert dati["accesso"]["ultimo_errore"] == "AppBloccata: app_blocked (POD_1)"
    assert dati["coordinator"]["riepilogo"] == {"POD_1": {"ultima_data_disponibile": "2026-10-12"}}
