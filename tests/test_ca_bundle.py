# Copyright (c) 2019-2026
# SPDX-License-Identifier: MIT
"""Tests for verifying a cluster's own CA: the system store and a named bundle."""

from __future__ import annotations

import datetime as dt
import ssl
from typing import TYPE_CHECKING

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.proxmoxve import DOMAIN
from custom_components.proxmoxve.api import (
    CABundleError,
    ProxmoxClient,
    build_verifying_context,
)
from custom_components.proxmoxve.const import CONF_CA_BUNDLE

from .const import (
    CURRENT_ENTRY_DATA,
    CURRENT_ENTRY_VERSION,
    USER_INPUT_SELECTION,
    USER_INPUT_USER_HOST,
)

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.core import HomeAssistant

    from .fake_api import FakeProxmox

CA_NAME = "Proxmox Virtual Environment Test CA"


def _write_ca(path: Path) -> Path:
    """Write a self-signed CA certificate, the way a PVE cluster has one."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = path / "pve-root-ca.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return pem


def _trusted_names(context: ssl.SSLContext) -> set[str]:
    """Return the common names of the CAs a context trusts."""
    names = set()
    for cert in context.get_ca_certs():
        for part in cert.get("subject", ()):
            for key, value in part:
                if key == "commonName":
                    names.add(value)
    return names


def test_the_context_verifies_and_takes_the_bundle_on_top(tmp_path: Path) -> None:
    """Test the named CA is trusted in addition to the public list, not instead."""
    pem = _write_ca(tmp_path)

    plain = build_verifying_context()
    with_ca = build_verifying_context(str(pem))

    assert plain.verify_mode is ssl.CERT_REQUIRED
    assert with_ca.verify_mode is ssl.CERT_REQUIRED
    assert CA_NAME not in _trusted_names(plain)
    assert CA_NAME in _trusted_names(with_ca)
    # Everything the plain context trusted is still there.
    assert _trusted_names(plain) <= _trusted_names(with_ca)


def test_a_bundle_that_cannot_be_read_is_reported(tmp_path: Path) -> None:
    """Test a missing file and a file that is not PEM both name the path."""
    with pytest.raises(CABundleError, match="does-not-exist"):
        build_verifying_context(str(tmp_path / "does-not-exist.pem"))

    not_pem = tmp_path / "notes.txt"
    not_pem.write_text("this is not a certificate")
    with pytest.raises(CABundleError, match=r"notes\.txt"):
        build_verifying_context(str(not_pem))


async def test_the_client_hands_the_context_to_the_library(
    hass: HomeAssistant, fake_api: FakeProxmox, tmp_path: Path
) -> None:
    """Test a verifying client builds a context; one that does not verify passes False."""
    pem = _write_ca(tmp_path)

    verifying = ProxmoxClient(
        hass,
        host="192.168.10.101",
        user="root",
        password="secret",  # noqa: S106 - invented
        realm="pam",
        verify_ssl=True,
        ca_bundle=str(pem),
    )
    await verifying.build_client()
    context = verifying.get_api_client().verify_ssl
    assert isinstance(context, ssl.SSLContext)
    assert CA_NAME in _trusted_names(context)

    trusting = ProxmoxClient(
        hass,
        host="192.168.10.101",
        user="root",
        password="secret",  # noqa: S106 - invented
        realm="pam",
        verify_ssl=False,
        ca_bundle=str(pem),
    )
    await trusting.build_client()
    assert trusting.get_api_client().verify_ssl is False


async def test_the_setup_form_refuses_a_bad_bundle_and_keeps_a_good_one(
    hass: HomeAssistant, fake_api: FakeProxmox, tmp_path: Path
) -> None:
    """Test the path is checked on the form, and stored with the entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={**USER_INPUT_USER_HOST, CONF_CA_BUNDLE: "/config/missing.pem"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "host"
    assert result["errors"] == {CONF_CA_BUNDLE: "ca_bundle_invalid"}

    pem = _write_ca(tmp_path)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={**USER_INPUT_USER_HOST, CONF_CA_BUNDLE: f" {pem} "},
    )
    assert result["step_id"] == "expose"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=USER_INPUT_SELECTION
    )

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CA_BUNDLE] == str(pem)


async def test_setup_with_a_bundle_that_is_gone_is_an_error_not_a_retry(
    hass: HomeAssistant, fake_api: FakeProxmox
) -> None:
    """Test a path that cannot be read stops setup with a message, no retry loop."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test",
        data={**CURRENT_ENTRY_DATA, CONF_CA_BUNDLE: "/config/gone.pem"},
        options={},
        version=CURRENT_ENTRY_VERSION,
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "gone.pem" in str(entry.reason)


async def test_an_entry_without_the_field_sets_up_as_before(
    hass: HomeAssistant, fake_api: FakeProxmox, current_entry: MockConfigEntry
) -> None:
    """Test entries from before the option carry on; verification is on, no bundle."""
    await hass.config_entries.async_setup(current_entry.entry_id)
    await hass.async_block_till_done()

    assert current_entry.state is ConfigEntryState.LOADED
    assert CONF_CA_BUNDLE not in current_entry.data
