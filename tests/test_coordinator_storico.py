"""Outcomes of the recupera_storico action.

The action is manual and launched from the UI: if it imports nothing it must
fail visibly, not end with an apparent success and a WARNING in the logs.

Each real call to async_get_daily_load_profile happens once per direction
(MAGNITUDE_TUTTE = consumption, injection): the mocks below do not tell the
magnitude apart unless the test explicitly needs it (see
TestGuardiaEnergyType), so by default they return the same curve for both
calls.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun import freeze_time
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)

from custom_components.edistribuzione.api import ApiError
from custom_components.edistribuzione.auth import TokenNonValido
from custom_components.edistribuzione.const import CONF_ACCESS_TOKEN, DOMAIN, MAGNITUDE_PRELEVATA

from .conftest import POD_A, POD_B

OGGI = "2026-09-15 12:00:00"
DATA_DA = date(2026, 9, 10)
DATA_A = date(2026, 9, 12)


def _curva(giorni: list[date], *, energy_type: str | None = None) -> list[dict]:
    """async_get_daily_load_profile response with samples for those days."""
    readings = {"sampleValues": [{"id": "1", "val": "0.5"}]}
    if energy_type is not None:
        readings["energyType"] = energy_type
    return [{"readings": {**readings, "sampleDate": g.strftime("%Y%m%d")}} for g in giorni]


@pytest.fixture(autouse=True)
async def _fuso_utc(hass):
    await hass.config.async_set_time_zone("UTC")


@pytest.fixture
def _senza_token(monkeypatch):
    """recupera_storico refreshes the token before calling the API: not
    relevant here, the tests are about the outcome of the fetch."""
    from custom_components.edistribuzione import coordinator as mod

    monkeypatch.setattr(
        mod.EdistribuzioneCoordinator, "_async_ensure_token", AsyncMock(return_value=None)
    )


@pytest.fixture
def _import_statistiche():
    """Neutralises writing the external statistics (it needs the recorder):
    the tests check the outcome of the action, not the import."""
    with patch(
        "custom_components.edistribuzione.coordinator.async_import_curva_giornaliera",
        AsyncMock(return_value=None),
    ) as mock:
        yield mock


async def test_errore_api_su_tutti_i_pod_fa_fallire_l_azione(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=ApiError("403 dal distributore")
    )

    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"
    assert "403 dal distributore" in errore.value.translation_placeholders["dettagli"]


async def test_risposta_vuota_fa_fallire_l_azione(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(return_value=[])

    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"


async def test_risposta_senza_misure_fa_fallire_l_azione(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    """There is a response but no day contains samples: for whoever ran the
    action it is the same as importing nothing, in both directions."""
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=[{"readings": {"sampleDate": "20260910", "sampleValues": []}}]
    )

    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"


async def test_primo_giorno_vuoto_non_scarta_gli_altri(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    """Issue #9: a first day without samples (before the meter's activation,
    or not published yet) used to discard the whole period."""
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=[{"readings": {"sampleDate": "20260910", "sampleValues": []}}]
        + _curva([date(2026, 9, 11), DATA_A])
    )

    with freeze_time(OGGI):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    assert _import_statistiche.await_count == 2


async def test_successo_importa_entrambe_le_direzioni(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=_curva([DATA_DA, DATA_A])
    )

    with freeze_time(OGGI):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    # Once for consumption, once for injection.
    assert _import_statistiche.await_count == 2
    chiamate_immessa = [
        c for c in _import_statistiche.await_args_list if c.kwargs.get("immessa") is True
    ]
    assert len(chiamate_immessa) == 1


async def test_fallimento_parziale_resta_un_successo(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    """With several PODs, if at least one imported something the action
    succeeds: something WAS imported, and the failed POD stays in the logs."""
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])

    async def _per_pod(pod, data_da, data_a, magnitude=None):
        if pod == POD_A:
            raise ApiError("timeout")
        return _curva([data_da])

    coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=_per_pod)

    with freeze_time(OGGI):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)

    assert _import_statistiche.await_count > 0


class TestGuardiaEnergyType:
    """If the server silently ignores the requested magnitude and always
    answers with the same energyType, the direction not honoured must not be
    imported - it would duplicate consumption into the injection series."""

    async def test_immessa_scartata_se_energy_type_non_corrisponde(
        self, hass, make_edist_coordinator, _senza_token, _import_statistiche
    ):
        coordinator = make_edist_coordinator(pods=[POD_A])

        async def _per_magnitude(pod, data_da, data_a, magnitude=None):
            # The server always answers with consumption, whatever the
            # requested magnitude - simulates an ignored parameter.
            return _curva([data_da], energy_type=MAGNITUDE_PRELEVATA)

        coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=_per_magnitude)

        with freeze_time(OGGI):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

        # Only consumption (matching energyType) is imported.
        assert _import_statistiche.await_count == 1
        assert _import_statistiche.await_args_list[0].kwargs.get("immessa") is not True

    async def test_entrambe_importate_se_energy_type_corrisponde(
        self, hass, make_edist_coordinator, _senza_token, _import_statistiche
    ):
        coordinator = make_edist_coordinator(pods=[POD_A])

        async def _per_magnitude(pod, data_da, data_a, magnitude=None):
            return _curva([data_da], energy_type=magnitude)

        coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=_per_magnitude)

        with freeze_time(OGGI):
            await coordinator.async_recupera_storico(DATA_DA, DATA_A)

        assert _import_statistiche.await_count == 2


# --- Requested period: every error says what to change -------------------------


@pytest.mark.parametrize(
    ("data_da", "data_a", "chiave", "segnaposto"),
    [
        # Swapped: the swap is suggested, not "date too recent".
        (date(2026, 9, 20), date(2026, 9, 1), "date_invertite", {}),
        (date(2026, 9, 15), date(2026, 9, 15), "data_inizio_troppo_recente", {}),
        (date(2026, 9, 1), date(2026, 9, 15), "data_fine_troppo_recente", {}),
        (
            date(2026, 1, 1),
            date(2026, 9, 14),
            "intervallo_troppo_ampio",
            {"giorni": "257", "massimo": "190", "fine_prima_parte": "2026-07-09"},
        ),
    ],
)
async def test_periodo_non_valido(
    hass, make_edist_coordinator, _senza_token, data_da, data_a, chiave, segnaposto
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    with freeze_time(OGGI), pytest.raises(ServiceValidationError) as errore:
        await coordinator.async_recupera_storico(data_da, data_a)
    assert errore.value.translation_key == chiave
    attesi = {
        "data_da": data_da.isoformat(),
        "data_a": data_a.isoformat(),
        "ultimo_utile": "2026-09-14",
        **segnaposto,
    }
    assert errore.value.translation_placeholders == attesi


async def test_ieri_e_l_ultimo_giorno_accettato(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=_curva([date(2026, 9, 14)])
    )
    with freeze_time(OGGI):
        await coordinator.async_recupera_storico(date(2026, 9, 14), date(2026, 9, 14))


@pytest.mark.parametrize(
    ("errore_api", "atteso"),
    [
        (ApiError("HTTP 503", status=503), "non risponde (HTTP 503)"),
        (ApiError("HTTP 401", status=401), "rifiutato la richiesta dei dati (HTTP 401)"),
        (ApiError("HTTP 400", status=400), "rifiutato la richiesta dei dati (HTTP 400)"),
        (ApiError("ClientConnectorError", rete=True), "non è raggiungibile"),
    ],
)
async def test_errori_del_servizio_dati_spiegati(
    hass, make_edist_coordinator, _senza_token, _import_statistiche, errore_api, atteso
):
    hass.config.language = "it"
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(side_effect=errore_api)
    with freeze_time(OGGI), pytest.raises(HomeAssistantError) as errore:
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    assert errore.value.translation_key == "nessun_dato_importato"
    assert f"{POD_A}: " in errore.value.translation_placeholders["dettagli"]
    assert atteso in errore.value.translation_placeholders["dettagli"]


async def test_accesso_scaduto_durante_l_azione_apre_la_riautenticazione(
    hass, make_edist_coordinator
):
    """Outside the polling cycle Home Assistant does not start the reauth on
    its own: without it, the message would point to a missing button."""
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=TokenNonValido("invalid_grant")
    )
    with freeze_time(OGGI), pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    await hass.async_block_till_done()
    flussi = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flussi] == ["reauth"]


async def test_dopo_l_import_i_sensori_si_aggiornano_subito(
    hass, make_edist_coordinator, _senza_token, _import_statistiche
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        return_value=_curva([DATA_DA, DATA_A])
    )
    ascoltatore = Mock()
    coordinator.async_add_listener(ascoltatore)
    with (
        freeze_time(OGGI),
        patch.object(coordinator, "async_aggiorna_riepilogo", AsyncMock()) as riepilogo,
    ):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    riepilogo.assert_awaited_once()
    ascoltatore.assert_called()


async def test_accesso_scaduto_durante_l_import_apre_la_riautenticazione(
    hass, make_edist_coordinator
):
    """The saved token is rejected during the fetch and the refresh says the
    access has expired: the reauth must start here too."""
    coordinator = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "salvato"})
    coordinator._api.async_get_daily_load_profile = AsyncMock(
        side_effect=ApiError("401", status=401)
    )
    coordinator._auth.async_refresh_access_token = AsyncMock(
        side_effect=TokenNonValido("invalid_grant")
    )
    with freeze_time(OGGI), pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_recupera_storico(DATA_DA, DATA_A)
    await hass.async_block_till_done()
    flussi = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flussi] == ["reauth"]
