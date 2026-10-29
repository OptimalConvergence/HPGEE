from geecomputation.quota import read_quota, monthly_limit_hours


def test_month_limit_is_not_unknown_or_unlimited_sentinel():
    metric={'metric':'earthengine.googleapis.com/monthly_eecu_usage_time',
            'consumerQuotaLimits':[{'quotaBuckets':[{'effectiveLimit':'360000000'}]}]}
    assert monthly_limit_hours([metric])==100000
    metric['consumerQuotaLimits'][0]['quotaBuckets'][0]['effectiveLimit']='9223372036854775807'
    assert monthly_limit_hours([metric]) is None
    assert monthly_limit_hours([]) is None


class Response:
    def __init__(self, payload, status=200): self.payload,self.status_code=payload,status
    def json(self): return self.payload


class Session:
    def __init__(self, replies): self.replies=list(replies); self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs));return self.replies.pop(0)


def test_quota_reads_all_pages_but_never_invents_month_balance():
    session=Session([Response({'projectNumber':'123'}),Response({'maxConcurrentExports':5}),
                     Response({'metrics':[{'metric':'a'}],'nextPageToken':'page2'}),Response({'metrics':[{'metric':'b'}]})])
    result=read_quota('demo-project',session=session)
    assert result['quota']['status']=='READ'
    assert [m['metric'] for m in result['quota']['metrics']]==['a','b']
    assert result['monthly_remaining_eecu_hours'] is None
    assert session.calls[-1][1]['params']['pageToken']=='page2'


def test_forbidden_quota_preserves_unknown_and_config():
    session=Session([Response({'projectNumber':'123'}),Response({'maxConcurrentExports':5}),Response({},403)])
    result=read_quota('demo-project',session=session)
    assert result['quota']['status']=='UNKNOWN'
    assert result['quota']['http_status']==403
    assert result['project_config']['status']=='READ'


def test_bad_project_is_rejected_before_credentials():
    import pytest
    with pytest.raises(ValueError): read_quota('../other')


def test_supplied_project_number_is_not_trusted():
    import pytest
    session=Session([Response({'projectNumber':'123'})])
    with pytest.raises(ValueError,match='does not belong'):
        read_quota('demo-project',project_number='456',session=session)


def test_identity_denied_cannot_label_an_arbitrary_number_as_project_quota():
    session=Session([Response({},403),Response({'name':'projects/demo-project/config'})])
    result=read_quota('demo-project',project_number='456',session=session)
    assert result['project_number'] is None
    assert result['quota']['status']=='UNKNOWN'
