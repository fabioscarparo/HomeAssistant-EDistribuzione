"""Client for the E-Distribuzione data API (xs-misura-p.de-c1.eu1.cloudhub.io).

Plain REST/JSON API behind a Bearer token; the Method_User header selects
per-endpoint permissions.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import aiohttp

from .const import (
    HTTP_CONNECT_TIMEOUT_SECONDS,
    HTTP_TIMEOUT_SECONDS,
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
from .redaction import redact_params, redact_preview, redact_url

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(
    total=HTTP_TIMEOUT_SECONDS,
    connect=HTTP_CONNECT_TIMEOUT_SECONDS,
)

# Markers of a gateway/routing 404 page rather than a MuleSoft "no data yet" reply.
_HTML_404_MARKERS = ("<html", "<!doctype", "cannot get ", "not found</title>")


def _is_routing_404(body: str) -> bool:
    lowered = body.lstrip().lower()
    return any(marker in lowered for marker in _HTML_404_MARKERS)


def _extract_data_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data")
    return data if isinstance(data, list) else []


class ApiError(Exception):
    """Raised when the API returns a non-OK meta.status or a transport error.

    'status' is the HTTP status when the server answered with an error, 'rete'
    is True when it could not be reached at all.
    """

    def __init__(self, message: str, *, status: int | None = None, rete: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.rete = rete


class ApiClient:
    """Thin wrapper over the 'misure' endpoints. Auth and refresh live in the coordinator."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        *,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._timeout = timeout

    def update_token(self, access_token: str) -> None:
        self._access_token = access_token

    @property
    def has_token(self) -> bool:
        """True when a non-empty Bearer access token is loaded."""
        return bool(self._access_token)

    def _headers(self, method_user: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Method_User": method_user,
            "Accept": "*/*",
            "User-Agent": "HomeAssistant-edistribuzione",
            "client_id": "",
            "client_secret": "",
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        method_user: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_empty_404: bool = False,
    ) -> dict[str, Any]:
        safe_url = redact_url(url)
        try:
            return await self._request_json_raw(
                method,
                url,
                method_user,
                params=params,
                json_body=json_body,
                allow_empty_404=allow_empty_404,
            )
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ApiError(f"{type(err).__name__} chiamando {safe_url}", rete=True) from err
        except ValueError as err:
            raise ApiError(f"Risposta non JSON da {safe_url}: {err}") from err

    async def _request_json_raw(
        self,
        method: str,
        url: str,
        method_user: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_empty_404: bool = False,
    ) -> dict[str, Any]:
        safe_url = redact_url(url)
        headers = self._headers(method_user)
        if method == "GET":
            ctx = self._session.get(url, headers=headers, params=params, timeout=self._timeout)
        else:
            ctx = self._session.post(url, headers=headers, json=json_body, timeout=self._timeout)

        async with ctx as resp:
            _LOGGER.debug(
                "%s %s (%s) %s: HTTP %s",
                method,
                safe_url,
                method_user,
                redact_params(params) if params else {},
                resp.status,
            )
            if resp.status == 401:
                raise ApiError("401 Unauthorized - access_token scaduto", status=401)
            if resp.status == 404 and allow_empty_404:
                body_preview = await resp.text()
                if _is_routing_404(body_preview):
                    _LOGGER.error(
                        "%s ha risposto 404 HTML (endpoint cambiato?). Anteprima: %r",
                        safe_url,
                        redact_preview(body_preview, 200),
                    )
                    raise ApiError(f"HTTP 404 da {safe_url}", status=404)
                # Backend uses 404 when a day's data is not published yet.
                _LOGGER.debug(
                    "404 su %s (nessun dato disponibile). Anteprima: %r",
                    safe_url,
                    redact_preview(body_preview, 200),
                )
                return {"data": []}
            if resp.status >= 400:
                body_preview = await resp.text()
                _LOGGER.error(
                    "%s ha risposto %s. Anteprima: %r",
                    safe_url,
                    resp.status,
                    redact_preview(body_preview, 300),
                )
                raise ApiError(f"HTTP {resp.status} da {safe_url}", status=resp.status)
            payload = await resp.json(content_type=None)

        if not isinstance(payload, dict):
            raise ApiError(f"Risposta JSON inattesa da {safe_url} ({type(payload).__name__})")

        meta = payload.get("meta")
        if isinstance(meta, dict):
            status = meta.get("status")
            if status not in ("OK", None):
                detail = meta.get("message") or meta.get("errorDescription") or ""
                safe_detail = redact_preview(str(detail), 200) if detail else ""
                suffix = f": {safe_detail}" if safe_detail else ""
                raise ApiError(f"API returned meta.status={status}{suffix}")
        return payload

    async def _get_json(self, url: str, method_user: str, params: dict[str, Any]) -> dict[str, Any]:
        return await self._request_json(
            "GET", url, method_user, params=params, allow_empty_404=True
        )

    async def async_get_supplies(self) -> list[dict[str, Any]]:
        """List of PODs (delivery points) tied to the account."""
        # Send json={} so MuleSoft always receives a valid JSON body.
        payload = await self._request_json(
            "POST", MISURE_GET_SUPPLIES_URL, METHOD_USER_ELENCO_POD, json_body={}
        )
        try:
            pods = payload["data"][0]["pods"]
            if not isinstance(pods, list):
                pods = []
        except (KeyError, IndexError, TypeError):
            pods = []
        _LOGGER.debug("getSupplies: %d POD sull'account", len(pods))
        return pods

    async def async_get_reading(
        self, pod: str, date_from: date, date_to: date
    ) -> list[dict[str, Any]]:
        """Official cumulative monthly readings (EA/ER per band + POT peaks)."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
        }
        payload = await self._get_json(MISURE_READING_URL, METHOD_USER_LETTURE, params)
        return _extract_data_list(payload)

    async def async_get_daily_load_profile(
        self,
        pod: str,
        date_from: date,
        date_to: date | None = None,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Quarter-hourly load curve over [date_from, date_to] for one magnitude."""
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
        return _extract_data_list(payload)

    async def async_get_monthly_load_profile(
        self,
        pod: str,
        date_from: date,
        date_to: date,
        magnitude: str = MAGNITUDE_PRELEVATA,
    ) -> list[dict[str, Any]]:
        """Total energy per day over a range."""
        params = {
            "pointofdelivery": pod,
            "rangeDateFrom": date_from.isoformat(),
            "rangeDateTo": date_to.isoformat(),
            "magnitude": magnitude,
        }
        payload = await self._get_json(
            MISURE_MONTHLY_LOAD_PROFILE_URL, METHOD_USER_CURVA_MESE, params
        )
        return _extract_data_list(payload)

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
        return _extract_data_list(payload)
