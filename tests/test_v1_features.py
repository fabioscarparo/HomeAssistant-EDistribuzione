"""Tests for v1.0.0 API features: 3-host refresh, token revocation, 365-day
chunking, supply metadata & M1/M2 auto-detection, daily/monthly peak power,
official monthly summaries, ARERA band reconciliation, official billing
readings, and opt-in four-quadrant reactive energy & power factor.
"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from freezegun import freeze_time
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import (
    async_remove_entry,
    diagnostics,
    raw_storage,
    sensor,
)
from custom_components.edistribuzione.auth import AuthClient, TokenNonValido
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_ENERGIA_REATTIVA,
    CONF_INSTANCE_URL,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    MAGNITUDE_IMMESSA,
    MAGNITUDE_PRELEVATA,
    MAGNITUDE_REATTIVA_R1,
    MAGNITUDE_REATTIVA_R2,
    MAGNITUDE_REATTIVA_R3,
    MAGNITUDE_REATTIVA_R4,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)
from custom_components.edistribuzione.dati_api import (
    calcola_reattiva_e_cos_phi,
    confronta_fasce_mese,
    estrai_fornitura,
    estrai_mensili,
    estrai_ultima_lettura,
    ruolo_suggerito_da_fornitura,
)
from custom_components.edistribuzione.fasce import FUSO_ITALIA

from .conftest import POD_A, POD_B

# ---------------------------------------------------------------------------
# Helpers for fake HTTP sessions
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, text: str, status: int = 200) -> None:
        self._text = text
        self.status = status
        self.headers = {}

    async def text(self) -> str:
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _QueuedSession:
    def __init__(self, responses: list[_FakeResp | Exception]) -> None:
        self._responses = list(responses)
        self.posts: list[tuple[str, dict]] = []

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, dict(data or {})))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


# ---------------------------------------------------------------------------
# 1. Auth: 3-host refresh fallback + OAuth revocation
# ---------------------------------------------------------------------------


async def test_refresh_tries_salesforce_org_host_first_then_portal_fallback():
    """Direct Salesforce org host is tried first; falls back to custom portal domain on block."""
    session = _QueuedSession([
        _FakeResp('{"error":"app_blocked","error_description":"Transaction Security"}', 400),
        _FakeResp('{"access_token":"tok-portal","instance_url":"https://edistribuzione.my.salesforce.com"}', 200),
    ])
    client = AuthClient(session)
    tokens = await client.async_refresh_access_token("rt-1")
    assert tokens.access_token == "tok-portal"
    assert tokens.refresh_token == "rt-1"
    assert client.host_attivo == "private.e-distribuzione.it"
    assert [url for url, _ in session.posts] == [
        "https://edistribuzione.my.salesforce.com/services/oauth2/token",
        "https://private.e-distribuzione.it/PortaleClienti/services/oauth2/token",
    ]


async def test_refresh_uses_validated_custom_instance_url():
    """A valid edistribuzione*.salesforce.com instance_url is used first as direct token host."""
    session = _QueuedSession([
        _FakeResp('{"access_token":"tok-custom"}', 200),
    ])
    client = AuthClient(session, instance_url="https://edistribuzione--eu1.my.salesforce.com")
    tokens = await client.async_refresh_access_token("rt-1")
    assert tokens.access_token == "tok-custom"
    assert client.host_attivo == "edistribuzione--eu1.my.salesforce.com"
    assert session.posts[0][0] == (
        "https://edistribuzione--eu1.my.salesforce.com/services/oauth2/token"
    )


@pytest.mark.parametrize(
    "untrusted_url",
    ["https://evil.example.com", "https://attacker-org.my.salesforce.com"],
)
async def test_refresh_ignores_untrusted_instance_url(untrusted_url):
    """Untrusted or third-party Salesforce org instance_urls fall back to SF_HOST_TOKEN."""
    session = _QueuedSession([
        _FakeResp('{"access_token":"tok-safe"}', 200),
    ])
    client = AuthClient(session, instance_url=untrusted_url)
    tokens = await client.async_refresh_access_token("rt-1")
    assert tokens.access_token == "tok-safe"
    assert session.posts[0][0] == "https://edistribuzione.my.salesforce.com/services/oauth2/token"


async def test_refresh_invalid_grant_does_not_retry_other_hosts():
    """An expired/revoked refresh token fails immediately on the first host."""
    session = _QueuedSession([
        _FakeResp('{"error":"invalid_grant","error_description":"expired"}', 400),
    ])
    with pytest.raises(TokenNonValido):
        await AuthClient(session).async_refresh_access_token("rt-bad")
    assert len(session.posts) == 1


async def test_revoke_token_succeeds_on_org_host_and_falls_back():
    session = _QueuedSession([_FakeResp("", 200)])
    assert await AuthClient(session).async_revoke_token("rt-1") is True
    assert session.posts == [
        ("https://edistribuzione.my.salesforce.com/services/oauth2/revoke", {"token": "rt-1"})
    ]

    session_fallback = _QueuedSession([_FakeResp("blocked", 403), _FakeResp("", 200)])
    assert await AuthClient(session_fallback).async_revoke_token("rt-2") is True
    assert len(session_fallback.posts) == 2

    session_err = _QueuedSession([RuntimeError("offline"), RuntimeError("offline")])
    assert await AuthClient(session_err).async_revoke_token("rt-3") is False


async def test_async_remove_entry_revokes_refresh_token(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PODS: [POD_A],
            CONF_REFRESH_TOKEN: "rt-remove",
            CONF_ACCESS_TOKEN: "at-remove",
            CONF_INSTANCE_URL: "https://edistribuzione.my.salesforce.com",
        },
    )
    entry.add_to_hass(hass)
    revoke_mock = AsyncMock(return_value=True)
    with patch(
        "custom_components.edistribuzione.AuthClient",
        return_value=Mock(async_revoke_token=revoke_mock),
    ):
        await async_remove_entry(hass, entry)
    revoke_mock.assert_awaited_once_with("rt-remove")


# ---------------------------------------------------------------------------
# 2. Pure parsers (dati_api.py)
# ---------------------------------------------------------------------------


def test_estrai_fornitura_strips_pii_and_keeps_technical_fields():
    raw = {
        "IdPod": POD_A,
        "SupplyStatusId": "ATT",
        "TaxCode": "RSSMRA80A01H501U",
        "VatNumber": None,
        "PointOfMeasureStreet": "VIA ROMA",
        "PointOfMeasureMunicipality": "PADOVA",
        "PointOfMeasureIstatCode": "028060",
        "VoltageLevel": "BM",
        "ContractualPower": 4.5,
        "AvailablePower": 6.6,
        "DeductiblePower": 6.6,
        "PlantCommercialRegimeId": "R",
        "IncentivizedPlant": "CESSIONE PARZIALE INCENTIVATA",
        "MeteringPointType": "M1",
        "HasPlant": True,
    }
    parsed = estrai_fornitura(raw)
    assert parsed == {
        "potenza_contrattuale": 4.5,
        "potenza_disponibile": 6.6,
        "potenza_prelevabile": 6.6,
        "tensione": "BM",
        "stato_fornitura": "ATT",
        "tipo_punto": "M1",
        "ha_impianto": True,
        "codice_regime_impianto": "R",
        "regime_impianto": "CESSIONE PARZIALE INCENTIVATA",
    }
    assert "RSSMRA" not in str(parsed)
    assert "PADOVA" not in str(parsed)
    assert "028060" not in str(parsed)


def test_ruolo_suggerito_da_fornitura():
    assert ruolo_suggerito_da_fornitura({"MeteringPointType": "M2"}) == TIPO_POD_PRODUZIONE
    assert ruolo_suggerito_da_fornitura({"MeteringPointType": "m2"}) == TIPO_POD_PRODUZIONE
    assert ruolo_suggerito_da_fornitura({"MeteringPointType": "M1"}) == TIPO_POD_SCAMBIO
    assert ruolo_suggerito_da_fornitura({}) == TIPO_POD_SCAMBIO


def test_estrai_mensili_parses_current_and_previous_months():
    rows = [
        {
            "yearMonth": "202610",
            "readings": {
                "energyType": "A1",
                "energyT1": "5.041",
                "energyT2": "14.404",
                "energyT3": "38.141",
                "energyT4": "57.586",
                "peakPowerT1": "1.888",
                "peakPowerT2": "2.316",
                "peakPowerT3": "4.068",
                "peakPowerT4": "4.068",
            },
        },
        {
            "yearMonth": "202611",
            "readings": {
                "energyType": "A1",
                "energyT1": "120.229",
                "energyT2": "114.493",
                "energyT3": "132.072",
                "energyT4": "366.794",
                "peakPowerT1": "5.864",
                "peakPowerT2": "5.756",
                "peakPowerT3": "5.796",
                "peakPowerT4": "5.864",
            },
        },
        # Wrong magnitude must be rejected when magnitude_attesa is given.
        {
            "yearMonth": "202611",
            "readings": {"energyType": "A2", "energyT4": "999.0"},
        },
    ]
    res = estrai_mensili(rows, oggi=date(2026, 11, 19), magnitude_attesa="A1")
    assert res["mese_precedente"]["mese"] == "2026-10"
    assert res["mese_precedente"]["totale"] == 57.586
    assert res["mese_precedente"]["picco_kw"] == 4.068
    assert res["mese_corrente"]["mese"] == "2026-11"
    assert res["mese_corrente"]["totale"] == 366.794
    assert res["mese_corrente"]["picco_kw"] == 5.864


def test_calcola_reattiva_e_cos_phi():
    attiva = {"mese": "2026-10", "totale": 100.0}
    quadranti = {
        MAGNITUDE_REATTIVA_R1: {"mese": "2026-10", "totale": 10.0},
        MAGNITUDE_REATTIVA_R2: {"mese": "2026-10", "totale": 0.0},
        MAGNITUDE_REATTIVA_R3: {"mese": "2026-10", "totale": 25.5},
        MAGNITUDE_REATTIVA_R4: {"mese": "2026-10", "totale": 23.0},
    }
    res = calcola_reattiva_e_cos_phi(attiva, quadranti)
    assert res is not None
    assert res["reattiva_prelevata"] == 33.0
    assert res["reattiva_immessa"] == 25.5
    assert res["tan_phi"] == 0.33
    assert res["cos_phi"] == 0.95  # 100 / sqrt(100^2 + 33^2) = 0.9497 -> 0.950


def test_estrai_ultima_lettura_picks_latest_and_strips_pii():
    rows = [
        {
            "pod": POD_A,
            "meterSerialNumber": "SECRET_SERIAL_1",
            "measureTs": "2026-09-01T00:00:00+02:00",
            "publishingInfo": {"flowType": "PDO2G_R", "measType": "E", "fileName": "secret.xml"},
            "publishedSlots": [{"magnitude": "EA", "slotId": "T1", "value": 1.0}],
            "publishedPowerPeaks": [{"magnitude": "POT", "slotId": "T1", "value": 0.5}],
        },
        {
            "pod": POD_A,
            "meterSerialNumber": "SECRET_SERIAL_2",
            "measureTs": "2026-10-01T00:00:00+02:00",
            "publishingInfo": {
                "flowType": "PDO2G_R",
                "publishingTs": "2026-10-01T10:37:00.000Z[Etc/UTC]",
                "measType": "E",
                "fileName": "secret2.xml",
            },
            "publishedSlots": [
                {"magnitude": "EA", "slotId": "T1", "value": 0.711},
                {"magnitude": "EA", "slotId": "T2", "value": 2.054},
                {"magnitude": "EA", "slotId": "T3", "value": 5.926},
                {"magnitude": "EA", "slotId": "T4", "value": None},
                {"magnitude": "ER", "slotId": "T1", "value": 0},
                {"magnitude": "ER", "slotId": "T2", "value": 0},
                {"magnitude": "ER", "slotId": "T3", "value": 0},
            ],
            "publishedPowerPeaks": [
                {"magnitude": "POT", "slotId": "T1", "value": 0.1},
                {"magnitude": "POT", "slotId": "T2", "value": 0.896},
                {"magnitude": "POT", "slotId": "T3", "value": 0.496},
            ],
        },
    ]
    res = estrai_ultima_lettura(rows)
    assert res is not None
    assert res["data_lettura"] == "2026-10-01"
    assert res["tipo_flusso"] == "PDO2G_R"
    assert res["tipo_misura"] == "E"
    assert res["ea"] == {"f1": 0.711, "f2": 2.054, "f3": 5.926, "totale": 8.691}
    assert res["pot"] == {"f1": 0.1, "f2": 0.896, "f3": 0.496, "massima": 0.896}
    assert "SECRET_SERIAL" not in str(res)
    assert "secret2.xml" not in str(res)


def test_confronta_fasce_mese():
    loc = {"f1": 10.0, "f2": 20.0, "f3": 30.0, "totale": 60.0, "picco_kw": 3.2, "campioni": 2880}
    uff_ok = {"mese": "2026-10", "f1": 10.01, "f2": 19.99, "f3": 30.0, "totale": 60.0, "picco_kw": 3.2}
    res_ok = confronta_fasce_mese(loc, uff_ok)
    assert res_ok["esito"] == "ok"
    assert res_ok["delta"]["totale"] == 0.0

    uff_bad = {"mese": "2026-10", "f1": 12.0, "f2": 18.0, "f3": 30.0, "totale": 60.0, "picco_kw": 3.2}
    res_bad = confronta_fasce_mese(loc, uff_bad)
    assert res_bad["esito"] == "scostamento"
    assert res_bad["delta"]["f1"] == -2.0

    assert confronta_fasce_mese(None, uff_ok) == {"esito": "non_disponibile"}


# ---------------------------------------------------------------------------
# 3. raw_storage: daily peak power + monthly ARERA bands
# ---------------------------------------------------------------------------


def test_raw_storage_picco_giorno_e_fasce_mensili(tmp_path):
    db_path = str(tmp_path / "curve.db")
    # Monday 2026-10-12: 08:15 CEST (F1) has peak 1.250 kWh -> 5.0 kW.
    # 02:00 CEST (F3) has 0.250 kWh -> 1.0 kW.
    samples = [
        (datetime(2026, 10, 12, 2, 0, tzinfo=FUSO_ITALIA), 0.25),
        (datetime(2026, 10, 12, 8, 15, tzinfo=FUSO_ITALIA), 1.25),
        (datetime(2026, 10, 12, 19, 30, tzinfo=FUSO_ITALIA), 0.50),
    ]
    raw_storage.upsert_campioni(db_path, POD_A, MAGNITUDE_PRELEVATA, samples)

    picchi = raw_storage.picchi_ultimo_giorno_db(
        db_path, [POD_A], (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA), FUSO_ITALIA
    )
    assert picchi[POD_A][MAGNITUDE_PRELEVATA] == (date(2026, 10, 12), 5.0, "08:15")
    assert picchi[POD_A][MAGNITUDE_IMMESSA] is None

    mensile = raw_storage.fasce_mensili_db(db_path, POD_A, MAGNITUDE_PRELEVATA, 2026, 10)
    assert mensile == {
        "f1": 1.25,
        "f2": 0.5,
        "f3": 0.25,
        "totale": 2.0,
        "picco_kw": 5.0,
        "campioni": 3,
    }


# ---------------------------------------------------------------------------
# 4. Coordinator: 365-day chunking + auxiliary metadata cadence
# ---------------------------------------------------------------------------


async def test_recupera_storico_chunks_over_180_days(hass, make_edist_coordinator):
    """A 250-day history fetch is allowed (<= 365) and split into 180 + 70 day chunks."""
    coord = make_edist_coordinator(pods=[POD_A])
    coord._async_ensure_token = AsyncMock()
    coord.async_aggiorna_riepilogo = AsyncMock(return_value={})

    calls: list[tuple[date, date, str]] = []

    async def fake_daily(pod, data_da, data_a, magnitude="A1"):
        calls.append((data_da, data_a, magnitude))
        return [
            {
                "readings": {
                    "energyType": magnitude,
                    "sampleDate": data_da.strftime("%Y%m%d"),
                    "sampleValues": [{"id": "1", "val": "0.1"}],
                },
                "sampleFrequency": 15,
                "initialSample": f"{data_da.isoformat()}T00:00:00+00:00",
            }
        ]

    coord._api.async_get_daily_load_profile = AsyncMock(side_effect=fake_daily)

    with (
        freeze_time("2026-11-19 12:00:00"),
        patch(
            "custom_components.edistribuzione.coordinator.async_import_curva_giornaliera",
            AsyncMock(),
        ),
    ):
        res = await coord.async_recupera_storico(date(2026, 3, 1), date(2026, 11, 5))

    # 250 days = 2 chunks x 2 directions = 4 API calls.
    assert len(calls) == 4
    a1_calls = [(da, a) for da, a, m in calls if m == MAGNITUDE_PRELEVATA]
    assert a1_calls == [
        (date(2026, 3, 1), date(2026, 8, 27)),
        (date(2026, 8, 28), date(2026, 11, 5)),
    ]
    assert res["pod"][POD_A]["giorni_ricevuti"] == 2


async def test_coordinator_ausiliari_forniture_mensili_letture_e_reattiva(
    hass, make_edist_coordinator
):
    """Coordinator refreshes supply metadata, monthly TOU, readings, and opt-in reactive."""
    coord = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_ACCESS_TOKEN: "tok-live"},
        options={CONF_ENERGIA_REATTIVA: True},
    )
    coord._api.update_token("tok-live")
    coord._api.async_get_supplies = AsyncMock(
        return_value=[
            {
                "IdPod": POD_A,
                "ContractualPower": 4.5,
                "AvailablePower": 6.6,
                "DeductiblePower": 6.6,
                "VoltageLevel": "BM",
                "SupplyStatusId": "ATT",
                "MeteringPointType": "M1",
                "HasPlant": True,
            }
        ]
    )

    async def fake_tou(pod, d_from, d_to, magnitude="A1"):
        val = {"A1": "100.0", "A2": "50.0", "R1": "10.0", "R2": "0.0", "R3": "5.0", "R4": "23.0"}[
            magnitude
        ]
        return [
            {
                "yearMonth": "202611",
                "readings": {
                    "energyType": magnitude,
                    "energyT1": val,
                    "energyT4": val,
                    "peakPowerT1": "4.2",
                    "peakPowerT4": "4.2",
                },
            }
        ]

    coord._api.async_get_monthly_time_of_use = AsyncMock(side_effect=fake_tou)
    coord._api.async_get_reading = AsyncMock(
        return_value=[
            {
                "measureTs": "2026-11-01T00:00:00+01:00",
                "publishingInfo": {"flowType": "PDO2G_R", "measType": "E"},
                "publishedSlots": [{"magnitude": "EA", "slotId": "T1", "value": 12.3}],
                "publishedPowerPeaks": [{"magnitude": "POT", "slotId": "T1", "value": 3.5}],
            }
        ]
    )

    with freeze_time("2026-11-19 20:00:00"):
        await coord._async_aggiorna_ausiliari({POD_A})

    assert coord.forniture[POD_A]["potenza_contrattuale"] == 4.5
    assert coord.mensili[POD_A][MAGNITUDE_PRELEVATA]["mese_corrente"]["totale"] == 100.0
    assert coord.reattiva[POD_A]["mese_corrente"]["cos_phi"] == 0.95
    assert coord.letture_ufficiali[POD_A]["data_lettura"] == "2026-11-01"

    # Next day in the same month: reading for 2026-11-01 is already present -> 0 calls to /reading.
    coord._api.async_get_reading.reset_mock()
    with freeze_time("2026-11-20 20:00:00"):
        await coord._async_aggiorna_letture(POD_A, date(2026, 11, 20))
    coord._api.async_get_reading.assert_not_awaited()


async def test_coordinator_skips_reactive_when_opt_in_disabled(hass, make_edist_coordinator):
    coord = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "tok-live"})
    coord._api.update_token("tok-live")
    coord._api.async_get_monthly_time_of_use = AsyncMock(return_value=[])
    await coord._async_aggiorna_mensili(POD_A, date(2026, 11, 19))
    mags = [c.kwargs["magnitude"] for c in coord._api.async_get_monthly_time_of_use.await_args_list]
    assert mags == [MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA]


async def test_coordinator_skips_a2_when_has_plant_is_false(hass, make_edist_coordinator):
    """Consumption-only POD (HasPlant=False) skips A2 in both daily curves and monthly TOU."""
    coord = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "tok-live"})
    coord._api.update_token("tok-live")
    coord.forniture[POD_A] = {"ha_impianto": False}
    coord._api.async_get_daily_load_profile = AsyncMock(return_value=[])
    coord._api.async_get_monthly_time_of_use = AsyncMock(return_value=[])

    await coord._async_importa_periodo(POD_A, date(2026, 11, 18), date(2026, 11, 18))
    await coord._async_aggiorna_mensili(POD_A, date(2026, 11, 19))

    assert [
        c.kwargs["magnitude"] for c in coord._api.async_get_daily_load_profile.await_args_list
    ] == [MAGNITUDE_PRELEVATA]
    assert [
        c.kwargs["magnitude"] for c in coord._api.async_get_monthly_time_of_use.await_args_list
    ] == [MAGNITUDE_PRELEVATA]


async def test_coordinator_keeps_a2_on_production_or_historical_injection_even_if_has_plant_false(
    hass, make_edist_coordinator
):
    coord_prod = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_ACCESS_TOKEN: "tok-live"},
        options={CONF_TIPO_POD: {POD_A: TIPO_POD_PRODUZIONE}},
    )
    coord_prod.forniture[POD_A] = {"ha_impianto": False}
    assert coord_prod._magnitudes_attive(POD_A) == (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA)

    coord_hist = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "tok-live"})
    coord_hist.forniture[POD_A] = {"ha_impianto": False}
    coord_hist.riepilogo[POD_A] = {
        "ultimo_giorno": {MAGNITUDE_IMMESSA: {"giorno": "2026-11-18", "kwh": 2.5}}
    }
    assert coord_hist._magnitudes_attive(POD_A) == (MAGNITUDE_PRELEVATA, MAGNITUDE_IMMESSA)


async def test_coordinator_idle_hour_makes_no_token_or_api_calls(hass, make_edist_coordinator):
    """After midnight when curves are not due and auxiliaries are loaded, zero network calls run."""
    coord = make_edist_coordinator(pods=[POD_A])
    coord.forniture[POD_A] = {"ha_impianto": False}
    coord.mensili[POD_A] = {}
    coord.letture_ufficiali[POD_A] = {"data_lettura": "2026-11-01"}
    coord._async_ensure_token = AsyncMock()
    coord._prossima_richiesta = AsyncMock(return_value=None)
    coord._api.async_get_supplies = AsyncMock()
    coord.async_aggiorna_riepilogo = AsyncMock(return_value={})

    with freeze_time("2026-11-20 01:00:00+01:00"):
        await coord._async_update_data()

    coord._async_ensure_token.assert_not_awaited()
    coord._api.async_get_supplies.assert_not_awaited()


async def test_coordinator_blocked_refresh_on_pre_curve_supplies_stops_cycle_after_one_attempt(
    hass, make_edist_coordinator
):
    """Expired token + blocked refresh during evening supplies fetch aborts after 1 refresh."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    from custom_components.edistribuzione.api import ApiError
    from custom_components.edistribuzione.auth import AppBloccata

    coord = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "expired"})
    coord._prossima_richiesta = AsyncMock(
        return_value=(date(2026, 11, 18), date(2026, 11, 18))
    )
    coord._api.async_get_supplies = AsyncMock(side_effect=ApiError("401", status=401))
    coord._api.async_get_daily_load_profile = AsyncMock()
    coord._auth.async_refresh_access_token = AsyncMock(side_effect=AppBloccata("app_blocked"))

    with freeze_time("2026-11-19 20:00:00+01:00"), pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord._auth.async_refresh_access_token.await_count == 1
    coord._api.async_get_daily_load_profile.assert_not_awaited()


async def test_coordinator_ausiliari_stop_on_first_blocked_refresh(hass, make_edist_coordinator):
    """Startup with expired token + blocked refresh must attempt refresh ONCE, not per endpoint."""
    from custom_components.edistribuzione.api import ApiError
    from custom_components.edistribuzione.auth import AppBloccata

    coord = make_edist_coordinator(
        pods=[POD_A, POD_B],
        data={CONF_ACCESS_TOKEN: "expired"},
        options={CONF_ENERGIA_REATTIVA: True},
    )
    coord._prossima_richiesta = AsyncMock(return_value=None)
    coord._api.async_get_supplies = AsyncMock(side_effect=ApiError("401", status=401))
    coord._api.async_get_monthly_time_of_use = AsyncMock(side_effect=ApiError("401", status=401))
    coord._api.async_get_reading = AsyncMock(side_effect=ApiError("401", status=401))
    coord._auth.async_refresh_access_token = AsyncMock(side_effect=AppBloccata("app_blocked"))
    coord.async_aggiorna_riepilogo = AsyncMock(return_value={})

    with freeze_time("2026-11-20 10:00:00+01:00"):
        await coord._async_update_data()

    assert coord._auth.async_refresh_access_token.await_count == 1
    coord._api.async_get_monthly_time_of_use.assert_not_awaited()
    coord._api.async_get_reading.assert_not_awaited()
    assert coord.accesso_bloccato is True


async def test_coordinator_pod_without_readings_does_not_wake_up_at_midnight(
    hass, make_edist_coordinator
):
    """If /reading returned [] on day 1, day 2 at 00:30 must stay completely idle."""
    coord = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "tok-live"})
    coord._api.update_token("tok-live")
    coord._api.async_get_supplies = AsyncMock(return_value=[{"IdPod": POD_A, "HasPlant": False}])
    coord._api.async_get_monthly_time_of_use = AsyncMock(return_value=[])
    coord._api.async_get_reading = AsyncMock(return_value=[])
    coord._prossima_richiesta = AsyncMock(return_value=None)
    coord.async_aggiorna_riepilogo = AsyncMock(return_value={})

    # Day 1 startup: loads auxiliaries once (reading returns [], so letture_ufficiali stays empty).
    with freeze_time("2026-11-19 10:00:00+01:00"):
        await coord._async_update_data()
    assert POD_A not in coord.letture_ufficiali
    assert coord._api.async_get_reading.await_count == 1

    # Day 2 at 00:30 (before evening window): zero token or API calls.
    coord._async_ensure_token = AsyncMock()
    coord._api.async_get_supplies.reset_mock()
    coord._api.async_get_monthly_time_of_use.reset_mock()
    coord._api.async_get_reading.reset_mock()

    with freeze_time("2026-11-20 00:30:00+01:00"):
        await coord._async_update_data()

    coord._async_ensure_token.assert_not_awaited()
    coord._api.async_get_supplies.assert_not_awaited()
    coord._api.async_get_monthly_time_of_use.assert_not_awaited()
    coord._api.async_get_reading.assert_not_awaited()


async def test_coordinator_persistent_auxiliary_api_error_does_not_retry_hourly(
    hass, make_edist_coordinator
):
    """If auxiliary endpoints fail with ApiError at startup, later idle hours must not retry."""
    from custom_components.edistribuzione.api import ApiError

    coord = make_edist_coordinator(pods=[POD_A], data={CONF_ACCESS_TOKEN: "tok-live"})
    coord._api.update_token("tok-live")
    coord._api.async_get_supplies = AsyncMock(side_effect=ApiError("HTTP 500", status=500))
    coord._api.async_get_monthly_time_of_use = AsyncMock(
        side_effect=ApiError("HTTP 500", status=500)
    )
    coord._api.async_get_reading = AsyncMock(side_effect=ApiError("HTTP 500", status=500))
    coord._prossima_richiesta = AsyncMock(return_value=None)
    coord.async_aggiorna_riepilogo = AsyncMock(return_value={})

    # Startup cycle (10:00): tries auxiliaries once, all fail with ApiError.
    with freeze_time("2026-11-20 10:00:00+01:00"):
        await coord._async_update_data()
    assert coord._api.async_get_supplies.await_count == 1
    assert coord._api.async_get_reading.await_count == 1

    # Next idle hours (11:00, 12:00): zero retries until the evening window.
    coord._async_ensure_token = AsyncMock()
    coord._api.async_get_supplies.reset_mock()
    coord._api.async_get_monthly_time_of_use.reset_mock()
    coord._api.async_get_reading.reset_mock()

    with freeze_time("2026-11-20 11:00:00+01:00"):
        await coord._async_update_data()
    with freeze_time("2026-11-20 12:00:00+01:00"):
        await coord._async_update_data()

    coord._async_ensure_token.assert_not_awaited()
    coord._api.async_get_supplies.assert_not_awaited()
    coord._api.async_get_monthly_time_of_use.assert_not_awaited()
    coord._api.async_get_reading.assert_not_awaited()


# ---------------------------------------------------------------------------
# 5. Sensors & Options Flow & Config Flow M2 auto-detection
# ---------------------------------------------------------------------------


async def test_new_sensors_values_and_opt_in_reactive_count(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PODS: [POD_A]}, title=POD_A)
    entry.add_to_hass(hass)
    coord = SimpleNamespace(
        entry=entry,
        pods=[POD_A],
        lingua="it",
        tipo_pod=lambda p: TIPO_POD_SCAMBIO,
        energia_reattiva_attiva=True,
        forniture={
            POD_A: {
                "potenza_contrattuale": 4.5,
                "potenza_disponibile": 6.6,
                "potenza_prelevabile": 6.6,
                "tensione": "BM",
                "stato_fornitura": "ATT",
                "tipo_punto": "M1",
                "ha_impianto": True,
                "regime_impianto": "CESSIONE PARZIALE INCENTIVATA",
            }
        },
        riepilogo={
            POD_A: {
                "ultima_data_disponibile": "2026-11-18",
                "ultimo_giorno": {
                    MAGNITUDE_PRELEVATA: {"giorno": "2026-11-18", "kwh": 12.0},
                    MAGNITUDE_IMMESSA: {"giorno": "2026-11-18", "kwh": 1.0},
                },
                "picco_ultimo_giorno": {
                    MAGNITUDE_PRELEVATA: {"giorno": "2026-11-18", "kw": 4.5, "ora": "19:15"},
                },
            }
        },
        mensili={
            POD_A: {
                MAGNITUDE_PRELEVATA: {
                    "mese_corrente": {
                        "mese": "2026-11",
                        "totale": 366.794,
                        "f1": 120.229,
                        "f2": 114.493,
                        "f3": 132.072,
                        "picco_kw": 5.864,
                        "picco_f1_kw": 5.864,
                        "picco_f2_kw": 5.756,
                        "picco_f3_kw": 5.796,
                    },
                    "mese_precedente": {
                        "mese": "2026-10",
                        "totale": 57.586,
                        "f1": 5.041,
                        "f2": 14.404,
                        "f3": 38.141,
                        "picco_kw": 4.068,
                        "picco_f1_kw": 1.888,
                        "picco_f2_kw": 2.316,
                        "picco_f3_kw": 4.068,
                    },
                },
                MAGNITUDE_IMMESSA: {
                    "mese_corrente": {"mese": "2026-11", "totale": 3.164, "f1": 1.69, "f2": 0.599, "f3": 0.875},
                    "mese_precedente": {"mese": "2026-10", "totale": 81.075, "f1": 64.57, "f2": 5.523, "f3": 10.982},
                },
            }
        },
        letture_ufficiali={
            POD_A: {
                "data_lettura": "2026-10-01",
                "pubblicata_il": "2026-10-01T10:37:00Z",
                "tipo_flusso": "PDO2G_R",
                "tipo_misura": "E",
                "ea": {"f1": 0.711, "f2": 2.054, "f3": 5.926, "totale": 8.691},
                "er": {"f1": 0.0, "f2": 0.0, "f3": 0.0, "totale": 0.0},
                "pot": {"f1": 0.1, "f2": 0.896, "f3": 0.496, "massima": 0.896},
            }
        },
        reattiva={
            POD_A: {
                "mese_corrente": {
                    "mese": "2026-11",
                    "reattiva_prelevata": 33.0,
                    "reattiva_immessa": 12.0,
                    "r1": 1.0,
                    "r2": 0.0,
                    "r3": 12.0,
                    "r4": 32.0,
                    "cos_phi": 0.95,
                    "tan_phi": 0.33,
                },
                "mese_precedente": {
                    "mese": "2026-10",
                    "reattiva_prelevata": 178.193,
                    "reattiva_immessa": 304.321,
                    "cos_phi": 0.307,
                    "tan_phi": 3.094,
                },
            }
        },
    )

    entities = sensor.build_entities(hass, coord)
    # 2 account + 9 per POD when reactive opt-in is enabled.
    assert len(entities) == 11
    by_key = {e.translation_key: e for e in entities}

    assert by_key["potenza_contrattuale"].native_value == 4.5
    assert by_key["potenza_contrattuale"].extra_state_attributes["tipo_punto"] == "M1"
    assert by_key["potenza_disponibile"].native_value == 6.6
    assert by_key["picco_potenza_ultimo_giorno"].native_value == 4.5
    assert by_key["picco_potenza_ultimo_giorno"].extra_state_attributes["ora_picco"] == "19:15"
    assert by_key["picco_potenza_ultimo_giorno"].extra_state_attributes["pct_potenza_contrattuale"] == 100.0
    assert by_key["picco_potenza_mese_corrente"].native_value == 5.864
    assert by_key["picco_potenza_mese_corrente"].extra_state_attributes["pct_potenza_disponibile"] == 88.8
    assert by_key["picco_potenza_mese_precedente"].native_value == 4.068
    assert by_key["ultima_lettura_ufficiale"].native_value == date(2026, 10, 1)
    assert by_key["ultima_lettura_ufficiale"].extra_state_attributes["tipo_flusso"] == "PDO2G_R"
    assert by_key["energia_reattiva_mese_corrente"].native_value == 33.0
    assert by_key["fattore_potenza_mese_corrente"].native_value == 0.95


async def test_cleanup_removes_dropped_energy_and_disabled_reactive_entities(hass):
    from homeassistant.helpers import entity_registry as er

    from custom_components.edistribuzione import _pulisci_entita_rimosse

    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PODS: [POD_A]}, options={})
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    for suffisso in (
        f"{POD_A}_consumo_giorno_prelevata",
        f"{POD_A}_consumo_giorno_immessa",
        f"{POD_A}_energia_mese_corrente_prelevata",
        f"{POD_A}_energia_mese_precedente_immessa",
        f"{POD_A}_energia_reattiva_mese",
        f"{POD_A}_fattore_potenza_mese",
        f"{POD_A}_picco_potenza_mese_corrente",
        f"{POD_A}_ultima_data_disponibile",
    ):
        ent_reg.async_get_or_create(
            "sensor", DOMAIN, f"{entry.entry_id}_{suffisso}", config_entry=entry
        )

    _pulisci_entita_rimosse(hass, entry)
    rimasti = {
        e.unique_id for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
    }
    assert rimasti == {
        f"{entry.entry_id}_{POD_A}_picco_potenza_mese_corrente",
        f"{entry.entry_id}_{POD_A}_ultima_data_disponibile",
    }

    # Re-add reactive sensors with opt-in enabled: they must be preserved.
    entry_opt = MockConfigEntry(
        domain=DOMAIN, data={CONF_PODS: [POD_A]}, options={CONF_ENERGIA_REATTIVA: True}
    )
    entry_opt.add_to_hass(hass)
    for suffisso in (
        f"{POD_A}_consumo_giorno_prelevata",
        f"{POD_A}_energia_reattiva_mese",
        f"{POD_A}_fattore_potenza_mese",
    ):
        ent_reg.async_get_or_create(
            "sensor", DOMAIN, f"{entry_opt.entry_id}_{suffisso}", config_entry=entry_opt
        )
    _pulisci_entita_rimosse(hass, entry_opt)
    rimasti_opt = {
        e.unique_id for e in er.async_entries_for_config_entry(ent_reg, entry_opt.entry_id)
    }
    assert rimasti_opt == {
        f"{entry_opt.entry_id}_{POD_A}_energia_reattiva_mese",
        f"{entry_opt.entry_id}_{POD_A}_fattore_potenza_mese",
    }


async def test_config_flow_auto_detects_m2_production_role(hass):
    from custom_components.edistribuzione import config_flow as cf

    api_mock = Mock()
    api_mock.async_get_supplies = AsyncMock(
        return_value=[
            {"IdPod": POD_A, "MeteringPointType": "M1"},
            {"IdPod": POD_B, "MeteringPointType": "M2"},
        ]
    )
    flow = cf.EdistribuzioneConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow._access_token = "at"
    flow._refresh_token = "rt"
    with patch.object(cf, "ApiClient", return_value=api_mock):
        step = await flow.async_step_pod()
        assert step["type"] == "form"
        result = await flow.async_step_pod({CONF_PODS: [POD_A, POD_B]})
    assert result["type"] == "create_entry"
    assert result["options"] == {
        CONF_TIPO_POD: {POD_A: TIPO_POD_SCAMBIO, POD_B: TIPO_POD_PRODUZIONE}
    }


async def test_options_flow_energia_reattiva_toggle(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PODS: [POD_A]}, options={})
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_reload", AsyncMock()) as reload_mock:
        res = await hass.config_entries.options.async_init(entry.entry_id)
        res = await hass.config_entries.options.async_configure(
            res["flow_id"], {"next_step_id": "energia_reattiva"}
        )
        assert res["type"] == "form"
        res = await hass.config_entries.options.async_configure(
            res["flow_id"], {CONF_ENERGIA_REATTIVA: True}
        )
    assert res["type"] == "create_entry"
    assert entry.options[CONF_ENERGIA_REATTIVA] is True
    reload_mock.assert_awaited_once_with(entry.entry_id)


# ---------------------------------------------------------------------------
# 6. Diagnostics: ARERA reconciliation & redaction of new sections
# ---------------------------------------------------------------------------


async def test_diagnostics_includes_reconciliation_and_redacts_pods(
    hass, make_edist_coordinator, tmp_path
):
    db_path = str(tmp_path / "curve.db")
    samples = [
        (datetime(2026, 10, 12, 9, 0, tzinfo=FUSO_ITALIA), 5.0),  # F1
        (datetime(2026, 10, 12, 20, 0, tzinfo=FUSO_ITALIA), 2.0),  # F2
        (datetime(2026, 10, 12, 2, 0, tzinfo=FUSO_ITALIA), 3.0),  # F3
    ]
    raw_storage.upsert_campioni(db_path, POD_A, MAGNITUDE_PRELEVATA, samples)

    coord = make_edist_coordinator(pods=[POD_A])
    coord.forniture = {POD_A: {"potenza_contrattuale": 4.5, "tipo_punto": "M1"}}
    coord.mensili = {
        POD_A: {
            MAGNITUDE_PRELEVATA: {
                "mese_precedente": {
                    "mese": "2026-10",
                    "f1": 5.0,
                    "f2": 2.0,
                    "f3": 3.0,
                    "totale": 10.0,
                    "picco_kw": 20.0,
                }
            }
        }
    }
    coord.letture_ufficiali = {POD_A: {"data_lettura": "2026-10-01", "tipo_flusso": "PDO2G_R"}}
    coord.entry.runtime_data = coord

    with patch.object(raw_storage, "percorso_predefinito", return_value=db_path):
        diag = await diagnostics.async_get_config_entry_diagnostics(hass, coord.entry)

    coord_diag = diag["coordinator"]
    assert POD_A not in str(diag)
    assert "POD_1" in coord_diag["forniture"]
    ric = coord_diag["riconciliazione_fasce"]["POD_1"][MAGNITUDE_PRELEVATA]
    assert ric["esito"] == "ok"
    assert ric["delta"] == {"f1": 0.0, "f2": 0.0, "f3": 0.0, "totale": 0.0}
