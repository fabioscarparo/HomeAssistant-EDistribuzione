"""Client for the E-Distribuzione data API (xs-misura-p.de-c1.eu1.cloudhub.io).

Unlike auth.py, this is a plain REST/JSON API behind a Bearer token. The only
quirk is the `Method_User` header, which the backend uses to pick permissions
per endpoint.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import aiohttp

from .const import (
    MAGNITUDE_PRELEVATA,
    METHOD_USER_CURVA_GIORNO,
    METHOD_USER_CURVA_MESE,
    METHOD_USER_CURVA_PERIODO,
    METHOD_USER_ELENCO_POD,
    METHOD_USER_LETTURE,
    MISURE_DAILY_LOAD_PROFILE_URL,
    MISURE_GET_SUPPLIES_URL,
    MISURE_MONTHLY_LOAD_PROFILE_URL,
    MISURE_MONTHLY_TIME_OF_USE_URL,
    MISURE_READING_URL,
)

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """Raised when the API returns a non-OK meta.status or a transport error."""


class ApiClient:
    """Thin wrapper over the 'misure' endpoints. Auth and refresh are handled
    upstream by the coordinator, which passes a valid access_token here."""

    def __init__(self, session: aiohttp.ClientSession, access_token: str) -> None:
        self._session = session
        self._access_token = access_token

    def update_token(self, access_token: str) -> None:
        self._access_token = access_token

    def _headers(self, method_user: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Method_User": method_user,
            "Accept": "*/*",
            # A generic User-Agent is fine: the backend keys permissions on the
            # Method_User header, not on the UA string.
            "User-Agent": "HomeAssistant-edistribuzione",
            "client_id": "",
            "client_secret": "",
        }

    async def _get_json(self, url: str, method_user: str, params: dict) -> dict:
        async with self._session.get(
            url, headers=self._headers(method_user), params=params
        ) as resp:
            if resp.status == 401:
                raise ApiError("401 Unauthorized - access_token scaduto")
            if resp.status == 404:
                # The backend returns 404 for a day whose data is not published
                # yet, instead of a 200 with an empty data:[] as for other
                # empty requests. Treated as "no data available yet" (same
                # meaning as empty data:[]), not as a fatal error.
                body_preview = await resp.text()
                _LOGGER.debug(
                    "404 su %s (probabile 'nessun dato ancora disponibile', non "
                    "un errore). Corpo (primi 300 caratteri): %r",
                    url,
                    body_preview[:300],
                )
                return {"data": []}
            if resp.status >= 400:
                body_preview = await resp.text()
                _LOGGER.error(
                    "%s ha risposto %s. Corpo (primi 500 caratteri): %r",
                    url,
                    resp.status,
                    body_preview[:500],
                )
            resp.raise_for_status()
            payload = await resp.json(content_type=None)

        meta = payload.get("meta", {})
        if meta.get("status") not in ("OK", None):
            raise ApiError(f"API returned meta.status={meta.get('status')}: {meta}")
        return payload

    async def async_get_supplies(self) -> list[dict[str, Any]]:
        """List of PODs (delivery points) tied to the account."""
        # json={} instead of manual headers with no body: the backend is
        # MuleSoft (cloudhub.io), and with a declared Content-Type but no body
        # the server-side parser fails silently with a generic 500 instead of
        # a precise validation error.
        async with self._session.post(
            MISURE_GET_SUPPLIES_URL,
            headers=self._headers(METHOD_USER_ELENCO_POD),
            json={},
        ) as resp:
            if resp.status >= 400:
                body_preview = await resp.text()
                _LOGGER.error(
                    "getSupplies ha risposto %s. Corpo (primi 500 caratteri): %r",
                    resp.status,
                    body_preview[:500],
                )
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        try:
            return payload["data"][0]["pods"]
        except (KeyError, IndexError):
            return []

    async def async_get_reading(self, pod: str, date_from: date, date_to: date) -> list[dict[str, Any]]:
        """Official cumulative monthly readings (EA/ER per band + POT peaks)."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
        }
        payload = await self._get_json(MISURE_READING_URL, METHOD_USER_LETTURE, params)
        return payload.get("data", [])

    async def async_get_daily_load_profile(
        self,
        pod: str,
        date_from: date,
        date_to: date | None = None,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Hourly/quarter-hourly load curve.

        If date_to is None, requests a single day (date_from == date_to in the
        request). The endpoint accepts rangeDateFrom/rangeDateTo as a real
        range, up to 181 days in a single response.

        'magnitude' selects the energy direction (see MAGNITUDE_* in const.py):
        the caller decides which one(s) to request, this function assumes
        nothing beyond the consumption default.
        """
        if date_to is None:
            date_to = date_from
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_DAILY_LOAD_PROFILE_URL, METHOD_USER_CURVA_GIORNO, params
        )
        return payload.get("data", [])

    async def async_get_monthly_load_profile(
        self,
        date_from: date,
        date_to: date,
        pod: str,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Total consumption per day over a range."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_MONTHLY_LOAD_PROFILE_URL, METHOD_USER_CURVA_MESE, params
        )
        return payload.get("data", [])

    async def async_get_monthly_time_of_use(
        self,
        pod: str,
        date_from: date,
        date_to: date,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Monthly total per band (T1-T4) + power peaks."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_MONTHLY_TIME_OF_USE_URL, METHOD_USER_CURVA_PERIODO, params
        )
        return payload.get("data", [])
