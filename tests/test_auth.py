"""auth.py: HTML/regex parsing and pure logic, with no real network call
(only fake in-memory sessions).

auth.py itself does not depend on Home Assistant (only aiohttp and the
standard library), but the package's __init__.py does (it imports
homeassistant.config_entries). To test it in isolation (faster, and checkable
even without homeassistant installed) this file loads const.py and auth.py
DIRECTLY through importlib, bypassing __init__.py - the same trick used by
scripts/verify_login.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import aiohttp
import pytest

PKG_DIR = Path(__file__).parent.parent / "custom_components" / "edistribuzione"


def _load_auth_module():
    pkg_name = "edistribuzione_test_auth"
    if f"{pkg_name}.auth" in sys.modules:
        return sys.modules[f"{pkg_name}.auth"]

    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str, filename: str):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{modname}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    _load("const", "const.py")
    return _load("auth", "auth.py")


auth = _load_auth_module()


# ---------------------------------------------------------------------------
# _estrai_fwuid
# ---------------------------------------------------------------------------


class TestEstraiFwuid:
    def test_forma_diretta(self):
        html = '<script>var ctx = {"mode":"PROD","fwuid":"ABC123"};</script>'
        assert auth.AuthClient._estrai_fwuid(html) == "ABC123"

    def test_forma_percent_encoded(self):
        """Confirmed on a real capture: the fwuid is embedded percent-encoded
        in the path of a bootstrap URL, not in a plain JS variable."""
        html = (
            '<script src="/PortaleClienti/s/sfsites/l/%7B%22mode%22%3A%22PROD%22'
            '%2C%22fwuid%22%3A%22XYZ789%22%2C%22loaded%22%3A%7B%7D%7D/app.js">'
            "</script>"
        )
        assert auth.AuthClient._estrai_fwuid(html) == "XYZ789"

    def test_non_trovato_solleva_errore(self):
        with pytest.raises(auth.ParsingError):
            auth.AuthClient._estrai_fwuid("<html>niente qui</html>")


class TestOAuthBrowser:
    def _client(self):
        return auth.AuthClient(session=None)

    def test_url_browser_usa_user_agent_flow_con_state(self):
        from urllib.parse import parse_qs, urlparse

        client = self._client()
        url = client.prepare_browser_token_authorization_url()
        params = parse_qs(urlparse(url).query)

        assert params["response_type"] == ["token"]
        assert params["state"] == [client._oauth_state]
        assert params["redirect_uri"] == [auth.OAUTH_REDIRECT_URI]

    def test_redirect_browser_estrae_token_e_istanza(self):
        from urllib.parse import urlencode

        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = (
            f"{auth.OAUTH_REDIRECT_URI}#"
            + urlencode({
                "access_token": "access",
                "refresh_token": "refresh",
                "instance_url": "https://example.my.salesforce.com",
                "state": client._oauth_state,
            })
        )

        tokens = client.tokens_from_browser_redirect(redirect)

        assert tokens.access_token == "access"
        assert tokens.refresh_token == "refresh"
        assert tokens.instance_url == "https://example.my.salesforce.com"

    def _redirect(self, client, **parametri) -> str:
        from urllib.parse import urlencode

        return f"{auth.OAUTH_REDIRECT_URI}#" + urlencode(
            {"state": client._oauth_state, **parametri}, safe="!"
        )

    def test_redirect_di_un_altro_link_e_rifiutato(self):
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = self._redirect(client, state="other", access_token="a", refresh_token="r")

        with pytest.raises(auth.InvalidBrowserRedirect) as errore:
            client.tokens_from_browser_redirect(redirect)
        assert errore.value.motivo == "altro_tentativo"

    def test_ogni_link_ha_uno_state_nuovo(self):
        client = self._client()
        client.prepare_browser_token_authorization_url()
        vecchio = self._redirect(client, access_token="a", refresh_token="r")
        client.prepare_browser_token_authorization_url()
        with pytest.raises(auth.InvalidBrowserRedirect):
            client.tokens_from_browser_redirect(vecchio)

    def test_accetta_la_riga_intera_della_console_di_chrome(self):
        """What the user copies from Chrome's console, quotes included."""
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = self._redirect(client, access_token="00D!AQ", refresh_token="5Aep")
        riga = (
            f"Failed to launch '{redirect}' because the scheme does not have a "
            "registered handler."
        )
        tokens = client.tokens_from_browser_redirect(riga)
        assert (tokens.access_token, tokens.refresh_token) == ("00D!AQ", "5Aep")

    def test_il_piu_nei_token_non_diventa_uno_spazio(self):
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = (
            f"{auth.OAUTH_REDIRECT_URI}#access_token=00D!AQ+abc&refresh_token=5Aep+xyz"
            f"&state={client._oauth_state}"
        )
        tokens = client.tokens_from_browser_redirect(redirect)
        assert (tokens.access_token, tokens.refresh_token) == ("00D!AQ+abc", "5Aep+xyz")

    def test_senza_indirizzo_dell_app(self):
        client = self._client()
        url = client.prepare_browser_token_authorization_url()
        with pytest.raises(auth.InvalidBrowserRedirect) as errore:
            # The sign-in link itself, pasted back by mistake.
            client.tokens_from_browser_redirect(url)
        assert errore.value.motivo == "mancante"

    def test_rifiuto_del_portale_con_la_sua_descrizione(self):
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = (
            f"{auth.OAUTH_REDIRECT_URI}#error=access_denied"
            f"&error_description=end-user+denied+authorization&state={client._oauth_state}"
        )
        with pytest.raises(auth.InvalidBrowserRedirect) as errore:
            client.tokens_from_browser_redirect(redirect)
        assert errore.value.motivo == "negato"
        assert errore.value.dettaglio == "end-user denied authorization"

    def test_app_blocked_nel_redirect(self):
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = self._redirect(client, error="app_blocked")
        with pytest.raises(auth.AppBloccata):
            client.tokens_from_browser_redirect(redirect)

    def test_senza_refresh_token(self):
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = self._redirect(client, access_token="a")
        with pytest.raises(auth.InvalidBrowserRedirect) as errore:
            client.tokens_from_browser_redirect(redirect)
        assert errore.value.motivo == "token_mancanti"
        assert errore.value.dettaglio == "refresh_token"

    def test_nei_log_non_finiscono_i_token(self, caplog):
        """Debug logs are meant to be attached to public issues."""
        client = self._client()
        client.prepare_browser_token_authorization_url()
        redirect = self._redirect(
            client, access_token="SEGRETO-ACCESSO", refresh_token="SEGRETO-RINNOVO"
        )
        with caplog.at_level("DEBUG"):
            client.tokens_from_browser_redirect(redirect)
            with pytest.raises(auth.InvalidBrowserRedirect):
                client.tokens_from_browser_redirect(redirect.replace("state=", "state=x"))
        assert "SEGRETO" not in caplog.text
        assert "access_token" in caplog.text


# ---------------------------------------------------------------------------
# _estrai_loaded
# ---------------------------------------------------------------------------


class TestEstraiLoaded:
    def test_forma_diretta(self):
        html = '{"loaded":{"APPLICATION@markup://siteforce:loginApp2":"123"}}'
        assert (
            auth.AuthClient._estrai_loaded(html)
            == '{"APPLICATION@markup://siteforce:loginApp2":"123"}'
        )

    def test_forma_percent_encoded(self):
        """Same percent-encoded blob as _estrai_fwuid: 'loaded' holds a
        reference to the loaded component version, needed because an
        aura.context with an empty 'loaded':{} is rejected by the server
        (AuraClientInputException)."""
        html = (
            "%22loaded%22%3A%7B%22APPLICATION%40markup%3A%2F%2Fsiteforce"
            "%3AloginApp2%22%3A%221628_TW-5Qu0N5YI_dQXZ9Cqalw%22%7D"
        )
        risultato = auth.AuthClient._estrai_loaded(html)
        assert risultato == (
            '{"APPLICATION@markup://siteforce:loginApp2":"1628_TW-5Qu0N5YI_dQXZ9Cqalw"}'
        )

    def test_fallback_oggetto_vuoto_se_non_trovato(self):
        """Raises no error: it degrades to '{}' and lets the server report the
        problem (already diagnosed elsewhere)."""
        assert auth.AuthClient._estrai_loaded("<html>niente</html>") == "{}"


# ---------------------------------------------------------------------------
# _estrai_url_redirect_js
# ---------------------------------------------------------------------------


class TestEstraiUrlRedirectJs:
    def test_window_location_replace(self):
        html = "<script>window.location.replace('https://example.com/target?a=1&b=2');</script>"
        assert (
            auth.AuthClient._estrai_url_redirect_js(html)
            == "https://example.com/target?a=1&b=2"
        )

    def test_window_location_href_variante(self):
        html = "<script>window.location.href = 'https://example.com/other';</script>"
        assert auth.AuthClient._estrai_url_redirect_js(html) == "https://example.com/other"

    def test_non_trovato_solleva_errore(self):
        with pytest.raises(auth.ParsingError):
            auth.AuthClient._estrai_url_redirect_js("<html>niente qui</html>")


# ---------------------------------------------------------------------------
# _analizza_pagina_otp
# ---------------------------------------------------------------------------


class TestAnalizzaPaginaOtp:
    """The most important group: it covers in particular the regression of
    the Richiedi_nuovo_OTP field (checkbox vs hidden), which took several
    rounds of debugging with real data to find."""

    HTML_PAGINA_OTP = """
    <html><body>
      <input type="hidden" name="com.salesforce.visualforce.ViewState" value="VS123" />
      <input type="hidden" name="com.salesforce.visualforce.ViewStateVersion" value="VSV456" />
      <input type="hidden" name="com.salesforce.visualforce.ViewStateMAC" value="VSM789" />
      <input type="hidden" name="com.salesforce.visualforce.ViewStateCSRF" value="VSC000" />
      <input id="thePage:j_id2:i:f:pb:d:OTP_Input.input"
             name="thePage:j_id2:i:f:pb:d:element___input____OTP_Input"
             type="text" value="" />
      <input id="thePage:j_id2:i:f:pb:d:Richiedi_nuovo_OTP.input"
             name="thePage:j_id2:i:f:pb:d:element___input____Richiedi_nuovo_OTP"
             type="checkbox" value="true" />
      <input type="hidden"
             id="thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"
             name="thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"
             value="false" />
    </body></html>
    """

    @staticmethod
    def _client():
        return auth.AuthClient(session=None)

    def test_estrae_tutti_i_campi_viewstate(self):
        client = self._client()
        client._analizza_pagina_otp(self.HTML_PAGINA_OTP)
        assert client._flow.view_state == "VS123"
        assert client._flow.view_state_version == "VSV456"
        assert client._flow.view_state_mac == "VSM789"
        assert client._flow.view_state_csrf == "VSC000"

    def test_resend_field_e_il_campo_hidden_non_la_checkbox(self):
        """The extraction must take the hidden field
        (element___hidden____Richiedi_nuovo_OTP), not the visible checkbox:
        getting it wrong, the server treats every submit as yet another
        resend request instead of a code validation."""
        client = self._client()
        client._analizza_pagina_otp(self.HTML_PAGINA_OTP)
        assert client._flow.resend_field_name == (
            "thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"
        )
        assert "input____Richiedi_nuovo_OTP" not in client._flow.resend_field_name

    def test_ordine_attributi_invertito_nel_tag(self):
        """The regex must not assume 'name' always comes before 'value' in
        the tag: with the order swapped (value then name), it must work
        anyway."""
        html_invertito = (
            '<input type="hidden" value="VS_INVERTITO" '
            'name="com.salesforce.visualforce.ViewState" />'
        )
        client = self._client()
        # Only the first field is there: extracting the second one will fail,
        # but we want to check that the FIRST was read correctly despite the
        # swapped order of the attributes.
        with pytest.raises(auth.ParsingError):
            client._analizza_pagina_otp(html_invertito)
        assert client._flow.view_state == "VS_INVERTITO"

    def test_campo_mancante_solleva_errore_con_nome_campo(self):
        client = self._client()
        with pytest.raises(auth.ParsingError, match="ViewState"):
            client._analizza_pagina_otp("<html><title>Sessione scaduta</title></html>")

    def test_cattura_anche_la_checkbox_di_reinvio(self):
        """The visible checkbox is used for the explicit resend of the code
        (async_resend_otp), where we reproduce a browser's submit with the box
        ticked - the hidden field alone is not enough to represent it."""
        client = self._client()
        client._analizza_pagina_otp(self.HTML_PAGINA_OTP)
        assert client._flow.resend_checkbox_field_name == (
            "thePage:j_id2:i:f:pb:d:element___input____Richiedi_nuovo_OTP"
        )


# ---------------------------------------------------------------------------
# _form_invio_iniziale / _form_richiedi_nuovo / _form_convalida
# (the three payload builders of the OTP form)
# ---------------------------------------------------------------------------


class TestCostruttoriFormOtp:
    @staticmethod
    def _client():
        client = auth.AuthClient(session=None)
        client._analizza_pagina_otp(TestAnalizzaPaginaOtp.HTML_PAGINA_OTP)
        return client

    def test_invio_iniziale_senza_codice_ne_flag_di_reinvio(self):
        """The first submit only makes the code be sent: it must contain
        neither an OTP to validate nor the resend flag."""
        dati = self._client()._form_invio_iniziale()
        assert not [k for k in dati if "OTP_Input" in k]
        assert not [k for k in dati if "Richiedi_nuovo_OTP" in k]
        assert dati["com.salesforce.visualforce.ViewState"] == "VS123"

    def test_convalida_codice_manda_hidden_a_false(self):
        """With the resend flag at 'true' the server would treat the
        validation as yet another request for a new code."""
        dati = self._client()._form_convalida("12345")
        assert dati["thePage:j_id2:i:f:pb:d:element___input____OTP_Input"] == "12345"
        assert dati["thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"] == "false"

    def test_reinvio_manda_hidden_e_checkbox_a_true_senza_codice(self):
        dati = self._client()._form_richiedi_nuovo()
        assert dati["thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"] == "true"
        assert dati["thePage:j_id2:i:f:pb:d:element___input____Richiedi_nuovo_OTP"] == "true"
        assert not [k for k in dati if "OTP_Input" in k]


# ---------------------------------------------------------------------------
# Sending / resending the code: confirmation and session limit
# ---------------------------------------------------------------------------


class _RispostaFinta:
    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class _SessioneFinta:
    """The bare minimum for _async_post_form_otp: records the data sent and
    always returns the same page."""

    def __init__(self, body: str) -> None:
        self._body = body
        self.dati_inviati: dict | None = None

    def post(self, url, data=None, headers=None):
        self.dati_inviati = data
        return _RispostaFinta(self._body)


PAGINA_OTP_CON_CONFERMA = (
    "<html><body><p>Abbiamo inviato un codice a 5 cifre al tuo indirizzo email</p>"
    + TestAnalizzaPaginaOtp.HTML_PAGINA_OTP
    + "</body></html>"
)


def _client_su_form_otp(body: str):
    session = _SessioneFinta(body)
    client = auth.AuthClient(session)
    client._analizza_pagina_otp(TestAnalizzaPaginaOtp.HTML_PAGINA_OTP)
    return client, session


class TestInvioOtp:
    async def test_conferma_invio_riconosciuta(self):
        client, _ = _client_su_form_otp(PAGINA_OTP_CON_CONFERMA)
        await client._async_avvia_invio_otp()
        assert client.otp_invio_confermato is True

    async def test_invio_non_confermato_non_e_fatale_ma_viene_segnalato(
        self, caplog, monkeypatch, tmp_path
    ):
        """If the page contains none of the known confirmation messages the
        flow goes on (the wording might just be different), but it is logged
        and the flag lets the config flow warn the user."""
        # The debug dump is written to the cwd: it is redirected to tmp_path so
        # that otp_send_debug.html is not left inside the repository.
        monkeypatch.chdir(tmp_path)
        client, _ = _client_su_form_otp(TestAnalizzaPaginaOtp.HTML_PAGINA_OTP)
        await client._async_avvia_invio_otp()
        assert client.otp_invio_confermato is False
        assert "non ha confermato l'invio" in caplog.text
        # The dump is written in an executor (not to block Home Assistant's event
        # loop), so it may not exist yet on return.
        dump = tmp_path / "otp_send_debug.html"
        for _ in range(50):
            if dump.exists():
                break
            await asyncio.sleep(0.02)
        assert dump.exists()

    async def test_pagina_limite_sessioni_solleva_eccezione_dedicata(self, monkeypatch, tmp_path):
        """With too many open sessions no code is sent: it must be told apart
        from a wrong OTP or a markup change, otherwise the user is left waiting
        for a code that will never arrive."""
        monkeypatch.chdir(tmp_path)
        client, _ = _client_su_form_otp(
            "<html><body>Hai superato il numero di sessioni simultanee "
            "consentite</body></html>"
        )
        with pytest.raises(auth.TroppeSessioni):
            await client._async_avvia_invio_otp()

    async def test_resend_otp_invia_il_flag_di_reinvio(self):
        client, session = _client_su_form_otp(PAGINA_OTP_CON_CONFERMA)
        assert await client.async_resend_otp() is True
        assert (
            session.dati_inviati["thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"]
            == "true"
        )

    async def test_resend_otp_prima_del_login_e_un_errore(self):
        client = auth.AuthClient(session=None)
        with pytest.raises(auth.AuthError, match="async_begin_login"):
            await client.async_resend_otp()


# ---------------------------------------------------------------------------
# OAuth consent screen
# ---------------------------------------------------------------------------


# Structure modelled on the real page: title "Consentire l'accesso?", a
# single form with 8 hidden fields and TWO submit buttons with the same
# name="save", told apart only by the value - "Consenti" approves, " Nega "
# (with the spaces) rejects.
HTML_PAGINA_CONSENSO = """
<html><head><title>Consentire l'accesso? | Portale Clienti</title></head>
<body>
  <form id="editPage" method="post"
        action="/PortaleClienti/_ui/identity/oauth/ui/AuthorizationPage">
    <input type="hidden" name="_CONFIRMATIONTOKEN" value="TOKEN123" />
    <input type="hidden" name="cancelURL" value="/PortaleClienti/home/home.jsp" />
    <input type="hidden" name="retURL" value="/PortaleClienti/home/home.jsp" />
    <input type="hidden" name="save_new_url"
           value="/services/oauth2/approval?a=1&amp;b=2&amp;c=l&#39;app" />
    <input type="hidden" name="source" value="SORGENTE456" />
    <input type="hidden" name="scope_hint" value="web api openid" />
    <input type="hidden" name="authPageHint" value="HINT789" />
    <input type="hidden" name="display" value="touch" />
    <input type="submit" name="save" value="Consenti" class="button primary" />
    <input type="submit" name="save" value=" Nega " id="oadeny" />
  </form>
</body></html>
"""


class TestFormConsenso:
    def test_seleziona_consenti_e_non_nega(self):
        """The two buttons have the same name: taking the wrong one would mean
        DENYING the integration's authorization."""
        action, dati = auth.AuthClient._estrai_form_consenso(HTML_PAGINA_CONSENSO)
        assert dati["save"] == "Consenti"
        assert action == (
            "https://private.e-distribuzione.it/PortaleClienti/_ui/identity/oauth/ui/AuthorizationPage"
        )

    def test_rimanda_tutti_i_campi_hidden_deescapati(self):
        """save_new_url and source are URLs full of parameters: if they stay
        escaped (&amp;) the server receives values different from the ones it
        generated."""
        _, dati = auth.AuthClient._estrai_form_consenso(HTML_PAGINA_CONSENSO)
        assert set(dati) == {
            "_CONFIRMATIONTOKEN",
            "cancelURL",
            "retURL",
            "save_new_url",
            "source",
            "scope_hint",
            "authPageHint",
            "display",
            "save",
        }
        assert dati["save_new_url"] == "/services/oauth2/approval?a=1&b=2&c=l'app"

    def test_pagina_senza_pulsante_di_consenso_ritorna_none(self):
        """On a page that is not a consent screen the caller must be able to
        go on with its usual parsing error."""
        assert (
            auth.AuthClient._estrai_form_consenso(
                "<html><body><form><input type='submit' name='x' value='Invia'>"
                "</form></body></html>"
            )
            is None
        )


class _RispostaConLocation(_RispostaFinta):
    def __init__(self, body: str, location: str | None = None) -> None:
        super().__init__(body)
        self.headers = {"Location": location} if location else {}


class _SessioneACoda:
    """Returns the queued responses, one per call (get or post), and records
    the payloads sent."""

    def __init__(self, risposte: list) -> None:
        self._risposte = list(risposte)
        self.post_inviati: list[dict] = []

    def _prossima(self):
        return self._risposte.pop(0)

    def post(self, url, data=None, headers=None, allow_redirects=None):
        self.post_inviati.append({"url": url, "data": data})
        return self._prossima()

    def get(self, url, headers=None):
        return self._prossima()


class TestApprovazioneConsenso:
    async def test_preme_consenti_e_recupera_il_codice(self):
        """After the OTP the consent screen arrives instead of the code;
        pressing "Consenti" the code arrives in the Location of the redirect to
        the app's custom scheme (eneldist://), which aiohttp cannot follow."""
        session = _SessioneACoda([
            _RispostaConLocation(
                "eneldist://redirect?code=CODICE_OK&state=S",
                location="eneldist://redirect?code=CODICE_OK&state=S",
            )
        ])
        client = auth.AuthClient(session)
        risultato = await client._async_approva_consenso(HTML_PAGINA_CONSENSO)
        assert "code=CODICE_OK" in risultato
        assert session.post_inviati[0]["data"]["save"] == "Consenti"
        assert session.post_inviati[0]["url"].endswith("/AuthorizationPage")

    async def test_senza_form_di_consenso_ritorna_none(self):
        client = auth.AuthClient(_SessioneACoda([]))
        assert await client._async_approva_consenso("<html>niente</html>") is None


# ---------------------------------------------------------------------------
# _verifica_non_troppe_sessioni (centralised: it was duplicated in three places)
# ---------------------------------------------------------------------------


class TestVerificaNonTroppeSessioni:
    def test_nessun_marcatore_non_solleva(self):
        auth.AuthClient._verifica_non_troppe_sessioni("tutto normale qui")

    def test_marcatore_presente_solleva(self):
        with pytest.raises(auth.TroppeSessioni):
            auth.AuthClient._verifica_non_troppe_sessioni(
                "Hai superato il numero di sessioni simultanee consentite"
            )

    def test_salva_il_dump_solo_se_richiesto(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(auth.TroppeSessioni):
            auth.AuthClient._verifica_non_troppe_sessioni(
                "sessioni contemporanee superate", "dump.html"
            )
        assert (tmp_path / "dump.html").exists()


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class TestGerarchiaEccezioni:
    def test_invalid_credentials_e_sottoclasse_di_auth_error(self):
        assert issubclass(auth.InvalidCredentials, auth.AuthError)

    def test_invalid_otp_e_sottoclasse_di_auth_error(self):
        assert issubclass(auth.InvalidOtp, auth.AuthError)

    def test_parsing_error_e_sottoclasse_di_auth_error(self):
        assert issubclass(auth.ParsingError, auth.AuthError)

    def test_troppe_sessioni_e_sottoclasse_di_auth_error_ma_non_di_credenziali(self):
        """It must be handled on its own: the credentials are right, it is the
        account that has too many open sessions."""
        assert issubclass(auth.TroppeSessioni, auth.AuthError)
        assert not issubclass(auth.TroppeSessioni, auth.InvalidCredentials)


# ---------------------------------------------------------------------------
# Imperva's anti-bot check in place of the expected pages
# ---------------------------------------------------------------------------

PAGINA_ANTIBOT = (
    "<html><head><title>Pardon Our Interruption</title></head><body>"
    "<p>As you were browsing something about your browser made us think you were a bot.</p>"
    '<script src="/_Incapsula_Resource?SWJIYLWA=abc"></script></body></html>'
)


class _RispostaToken(_RispostaFinta):
    def __init__(self, body: str, status: int = 200) -> None:
        super().__init__(body)
        self.status = status


class TestAccessoBloccato:
    @pytest.mark.parametrize(
        "pagina",
        [PAGINA_ANTIBOT, "<html>Request unsuccessful. Incapsula incident ID: 123-456</html>"],
    )
    def test_riconosce_la_verifica_e_il_blocco(self, pagina):
        with pytest.raises(auth.AccessoBloccato):
            auth._verifica_non_bloccato(pagina)

    def test_lo_script_di_imperva_in_una_pagina_normale_non_e_un_blocco(self):
        """Imperva also adds _Incapsula_Resource to the normal pages of a
        protected site: on its own it must not block a valid login."""
        auth._verifica_non_bloccato(
            '<html><title>Login</title><script src="/_Incapsula_Resource?x=1"></script></html>'
        )

    def test_e_un_auth_error(self):
        assert issubclass(auth.AccessoBloccato, auth.AuthError)

    async def test_refresh_bloccato_su_entrambi_gli_host(self):
        """Primary and direct host both blocked: the primary is tried, then
        the direct one, and only then AccessoBloccato is raised."""
        session = _SessioneACoda([_RispostaToken(PAGINA_ANTIBOT), _RispostaToken(PAGINA_ANTIBOT)])
        with pytest.raises(auth.AccessoBloccato):
            await auth.AuthClient(session).async_refresh_access_token("rt")
        host_provati = [u["url"].split("/")[2] for u in session.post_inviati]
        assert host_provati == ["private.e-distribuzione.it", "edistribuzione.my.site.com"]

    async def test_refresh_fallback_sul_dominio_diretto(self):
        """Primary blocked, direct host fine: the refresh succeeds through the
        direct domain, without raising."""
        session = _SessioneACoda(
            [_RispostaToken(PAGINA_ANTIBOT), _RispostaToken('{"access_token": "nuovo"}')]
        )
        tokens = await auth.AuthClient(session).async_refresh_access_token("rt")
        assert tokens.access_token == "nuovo"
        assert session.post_inviati[-1]["url"].split("/")[2] == "edistribuzione.my.site.com"

    async def test_refresh_con_una_pagina_html_qualunque_e_un_parsing_error(self):
        session = _SessioneACoda([_RispostaToken("<html>manutenzione</html>")])
        with pytest.raises(auth.ParsingError):
            await auth.AuthClient(session).async_refresh_access_token("rt")

    async def test_refresh_riuscito_tiene_il_vecchio_refresh_token(self):
        session = _SessioneACoda([_RispostaToken('{"access_token": "nuovo"}')])
        tokens = await auth.AuthClient(session).async_refresh_access_token("rt")
        assert (tokens.access_token, tokens.refresh_token) == ("nuovo", "rt")

    async def test_invio_otp_bloccato(self):
        client, _ = _client_su_form_otp(PAGINA_ANTIBOT)
        with pytest.raises(auth.AccessoBloccato):
            await client._async_avvia_invio_otp()


# ---------------------------------------------------------------------------
# Login refusals: the portal's message decides the error shown
# ---------------------------------------------------------------------------

PAGINA_LOGIN = (
    '<script>{"fwuid":"FW123","loaded":{"APPLICATION@markup://siteforce:'
    'loginApp2":"APP123"}}</script>'
)


async def _invia_credenziali_con_risposta(body: str):
    client = auth.AuthClient(_SessioneACoda([_RispostaToken(body)]))
    return await client._invia_credenziali(
        "utente@example.com",
        "password",
        PAGINA_LOGIN,
        "/start",
        "/PortaleClienti/login",
        "https://private.e-distribuzione.it/PortaleClienti/login",
    )


class TestRifiutoLogin:
    @pytest.mark.parametrize(
        ("risposta", "eccezione", "messaggio"),
        [
            ("KO: Username non valido", "InvalidCredentials", "Username non valido"),
            ("KO: Password errata", "InvalidCredentials", "Password errata"),
            ("KO: Errore Tecnico", "ErroreTecnicoPortale", "Errore Tecnico"),
            ("KO: Utente bloccato", "AccountBloccato", "Utente bloccato"),
            ("KO: La password è scaduta", "PasswordScaduta", "La password è scaduta"),
            ("KO: Servizio sospeso", "LoginRifiutato", "Servizio sospeso"),
        ],
    )
    async def test_ogni_rifiuto_ha_la_sua_eccezione_con_il_messaggio(
        self, risposta, eccezione, messaggio
    ):
        """Up to v0.6.0 every KO became "invalid email or password", even a
        technical error of the portal (issues #3, #5, #6)."""
        body = json.dumps({"actions": [{"state": "SUCCESS", "returnValue": risposta}]})
        with pytest.raises(auth.LoginRifiutato) as errore:
            await _invia_credenziali_con_risposta(body)
        assert type(errore.value).__name__ == eccezione
        assert errore.value.messaggio == messaggio

    async def test_l_errore_tecnico_non_e_un_errore_di_credenziali(self):
        body = '{"actions":[{"state":"SUCCESS","returnValue":"KO: Errore Tecnico"}]}'
        with pytest.raises(auth.ErroreTecnicoPortale) as errore:
            await _invia_credenziali_con_risposta(body)
        assert not isinstance(errore.value, auth.InvalidCredentials)

    async def test_troppe_sessioni_resta_un_caso_a_parte(self):
        body = (
            '{"actions":[{"state":"SUCCESS","returnValue":'
            '"KO: Hai superato il numero di sessioni simultanee consentite"}]}'
        )
        with pytest.raises(auth.TroppeSessioni):
            await _invia_credenziali_con_risposta(body)

    async def test_risposta_aura_senza_esito_non_e_un_errore_di_credenziali(self):
        """state ERROR and returnValue null: the controller did not even look
        at the credentials, the framework rejected the request. It is an
        unexpected answer, not "wrong password"."""
        body = (
            '{"actions":[{"state":"ERROR","returnValue":null,'
            '"error":[{"message":"An internal server error has occurred"}]}]}'
        )
        with pytest.raises(auth.ParsingError, match="internal server error"):
            await _invia_credenziali_con_risposta(body)

    async def test_l_indirizzo_di_sessione_non_finisce_nei_log(self, caplog):
        """After "OK:" comes the frontdoor address, with the session id."""
        body = (
            '{"actions":[{"state":"SUCCESS","returnValue":'
            '"OK:https://private.e-distribuzione.it/secur/frontdoor.jsp?sid=SEGRETO"}]}'
        )
        with caplog.at_level("DEBUG"):
            await _invia_credenziali_con_risposta(body)
        assert "SEGRETO" not in caplog.text
        assert "esito OK:" in caplog.text

    async def test_ok_restituisce_l_indirizzo_da_seguire(self):
        body = '{"actions":[{"state":"SUCCESS","returnValue":"OK:https://private.e-distribuzione.it/fd"}]}'
        assert await _invia_credenziali_con_risposta(body) == (
            "https://private.e-distribuzione.it/fd"
        )


# ---------------------------------------------------------------------------
# OAuth endpoint errors: client blocked, token expired, other
# ---------------------------------------------------------------------------

APP_BLOCKED = '{"error":"app_blocked","error_description":"this app is blocked by Transaction Security"}'
INVALID_GRANT = '{"error":"invalid_grant","error_description":"expired access/refresh token"}'


class TestErroriToken:
    async def test_app_blocked_e_un_blocco_non_un_token_revocato(self):
        session = _SessioneACoda([_RispostaToken(APP_BLOCKED, status=400)])
        with pytest.raises(auth.AppBloccata) as errore:
            await auth.AuthClient(session).async_refresh_access_token("rt")
        assert not isinstance(errore.value, auth.TokenNonValido)
        # On the main domain (no anti-bot) the other host is not tried: it is
        # the client that is rejected, not the domain.
        assert len(session.post_inviati) == 1

    async def test_antibot_poi_app_blocked_sul_dominio_diretto(self):
        """The case of issue #4: Imperva blocks the main domain and on the
        direct host Salesforce rejects the OAuth client."""
        session = _SessioneACoda(
            [_RispostaToken(PAGINA_ANTIBOT), _RispostaToken(APP_BLOCKED, status=400)]
        )
        with pytest.raises(auth.AppBloccata):
            await auth.AuthClient(session).async_refresh_access_token("rt")
        host_provati = [u["url"].split("/")[2] for u in session.post_inviati]
        assert host_provati == ["private.e-distribuzione.it", "edistribuzione.my.site.com"]

    async def test_invalid_grant_e_un_token_non_valido(self):
        session = _SessioneACoda([_RispostaToken(INVALID_GRANT, status=400)])
        with pytest.raises(auth.TokenNonValido, match="expired access/refresh token"):
            await auth.AuthClient(session).async_refresh_access_token("rt")

    async def test_altri_errori_oauth_restano_generici(self):
        session = _SessioneACoda([_RispostaToken('{"error":"invalid_client"}', status=400)])
        with pytest.raises(auth.AuthError) as errore:
            await auth.AuthClient(session).async_refresh_access_token("rt")
        assert type(errore.value) is auth.AuthError
        assert "invalid_client" in str(errore.value)

    async def test_app_blocked_dopo_l_otp(self):
        session = _SessioneACoda([_RispostaToken(APP_BLOCKED, status=400)])
        with pytest.raises(auth.AppBloccata):
            await auth.AuthClient(session)._async_exchange_code("codice")


class _RispostaRedirect:
    def __init__(self, location: str) -> None:
        self.headers = {"Location": location}
        self.url = aiohttp.client.URL("https://private.e-distribuzione.it/PortaleClienti/x")

    def close(self) -> None:
        pass


class _SessioneRedirect:
    def __init__(self, location: str) -> None:
        self._location = location
        self.get_eseguiti = 0

    async def get(self, url, params=None, headers=None, allow_redirects=None):
        self.get_eseguiti += 1
        return _RispostaRedirect(self._location)


class TestRedirectConErrore:
    async def test_redirect_all_app_con_errore_diventa_l_eccezione_giusta(self):
        """If the OAuth server refuses the request, it redirects to the app
        (eneldist://) with error=...: before, a GET to that non-HTTP address was
        attempted, with an obscure error."""
        session = _SessioneRedirect(
            "eneldist://redirect?error=app_blocked&error_description=blocked"
        )
        with pytest.raises(auth.AppBloccata):
            await auth._get_following_redirects(session, "https://private.e-distribuzione.it/a")
        assert session.get_eseguiti == 1
