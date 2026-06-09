import ssl
import sys
from unittest import mock

from soliplex_concierge import tls


def test_httpx_verify_without_truststore(monkeypatch):
    # 'import truststore' raises ImportError when the module is None.
    monkeypatch.setitem(sys.modules, "truststore", None)

    verify = tls.httpx_verify()

    assert verify is True


def test_httpx_verify_with_truststore(monkeypatch):
    fake = mock.Mock(name="truststore", spec=["SSLContext"])
    monkeypatch.setitem(sys.modules, "truststore", fake)

    verify = tls.httpx_verify()

    assert verify is fake.SSLContext.return_value
    fake.SSLContext.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
