# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Tests for moving to another node of the cluster when the configured one is gone."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmoxve.api import ProxmoxClient
from custom_components.proxmoxve.const import COORDINATORS, PROXMOX_CLIENT, ProxmoxType

from .fake_api import NODE, FakeProxmox

CONFIGURED = "192.168.10.101"
LEARNED = ("192.0.2.10", "192.0.2.11")


async def test_learned_hosts_come_after_the_configured_one(
    hass: HomeAssistant, fake_api: FakeProxmox
) -> None:
    """Test the configured host stays first and repeats are dropped."""
    client = ProxmoxClient(
        hass,
        host=CONFIGURED,
        user="homeassistant",
        password="secret",  # noqa: S106 - invented
        token_name="homeassistant",  # noqa: S106 - not a secret
        realm="pve",
        verify_ssl=False,
    )
    await client.build_client()

    client.learn_hosts([*LEARNED, CONFIGURED, LEARNED[0], "", None])  # type: ignore[list-item]

    assert client.hosts == (CONFIGURED, *LEARNED)
    assert client.host == CONFIGURED


async def test_the_cluster_stays_in_home_assistant_when_the_host_goes(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test the reported problem: one host down took the whole cluster out.

    Everything goes through the configured host's pveproxy. Once it is
    gone, the API object moves to a node it learned from `cluster/status`
    and the coordinators keep reading - they all hold that one object, so
    the move reaches every one of them.
    """
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    assert current_entry.state is ConfigEntryState.LOADED
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    assert client.hosts == (CONFIGURED, "192.0.2.10", "192.0.2.11")
    node = current_entry.runtime_data[COORDINATORS][f"{ProxmoxType.Node}_{NODE}"]
    assert node.proxmox is client.get_api_client()

    fake_api.dead_hosts.add(CONFIGURED)
    seen_before = len(fake_api.hosts_seen)
    await node.async_refresh()

    assert node.last_update_success
    assert client.host == "192.0.2.10"
    # The dead host was tried once for this poll, then the fallback answered
    # - that read and every one after it.
    assert fake_api.hosts_seen[seen_before] == CONFIGURED
    assert set(fake_api.hosts_seen[seen_before + 1 :]) == {"192.0.2.10"}


async def test_a_second_coordinator_finds_the_switch_done(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test the next poll after a switch goes to the fallback straight away."""
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    coordinators = current_entry.runtime_data[COORDINATORS]
    node = coordinators[f"{ProxmoxType.Node}_{NODE}"]
    lxc = coordinators[f"{ProxmoxType.LXC}_100"]

    fake_api.dead_hosts.add(CONFIGURED)
    await node.async_refresh()
    seen_before = len(fake_api.hosts_seen)
    await lxc.async_refresh()

    assert lxc.last_update_success
    assert set(fake_api.hosts_seen[seen_before:]) == {"192.0.2.10"}


async def test_without_any_other_node_the_failure_is_reported(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test a single node that goes away still fails the update, as before."""
    del fake_api.routes["cluster/status"]
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    assert client.hosts == (CONFIGURED,)
    node = current_entry.runtime_data[COORDINATORS][f"{ProxmoxType.Node}_{NODE}"]

    fake_api.dead_hosts.add(CONFIGURED)
    await node.async_refresh()

    assert not node.last_update_success
    assert client.host == CONFIGURED


async def test_nothing_else_answering_leaves_the_configured_host(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test the original failure stands when no other node answers either."""
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    node = current_entry.runtime_data[COORDINATORS][f"{ProxmoxType.Node}_{NODE}"]

    fake_api.dead_hosts.update((CONFIGURED, *LEARNED))
    await node.async_refresh()

    assert not node.last_update_success
    assert client.host == CONFIGURED
