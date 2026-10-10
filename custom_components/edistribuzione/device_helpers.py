"""Helpers to link a device to its "parent" in the device registry.

Home Assistant 2026.8 deprecated DeviceInfo["via_device"] (the identifier
tuple) in favour of via_device_id (the parent device's internal id), because
identifiers are no longer globally unique but only per config entry.

This is not just a far-off deprecation (2027.8): on Home Assistant 2026.9 the
sensor platform can turn it into a RuntimeError instead of a warning, when the
call chain does not let Core attribute the call to the custom integration. In
that case the entities are NOT added at all (already seen on other integrations
with HA 2026.9).

via_device_id does not exist before HA 2026.8, so the two are chosen at runtime
based on the Home Assistant version.
"""
from __future__ import annotations

import logging

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

_LOGGER = logging.getLogger(__name__)

# First version in which DeviceInfo accepts via_device_id.
_VERSIONE_VIA_DEVICE_ID = (2026, 8)


def supporta_via_device_id() -> bool:
    """True if this HA version accepts via_device_id in DeviceInfo.

    When in doubt (version not parseable) returns False: it falls back to
    via_device, which on old versions is the only thing that works and on new
    ones produces at most a warning.
    """
    try:
        maggiore, minore = HA_VERSION.split(".")[:2]
        return (int(maggiore), int(minore)) >= _VERSIONE_VIA_DEVICE_ID
    except (AttributeError, ValueError):
        _LOGGER.debug("Versione di Home Assistant non interpretabile: %r", HA_VERSION)
        return False


def assicura_dispositivo_padre(
    hass: HomeAssistant, entry_id: str, device_info_padre: dict
) -> str | None:
    """Register (or fetch) the parent device and return its internal id.

    Must be called BEFORE building the child entities: on first start the parent
    does not exist in the registry yet, because normally the entities create it
    when they are added - so just looking it up would return None exactly when
    it is needed.

    Returns None if this HA version does not use via_device_id (in which case
    the caller falls back to via_device) or if registration fails: a failure
    here must not prevent the entities from being created, at most the
    hierarchical link in the UI is lost.
    """
    if not supporta_via_device_id():
        return None
    try:
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_or_create(config_entry_id=entry_id, **device_info_padre)
        return device.id
    except Exception:  # noqa: BLE001 - see docstring: must never block
        _LOGGER.exception(
            "Impossibile registrare il dispositivo padre: i dispositivi per POD "
            "non verranno collegati gerarchicamente, ma le entità funzionano"
        )
        return None


def collega_al_padre(
    device_info: dict,
    identificatori_padre: set[tuple[str, str]],
    id_padre: str | None,
) -> dict:
    """Add the link to the parent device to device_info.

    Uses via_device_id when available (HA 2026.8+ and a registered parent),
    otherwise via_device. Mutates and returns the same dict.
    """
    if id_padre is not None:
        device_info["via_device_id"] = id_padre
    else:
        device_info["via_device"] = next(iter(identificatori_padre))
    return device_info
