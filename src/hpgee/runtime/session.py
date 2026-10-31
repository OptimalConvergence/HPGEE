"""Explicit credentials and bounded initialization for acquisition clients."""

import json
import os
from pathlib import Path
from .admission import Admission, LimitedTransport
from .bounded import BoundedTransport
from .retry import retry_read


def load_credentials(path):
    path = Path(path).expanduser()
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("Credential file must be owned by this user and private")
    from google.oauth2.credentials import Credentials
    from google.oauth2.service_account import Credentials as ServiceCredentials
    import ee.oauth

    data = json.loads(path.read_text())
    if data.get("type") == "service_account":
        return ServiceCredentials.from_service_account_info(data, scopes=ee.oauth.SCOPES)
    return Credentials(
        None,
        refresh_token=data["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=data.get("client_id", ee.oauth.CLIENT_ID),
        client_secret=data.get("client_secret", ee.oauth.CLIENT_SECRET),
        scopes=data.get("scopes", ee.oauth.SCOPES),
    )


def initialize(
    project,
    endpoint,
    credentials,
    *,
    scratch,
    route_pool=None,
    http_limit=40,
    timeout=60,
    observer=None,
    transport_factory=None,
):
    import ee
    from google_auth_httplib2 import Request

    if endpoint not in ("standard", "high-volume"):
        raise ValueError("Unknown EE endpoint")
    transport = (transport_factory or BoundedTransport)(
        pool_size=http_limit,
        timeout=timeout,
        observer=observer,
        scratch=scratch,
        route_pool=route_pool,
    )
    limited = LimitedTransport(transport, Admission(http_limit))
    if not credentials.valid:
        retry_read(
            lambda: credentials.refresh(Request(limited)), attempts=2, base_seconds=1, cap_seconds=1
        )
    ee.data.setMaxRetries(0)
    original = ee.deprecation._FetchDataCatalogStac
    ee.deprecation._FetchDataCatalogStac = lambda: json.loads(
        limited.request(ee.deprecation._DEPRECATED_ASSETS_URL)[1]
    )
    try:
        retry_read(
            lambda: ee.Initialize(
                project=project,
                credentials=credentials,
                http_transport=limited,
                url="https://earthengine-highvolume.googleapis.com"
                if endpoint == "high-volume"
                else "https://earthengine.googleapis.com",
            ),
            attempts=2,
            base_seconds=1,
            cap_seconds=1,
        )
    finally:
        ee.deprecation._FetchDataCatalogStac = original
    transport.adaptive = False
    return ee, limited


def monitoring_client(credentials, transport):
    from google_auth_httplib2 import AuthorizedHttp
    from requests import Response
    from urllib.parse import urlencode
    from hpgee.accounting.metrics import MonitoringRESTClient

    class Session:
        def __init__(self):
            self.http = AuthorizedHttp(credentials, http=transport)

        def get(self, url, *, params, timeout):
            response, body = self.http.request(url + "?" + urlencode(params), method="GET")
            result = Response()
            result.status_code = response.status
            result._content = body
            result.headers.update(dict(response))
            result.url = url
            return result

    return MonitoringRESTClient(session=Session())
