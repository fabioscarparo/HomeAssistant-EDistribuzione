"""Config flow: login/OTP/POD, options flow (POD role, add/remove POD,
request time) and reauth.

Everything that touches the network is replaced: the AuthClient / ApiClient
clients and the aiohttp sessions. No ARERA/municipality wizard (a single
distributor, a single flow): it starts straight from the "user" step.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.edistribuzione import api as edist_api
from custom_components.edistribuzione import auth as edist_auth
from custom_components.edistribuzione import config_flow as cf
from custom_components.edistribuzione.auth import (
    AccessoBloccato,
    AccountBloccato,
    AppBloccata,
    ErroreTecnicoPortale,
    InvalidCredentials,
    InvalidOtp,
    LoginRifiutato,
    ParsingError,
    PasswordScaduta,
    TokenNonValido,
    TroppeSessioni,
)
from custom_components.edistribuzione.const import (
    CONF_ACCESS_TOKEN,
    CONF_INSTANCE_URL,
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
    """Keeps a CREATE_ENTRY (or an async_reload after reauth/options) from
    starting the entry's real setup - coordinator, aiohttp session, first
    refresh: only the outcome of the flow matters here."""
    with patch("custom_components.edistribuzione.async_setup_entry", return_value=True):
        yield


@pytest.fixture
def edist_mocks(monkeypatch):
    """Replaces AuthClient / ApiClient and the aiohttp sessions. Default:
    login and OTP succeed, a single POD on the account."""
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
    auth.prepare_browser_token_authorization_url = Mock(
        return_value="https://login.example/authorize"
    )
    auth.tokens_from_browser_redirect = Mock(
        return_value=SimpleNamespace(
            access_token="acc-browser",
            refresh_token="ref-browser",
            instance_url="https://example.my.salesforce.com",
        )
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
    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(
        res["flow_id"], {"next_step_id": "password_login"}
    )


async def _fino_al_browser(hass):
    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    return await hass.config_entries.flow.async_configure(
        res["flow_id"], {"next_step_id": "browser_login"}
    )


async def test_login_nel_browser_crea_la_configurazione_con_i_token(hass, edist_mocks):
    res = await _fino_al_browser(hass)
    assert res["step_id"] == "browser_login"
    assert res["description_placeholders"]["authorization_url"] == "https://login.example/authorize"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"redirect_url": "eneldist://redirect#..."}
    )
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_ACCESS_TOKEN] == "acc-browser"
    assert res["data"][CONF_REFRESH_TOKEN] == "ref-browser"
    assert res["data"][CONF_INSTANCE_URL] == "https://example.my.salesforce.com"


@pytest.mark.parametrize(
    ("errore", "chiave", "messaggio"),
    [
        (edist_auth.InvalidBrowserRedirect("mancante"), "browser_url_mancante", ""),
        (
            edist_auth.InvalidBrowserRedirect("negato", "end-user denied"),
            "browser_negato",
            "end-user denied",
        ),
        (edist_auth.InvalidBrowserRedirect("altro_tentativo"), "browser_altro_link", ""),
        (
            edist_auth.InvalidBrowserRedirect("token_mancanti", "refresh_token"),
            "browser_token_mancanti",
            "refresh_token",
        ),
        (AppBloccata("app_blocked"), "app_bloccata", ""),
    ],
)
async def test_login_nel_browser_spiega_cosa_non_va(hass, edist_mocks, errore, chiave, messaggio):
    edist_mocks.auth.tokens_from_browser_redirect.side_effect = errore
    res = await _fino_al_browser(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"redirect_url": "qualcosa"}
    )
    assert res["step_id"] == "browser_login"
    assert res["errors"] == {"base": chiave}
    assert res["description_placeholders"]["messaggio"] == messaggio


async def test_dal_login_bloccato_al_browser_con_lo_state_giusto(hass, monkeypatch):
    """The password login replaces its client on every attempt: the browser
    login keeps its own, or the pasted redirect would be checked against the
    state of another link. Real AuthClient for the browser part."""
    bloccato = Mock(async_begin_login=AsyncMock(side_effect=AccessoBloccato("antibot")))
    client_reali = iter([bloccato, edist_auth.AuthClient(None)])
    monkeypatch.setattr(cf, "AuthClient", lambda session: next(client_reali))
    monkeypatch.setattr(cf, "async_create_clientsession", lambda *a, **k: Mock())
    monkeypatch.setattr(
        cf,
        "ApiClient",
        Mock(return_value=Mock(async_get_supplies=AsyncMock(return_value=[{"IdPod": "IT001"}]))),
    )
    monkeypatch.setattr(cf, "async_get_clientsession", lambda *a, **k: Mock())

    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["step_id"] == "antibot"
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"next_step_id": "browser_login"}
    )
    # Two links: first the portal's sign-in, then the authorization.
    assert res["description_placeholders"]["login_url"] == (
        "https://private.e-distribuzione.it/PortaleClienti/s/login/"
    )
    link = res["description_placeholders"]["authorization_url"]
    state = parse_qs(urlsplit(link).query)["state"][0]
    riga = (
        f"Failed to launch 'eneldist://redirect#access_token=00D!AQ&refresh_token=5Aep"
        f"&instance_url=https%3A%2F%2Fx.my.salesforce.com&state={state}' because the scheme "
        "does not have a registered handler."
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"redirect_url": riga})
    assert res["type"] == FlowResultType.CREATE_ENTRY
    assert res["data"][CONF_ACCESS_TOKEN] == "00D!AQ"
    assert res["data"][CONF_INSTANCE_URL] == "https://x.my.salesforce.com"


async def test_errore_tecnico_propone_l_accesso_dal_browser(hass, edist_mocks):
    """Issues #4, #5, #6: on the alternative host E-Distribuzione answers the
    automated login with "Errore Tecnico". Retrying does not help: the setup
    offers the browser, as for the anti-bot check."""
    res = await _login_con_errore(hass, edist_mocks, ErroreTecnicoPortale("Errore Tecnico"))
    assert res["type"] == FlowResultType.MENU
    assert res["step_id"] == "antibot"
    assert set(res["menu_options"]) == {"browser_login", "riprova_login"}


async def test_menu_login_offre_browser_e_password(hass):
    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert res["step_id"] == "user"
    assert set(res["menu_options"]) == {"browser_login", "password_login"}


# --- login -> OTP -----------------------------------------------------------


async def test_credenziali_non_valide(hass, edist_mocks):
    edist_mocks.auth.async_begin_login.side_effect = InvalidCredentials("no")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "password_login"
    assert res["errors"] == {"base": "invalid_auth"}


async def test_login_bloccato_dalla_verifica_antibot(hass, edist_mocks):
    edist_mocks.auth.async_begin_login.side_effect = AccessoBloccato("antibot")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["type"] == FlowResultType.MENU
    assert res["step_id"] == "antibot"
    assert set(res["menu_options"]) == {"browser_login", "riprova_login"}


async def test_convalida_otp_bloccata_dalla_verifica_antibot(hass, edist_mocks):
    """The OTP is single-use: with access blocked the flow stops with a clear
    reason, as for the other errors after validation."""
    edist_mocks.auth.async_submit_otp.side_effect = AccessoBloccato("antibot")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "12345"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "accesso_bloccato"


async def _login_con_errore(hass, edist_mocks, errore):
    edist_mocks.auth.async_begin_login.side_effect = errore
    res = await _fino_a_user(hass)
    return await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )


async def test_pagina_login_cambiata(hass, edist_mocks):
    """An unexpected answer from the portal is neither a connection nor a
    credentials problem: it has its own message, with the technical detail."""
    res = await _login_con_errore(hass, edist_mocks, ParsingError("markup"))
    assert res["errors"] == {"base": "risposta_inattesa"}
    assert "markup" in res["description_placeholders"]["messaggio"]


@pytest.mark.parametrize(
    ("eccezione", "chiave"),
    [
        (InvalidCredentials, "invalid_auth"),
        (AccountBloccato, "account_bloccato"),
        (PasswordScaduta, "password_scaduta"),
        (LoginRifiutato, "login_rifiutato"),
    ],
)
async def test_ogni_rifiuto_mostra_il_suo_errore_e_il_messaggio_del_portale(
    hass, edist_mocks, eccezione, chiave
):
    """Issues #3, #5, #6: with the right credentials "invalid email or
    password" showed up. Now every refusal has its own error, always with
    E-Distribuzione's exact message."""
    res = await _login_con_errore(hass, edist_mocks, eccezione("Testo del portale"))
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "password_login"
    assert res["errors"] == {"base": chiave}
    assert res["description_placeholders"]["messaggio"] == "Testo del portale"


@pytest.mark.parametrize(
    ("errore", "chiave"),
    [
        (aiohttp.ClientConnectionError("dns"), "cannot_connect"),
        (TimeoutError(), "cannot_connect"),
        (RuntimeError("boom"), "errore_imprevisto"),
    ],
)
async def test_errori_di_rete_blocco_e_imprevisti_restano_distinti(
    hass, edist_mocks, errore, chiave
):
    res = await _login_con_errore(hass, edist_mocks, errore)
    assert res["errors"] == {"base": chiave}


async def test_l_errore_imprevisto_dice_cosa_e_successo(hass, edist_mocks):
    res = await _login_con_errore(hass, edist_mocks, RuntimeError("boom"))
    assert res["description_placeholders"]["messaggio"] == "RuntimeError: boom"


async def test_spazi_attorno_all_email_vengono_tolti(hass, edist_mocks):
    res = await _fino_a_user(hass)
    await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": " a@b.it ", "password": " x "}
    )
    # The password stays as it is: a space may be part of it.
    edist_mocks.auth.async_begin_login.assert_awaited_once_with("a@b.it", " x ")


async def test_otp_con_lettere_non_viene_inviato(hass, edist_mocks):
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "12a45"})
    assert res["step_id"] == "otp"
    assert res["errors"] == {"base": "otp_formato"}
    edist_mocks.auth.async_submit_otp.assert_not_called()


async def test_otp_con_spazi_viene_ripulito(hass, edist_mocks):
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": " 12 345 "})
    edist_mocks.auth.async_submit_otp.assert_awaited_once_with("12345")


async def test_app_bloccata_dopo_l_otp_interrompe_con_un_motivo_chiaro(hass, edist_mocks):
    edist_mocks.auth.async_submit_otp.side_effect = AppBloccata("app_blocked")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "12345"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "app_bloccata"


async def test_troppe_sessioni_al_login(hass, edist_mocks):
    """Right credentials but an account with too many open sessions: in this
    state E-Distribuzione sends no OTP, so the flow must stop on the
    credentials form, not move on to ask for a code that will not arrive."""
    edist_mocks.auth.async_begin_login.side_effect = TroppeSessioni(
        "Hai superato il numero di sessioni simultanee consentite"
    )
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["type"] == FlowResultType.FORM
    assert res["step_id"] == "password_login"
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
    """The OTP field is Optional (so a new code can be requested with the
    field empty): with no code and no tick, the form shows again with an
    error instead of trying to validate an empty string."""
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
    """The notice is a description_placeholder, which HA does not
    translate: it follows the server language, with English for the
    unsupported ones."""
    hass.config.language = lingua
    edist_mocks.auth.otp_invio_confermato = False
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    assert res["step_id"] == "otp"
    assert atteso in res["description_placeholders"]["avviso"]


async def test_otp_parsing_fallito_abortisce(hass, edist_mocks):
    """The OTP was already accepted but a later step fails: the flow aborts
    instead of asking for the same (single-use) OTP again."""
    edist_mocks.auth.async_submit_otp.side_effect = ParsingError("consent page")
    res = await _fino_a_user(hass)
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"email": "a@b.it", "password": "x"}
    )
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"otp": "123456"})
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "otp_exchange_failed"


# --- POD selection ----------------------------------------------------------


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


async def _reauth_nel_browser(hass, entry):
    res = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reauth", "entry_id": entry.entry_id}, data=entry.data
    )
    # Same choice as the setup, with the PODs being re-authenticated.
    assert res["type"] == FlowResultType.MENU
    assert set(res["menu_options"]) == {"browser_login", "password_login"}
    assert "IT001E00000009" in res["description_placeholders"]["pod_correnti"]
    res = await hass.config_entries.flow.async_configure(
        res["flow_id"], {"next_step_id": "browser_login"}
    )
    assert res["step_id"] == "browser_login"
    return await hass.config_entries.flow.async_configure(
        res["flow_id"], {"redirect_url": "eneldist://redirect#state=ok"}
    )


async def test_reauth_aggiorna_i_token(hass, edist_mocks):
    entry = _entry_edist(hass)
    res = await _reauth_nel_browser(hass, entry)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-browser"
    assert entry.data[CONF_ACCESS_TOKEN] == "acc-browser"
    assert entry.data[CONF_INSTANCE_URL] == "https://example.my.salesforce.com"


async def test_reauth_con_un_altro_account_non_sovrascrive_i_token(hass, edist_mocks):
    """The browser reuses the portal session it already has, whatever its
    account: tokens of another account must not replace the working ones."""
    entry = _entry_edist(hass)
    edist_mocks.api.async_get_supplies.return_value = [{"IdPod": "IT001E99999999"}]
    res = await _reauth_nel_browser(hass, entry)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "account_diverso"
    assert res["description_placeholders"] == {"pods": "IT001E00000009"}
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-vecchio"


async def test_reauth_se_i_pod_non_si_leggono_accetta_il_login(hass, edist_mocks):
    entry = _entry_edist(hass)
    edist_mocks.api.async_get_supplies.side_effect = RuntimeError("503")
    res = await _reauth_nel_browser(hass, entry)
    assert res["reason"] == "reauth_successful"


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
    """Changing the role is not a one-off: reopening the step must show the
    value already saved, not go back to the default."""
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


async def test_opzioni_aggiungi_pod_usa_il_token_salvato(hass, edist_mocks):
    """After a login in the browser the saved token works while the refresh
    may be blocked: adding a POD must not depend on it."""
    entry = _entry_edist(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ACCESS_TOKEN: "acc"})
    edist_mocks.api.async_get_supplies.return_value = [
        {"IdPod": "IT001E00000009"},
        {"IdPod": "IT001E00000010"},
    ]
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "aggiungi_pod"}
    )
    assert res["step_id"] == "aggiungi_pod"
    edist_mocks.auth.async_refresh_access_token.assert_not_awaited()


async def test_opzioni_token_salvato_scaduto_rinnova_e_salva(hass, edist_mocks):
    entry = _entry_edist(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_ACCESS_TOKEN: "vecchio"})
    edist_mocks.api.async_get_supplies.side_effect = [
        RuntimeError("401"),
        [{"IdPod": "IT001E00000009"}, {"IdPod": "IT001E00000010"}],
    ]
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "aggiungi_pod"}
    )
    assert res["step_id"] == "aggiungi_pod"
    edist_mocks.auth.async_refresh_access_token.assert_awaited_once_with("ref-vecchio")
    assert entry.data[CONF_ACCESS_TOKEN] == "acc2"
    assert entry.data[CONF_REFRESH_TOKEN] == "ref-nuovo"


async def _opzioni_aggiungi_pod_con_rinnovo(hass, edist_mocks, errore):
    entry = _entry_edist(hass, pods=["IT001E00000009"])
    edist_mocks.auth.async_refresh_access_token.side_effect = errore
    res = await hass.config_entries.options.async_init(entry.entry_id)
    res = await hass.config_entries.options.async_configure(
        res["flow_id"], {"next_step_id": "aggiungi_pod"}
    )
    return entry, res


async def test_opzioni_aggiungi_pod_con_accesso_scaduto_chiede_un_nuovo_login(
    hass, edist_mocks
):
    entry, res = await _opzioni_aggiungi_pod_con_rinnovo(
        hass, edist_mocks, TokenNonValido("invalid_grant")
    )
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == "accesso_scaduto"
    flussi = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flussi] == ["reauth"]
    assert flussi[0]["context"]["entry_id"] == entry.entry_id


@pytest.mark.parametrize(
    ("errore", "motivo"),
    [
        (AppBloccata("app_blocked"), "app_bloccata"),
        (AccessoBloccato("antibot"), "accesso_bloccato"),
        (RuntimeError("boom"), "refresh_failed"),
    ],
)
async def test_opzioni_aggiungi_pod_rinnovo_fallito(hass, edist_mocks, errore, motivo):
    _, res = await _opzioni_aggiungi_pod_con_rinnovo(hass, edist_mocks, errore)
    assert res["type"] == FlowResultType.ABORT
    assert res["reason"] == motivo
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)


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
