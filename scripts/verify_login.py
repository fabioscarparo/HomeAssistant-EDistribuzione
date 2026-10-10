"""Test the real login/API code (auth.py + api.py) from the terminal, without
Home Assistant in the middle - much faster than going through the UI on every
attempt.

Covers the whole chain: email/password -> OTP -> POD retrieval
(async_get_supplies), the same path as the real config flow.

It also PROBES the 'magnitude' vocabulary of querydailyloadprofile: the
production code requests "A1" (consumption, confirmed) and "A2" (injection, NOT
confirmed - see const.py). This script tries several candidates and compares the
results, to establish which value actually returns injected energy before
trusting it in production.

After a successful login it saves the refresh_token to refresh_token.txt (local
file, excluded from git): on the next run the script offers to test ONLY the
refresh (async_refresh_access_token), without redoing email/password/OTP.

It imports auth.py/api.py/const.py DIRECTLY, bypassing __init__.py (which
imports homeassistant, not installed here and not needed: none of the three
modules depend on Home Assistant, only on aiohttp and the standard library).

Usage:
    pip install aiohttp
    python3 scripts/verify_login.py

The password is read with getpass (it does not appear on screen and does not
stay in the terminal history).
"""
from __future__ import annotations

import asyncio
import getpass
import importlib.util
import json
import logging
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "edistribuzione"


def _load_modules():
    """Load const.py, auth.py and api.py as an isolated mini-package, without
    running __init__.py (which requires homeassistant). Returns
    (const, auth, api)."""
    if not PKG_DIR.exists():
        sys.exit(
            f"Non trovo {PKG_DIR} - lancia questo script dalla cartella "
            "'scripts/' dentro il repository, con la struttura "
            "custom_components/edistribuzione/... accanto."
        )

    pkg_name = "edistribuzione_standalone"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = [str(PKG_DIR)]
    sys.modules[pkg_name] = pkg

    def _load(modname: str, filename: str):
        spec = importlib.util.spec_from_file_location(f"{pkg_name}.{modname}", PKG_DIR / filename)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"{pkg_name}.{modname}"] = mod
        spec.loader.exec_module(mod)
        return mod

    const = _load("const", "const.py")
    auth = _load("auth", "auth.py")
    api = _load("api", "api.py")
    return const, auth, api


REFRESH_TOKEN_FILE = Path("refresh_token.txt")

# Candidates for the 'magnitude' value that returns injected energy on MuleSoft
# (querydailyloadprofile). "A1" is the reference (consumption, confirmed): every
# other candidate is compared against it to find out whether the server honours
# it or silently ignores it (in which case it returns the exact same total as
# "A1" - the worst case, because without this comparison it would look like a
# success). The web portal's "Magnitude: A+/A-" is deliberately not included:
# that vocabulary belongs to a different backend (Aura, not MuleSoft) and trying
# it here would say nothing about this API.
MAGNITUDE_RIFERIMENTO = "A1"
MAGNITUDE_CANDIDATE = [MAGNITUDE_RIFERIMENTO, "A2", "A-", "A+", "A3"]

FUSO_ITALIA = ZoneInfo("Europe/Rome")

# Local hours considered "night" for the photovoltaic shape check: if a curve is
# really PV production, it should be practically zero here.
ORE_NOTTURNE = set(range(0, 5)) | set(range(21, 24))


def _analizza_curva(curva: list[dict]) -> dict:
    """Summarise a querydailyloadprofile response for the magnitude comparison:
    total kWh, days received, energyType/timeType returned and the night/day
    split in Italian local time.

    energyType is the decisive field: it echoes back the magnitude the server
    SERVED, not the one requested. If it does not match the one requested, the
    request was ignored.
    """
    totale = 0.0
    notte = 0.0
    giorno_solare = 0.0
    energy_types: set[str] = set()
    time_types: set[str] = set()
    date_ricevute: set[str] = set()

    for elemento in curva:
        readings = elemento.get("readings", {})
        campioni = readings.get("sampleValues") or []
        if not campioni:
            continue

        if readings.get("energyType") is not None:
            energy_types.add(str(readings["energyType"]))
        if elemento.get("timeType") is not None:
            time_types.add(str(elemento["timeType"]))
        if readings.get("sampleDate"):
            date_ricevute.add(str(readings["sampleDate"]))

        frequenza = elemento.get("sampleFrequency")
        try:
            inizio = datetime.fromisoformat(elemento["initialSample"])
        except (KeyError, TypeError, ValueError):
            inizio = None

        for campione in campioni:
            try:
                valore = float(campione["val"])
                indice = int(campione["id"])
            except (KeyError, TypeError, ValueError):
                continue
            totale += valore
            if inizio is None or not frequenza:
                continue
            ora_locale = (
                (inizio + timedelta(minutes=(indice - 1) * frequenza)).astimezone(FUSO_ITALIA).hour
            )
            if ora_locale in ORE_NOTTURNE:
                notte += valore
            else:
                giorno_solare += valore

    return {
        "totale": totale,
        "notte": notte,
        "giorno": giorno_solare,
        "energy_types": energy_types,
        "time_types": time_types,
        "giorni": len(date_ricevute),
    }


async def _sonda_magnitude(api_client, pod: str, giorno_da: date, giorno_a: date) -> dict:
    """Request the curve once per candidate and print a comparison. Returns
    {magnitude: curva} for the candidates that returned samples."""
    curve: dict[str, list[dict]] = {}
    analisi: dict[str, dict] = {}

    for magnitude in MAGNITUDE_CANDIDATE:
        print(f"\n--- magnitude={magnitude!r} su {pod}: {giorno_da} - {giorno_a} ---")
        try:
            curva = await api_client.async_get_daily_load_profile(
                pod, giorno_da, giorno_a, magnitude=magnitude
            )
        except Exception as exc:  # noqa: BLE001 - a rejected magnitude must not stop the probe
            print(f"  RIFIUTATA: {type(exc).__name__}: {exc}")
            continue

        info = _analizza_curva(curva)
        if not info["giorni"]:
            print("  Risposta senza campioni (nessun dato per questa magnitude).")
            continue

        curve[magnitude] = curva
        analisi[magnitude] = info
        print(
            f"  energyType restituito: {sorted(info['energy_types']) or '(assente)'}   "
            f"timeType: {sorted(info['time_types']) or '(assente)'}"
        )
        print(f"  giorni con dati: {info['giorni']}   totale: {info['totale']:.3f} kWh")
        print(
            f"  ripartizione oraria locale -> notte: {info['notte']:.3f} kWh   "
            f"ore diurne: {info['giorno']:.3f} kWh"
        )
        if info["energy_types"] and info["energy_types"] != {magnitude}:
            print(
                f"  !! energyType ({sorted(info['energy_types'])}) DIVERSO dalla magnitude "
                f"richiesta ({magnitude!r}): il server non ha onorato la richiesta."
            )

    if not analisi:
        print("\n=== ESITO SONDA: nessun candidato ha restituito dati. ===")
        return curve

    print("\n=== ESITO SONDA ===")
    riferimento = analisi.get(MAGNITUDE_RIFERIMENTO)
    for magnitude, info in analisi.items():
        etichetta = f"  {magnitude:4}  {info['totale']:12.3f} kWh"
        if magnitude == MAGNITUDE_RIFERIMENTO:
            print(f"{etichetta}   <- riferimento (prelevata, quella usata oggi)")
            continue
        if riferimento and abs(info["totale"] - riferimento["totale"]) < 0.001:
            print(f"{etichetta}   IDENTICA al riferimento: parametro IGNORATO, non usare")
        else:
            forma = (
                "notte ~0, compatibile con produzione FV"
                if info["totale"] > 0 and info["notte"] < info["totale"] * 0.02
                else "notte non trascurabile, NON e' una curva di sola produzione"
            )
            print(f"{etichetta}   curva DIVERSA dal riferimento ({forma})")

    if riferimento is None:
        print(
            f"\nNOTA: {MAGNITUDE_RIFERIMENTO} non ha restituito dati per questo POD e "
            "periodo, quindi non c'e' un riferimento con cui confrontare gli altri "
            "candidati: i 'diversa dal riferimento' qui sopra non sono conclusivi."
        )

    debug_path = Path("magnitude_probe_debug.json")
    debug_path.write_text(
        json.dumps(
            {
                m: analisi[m]
                | {
                    "energy_types": sorted(analisi[m]["energy_types"]),
                    "time_types": sorted(analisi[m]["time_types"]),
                }
                for m in analisi
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nRiepilogo della sonda salvato in {debug_path.resolve()}")
    print(
        "\nSe un candidato risulta corretto, aggiorna SOLO MAGNITUDE_IMMESSA in "
        "custom_components/edistribuzione/const.py con quel valore."
    )
    return curve


async def _test_refresh_soltanto(auth) -> None:
    """Test async_refresh_access_token with the token saved from a previous
    login, without redoing email/password/OTP."""
    refresh_token_salvato = REFRESH_TOKEN_FILE.read_text(encoding="utf-8").strip()

    import aiohttp

    async with aiohttp.ClientSession() as session:
        client = auth.AuthClient(session)
        print(f"\n--- Test refresh con il token salvato in {REFRESH_TOKEN_FILE} ---")
        try:
            tokens = await client.async_refresh_access_token(refresh_token_salvato)
        except auth.AuthError as exc:
            print(f"\nREFRESH FALLITO: {exc}")
            print(
                "Se il messaggio parla di token non valido/scaduto/revocato, il "
                "refresh_token non è più utilizzabile: serve rifare login+OTP da capo "
                "(rilancia lo script e rispondi 'n' alla prossima domanda)."
            )
            return
        except Exception:
            print("Errore IMPREVISTO durante il refresh (traceback completo sotto):\n")
            raise

        print("\n=== REFRESH RIUSCITO ===")
        print(f"access_token  (primi 20 char): {tokens.access_token[:20]}...")
        if tokens.refresh_token == refresh_token_salvato:
            print("refresh_token: invariato rispetto a quello salvato")
        else:
            print(
                f"refresh_token: CAMBIATO (primi 20 char nuovo: {tokens.refresh_token[:20]}...) "
                "- salvo il nuovo al posto del vecchio"
            )
            REFRESH_TOKEN_FILE.write_text(tokens.refresh_token, encoding="utf-8")


async def main() -> None:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    const, auth, api = _load_modules()

    try:
        import aiohttp
    except ImportError:
        sys.exit("Manca aiohttp: pip install aiohttp")

    if REFRESH_TOKEN_FILE.exists():
        scelta = input(
            f"\nTrovato un refresh_token salvato in {REFRESH_TOKEN_FILE} (da un login "
            "precedente). Vuoi testare SOLO quello (niente email/password/OTP)? [S/n]: "
        ).strip().lower()
        if scelta in ("", "s", "si", "sì", "y", "yes"):
            await _test_refresh_soltanto(auth)
            return
        print("Ok, procedo con login completo (email/password/OTP).\n")

    email = input("Email E-Distribuzione: ").strip()
    password = getpass.getpass("Password (non visibile mentre digiti): ")

    async with aiohttp.ClientSession() as session:
        client = auth.AuthClient(session)

        print("\n--- Invio email/password ---")
        try:
            await client.async_begin_login(email, password)
        except auth.InvalidCredentials as exc:
            print(f"Credenziali rifiutate: {exc}")
            return
        except auth.TroppeSessioni as exc:
            print(
                "E-Distribuzione ha rifiutato l'accesso: l'account ha troppe sessioni "
                f"aperte contemporaneamente ({exc}).\nEsci dall'app ufficiale e dal sito, "
                "attendi qualche minuto e riprova: in questo stato nessun OTP viene inviato."
            )
            return
        except auth.ParsingError as exc:
            print(f"Errore di parsing (pagina di login cambiata?): {exc}")
            return
        except Exception:
            print("Errore IMPREVISTO (traceback completo sotto):\n")
            raise

        print("OK: email/password accettate.")

        print("\n--- Invio codice OTP ---")
        if client.otp_invio_confermato is False:
            print(
                "ATTENZIONE: E-Distribuzione non ha confermato l'invio del codice (vedi "
                "otp_send_debug.html nella cartella corrente). Se non arriva nulla, "
                "rispondi 'r' alla domanda qui sotto per farne rispedire uno."
            )
        print(
            "Il codice deve essere quello inviato da QUESTA sessione: uno generato sul "
            "sito o nell'app appartiene a un altro login e viene sempre rifiutato."
        )
        while True:
            otp = input(
                "Codice OTP ricevuto via email o SMS ('r' per farne rispedire uno nuovo): "
            ).strip()
            if otp.lower() != "r":
                break
            try:
                confermato = await client.async_resend_otp()
            except auth.TroppeSessioni as exc:
                print(f"Reinvio rifiutato, troppe sessioni aperte: {exc}")
                return
            print(
                "Nuovo codice richiesto: controlla email e SMS."
                if confermato
                else "Reinvio richiesto ma NON confermato dal portale (vedi otp_send_debug.html)."
            )
        try:
            tokens = await client.async_submit_otp(otp)
        except auth.InvalidOtp as exc:
            print(f"OTP rifiutato: {exc}")
            return
        except auth.TroppeSessioni as exc:
            print(f"Convalida rifiutata, troppe sessioni aperte: {exc}")
            return
        except auth.ParsingError as exc:
            print(f"Errore di parsing (pagina OTP cambiata?): {exc}")
            return
        except Exception:
            print("Errore IMPREVISTO (traceback completo sotto):\n")
            raise

        print("\n=== LOGIN COMPLETO RIUSCITO ===")
        print(f"access_token  (primi 20 char): {tokens.access_token[:20]}...")
        print(f"refresh_token (primi 20 char): {tokens.refresh_token[:20]}...")

        REFRESH_TOKEN_FILE.write_text(tokens.refresh_token, encoding="utf-8")
        print(
            f"\nrefresh_token salvato in {REFRESH_TOKEN_FILE.resolve()} - la prossima volta "
            "puoi testarlo direttamente senza rifare login+OTP (file locale, già escluso "
            "da git: non va mai committato)."
        )

        print("\n--- Recupero POD (async_get_supplies) ---")
        api_client = api.ApiClient(session, tokens.access_token)
        try:
            pods = await api_client.async_get_supplies()
        except Exception:
            print("Errore IMPREVISTO nel recupero dei POD (traceback completo sotto):\n")
            raise

        print(f"\n=== POD TROVATI: {len(pods)} ===")
        for i, p in enumerate(pods, start=1):
            indirizzo = (
                f"{p.get('PointOfMeasureStreetPrefix', '')} "
                f"{p.get('PointOfMeasureStreet', '')} "
                f"{p.get('PointOfMeasureStreetNumber', '')}, "
                f"{p.get('PointOfMeasureMunicipality', '')} "
                f"({p.get('PointOfMeasureProvince', '')})"
            ).strip()
            # HasPlant is the only getSupplies metadata that might distinguish a
            # POD with a production plant.
            print(f"  [{i}] {p.get('IdPod')} - {indirizzo}   HasPlant={p.get('HasPlant')!r}")

        if not pods:
            print("Nessun POD trovato, mi fermo qui.")
            return

        scelta = input(f"\nQuale POD vuoi interrogare? [1-{len(pods)}, invio per saltare]: ").strip()
        if not scelta:
            return
        try:
            pod_scelto = pods[int(scelta) - 1]["IdPod"]
        except (ValueError, IndexError):
            print("Scelta non valida, mi fermo qui.")
            return

        default_giorno = date.today() - timedelta(days=7)
        giorno_input = input(
            f"Giorno di inizio da interrogare (YYYY-MM-DD) [invio per {default_giorno.isoformat()}]: "
        ).strip()
        try:
            giorno_da = date.fromisoformat(giorno_input) if giorno_input else default_giorno
        except ValueError:
            print(f"Data non valida, uso il default {default_giorno.isoformat()}.")
            giorno_da = default_giorno

        giorno_a_input = input(
            f"Giorno di fine (YYYY-MM-DD) [invio per usare solo {giorno_da.isoformat()}]: "
        ).strip()
        try:
            giorno_a = date.fromisoformat(giorno_a_input) if giorno_a_input else giorno_da
        except ValueError:
            print(f"Data non valida, uso solo {giorno_da.isoformat()}.")
            giorno_a = giorno_da

        print(f"\n--- Sonda delle magnitude per {pod_scelto}: {giorno_da} - {giorno_a} ---")
        print(
            "Candidati provati: "
            + ", ".join(repr(m) for m in MAGNITUDE_CANDIDATE)
            + f" (il primo, {MAGNITUDE_RIFERIMENTO!r}, e' quello usato oggi come prelevata)."
        )
        curve = await _sonda_magnitude(api_client, pod_scelto, giorno_da, giorno_a)

        curva_riferimento = curve.get(MAGNITUDE_RIFERIMENTO, [])
        if not curva_riferimento:
            print(
                f"\nNessun dato per {MAGNITUDE_RIFERIMENTO!r} su questo POD e periodo: "
                "salto il confronto con la lettura ufficiale."
            )
            return

        totale_curva = sum(
            float(c.get("val", 0))
            for g in curva_riferimento
            for c in g.get("readings", {}).get("sampleValues", [])
        )

        conferma = input(
            "\nVuoi confrontare la curva prelevata con la lettura ufficiale per lo stesso "
            "periodo (async_get_reading), per verificare che 'val' sia davvero kWh e non "
            "kW? [S/n]: "
        ).strip().lower()
        if conferma in ("", "s", "si", "sì", "y", "yes"):
            print(f"\n--- Recupero lettura ufficiale per {pod_scelto}: {giorno_da} - {giorno_a} ---")
            try:
                letture = await api_client.async_get_reading(pod_scelto, giorno_da, giorno_a)
            except Exception:
                print("Errore IMPREVISTO nel recupero della lettura (traceback completo sotto):\n")
                raise

            if len(letture) < 2:
                print(
                    "Meno di 2 letture nel periodo: non posso calcolare un delta "
                    "automaticamente (servono almeno una lettura prima e una dopo il periodo)."
                )
                return

            def _somma_ea(lettura: dict) -> float:
                """Sum the 'EA' (active energy) values of all the bands
                (publishedSlots) in a single cumulative reading. The bands not
                active on the contract have "value": null (key present, null
                value) - .get("value", 0) does not catch it, it must be handled
                separately."""
                return sum(
                    float(slot.get("value") or 0)
                    for slot in lettura.get("publishedSlots", [])
                    if slot.get("magnitude") == "EA"
                )

            prima = _somma_ea(letture[0])
            ultima = _somma_ea(letture[-1])
            delta_ufficiale = ultima - prima

            print(f"\nSomma EA (tutte le fasce) prima lettura: {prima:.3f}")
            print(f"Somma EA (tutte le fasce) ultima lettura: {ultima:.3f}")
            print(f"Delta (consumo ufficiale nel periodo): {delta_ufficiale:.3f} kWh")
            print(f"Totale curva prelevata per lo stesso periodo: {totale_curva:.3f} kWh")

            if delta_ufficiale > 0:
                rapporto = totale_curva / delta_ufficiale
                print(f"\nRapporto curva/ufficiale: {rapporto:.3f}")
                if 0.95 <= rapporto <= 1.05:
                    print("=> Combaciano (entro il 5%): 'val = kWh per intervallo' confermato.")
                else:
                    print(
                        "=> NON combaciano: 'val' potrebbe non essere kWh per intervallo. "
                        "Occhio a un rapporto vicino a 0.25 o 4 (confusione kWh/kW su "
                        "intervalli da 15 minuti)."
                    )
            else:
                print(
                    "\nDelta ufficiale a zero o negativo: non riesco a calcolare un rapporto "
                    "sensato. Probabile che il periodo scelto non sia coperto da letture "
                    "ufficiali pubblicate."
                )


if __name__ == "__main__":
    asyncio.run(main())
