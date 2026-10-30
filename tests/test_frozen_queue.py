from collections import Counter
from pathlib import Path
from hpgee.transfer.queue import run_queue,PreparedRequests


def test_failed_request_waits_for_main_pass_before_one_recovery_pass():
    calls=[];attempts=Counter();events=[]
    def worker(item):
        calls.append(item);attempts[item]+=1
        return dict(id=item,status='failed' if item=='a' and attempts[item]==1 else 'success')
    result=run_queue(['a','b','c'],worker,workers=1,on_result=events.append,retry_rounds=1)
    assert calls==['a','b','c','a']
    assert result['pending']==[] and result['complete']
    assert [x['round'] for x in events]==[0,0,0,1]


def test_persistent_failures_remain_explicit_after_bounded_recovery():
    rows=[]
    result=run_queue(['a','b'],lambda x:dict(id=x,status='success' if x=='a' else 'failed'),workers=2,on_result=rows.append,retry_rounds=1)
    assert not result['complete'] and result['pending']==['b']
    assert Counter(x['id'] for x in rows)=={'a':1,'b':2}


def test_prepared_json_is_indexed_in_local_file_instead_of_retained_in_memory(tmp_path):
    values={'a':'{"值":1}','b':'{"x":"longer"}'}
    with PreparedRequests(tmp_path/'requests.bin',values.items()) as cache:
        assert {k:cache[k] for k in values}==values
        assert all(isinstance(v,tuple) and len(v)==2 for v in cache.index.values())
    assert not cache.path.exists()


def test_eager_cache_refuses_existing_path_without_erasing_it(tmp_path):
    import pytest
    path=tmp_path/'requests.bin';path.write_bytes(b'existing frozen inputs')
    with pytest.raises(FileExistsError):PreparedRequests(path,[('new','{}')])
    assert path.read_bytes()==b'existing frozen inputs'


def test_unavailable_route_gate_preserves_unsent_work_without_false_failures():
    calls=[];rows=[]
    result=run_queue(['a','b'],lambda uid:calls.append(uid),workers=2,on_result=rows.append,
                     before_submit=lambda:False)
    assert calls==rows==[] and result['pending']==['a','b']
    assert not result['complete'] and result['admission_stopped']


def test_route_gate_is_not_called_after_last_work_item():
    gates=[]
    result=run_queue(['a','b'],lambda uid:dict(id=uid,status='success'),workers=1,
                     on_result=lambda row:None,before_submit=lambda:gates.append(1))
    assert result['complete'] and len(gates)==2
