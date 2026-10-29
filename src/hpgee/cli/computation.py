"""Explicit synchronous requests; never browser-authenticate or submit export tasks."""
import argparse
import json
import re
from datetime import datetime,timezone
from pathlib import Path
import urllib.request
from hpgee.workflows.config import Config
from hpgee.compute.features import HLS_COLLECTIONS, DEM_COLLECTION, region, hls_collection, min_nbr2_composite
from hpgee.workflows.recipe import manifest


def initialize(cfg, *, timeout=60):
    from hpgee.core.models import AuthConfig
    from hpgee.runtime.auth import initialize_earth_engine
    allowed={'service_account','key_file','high_volume'}
    if set(cfg.auth)-allowed: raise ValueError('unsupported auth configuration field')
    ee=initialize_earth_engine(AuthConfig(project=cfg.project,**cfg.auth),pool_size=2,timeout=timeout)
    if cfg.workload_tag is not None: ee.data.setWorkloadTag(cfg.workload_tag)
    return ee


def inspect_collections(cfg,limit=8):
    import ee
    collection_ids=list(HLS_COLLECTIONS.values())
    if cfg.s1: collection_ids.append('COPERNICUS/S1_GRD')
    if cfg.dem or cfg.rf['feature_set']=='core6_dem': collection_ids.append(DEM_COLLECTION)
    results={}
    for cid in collection_ids:
        collection=ee.ImageCollection(cid).filterBounds(region(cfg))
        if cid!=DEM_COLLECTION: collection=collection.filterDate(cfg.start,cfg.end)
        count=collection.size(); first=ee.Image(collection.first())
        details=ee.Dictionary({'collection':cid,'count':count,'sample_limit':limit,
            'scene_indices':collection.limit(limit).aggregate_array('system:index'),
            'sample':ee.Algorithms.If(count.gt(0),ee.Dictionary({
                'bands':first.bandNames(),'band_types':first.bandTypes(),
                'projection':first.select(0).projection(),'time_ms':first.get('system:time_start')}),None)})
        results[cid]=details.getInfo()
    return results


def save_preview(cfg,path,dimensions=512):
    image=min_nbr2_composite(hls_collection(cfg),cfg)
    params={'bands':['red','green','blue'],'min':.01,'max':.3,'region':region(cfg),
            'dimensions':dimensions,'format':'png'}
    # Signed URL is kept in memory and deliberately omitted from logs/manifests.
    url=image.getThumbURL(params)
    try:
        with urllib.request.urlopen(url,timeout=90) as response:
            payload=response.read(5*1024*1024+1)
    except Exception:
        raise RuntimeError('preview transfer failed (URL redacted)') from None
    if len(payload)>5*1024*1024 or not payload.startswith(b'\x89PNG\r\n\x1a\n'):
        raise RuntimeError('preview response is not a bounded PNG (URL redacted)')
    Path(path).write_bytes(payload)
    return {'path':str(path),'bytes':len(payload),'dimensions_max':dimensions,
            'kind':'HLS monthly minimum-NBR2 RGB; visualization only, not analytical pixels'}


def _write(path,value):
    Path(path).write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


def main(argv=None, *, prog='geecompute'):
    parser=argparse.ArgumentParser(prog=prog)
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('manifest','inspect','preview','train','usage','status','quota'):
        child=commands.add_parser(name); child.add_argument('--config',required=True)
        if name=='usage':
            child.add_argument('--start',required=True)
            child.add_argument('--end',default=datetime.now(timezone.utc).isoformat())
            child.add_argument('--workload-tag')
        if name=='status': child.add_argument('--operation',required=True)
        if name=='quota': child.add_argument('--project-number')
    args=parser.parse_args(argv); cfg=Config.load(args.config)
    if args.command=='status' and not re.fullmatch(r'projects/[^/]+/operations/[A-Za-z0-9_-]+',args.operation):
        raise ValueError('operation must be an exact projects/.../operations/... resource')
    if args.command=='train' and not cfg.labels.get('frozen_split_manifest'):
        raise ValueError('labels.frozen_split_manifest is required before train')
    if args.command=='train':
        from hpgee.compute.rf import load_split_manifest, sample_source_info
        load_split_manifest(cfg.labels['frozen_split_manifest'])
        sample_source_info(cfg)
    cfg.output.mkdir(parents=True,exist_ok=True)
    contract=manifest(cfg)
    receipt=cfg.output/'recipe-manifest.json'
    if receipt.exists() and json.loads(receipt.read_text()).get('config_sha256')!=contract['config_sha256']:
        raise ValueError('output already belongs to a different configuration; choose a new output directory')
    _write(receipt,contract)
    if args.command=='manifest':
        result=contract
    elif args.command in ('usage','quota'):
        credentials=None
        if cfg.auth.get('service_account'):
            import ee
            from hpgee.runtime.auth import validate_auth_config
            from hpgee.core.models import AuthConfig
            auth=AuthConfig(project=cfg.project,**cfg.auth); validate_auth_config(auth)
            credentials=ee.ServiceAccountCredentials(auth.service_account,str(auth.key_file))
        if args.command=='usage':
            from hpgee.accounting.usage import read_usage
            result=read_usage(cfg.project,start=args.start,end=args.end,workload_tag=args.workload_tag,credentials=credentials)
        else:
            from hpgee.accounting.quota import read_quota
            result=read_quota(cfg.project,project_number=args.project_number,credentials=credentials)
        _write(cfg.output/(args.command+'.json'),result)
    else:
        ee=initialize(cfg)
        if args.command=='status':
            from hpgee.accounting.usage import normalize_operation
            result=normalize_operation(ee.data.getOperation(args.operation)); _write(cfg.output/'operation.json',result)
        elif args.command=='inspect':
            result=inspect_collections(cfg); _write(cfg.output/'inspect.json',result)
        elif args.command=='preview':
            result=save_preview(cfg,cfg.output/'preview.png'); _write(cfg.output/'preview.json',result)
        else:
            from hpgee.compute.rf import train_rf, input_samples
            samples,source=input_samples(cfg)
            model,scores,result=train_rf(samples,cfg)
            result['input_source']=source
            # Explicit evaluation trains the RF. Persist a recipe graph, not model weights/checkpoint.
            result['explanation']=model.explain().select(['importance','numberOfTrees']).getInfo()
            (cfg.output/'rf-model-graph.json').write_text(model.serialize()+'\n')
            (cfg.output/'rf-score-graph.json').write_text(scores.serialize()+'\n')
            result['model_storage']='serialized EE computation graph; re-evaluates training, not durable weights'
            result['validation_metrics']='not evaluated; no validation acceptance claimed'
            _write(cfg.output/'rf-training.json',result)
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 2 if (args.command=='usage' and result['status']=='UNKNOWN') or (args.command=='quota' and result['quota']['status']=='UNKNOWN') else 0


if __name__=='__main__':
    raise SystemExit(main())
