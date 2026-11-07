import ssl

import certifi
import httpx
import pytest

from jobcockpit.core.tls import install_windows_certifi_workaround


@pytest.fixture
def original_loader(monkeypatch):
    loader = ssl.SSLContext.load_verify_locations
    loader = getattr(loader, "__wrapped__", loader)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", loader)
    monkeypatch.setattr("jobcockpit.core.tls.sys.platform", "win32")
    return loader


def test_certifi_roots_and_verification_are_preserved(original_loader):
    expected = ssl.create_default_context(cafile=certifi.where())
    assert install_windows_certifi_workaround()
    loader = ssl.SSLContext.load_verify_locations
    assert install_windows_certifi_workaround()
    assert ssl.SSLContext.load_verify_locations is loader
    actual = httpx.create_ssl_context(trust_env=False)
    assert actual.get_ca_certs(binary_form=True) == expected.get_ca_certs(binary_form=True)
    assert actual.verify_mode == ssl.CERT_REQUIRED
    assert actual.check_hostname is True


def test_native_loader_receives_memory_for_both_http_libraries(original_loader, monkeypatch):
    calls = []

    def record(self, cafile=None, capath=None, cadata=None):
        calls.append((cafile, capath, cadata))
        return original_loader(self, cafile=cafile, capath=capath, cadata=cadata)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", record)
    install_windows_certifi_workaround()
    httpx.create_ssl_context(trust_env=False)
    # This is the context/cafile boundary urllib3.ssl_wrap_socket uses in the dump.
    from urllib3.util.ssl_ import create_urllib3_context

    context = create_urllib3_context()
    context.load_verify_locations(certifi.where())
    assert len(calls) == 2
    assert all(cafile is None and "BEGIN CERTIFICATE" in data for cafile, _, data in calls)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_custom_ca_paths_directories_and_data_are_not_replaced(original_loader, monkeypatch):
    calls = []
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations",
                        lambda self, **kwargs: calls.append(kwargs))
    install_windows_certifi_workaround()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations("custom.pem", "custom-dir", "custom-data")
    context.load_verify_locations(certifi.where(), cadata="extra-data")
    assert calls == [
        {"cafile": "custom.pem", "capath": "custom-dir", "cadata": "custom-data"},
        {"cafile": certifi.where(), "capath": None, "cadata": "extra-data"},
    ]


def test_non_windows_and_missing_bundle_do_not_silently_disable_verification(
    original_loader, monkeypatch, tmp_path,
):
    monkeypatch.setattr("jobcockpit.core.tls.sys.platform", "linux")
    assert not install_windows_certifi_workaround()
    assert ssl.SSLContext.load_verify_locations is original_loader
    monkeypatch.setattr("jobcockpit.core.tls.sys.platform", "win32")
    monkeypatch.setattr(certifi, "where", lambda: str(tmp_path / "missing.pem"))
    with pytest.raises(FileNotFoundError):
        install_windows_certifi_workaround()
    assert ssl.SSLContext.load_verify_locations is original_loader
