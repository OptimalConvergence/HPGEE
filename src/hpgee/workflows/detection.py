"""One bounded samples → features → RF → products workflow; no model assets."""
import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import numpy as np

from hpgee.workflows.config import Config, utc_datetime
from hpgee.compute.features import RF_BANDS, DEM_BANDS, feature_image, hls_collection, min_nbr2_composite, rf_image, dem_image
from hpgee.compute.rf import input_samples, train_rf
from hpgee.workflows.contracts import (
    PRODUCT_BANDS, QUALITY_BANDS as QUALITY_BANDS, _threshold,
    product_collection as product_collection, workflow_contract,
)


def candidate_pixels(scores, observed, threshold=.5):
    """Saved-pixel oracle: unknown remains masked, including score NaN."""
    values=np.asarray(scores,dtype=float)
    return np.ma.array((values>=_threshold(threshold)).astype('uint8'),
                       mask=~np.asarray(observed,dtype=bool)|~np.isfinite(values))


def output_region(cfg):
    import ee
    # Rectangular output storage support, not the independent WGS84 query bbox.
    a,_,x,_,e,y=cfg.grid['transform']
    return ee.Geometry.Rectangle([x,y+e*cfg.grid['height'],x+a*cfg.grid['width'],y],
                                 proj=cfg.grid['crs'],geodesic=False)


def boundaries_from_raster(cfg,raster,threshold=.5):
    import ee
    candidate=ee.Image(raster).select('candidate').toInt8().selfMask()
    return candidate.reduceToVectors(geometry=output_region(cfg),crs=cfg.grid['crs'],
        crsTransform=cfg.grid['transform'],geometryType='polygon',eightConnected=True,
        labelProperty='candidate',maxPixels=cfg.grid['width']*cfg.grid['height'],
        geometryInNativeProjection=False).map(lambda f:f.set('threshold',_threshold(threshold)))


def build_products(cfg,scores,threshold=.5,*,pre=None,post=None,source_manifest_sha256=None,aux=None,aux_context_sha256=None):
    import ee
    frozen=pre is not None or post is not None or source_manifest_sha256 is not None
    if frozen and (pre is None or post is None or source_manifest_sha256 is None):
        raise ValueError('Frozen input requires pre, post and source_manifest_sha256 together')
    if (aux is None)!=(aux_context_sha256 is None):
        raise ValueError('Frozen auxiliary requires aux and aux_context_sha256 together')
    contract=workflow_contract(cfg,threshold,source_manifest_sha256=source_manifest_sha256,aux_context_sha256=aux_context_sha256)
    if not frozen:
        pre=min_nbr2_composite(hls_collection(cfg,start=cfg.pre_start,end=cfg.pre_end),cfg)
        post=min_nbr2_composite(hls_collection(cfg),cfg)
    core=rf_image(pre,post)
    observed=core.mask().reduce(ee.Reducer.min())
    scores=scores.select(['score_unburned','score_burned']).updateMask(observed)
    candidate=scores.select('score_burned').gte(contract['threshold']).rename('candidate').toUint8().updateMask(observed)
    quality=ee.Image.cat([image.select(['valid_count','time_ms','Fmask','clear'],
              [f'{period}_{band}' for band in ('valid_count','time_ms','Fmask','clear')])
              for period,image in [('pre',pre),('post',post)]])
    aux=(dem_image(cfg) if aux is None else ee.Image(aux).select(list(DEM_BANDS))).rename([f'aux_dem_{band}' for band in DEM_BANDS])
    raster=(scores.addBands(candidate).addBands(core).addBands(quality).addBands(aux)
            .select(list(PRODUCT_BANDS)).toDouble().setDefaultProjection(cfg.grid['crs'],cfg.grid['transform'])
            .set('system:index','workflow_product').set('system:time_start',int(utc_datetime(cfg.start).timestamp()*1000))
            .set('contract_sha256',contract['contract_sha256']))
    return dict(scores=scores,candidate=candidate,core6=core,quality=quality,aux=aux,raster=raster,
                boundaries=boundaries_from_raster(cfg,raster,threshold),contract=contract,bands=list(PRODUCT_BANDS))


def build_workflow(cfg,samples=None,threshold=.5):
    source=None
    if samples is None: samples,source=input_samples(cfg)
    model,scores,metadata=train_rf(samples,cfg)
    return dict(build_products(cfg,scores,threshold),model=model,metadata=dict(metadata,input_source=source))


def sample_points(cfg,points_path):
    """Sample authorized labeled point supports; preserve provenance, infer no labels.

    Masked core observations are dropped. Auxiliary DEM missingness is filled
    only for sampling (-9999 sentinel), so it never changes the core cohort.
    Existing RF validator checks the frozen result before any training.
    """
    import ee
    path=Path(points_path);payload=path.read_bytes()
    cap=cfg.labels.get('max_samples',5000)
    if len(payload)>10*1024*1024: raise ValueError('point supports exceed 10 MiB')
    data=json.loads(payload);points=data.get('features',[]) if data.get('type')=='FeatureCollection' else []
    if not points or len(points)>cap: raise ValueError('point supports empty or exceed max_samples')
    required=('label','event_id','support_id','split','label_source','label_eligible')
    for f in points:
        if f.get('geometry',{}).get('type')!='Point' or any(k not in f.get('properties',{}) for k in required):
            raise ValueError('labeled Point supports require explicit source/eligibility/group properties')
        if any(k in f['properties'] for k in (*RF_BANDS,*DEM_BANDS)):
            raise ValueError('point supports must not contain stale feature properties')
    pre=min_nbr2_composite(hls_collection(cfg,start=cfg.pre_start,end=cfg.pre_end),cfg)
    post=min_nbr2_composite(hls_collection(cfg),cfg)
    image=(feature_image(cfg).select(list(RF_BANDS))
           .addBands(dem_image(cfg).unmask(-9999))
           .addBands(pre.select('valid_count').rename('pre_valid_count'))
           .addBands(post.select('valid_count').rename('post_valid_count')))
    # EE getInfo() adds geodesic=False to Point GeoJSON, but the Python
    # Point constructor does not accept that polygon-only argument.
    normalized=[dict(f,geometry={k:v for k,v in f['geometry'].items() if k!='geodesic'}) for f in points]
    sampled=image.sampleRegions(collection=ee.FeatureCollection(normalized),
        projection=ee.Projection(cfg.grid['crs'],cfg.grid['transform']),geometries=True)
    return sampled,{'kind':'authorized_point_supports','path':str(path.resolve()),
                   'sha256':hashlib.sha256(payload).hexdigest(),'point_count':len(points),
                   'sampling_grid':cfg.pixel_grid(),'aux_missing_sentinel':-9999,
                   'label_policy':'preserve original properties; no new eligibility or ground truth assigned',
                   'sampled_count':'bounded materialization required; masked core points omitted'}


def prepare_exports(cfg,products,*,asset_id=None,drive_folder=None):
    """Prepare only; use tasks.submit_ee_export for journaled submission/recovery."""
    import ee
    tasks={}
    grid=dict(region=output_region(cfg),crs=cfg.grid['crs'],crsTransform=cfg.grid['transform'],
              maxPixels=cfg.grid['width']*cfg.grid['height']*len(PRODUCT_BANDS))
    if asset_id:
        tasks['image']=ee.batch.Export.image.toAsset(image=products['raster'],assetId=asset_id,
            description='workflow-product',pyramidingPolicy={'.default':'sample'},**grid)
    elif drive_folder:
        tasks['image']=ee.batch.Export.image.toDrive(image=products['raster'],folder=drive_folder,
            description='workflow-product',fileFormat='GeoTIFF',**grid)
    if drive_folder:
        tasks['table']=ee.batch.Export.table.toDrive(collection=products['boundaries'],folder=drive_folder,
            description='workflow-boundaries',fileFormat='GeoJSON')
    return tasks


def sample_grid(sample,grid_spec,*,cfg):
    return cfg.pixel_grid()


def bounded_boundaries(collection,cap=2000):
    if type(cap) is not int or not 1<=cap<=10000: raise ValueError('boundary cap must be integer in [1,10000]')
    rows=collection.toList(cap+1).getInfo()
    if len(rows)>cap: raise ValueError('boundary cap exceeded; use prepared table export, do not accept partial geometry')
    return {'type':'FeatureCollection','features':rows}


def boundaries_from_saved_raster(raster_path,*,threshold=.5,cap=2000):
    """Polygonize the delivered candidate mask on its frozen grid."""
    import rasterio
    from rasterio.features import shapes
    from rasterio.warp import transform_geom
    if type(cap) is not int or not 1<=cap<=10000:
        raise ValueError('boundary cap must be integer in [1,10000]')
    features=[]
    with rasterio.open(raster_path) as ds:
        band=ds.descriptions.index('candidate')+1
        pixels=ds.read(band,masked=True).filled(0).astype('uint8')
        for geometry,value in shapes(pixels,mask=pixels.astype(bool),
                                     connectivity=8,transform=ds.transform):
            if value!=1: raise ValueError('candidate mask is not binary')
            if len(features)>=cap:
                raise ValueError('boundary cap exceeded; do not accept partial geometry')
            features.append({'type':'Feature',
                             'properties':{'candidate':1,'threshold':_threshold(threshold)},
                             'geometry':transform_geom(ds.crs,'EPSG:4326',geometry)})
    return {'type':'FeatureCollection','features':features}


def finalize_transfer(source,destination,contract,*,transfer_bands):
    """Package an EE TIFF whose descriptions may be absent; retain raw transfer.

    Names bind to the frozen computePixels bandIds, not inferred pixel values.
    Existing contradictory names or a changed request order are rejected.
    """
    import rasterio
    import shutil
    import os
    source,destination=Path(source),Path(destination)
    expected=contract['raster_bands']
    if list(transfer_bands)!=expected:
        raise ValueError('computePixels bandIds differ from frozen band order')
    with rasterio.open(source) as ds:
        if ds.count!=len(expected) or any(dtype!='float64' for dtype in ds.dtypes):
            raise ValueError('transferred band count/dtype differs from contract')
        if any(name is not None and name!=expected[i] for i,name in enumerate(ds.descriptions)):
            raise ValueError('transferred band descriptions contradict frozen bandIds')
    temporary=destination.with_suffix(destination.suffix+'.packaging')
    shutil.copy2(source,temporary)
    with rasterio.open(temporary,'r+') as ds:
        ds.descriptions=tuple(expected)
    os.replace(temporary,destination)
    return {'band_identity_source':'frozen computePixels bandIds order',
            'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'packaged_sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),
            'raw_transfer_preserved':source.resolve()!=destination.resolve()}


def verify_pixels(raster_path,contract,*,allow_no_observation=False):
    """Strict saved pixel QA, independently of vector completeness.

    Explicit no_observation is allowed only when requested; legacy acceptance
    still rejects it. Missing core support never becomes an unburned value.
    """
    import rasterio
    if type(allow_no_observation) is not bool:raise ValueError('allow_no_observation must be an explicit bool')
    raster_path=Path(raster_path)
    if not raster_path.is_file(): raise ValueError('missing raster artifact')
    grid=contract['grid'];expected=contract['raster_bands']
    with rasterio.open(raster_path) as ds:
        affine=grid['affineTransform'];want=tuple(affine[k] for k in ('scaleX','shearX','translateX','shearY','scaleY','translateY'))
        if ds.crs.to_string()!=grid['crsCode'] or tuple(ds.transform)[:6]!=want or ds.width!=grid['dimensions']['width'] or ds.height!=grid['dimensions']['height']:
            raise ValueError('saved raster grid differs from frozen contract')
        if any(dtype!='float64' for dtype in ds.dtypes): raise ValueError('saved product dtype differs from float64 contract')
        if ds.count!=len(expected) or list(ds.descriptions)!=expected:
            raise ValueError('saved raster band order differs from frozen contract')
        arrays=ds.read(masked=True);arrays=np.ma.masked_invalid(arrays)
        index={name:i for i,name in enumerate(expected)}
        core_valid=~np.ma.getmaskarray(arrays[[index[b] for b in RF_BANDS]]).any(axis=0)
        score=arrays[index['score_burned']];candidate=arrays[index['candidate']]
        for name in ('score_unburned','score_burned','candidate'):
            if not np.array_equal(~np.ma.getmaskarray(arrays[index[name]]),core_valid):
                raise ValueError('score/candidate mask differs from observed core support')
        if not core_valid.any() and not allow_no_observation: raise ValueError('no observed prediction pixels; not accepted')
        unburned=arrays[index['score_unburned']]
        for probability in (score,unburned):
            if np.any((probability.compressed()<0)|(probability.compressed()>1)):
                raise ValueError('binary probabilities outside [0,1]')
        if not np.allclose(score.compressed()+unburned.compressed(),1,rtol=0,atol=1e-6):
            raise ValueError('binary probabilities do not sum to one')
        oracle=candidate_pixels(score.filled(np.nan),core_valid,contract['threshold'])
        if not np.array_equal(candidate.compressed(),oracle.compressed()):
            raise ValueError('candidate differs from declared score threshold')
        for period in ('pre','post'):
            layers={name:arrays[index[f'{period}_{name}']] for name in ('valid_count','time_ms','Fmask','clear')}
            if any(np.ma.getmaskarray(layer)[core_valid].any() for layer in layers.values()):
                raise ValueError('observed core lacks observation quality values')
            values={name:layer.data[core_valid] for name,layer in layers.items()}
            counts=values['valid_count']
            if np.any((counts<1)|(counts!=np.floor(counts))):
                raise ValueError('invalid observation count')
            window=contract['recipe']['pre_interval_utc' if period=='pre' else 'interval_utc']
            start,end=[utc_datetime(window[key]).timestamp()*1000 for key in ('start','end')]
            clocks=values['time_ms']
            if np.any((clocks<start)|(clocks>=end)|(clocks!=np.floor(clocks))):
                raise ValueError('observation time outside its UTC half-open interval')
            qa=values['Fmask']
            from hpgee.compute.features import clear_fmask
            if np.any((qa<0)|(qa>255)|(qa!=np.floor(qa))) or not clear_fmask(qa).all() or not np.all(values['clear']==1):
                raise ValueError('observation quality differs from frozen Fmask/clear rule')
    return dict(status='verified_saved_pixels' if core_valid.any() else 'no_observation',
        observed_pixels=int(core_valid.sum()),unknown_pixels=int(core_valid.size-core_valid.sum()),
        candidate_pixels=int(candidate.filled(0).sum()),independent_accuracy_acceptance=False)


def verify_artifacts(raster_path,boundary_path,contract):
    """Check saved grid/bands/masks and exact candidate union from actual GeoJSON."""
    import rasterio
    from rasterio.features import rasterize, bounds as geometry_bounds
    from rasterio.warp import transform_geom
    raster_path,boundary_path=Path(raster_path),Path(boundary_path)
    if not raster_path.is_file(): raise ValueError('missing raster artifact')
    if not boundary_path.is_file(): raise ValueError('missing boundary artifact')
    pixel_qa=verify_pixels(raster_path,contract)
    with rasterio.open(raster_path) as ds:
        candidate=np.ma.masked_invalid(ds.read(ds.descriptions.index('candidate')+1,masked=True))
        data=json.loads(boundary_path.read_text())
        if data.get('type')!='FeatureCollection' or not isinstance(data.get('features'),list):
            raise ValueError('invalid or partial GeoJSON boundary artifact')
        shapes=[]
        for f in data['features']:
            if f.get('properties',{}).get('threshold')!=contract['threshold'] or f.get('geometry',{}).get('type') not in ('Polygon','MultiPolygon'):
                raise ValueError('invalid boundary geometry or threshold')
            geometry=transform_geom('EPSG:4326',ds.crs,f['geometry'])
            west,south,east,north=geometry_bounds(geometry)
            # Allow millimetre-level inverse-projection roundoff at grid edges.
            if west<ds.bounds.left-1e-3 or south<ds.bounds.bottom-1e-3 or east>ds.bounds.right+1e-3 or north>ds.bounds.top+1e-3:
                raise ValueError('boundary geometry lies outside output grid')
            shapes.append((geometry,1))
        union=rasterize(shapes,out_shape=(ds.height,ds.width),transform=ds.transform,fill=0,dtype='uint8') if shapes else np.zeros((ds.height,ds.width),dtype='uint8')
        if not np.array_equal(union,candidate.filled(0).astype('uint8')):
            raise ValueError('boundary union differs from raster candidate; missing/partial output rejected')
    return {'status':'verified_saved_products','observed_pixels':pixel_qa['observed_pixels'],
            'candidate_pixels':int(union.sum()),'boundary_features':len(shapes),
            'payload_bytes':raster_path.stat().st_size+boundary_path.stat().st_size,
            'independent_accuracy_acceptance':False}


class StageRecorder:
    """Sequential request wall and partial telemetry; graph requests may recompute."""
    def __init__(self,cfg,output,*,run_id=None,profile=True):
        self.cfg=cfg;self.output=Path(output);self.profile=profile
        self.output.mkdir(parents=True,exist_ok=True)
        path=self.output/'stages.json'
        existing=json.loads(path.read_text()) if path.exists() else {}
        self.run_id=run_id or existing.get('run_id') or hashlib.sha256(str(self.output.resolve()).encode()).hexdigest()
        if existing.get('run_id') not in (None,self.run_id):
            raise ValueError('stage output belongs to another run')
        self.rows=existing.get('stages',[])

    @contextmanager
    def stage(self,name,*,evaluates_graph=False,unit_id=None):
        import ee
        from hpgee.accounting.usage import make_workload_tag
        unit_id=unit_id or name
        attempt=1+sum(r.get('unit_id',r['stage'])==unit_id and r['stage']==name for r in self.rows)
        tag=make_workload_tag(self.run_id,unit_id,name,attempt)
        old_tag=ee.data.getWorkloadTag()
        ee.data.setWorkloadTag(tag)
        row={'run_id':self.run_id,'unit_id':unit_id,'client_attempt':attempt,'stage':name,'workload_tag':tag,'start':datetime.now(timezone.utc).isoformat(),
             'evaluates_lazy_graph':evaluates_graph,'payload_bytes':None,'reported_eecu_seconds':None,
             'eecu_seconds':None,'telemetry_complete':False,
             'cost_scope':'entire evaluated dependencies; overlapping graph work, never pure train time'}
        tick=time.perf_counter()
        try:
            with (self.output/(tag+'-profile.txt')).open('w') as stream, (ee.profilePrinting(destination=stream) if self.profile else nullcontext()):
                yield row
            row['state']='completed'
        except Exception as exc:
            row.update(state='failed',error_type=type(exc).__name__)
            raise
        finally:
            ee.data.setWorkloadTag(old_tag)
            row.update(end=datetime.now(timezone.utc).isoformat(),wall_seconds=time.perf_counter()-tick)
            self.rows.append(row)
            _write(self.output/'stages.json',{'schema_version':2,'run_id':self.run_id,'stages':self.rows,'sum_pure_train_seconds':None,
                'eecu_seconds':None,'telemetry_complete':False,
                'limitation':'profiles are request diagnostics; reported usage delayed; do not sum overlapping usage metrics'})

    def refresh_usage(self,*,end):
        from hpgee.accounting.usage import read_usage
        for row in self.rows:
            usage=read_usage(self.cfg.project,start=row['start'],end=end,workload_tag=row['workload_tag'])
            row.setdefault('usage_observations',[]).append(usage)
            row['reported_usage']=usage
            hours=usage.get('guard_hours')
            row['reported_eecu_seconds']=hours*3600 if hours is not None else None
            row['reported_eecu_basis']='maximum of overlapping completed/in-progress views; partial'
        _write(self.output/'usage-observations.json',{'stages':self.rows,'eecu_seconds':None,'telemetry_complete':False})


def _write(path,value):
    Path(path).write_text(json.dumps(value,indent=2,allow_nan=False)+'\n')


def main(argv=None, *, prog='geecompute-workflow'):
    parser=argparse.ArgumentParser(prog=prog)
    parser.add_argument('--config',required=True);parser.add_argument('--threshold',type=float,default=.5)
    parser.add_argument('--sample-points');parser.add_argument('--boundary-cap',type=int,default=2000)
    parser.add_argument('--transfer',action='store_true');parser.add_argument('--hot-dir')
    parser.add_argument('--usage-end');parser.add_argument('--manifest-only',action='store_true')
    args=parser.parse_args(argv)
    cfg=Config.load(args.config);contract=workflow_contract(cfg,args.threshold,sample_points_path=args.sample_points)
    cfg.output.mkdir(parents=True,exist_ok=True)
    path=cfg.output/'workflow-contract.json'
    if path.exists() and json.loads(path.read_text())['contract_sha256']!=contract['contract_sha256']:
        raise ValueError('output belongs to different workflow inputs; use fresh output')
    _write(path,contract)
    if args.manifest_only:
        print(json.dumps(contract,indent=2));return 0
    if args.transfer and not args.hot_dir: raise ValueError('--transfer requires --hot-dir on allocated local storage')
    from hpgee.cli.computation import initialize
    # Interactive EE has a five-minute ceiling; let EE report its result/error
    # before the client abandons a cold lazy RF graph at 60 seconds.
    ee=initialize(cfg,timeout=330 if args.transfer else 60);stages=StageRecorder(cfg,cfg.output)
    if args.sample_points:
        with stages.stage('sample-build',evaluates_graph=True) as row:
            samples,source=sample_points(cfg,args.sample_points)
            records=samples.toList(cfg.labels.get('max_samples',5000)+1).getInfo()
            if len(records)>cfg.labels.get('max_samples',5000): raise ValueError('sample cap exceeded')
            sampled=cfg.output/'sampled-samples.geojson'
            _write(sampled,{'type':'FeatureCollection','features':records})
            row.update(payload_bytes=sampled.stat().st_size,input_source=source,sample_count=len(records))
        labels=dict(cfg.labels);labels.pop('asset',None);labels.pop('expected_sample_sha256',None)
        labels['local_samples']=str(sampled);cfg=replace(cfg,labels=labels)
    with stages.stage('feature-train-graph',evaluates_graph=True) as row:
        products=build_workflow(cfg,threshold=args.threshold)
        row['note']='sample validation RPC; train and feature graph lazy until evaluation'
        _write(cfg.output/'training.json',products['metadata'])
        _write(cfg.output/'effective-workflow-contract.json',products['contract'])
        (cfg.output/'product-graph.json').write_text(products['raster'].serialize()+'\n')
        _write(cfg.output/'export-plan.json',{'contract':products['contract'],'prepared_only':True,
            'image':'prepare_exports(...,asset_id=...) → tasks.submit_ee_export; no model asset',
            'table':'prepare_exports(...,drive_folder=...) → tasks.submit_ee_export'})
    boundary=cfg.output/'boundaries.geojson'
    if not args.transfer:
        with stages.stage('boundary',evaluates_graph=True) as row:
            _write(boundary,bounded_boundaries(products['boundaries'],args.boundary_cap))
            row['payload_bytes']=boundary.stat().st_size
    if args.transfer:
        if not args.hot_dir: raise ValueError('--transfer requires --hot-dir on allocated local storage')
        from hpgee.transfer.engine import DownloadEngine
        from hpgee.core.models import RunConfig, EECUMonitorConfig
        from hpgee.transfer.products import product_sampler_arguments
        with stages.stage('transfer',evaluates_graph=True) as row:
            engine=DownloadEngine(cfg.project,RunConfig(output_dir=Path(args.hot_dir),workers=1,retries=1,
                workload_prefix=cfg.workload_tag or 'geecompute',eecu=EECUMonitorConfig(enabled=False)),ee_module=ee)
            summary=engine.download_patch_series(
                **product_sampler_arguments(cfg,products,sample_id='workflow'),scenario='workflow')
            _write(cfg.output/'sampler-summary.json',asdict(summary))
            row['stage_request_workload_tag']=row['workload_tag']
            row['workload_tag']=summary.workload_tag
            files=[Path(r.output_path) for r in summary.results if r.status in ('success','skipped') and r.output_path]
            if summary.failed or len(files)!=1 or not files[0].is_file(): raise ValueError('missing/partial sampler products')
            import shutil
            raw=cfg.output/'product.raw.tif'
            shutil.copy2(files[0],raw)
            raster=cfg.output/'product.tif'
            row['packaging']=finalize_transfer(raw,raster,products['contract'],transfer_bands=products['bands'])
            row['payload_bytes']=raster.stat().st_size
        with stages.stage('boundary-from-saved-raster') as row:
            _write(boundary,boundaries_from_saved_raster(raster,threshold=args.threshold,cap=args.boundary_cap))
            row['payload_bytes']=boundary.stat().st_size
        with stages.stage('verify') as row:
            accepted=verify_artifacts(raster,boundary,products['contract']);_write(cfg.output/'verification.json',accepted)
    if args.usage_end: stages.refresh_usage(end=args.usage_end)
    print(json.dumps({'output':str(cfg.output),'saved_pixels_verified':args.transfer,'scientific_accuracy_accepted':False}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
