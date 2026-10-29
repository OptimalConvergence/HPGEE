import json
from types import SimpleNamespace as NS
import pytest
from geesampler.transport import RequestsTransport

def test_attempts_include_failure_bytes_tags_but_no_secrets():
    events=[]
    transport=RequestsTransport(pool_size=2,observer=events.append)
    responses=iter([NS(status_code=429,headers={},content=b'quota'),NS(status_code=200,headers={},content=b'abcd')])
    transport.session.request=lambda *a,**kw:next(responses)
    body=json.dumps({'workloadTag':'trial-a','expression':{'secret':'do-not-log'}})
    for _ in range(2):transport.request('https://earthengine.googleapis.com/v1/projects/test/image:computePixels?key=secret','POST',body)
    assert len([e for e in events if e['phase']=='started'])==2
    events=[e for e in events if e['phase']=='finished']
    assert [e['response_bytes'] for e in events]==[5,4]
    assert [e['status'] for e in events]==[429,200]
    assert all(e['workload_tag']=='trial-a' for e in events)
    assert 'secret' not in json.dumps(events)

def test_network_failure_is_attempt_with_unknown_bytes():
    events=[];transport=RequestsTransport(pool_size=1,observer=events.append)
    def fail(*a,**kw):raise TimeoutError('secret URL')
    transport.session.request=fail
    with pytest.raises(TimeoutError):transport.request('https://earthengine.googleapis.com/x')
    assert events[-1]['response_bytes'] is None
    assert events[-1]['error_type']=='TimeoutError'
    assert events[0]['phase']=='started'


def test_response_phase_fields_use_same_clock_and_allowlisted_headers(monkeypatch):
    events = []
    transport = RequestsTransport(pool_size=1, observer=events.append)
    response = NS(status_code=200,
                  headers={"Content-Encoding": "gzip", "Content-Length": "19"},
                  content=b"data", history=[])

    def fake_request(*args, **kwargs):
        kwargs["hooks"]["response"](response)
        return response

    transport.session.request = fake_request
    ticks = iter((10.0, 10.25, 11.0))
    monkeypatch.setattr("geesampler.transport.time.monotonic", lambda: next(ticks))
    transport.request("https://earthengine.googleapis.com/x")

    event = next(row for row in events if row["phase"] == "finished")
    assert event["elapsed_seconds"] == pytest.approx(1.0)
    assert event["response_header_seconds"] == pytest.approx(0.25)
    assert event["response_body_seconds"] == pytest.approx(0.75)
    assert event["content_encoding"] == "gzip"
    assert event["content_length"] == "19"
    assert event["decoded_payload_bytes"] == event["response_bytes"] == 4


def test_redirect_chain_keeps_phase_times_unknown(monkeypatch):
    events = []
    transport = RequestsTransport(pool_size=1, observer=events.append)
    redirect = NS(status_code=302, headers={"Location": "https://example.invalid/final"},
                  content=b"", history=[])
    final = NS(status_code=200, headers={"Content-Length": "4"}, content=b"data",
               history=[redirect])

    def fake_request(*args, **kwargs):
        hook = kwargs["hooks"]["response"]
        hook(redirect)
        hook(final)
        return final

    transport.session.request = fake_request
    ticks = iter((20.0, 20.1, 20.2, 20.4))
    monkeypatch.setattr("geesampler.transport.time.monotonic", lambda: next(ticks))
    transport.request("https://earthengine.googleapis.com/x")

    event = next(row for row in events if row["phase"] == "finished")
    assert event["response_header_seconds"] is None
    assert event["response_body_seconds"] is None
