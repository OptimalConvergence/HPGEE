"""Exact-source GEESampler adapter and offline saved-pixel acceptance.

The caller owns authorization, common budget reservations and local hot storage.
This module supplies callbacks and receipts, never another download scheduler.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import numpy as np
from hpgee.catalog.catalog import SceneRecord
from hpgee.transfer.requests import FrozenSceneRequest
from hpgee.workflows.config import utc_datetime
from hpgee.planning.cube import projected_support


def _file_sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def sampler_arguments(unit):
    """Arguments for the installed download_patch_series API: one frozen scene.

    Engine's inclusive day offsets cannot expand the source set because the
    callback contains exactly one source fixed by prior half-open enumeration.
    The explicit system:index avoids anonymous fromImages metadata identities.
    """
    from hpgee.core.models import SampleRecord, PatchGrid
    from rasterio.warp import transform_geom
    scene=deepcopy(unit['scene']);grid=deepcopy(unit['grid']);pixel=unit['pixel_grid']
    from hpgee.planning.observations import HLS_OBSERVATION_BANDS, S1_OBSERVATION_BANDS
    expected=S1_OBSERVATION_BANDS if scene['modality']=='S1' else HLS_OBSERVATION_BANDS
    if [b['name'] for b in unit['bands']]!=list(expected) or any(b['dtype']!='float32' for b in unit['bands']):
        raise ValueError('Sampler requires the complete float32 observation contract')
    recipe='s1-common-area-power-v1' if scene['modality']=='S1' else 'hls-raw-nearest-v1'
    if any(b['recipe']!=recipe for b in unit['bands']):
        raise ValueError('Sampler cannot produce the declared observation recipe')
    if not scene.get('system_index') or scene['asset_id']!=scene['collection']+'/'+scene['system_index']:
        raise ValueError('Exact frozen scene identity required')
    from shapely.geometry import box,mapping
    affine=grid['transform'];width=pixel['dimensions']['width'];height=pixel['dimensions']['height']
    geometry=transform_geom(grid['crs'],'EPSG:4326',mapping(box(affine[2],affine[5]-height*30,affine[2]+width*30,affine[5])))
    record=SampleRecord(unit['unit_id'],geometry,date=datetime.fromtimestamp(scene['time_ms']/1000,tz=timezone.utc),
        properties={'unit_id':unit['unit_id'],'original_source':scene['asset_id']})
    def collection_builder(sample):
        import ee
        from hpgee.planning.observations import observation_image
        image=observation_image(scene,grid=grid).set({'system:index':scene['system_index'],'system:time_start':scene['time_ms']})
        return ee.ImageCollection.fromImages([image])
    frozen_scene=SceneRecord(collection=scene['collection'],asset_id=scene['asset_id'],
        scene_id=scene['system_index'],acquired_millis=scene['time_ms'],mgrs_tile='',
        properties=scene.get('properties'))
    arguments=FrozenSceneRequest(record,frozen_scene,collection_builder,
        [b['name'] for b in unit['bands']],PatchGrid(size=max(width,height),scale=30),
        pixel).sampler_arguments()
    return dict(arguments,scenario='frozen-observation')


def _verify_unit(unit,receipt):
    import rasterio
    scene=unit['scene'];pixel=unit['pixel_grid'];path=Path(receipt['path'])
    if receipt.get('unit_id')!=unit['unit_id'] or receipt.get('scene_id')!=scene['system_index'] or receipt.get('scene_time_ms')!=scene['time_ms']:
        raise ValueError('Delivered scene identity/time differs from frozen source')
    if not path.is_file() or _file_sha(path)!=receipt.get('sha256'):
        raise ValueError('Saved artifact missing or SHA mismatch')
    bands=[b['name'] for b in unit['bands']]
    if receipt.get('band_order')!=bands:
        raise ValueError('Transport band order differs from frozen units/bands')
    with rasterio.open(path) as ds:
        transform=pixel['affineTransform'];expected=tuple(transform[k] for k in ('scaleX','shearX','translateX','shearY','scaleY','translateY'))
        if ds.crs.to_string()!=pixel['crsCode'] or tuple(ds.transform)[:6]!=expected or ds.width!=pixel['dimensions']['width'] or ds.height!=pixel['dimensions']['height']:
            raise ValueError('Saved grid/affine differs from frozen chip')
        if ds.count!=len(bands) or list(ds.dtypes)!=[b['dtype'] for b in unit['bands']]:
            raise ValueError('Saved band count/dtype differs from contract')
        if any(name is not None and name!=bands[i] for i,name in enumerate(ds.descriptions)):
            raise ValueError('Saved band descriptions contradict transport order')
        data=ds.read(masked=True)
        valid=~np.ma.getmaskarray(data)&np.isfinite(data.data)
        lookup={name:i for i,name in enumerate(bands)}
        observed=valid[0].copy()
        if 'source_valid_bits' in lookup:
            bits=data.data[lookup['source_valid_bits']]
            if not valid[lookup['source_valid_bits']].all() or np.any(bits!=np.floor(bits)):
                raise ValueError('Missing/noninteger source validity bits')
            if scene['modality']=='HLS':
                if np.any((bits<0)|(bits>127)):
                    raise ValueError('Invalid HLS source validity bits')
                for index,name in enumerate(('blue','green','red','nir','swir1','swir2','Fmask')):
                    if name in lookup and np.any(((bits.astype('uint8')>>index)&1).astype(bool)&~valid[lookup[name]]):
                        raise ValueError('Source validity bit lacks saved band support')
                qa_valid=(bits.astype('uint8')&64)!=0
                observed=(bits.astype('uint8')&63)==63
                if 'clear' in lookup and 'Fmask' in lookup:
                    from hpgee.compute.features import clear_fmask
                    clear=data.data[lookup['clear']];qa=data.data[lookup['Fmask']]
                    if not valid[lookup['clear']].all() or np.any(clear[~qa_valid]!=-1):
                        raise ValueError('Unknown Fmask must have clear=-1')
                    if np.any((qa[qa_valid]<0)|(qa[qa_valid]>255)|(qa[qa_valid]!=np.floor(qa[qa_valid]))):
                        raise ValueError('Invalid Fmask values')
                    if np.any(clear[qa_valid]!=clear_fmask(qa[qa_valid]).astype('int8')):
                        raise ValueError('clear differs from source Fmask')
            elif scene['modality']=='S1':
                if np.any(((bits.astype('uint8')&3)!=0)&((bits.astype('uint8')&3)!=3)):
                    raise ValueError('S1 VV/VH bits must identify common support')
                if np.any((bits<0)|(bits>7)):
                    raise ValueError('Invalid S1 source validity bits')
                for index,name in enumerate(('VV_power','VH_power','angle')):
                    if name in lookup and np.any(((bits.astype('uint8')>>index)&1).astype(bool)&~valid[lookup[name]]):
                        raise ValueError('S1 validity lacks saved band support')
                observed=(bits.astype('uint8')&3)==3
                if 'valid_fraction' in lookup:
                    fraction=data.data[lookup['valid_fraction']]
                    if not valid[lookup['valid_fraction']].all() or np.any((fraction<0)|(fraction>1)) or np.any((fraction>0)!=observed):
                        raise ValueError('S1 full-pixel valid fraction differs from common support')
                for name in ('VV_power','VH_power'):
                    if name in lookup and np.any(data.data[lookup[name]][observed]<=0):
                        raise ValueError('S1 power must be positive on observed support')
        return dict(path=str(path),observed=observed,transform=ds.transform,shape=(ds.height,ds.width))


def verify_delivery(index):
    """Read saved pixels over every original support; missing stays partial.

    Acquisition clocks/scene identity are exact transport receipt metadata, not
    inferred burning dates. All-invalid sources are observed absence of support,
    and a complete zero-scene query is reported separately as no_observation.
    """
    from rasterio.features import rasterize
    from shapely.geometry import mapping
    plan=index['plan'];receipts={}
    unit_ids={u['unit_id'] for u in plan['units']}
    for row in index.get('deliveries',[]):
        if row['unit_id'] not in unit_ids or row['unit_id'] in receipts:
            raise ValueError('Duplicate or foreign physical unit receipt')
        receipts[row['unit_id']]=row
    verified={};failures=[]
    for unit in plan['units']:
        receipt=receipts.get(unit['unit_id'])
        if not receipt or receipt.get('status') in ('failed','not_executed'):
            failures.append(dict(unit_id=unit['unit_id'],status=receipt.get('status') if receipt else 'not_executed'))
            continue
        verified[unit['unit_id']]=_verify_unit(unit,receipt)
    supports=[]
    for support in plan['supports']:
        requested=observed=0;missing=[]
        geometry=projected_support(support,plan['grid']['crs'])
        for uid in support['unit_ids']:
            unit=next(u for u in plan['units'] if u['unit_id']==uid)
            from affine import Affine
            transform=Affine(*unit['grid']['transform'])
            shape=(unit['pixel_grid']['dimensions']['height'],unit['pixel_grid']['dimensions']['width'])
            mask=rasterize([(mapping(geometry),1)],out_shape=shape,transform=transform,fill=0,dtype='uint8').astype(bool)
            requested+=int(mask.sum())
            if uid not in verified:
                missing.append(uid)
            else:
                observed+=int((mask&verified[uid]['observed']).sum())
        supports.append(dict(support_id=support['support_id'],original_draw_id=support.get('original_draw_id'),kind=support.get('kind'),
            requested_pixels=requested,observed_pixels=observed,missing_unit_ids=missing,
            status='no_observation' if not support['unit_ids'] else 'partial' if missing else 'complete',
            mask_reference=support.get('mask_reference'),label_eligibility=support.get('label_eligibility')))
    return dict(schema_version='delivery-verification-v1',plan_id=plan['plan_id'],status='partial' if failures else 'complete',
        verified_units=len(verified),missing_or_failed=failures,supports=supports,
        scientific_label_eligibility_changed=False,limitation='Consistency/coverage QA is not independent scientific validation.')


def _deliver_unit(unit,*,engine,hot_dir,archive_dir,trial_id):
    """Run/recover one physical unit using the existing GEESampler scheduler.

    A new explicit trial has an independent run directory/ledger. Same-trial
    recovery verifies archived identity and bytes before returning a cache hit.
    Parent must reserve estimated EECU/bytes before invoking this function.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]+',trial_id):
        raise ValueError('Explicit safe trial_id required')
    if Path(engine.config.output_dir).resolve()!=Path(hot_dir).resolve():
        raise ValueError('Sampler must write to configured allocated hot-dir')
    root=Path(archive_dir)/trial_id;root.mkdir(parents=True,exist_ok=True)
    target=root/(unit['unit_id']+'.tif');receipt_path=root/(unit['unit_id']+'.json')
    if receipt_path.exists():
        previous=json.loads(receipt_path.read_text())
        if Path(previous.get('path','')).resolve()!=target.resolve():
            raise ValueError('Archive receipt path differs from its fixed unit/trial destination')
        if target.exists():
            _verify_unit(unit,previous)
            return dict(previous,local_cache_hit=True,network_bytes=0,new_archive_bytes=0)
    args=sampler_arguments(unit)
    from hpgee.transfer.engine import make_workload_tag
    request_identity=dict(archive_dir=str(Path(archive_dir).resolve()),trial_id=trial_id,unit_id=unit['unit_id'])
    sampler_run_id=hashlib.sha256(json.dumps(request_identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:32]
    workload_tag=make_workload_tag(engine.config.workload_prefix,args['scenario'],sampler_run_id)
    if sampler_run_id not in workload_tag:
        raise ValueError('Sampler workload tag truncates request identity; shorten workload_prefix')
    summary=engine.download_patch_series(**args,run_id=sampler_run_id)
    if summary.failed or len(summary.results)!=1:
        raise RuntimeError('Missing/failed frozen-scene delivery; preserve engine failure receipt')
    result=summary.results[0];scene=unit['scene']
    if result.status not in ('success','skipped') or result.scene_id!=scene['system_index'] or not result.scene_date or round(utc_datetime(result.scene_date).timestamp()*1000)!=scene['time_ms']:
        raise ValueError('Sampler scene identity/time differs; metadata-missing fallback is not accepted')
    source=Path(result.output_path);digest=_file_sha(source)
    receipt=dict(schema_version='observation-delivery-v1',unit_id=unit['unit_id'],trial_id=trial_id,
        path=str(target.resolve()),sha256=digest,status='verified',scene_id=scene['system_index'],scene_time_ms=scene['time_ms'],
        band_order=[b['name'] for b in unit['bands']],workload_tag=summary.workload_tag,
        sampler_run_id=sampler_run_id,request_identity=request_identity,
        workload_tag_scope='actual sampler run/shard; shared tag costs are not per-unit measurements',
        network_bytes=result.bytes_downloaded,local_cache_hit=result.status=='skipped',new_archive_bytes=0)
    _verify_unit(unit,dict(receipt,path=str(source)))
    if target.exists():
        if _file_sha(target)!=digest:
            raise ValueError('Existing archive SHA differs from recovered source; refusing overwrite')
    else:
        temporary=target.with_suffix('.tif.partial');shutil.copy2(source,temporary);os.replace(temporary,target)
        receipt['new_archive_bytes']=target.stat().st_size
    temporary=receipt_path.with_suffix('.json.partial')
    temporary.write_text(json.dumps(receipt,sort_keys=True,indent=2)+'\n');os.replace(temporary,receipt_path)
    return receipt


def deliver_unit(unit,*,engine,hot_dir,archive_dir,trial_id):
    """Serialize same-unit recovery while leaving GEESampler scheduling intact."""
    from hpgee.accounting.tasks import _locked
    if not re.fullmatch(r'[A-Za-z0-9_-]+',trial_id):
        raise ValueError('Explicit safe trial_id required')
    if not re.fullmatch(r'[a-f0-9]{64}',unit['unit_id']):
        raise ValueError('Expected hashed physical unit identity')
    with _locked(Path(archive_dir)/trial_id/(unit['unit_id']+'.json')):
        return _deliver_unit(unit,engine=engine,hot_dir=hot_dir,archive_dir=archive_dir,trial_id=trial_id)
