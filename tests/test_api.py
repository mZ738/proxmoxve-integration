# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Tests for the Proxmox VE API helpers."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioproxmox.exceptions import ProxmoxAPIError, ProxmoxAuthError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmoxve import DOMAIN
from custom_components.proxmoxve.api import (
    SNAPSHOT_NAME_MAX_LENGTH,
    ProxmoxClient,
    auth_error_status,
    is_auth_error,
    permission_check_from,
    post_api_command,
    snapshot_name,
    token_name_only,
)
from custom_components.proxmoxve.const import ProxmoxCommand, ProxmoxType

from .const import USER_INPUT_OK, mock_config_entry
from .fake_api import api_error

UPID = "UPID:pve:0000FFFF:0000FFFF:69554D00:qmstart:100:root@pam:"


def _client_with(proxmox: MagicMock) -> MagicMock:
    """Return a client whose API object is `proxmox`."""
    proxmox_client = MagicMock()
    proxmox_client.get_api_client.return_value = proxmox
    return proxmox_client


def _api(answer: object = UPID) -> MagicMock:
    """Return an API object that answers every request with `answer`."""
    proxmox = MagicMock()
    proxmox.request = AsyncMock(
        side_effect=answer if isinstance(answer, Exception) else None,
        return_value=None if isinstance(answer, Exception) else answer,
    )
    return proxmox


@pytest.mark.parametrize(
    ("api_category", "expected_path", "expected_body"),
    [
        # LXC config endpoint has no skiplock parameter and rejects it.
        (ProxmoxType.LXC, "nodes/pve/lxc/100/config", {"delete": "lock"}),
        # QEMU needs skiplock=1 to edit a locked guest (root@pam only).
        (
            ProxmoxType.QEMU,
            "nodes/pve/qemu/100/config",
            {"delete": "lock", "skiplock": 1},
        ),
    ],
)
async def test_post_api_command_unlock(
    hass: HomeAssistant,
    api_category: ProxmoxType,
    expected_path: str,
    expected_body: dict,
) -> None:
    """Test unlock sends a PUT to the config endpoint to remove the lock."""
    proxmox = _api()
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=api_category,
        command=ProxmoxCommand.UNLOCK,
        node="pve",
        vm_id=100,
    )

    proxmox.request.assert_awaited_once_with(
        "PUT", expected_path, json_data=expected_body
    )


async def test_post_api_command_start_uses_post(hass: HomeAssistant) -> None:
    """Test a regular command uses POST on the status endpoint."""
    proxmox = _api()
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    result = await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=ProxmoxType.LXC,
        command=ProxmoxCommand.START,
        node="pve",
        vm_id=100,
    )

    assert result == UPID
    proxmox.request.assert_awaited_once_with(
        "POST", "nodes/pve/lxc/100/status/start", json_data=None
    )


@pytest.mark.parametrize(
    "command",
    [
        ProxmoxCommand.START_ALL,
        ProxmoxCommand.STOP_ALL,
        ProxmoxCommand.SUSPEND_ALL,
        ProxmoxCommand.WAKEONLAN,
    ],
)
async def test_post_api_command_node_bulk_actions(
    hass: HomeAssistant, command: ProxmoxCommand
) -> None:
    """Test the bulk node actions post to their own endpoint, not status."""
    proxmox = _api()
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=ProxmoxType.Node,
        command=command,
        node="pve",
    )

    proxmox.request.assert_awaited_once_with(
        "POST", f"nodes/pve/{command}", json_data=None
    )


@pytest.mark.parametrize(
    ("command", "expected_path", "expected_body"),
    [
        # A node is rebooted or shut down through its status endpoint, with
        # the command as a parameter - there is no `nodes/{node}/reboot`.
        (ProxmoxCommand.REBOOT, "nodes/pve/status", {"command": "reboot"}),
        (ProxmoxCommand.SHUTDOWN, "nodes/pve/status", {"command": "shutdown"}),
    ],
)
async def test_post_api_command_node_power(
    hass: HomeAssistant,
    command: ProxmoxCommand,
    expected_path: str,
    expected_body: dict,
) -> None:
    """Test node reboot and shutdown carry the command in the body."""
    proxmox = _api()
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=ProxmoxType.Node,
        command=command,
        node="pve",
    )

    proxmox.request.assert_awaited_once_with(
        "POST", expected_path, json_data=expected_body
    )


async def test_post_api_command_hibernate_and_disarm(hass: HomeAssistant) -> None:
    """Test the commands that need a parameter next to their path."""
    proxmox = _api()
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=ProxmoxType.QEMU,
        command=ProxmoxCommand.HIBERNATE,
        node="pve",
        vm_id=100,
    )
    proxmox.request.assert_awaited_with(
        "POST", "nodes/pve/qemu/100/status/suspend", json_data={"todisk": 1}
    )

    await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=ProxmoxType.Proxmox,
        command=ProxmoxCommand.DISARM_HA,
        node="pve",
    )
    proxmox.request.assert_awaited_with(
        "POST", "cluster/ha/status/disarm-ha", json_data={"resource-mode": "freeze"}
    )


def test_snapshot_name_is_one_the_api_accepts() -> None:
    """Test the generated name fits Proxmox's `pve-configid` rules."""
    name = snapshot_name()

    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]+", name)
    assert len(name) <= SNAPSHOT_NAME_MAX_LENGTH
    assert name.startswith("homeassistant_")


@pytest.mark.parametrize("api_category", [ProxmoxType.QEMU, ProxmoxType.LXC])
async def test_post_api_command_snapshot(
    hass: HomeAssistant, api_category: ProxmoxType
) -> None:
    """Test a snapshot posts to the snapshot endpoint with a name."""
    proxmox = _api()
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    await post_api_command(
        entity,
        proxmox_client=_client_with(proxmox),
        api_category=api_category,
        command=ProxmoxCommand.SNAPSHOT,
        node="pve",
        vm_id=100,
    )

    proxmox.request.assert_awaited_once()
    method, path = proxmox.request.await_args.args
    body = proxmox.request.await_args.kwargs["json_data"]
    assert method == "POST"
    assert path == f"nodes/pve/{api_category}/100/snapshot"
    assert body["snapname"].startswith("homeassistant_")
    assert body["description"] == "Created by Home Assistant"
    # Disks only: no RAM state, which would make the snapshot slow and big.
    assert "vmstate" not in body


async def test_post_api_command_surfaces_non_403_error(hass: HomeAssistant) -> None:
    """Test a non-403 API error is raised instead of being swallowed."""
    proxmox = _api(api_error(500, "Internal Server Error", "CT is locked (fstrim)"))
    entity = SimpleNamespace(hass=hass, config_entry=mock_config_entry)

    with pytest.raises(HomeAssistantError, match="CT is locked"):
        await post_api_command(
            entity,
            proxmox_client=_client_with(proxmox),
            api_category=ProxmoxType.LXC,
            command=ProxmoxCommand.UNLOCK,
            node="pve",
            vm_id=100,
        )


async def test_post_api_command_refused_raises_a_repair(hass: HomeAssistant) -> None:
    """Test a 403 names the missing privilege in a repair and still fails the press."""
    proxmox = _api(
        api_error(
            403,
            "Forbidden",
            "Permission check failed (/vms/100, VM.PowerMgmt)",
        )
    )
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT_OK)
    entry.add_to_hass(hass)
    entity = SimpleNamespace(hass=hass, config_entry=entry)

    with pytest.raises(HomeAssistantError):
        await post_api_command(
            entity,
            proxmox_client=_client_with(proxmox),
            api_category=ProxmoxType.LXC,
            command=ProxmoxCommand.START,
            node="pve",
            vm_id=100,
        )

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{entry.entry_id}_100_command_forbiden"
    )
    assert issue is not None
    assert issue.translation_placeholders["resource"] == "LXC 100"
    assert (
        issue.translation_placeholders["permission"]
        == "['perm','/vms/100',['VM.PowerMgmt']]"
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Permission check failed (/nodes/pve, Sys.PowerMgmt)",
            "['perm','/nodes/pve',['Sys.PowerMgmt']]",
        ),
        (
            "Permission check failed (/vms/100, VM.Snapshot)",
            "['perm','/vms/100',['VM.Snapshot']]",
        ),
        ("Forbidden", None),
    ],
)
def test_permission_check_from(message: str, expected: str | None) -> None:
    """Test the privilege is read out of Proxmox's reason phrase."""
    assert permission_check_from(api_error(403, "Forbidden", message)) == expected


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        # What the README asks for.
        ("homeassistant", "homeassistant"),
        # What the Proxmox web interface shows, and what people copy.
        ("homeassistant@pve!homeassistant", "homeassistant"),
        ("root@pam!ha-token", "ha-token"),
        ("  homeassistant@pve!homeassistant  ", "homeassistant"),
        ("", ""),
        (None, ""),
    ],
)
def test_token_name_only(typed: str | None, expected: str) -> None:
    """
    Test the token field accepts the token's full id as well as its name.

    Proxmox displays a token as `user@realm!name`; entering that verbatim
    made the login fail with "no such user ('user@realm!user@realm')".
    """
    assert token_name_only(typed) == expected


async def test_client_logs_in_with_the_bare_token_name(hass: HomeAssistant) -> None:
    """Test a full token id in the entry still builds a working client."""
    client = ProxmoxClient(
        hass,
        host="node.example.invalid",
        user="homeassistant",
        password="secret",  # noqa: S106 - invented
        token_name="homeassistant@pve!homeassistant",  # noqa: S106 - not a secret
        realm="pve",
        verify_ssl=False,
    )

    with patch(
        "aioproxmox.ProxmoxVE._request_once", new=AsyncMock(return_value={})
    ) as request:
        await client.build_client()

    # A token is never logged in; `version` stands in for the login.
    request.assert_awaited_once_with("GET", "version", None, None)
    auth = client.get_api_client().auth
    assert auth.token_name == "homeassistant"
    assert auth.username == "homeassistant@pve"
    assert client.uses_token


async def test_a_refused_token_is_an_authentication_error(
    hass: HomeAssistant,
) -> None:
    """Test the 401 a wrong token gets is reported like a wrong password."""
    client = ProxmoxClient(
        hass,
        host="node.example.invalid",
        user="homeassistant",
        password="secret",  # noqa: S106 - invented
        token_name="homeassistant",  # noqa: S106 - not a secret
        realm="pve",
        verify_ssl=False,
    )

    with (
        patch(
            "aioproxmox.ProxmoxVE._request_once",
            new=AsyncMock(side_effect=api_error(401, "Unauthorized", "")),
        ),
        pytest.raises(ProxmoxAuthError) as refused,
    ):
        await client.build_client()

    assert auth_error_status(refused.value) == 401


async def test_a_password_client_logs_in_once(hass: HomeAssistant) -> None:
    """Test building a password client is the login, with user and realm joined."""
    client = ProxmoxClient(
        hass,
        host="node.example.invalid",
        user="homeassistant",
        password="secret",  # noqa: S106 - invented
        realm="pve",
        verify_ssl=False,
    )

    with patch("aioproxmox.ProxmoxHTTPAuth._get_new_tokens", new=AsyncMock()) as login:
        await client.build_client()

    login.assert_awaited_once()
    auth = client.get_api_client().auth
    assert auth.username == "homeassistant@pve"
    assert auth.password == "secret"
    assert not client.uses_token
    assert client.host == "node.example.invalid"
    assert client.hosts == ("node.example.invalid",)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ProxmoxAuthError("Couldn't authenticate user x@pve to h: Code 401"), 401),
        (ProxmoxAuthError("Couldn't authenticate user x@pve to h: Code 595"), 595),
        (ProxmoxAuthError("something else entirely"), None),
        (ProxmoxAPIError(401, "Unauthorized", "version"), 401),
    ],
)
def test_auth_error_status(error: Exception, status: int | None) -> None:
    """Test the HTTP status is read out of the error when present."""
    assert auth_error_status(error) == status


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ProxmoxAuthError("Code 401"), True),
        (ProxmoxAPIError(401, "Unauthorized", "version"), True),
        (ProxmoxAPIError(403, "Forbidden", "version"), False),
        (TimeoutError(), False),
    ],
)
def test_is_auth_error(error: Exception, expected: bool) -> None:  # noqa: FBT001
    """Test only a refused login or a 401 count as the credentials' fault."""
    assert is_auth_error(error) is expected
