"""Small single-journal EE submission controller, not a pixel-download queue."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import uuid

from hpgee.accounting.usage import submission_allowed, normalize_operation


from hpgee.core.locking import file_lock as _locked


def _append(path, record):
    record = dict(record, recorded_at=datetime.now(timezone.utc).isoformat())
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def submit_once(spec, *, journal, project, workload_tag, usage, reserve_hours,
                limit_hours, export, max_active=2, logical_task_id=None, _legacy_spec=None):
    """Persist intent before I/O; an ambiguous request requires reconciliation.

    export(request_id, description) must submit with the supplied idempotency key.
    Reservations remain conservatively charged for this journal's lifetime;
    this is a submission guard, not a provider-enforced EECU hard limit.
    Use a separate journal per frozen run; never discard it to bypass the guard.
    """
    if reserve_hours <= 0 or max_active < 1:
        raise ValueError('Positive reservation and max_active are required')
    identity = {'project': project, 'spec': spec}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
    with _locked(journal) as path:
        records = _records(path)
        legacy = [r for r in records if 'schema_version' not in r]
        if legacy:
            # Old hashes included tags; test each journaled original tag. Never
            # append a new interpretation or a new task into a frozen v1 log.
            matching = [r for r in legacy if r.get('spec_hash') == hashlib.sha256(
                json.dumps(dict(identity, spec=_legacy_spec(r.get('workload_tag')) if _legacy_spec else spec, workload_tag=r.get('workload_tag')), sort_keys=True, allow_nan=False).encode()).hexdigest()]
            if not matching:
                raise RuntimeError('Legacy journal is recovery-only; use a separate versioned journal')
        else:
            matching = [r for r in records if r.get('logical_task_id') == (logical_task_id or fingerprint)]
            if matching and matching[-1]['spec_hash'] != fingerprint:
                raise ValueError('logical_task_id reused with a different export contract')
        if matching:
            last = matching[-1]
            if last['state'] == 'INTENT':
                return dict(last, state='SUBMISSION_UNKNOWN')
            return last
        if usage.get('project') != project or usage.get('workload_tag') not in (None, workload_tag):
            raise ValueError('Usage project/workload tag does not match submission')
        intents = [r for r in records if r['state'] == 'INTENT']
        reserved = sum(r['reserve_hours'] for r in intents)
        if not submission_allowed(usage, limit_hours, reserve_hours=reserved + reserve_hours):
            raise RuntimeError('Reported budget unknown or insufficient; no task submitted')
        latest = {r.get('logical_task_id', r['spec_hash']): r for r in records}
        active = sum(r['state'] not in ('SUCCEEDED', 'COMPLETED', 'FAILED', 'CANCELLED') for r in latest.values())
        if active >= max_active:
            raise RuntimeError('Run active-task limit reached; refresh known operations first')
        request_id = str(uuid.uuid4())
        record = {'schema_version': 2, 'logical_task_id': logical_task_id or fingerprint,
                  'spec_hash': fingerprint, 'request_id': request_id,
                  'description': 'gc-' + fingerprint[:24], 'project': project,
                  'workload_tag': workload_tag, 'state': 'INTENT', 'reserve_hours': reserve_hours}
        _append(path, record)
        # Keep the lock through submission so two controllers cannot both reserve.
        result = export(request_id, record['description'])
        if not result.get('name'):
            raise RuntimeError('Submission response lacks operation identity; reconcile INTENT')
        record = dict(record, state='SUBMITTED', name=result['name'])
        _append(path, record)
        return record


def refresh_known(*, journal, get_operation):
    """Refresh only operations owned by this journal. Never cancel or resubmit."""
    with _locked(journal) as path:
        latest = {r.get('logical_task_id', r['spec_hash']): r for r in _records(path)}
        out = []
        for record in latest.values():
            if not record.get('name'):
                out.append(dict(record, state='SUBMISSION_UNKNOWN'))
                continue
            try:
                observation = normalize_operation(get_operation(record['name']))
            except Exception as exc:
                out.append(dict(record, refresh_error_type=type(exc).__name__))
                continue
            history = record.get('operation_observations', []) + [dict(observation, observed_at=datetime.now(timezone.utc).isoformat())]
            attempts = {}
            for item in history:
                attempt, cost = item.get('attempt'), item.get('attempt_eecu_seconds')
                if attempt is not None and cost is not None:
                    attempts[attempt] = max(attempts.get(attempt, 0), cost)
            updated = dict(record, **observation, operation_observations=history,
                           operation_lower_bound_seconds=sum(attempts.values()) if attempts else None,
                           operation_cost_complete=bool(observation['done'] and observation.get('attempt') == 1 and observation.get('attempt_eecu_seconds') is not None))
            if record.get('schema_version') == 2:
                _append(path, updated)
            out.append(updated)
        return out


def submit_ee_export(task, *, contract, journal, project, workload_tag, usage,
                     reserve_hours, limit_hours, max_active=2, logical_task_id=None):
    """Accept a prepared ee.batch.Export task without calling Task.start().

    REST requestId is persisted before exportImage/exportTable. This avoids
    relying on Task.start's private request-id lifecycle after timeouts.
    """
    import ee
    # EE's exporter uses process-global state, not this function's project argument.
    # This narrow version adapter fails closed if the installed client changes.
    # Keep one initialized project per process; do not mutate it concurrently.
    project_path = getattr(ee.data, '_get_projects_path', None)
    if project_path is None or project_path() != 'projects/' + project:
        raise ValueError('Initialized Earth Engine project does not match submission')
    dispatch = {ee.batch.Task.Type.EXPORT_IMAGE: ee.data.exportImage,
                ee.batch.Task.Type.EXPORT_TABLE: ee.data.exportTable,
                ee.batch.Task.Type.EXPORT_CLASSIFIER: ee.data.exportClassifier}
    if task.task_type not in dispatch:
        raise ValueError('Only image/table/classifier exports are supported')
    original_config = dict(task.config or {})
    config = dict(original_config)
    config.pop('workloadTag', None)
    config.pop('description', None)
    spec = {'contract': contract, 'task_type': str(task.task_type),
            'expression': ee.serializer.encode(config, for_cloud_api=True)}
    def legacy_spec(tag):
        return dict(spec, expression=ee.serializer.encode(dict(original_config, workloadTag=tag), for_cloud_api=True))
    def export(request_id, description):
        if project_path() != 'projects/' + project:
            raise ValueError('Initialized Earth Engine project changed before submission')
        return dispatch[task.task_type](request_id, dict(config, description=description, workloadTag=workload_tag))
    return submit_once(spec, journal=journal, project=project, workload_tag=workload_tag,
                       usage=usage, reserve_hours=reserve_hours, limit_hours=limit_hours,
                       export=export, max_active=max_active, logical_task_id=logical_task_id, _legacy_spec=legacy_spec)


def make_logical_task_id(*, unit_id, trial_id, backend, export_contract):
    """Stable unit/trial/backend/export identity; plans are provenance only."""
    if any(not isinstance(x, str) or not x for x in (unit_id, trial_id, backend)):
        raise ValueError('Explicit unit, trial and backend required')
    identity = dict(unit_id=unit_id, trial_id=trial_id, backend=backend, export_contract=export_contract)
    return hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
