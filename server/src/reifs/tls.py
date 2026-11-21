"""The server's self-signed TLS certificate.

Clients pin the certificate's SHA-256 fingerprint the first time they connect
(trust on first use, like SSH), so no CA is involved and hostnames do not
matter. The certificate is created on first ``reifs serve`` with the
``cryptography`` package, or the ``openssl`` command if that is missing
(handy on Termux).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import ssl
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


class TLSError(Exception):
    pass


def paths(home: Path) -> tuple[Path, Path]:
    d = Path(home) / "tls"
    return d / "cert.pem", d / "key.pem"


def ensure_certificate(home: Path, server_name: str) -> tuple[Path, Path]:
    cert, key = paths(home)
    if cert.exists() and key.exists():
        return cert, key
    cert.parent.mkdir(parents=True, exist_ok=True)
    try:
        _generate_with_cryptography(cert, key, server_name)
    except ImportError:
        _generate_with_openssl(cert, key, server_name)
    try:
        os.chmod(key, 0o600)
    except OSError:
        pass
    return cert, key


def _generate_with_cryptography(cert: Path, key: Path, server_name: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"rei-fs {server_name}"[:64])])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 50))
        .sign(private, hashes.SHA256())
    )
    key.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))


def _generate_with_openssl(cert: Path, key: Path, server_name: str) -> None:
    exe = shutil.which("openssl")
    if exe is None:
        raise TLSError("cannot create a TLS certificate: install the 'cryptography' "
                       "Python package or the openssl command")
    cn = f"rei-fs {server_name}"[:64].replace("/", "_")
    result = subprocess.run(
        [exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-keyout", str(key), "-out", str(cert), "-days", str(365 * 50),
         "-subj", f"/CN={cn}"],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise TLSError(f"openssl failed: {result.stderr.strip()}")


def fingerprint(cert: Path) -> str:
    """SHA-256 of the DER certificate, as colon-separated upper-case hex."""
    der = ssl.PEM_cert_to_DER_cert(Path(cert).read_text(encoding="ascii"))
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def server_context(cert: Path, key: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx
