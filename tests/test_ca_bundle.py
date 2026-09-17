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


def _pve_root_ca() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    """
    Return a CA like the one Proxmox generates for a cluster.

    `pve-root-ca.pem` carries basic constraints, a subject key identifier
    and an authority key identifier - and no keyUsage extension, which is
    what Python's strict X.509 mode refuses.
    """
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
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _write_ca(path: Path) -> Path:
    """Write the cluster's CA certificate to a file, as someone would copy it."""
    cert, _ = _pve_root_ca()
    pem = path / "pve-root-ca.pem"
    pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return pem


def _node_certificate(
    path: Path, ca: x509.Certificate, ca_key: ec.EllipticCurvePrivateKey
) -> tuple[Path, Path]:
    """Write a node certificate for `localhost` signed by the cluster's CA."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = path / "pve-ssl.pem"
    key_path = path / "pve-ssl.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _handshake(client_context: ssl.SSLContext, cert: Path, key: Path) -> None:
    """
    Complete one TLS handshake against a server presenting `cert`, or raise.

    Done in memory: the test plugin blocks sockets, and none are needed to
    find out whether the client accepts the certificate.
    """
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert), str(key))
    to_server, to_client = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(to_client, to_server, server_hostname="localhost")
    server = server_context.wrap_bio(to_server, to_client, server_side=True)

    client_done = server_done = False
    for _ in range(20):
        if not client_done:
            try:
                client.do_handshake()
                client_done = True
            except ssl.SSLWantReadError:
                pass
        if not server_done:
            try:
                server.do_handshake()
                server_done = True
            except ssl.SSLWantReadError:
                pass
        if client_done and server_done:
            return
    msg = "handshake did not complete"
    raise AssertionError(msg)


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


def test_a_cluster_ca_without_key_usage_is_accepted(tmp_path: Path) -> None:
    """
    Test the reported problem: Proxmox's own root CA was refused as such.

    Python 3.13 turned on VERIFY_X509_STRICT, which rejects a CA without a
    keyUsage extension - and the CA Proxmox generates has none. So the
    cluster's CA was refused however it was made known. Only a real
    handshake shows the difference; the trust listing looks fine either way.
    """
    ca, ca_key = _pve_root_ca()
    pem = tmp_path / "pve-root-ca.pem"
    pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert, key = _node_certificate(tmp_path, ca, ca_key)

    context = build_verifying_context(str(pem))
    assert not context.verify_flags & ssl.VERIFY_X509_STRICT
    _handshake(context, cert, key)  # raises SSLCertVerificationError if refused

    # The chain is still verified: a context without the CA refuses the node.
    with pytest.raises(ssl.SSLCertVerificationError):
        _handshake(build_verifying_context(), cert, key)


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


async def test_a_bad_bundle_is_refused_even_with_verification_off(
    hass: HomeAssistant, fake_api: FakeProxmox
) -> None:
    """Test a wrong path is a mistake whichever way the switch stands."""
    client = ProxmoxClient(
        hass,
        host="192.168.10.101",
        user="root",
        password="secret",  # noqa: S106 - invented
        realm="pam",
        verify_ssl=False,
        ca_bundle="/config/typo.pem",
    )
    with pytest.raises(CABundleError, match=r"typo\.pem"):
        await client.build_client()


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
