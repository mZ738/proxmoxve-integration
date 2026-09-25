# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Tests for moving to another node of the cluster when the configured one is gone."""

from typing import Any
from unittest.mock import patch

from aioproxmox import ProxmoxVE
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmoxve.api import ProxmoxClient
from custom_components.proxmoxve.const import (
    CONF_CLUSTER_HOSTS,
    CONF_TOKEN_NAME,
    COORDINATORS,
    PROXMOX_CLIENT,
    ProxmoxType,
)
from custom_components.proxmoxve.coordinator import shared_resources

from .fake_api import NODE, FakeProxmox, api_error, connection_refused

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
    # The read, then the host asked whether it is really gone - a failed
    # read alone is no proof, since a path may name a node that is down
    # rather than the host itself. From there on, the fallback answers.
    tried = fake_api.hosts_seen[seen_before:]
    assert tried[:2] == [CONFIGURED, CONFIGURED]
    assert set(tried[2:]) == {"192.0.2.10"}


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


async def test_the_entry_remembers_the_cluster_for_the_next_start(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test the addresses read from `cluster/status` are kept in the entry."""
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.data[CONF_CLUSTER_HOSTS] == list(LEARNED)


async def test_a_start_while_the_configured_host_is_down(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test the reported problem: a restart took the entry out until the node was back.

    The fallback was learned from `cluster/status` once a connection stood,
    and was forgotten with the session. Home Assistant restarting while the
    configured node is down - a rack rebuild, in the report - then had
    nowhere to go, because setup begins against the configured host and
    nothing else was known.
    """
    hass.config_entries.async_update_entry(
        current_entry,
        data={**current_entry.data, CONF_CLUSTER_HOSTS: list(LEARNED)},
    )
    fake_api.dead_hosts.add(CONFIGURED)

    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.LOADED
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    assert client.host == LEARNED[0]
    # Still the configured host's entry; it is where setup goes again once
    # the node is back, and nothing was rewritten behind the user's back.
    assert current_entry.data[CONF_HOST] == CONFIGURED


async def test_a_start_with_nothing_remembered_waits_as_before(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test a host that is down and no known cluster still leaves setup retrying."""
    fake_api.dead_hosts.add(CONFIGURED)

    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.SETUP_RETRY


async def test_a_token_start_while_the_configured_host_is_down(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test the other credential kind gets to another node as well.

    A token is not logged in, so what meets the dead host is the `version`
    read that stands in for the login - inside `request()`, which moves to
    another node itself once it knows of one.
    """
    hass.config_entries.async_update_entry(
        current_entry,
        data={
            **current_entry.data,
            CONF_TOKEN_NAME: "homeassistant",
            CONF_CLUSTER_HOSTS: list(LEARNED),
        },
    )
    fake_api.dead_hosts.add(CONFIGURED)

    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.LOADED
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    assert client.host == LEARNED[0]


async def test_a_login_that_meets_a_dead_host_moves_on(
    hass: HomeAssistant, fake_api: FakeProxmox
) -> None:
    """
    Test the password login is given the failover it does not have itself.

    A token is proved with `version` through `request()`, which moves to
    another node on its own. The login does not go through it, so a
    password setup would sit on the dead host although the cluster was
    known.
    """
    client = ProxmoxClient(
        hass,
        host=CONFIGURED,
        user="homeassistant",
        password="secret",  # noqa: S106 - invented
        realm="pve",
        verify_ssl=False,
        fallback_hosts=LEARNED,
    )
    logged_in: list[str] = []

    # The fake already replaced the login with one that does nothing; this
    # one answers for the host it is pointed at.
    async def login(**_kwargs: Any) -> None:
        auth = built[-1].auth
        logged_in.append(auth.base_url)
        if CONFIGURED in auth.base_url:
            raise connection_refused(CONFIGURED)

    built: list[Any] = []
    real_init = ProxmoxVE.__init__

    def remember(self: Any, *args: Any, **kwargs: Any) -> None:
        real_init(self, *args, **kwargs)
        self.auth._get_new_tokens = login  # noqa: SLF001
        built.append(self)

    with patch.object(ProxmoxVE, "__init__", remember):
        await client.build_client()

    assert client.host == LEARNED[0]
    assert CONFIGURED in logged_in[0]
    assert LEARNED[0] in logged_in[-1]


async def test_a_node_the_cluster_calls_offline_is_not_asked_after(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test the reads for a node that is off are skipped, with a reason.

    Everything goes to one host, which forwards what belongs to another
    node. With that node down, pveproxy waits and then answers 595 "No
    route to host" - once per guest, storage and node read, every poll,
    seconds at a time. Worse, a connection it drops there looks exactly
    like a host that went away, which is what sent the client around the
    cluster on a live four-node test.
    """
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = current_entry.runtime_data[COORDINATORS][f"{ProxmoxType.LXC}_100"]

    for row in fake_api.routes["cluster/resources"]:
        if row.get("type") == "node" and row.get("node") == NODE:
            row["status"] = "offline"
    # The shared read is a few seconds old; in the real world the next
    # burst reads again.
    shared_resources(hass, current_entry).forget()
    fake_api.calls.clear()
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert "offline" in str(coordinator.last_exception)
    assert not [path for path in fake_api.paths() if path.startswith(f"nodes/{NODE}/")]


async def test_a_host_that_answers_is_kept(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test a failed read does not move a client whose host is fine.

    The library asks the host `version` before switching. Without that, a
    read that cannot be answered here - a guest on a node that is down -
    read as "this host stopped answering", and every coordinator took the
    client one node further around the cluster.
    """
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    node = current_entry.runtime_data[COORDINATORS][f"{ProxmoxType.Node}_{NODE}"]

    # What pveproxy does for a node it cannot reach: it drops the
    # connection, on the host that is answering perfectly well.
    fake_api.routes["nodes"] = connection_refused(CONFIGURED)
    await node.async_refresh()

    assert not node.last_update_success
    assert client.host == CONFIGURED
    assert client.hosts == (CONFIGURED, *LEARNED)


async def test_a_start_where_the_other_nodes_want_a_ticket_first(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test a fallback node is used although it refuses the read that proves it.

    `version` needs authentication, and a password client has no ticket
    before it logs in - so every healthy node answers 401 while the
    configured one is down. Taken for silence, that left the reload of a
    live cluster failing with "connection is unreachable" for as long as
    the node stayed off, with three nodes running.
    """
    hass.config_entries.async_update_entry(
        current_entry,
        data={**current_entry.data, CONF_CLUSTER_HOSTS: list(LEARNED)},
    )
    fake_api.dead_hosts.add(CONFIGURED)
    fake_api.routes["version"] = api_error(
        401, "authentication failure", "no ticket was sent"
    )

    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.LOADED
    client: ProxmoxClient = current_entry.runtime_data[PROXMOX_CLIENT]
    assert client.host == LEARNED[0]
