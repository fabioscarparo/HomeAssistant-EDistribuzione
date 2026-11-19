"""Pure parsers for E-Distribuzione supply, monthly, reactive and reading payloads.

Strips all personal identifiers (tax code, address, meter serial, file names)
immediately so only technical energy/power data enters coordinator state,
entities and diagnostics. No Home Assistant dependencies.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from .const import (
    MAGNITUDE_REATTIVA_R1,
    MAGNITUDE_REATTIVA_R2,
    MAGNITUDE_REATTIVA_R3,
    MAGNITUDE_REATTIVA_R4,
    TIPO_POD_PRODUZIONE,
    TIPO_POD_SCAMBIO,
)

_SLOT_A_FASCIA = {"T1": "f1", "T2": "f2", "T3": "f3"}


def _to_float(valore: Any) -> float | None:
    if valore is None or valore == "":
        return None
    try:
        num = float(valore)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _to_str(valore: Any) -> str | None:
    if valore is None:
        return None
    testo = str(valore).strip()
    return testo or None


def estrai_fornitura(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract only non-PII technical metadata from a getSupplies POD record."""
    tipo_punto = _to_str(raw.get("MeteringPointType"))
    has_plant = raw.get("HasPlant")
    return {
        "potenza_contrattuale": _to_float(raw.get("ContractualPower")),
        "potenza_disponibile": _to_float(raw.get("AvailablePower")),
        "potenza_prelevabile": _to_float(raw.get("DeductiblePower")),
        "tensione": _to_str(raw.get("VoltageLevel")),
        "stato_fornitura": _to_str(raw.get("SupplyStatusId")),
        "tipo_punto": tipo_punto.upper() if tipo_punto else None,
        "ha_impianto": has_plant if isinstance(has_plant, bool) else None,
        "codice_regime_impianto": _to_str(raw.get("PlantCommercialRegimeId")),
        "regime_impianto": _to_str(raw.get("IncentivizedPlant")),
    }


def ruolo_suggerito_da_fornitura(raw: dict[str, Any]) -> str:
    """Suggest initial POD role from MeteringPointType (M2 -> production, else exchange)."""
    tipo_punto = _to_str(raw.get("MeteringPointType"))
    if tipo_punto and tipo_punto.upper() == "M2":
        return TIPO_POD_PRODUZIONE
    return TIPO_POD_SCAMBIO


def _parse_riga_mensile(riga: dict[str, Any], *, magnitude_attesa: str | None) -> tuple[str, dict[str, Any]] | None:
    ym = _to_str(riga.get("yearMonth"))
    if not ym or len(ym) != 6 or not ym.isdigit():
        return None
    letture = riga.get("readings")
    if not isinstance(letture, dict):
        return None
    energy_type = _to_str(letture.get("energyType"))
    if magnitude_attesa and energy_type and energy_type != magnitude_attesa:
        return None

    f1 = _to_float(letture.get("energyT1"))
    f2 = _to_float(letture.get("energyT2"))
    f3 = _to_float(letture.get("energyT3"))
    totale = _to_float(letture.get("energyT4"))
    if totale is None:
        presenti = [v for v in (f1, f2, f3) if v is not None]
        totale = round(sum(presenti), 3) if presenti else None
    else:
        totale = round(totale, 3)

    p1 = _to_float(letture.get("peakPowerT1"))
    p2 = _to_float(letture.get("peakPowerT2"))
    p3 = _to_float(letture.get("peakPowerT3"))
    p_max = _to_float(letture.get("peakPowerT4"))
    if p_max is None:
        picchi = [v for v in (p1, p2, p3) if v is not None]
        p_max = round(max(picchi), 3) if picchi else None
    else:
        p_max = round(p_max, 3)

    if totale is None and p_max is None:
        return None

    return ym, {
        "mese": f"{ym[:4]}-{ym[4:6]}",
        "totale": totale,
        "f1": round(f1, 3) if f1 is not None else None,
        "f2": round(f2, 3) if f2 is not None else None,
        "f3": round(f3, 3) if f3 is not None else None,
        "picco_kw": p_max,
        "picco_f1_kw": round(p1, 3) if p1 is not None else None,
        "picco_f2_kw": round(p2, 3) if p2 is not None else None,
        "picco_f3_kw": round(p3, 3) if p3 is not None else None,
    }


def estrai_mensili(
    righe: list[dict[str, Any]],
    *,
    oggi: date,
    magnitude_attesa: str | None = None,
) -> dict[str, dict[str, Any] | None]:
    """Extract current and previous calendar month records from querymonthlytimeofuse."""
    corrente_ym = f"{oggi.year:04d}{oggi.month:02d}"
    fine_prec = oggi.replace(day=1) - timedelta(days=1)
    precedente_ym = f"{fine_prec.year:04d}{fine_prec.month:02d}"

    per_mese: dict[str, dict[str, Any]] = {}
    for riga in righe:
        if not isinstance(riga, dict):
            continue
        parsed = _parse_riga_mensile(riga, magnitude_attesa=magnitude_attesa)
        if parsed is not None:
            ym, dato = parsed
            per_mese[ym] = dato

    return {
        "mese_corrente": per_mese.get(corrente_ym),
        "mese_precedente": per_mese.get(precedente_ym),
    }


def calcola_reattiva_e_cos_phi(
    attiva_a1: dict[str, Any] | None,
    per_quadrante: dict[str, dict[str, Any] | None],
) -> dict[str, Any] | None:
    """Compute monthly reactive totals (kvarh) and withdrawal power factor (cos phi)."""
    valori: dict[str, float | None] = {}
    mese: str | None = None
    for q in (
        MAGNITUDE_REATTIVA_R1,
        MAGNITUDE_REATTIVA_R2,
        MAGNITUDE_REATTIVA_R3,
        MAGNITUDE_REATTIVA_R4,
    ):
        blocco = per_quadrante.get(q)
        if isinstance(blocco, dict):
            valori[q] = _to_float(blocco.get("totale"))
            if mese is None and blocco.get("mese"):
                mese = str(blocco["mese"])
        else:
            valori[q] = None

    if all(v is None for v in valori.values()):
        return None

    r1 = valori[MAGNITUDE_REATTIVA_R1]
    r2 = valori[MAGNITUDE_REATTIVA_R2]
    r3 = valori[MAGNITUDE_REATTIVA_R3]
    r4 = valori[MAGNITUDE_REATTIVA_R4]
    reattiva_prelevata = round((r1 or 0.0) + (r4 or 0.0), 3)
    reattiva_immessa = round((r2 or 0.0) + (r3 or 0.0), 3)

    attiva_val = _to_float((attiva_a1 or {}).get("totale"))
    cos_phi: float | None = None
    tan_phi: float | None = None
    if attiva_val is not None and attiva_val > 0:
        apparente = math.hypot(attiva_val, reattiva_prelevata)
        if apparente > 0:
            cos_phi = round(attiva_val / apparente, 3)
            tan_phi = round(reattiva_prelevata / attiva_val, 3)

    if mese is None and isinstance(attiva_a1, dict) and attiva_a1.get("mese"):
        mese = str(attiva_a1["mese"])

    return {
        "mese": mese,
        "reattiva_prelevata": reattiva_prelevata,
        "reattiva_immessa": reattiva_immessa,
        "r1": round(r1, 3) if r1 is not None else None,
        "r2": round(r2, 3) if r2 is not None else None,
        "r3": round(r3, 3) if r3 is not None else None,
        "r4": round(r4, 3) if r4 is not None else None,
        "cos_phi": cos_phi,
        "tan_phi": tan_phi,
    }


def estrai_ultima_lettura(righe: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract the most recent official billing reading without PII fields."""
    migliore_ts = ""
    migliore: dict[str, Any] | None = None
    for riga in righe:
        if not isinstance(riga, dict):
            continue
        ts = _to_str(riga.get("measureTs"))
        if not ts:
            continue
        if ts >= migliore_ts:
            migliore_ts = ts
            migliore = riga

    if migliore is None:
        return None

    data_lettura = migliore_ts[:10]
    try:
        date.fromisoformat(data_lettura)
    except ValueError:
        return None

    pub = migliore.get("publishingInfo") if isinstance(migliore.get("publishingInfo"), dict) else {}

    ea: dict[str, float | None] = {"f1": None, "f2": None, "f3": None}
    er: dict[str, float | None] = {"f1": None, "f2": None, "f3": None}
    for slot in migliore.get("publishedSlots") or []:
        if not isinstance(slot, dict):
            continue
        fascia = _SLOT_A_FASCIA.get(str(slot.get("slotId") or "").upper())
        val = _to_float(slot.get("value"))
        if fascia is None or val is None:
            continue
        mag = str(slot.get("magnitude") or "").upper()
        if mag == "EA":
            ea[fascia] = round(val, 3)
        elif mag == "ER":
            er[fascia] = round(val, 3)

    ea_presenti = [v for v in ea.values() if v is not None]
    er_presenti = [v for v in er.values() if v is not None]
    ea["totale"] = round(sum(ea_presenti), 3) if ea_presenti else None
    er["totale"] = round(sum(er_presenti), 3) if er_presenti else None

    pot: dict[str, float | None] = {"f1": None, "f2": None, "f3": None}
    for slot in migliore.get("publishedPowerPeaks") or []:
        if not isinstance(slot, dict):
            continue
        fascia = _SLOT_A_FASCIA.get(str(slot.get("slotId") or "").upper())
        val = _to_float(slot.get("value"))
        if fascia is None or val is None:
            continue
        if str(slot.get("magnitude") or "").upper() == "POT":
            pot[fascia] = round(val, 3)
    pot_presenti = [v for v in pot.values() if v is not None]
    pot["massima"] = round(max(pot_presenti), 3) if pot_presenti else None

    return {
        "data_lettura": data_lettura,
        "pubblicata_il": _to_str(pub.get("publishingTs")),
        "tipo_flusso": _to_str(pub.get("flowType")),
        "tipo_misura": _to_str(pub.get("measType")),
        "ea": ea,
        "er": er,
        "pot": pot,
    }


def confronta_fasce_mese(
    locale: dict[str, Any] | None,
    ufficiale: dict[str, Any] | None,
    *,
    tolleranza_kwh: float = 0.05,
) -> dict[str, Any]:
    """Compare locally classified monthly ARERA bands against official API totals."""
    if not locale or not ufficiale:
        return {"esito": "non_disponibile"}

    delta: dict[str, float | None] = {}
    scostamento = False
    for chiave in ("f1", "f2", "f3", "totale"):
        loc = _to_float(locale.get(chiave))
        uff = _to_float(ufficiale.get(chiave))
        if loc is None or uff is None:
            delta[chiave] = None
            continue
        diff = round(loc - uff, 3)
        delta[chiave] = diff
        if abs(diff) > tolleranza_kwh:
            scostamento = True

    return {
        "esito": "scostamento" if scostamento else "ok",
        "mese": ufficiale.get("mese"),
        "locale": {k: locale.get(k) for k in ("f1", "f2", "f3", "totale", "picco_kw", "campioni")},
        "ufficiale": {k: ufficiale.get(k) for k in ("f1", "f2", "f3", "totale", "picco_kw")},
        "delta": delta,
    }
