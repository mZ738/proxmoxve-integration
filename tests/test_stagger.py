# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""
Tests for spreading an entry's coordinators across the start of the interval.

Every coordinator of an entry is scheduled in the same second of setup and
stays in step with the rest for as long as Home Assistant runs, so the whole
cluster polled - and wrote its entities - in one lump each minute.
"""

import logging
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.proxmoxve.const import COORDINATORS, DOMAIN
from custom_components.proxmoxve.coordinator import RESOURCES_TTL
from custom_components.proxmoxve.stagger import (
    MAX_STAGGERED_SECONDS,
    STAGGER_START,
    STAGGER_WINDOW,
    async_stagger_polling,
    stagger_offsets,
)

from .fake_api import FakeProxmox
from .test_setup_full import _setup


def test_offsets_are_even_ordered_and_inside_the_shared_read() -> None:
    """Test offsets are evenly spaced, stable, and end before the read goes stale."""
    offsets = stagger_offsets(["qemu_101", "node_pve", "lxc_200", "storage_local"])
    assert sorted(offsets.values()) == pytest.approx(
        [0.0, STAGGER_WINDOW / 4, STAGGER_WINDOW / 2, 3 * STAGGER_WINDOW / 4]
    )
    # Sorted by key: the same cluster gets the same order on every start.
    assert offsets == stagger_offsets(
        ["storage_local", "lxc_200", "qemu_101", "node_pve"]
    )
    assert max(offsets.values()) < RESOURCES_TTL
    assert stagger_offsets([]) == {}


def _coordinator(
    hass: HomeAssistant,
    name: str,
    seconds: int | None,
    calls: list[str],
    entry: MockConfigEntry,
) -> DataUpdateCoordinator:
    async def _update() -> str:
        calls.append(name)
        return name

    return DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        name=name,
        config_entry=entry,
        update_method=_update,
        update_interval=None if seconds is None else timedelta(seconds=seconds),
    )


async def test_each_coordinator_is_refreshed_at_its_own_offset(
    hass: HomeAssistant,
) -> None:
    """Test the refreshes land spread over the window, not together."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    calls: list[str] = []
    coordinators = {
        "node_a": _coordinator(hass, "node_a", 60, calls, entry),
        "qemu_1": _coordinator(hass, "qemu_1", 60, calls, entry),
        "disk_a": [
            _coordinator(hass, "disk_a0", 60, calls, entry),
            _coordinator(hass, "disk_a1", 60, calls, entry),
        ],
        "tasks_a": _coordinator(hass, "tasks_a", MAX_STAGGERED_SECONDS, calls, entry),
        "certificate_a": _coordinator(
            hass, "certificate_a", 3600, calls, entry
        ),  # hourly: left alone
        "proxmox_discovery": _coordinator(
            hass, "discovery", 60, calls, entry
        ),  # skipped by name
        "no_schedule": _coordinator(hass, "no_schedule", None, calls, entry),
        "not_a_coordinator": object(),
    }

    count = async_stagger_polling(
        hass, entry, coordinators, skip=frozenset({"proxmox_discovery"})
    )
    assert count == 5

    start = dt_util.utcnow()
    seen: list[tuple[float, list[str]]] = []
    for step in range(int((STAGGER_START + STAGGER_WINDOW) * 4) + 2):
        async_fire_time_changed(hass, start + timedelta(seconds=step / 4))
        await hass.async_block_till_done()
        seen.append((step / 4, list(calls)))

    assert sorted(calls) == sorted(
        ["node_a", "qemu_1", "disk_a0", "disk_a1", "tasks_a"]
    )
    # Not all at once: the first refresh has happened well before the last.
    first_at = next(t for t, done in seen if done)
    all_at = next(t for t, done in seen if len(done) == 5)
    assert all_at - first_at >= STAGGER_WINDOW * 0.6
    # The test clock fires timers at whole-second granularity.
    assert first_at >= STAGGER_START - 1


async def test_unloading_cancels_what_has_not_run(hass: HomeAssistant) -> None:
    """Test an entry unloaded before its burst leaves nothing behind to fire."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    calls: list[str] = []
    coordinators = {
        f"qemu_{i}": _coordinator(hass, f"qemu_{i}", 60, calls, entry) for i in range(4)
    }
    assert async_stagger_polling(hass, entry, coordinators) == 4
    await entry._async_process_on_unload(hass)  # noqa: SLF001 - what unloading runs
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=STAGGER_START + STAGGER_WINDOW + 5)
    )
    await hass.async_block_till_done()
    assert calls == []


async def test_a_single_coordinator_is_left_alone(hass: HomeAssistant) -> None:
    """Test there is nothing to spread with one coordinator."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    assert (
        async_stagger_polling(
            hass, entry, {"node_a": _coordinator(hass, "node_a", 60, [], entry)}
        )
        == 0
    )


async def test_setup_spreads_the_real_cluster_and_keeps_one_resource_read(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test a full setup staggers its coordinators and the burst still reads once."""
    await _setup(hass, current_entry)
    coordinators = current_entry.runtime_data[COORDINATORS]
    assert len(coordinators) > 2

    before = len([p for p in fake_api.paths() if p.startswith("cluster/resources")])
    start = dt_util.utcnow()
    for step in range(int((STAGGER_START + STAGGER_WINDOW) * 2) + 2):
        async_fire_time_changed(hass, start + timedelta(seconds=step / 2))
        await hass.async_block_till_done()
    reads = (
        len([p for p in fake_api.paths() if p.startswith("cluster/resources")]) - before
    )
    # The whole staggered burst fits inside the shared read's lifetime.
    assert reads <= 1
