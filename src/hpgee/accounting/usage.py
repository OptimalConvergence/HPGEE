"""Fixed-interval reported EE usage; absence of telemetry is never zero usage."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from hpgee.accounting import metrics
from hpgee.core.locking import file_lock

COMPLETED = metrics.COMPLETED_METRIC
IN_PROGRESS = metrics.IN_PROGRESS_METRIC
deduplicate_points = metrics.deduplicate_points
_utc = metrics.utc_datetime


def summarize_usage(completed, in_progress, *, start, end, error=None):
    if _utc(start) >= _utc(end):
        raise ValueError('Usage interval must have start < end')
    a, b = metrics.sum_usage(completed), metrics.sum_usage(in_progress)
    available = not error and (a is not None or b is not None)
    return {
        'start': start, 'end': end, 'status': 'REPORTED' if available else 'UNKNOWN',
        'completed_seconds': a, 'in_progress_seconds': b,
        'guard_hours': metrics.guard_seconds(a, b) / 3600 if available else None,
        'error_type': error, 'telemetry_complete': False, 'evidence_status': 'partial' if available else 'unknown',
        'reconciliation_reference': None,
        'limitation': 'Delayed reported usage; metrics are overlapping views, not additive. Not a hard cost cap.',
    }


def submission_allowed(usage, limit_hours, *, reserve_hours):
    if not math.isfinite(limit_hours) or limit_hours <= 0 or not math.isfinite(reserve_hours) or reserve_hours < 0:
        raise ValueError('Invalid limit or reservation')
    value = usage.get('guard_hours')
    return usage.get('status') == 'REPORTED' and value is not None and value + reserve_hours <= limit_hours


def read_usage(project, *, start, end, workload_tag=None, credentials=None, client=None):
    """Read complete paginated DELTA metrics using explicit credentials, never ADC by accident.

    Start must be the persisted run start for run totals. Monthly accounting
    requires the provider's billing-period start, not a rolling 30-minute window.
    """
    metrics.validate_scope(project, workload_tag)
    t0, t1 = _utc(start), _utc(end)
    if t0 >= t1:
        raise ValueError('Usage interval must have start < end')
    try:
        reader = metrics.MonitoringReader(project, workload_tag, credentials=credentials, client=client)
        queried_at = datetime.now(timezone.utc).isoformat()
        observation = reader.read(start=t0, end=t1)
        raw_points = [dict(point, queried_at=queried_at) for point in observation.raw_points]
        values = ([observation.completed_seconds] if observation.completed_seconds is not None else [],
                  [observation.in_progress_seconds] if observation.in_progress_seconds is not None else [])
        return dict(summarize_usage(*values, start=start, end=end), project=project, workload_tag=workload_tag, raw_points=raw_points, queried_at=queried_at)
    except Exception as exc:
        return dict(summarize_usage([], [], start=start, end=end, error=type(exc).__name__), project=project, workload_tag=workload_tag)


def normalize_operation(operation):
    metadata = operation.get('metadata', {})
    state = metadata.get('state', 'UNKNOWN')
    if operation.get('error'):
        state = 'FAILED' if state not in ('CANCELLED', 'CANCELLING') else state
    return {
        'name': operation.get('name'), 'state': state, 'done': operation.get('done', False),
        'description': metadata.get('description'), 'attempt': metadata.get('attempt'),
        'attempt_eecu_seconds': metadata.get('batchEecuUsageSeconds'),
        'progress': metadata.get('progress'), 'destination_uris': metadata.get('destinationUris', []),
        'error_code': operation.get('error', {}).get('code'),
        'artifact_verified': False,
    }


def make_workload_tag(run_id, unit_id, stage, attempt):
    """A client attempt tag; server operation attempts are a separate identity."""
    import hashlib
    import json
    if any(not isinstance(v, str) or not v for v in (run_id, unit_id, stage)) or type(attempt) is not int or attempt < 1:
        raise ValueError('Explicit run/unit/stage and positive client attempt required')
    digest = hashlib.sha256(json.dumps([run_id, unit_id, stage, attempt]).encode()).hexdigest()
    prefix = re.sub('[^a-z0-9]+', '-', stage.lower()).strip('-')[:20] or 'request'
    return f'gc-{prefix}-{digest[:32]}'


def project_month_interval(at, *, timezone_name='America/Los_Angeles'):
    """Provider calendar month, including DST offsets at each boundary."""
    from zoneinfo import ZoneInfo
    local = _utc(at).astimezone(ZoneInfo(timezone_name))
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    return tuple(v.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z') for v in (start, end))


class RunLedger:
    """Versioned, locked run reservations shared by Batch and sampler batches.

    Project remaining is a separately reconciled baseline at run start (it must
    exclude this run's subsequent charges). Unknown observations stop expansion.
    This estimates exposure; it cannot enforce a provider hard cost cap.
    """
    def __init__(self, path, *, project, run_id, run_limit_hours,
                 project_remaining_hours, bootstrap_max_hours=0):
        from pathlib import Path
        self.path = Path(path)
        for value in (run_limit_hours, project_remaining_hours):
            if value is not None and (isinstance(value, bool) or not math.isfinite(value) or value <= 0):
                raise ValueError('Explicit positive budget required')
        if run_limit_hours is None:
            raise ValueError('Explicit run budget required')
        if not math.isfinite(bootstrap_max_hours) or not 0 <= bootstrap_max_hours <= run_limit_hours:
            raise ValueError('Invalid bootstrap bound')
        self.identity = dict(schema_version=2, project=project, run_id=run_id,
                             run_limit_hours=run_limit_hours, project_remaining_hours=project_remaining_hours,
                             bootstrap_max_hours=bootstrap_max_hours)

    def _read(self):
        import json
        value = json.loads(self.path.read_text()) if self.path.exists() else dict(self.identity, units={})
        if any(value.get(k) != v for k, v in self.identity.items()):
            raise ValueError('Ledger identity/budget differs; reconcile original ledger')
        return value

    def _write(self, value):
        import json
        import os
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        with temporary.open('w') as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    @staticmethod
    def _summary(value):
        lower = unsettled = charged = 0
        unknown = False
        for row in value['units'].values():
            observations = row.get('attempts', {})
            cost = sum(x['hours'] for x in observations.values())
            settled = bool(observations) and all(x['complete'] for x in observations.values())
            lower += cost
            if not settled:
                unsettled += row['reserve_hours']
                unknown |= bool(observations)
            charged += cost if settled else max(row['reserve_hours'], cost)
        return dict(reported_lower_bound_hours=lower, unsettled_reservations_hours=unsettled,
                    charged_hours=charged, unsettled_cost_unknown=unknown,
                    run_limit_hours=value['run_limit_hours'], project_remaining_hours=value['project_remaining_hours'])

    def summary(self):
        with file_lock(self.path):
            return self._summary(self._read())

    def reserve(self, unit_id, hours, *, estimate=None, bootstrap=False, require_verified_pilot=True):
        if isinstance(hours, bool) or not math.isfinite(hours) or hours <= 0:
            raise ValueError('Positive reservation required')
        if not bootstrap:
            if not estimate or not estimate.get('pilot_reference') or not isinstance(estimate.get('unknowns'), list):
                raise ValueError('Reservation estimate with pilot reference and unknowns required')
            expected, factor = estimate.get('estimate_hours'), estimate.get('safety_factor')
            if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in (expected, factor)) or expected <= 0 or factor < 1 or hours < expected * factor:
                raise ValueError('Reservation must cover estimate times safety factor')
        with file_lock(self.path):
            value = self._read()
            if unit_id in value['units']:
                return value['units'][unit_id]
            if not bootstrap and require_verified_pilot:
                pilot = value['units'].get(estimate['pilot_reference'], {})
                observed = pilot.get('attempts', {})
                if not observed or not all(x['complete'] for x in observed.values()):
                    raise ValueError('Reservation pilot must reference reconciled ledger evidence')
            summary = self._summary(value)
            if summary['unsettled_cost_unknown']:
                raise RuntimeError('Unknown unsettled costs; reconcile before expansion')
            if bootstrap and (not self.identity['bootstrap_max_hours'] or hours > self.identity['bootstrap_max_hours'] or summary['unsettled_reservations_hours']):
                raise RuntimeError('Bootstrap permits one configured small unit in flight')
            remaining = value['project_remaining_hours']
            if remaining is None and not bootstrap:
                raise RuntimeError('Project budget unknown')
            if summary['charged_hours'] + hours > min(value['run_limit_hours'], remaining if remaining is not None else value['run_limit_hours']):
                raise RuntimeError('Run or project budget insufficient')
            row = dict(reserve_hours=hours, estimate=estimate, bootstrap=bootstrap, attempts={})
            value['units'][unit_id] = row
            self._write(value)
            return row

    def observe(self, unit_id, *, hours, attempt, complete, reference):
        if isinstance(hours, bool) or not math.isfinite(hours) or hours < 0 or type(attempt) is not int or attempt < 1 or not reference:
            raise ValueError('Valid attempt cost and reconciliation reference required')
        with file_lock(self.path):
            value = self._read()
            row = value['units'][unit_id]
            for other_id, other in value['units'].items():
                if other_id != unit_id and any(x['reference'] == reference for x in other.get('attempts', {}).values()):
                    raise ValueError('Cost reference is already attributed to another unit')
            previous = row['attempts'].get(str(attempt), {})
            row['attempts'][str(attempt)] = dict(hours=max(hours, previous.get('hours', 0)),
                                               complete=bool(complete), reference=reference)
            self._write(value)
