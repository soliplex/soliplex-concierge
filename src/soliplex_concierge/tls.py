"""Choose the TLS trust store backing the extension's httpx clients.

By default httpx verifies against certifi's bundled CA set
('load_verify_locations(cafile=certifi.where())'), which deliberately ignores
the OS trust store and 'SSL_CERT_FILE' / 'SSL_CERT_DIR'. When the optional
'truststore' package is installed, this module hands httpx an
OS-trust-store-backed SSLContext instead, so an operator who installs an
enterprise CA root the idiomatic way (e.g. 'update-ca-certificates') is trusted
without touching the venv's certifi bundle. See issue #46.
"""

import ssl


def httpx_verify():
    """Return an httpx 'verify' value honoring the OS trust store if possible.

    With 'truststore' installed, returns an OS-trust-store-backed SSLContext;
    otherwise returns True (httpx's certifi default).
    """
    try:
        import truststore
    except ImportError:
        return True
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
