from __future__ import annotations

from typing import Any
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httplib2
import requests
from requests.adapters import HTTPAdapter


class RequestsTransport:
    """Thread-safe-enough httplib2-compatible transport with a sized connection pool."""

    def __init__(self, *, pool_size: int, timeout: float | None = None, observer=None):
        self.observer = observer
        self.pool_size = pool_size
        self.timeout = timeout
        self.session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            pool_block=True,
            max_retries=0,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def request(
        self,
        uri: str,
        method: str = "GET",
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        redirections: int | None = None,
        connection_type: type[Any] | None = None,
    ) -> tuple[httplib2.Response, bytes]:
        del redirections, connection_type
        started = time.monotonic()
        url = urlsplit(uri)
        event = dict(attempt_id=uuid.uuid4().hex, started_at=datetime.now(timezone.utc).isoformat(),
                     host=url.hostname, path=url.path, method=method, status=None,
                     response_bytes=None, error_type=None, workload_tag=None)
        try:
            parsed = json.loads(body) if body else {}
            event['workload_tag'] = parsed.get('workloadTag')
        except (ValueError, TypeError, AttributeError):
            pass
        if self.observer is not None:
            self.observer(dict(event, phase='started'))
            event.update(response_header_seconds=None, response_body_seconds=None,
                         content_encoding=None, content_length=None, decoded_payload_bytes=None)
        response_header_at = None
        response_count = 0
        ambiguous_response_timing = False

        def response_header_hook(response, **_kwargs):
            nonlocal response_header_at, response_count, ambiguous_response_timing
            response_count += 1
            response_header_at = time.monotonic()
            if (response_count > 1 or
                    (300 <= response.status_code < 400 and response.headers.get('Location'))):
                ambiguous_response_timing = True
            event.update(status=response.status_code,
                         content_encoding=response.headers.get('Content-Encoding'),
                         content_length=response.headers.get('Content-Length'))
            return response

        try:
            request_options = ({'hooks': {'response': response_header_hook}}
                               if self.observer is not None else {})
            response = self.session.request(method, uri, data=body, headers=headers,
                                            timeout=self.timeout, **request_options)
            if getattr(response, 'history', None):
                ambiguous_response_timing = True
            response_body = response.content
            event.update(status=response.status_code, response_bytes=len(response_body),
                         decoded_payload_bytes=len(response_body))
            response_headers: dict[str, Any] = dict(response.headers)
            response_headers['status'] = response.status_code
            return httplib2.Response(response_headers), response_body
        except Exception as exc:
            event['error_type'] = type(exc).__name__
            raise
        finally:
            finished = time.monotonic()
            event['phase'] = 'finished'
            event['elapsed_seconds'] = finished-started
            if (self.observer is not None and response_header_at is not None
                    and not ambiguous_response_timing):
                event['response_header_seconds'] = max(0.0, response_header_at-started)
                event['response_body_seconds'] = max(0.0, finished-response_header_at)
            # Payload bytes after HTTP content decoding, not TLS/on-wire bytes.
            event['byte_scope'] = 'decoded_response_body'
            if self.observer is not None:
                self.observer(event)

    def __deepcopy__(self, memo):
        # google-auth background refresh may clone transports. Preserve one pool
        # and the attempt observer rather than multiplying concurrent pools.
        return self

    def close(self) -> None:
        self.session.close()
