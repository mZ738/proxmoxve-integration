# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""
Spread an entry's coordinators across the start of the polling interval.

Home Assistant schedules a coordinator's next poll at the previous one,
rounded down to the whole second, plus the interval - with under half a
second of random jitter. Every coordinator of an entry gets its first
schedule when its entities attach, which all happens within the same second
of setup, so they stay in step for as long as Home Assistant runs: forty of
them on a four-node cluster, all due in the same second, every minute. The
reads themselves are cheap and shared (see `SharedResources`), but each
coordinator then decodes its answer and writes its entities' states on the
event loop, and forty of those back to back stall the whole of Home
Assistant for a noticeable fraction of a second once a minute.

So once setup is done each coordinator is refreshed again at its own offset,
which re-anchors its schedule there. The offsets are spread evenly over a
window shorter than `RESOURCES_TTL`, so one `cluster/resources` read still
serves the whole burst - the burst just takes a few seconds instead of one.
It costs one extra poll per coordinator, once, at startup.

Only public coordinator API is used: `async_refresh()` reschedules the
coordinator from the moment it completes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .coordinator import RESOURCES_TTL

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

# The burst is spread over this many seconds. Under RESOURCES_TTL with a
# margin for a slow read, so the shared read made by the first coordinator of
# the burst is still fresh for the last.
STAGGER_WINDOW: Final = min(12.0, RESOURCES_TTL * 0.8)
# Wait this long after setup before the first re-anchoring refresh, so every
# platform has attached its listeners (a coordinator without listeners does
# not keep a schedule).
STAGGER_START: Final = 2.0
# Coordinators on this interval or shorter take part: the live ones (30-120 s)
# and the five-minute task scan, which lands on the burst every fifth minute.
# The hourly reads are left alone - an extra read of them buys nothing.
MAX_STAGGERED_SECONDS: Final = 300


def _flatten(coordinators: dict[str, Any]) -> list[tuple[str, DataUpdateCoordinator]]:
    """(key, coordinator) pairs; disk and ZFS keys hold a list per node."""
    out: list[tuple[str, DataUpdateCoordinator]] = []
    for key, value in coordinators.items():
        items = value if isinstance(value, list) else [value]
        out.extend(
            (f"{key}#{index}", item)
            for index, item in enumerate(items)
            if isinstance(item, DataUpdateCoordinator)
        )
    return out


def stagger_offsets(
    keys: list[str], window: float = STAGGER_WINDOW
) -> dict[str, float]:
    """
    Evenly spaced offsets in [0, window) for the given keys.

    Sorted by key, so the same cluster lands in the same order on every
    start and a coordinator's place in the burst does not move about.
    """
    ordered = sorted(keys)
    step = window / len(ordered) if ordered else 0.0
    return {key: index * step for index, key in enumerate(ordered)}


@callback
def async_stagger_polling(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinators: dict[str, Any],
    *,
    skip: frozenset[str] = frozenset(),
) -> int:
    """
    Schedule each coordinator's re-anchoring refresh; return how many.

    `skip` names coordinator keys to leave where they are - discovery, which
    deliberately does not refresh at setup and reads afresh when it does.
    Coordinators slower than MAX_STAGGERED_SECONDS are left alone as well.
    """
    candidates = [
        (key, coordinator)
        for key, coordinator in _flatten(coordinators)
        if key.split("#", 1)[0] not in skip
        and coordinator.update_interval is not None
        and coordinator.update_interval.total_seconds() <= MAX_STAGGERED_SECONDS
    ]
    if len(candidates) < 2:
        return 0
    offsets = stagger_offsets([key for key, _ in candidates])
    for key, coordinator in candidates:

        @callback
        def _refresh(
            _now: Any, coordinator: DataUpdateCoordinator = coordinator
        ) -> None:
            config_entry.async_create_background_task(
                hass,
                coordinator.async_refresh(),
                f"proxmoxve stagger {coordinator.name}",
            )

        config_entry.async_on_unload(
            async_call_later(hass, STAGGER_START + offsets[key], _refresh)
        )
    return len(candidates)
