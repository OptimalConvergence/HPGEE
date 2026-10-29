"""Read-only GCP quota discovery; limits are distinct from monthly usage."""
from datetime import datetime, timezone
import re


def monthly_limit_hours(metrics):
    """Parse a verified limit, never substitute the limit for remaining usage."""
    limits=[]
    for metric in metrics:
        if metric.get('metric')!='earthengine.googleapis.com/monthly_eecu_usage_time':
            continue
        for limit in metric.get('consumerQuotaLimits',[]):
            for bucket in limit.get('quotaBuckets',[]):
                value=bucket.get('effectiveLimit')
                if value is not None and str(value).isdigit() and 0<=int(value)<9223372036854775807:
                    limits.append(int(value)/3600)
    return min(limits) if limits else None


def _get(session, url, **kwargs):
    try:
        response=session.get(url, timeout=20, **kwargs)
        if response.status_code != 200:
            return {'status':'UNKNOWN','http_status':response.status_code}, None
        return {'status':'READ'}, response.json()
    except Exception as exc:
        return {'status':'UNKNOWN','error_type':type(exc).__name__}, None


def read_quota(project, *, project_number=None, session=None, credentials=None):
    if not re.fullmatch(r'[a-z][a-z0-9-]{4,61}[a-z0-9]',project):
        raise ValueError('Invalid project ID')
    if project_number is not None and not str(project_number).isdigit():
        raise ValueError('Project number must be numeric')
    base={'project':project,'checked_at':datetime.now(timezone.utc).isoformat(),
          'monthly_remaining_eecu_hours':None,
          'monthly_balance_status':'UNKNOWN: requires verified tier/period and project usage',
          'required_read_permissions':['resourcemanager.projects.get','serviceusage.quotas.get','monitoring.timeSeries.list'],
          'mutations_performed':False}
    if session is None:
        try:
            import ee
            from google.auth.transport.requests import AuthorizedSession
            session=AuthorizedSession(credentials or ee.data.get_persistent_credentials())
        except Exception as exc:
            return dict(base,project_config={'status':'UNKNOWN','error_type':type(exc).__name__},
                        quota={'status':'UNKNOWN','error_type':type(exc).__name__})
    identity_status,identity=_get(session,f'https://cloudresourcemanager.googleapis.com/v1/projects/{project}')
    verified_number=identity.get('projectNumber') if identity else None
    base['project_identity']=identity_status
    if project_number is not None and verified_number is not None and str(project_number)!=str(verified_number):
        raise ValueError('Project number does not belong to requested project')
    project_number=verified_number
    base['project_number']=project_number
    config_status,config=_get(session,f'https://earthengine.googleapis.com/v1/projects/{project}/config')
    base['project_config']=dict(config_status,config=config)
    if project_number is None:
        base['quota']={'status':'UNKNOWN','reason':'project number unavailable'}
        return base
    url=f'https://serviceusage.googleapis.com/v1beta1/projects/{project_number}/services/earthengine.googleapis.com/consumerQuotaMetrics'
    metrics=[];seen=set();params={'view':'FULL','pageSize':100}
    for _ in range(50):
        status,payload=_get(session,url,params=dict(params))
        if payload is None:
            base['quota']=dict(status,metrics=metrics,enumeration_complete=False)
            return base
        metrics.extend(payload.get('metrics',[]))
        token=payload.get('nextPageToken')
        if not token:
            base['quota']={'status':'READ','metrics':metrics,'enumeration_complete':True}
            base['monthly_limit_eecu_hours']=monthly_limit_hours(metrics)
            base['monthly_reset_timezone']='America/Los_Angeles (noncommercial projects)'
            return base
        if token in seen: break
        seen.add(token);params['pageToken']=token
    base['quota']={'status':'UNKNOWN','metrics':metrics,'enumeration_complete':False,'reason':'pagination incomplete'}
    return base
