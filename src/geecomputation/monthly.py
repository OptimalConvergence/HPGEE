"""Offline organization of caller-supplied burn-state time support.

References are preserved, not authenticated or granted scientific eligibility.
No score, threshold, observation lookup, event closure or EE initialization.
"""
from copy import deepcopy
from datetime import datetime, timezone
import re


def _utc(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Explicit timezone-aware ISO timestamp required') from exc


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z')


def _endpoint(value, as_of):
    if value is None:
        return None, None
    if not isinstance(value, dict):
        raise ValueError('Endpoint requires time and evidence_ref')
    reference = value.get('evidence_ref')
    if not ((isinstance(reference, str) and reference.strip())
            or (isinstance(reference, dict) and reference)):
        raise ValueError('Nonempty caller evidence_ref required')
    time = _utc(value.get('time'))
    if time >= as_of:
        raise ValueError('Endpoint time must be strictly before as_of')
    result = deepcopy(value)
    result['time'] = _iso(time)
    return result, time


def resolve_burn_month_support(*, target_month, as_of, last_unburned=None, first_burned=None):
    """Bound possible months of (last_unburned, first_burned] without dating a fire.

    Endpoints are None or {time, evidence_ref}; references may be opaque nonempty
    strings or dictionaries. Their pixel/source/state truth is the caller's duty.
    A null target_month_supported means missing or ambiguous temporal support;
    false requires a bounded interval entirely outside the target month.
    """
    if not isinstance(target_month, str) or not re.fullmatch(r'(201[89]|202[0-5])-(0[1-9]|1[0-2])', target_month):
        raise ValueError('target_month must be YYYY-MM within 2018–2025')
    cutoff = _utc(as_of)
    unburned, u = _endpoint(last_unburned, cutoff)
    burned, b = _endpoint(first_burned, cutoff)
    interval = months = bounded = supported = None
    if u is not None and b is not None:
        if u >= b:
            raise ValueError('last_unburned must precede first_burned')
        interval = dict(start=_iso(u), end=_iso(b), start_inclusive=False, end_inclusive=True)
        # Both endpoint months intersect (u,b]; b at a month boundary is included.
        months = [f'{index // 12:04}-{index % 12 + 1:02}'
                  for index in range(u.year * 12 + u.month - 1, b.year * 12 + b.month)]
        bounded = months[0] if len(months) == 1 else None
        supported = True if months == [target_month] else False if target_month not in months else None
        status = 'bounded'
    else:
        status = 'unknown' if u is None and b is None else 'left_missing' if u is None else 'right_missing'
    return dict(target_month=target_month, as_of=_iso(cutoff), last_unburned=unburned,
        first_burned=burned, interval_status=status, burn_interval=interval,
        candidate_months=months, bounded_month=bounded, target_month_supported=supported,
        event_id=None, precise_burn_date=None, confirmed_new_burn_area_m2=None)
