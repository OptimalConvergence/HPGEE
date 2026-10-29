from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from geesampler.monitoring import CloudEECUReader, EECUSnapshot
from google.cloud import monitoring_v3 as m

def series(value, tag='test', integer=True):
    return m.TimeSeries(metric={'type':'earthengine.googleapis.com/project/cpu/usage_time','labels':{'workload_tag':tag}},resource={'type':'earthengine_project','labels':{'project_id':'project-x'}},points=[{'interval':{'start_time':{'seconds':100},'end_time':{'seconds':160}},'value':{'int64_value' if integer else 'double_value':value}}])

def test_fixed_start_typed_pages_dedup_and_missing():
    start=datetime.now(timezone.utc)-timedelta(hours=2)
    calls=[]
    class Client:
        def list_time_series(self,request,**kw):
            calls.append(request)
            return iter([series(7),series(7)]) if 'in_progress' not in request['filter'] else iter([])
    reader=CloudEECUReader('project-x','test',start=start,client=Client())
    result=reader.read()
    assert result.completed_seconds==7
    assert result.in_progress_seconds is None
    assert all(r['interval'].start_time==start for r in calls)
    assert result.guard_seconds==7

def test_wrong_tag_is_rejected():
    class Client:
        def list_time_series(self,**kw):return [series(0.5,tag='other',integer=False)]
    import pytest
    with pytest.raises(ValueError):CloudEECUReader('project-x','test',client=Client()).read()

def test_unknown_is_not_zero():
    assert EECUSnapshot('now',None,None,available=False).guard_seconds is None

def test_rest_monitor_paginates_using_same_authorized_session():
    from geesampler.monitoring import MonitoringRESTClient
    calls=[]
    payload=m.TimeSeries.to_json(series(7))
    import json
    class Session:
        def get(self,url,params,timeout):
            calls.append(params.copy())
            body={'timeSeries':[json.loads(payload)]}
            if len(calls)==1:body['nextPageToken']='next'
            return NS(raise_for_status=lambda:None,json=lambda:body)
    client=MonitoringRESTClient(session=Session())
    interval=m.TimeInterval({'start_time':{'seconds':1},'end_time':{'seconds':200}})
    result=list(client.list_time_series(request={'name':'projects/project-x','filter':'test','interval':interval},timeout=30))
    assert len(result)==2 and calls[1]['pageToken']=='next'
    assert client.http_attempts==2
