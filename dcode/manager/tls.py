"""One TLS context for every outbound HTTPS call (model servers, OpenRouter, metrics).

A PyInstaller binary carries its own OpenSSL, which looks for CA certificates where the build
machine keeps them, so on another distribution it trusts nothing (every HTTPS model server failed
with CERTIFICATE_VERIFY_FAILED). The host's own bundle is loaded too: that is also where a company
CA for internal model servers is installed. SSL_CERT_FILE (or DCODE_MANAGER_CA_BUNDLE) adds one.
"""

from __future__ import annotations

import os
import ssl
import threading

SYSTEM_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",                 # Debian, Ubuntu, Alpine
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  # RHEL, AlmaLinux, Fedora
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",                             # SUSE
    "/etc/ssl/cert.pem",                                  # macOS, BSD
)
_context: ssl.SSLContext | None = None
_lock = threading.Lock()


def context() -> ssl.SSLContext:
    global _context
    with _lock:
        if _context is None:
            _context = _build()
        return _context


def _build() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    loaded = False
    for path in (os.environ.get("DCODE_MANAGER_CA_BUNDLE"), os.environ.get("SSL_CERT_FILE"), *SYSTEM_BUNDLES):
        if path and os.path.isfile(path):
            try:
                ctx.load_verify_locations(path)
                loaded = True
            except (ssl.SSLError, OSError):
                continue
            if path in SYSTEM_BUNDLES:
                break  # one system bundle is the whole store
    if not loaded:
        try:
            import certifi  # bundled with the client libraries; last resort

            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - the default paths may still work
            pass
    return ctx
