# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Tests for how a refused or failed API read is reported."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioproxmox.exceptions import ProxmoxAuthError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmoxve import DOMAIN
from custom_components.proxmoxve.api import ProxmoxClient
from custom_components.proxmoxve.const import COORDINATORS, ProxmoxType
from custom_components.proxmoxve.coordinator import poll_api

from .const import USER_INPUT_OK
from .fake_api import NODE, Answers, FakeProxmox, api_error, connection_refused

FORBIDDEN = api_error(403, "Forbidden", "Permission check failed")


def _api(*answers: object) -> MagicMock:
    """Return an API object whose requests answer, or raise, in turn."""
    proxmox = MagicMock()
    proxmox.request = AsyncMock(side_effect=list(answers))
    return proxmox


@pytest.mark.parametrize(
    ("api_path", "api_category", "resource_id", "expected_resource"),
    [
        ("nodes/pve/status", ProxmoxType.Node, "pve", "Node pve"),
        ("nodes/pve/apt/update", ProxmoxType.Update, "Update pve", "Update pve"),
        # The cluster-wide reads pass no resource id at all. This used to
        # raise AttributeError on `None.replace` instead of a repair issue.
        ("cluster/resources", ProxmoxType.Resources, None, "Resources"),
    ],
)
async def test_a_refused_read_raises_a_repair(
    hass: HomeAssistant,
    api_path: str,
    api_category: ProxmoxType,
    resource_id: str | None,
    expected_resource: str,
) -> None:
    """Test a 403 yields None and a repair naming the resource and privilege."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)

    result = await poll_api(
        hass, entry, _api(FORBIDDEN), api_path, api_category, resource_id
    )

    assert result is None
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_forbidden")
    assert issue is not None
    assert issue.translation_placeholders["count"] == "1"
    assert issue.translation_placeholders["items"].startswith(
        f"* `{expected_resource}` — `['perm'"
    )


async def test_a_read_allowed_again_clears_the_repair(hass: HomeAssistant) -> None:
    """Test the line goes off the repair once the read works."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    proxmox = _api(FORBIDDEN, {"status": "online"})

    await poll_api(hass, entry, proxmox, "nodes/pve/status", ProxmoxType.Node, "pve")
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_forbidden")

    result = await poll_api(
        hass, entry, proxmox, "nodes/pve/status", ProxmoxType.Node, "pve"
    )

    assert result == {"status": "online"}
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_forbidden")
        is None
    )


async def test_other_errors_fail_the_update(hass: HomeAssistant) -> None:
    """Test anything but a 403 is an update failure, not a repair."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    proxmox = _api(api_error(500, "Internal Server Error", "boom"))

    with pytest.raises(UpdateFailed):
        await poll_api(hass, entry, proxmox, "nodes", ProxmoxType.Node, "pve")


async def test_a_host_that_is_gone_fails_the_update(hass: HomeAssistant) -> None:
    """Test a connection error is an update failure, to be retried next time."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)

    with pytest.raises(UpdateFailed):
        await poll_api(
            hass, entry, _api(connection_refused()), "nodes", ProxmoxType.Node, "pve"
        )
    with pytest.raises(UpdateFailed):
        await poll_api(
            hass, entry, _api(TimeoutError()), "nodes", ProxmoxType.Node, "pve"
        )


async def test_a_refused_ticket_is_renewed_and_the_read_repeated(
    hass: HomeAssistant,
) -> None:
    """
    Test the reported problem: a host off for a night demanded new credentials.

    A ticket renews itself with the ticket, and once that has expired the
    renewal is refused exactly like a wrong password. The library keeps the
    password and logs in again with it before giving up - and here that
    works, so the read is simply repeated.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    client = ProxmoxClient(
        hass,
        host="node.example.invalid",
        user="homeassistant",
        password="secret",  # noqa: S106 - invented
        realm="pve",
        verify_ssl=False,
    )
    with (
        patch("aioproxmox.ProxmoxHTTPAuth._get_new_tokens", new=AsyncMock()) as login,
        patch(
            "aioproxmox.ProxmoxVE._request_once",
            new=AsyncMock(
                side_effect=[api_error(401, "Unauthorized", ""), {"status": "online"}]
            ),
        ) as request,
    ):
        await client.build_client()
        login.reset_mock()

        result = await poll_api(
            hass,
            entry,
            client.get_api_client(),
            "nodes/pve/status",
            ProxmoxType.Node,
            "pve",
        )

    assert result == {"status": "online"}
    assert request.await_count == 2
    # The login after the refusal carries the password, not the dead ticket.
    assert login.await_args.kwargs["password"] == "secret"


async def test_a_password_that_really_is_wrong_still_asks_for_credentials(
    hass: HomeAssistant,
) -> None:
    """Test the fresh login failing too is what reauthentication is for."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    refused = ProxmoxAuthError("Couldn't authenticate user x@pve: Code 401")

    with pytest.raises(ConfigEntryAuthFailed):
        await poll_api(
            hass, entry, _api(refused), "nodes/pve/status", ProxmoxType.Node, "pve"
        )


async def test_a_host_not_issuing_tickets_is_not_a_wrong_password(
    hass: HomeAssistant,
) -> None:
    """Test a login refused with anything but 401 fails the update instead."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    not_yet = ProxmoxAuthError("Couldn't authenticate user x@pve: Code 595")

    with pytest.raises(UpdateFailed):
        await poll_api(
            hass, entry, _api(not_yet), "nodes/pve/status", ProxmoxType.Node, "pve"
        )


async def test_a_refused_token_asks_for_credentials(hass: HomeAssistant) -> None:
    """Test a token has no login to repeat; its 401 goes straight to reauthentication."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    proxmox = _api(api_error(401, "Unauthorized", ""))

    with pytest.raises(ConfigEntryAuthFailed):
        await poll_api(
            hass, entry, proxmox, "nodes/pve/status", ProxmoxType.Node, "pve"
        )
    assert proxmox.request.await_count == 1


@pytest.mark.parametrize(
    ("api_path", "api_category", "resource_id", "expected"),
    [
        # A disk's id is a WWN, and the privilege belongs to its node.
        (
            "nodes/pve/disks/list",
            ProxmoxType.Disk,
            "0x5002538e40000001",
            "['perm','/nodes/pve',['Sys.Audit']]",
        ),
        (
            "nodes/pve/disks/smart?disk=/dev/sda",
            ProxmoxType.Disk,
            "0x5002538e40000001",
            "['perm','/nodes/pve',['Sys.Audit']]",
        ),
        # A ZFS pool had no case at all and came out as "Unmapped".
        (
            "nodes/pve/disks/zfs",
            ProxmoxType.ZFS,
            "rpool",
            "['perm','/nodes/pve',['Sys.Audit']]",
        ),
        # A storage id carries its node; the ACL path is the bare name.
        (
            "nodes/pve/storage?storage=local",
            ProxmoxType.Storage,
            "storage/pve/local",
            "['perm','/storage/local',['Datastore.Audit'],'any',1]",
        ),
        (
            "nodes/pve/storage?storage=nas",
            ProxmoxType.Storage,
            "storage/nas",
            "['perm','/storage/nas',['Datastore.Audit'],'any',1]",
        ),
        # These were already right, and stay right.
        (
            "nodes/pve/status",
            ProxmoxType.Node,
            "pve",
            "['perm','/nodes/pve',['Sys.Audit']]",
        ),
        (
            "nodes/pve/tasks",
            ProxmoxType.Tasks,
            "pve",
            "['perm','/nodes/pve',['Sys.Audit']]",
        ),
        (
            "nodes/pve/apt/update",
            ProxmoxType.Update,
            "Update pve",
            "['perm','/nodes/pve',['Sys.Modify']]",
        ),
        (
            "nodes/pve/qemu/101/status/current",
            ProxmoxType.QEMU,
            "101",
            "['perm','/vms/101',['VM.Audit']]",
        ),
        # The bare listing has no node in the path; the read passes it.
        ("nodes", ProxmoxType.Node, "pve", "['perm','/nodes/pve',['Sys.Audit']]"),
    ],
)
async def test_the_repair_names_the_path_the_privilege_belongs_to(
    hass: HomeAssistant,
    api_path: str,
    api_category: ProxmoxType,
    resource_id: str,
    expected: str,
) -> None:
    """
    Test a refused read names an ACL path Proxmox actually has.

    The resource id used to be put where a node name belongs: a disk's
    repair asked for the privilege on `/nodes/<wwn>`, a storage's on
    `/storage/storage/<node>/<name>`, and a pool had no case at all.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)

    await poll_api(hass, entry, _api(FORBIDDEN), api_path, api_category, resource_id)
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_forbidden")
    assert issue is not None
    assert expected in issue.translation_placeholders["items"]


async def test_a_cluster_wide_refusal_raises_a_repair_not_a_name_error(
    hass: HomeAssistant,
) -> None:
    """
    Test the cluster-wide branch works at all.

    It names the optional HA-admin credentials, and that constant was used
    without being imported - so in the one situation the repair exists for,
    the handler raised `NameError`.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, data={**USER_INPUT_OK, "ha_admin_username": "ha-admin"}
    )
    entry.add_to_hass(hass)

    result = await poll_api(
        hass, entry, _api(FORBIDDEN), "cluster/ceph/status", ProxmoxType.Proxmox, None
    )
    await hass.async_block_till_done()

    assert result is None
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{entry.entry_id}_forbidden")
    assert issue is not None
    assert "ha-admin" in issue.translation_placeholders["items"]
    assert "['perm','/',['Sys.Audit']]" in issue.translation_placeholders["items"]


async def test_a_busy_proxy_is_asked_once_more(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test a read pveproxy could not finish is repeated instead of given up on.

    Reported in #595: a storage went unavailable every so often, and once
    5.3.2 named the reason it was `596 Errors during TLS negotiation,
    request sending and header processing: Connection timed out` -
    pveproxy passed the request on and the answer did not come back in
    time. Nothing refused it and no node was gone; the node was busy for
    a moment, and that moment took the entity out for a whole polling
    interval.
    """
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    key = f"{ProxmoxType.Storage}_storage/{NODE}/local"
    coordinator = current_entry.runtime_data[COORDINATORS][key]
    path = f"nodes/{NODE}/storage?storage=local"
    fake_api.routes[path] = Answers(
        api_error(
            596,
            "Errors during TLS negotiation, request sending and header processing",
            "Connection timed out",
        ),
        fake_api.routes[path],
    )
    fake_api.calls.clear()

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert fake_api.paths().count(path) == 2


async def test_a_proxy_that_cannot_reach_the_node_is_not_asked_twice(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """
    Test 595 is taken at its word: the node is not there.

    `595 No route to host` is the connection pveproxy could not establish
    at all, which is what a node that is switched off answers. Asking
    again would only wait out a second timeout - up to 25 seconds on a
    live cluster - for an answer that cannot come.
    """
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()
    key = f"{ProxmoxType.Storage}_storage/{NODE}/local"
    coordinator = current_entry.runtime_data[COORDINATORS][key]
    path = f"nodes/{NODE}/storage?storage=local"
    fake_api.routes[path] = api_error(595, "No route to host", "")
    fake_api.calls.clear()

    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert fake_api.paths().count(path) == 1
