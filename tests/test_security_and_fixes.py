"""Regression tests for security/redaction and backend concurrency/logic fixes."""
from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest
from freezegun import freeze_time
from homeassistant.components.energy.data import async_get_manager
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import (
    api as edist_api,
)
from custom_components.edistribuzione import (
    async_remove_config_entry_device,
    diagnostics,
    energy_dashboard,
    lovelace_card,
    raw_storage,
    repairs,
    sensor,
    statistics,
)
from custom_components.edistribuzione import auth as edist_auth
from custom_components.edistribuzione import (
    config_flow as cf,
)
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_GIORNI_DA_RIPROVARE,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    ISSUE_ACCESSO_BLOCCATO,
    MAGNITUDE_PRELEVATA,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
    issue_id_per_entry,
)

from .conftest import POD_A, POD_B

# ---------------------------------------------------------------------------
# API client fixes
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status: int, body, *, text_body: str | None = None):
        self.status = status
        self._body = body
        self._text = text_body
        self.closed = False
        self.headers = {}
        self.url = aiohttp.client.URL("https://private.e-distribuzione.it/PortaleClienti/x")

    async def json(self, content_type=None):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def text(self):
        if self._text is not None:
            return self._text
        return str(self._body)

    def close(self):
        self.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, responses: list[_FakeResp]):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def _next(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": str(url), **kwargs})
        return self._responses.pop(0)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)


async def test_api_get_supplies_uses_unified_error_handling_and_handles_nulls():
    session = _FakeSession([
        _FakeResp(401, {}),
        _FakeResp(200, {"meta": None, "data": None}),
        _FakeResp(200, {"data": [{"pods": None}]}),
    ])
    client = edist_api.ApiClient(session, "tok")

    with pytest.raises(edist_api.ApiError) as err:
        await client.async_get_supplies()
    assert err.value.status == 401
    assert session.calls[0]["timeout"] == edist_api.DEFAULT_TIMEOUT

    assert await client.async_get_supplies() == []
    assert await client.async_get_supplies() == []


async def test_api_404_html_raises_while_empty_404_returns_empty_list(caplog):
    session = _FakeSession([
        _FakeResp(404, "", text_body="<html><title>404 Not Found</title></html>"),
        _FakeResp(404, "", text_body=""),
    ])
    client = edist_api.ApiClient(session, "tok")

    with pytest.raises(edist_api.ApiError) as err:
        await client.async_get_daily_load_profile(POD_A, date(2026, 9, 1))
    assert err.value.status == 404

    with caplog.at_level("DEBUG"):
        assert await client.async_get_daily_load_profile(POD_A, date(2026, 9, 1)) == []
    assert POD_A not in caplog.text
    assert "IT001E****0001" in caplog.text


async def test_api_other_endpoints_parameter_order_and_null_data():
    session = _FakeSession([
        _FakeResp(200, {"meta": None, "data": None}),
        _FakeResp(200, {"data": [{"ok": 1}]}),
        _FakeResp(200, {"data": [{"ok": 2}]}),
    ])
    client = edist_api.ApiClient(session, "tok")

    assert await client.async_get_monthly_load_profile(
        POD_A, date(2026, 9, 1), date(2026, 9, 30), magnitude="A2"
    ) == []
    assert session.calls[0]["params"]["pointofdelivery"] == POD_A
    assert session.calls[0]["params"]["magnitude"] == "A2"

    assert await client.async_get_reading(POD_A, date(2026, 9, 1), date(2026, 9, 30)) == [
        {"ok": 1}
    ]
    assert await client.async_get_monthly_time_of_use(
        POD_A, date(2026, 9, 1), date(2026, 9, 30)
    ) == [{"ok": 2}]


# ---------------------------------------------------------------------------
# Auth security & parsing fixes
# ---------------------------------------------------------------------------


async def test_auth_too_many_redirects_redacts_sid_from_error():
    class _LoopSession:
        async def get(self, url, **kwargs):
            resp = _FakeResp(302, "")
            resp.headers = {"Location": "https://private.e-distribuzione.it/loop?sid=SUPER_SID"}
            return resp

    with pytest.raises(edist_auth.AuthError) as err:
        await edist_auth._get_following_redirects(
            _LoopSession(), "https://private.e-distribuzione.it/frontdoor.jsp?sid=SUPER_SID"
        )
    assert "SUPER_SID" not in str(err.value)


def test_auth_valida_state_rejects_missing_or_mismatched_without_logging_secret(caplog):
    client = edist_auth.AuthClient(None)
    client._oauth_state = "EXPECTED_SECRET_STATE"

    with caplog.at_level("WARNING"), pytest.raises(edist_auth.AuthError):
        client._valida_state(None)
    assert "EXPECTED_SECRET_STATE" not in caplog.text

    import re

    bad = re.search(r"state=([^&]+)", "state=OTHER_SECRET_STATE")
    with caplog.at_level("WARNING"), pytest.raises(edist_auth.AuthError):
        client._valida_state(bad)
    assert "EXPECTED_SECRET_STATE" not in caplog.text
    assert "OTHER_SECRET_STATE" not in caplog.text


async def test_auth_refresh_keeps_old_refresh_token_when_server_returns_null():
    session = _FakeSession([
        _FakeResp(200, "", text_body='{"access_token": "new-acc", "refresh_token": null}')
    ])
    client = edist_auth.AuthClient(session)
    assert client.host_attivo is None

    tokens = await client.async_refresh_access_token("old-rt")
    assert tokens.access_token == "new-acc"
    assert tokens.refresh_token == "old-rt"
    assert client.host_attivo == "edistribuzione.my.salesforce.com"


def test_auth_browser_redirect_strips_trailing_punctuation():
    client = edist_auth.AuthClient(None)
    url = client.prepare_browser_token_authorization_url()
    state = url.split("state=")[1].split("&")[0]

    pasted = f"(eneldist://redirect#access_token=AT1&refresh_token=RT1&state={state})."
    tokens = client.tokens_from_browser_redirect(pasted)
    assert tokens.access_token == "AT1"
    assert tokens.refresh_token == "RT1"


async def test_auth_otp_invalid_english_and_meta_location_html_unescape():
    session = _FakeSession([
        _FakeResp(200, "", text_body="<html><div>Your OTP verification code is expired</div></html>"),
        _FakeResp(
            200,
            "",
            text_body='<meta name="Location" content="/PortaleClienti/next?a=1&amp;b=2">',
        ),
    ])
    client = edist_auth.AuthClient(session)
    client._flow.view_state = "VS"

    with pytest.raises(edist_auth.InvalidOtp) as err:
        await client._sottometti_codice_otp("12345")
    assert "<html>" not in str(err.value)

    next_url = await client._sottometti_codice_otp("12345")
    assert next_url == "https://private.e-distribuzione.it/PortaleClienti/next?a=1&b=2"


async def test_auth_scarica_pagina_dopo_otp_handles_direct_302_to_app_scheme():
    class _RedirectToAppSession:
        async def get(self, url, **kwargs):
            resp = _FakeResp(302, "")
            resp.headers = {"Location": "eneldist://redirect?code=AUTH_CODE&state=S1"}
            return resp

    client = edist_auth.AuthClient(_RedirectToAppSession())
    result = await client._scarica_pagina_dopo_otp("https://private.e-distribuzione.it/done")
    assert result == "eneldist://redirect?code=AUTH_CODE&state=S1"


async def test_auth_closes_response_when_antibot_raises():
    antibot_resp = _FakeResp(200, "", text_body="<html>Pardon Our Interruption</html>")

    class _AntibotSession:
        async def get(self, url, **kwargs):
            return antibot_resp

    client = edist_auth.AuthClient(_AntibotSession())
    with pytest.raises(edist_auth.AccessoBloccato):
        await client._carica_pagina_login("u@example.com")
    assert antibot_resp.closed is True


def test_auth_discovers_dynamic_richfaces_form_id_and_nav_field():
    html = """
    <form name="thePage:j_id9:i:f" action="/PortaleClienti/loginflow/x?a=1&amp;b=2">
      <input type="hidden" name="thePage:j_id9:i:f:pb:d:navigationType" value="" />
      <input type="hidden" name="com.salesforce.visualforce.ViewState" value="VS" />
      <input type="hidden" name="com.salesforce.visualforce.ViewStateVersion" value="1" />
      <input type="hidden" name="com.salesforce.visualforce.ViewStateMAC" value="MAC" />
      <input type="hidden" name="com.salesforce.visualforce.ViewStateCSRF" value="CSRF" />
    </form>
    """
    client = edist_auth.AuthClient(None)
    client._analizza_pagina_otp(html)
    common = client._campi_comuni_form_otp()
    assert common["thePage:j_id9:i:f"] == "thePage:j_id9:i:f"
    assert "thePage:j_id9:i:f:pb:d:navigationType" in common
    assert client._flow.form_action_url == (
        "https://private.e-distribuzione.it/PortaleClienti/loginflow/x?a=1&b=2"
    )


# ---------------------------------------------------------------------------
# Coordinator concurrency, multi-account Repairs & scheduling fixes
# ---------------------------------------------------------------------------


async def test_coordinator_concurrent_token_refresh_runs_once(make_edist_coordinator):
    coordinator = make_edist_coordinator(data={CONF_ACCESS_TOKEN: "old-token"})

    async def slow_refresh(rt, client_id=None):
        await asyncio.sleep(0.05)
        return SimpleNamespace(access_token="new-token", refresh_token="rt2", instance_url="")

    coordinator._auth.async_refresh_access_token = AsyncMock(side_effect=slow_refresh)

    await asyncio.gather(
        coordinator._async_ensure_token(force_refresh=True, failed_token="old-token"),
        coordinator._async_ensure_token(force_refresh=True, failed_token="old-token"),
    )
    assert coordinator._auth.async_refresh_access_token.await_count == 1
    assert coordinator.entry.data[CONF_ACCESS_TOKEN] == "new-token"


async def test_repairs_multi_account_isolation(hass, make_edist_coordinator):
    coord_a = make_edist_coordinator(pods=[POD_A])
    coord_b = make_edist_coordinator(pods=[POD_B])
    registro = ir.async_get(hass)

    coord_a._segnala_blocco(ISSUE_ACCESSO_BLOCCATO)
    coord_b._accesso_riuscito()

    issue_a = issue_id_per_entry(ISSUE_ACCESSO_BLOCCATO, coord_a.entry.entry_id)
    assert registro.async_get_issue(DOMAIN, issue_a) is not None

    flusso = await repairs.async_create_fix_flow(
        hass, issue_a, {"entry_id": coord_a.entry.entry_id}
    )
    flusso.hass = hass
    with patch.object(MockConfigEntry, "async_start_reauth") as reauth:
        await flusso.async_step_confirm({})
    reauth.assert_called_once_with(hass)


async def test_pod_without_data_does_not_poll_hourly_after_bootstrap(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(pods=[POD_A])

    with (
        patch(
            "custom_components.edistribuzione.coordinator.async_get_ultima_data_disponibile",
            AsyncMock(return_value=None),
        ),
        freeze_time("2026-09-23 10:00:00+02:00"),
    ):
        assert await coordinator._prossima_richiesta(POD_A) is not None
        # Second cycle before configured hour (19:00): skipped.
        assert await coordinator._prossima_richiesta(POD_A) is None

    with (
        patch(
            "custom_components.edistribuzione.coordinator.async_get_ultima_data_disponibile",
            AsyncMock(return_value=None),
        ),
        freeze_time("2026-09-23 20:00:00+02:00"),
    ):
        # Already tried today during bootstrap: wait until tomorrow.
        assert await coordinator._prossima_richiesta(POD_A) is None

    with (
        patch(
            "custom_components.edistribuzione.coordinator.async_get_ultima_data_disponibile",
            AsyncMock(return_value=None),
        ),
        freeze_time("2026-09-24 20:00:00+02:00"),
    ):
        assert await coordinator._prossima_richiesta(POD_A) is not None
        assert await coordinator._prossima_richiesta(POD_A) is None


async def test_queued_backlog_retried_even_when_latest_day_present(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={CONF_GIORNI_DA_RIPROVARE: {POD_A: {"2026-09-10": "2026-09-20"}}},
    )

    with (
        patch(
            "custom_components.edistribuzione.coordinator.async_get_ultima_data_disponibile",
            AsyncMock(return_value=date(2026, 9, 22)),
        ),
        freeze_time("2026-09-23 20:00:00+02:00"),
    ):
        assert await coordinator._prossima_richiesta(POD_A) == (
            date(2026, 9, 10),
            date(2026, 9, 22),
        )
        # Only once per day.
        assert await coordinator._prossima_richiesta(POD_A) is None


async def test_asymmetric_injection_delay_keeps_day_queued(hass, make_edist_coordinator):
    coordinator = make_edist_coordinator(
        pods=[POD_A], options={CONF_TIPO_POD: {POD_A: TIPO_POD_PRODUZIONE}}
    )
    periodo = (date(2026, 9, 14), date(2026, 9, 14))
    coordinator._async_ensure_token = AsyncMock()
    coordinator._prossima_richiesta = AsyncMock(return_value=periodo)
    coordinator.async_aggiorna_riepilogo = AsyncMock(return_value={})
    coordinator._async_importa_periodo = AsyncMock(
        return_value={MAGNITUDE_PRELEVATA: {"giorni_ricevuti": {date(2026, 9, 14)}}}
    )

    with freeze_time("2026-09-15 20:00:00+02:00"):
        await coordinator._async_update_data()

    assert "2026-09-14" in coordinator._leggi_code()[POD_A]


async def test_recupera_storico_refreshes_summary_even_if_second_pod_auth_fails(
    hass, make_edist_coordinator
):
    coordinator = make_edist_coordinator(pods=[POD_A, POD_B])
    coordinator._async_ensure_token = AsyncMock()
    coordinator.async_aggiorna_riepilogo = AsyncMock(return_value={})
    listener = Mock()
    coordinator.async_add_listener(listener)

    async def importa(pod, da, a):
        if pod == POD_A:
            return {MAGNITUDE_PRELEVATA: {"giorni_ricevuti": {da}}}
        raise ConfigEntryAuthFailed("expired")

    coordinator._async_importa_periodo = AsyncMock(side_effect=importa)

    with freeze_time("2026-09-15 12:00:00+02:00"), pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_recupera_storico(date(2026, 9, 10), date(2026, 9, 10))

    coordinator.async_aggiorna_riepilogo.assert_awaited_once()
    listener.assert_called()


# ---------------------------------------------------------------------------
# Diagnostics redaction fixes
# ---------------------------------------------------------------------------


async def test_diagnostics_redacts_removed_pods_lowercase_and_error_secrets(
    hass, make_edist_coordinator
):
    removed_pod = "IT001E99998888"
    coordinator = make_edist_coordinator(
        pods=[POD_A],
        data={
            CONF_ACCESS_TOKEN: "SECRET_ACC",
            CONF_REFRESH_TOKEN: "SECRET_REF",
            CONF_GIORNI_DA_RIPROVARE: {removed_pod: {"2026-10-01": "2026-10-02"}},
        },
        options={CONF_TIPO_POD: {removed_pod: TIPO_POD_SCAMBIO}},
    )
    coordinator.ultimo_errore_accesso = (
        f"Error on {POD_A.lower()} Bearer TOK999 sid=SID999 user=a@b.it"
    )
    hass.data[DOMAIN] = {coordinator.entry.entry_id: coordinator}

    with patch(
        "custom_components.edistribuzione.diagnostics.async_get_integration",
        AsyncMock(return_value=Mock(version="1.0.0")),
    ):
        data = await diagnostics.async_get_config_entry_diagnostics(hass, coordinator.entry)

    import json

    dumped = json.dumps(data, default=str)
    for leaked in (POD_A, POD_A.lower(), removed_pod, "TOK999", "SID999", "a@b.it"):
        assert leaked not in dumped
    assert data["accesso"]["host_ultimo_accesso"] is None


# ---------------------------------------------------------------------------
# Config flow, options cleanup & device helpers
# ---------------------------------------------------------------------------


def test_etichetta_pod_handles_missing_and_none_fields():
    assert cf._etichetta_pod({"IdPod": POD_A}) == POD_A
    assert (
        cf._etichetta_pod({
            "IdPod": POD_A,
            "PointOfMeasureStreetPrefix": None,
            "PointOfMeasureStreet": "ROMA",
            "PointOfMeasureStreetNumber": None,
            "PointOfMeasureMunicipality": "ROMA",
            "PointOfMeasureProvince": "RM",
        })
        == f"{POD_A} - ROMA, ROMA (RM)"
    )
    assert (
        cf._etichetta_pod({"IdPod": POD_A, "PointOfMeasureProvince": "RM"})
        == f"{POD_A} - (RM)"
    )


async def test_config_flow_prevents_duplicate_account_and_detaches_session(hass, monkeypatch):
    fake_session = Mock()
    fake_auth = Mock(
        async_begin_login=AsyncMock(return_value=None),
        otp_invio_confermato=True,
        async_submit_otp=AsyncMock(
            return_value=SimpleNamespace(access_token="a", refresh_token="r", instance_url="")
        ),
        prepare_browser_token_authorization_url=Mock(return_value="https://login.example"),
        tokens_from_browser_redirect=Mock(
            return_value=SimpleNamespace(access_token="a", refresh_token="r", instance_url="")
        ),
    )
    fake_api = Mock(
        async_get_supplies=AsyncMock(return_value=[{"IdPod": POD_A, "TaxCode": "RSSMRA80A01H501U"}])
    )
    create_session = Mock(return_value=fake_session)
    monkeypatch.setattr(cf, "AuthClient", Mock(return_value=fake_auth))
    monkeypatch.setattr(cf, "ApiClient", Mock(return_value=fake_api))
    monkeypatch.setattr(cf, "async_create_clientsession", create_session)
    monkeypatch.setattr(cf, "async_get_clientsession", lambda *a, **k: Mock())

    with patch("custom_components.edistribuzione.async_setup_entry", return_value=True):
        # Browser login goes straight from user step and must not create a dedicated ClientSession.
        res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert res["step_id"] == "browser_login"
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {"redirect_url": "eneldist://redirect#ok"}
        )
        assert res["type"] == FlowResultType.CREATE_ENTRY
        create_session.assert_not_called()

        # Second setup with same account must abort as already_configured and detach session.
        monkeypatch.setattr(cf, "LOGIN_PASSWORD_DISPONIBILE", True)
        res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {"next_step_id": "password_login"}
        )
        res = await hass.config_entries.flow.async_configure(
            res["flow_id"], {"email": "a@b.it", "password": "pw"}
        )
        res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "12345"})
        assert res["type"] == FlowResultType.ABORT
        assert res["reason"] == "already_configured"
        create_session.assert_called_once()
        fake_session.detach.assert_called_once()


async def test_rimuovi_pod_cleans_options_queue_device_and_title(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="2 POD",
        data={
            CONF_PODS: [POD_A, POD_B],
            CONF_REFRESH_TOKEN: "rt",
            CONF_GIORNI_DA_RIPROVARE: {
                POD_A: {"2026-09-01": "2026-09-02"},
                POD_B: {"2026-09-01": "2026-09-02"},
            },
        },
        options={CONF_TIPO_POD: {POD_A: TIPO_POD_SCAMBIO, POD_B: TIPO_POD_PRODUZIONE}},
    )
    entry.add_to_hass(hass)

    dev_reg = dr.async_get(hass)
    dev_a = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_{POD_A}")}
    )
    dev_b = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_{POD_B}")}
    )

    with patch("custom_components.edistribuzione.async_setup_entry", return_value=True):
        res = await hass.config_entries.options.async_init(entry.entry_id)
        res = await hass.config_entries.options.async_configure(
            res["flow_id"], {"next_step_id": "rimuovi_pod"}
        )
        res = await hass.config_entries.options.async_configure(
            res["flow_id"], {"pods_da_rimuovere": [POD_B]}
        )

    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PODS] == [POD_A]
    assert entry.title == POD_A
    assert POD_B not in entry.data[CONF_GIORNI_DA_RIPROVARE]
    assert POD_B not in entry.options[CONF_TIPO_POD]
    assert dev_reg.async_get(dev_a.id) is not None
    assert dev_reg.async_get(dev_b.id) is None


async def test_orphaned_pod_device_rejected_in_recupera_storico_and_removable(
    hass, make_edist_coordinator
):
    coordinator = make_edist_coordinator(pods=[POD_A])
    entry = coordinator.entry
    entry._async_set_state(hass, cf.config_entries.ConfigEntryState.LOADED, None)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    dev_reg = dr.async_get(hass)
    active_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_{POD_A}")}
    )
    orphan_dev = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"{entry.entry_id}_{POD_B}")}
    )

    assert await async_remove_config_entry_device(hass, entry, active_dev) is False
    assert await async_remove_config_entry_device(hass, entry, orphan_dev) is True

    from custom_components.edistribuzione import _risolvi_coordinator_e_pod_da_device

    with pytest.raises(ServiceValidationError) as err:
        _risolvi_coordinator_e_pod_da_device(hass, orphan_dev.id)
    assert err.value.translation_key == "pod_non_configurato"


# ---------------------------------------------------------------------------
# Energy Dashboard fixes (F1/F2/F3 dedup + legacy format)
# ---------------------------------------------------------------------------


async def test_energy_dashboard_does_not_duplicate_when_bands_already_configured(hass):
    manager = await async_get_manager(hass)
    f1_stat = statistics.statistic_ids_fasce(POD_A)["f1"]
    await manager.async_update({
        "energy_sources": [{
            "type": "grid",
            "stat_energy_from": f1_stat,
            "stat_energy_to": None,
            "stat_cost": None,
            "entity_energy_price": None,
            "number_energy_price": None,
            "stat_compensation": None,
            "entity_energy_price_export": None,
            "number_energy_price_export": None,
            "cost_adjustment_day": 0.0,
        }]
    })

    coord = SimpleNamespace(pods=[POD_A], tipo_pod=lambda _p: TIPO_POD_SCAMBIO)
    added = await energy_dashboard.async_configura_energy_dashboard(hass, [coord])
    assert added == []
    assert len(manager.data["energy_sources"]) == 1


async def test_energy_dashboard_legacy_format_fallback(hass):
    fake_manager = SimpleNamespace(data={"energy_sources": []}, async_update=AsyncMock())
    coord = SimpleNamespace(pods=[POD_A], tipo_pod=lambda _p: TIPO_POD_SCAMBIO)

    with (
        patch.object(energy_dashboard, "async_get_manager", AsyncMock(return_value=fake_manager)),
        patch.object(energy_dashboard, "_usa_formato_unified", return_value=False),
    ):
        added = await energy_dashboard.async_configura_energy_dashboard(hass, [coord])

    assert len(added) == 1
    sent = fake_manager.async_update.await_args.args[0]["energy_sources"]
    assert sent[0]["type"] == "grid"
    assert sent[0]["flow_from"][0]["stat_energy_from"] == statistics.statistic_ids(POD_A)[0]
    assert sent[0]["flow_to"][0]["stat_energy_to"] == statistics.statistic_ids(POD_A)[1]


# ---------------------------------------------------------------------------
# Raw storage, statistics, sensor & card registration fixes
# ---------------------------------------------------------------------------


def test_raw_storage_dst_march_missing_sample_and_invalid_values():
    # 95 samples out of 96 (id 1 missing, max_id == 96) on DST spring-forward day.
    values = [{"id": str(i), "val": "0.0" if 9 <= i <= 12 else "0.25"} for i in range(2, 97)]
    values.append({"id": "97", "val": "NaN"})
    values.append({"id": "0", "val": "1.0"})
    payload = [
        {
            "readings": {"sampleDate": "20260329", "sampleValues": values},
            "sampleFrequency": 15,
            "initialSample": "2026-03-28T23:00:00.000+00:00",
        },
        # Naive initialSample and negative frequency must be skipped.
        {
            "readings": {"sampleDate": "20260330", "sampleValues": [{"id": "1", "val": "1.0"}]},
            "sampleFrequency": 15,
            "initialSample": "2026-03-30T00:00:00",
        },
        {
            "readings": {"sampleDate": "20260331", "sampleValues": [{"id": "1", "val": "1.0"}]},
            "sampleFrequency": -15,
            "initialSample": "2026-03-30T22:00:00+00:00",
        },
    ]
    samples = raw_storage.estrai_campioni(payload)
    # 95 valid ids minus 4 nonexistent 02:00-02:45 slots = 91 samples.
    assert len(samples) == 91
    assert samples[-1][0] == datetime(2026, 3, 29, 21, 45, tzinfo=UTC)


async def test_statistics_metadata_no_has_mean_and_no_recorder_safe(hass, caplog):
    meta = statistics._metadata("edistribuzione:it001_energia", "test")
    assert "has_mean" not in meta
    assert await statistics.async_get_ultima_data_disponibile(hass, POD_A) is None

    huge = [{"readings": {"sampleValues": [{"id": "bad", "val": "x" * 1000}]}}] * 20
    with caplog.at_level("WARNING"):
        res = await statistics.async_import_curva_giornaliera(hass, POD_A, huge)
    assert res is None
    assert POD_A not in caplog.text
    assert len(caplog.text) < 2000


def test_pod_configurati_sensor_disables_polling():
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PODS: [POD_A], CONF_REFRESH_TOKEN: "rt"})
    coord = SimpleNamespace(pods=[POD_A], lingua="it", entry=entry)
    ent = sensor.PodConfiguratiSensor(coord, entry)
    assert ent.should_poll is False


async def test_lovelace_card_concurrent_registration_registers_once(hass):
    hass.http = Mock(async_register_static_paths=AsyncMock())
    hass.config.components.add("frontend")

    with patch("homeassistant.components.frontend.add_extra_js_url") as add_js:
        await asyncio.gather(
            lovelace_card.async_registra_card(hass),
            lovelace_card.async_registra_card(hass),
        )

    assert hass.http.async_register_static_paths.await_count == 1
    assert add_js.call_count == 1
