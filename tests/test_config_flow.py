"""Test del config flow: login/OTP/POD, options flow (tipo POD, aggiungi/
rimuovi POD, orario) e reauth.

Tutto ciò che tocca la rete è sostituito: i client AuthClient / ApiClient e
le sessioni aiohttp. Niente wizard ARERA/comune (un solo distributore, un
solo flusso): si parte direttamente dallo step "user".
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import api as edist_api
from custom_components.edistribuzione import auth as edist_auth
from custom_components.edistribuzione import config_flow as cf
from custom_components.edistribuzione.auth import (
    InvalidCredentials,
    InvalidOtp,
    ParsingError,
    TroppeSessioni,
)
from custom_components.edistribuzione.const import (
    CONF_ORA_RICHIESTA,
    CONF_PODS,
    CONF_REFRESH_TOKEN,
    CONF_TIPO_POD,
    DOMAIN,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)


@pytest.fixture(autouse=True)
def _mock_setup_entry():
    """Evita che una CREATE_ENTRY (o un async_reload dopo reauth/opzioni)
    faccia partire il setup reale della entry - coordinator, sessione
    aiohttp, primo refresh: qui interessa solo l'esito del flow."""
    with patch("custom_components.edistribuzione.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def edist_mocks(monkeypatch):
    """Sostituisce AuthClient / ApiClient e le sessioni aiohttp. Default:
    login e OTP ok, un solo POD sull'account."""
    auth = Mock()
    auth.async_begin_login = AsyncMock(return_value=None)
    auth.otp_invio_confermato = True
    auth.async_resend_otp = AsyncMock(return_value=True)
    auth.async_submit_otp = AsyncMock(
        return_value=SimpleNamespace(access_token="acc", refresh_token="ref-nuovo")
    )
    auth.async_refresh_access_token = AsyncMock(
        return_value=SimpleNamespace(access_token="acc2", refresh_token="ref-nuovo")
    )
    api = Mock()
    api.async_get_supplies = AsyncMock(return_value=[{"IdPod": "IT001E00000009"}])

    monkeypatch.setattr(edist_auth, "AuthClient", Mock(return_value=auth))
    monkeypatch.setattr(edist_api, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(cf, "AuthClient", Mock(return_value=auth))
    monkeypatch.setattr(cf, "ApiClient", Mock(return_value=api))
    monkeypatch.setattr(cf, "async_create_clientsession", lambda *a, **k: Mock())
    monkeypatch.setattr(cf, "async_get_clientsession", lambda *a, **k: Mock())
    return SimpleNamespace(auth=auth, api=api)


async def _fino_a_user(hass):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})


# --- login -> OTP -----------------------------------------------------------


async def test_credenziali_non_valide(hass, edist_mocks):
    edist_mocks.auth.async_begin_login.side_effect = InvalidCredentials("no")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "user"
    assert res["errors"] == {"base": "invalid_auth"}


async def test_pagina_login_cambiata(hass, edist_mocks):
    edist_mocks.auth.async_begin_login.side_effect = ParsingError("markup")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["errors"] == {"base": "cannot_connect"}


async def test_troppe_sessioni_al_login(hass, edist_mocks):
    """Credenziali giuste ma account con troppe sessioni aperte: in questo
    stato E-Distribuzione non invia nessun OTP, quindi il flow deve fermarsi
    sul form delle credenziali, non proseguire a chiedere un codice che non
    arriverà."""
    edist_mocks.auth.async_begin_login.side_effect = TroppeSessioni(
        "Hai superato il numero di sessioni simultanee consentite"
    )
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "user"
    assert res["errors"] == {"base": "troppe_sessioni"}


async def test_otp_non_valido(hass, edist_mocks):
    edist_mocks.auth.async_submit_otp.side_effect = InvalidOtp("nope")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["step_id"] == "otp"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "000000"})
    assert res["errors"] == {"base": "invalid_otp"}


async def test_otp_form_vuoto_chiede_il_codice(hass, edist_mocks):
    """Il campo OTP è Optional (serve a poter richiedere un nuovo codice a
    campo vuoto): senza codice e senza spunta, il form si ripresenta con un
    errore invece di provare a convalidare una stringa vuota."""
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": ""})
    assert res["step_id"] == "otp"
    assert res["errors"] == {"base": "otp_mancante"}
    edist_mocks.auth.async_submit_otp.assert_not_called()


async def test_richiesta_nuovo_otp(hass, edist_mocks):
    hass.config.language = "it"
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"otp": "", "richiedi_nuovo_codice": True}
    )
    edist_mocks.auth.async_resend_otp.assert_awaited_once()
    edist_mocks.auth.async_submit_otp.assert_not_called()
    assert res["step_id"] == "otp"
    assert res["errors"] == {}
    assert "nuovo codice" in res["description_placeholders"]["avviso"]


@pytest.mark.parametrize(
    ("lingua", "atteso"),
    [("it", "non ha confermato"), ("en", "did not confirm"), ("de", "did not confirm")],
)
async def test_avviso_se_invio_otp_non_confermato(hass, edist_mocks, lingua, atteso):
    """L'avviso è un description_placeholder, che HA non traduce: segue la
    lingua del server, con l'inglese per le lingue non supportate."""
    hass.config.language = lingua
    edist_mocks.auth.otp_invio_confermato = False
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["step_id"] == "otp"
    assert atteso in res["description_placeholders"]["avviso"]


async def test_otp_parsing_fallito_abortisce(hass, edist_mocks):
    """L'OTP è già stato accettato ma un passo successivo fallisce: il
    flusso abortisce invece di far ripresentare lo stesso OTP (monouso)."""
    edist_mocks.auth.async_submit_otp.side_effect = ParsingError("consent page")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "otp_exchange_failed"


# --- selezione POD ----------------------------------------------------------


async def test_un_solo_pod_crea_entry_subito(hass, edist_mocks):
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_PODS] == ["IT001E00000009"]
    assert res["data"][CONF_REFRESH_TOKEN] == "ref-nuovo"


async def test_nessun_pod_sull_account_abortisce(hass, edist_mocks):
    edist_mocks.api.async_get_supplies.return_value = []
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "no_pods_found"


async def test_recupero_pod_fallito_abortisce(hass, edist_mocks):
    edist_mocks.api.async_get_supplies.side_effect = RuntimeError("boom")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "supplies_failed"


async def test_piu_pod_si_scelgono(hass, edist_mocks):
    edist_mocks.api.async_get_supplies.return_value = [
        {"IdPod": "IT001E00000009"},
        {"IdPod": "IT001E00000010"},
    ]
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["step_id"] == "pod"

    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {CONF_PODS: ["IT001E00000010"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_PODS] == ["IT001E00000010"]


# --- reauth ------------------------------------------------------------------


def _entry_edist(hass, pods=("IT001E00000009",), options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PODS: list(pods), CONF_REFRESH_TOKEN: "ref-vecchio"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_aggiorna_refresh_token(hass, edist_mocks):
    entry = _entry_edist(hass)
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    assert res["step_id"] == "user"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-nuovo"


# --- options flow ------------------------------------------------------------


async def test_opzioni_menu(hass):
    entry = _entry_edist(hass)
    res = await hass.config_entries.options.async_init(entry.entry_id)
    assert res["type"] == FlowResultType.MENU
    assert set(res["menu_options"]) == {"tipo_pod", "aggiungi_pod", "rimuovi_pod", "orario"}


async def test_opzioni_orario_salva_valore(hass):
    entry = _entry_edist(hass)
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "orario"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {CONF_ORA_RICHIESTA: 21}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ORA_RICHIESTA] == 21


async def test_opzioni_tipo_pod_default_e_scambio(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "tipo_pod"}
    )
    assert res["step_id"] == "tipo_pod"
    assert res["data_schema"]({})["tipo_IT001E00000009"] == TIPO_POD_SCAMBIO


async def test_opzioni_tipo_pod_salva_e_persiste(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "tipo_pod"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"tipo_IT001E00000009": TIPO_POD_PRODUZIONE}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TIPO_POD] == {"IT001E00000009": TIPO_POD_PRODUZIONE}


async def test_opzioni_tipo_pod_modificabile_di_nuovo(hass):
    """Cambiare il ruolo non è un'operazione unica: riaprendo lo step si
    deve vedere il valore già salvato, non tornare al default."""
    entry = _entry_edist(
        hass, pods=["IT001E00000009"], options={CONF_TIPO_POD: {"IT001E00000009": TIPO_POD_PRODUZIONE}}
    )
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "tipo_pod"}
    )
    assert res["data_schema"]({})["tipo_IT001E00000009"] == TIPO_POD_PRODUZIONE


async def test_opzioni_aggiungi_pod(hass, edist_mocks):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    edist_mocks.api.async_get_supplies.return_value = [
        {"IdPod": "IT001E00000009"},
        {"IdPod": "IT001E00000010"},
    ]
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "aggiungi_pod"}
    )
    assert res["step_id"] == "aggiungi_pod"
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"pods_da_aggiungere": ["IT001E00000010"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PODS] == ["IT001E00000009", "IT001E00000010"]


async def test_opzioni_rimuovi_pod_non_tutti(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009", "IT001E00000010"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "rimuovi_pod"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"],
        {"pods_da_rimuovere": ["IT001E00000009", "IT001E00000010"]},
    )
    assert res["type"] == FlowResultType.FORM
    assert res["errors"] == {"pods_da_rimuovere": "non_puoi_rimuoverli_tutti"}


async def test_opzioni_rimuovi_pod(hass):
    entry = _entry_edist(hass, pods=["IT001E00000009", "IT001E00000010"])
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "rimuovi_pod"}
    )
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"pods_da_rimuovere": ["IT001E00000010"]}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert entry.data[CONF_PODS] == ["IT001E00000009"]
