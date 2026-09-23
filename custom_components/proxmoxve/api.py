# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Handle API for Proxmox VE."""

import re
import ssl
from collections.abc import Iterable
from contextlib import suppress
from typing import Any

import aiohttp
import homeassistant.util.dt as dt_util
from aioproxmox import ProxmoxVE
from aioproxmox.exceptions import ProxmoxAPIError, ProxmoxAuthError
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.ssl import create_client_context

from .const import (
    CONF_BACKUP_STORAGE,
    CONF_HA_ADMIN_USERNAME,
    DEFAULT_PORT,
    DEFAULT_REALM,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    LOGGER,
    ProxmoxCommand,
    ProxmoxType,
)

AUTH_STATUS_PATTERN = re.compile(r"Code (\d+)")
API_TIMEOUT = 30

# What a request raises when the host is not there, does not answer in
# time, or closes the connection - everything that says nothing about the
# credentials or the request itself. aiohttp wraps the socket errors, and
# `asyncio.TimeoutError` is `TimeoutError` since Python 3.11.
CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientError,
    TimeoutError,
)
# The connection failed because the certificate was not accepted; a subset
# of the above, so it has to be caught first where it matters.
SSL_ERRORS: tuple[type[BaseException], ...] = (aiohttp.ClientSSLError,)
# Anything a request may raise - for the callers that only want to know
# that it did not work.
REQUEST_ERRORS: tuple[type[BaseException], ...] = (
    *CONNECTION_ERRORS,
    ProxmoxAuthError,
    ProxmoxAPIError,
)


class CABundleError(Exception):
    """The CA bundle named in the configuration could not be loaded."""


def build_verifying_context(ca_bundle: str = "") -> ssl.SSLContext:
    """
    Return the SSL context a verifying client checks certificates against.

    Home Assistant's own client context trusts the public list (certifi).
    A Proxmox cluster signs its nodes' certificates with its own CA, so
    that alone refuses every cluster that has verification on - even one
    whose CA is installed in the operating system's store, which the
    Additional CA integration fills. That store is added here, and on top
    of it the bundle named in the configuration, if any. Nothing is taken
    away from the public list. Reads files, so it belongs in the executor.

    The root CA Proxmox generates for a cluster carries no keyUsage
    extension, and Python 3.13 turned on VERIFY_X509_STRICT, whose RFC
    5280 profile checks refuse exactly that - so the cluster's own CA was
    rejected however it was made known. Strict mode is switched off for
    this context only; the chain and the hostname are still verified.
    """
    context = create_client_context()
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    with suppress(ssl.SSLError, OSError):
        context.load_default_certs()
    if ca_bundle:
        try:
            context.load_verify_locations(cafile=ca_bundle)
        except (OSError, ssl.SSLError) as error:
            msg = f"CA bundle {ca_bundle} could not be loaded: {error}"
            raise CABundleError(msg) from error
    return context


def auth_error_status(error: BaseException) -> int | None:
    """
    Return the HTTP status behind an authentication error, if it says.

    Logging in fails with the same exception for a wrong password (401) and
    for a host whose API is up but not ready to issue tickets yet (500,
    595, ...). Only the first is a reason to ask for new credentials.
    """
    if isinstance(error, ProxmoxAPIError):
        return error.status
    match = AUTH_STATUS_PATTERN.search(str(error))
    return int(match.group(1)) if match else None


def is_auth_error(error: BaseException) -> bool:
    """
    Return whether `error` says the credentials were refused.

    A password login answers with ProxmoxAuthError. A token is never
    logged in - the first request made with it is refused with 401, and
    that arrives as an API error like any other.
    """
    if isinstance(error, ProxmoxAuthError):
        return True
    return isinstance(error, ProxmoxAPIError) and error.status == 401


def token_name_only(value: str | None) -> str:
    """
    Reduce whatever was typed into the token field to the token's name.

    Proxmox shows a token as `user@realm!name`, and that is what people copy
    into the field - the login then fails with "no such user
    ('user@realm!user@realm')", because the user and realm get prepended a
    second time. A token name itself can never contain `!`, so everything up
    to the last one is not part of it.
    """
    if not value:
        return ""
    return value.strip().rsplit("!", 1)[-1].strip()


class ProxmoxClient:
    """
    The credentials of one entry, and the API object built with them.

    The API object is aioproxmox's `ProxmoxVE`. It keeps the password, so
    a ticket that was refused after the host was away is renewed with a
    fresh login rather than with the dead ticket; and it knows the other
    nodes of the cluster once told, so a configured host that stops
    answering is left for one of them. Both happen inside the object, and
    every coordinator holds the same object - nothing has to be told.
    """

    _proxmox: ProxmoxVE

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        host: str,
        user: str,
        password: str,
        token_name: str = "",
        port: int | None = DEFAULT_PORT,
        realm: str | None = DEFAULT_REALM,
        verify_ssl: bool | None = DEFAULT_VERIFY_SSL,
        ca_bundle: str | None = "",
        fallback_hosts: Iterable[str] = (),
    ) -> None:
        """Initialize the ProxmoxClient."""
        self._hass = hass
        self._host = host
        self._port = port
        self._user = user
        self._token_name = token_name
        self._realm = realm
        self._password = password
        self._verify_ssl = verify_ssl
        self._ca_bundle = (ca_bundle or "").strip()
        # What the cluster said the last time it could be asked. Without
        # them the first request of a setup can only go to the configured
        # host, and an entry whose host is down would never load again.
        self._fallback_hosts = [
            host for host in fallback_hosts if isinstance(host, str) and host
        ]

    @property
    def host(self) -> str:
        """Return the host currently in use."""
        try:
            return self._proxmox.host
        except AttributeError:
            return self._host

    @property
    def hosts(self) -> tuple[str, ...]:
        """Return every host this client may use, the configured one first."""
        try:
            return self._proxmox.hosts
        except AttributeError:
            return (self._host, *self._fallback_hosts)

    @property
    def uses_token(self) -> bool:
        """Return whether the credentials are an API token rather than a password."""
        return bool(token_name_only(self._token_name))

    async def build_client(self) -> None:
        """
        Construct the API object and prove the credentials against the host.

        A password is logged in; a token is not, so the one read every
        credential may make - `version` - stands in for the login, and a
        refused token is reported here rather than by whatever is read
        first. Raises ProxmoxAuthError for refused credentials, the aiohttp
        errors for a host that is not there, ProxmoxAPIError for anything
        else the API answered, and CABundleError for a bundle path that
        cannot be read.

        Where the cluster's other nodes are known from an earlier setup,
        a configured host that does not answer at all is left for one of
        them right here: the login is what a restart runs into first, and
        without this the entry could not load while that node is down.
        """
        verify_ssl: bool | ssl.SSLContext = bool(self._verify_ssl)
        session = async_get_clientsession(self._hass, verify_ssl=bool(verify_ssl))
        if verify_ssl or self._ca_bundle:
            # A bundle is read even when it will not be used, so a wrong
            # path is caught on the form whichever way the switch stands.
            context = await self._hass.async_add_executor_job(
                build_verifying_context, self._ca_bundle
            )
            if verify_ssl:
                verify_ssl = context
        user_id = self._user_id()

        if token_name := token_name_only(self._token_name):
            proxmox = ProxmoxVE(
                session,
                self._host,
                port=self._port,
                user=user_id,
                token_name=token_name,
                token_value=self._password,
                verify_ssl=verify_ssl,
                timeout=API_TIMEOUT,
            )
            proxmox.learn_hosts(list(self._fallback_hosts))
            try:
                # `request` moves to another node itself when the current
                # one does not answer and others are known.
                await proxmox.request("GET", "version")
            except ProxmoxAPIError as error:
                if error.status == 401:
                    msg = f"Token {user_id}!{token_name} was refused: Code 401"
                    raise ProxmoxAuthError(msg) from error
                raise
        else:
            proxmox = ProxmoxVE(
                session,
                self._host,
                port=self._port,
                user=user_id,
                password=self._password,
                verify_ssl=verify_ssl,
                timeout=API_TIMEOUT,
            )
            proxmox.learn_hosts(list(self._fallback_hosts))
            try:
                await proxmox.auth.async_init()  # type: ignore[attr-defined]
            except CONNECTION_ERRORS:
                # The login goes straight out, with no failover of its
                # own. A node that answers `version` is one to log in to;
                # that the credentials are then refused is the login's
                # answer, and the same answer on every node.
                if not await proxmox.failover():
                    raise
                await proxmox.auth.async_init()  # type: ignore[attr-defined]

        self._proxmox = proxmox

    def get_api_client(self) -> ProxmoxVE:
        """Return the API object."""
        return self._proxmox

    def learn_hosts(self, hosts: Iterable[str]) -> None:
        """
        Remember the other nodes of the cluster as places to fall back to.

        `cluster/status` says what address every node answers on. That is
        the corosync address, which on a cluster with a separate cluster
        network is not reachable from Home Assistant at all - so these are
        tried, not relied on. The configured host stays first.
        """
        self._proxmox.learn_hosts([host for host in hosts if isinstance(host, str)])

    def _user_id(self) -> str:
        """Return the user with its realm, as Proxmox wants it."""
        return self._user if "@" in self._user else f"{self._user}@{self._realm}"


async def get_api(
    proxmox: ProxmoxVE,
    api_path: str,
) -> Any:
    """Return data from the Proxmox API."""
    api_result = await proxmox.request("GET", api_path)
    LOGGER.debug("API GET Response - %s: %s", api_path, api_result)
    return api_result


async def post_api(
    proxmox: ProxmoxVE,
    api_path: str,
    **kwargs: Any,
) -> Any:
    """Post data to Proxmox API."""
    api_result = await proxmox.request("POST", api_path, json_data=kwargs or None)
    LOGGER.debug("API POST - %s %s: %s", api_path, kwargs or "", api_result)
    return api_result


# Proxmox accepts a snapshot name matching `[a-zA-Z][a-zA-Z0-9_-]+`, at most
# 40 characters. This prefix plus a local timestamp comes to 29, and a local
# timestamp is what reads naturally next to the snapshot list in the web
# interface, which shows the creation time in local time as well.
SNAPSHOT_NAME_PREFIX = "homeassistant_"
SNAPSHOT_NAME_MAX_LENGTH = 40


def snapshot_name() -> str:
    """Return a snapshot name for right now that the API accepts."""
    name = f"{SNAPSHOT_NAME_PREFIX}{dt_util.now().strftime('%Y%m%d_%H%M%S')}"
    return name[:SNAPSHOT_NAME_MAX_LENGTH]


async def put_api(
    proxmox: ProxmoxVE,
    api_path: str,
    **kwargs: Any,
) -> Any:
    """Put data to Proxmox API."""
    api_result = await proxmox.request("PUT", api_path, json_data=kwargs or None)
    LOGGER.debug("API PUT - %s: %s", api_path, api_result)
    return api_result


def permission_check_from(error: ProxmoxAPIError) -> str | None:
    """
    Return the permission check a 403 names, in the form the docs use.

    Proxmox answers a refused command with "Permission check failed
    (/nodes/pve, Sys.PowerMgmt)"; the repair shows it as
    `['perm','/nodes/pve',['Sys.PowerMgmt']]`, which is what the ACL
    section of the documentation lists.
    """
    match = re.search(r"\(([^,()]+),\s*([^()]+)\)", str(error))
    if match is None:
        return None
    path = match.group(1).strip()
    # Proxmox lists alternatives as `VM.PowerMgmt|VM.Audit`.
    privileges = [name.strip() for name in match.group(2).split("|") if name.strip()]
    quoted = ",".join(f"'{name}'" for name in privileges)
    return f"['perm','{path}',[{quoted}]]"


async def post_api_command(
    self,
    *,
    proxmox_client: ProxmoxClient,
    api_category: ProxmoxType,
    command: str,
    node: str,
    vm_id: int | None = None,
) -> Any:
    """Make proper api post status calls to set state."""
    result = None

    proxmox = proxmox_client.get_api_client()

    if command not in ProxmoxCommand:
        msg = "Invalid Command"
        raise ValueError(msg)

    if api_category is ProxmoxType.Proxmox:
        issue_id = f"{self.config_entry.entry_id}_cluster_command_forbiden"
    elif api_category is ProxmoxType.Node:
        issue_id = f"{self.config_entry.entry_id}_{node}_command_forbiden"
    elif api_category in (ProxmoxType.QEMU, ProxmoxType.LXC):
        issue_id = f"{self.config_entry.entry_id}_{vm_id}_command_forbiden"

    if api_category is ProxmoxType.Proxmox:
        resource = "Cluster HA"
    elif api_category is ProxmoxType.Node:
        resource = f"{api_category.capitalize()} {node}"
    else:
        resource = f"{api_category.upper()} {vm_id}"

    try:
        if api_category is ProxmoxType.Proxmox:
            # Cluster-wide HA arm/disarm; not tied to a node or guest.
            # Mounted under PVE::API2::HA::Status, i.e. cluster/ha/status/...,
            # not directly under cluster/ha/.
            # disarm-ha requires resource-mode (freeze|ignore); default to
            # the safer "freeze" (HA services are locked in their current
            # state, no automatic action) rather than "ignore" (HA tracking
            # is fully suspended, allowing manual guest management during
            # the disarmed window).
            if command == ProxmoxCommand.DISARM_HA:
                result = await post_api(
                    proxmox,
                    f"cluster/ha/status/{command}",
                    **{"resource-mode": "freeze"},
                )
            else:
                result = await post_api(proxmox, f"cluster/ha/status/{command}")
        # START_ALL, STOP_ALL, SUSPEND_ALL, WAKEONLAN are not part of status API
        elif command in (ProxmoxCommand.BACKUP, ProxmoxCommand.BACKUP_ALL):
            # One vzdump run, in snapshot mode, to the storage picked in the
            # options; the button exists only while one is picked. The task
            # shows up where the backup sensors already look.
            storage = self.config_entry.options.get(CONF_BACKUP_STORAGE)
            target = (
                {"all": 1} if command == ProxmoxCommand.BACKUP_ALL else {"vmid": vm_id}
            )
            result = await post_api(
                proxmox,
                f"nodes/{node}/vzdump",
                mode="snapshot",
                storage=storage,
                **target,
            )
        elif api_category is ProxmoxType.Node and command in [
            ProxmoxCommand.START_ALL,
            ProxmoxCommand.STOP_ALL,
            ProxmoxCommand.SUSPEND_ALL,
            ProxmoxCommand.WAKEONLAN,
        ]:
            result = await post_api(proxmox, f"nodes/{node}/{command}")
        elif api_category is ProxmoxType.Node:
            result = await post_api(proxmox, f"nodes/{node}/status", command=command)
        elif command == ProxmoxCommand.HIBERNATE:
            result = await post_api(
                proxmox,
                f"nodes/{node}/{api_category}/{vm_id}/status/{ProxmoxCommand.SUSPEND}",
                todisk=1,
            )
        elif command == ProxmoxCommand.SNAPSHOT:
            # Not part of the status API either. Without `vmstate` a VM
            # snapshot captures the disks only, which is the quick, safe
            # default; the description says where it came from when it
            # turns up in the snapshot list months later.
            result = await post_api(
                proxmox,
                f"nodes/{node}/{api_category}/{vm_id}/snapshot",
                snapname=snapshot_name(),
                description="Created by Home Assistant",
            )
        elif command == ProxmoxCommand.UNLOCK:
            # Unlock is not part of the status API; it removes the config
            # lock (equivalent to `pct unlock` / `qm unlock`). QEMU refuses to
            # edit a locked guest's config unless skiplock=1 is passed (allowed
            # for root@pam only); the LXC config endpoint has no skiplock
            # parameter and rejects it, so it is sent for QEMU only.
            extra = {"skiplock": 1} if api_category is ProxmoxType.QEMU else {}
            result = await put_api(
                proxmox,
                f"nodes/{node}/{api_category}/{vm_id}/config",
                delete="lock",
                **extra,
            )
        else:
            result = await post_api(
                proxmox, f"nodes/{node}/{api_category}/{vm_id}/status/{command}"
            )

    except ProxmoxAPIError as error:
        if error.status == 403:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="resource_command_forbiden",
                translation_placeholders={
                    "resource": resource,
                    "user": (
                        self.config_entry.data.get(CONF_HA_ADMIN_USERNAME)
                        if api_category is ProxmoxType.Proxmox
                        else self.config_entry.data[CONF_USERNAME]
                    ),
                    "permission": permission_check_from(error) or str(error),
                    "command": command,
                },
            )
        # Surface every API error (e.g. a still-present lock, or skiplock
        # being rejected for a non-root user) instead of silently swallowing
        # non-403 responses, which made failed commands look successful.
        msg = f"Proxmox {resource} {command} error - {error}"
        raise HomeAssistantError(
            msg,
        ) from error

    except (*CONNECTION_ERRORS, ProxmoxAuthError) as error:
        msg = f"Proxmox {resource} {command} error - {error}"
        raise HomeAssistantError(
            msg,
        ) from error

    ir.async_delete_issue(
        self.hass,
        DOMAIN,
        issue_id,
    )

    return result
