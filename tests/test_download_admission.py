from concurrent.futures import ThreadPoolExecutor
import threading
import time
import pytest
from hpgee.runtime.admission import Admission, split_budget


def test_three_host_http_budget_never_inherits_45_logical_threads():
    for cap in (32,40):
        assert sum(split_budget(cap,r,3) for r in range(3))==cap
    limiter=Admission(4);lock=threading.Lock();active=peak=0
    def work(i):
        nonlocal active,peak
        try:
            with limiter:
                with lock:active+=1;peak=max(peak,active)
                time.sleep(.001)
                with lock:active-=1
                if i%3==0:raise RuntimeError('simulated failed HTTP')
        except RuntimeError:pass
    with ThreadPoolExecutor(45) as pool:list(pool.map(work,range(90)))
    assert peak<=4
    assert limiter.peak<=4 and limiter.active==0


def test_bad_budgets_refused():
    with pytest.raises(ValueError):Admission(0)
    with pytest.raises(ValueError):split_budget(45,0,3)
