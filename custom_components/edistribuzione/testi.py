"""Visible texts that Home Assistant cannot translate via translations/*.json.

Entity names, config flow, actions and exceptions go through the translation
files and follow each user's profile language. A few strings stay out, which HA
treats as plain data with no translation key:

- the device model (DeviceInfo translates only the name);
- the external statistics name, stored in the Recorder metadata;
- the config flow description_placeholders, substituted as-is.

For these the server language is used (hass.config.language): Italian if it is
a variant of "it", otherwise English, like HA's fallback.

Pure module, no Home Assistant dependencies.
"""
from __future__ import annotations

LINGUA_PREDEFINITA = "en"

TESTI: dict[str, dict[str, str]] = {
    "it": {
        "modello_account": "Account API",
        "modello_prelievo": "Punto di prelievo",
        "modello_produzione": "Punto di produzione",
        "serie_prelievo": "prelievo",
        "serie_immissione": "immissione",
        "serie_produzione": "produzione",
        "serie_prelievo_tecnico": "prelievo (tecnico)",
        "otp_reinviato": (
            "Ho chiesto a E-Distribuzione un nuovo codice: controlla email e SMS. "
            "Usa l'ultimo arrivato."
        ),
        "otp_invio_non_confermato": (
            "Attenzione: E-Distribuzione non ha confermato l'invio del codice. Se non "
            "ti arriva nulla, chiudi le altre sessioni aperte (esci dall'app "
            "ufficiale e dal sito), poi spunta \"Richiedi un nuovo codice\" qui sotto "
            "e invia il form senza inserire nessun codice."
        ),
        "otp_solo_da_qui": (
            "Il codice deve essere quello inviato da questa configurazione: un OTP "
            "generato sul sito o nell'app appartiene a un'altra sessione di login e "
            "verrebbe rifiutato."
        ),
        "nota_reauth": " Stai aggiornando le credenziali per i POD: {pods}.",
        "nessun_pod": "nessuno",
        "nessun_dato_pod": "nessun dato per il periodo richiesto",
    },
    "en": {
        "modello_account": "API account",
        "modello_prelievo": "Withdrawal point",
        "modello_produzione": "Production point",
        "serie_prelievo": "consumption",
        "serie_immissione": "return to grid",
        "serie_produzione": "production",
        "serie_prelievo_tecnico": "consumption (technical)",
        "otp_reinviato": (
            "I asked E-Distribuzione for a new code: check your email and SMS. "
            "Use the most recent one."
        ),
        "otp_invio_non_confermato": (
            "Warning: E-Distribuzione did not confirm that the code was sent. If "
            "nothing arrives, close any other open session (log out of the official "
            "app and website), then tick \"Request a new code\" below and submit the "
            "form without entering a code."
        ),
        "otp_solo_da_qui": (
            "The code must be the one sent by this setup: an OTP generated on the "
            "website or in the app belongs to another login session and would be "
            "rejected."
        ),
        "nota_reauth": " You are updating the credentials for these PODs: {pods}.",
        "nessun_pod": "none",
        "nessun_dato_pod": "no data for the requested period",
    },
}


def lingua_supportata(lingua: str | None) -> str:
    """'it', 'it-IT' -> 'it'; everything else (None included) -> English."""
    base = (lingua or "").split("-")[0].split("_")[0].lower()
    return base if base in TESTI else LINGUA_PREDEFINITA


def testo(lingua: str | None, chiave: str, **segnaposto: str) -> str:
    """Text for 'chiave' in the given language, with placeholders substituted."""
    valore = TESTI[lingua_supportata(lingua)][chiave]
    return valore.format(**segnaposto) if segnaposto else valore
