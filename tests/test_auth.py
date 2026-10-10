"""Test per auth.py: parsing HTML/regex e logica pura, nessuna chiamata di
rete reale (solo sessioni finte in memoria).

auth.py di per sé non dipende da Home Assistant (solo aiohttp e libreria
standard), ma __init__.py del pacchetto sì (importa homeassistant.config_entries).
Per poterlo testare in isolamento (più veloce, e verificabile anche senza
homeassistant installato) questo file carica const.py e auth.py DIRETTAMENTE
via importlib, bypassando __init__.py - lo stesso trucco usato da
scripts/verify_login.py.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

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
        """Confermato su una cattura reale: il fwuid è incorporato
        percent-encoded nel path di un URL di bootstrap, non in una
        variabile JS in chiaro."""
        html = (
            '<script src="/PortaleClienti/s/sfsites/l/%7B%22mode%22%3A%22PROD%22'
            '%2C%22fwuid%22%3A%22XYZ789%22%2C%22loaded%22%3A%7B%7D%7D/app.js">'
            "</script>"
        )
        assert auth.AuthClient._estrai_fwuid(html) == "XYZ789"

    def test_non_trovato_solleva_errore(self):
        with pytest.raises(auth.ParsingError):
            auth.AuthClient._estrai_fwuid("<html>niente qui</html>")


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
        """Stesso blob percent-encoded di _estrai_fwuid: 'loaded' contiene un
        riferimento alla versione del componente caricato, necessario perché
        aura.context con 'loaded':{} vuoto viene rifiutato dal server
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
        """Non solleva errore: degrada a '{}' e lascia che sia il server a
        segnalare il problema (già diagnosticato altrove)."""
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
    """Il gruppo più importante: copre in particolare la regressione del
    campo Richiedi_nuovo_OTP (checkbox vs hidden), che ha richiesto diversi
    round di debug con dati reali per essere trovata."""

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
        """L'estrazione deve prendere il campo nascosto
        (element___hidden____Richiedi_nuovo_OTP), non la checkbox visibile:
        sbagliando qui il server tratta ogni submit come un'ennesima
        richiesta di reinvio invece che una convalida del codice."""
        client = self._client()
        client._analizza_pagina_otp(self.HTML_PAGINA_OTP)
        assert client._flow.resend_field_name == (
            "thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"
        )
        assert "input____Richiedi_nuovo_OTP" not in client._flow.resend_field_name

    def test_ordine_attributi_invertito_nel_tag(self):
        """Il regex non deve assumere 'name' sempre prima di 'value' nel
        tag: se l'ordine è invertito (value poi name), deve funzionare
        comunque."""
        html_invertito = (
            '<input type="hidden" value="VS_INVERTITO" '
            'name="com.salesforce.visualforce.ViewState" />'
        )
        client = self._client()
        # Solo il primo campo è presente: l'estrazione del secondo fallirà,
        # ma vogliamo verificare che il PRIMO sia stato letto correttamente
        # nonostante l'ordine invertito degli attributi.
        with pytest.raises(auth.ParsingError):
            client._analizza_pagina_otp(html_invertito)
        assert client._flow.view_state == "VS_INVERTITO"

    def test_campo_mancante_solleva_errore_con_nome_campo(self):
        client = self._client()
        with pytest.raises(auth.ParsingError, match="ViewState"):
            client._analizza_pagina_otp("<html><title>Sessione scaduta</title></html>")

    def test_cattura_anche_la_checkbox_di_reinvio(self):
        """La checkbox visibile serve per il reinvio esplicito del codice
        (async_resend_otp), dove riproduciamo il submit di un browser con la
        casella spuntata - il campo hidden da solo non basta a rappresentarlo."""
        client = self._client()
        client._analizza_pagina_otp(self.HTML_PAGINA_OTP)
        assert client._flow.resend_checkbox_field_name == (
            "thePage:j_id2:i:f:pb:d:element___input____Richiedi_nuovo_OTP"
        )


# ---------------------------------------------------------------------------
# _form_invio_iniziale / _form_richiedi_nuovo / _form_convalida
# (i tre costruttori di payload del form OTP)
# ---------------------------------------------------------------------------


class TestCostruttoriFormOtp:
    @staticmethod
    def _client():
        client = auth.AuthClient(session=None)
        client._analizza_pagina_otp(TestAnalizzaPaginaOtp.HTML_PAGINA_OTP)
        return client

    def test_invio_iniziale_senza_codice_ne_flag_di_reinvio(self):
        """Il primo submit serve solo a far spedire il codice: non deve
        contenere né un OTP da convalidare né il flag di reinvio."""
        dati = self._client()._form_invio_iniziale()
        assert not [k for k in dati if "OTP_Input" in k]
        assert not [k for k in dati if "Richiedi_nuovo_OTP" in k]
        assert dati["com.salesforce.visualforce.ViewState"] == "VS123"

    def test_convalida_codice_manda_hidden_a_false(self):
        """Con il flag di reinvio a 'true' il server tratterebbe la
        convalida come un'ennesima richiesta di nuovo codice."""
        dati = self._client()._form_convalida("12345")
        assert dati["thePage:j_id2:i:f:pb:d:element___input____OTP_Input"] == "12345"
        assert dati["thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"] == "false"

    def test_reinvio_manda_hidden_e_checkbox_a_true_senza_codice(self):
        dati = self._client()._form_richiedi_nuovo()
        assert dati["thePage:j_id2:i:f:pb:d:element___hidden____Richiedi_nuovo_OTP"] == "true"
        assert dati["thePage:j_id2:i:f:pb:d:element___input____Richiedi_nuovo_OTP"] == "true"
        assert not [k for k in dati if "OTP_Input" in k]


# ---------------------------------------------------------------------------
# Invio / reinvio del codice: conferma e limite di sessioni
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
    """Minimo indispensabile per _async_post_form_otp: registra i dati
    inviati e restituisce sempre la stessa pagina."""

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
        """Se la pagina non contiene nessuno dei messaggi di conferma noti il
        flusso continua (la formulazione potrebbe essere solo diversa), ma
        resta traccia nei log e il flag permette al config flow di
        avvisare."""
        # Il dump di debug viene scritto nella cwd: lo dirottiamo su tmp_path
        # per non lasciare otp_send_debug.html dentro il repository.
        monkeypatch.chdir(tmp_path)
        client, _ = _client_su_form_otp(TestAnalizzaPaginaOtp.HTML_PAGINA_OTP)
        await client._async_avvia_invio_otp()
        assert client.otp_invio_confermato is False
        assert "non ha confermato l'invio" in caplog.text
        # Il dump e' scritto in un executor (per non bloccare l'event loop
        # di Home Assistant), quindi puo' non esistere ancora al ritorno.
        dump = tmp_path / "otp_send_debug.html"
        for _ in range(50):
            if dump.exists():
                break
            await asyncio.sleep(0.02)
        assert dump.exists()

    async def test_pagina_limite_sessioni_solleva_eccezione_dedicata(self, monkeypatch, tmp_path):
        """Con troppe sessioni aperte nessun codice viene spedito: va
        distinto da un OTP sbagliato o da un cambio di markup, altrimenti
        l'utente resta ad aspettare un codice che non arriverà mai."""
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
# Schermata di consenso OAuth
# ---------------------------------------------------------------------------


# Struttura ricalcata sulla pagina reale: titolo "Consentire l'accesso?", un
# solo form con 8 campi hidden e DUE pulsanti submit con lo stesso
# name="save", distinguibili solo per il valore - "Consenti" approva, " Nega "
# (con gli spazi) rifiuta.
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
        """I due pulsanti hanno lo stesso name: prendere quello sbagliato
        significherebbe NEGARE l'autorizzazione all'integrazione."""
        action, dati = auth.AuthClient._estrai_form_consenso(HTML_PAGINA_CONSENSO)
        assert dati["save"] == "Consenti"
        assert action == (
            "https://edistribuzione.my.site.com/PortaleClienti/_ui/identity/oauth/ui/AuthorizationPage"
        )

    def test_rimanda_tutti_i_campi_hidden_deescapati(self):
        """save_new_url e source sono URL pieni di parametri: se restano
        escapati (&amp;) il server riceve valori diversi da quelli che ha
        generato."""
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
        """Su una pagina che non è una schermata di consenso il chiamante
        deve poter proseguire col suo errore di parsing abituale."""
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
    """Restituisce le risposte in coda, una per chiamata (get o post), e
    registra i payload inviati."""

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
        """Dopo l'OTP arriva la schermata di consenso invece del codice;
        premendo "Consenti" il codice arriva nel Location del redirect verso
        lo schema custom dell'app (eneldist://), che aiohttp non può
        seguire."""
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
# _verifica_non_troppe_sessioni (centralizzata: era duplicata in tre punti)
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
# _url_via_host_diretto: gli URL che il server genera puntando al dominio
# custom dietro Imperva vengono riscritti sul dominio *.my.site.com
# ---------------------------------------------------------------------------


class TestUrlViaHostDiretto:
    def test_riscrive_il_dominio_protetto(self):
        """Seguire un URL su private.e-distribuzione.it riporterebbe dentro
        la verifica antibot "Pardon Our Interruption"."""
        assert auth._url_via_host_diretto(
            "https://private.e-distribuzione.it/PortaleClienti/secur/frontdoor.jsp?retURL=x"
        ) == "https://edistribuzione.my.site.com/PortaleClienti/secur/frontdoor.jsp?retURL=x"

    def test_lascia_invariato_l_host_diretto(self):
        url = "https://edistribuzione.my.site.com/PortaleClienti/loginflow/loginFlow.apexp"
        assert auth._url_via_host_diretto(url) == url

    def test_lascia_invariati_altri_host_e_relativi(self):
        """Solo l'esatto dominio protetto va riscritto: un hostname diverso
        (anche dello stesso sito) passa com'è, e un path relativo resta
        relativo per il join del chiamante."""
        assert auth._url_via_host_diretto("https://example.com/x") == "https://example.com/x"
        assert (
            auth._url_via_host_diretto("/PortaleClienti/loginflow/loginFlow.apexp")
            == "/PortaleClienti/loginflow/loginFlow.apexp"
        )


# ---------------------------------------------------------------------------
# Gerarchia eccezioni
# ---------------------------------------------------------------------------


class TestGerarchiaEccezioni:
    def test_invalid_credentials_e_sottoclasse_di_auth_error(self):
        assert issubclass(auth.InvalidCredentials, auth.AuthError)

    def test_invalid_otp_e_sottoclasse_di_auth_error(self):
        assert issubclass(auth.InvalidOtp, auth.AuthError)

    def test_parsing_error_e_sottoclasse_di_auth_error(self):
        assert issubclass(auth.ParsingError, auth.AuthError)

    def test_troppe_sessioni_e_sottoclasse_di_auth_error_ma_non_di_credenziali(self):
        """Deve essere gestibile a parte: le credenziali sono corrette, è
        l'account ad avere troppe sessioni aperte."""
        assert issubclass(auth.TroppeSessioni, auth.AuthError)
        assert not issubclass(auth.TroppeSessioni, auth.InvalidCredentials)


# ---------------------------------------------------------------------------
# Verifica antibot di Imperva al posto delle pagine attese
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
        """Imperva inserisce _Incapsula_Resource anche nelle pagine normali
        di un sito protetto: da solo non deve bloccare un login valido."""
        auth._verifica_non_bloccato(
            '<html><title>Login</title><script src="/_Incapsula_Resource?x=1"></script></html>'
        )

    def test_e_un_auth_error(self):
        assert issubclass(auth.AccessoBloccato, auth.AuthError)

    async def test_refresh_del_token_bloccato(self):
        session = _SessioneACoda([_RispostaToken(PAGINA_ANTIBOT)])
        with pytest.raises(auth.AccessoBloccato):
            await auth.AuthClient(session).async_refresh_access_token("rt")

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
