"""EE HTTP transport with cancellable wall-clock deadlines and small timing history."""
from collections import defaultdict, deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from urllib.parse import urlsplit
import uuid

from hpgee.runtime.admission import Admission

import httplib2
from requests.exceptions import ConnectionError, SSLError, Timeout


class BoundedTransport:
    """curl owns each request, so a deadline stops the network operation itself.

    Requests use fresh connections. No worker thread or stale pooled connection
    survives cancellation. Credentials travel through private stdin, not argv.
    """
    def __init__(self, *, pool_size, timeout=60, observer=None, scratch=None, route_pool=None):
        self.admission = Admission(pool_size)
        if timeout <= 0: raise ValueError("Positive HTTP timeout required")
        self.pool_size, self.timeout, self.observer = pool_size, timeout, observer
        self.scratch = scratch
        self.adaptive = True
        self.samples = defaultdict(lambda: deque(maxlen=20))
        self.lock = threading.Lock()
        self.route_pool = route_pool
        self.failed_routes = {}

    @staticmethod
    def request_key(path, body):
        if not path.endswith(':computePixels'):
            return None
        request = json.loads(body)
        dims = request['grid']['dimensions']
        return dims['width'], dims['height'], len(request['bandIds'])

    def deadline(self, key):
        if key is None:
            return min(20, self.timeout)
        with self.lock:
            samples = list(self.samples[key])
        return min(self.timeout, max(20, 3 * statistics.median(samples))) if self.adaptive and len(samples) >= 5 else self.timeout

    def observe_success(self, key, seconds):
        if key is not None:
            with self.lock:
                self.samples[key].append(seconds)

    def request(self, *args, **kwargs):
        with self.admission:
            return self._request(*args, **kwargs)

    def _request(self, uri, method='GET', body=None, headers=None, redirections=None, connection_type=None):
        del redirections, connection_type
        url = urlsplit(uri)
        key = self.request_key(url.path, body)
        pixels = key is not None
        parsed = json.loads(body) if pixels else {}
        identity = hashlib.sha256(json.dumps({k: v for k, v in parsed.items() if k != 'workloadTag'},
                                            sort_keys=True).encode()).hexdigest() if pixels else None
        lease = None
        if self.route_pool is not None:
            with self.lock:
                avoid = set(self.failed_routes.get(identity, ())) if identity else set()
            lease = self.route_pool.acquire(exclude=avoid)
            if key is not None:
                key = (lease.name, *key)
        deadline = self.deadline(key)
        started = time.monotonic()
        event = dict(attempt_id=uuid.uuid4().hex, started_at=datetime.now(timezone.utc).isoformat(),
                     host=url.hostname, path=url.path, method=method, workload_tag=parsed.get('workloadTag'),
                     status=None, response_bytes=None, error_type=None, transport_ok=False, deadline_seconds=deadline,
                     byte_scope='decoded_response_body', partial_response_bytes=None,
                     response_header_seconds=None, response_body_seconds=None)
        event['route'] = lease.name if lease is not None else 'isolated-evaluation'
        network_error = False
        try:
            if self.observer:
                self.observer(dict(event, phase='started'))
            with TemporaryDirectory(prefix='ee-http-', dir=self.scratch) as directory:
                root = Path(directory)
                # curl configuration is read from stdin; do not log it or stderr.
                quote = lambda value: json.dumps(str(value), ensure_ascii=False)
                config = [f'url = {quote(uri)}', f'request = {quote(method)}']
                if lease is not None:
                    config += [f'proxy = {quote(lease.proxy_url)}', 'noproxy = ""']
                auth_file = lease.proxy_auth_file if lease is not None else os.environ.get('DATAFETCH_PROXY_AUTH_FILE')
                if auth_file:
                    path = Path(auth_file)
                    info = path.stat()
                    if info.st_uid != os.getuid() or info.st_mode & 0o077:
                        raise PermissionError('Proxy credential file must be owner-only')
                    lines = [line.strip() for line in path.read_text().splitlines() if line.strip()]
                    if not lines or any(not line.startswith('proxy-user = ') for line in lines):
                        raise ValueError('Proxy auth config may contain only proxy-user')
                    config += lines
                config += [f'header = {quote(str(name) + ": " + str(value))}' for name, value in (headers or {}).items()]
                if body is not None:
                    request_file = root / 'request'
                    request_file.write_bytes(body.encode() if isinstance(body, str) else body)
                    request_file.chmod(0o600)
                    config.append(f'data-binary = {quote("@" + str(request_file))}')
                command = ['curl', '--disable', '--silent', '--show-error', '--compressed',
                           '--connect-timeout', str(min(10, deadline)), '--max-time', str(deadline),
                           '--dump-header', str(root / 'headers'), '--output', str(root / 'body'),
                           '--write-out', '{"http_code":"%{http_code}","time_starttransfer":%{time_starttransfer},'
                           '"time_total":%{time_total}}', '--config', '-']
                process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    try:
                        stdout, _ = process.communicate(('\n'.join(config) + '\n').encode(), timeout=deadline + 2)
                    except subprocess.TimeoutExpired:
                        raise Timeout(f'HTTP wall-clock deadline exceeded ({deadline:.1f}s)') from None
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                metrics = json.loads(stdout or b'{}')
                event['status'] = int(metrics.get('http_code') or metrics.get('response_code') or 0) or None
                network_error = event['status'] in (502, 503, 504)
                if network_error and lease is not None and identity:
                    with self.lock:
                        self.failed_routes.setdefault(identity, set()).add(lease.name)
                event['response_header_seconds'] = metrics.get('time_starttransfer')
                event['response_body_seconds'] = max(0, metrics.get('time_total', 0) - (metrics.get('time_starttransfer') or 0))
                if process.returncode:
                    event['partial_response_bytes'] = (root / 'body').stat().st_size if (root / 'body').exists() else 0
                    error_type = Timeout if process.returncode == 28 else SSLError if process.returncode in (35, 51, 58, 60) else ConnectionError
                    raise error_type(f'HTTP transport failed (curl exit {process.returncode}; limit {deadline:.1f}s)')
                if event['status'] is None:
                    raise ConnectionError('HTTP response status is missing from curl metrics')
                payload = (root / 'body').read_bytes()
                # curl writes CRLF blocks for proxy CONNECT/100 responses, then the final one.
                raw_headers = (root / 'headers').read_text(errors='replace').replace('\r\n', '\n')
                raw_headers = raw_headers.strip().split('\n\n')[-1]
                response_headers = dict(line.split(':', 1) for line in raw_headers.splitlines()[1:] if ':' in line)
                response_headers = {name.strip().lower(): value.strip() for name, value in response_headers.items()}
                response_headers['status'] = event['status']
                event.update(response_bytes=len(payload), decoded_payload_bytes=len(payload))
                if event['status'] == 200:
                    self.observe_success(key, time.monotonic() - started)
                    if identity:
                        with self.lock:
                            self.failed_routes.pop(identity, None)
                response = httplib2.Response(response_headers)
                event['transport_ok'] = True
                return response, payload
        except Exception as error:
            network_error = isinstance(error, (ConnectionError, SSLError, Timeout))
            if network_error and lease is not None and identity:
                with self.lock:
                    self.failed_routes.setdefault(identity, set()).add(lease.name)
            event['error_type'] = type(error).__name__
            raise
        finally:
            elapsed = time.monotonic() - started
            if lease is not None:
                self.route_pool.release(lease, network_error=network_error,
                    bytes_count=event['response_bytes'] if event['status'] == 200 else None,
                    seconds=elapsed, pixels=pixels)
            if self.observer:
                self.observer(dict(event, phase='finished', elapsed_seconds=elapsed))

    def __deepcopy__(self, memo):
        return self

    def close(self):
        pass
