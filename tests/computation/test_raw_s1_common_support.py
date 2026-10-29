"""Raw S1-30 regression; the arithmetic oracle does not emulate EE optimization."""
import hashlib
import json
import math

import numpy as np
import pytest

from geecomputation import raw_download as raw
from .test_observations import evaluate_s1_graph

SCENE=dict(collection='COPERNICUS/S1_GRD',asset_id='COPERNICUS/S1_GRD/one',
           system_index='one',time_ms=1596240000000)
GRID={'crsCode':'EPSG:32610','affineTransform':dict(scaleX=30,shearX=0,translateX=499980,
      shearY=0,scaleY=-30,translateY=4500000),'dimensions':dict(width=1,height=1)}


@pytest.fixture
def raw_offline_ee():
    import ee
    from ee import apitestcase
    helper=apitestcase.ApiTestCase();helper.setUp()
    try:yield
    finally:helper.tearDown();ee.Reset()


def test_only_raw_s1_30_recipe_changes_unit_identity():
    for profile in ('hls','s1-10','s1-30'):
        unit=raw.make_unit([SCENE],GRID,profile)
        legacy_identity=dict(sources=[SCENE['asset_id']],grid=GRID,profile=profile,
                             recipe='raw-full-support-v1')
        legacy_uid=hashlib.sha256(json.dumps(legacy_identity,sort_keys=True).encode()).hexdigest()[:32]
        assert unit['recipe']==('raw-s1-30-value-support-v2' if profile=='s1-30' else 'raw-full-support-v1')
        assert (unit['unit_id']!=legacy_uid)==(profile=='s1-30')


def test_old_s1_30_units_are_rejected_before_building_graph(monkeypatch):
    old=raw.make_unit([SCENE],GRID,'s1-30')
    old['recipe']='raw-full-support-v1'
    identity={key:old[key] for key in ('sources','grid','profile','recipe')}
    old['unit_id']=hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()[:32]
    monkeypatch.setattr(raw,'raw_image',lambda *args:pytest.fail('Legacy identity reached graph builder'))
    with pytest.raises(ValueError,match='recipe'):raw.unit_image(old)


@pytest.mark.parametrize('vv,vh,vv_ok,vh_ok,weights',[
    ([-10,-20],[-20,-30],[True,True],[True,True],[.4,.6]),  # full
    ([-10,-20],[-20,-30],[True,True],[True,False],[.4,.6]),  # common only
    ([-10,-20],[-20,-30],[False,False],[True,True],[.4,.6]),  # empty
    ([0],[-3],[True],[True],[.2]),  # valid zero dB + absent footprint area
])
def test_raw_power_uses_common_valid_area_mean(raw_offline_ee,vv,vh,vv_ok,vh_ok,weights):
    source={'VV':np.ma.array(vv,dtype=float,mask=np.logical_not(vv_ok)),
            'VH':np.ma.array(vh,dtype=float,mask=np.logical_not(vh_ok)),
            'angle':np.ma.array([35.]*len(weights))}
    output=evaluate_s1_graph(raw.raw_image(SCENE,GRID,'s1-30').serialize(),source,np.array(weights))
    # Independent scalar definition: overlap is divided by whole target area;
    # power is conditional on support common to both channels, not whole area.
    supported=[i for i in range(len(weights)) if vv_ok[i] and vh_ok[i]]
    fraction=sum(weights[i] for i in supported)
    assert float(output['valid_fraction'])==pytest.approx(fraction)
    assert float(output['source_valid_bits'])==(3 if fraction else 0)
    for name,db in [('VV_power',vv),('VH_power',vh)]:
        if not fraction:
            assert np.ma.is_masked(output[name])
        else:
            expected=math.fsum(weights[i]*10**(db[i]/10) for i in supported)/fraction
            assert float(output[name])==pytest.approx(expected)


def test_fraction_reduction_uses_value_carried_support(raw_offline_ee):
    graph=json.loads(raw.raw_image(SCENE,GRID,'s1-30').serialize())
    def resolve(node):
        return resolve(graph['values'][node['valueReference']]) if 'valueReference' in node else node
    fractions=[]
    for node in graph['values'].values():
        call=node.get('functionInvocationValue',{})
        if call.get('functionName')=='Image.rename' and resolve(call['arguments']['names']).get('constantValue')==['valid_fraction']:
            fractions.append(call['arguments']['input'])
    assert len(fractions)==1
    node=resolve(fractions[0])
    chain=[('Image.reproject','image'),('Image.reduceResolution','image'),
           ('Image.unmask','input'),('Image.updateMask','image'),
           ('Image.add','image1'),('Image.multiply','image1'),('Image.select','input')]
    for operation,argument in chain:
        call=node['functionInvocationValue'];assert call['functionName']==operation
        node=resolve(call['arguments'][argument])
    assert node['functionInvocationValue']['functionName']=='Image.load'
