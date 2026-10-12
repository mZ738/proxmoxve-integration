# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Tests for surviving an unreachable Proxmox and a Home Assistant shutdown."""

from unittest.mock import AsyncMock, patch

import pytest
from aioproxmox.exceptions import ProxmoxAuthError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .fake_api import NODE, FakeProxmox, connection_refused, ssl_rejection


async def test_unreachable_host_asks_to_be_retried(
    hass: HomeAssistant, current_entry: MockConfigEntry
) -> None:
    """
    Test a host that is down leaves the entry retrying rather than failed.

    An exception escaping async_setup_entry puts the entry in SETUP_ERROR,
    which Home Assistant never retries - the integration then stays dead until
    someone reloads it by hand, even after Proxmox comes back.
    """
    with patch(
        "aioproxmox.ProxmoxHTTPAuth._get_new_tokens",
        new=AsyncMock(side_effect=connection_refused()),
    ):
        await hass.config_entries.async_setup(current_entry.entry_id)
        await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_rejected_certificate_asks_to_be_retried(
    hass: HomeAssistant, current_entry: MockConfigEntry
) -> None:
    """Test a certificate the session does not accept is a retry with a hint, not a crash."""
    with patch(
        "aioproxmox.ProxmoxHTTPAuth._get_new_tokens",
        new=AsyncMock(side_effect=ssl_rejection()),
    ):
        await hass.config_entries.async_setup(current_entry.entry_id)
        await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.SETUP_RETRY
    assert "verify_ssl" in str(current_entry.reason)


async def test_reachable_host_sets_up(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test the guard does not get in the way when the host answers."""
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.LOADED


async def test_shutdown_stops_the_coordinators(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test the stop event stops refreshes from being scheduled.

    A refresh starting late would hold up shutdown waiting for Proxmox.
    """
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    with patch.object(
        DataUpdateCoordinator, "async_shutdown", new=AsyncMock()
    ) as shutdown:
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await hass.async_block_till_done()

    assert shutdown.await_count > 0


async def test_a_host_not_issuing_tickets_yet_is_retried(
    hass: HomeAssistant, current_entry: MockConfigEntry
) -> None:
    """
    Test a login refused with anything but 401 leaves the entry retrying.

    During boot pveproxy answers before the ticket service does, and the
    login fails with the same exception as a wrong password. Only a 401 is
    about the credentials; everything else is "not yet".
    """
    with patch(
        "aioproxmox.ProxmoxHTTPAuth._get_new_tokens",
        new=AsyncMock(
            side_effect=ProxmoxAuthError(
                "Couldn't authenticate user x@pve to https://h/access/ticket: Code 595"
            )
        ),
    ):
        await hass.config_entries.async_setup(current_entry.entry_id)
        await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_refused_password_asks_for_credentials(
    hass: HomeAssistant, current_entry: MockConfigEntry
) -> None:
    """Test a 401 at setup is still what reauthentication is for."""
    with patch(
        "aioproxmox.ProxmoxHTTPAuth._get_new_tokens",
        new=AsyncMock(
            side_effect=ProxmoxAuthError(
                "Couldn't authenticate user x@pve to https://h/access/ticket: Code 401"
            )
        ),
    ):
        await hass.config_entries.async_setup(current_entry.entry_id)
        await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.SETUP_ERROR


async def test_a_node_without_data_leaves_the_other_entities_alone(
    hass: HomeAssistant,
    fake_api: FakeProxmox,
    current_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Test a node without data does not take the platforms down with it.

    Reported upstream in #712: a node whose status read fails leaves its
    coordinator without data, and the device description then read a
    processor name that was never assigned. The UnboundLocalError came out
    of `device_info` while the platforms were being set up, so one node
    that could not be read cost the entry every entity it has - the
    cluster's and the other nodes' included.
    """
    # Listed by the cluster, so its coordinators are built - but its own
    # status read fails, which is what leaves the coordinator without data.
    del fake_api.routes[f"nodes/{NODE}/status"]

    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.LOADED
    assert "model_processor" not in caplog.text
    assert "Error while setting up proxmoxve platform" not in caplog.text
    # The sensor platform ran to the end: the node's failed-task sensor is
    # created from the same device description that used to raise.
    assert er.async_get(hass).async_get(f"sensor.node_{NODE}_failed_tasks") is not None
