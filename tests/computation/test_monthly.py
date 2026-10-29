"""Pure time-support contracts; all dates and references are synthetic."""
from copy import deepcopy

import pytest

from hpgee.compute.monthly import resolve_burn_month_support


def endpoint(time):
    return {'time': time, 'evidence_ref': {'caller_reference': 'unverified-state-support'}}


def resolve(u=None, b=None, *, target_month='2020-08', as_of='2020-09-01T00:00:00Z'):
    return resolve_burn_month_support(target_month=target_month, as_of=as_of,
        last_unburned=endpoint(u) if u else None, first_burned=endpoint(b) if b else None)


@pytest.mark.parametrize('u,b,months,bounded,supported', [
    ('2020-08-03T10:00Z', '2020-08-12T10:00Z', ['2020-08'], '2020-08', True),
    ('2020-07-30T10:00Z', '2020-08-02T10:00Z', ['2020-07', '2020-08'], None, None),
    ('2020-07-30T10:00Z', '2020-08-01T00:00Z', ['2020-07', '2020-08'], None, None),
    ('2020-08-01T00:00Z', '2020-08-12T10:00Z', ['2020-08'], '2020-08', True),
    ('2020-07-03T00:00Z', '2020-07-10T00:00Z', ['2020-07'], '2020-07', False),
    ('2020-06-30T00:00Z', '2020-07-02T00:00Z', ['2020-06', '2020-07'], None, False),
    ('2020-07-01T10:00Z', '2020-08-05T10:00Z', ['2020-07', '2020-08'], None, None),
])
def test_bounded_interval_and_month_support(u, b, months, bounded, supported):
    result = resolve(u, b)
    assert result['interval_status'] == 'bounded'
    assert result['burn_interval'] == {
        'start': result['last_unburned']['time'], 'end': result['first_burned']['time'],
        'start_inclusive': False, 'end_inclusive': True}
    assert result['candidate_months'] == months
    assert result['bounded_month'] == bounded
    assert result['target_month_supported'] is supported
    assert result['event_id'] is None
    assert result['precise_burn_date'] is None
    assert result['confirmed_new_burn_area_m2'] is None


@pytest.mark.parametrize('u,b,status', [
    (None, '2020-08-12T10:00Z', 'left_missing'),
    ('2020-08-03T10:00Z', None, 'right_missing'),
    (None, None, 'unknown'),
])
def test_missing_endpoints_remain_unknown(u, b, status):
    result = resolve(u, b)
    assert result['interval_status'] == status
    assert result['burn_interval'] is None
    assert result['candidate_months'] is None
    assert result['bounded_month'] is None
    assert result['target_month_supported'] is None
    assert (result['last_unburned'] is None) == (u is None)
    assert (result['first_burned'] is None) == (b is None)


def test_candidate_months_retain_project_boundary_and_year_transition():
    result = resolve('2017-12-31T12:00Z', '2018-01-02T12:00Z',
        target_month='2018-01', as_of='2018-02-01T00:00Z')
    assert result['candidate_months'] == ['2017-12', '2018-01']
    assert result['target_month_supported'] is None


def test_utc_offset_normalization_and_caller_reference_preservation():
    u = endpoint('2020-08-01T02:00:00+02:00')
    b = {'time': '2020-08-12T05:00:00-05:00', 'evidence_ref': 'opaque-local-reference'}
    original = deepcopy(u)
    result = resolve_burn_month_support(target_month='2020-08', as_of='2020-09-01T08:00:00+08:00',
        last_unburned=u, first_burned=b)
    assert result['as_of'] == '2020-09-01T00:00:00Z'
    assert result['last_unburned']['time'] == '2020-08-01T00:00:00Z'
    assert result['first_burned']['time'] == '2020-08-12T10:00:00Z'
    assert result['last_unburned']['evidence_ref'] == u['evidence_ref']
    assert result['first_burned']['evidence_ref'] == 'opaque-local-reference'
    result['last_unburned']['evidence_ref']['caller_reference'] = 'changed output'
    assert u == original


@pytest.mark.parametrize('side', ['last_unburned', 'first_burned'])
@pytest.mark.parametrize('time', ['2020-09-01T00:00Z', '2020-09-01T01:00Z'])
def test_as_of_is_exclusive_for_either_endpoint_even_without_other_side(side, time):
    with pytest.raises(ValueError, match='as_of'):
        resolve_burn_month_support(target_month='2020-08', as_of='2020-09-01T00:00Z',
            **{side: endpoint(time)})


def test_instant_before_as_of_is_allowed():
    result = resolve('2020-08-03T00:00Z', '2020-08-31T23:59:59.999999Z')
    assert result['target_month_supported'] is True


@pytest.mark.parametrize('u,b', [
    ('2020-08-03T10:00Z', '2020-08-03T10:00Z'),
    ('2020-08-04T10:00Z', '2020-08-03T10:00Z'),
    ('2020-08-03T12:00+02:00', '2020-08-03T10:00Z'),
])
def test_nonpositive_interval_is_rejected(u, b):
    with pytest.raises(ValueError, match='last_unburned.*first_burned'):
        resolve(u, b)


@pytest.mark.parametrize('record', [
    {'time': '2020-08-03T00:00Z'},
    {'time': '2020-08-03T00:00Z', 'evidence_ref': None},
    {'time': '2020-08-03T00:00Z', 'evidence_ref': ''},
    {'time': '2020-08-03T00:00Z', 'evidence_ref': '  '},
    {'time': '2020-08-03T00:00Z', 'evidence_ref': {}},
    {'time': '2020-08-03T00:00Z', 'evidence_ref': False},
])
@pytest.mark.parametrize('side', ['last_unburned', 'first_burned'])
def test_endpoint_requires_nonempty_caller_reference(record, side):
    with pytest.raises(ValueError, match='evidence_ref'):
        resolve_burn_month_support(target_month='2020-08', as_of='2020-09-01T00:00Z',
            **{side: record})


@pytest.mark.parametrize('time', ['2020-08-03', '2020-08-03T10:00', 'bad-time', None])
@pytest.mark.parametrize('field', ['as_of', 'last_unburned', 'first_burned'])
def test_all_clocks_require_explicit_timezone(time, field):
    arguments = dict(target_month='2020-08', as_of='2020-09-01T00:00Z')
    arguments[field] = time if field == 'as_of' else endpoint(time)
    with pytest.raises(ValueError, match='timezone'):
        resolve_burn_month_support(**arguments)


@pytest.mark.parametrize('month', ['2017-12', '2026-01', '2020-00', '2020-13', '2020-8', None])
def test_target_month_is_limited_to_project_calendar(month):
    with pytest.raises(ValueError, match='target_month'):
        resolve(target_month=month)
