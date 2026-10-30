from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import time
import pytest
from requests.exceptions import Timeout
from hpgee.runtime.bounded import BoundedTransport


def test_http200_trickle_still_fails_total_deadline(monkeypatch):
    monkeypatch.setenv('NO_PROXY','127.0.0.1');monkeypatch.setenv('no_proxy','127.0.0.1')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.send_header('Content-Length','100');self.end_headers()
            try:
                for _ in range(100):self.wfile.write(b'x');self.wfile.flush();time.sleep(.05)
            except (BrokenPipeError,ConnectionResetError):pass
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    events=[];http=BoundedTransport(pool_size=1,timeout=.25,observer=events.append)
    try:
        began=time.monotonic()
        with pytest.raises(Timeout):http.request(f'http://127.0.0.1:{server.server_port}/body')
        assert time.monotonic()-began<2
        event=events[-1]
        assert event['status']==200 and event['error_type']=='Timeout'
        assert not event['transport_ok'] and event['response_bytes'] is None
        assert event['partial_response_bytes']>0 and http.admission.active==0
    finally:server.shutdown();server.server_close()


def test_pool_size_is_a_real_limit_and_exceptions_release_it(monkeypatch):
    http=BoundedTransport(pool_size=3);lock=threading.Lock();active=[0];peak=[0]
    def request(value):
        with lock:active[0]+=1;peak[0]=max(peak[0],active[0])
        try:
            time.sleep(.01)
            if value==0:raise Timeout('one simulated failure')
            return value
        finally:
            with lock:active[0]-=1
    monkeypatch.setattr(http,'_request',request)
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures=[pool.submit(http.request,n) for n in range(12)]
        for n,future in enumerate(futures):
            if n==0:
                with pytest.raises(Timeout):future.result()
            else:assert future.result()==n
    assert peak[0]<=3 and http.admission.active==0
    assert http.request(12)==12


@pytest.mark.parametrize('size',[0,41])
def test_invalid_global_budget_rejected(size):
    with pytest.raises(ValueError):BoundedTransport(pool_size=size)


@pytest.mark.parametrize('field',['http_code','response_code'])
@pytest.mark.parametrize('exit_code',[0,28])
def test_curl_status_field_names_are_portable_and_partial200_is_not_success(tmp_path,monkeypatch,field,exit_code):
    import json
    import hpgee.runtime.bounded as module
    monkeypatch.delenv('DATAFETCH_PROXY_AUTH_FILE',raising=False)
    class Process:
        returncode=exit_code
        def __init__(self,command,**kwargs):
            from pathlib import Path
            assert command[command.index('--write-out')+1] != '%{json}'
            Path(command[command.index('--dump-header')+1]).write_text('HTTP/1.1 200 OK\nContent-Type: application/json\n\n')
            Path(command[command.index('--output')+1]).write_bytes(b'{}')
        def communicate(self,*args,**kwargs):return json.dumps({field:'200','time_total':.2,'time_starttransfer':.1}).encode(),b''
        def poll(self):return self.returncode
    monkeypatch.setattr(module.subprocess,'Popen',Process)
    events=[];http=module.BoundedTransport(pool_size=1,scratch=tmp_path,observer=events.append)
    if exit_code:
        with pytest.raises(Timeout):http.request('https://example.invalid/token')
        assert events[-1]['status']==200 and not events[-1]['transport_ok']
    else:
        response,payload=http.request('https://example.invalid/token')
        assert response.status==200 and payload==b'{}' and events[-1]['transport_ok']


def test_proxy_connect_headers_do_not_leak_into_final_response(tmp_path, monkeypatch):
    import json
    import hpgee.runtime.bounded as module
    monkeypatch.delenv('DATAFETCH_PROXY_AUTH_FILE', raising=False)
    headers = ('HTTP/1.1 200 Connection established\r\nVia: proxy\r\n\r\n'
               'HTTP/2 429\r\nretry-after: 17\r\n\r\n')

    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            from pathlib import Path
            Path(command[command.index('--dump-header') + 1]).write_text(headers)
            Path(command[command.index('--output') + 1]).write_bytes(b'{}')
        def communicate(self, *args, **kwargs):
            return json.dumps({'http_code': '429', 'time_total': .2, 'time_starttransfer': .1}).encode(), b''
        def poll(self):
            return self.returncode

    monkeypatch.setattr(module.subprocess, 'Popen', Process)
    response, _ = module.BoundedTransport(pool_size=1, scratch=tmp_path).request('https://example.invalid/x')
    assert response.status == 429 and response['retry-after'] == '17' and 'via' not in response
