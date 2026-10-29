"""Lossless, atomic raw pixel acceptance; 512-pixel canonical subblocks.

Independent of request spatial/temporal grouping, so trial outputs are reusable.
No hot database on durable storage. Receipts are immutable per source/grid/recipe.
"""
from pathlib import Path
import hashlib
import json
import os
import uuid
import numpy as np
from .raw_download import (chunks, make_unit, HLS_BANDS, S1_BANDS,
                           HLS_INT16_RECIPE, HLS_INT16_SCALE, HLS_INT16_OFFSET,
                           HLS_INT16_OVERFLOW_FLAG_BIT)


def sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def atoms(unit):
    return [make_unit([s],g,unit['profile']) for s in unit['scenes'] for g in chunks(unit['grid'],512)]


def verified(unit, destination):
    dest=Path(destination)
    for atom in atoms(unit):
        receipt=dest/(atom['unit_id']+'.json')
        if not receipt.exists():return False
        try:
            row=json.loads(receipt.read_text())
            if row['identity']!=atom:return False
            for k in ('data','qa'):
                if sha(row[k+'_path'])!=row[k+'_sha256']:return False
        except (OSError,ValueError,KeyError):return False
    return True


def _write(path, data, grid, names, nodata, *, scales=None, offsets=None, tags=None):
    import rasterio
    from affine import Affine
    from .raw_download import grid_spec
    path=Path(path);tmp=path.with_suffix('.'+uuid.uuid4().hex+'.partial')
    try:
        with rasterio.open(tmp,'w',driver='GTiff',width=data.shape[2],height=data.shape[1],count=data.shape[0],
                           dtype=str(data.dtype),crs=grid_spec(grid)['crs'],transform=Affine(*grid_spec(grid)['transform']),
                           nodata=nodata,compress='deflate',predictor=3 if data.dtype.kind=='f' else 2,tiled=True) as ds:
            ds.write(data);ds.descriptions=tuple(names)
            if scales is not None:ds.scales=tuple(scales)
            if offsets is not None:ds.offsets=tuple(offsets)
            if tags:ds.update_tags(**tags)
        os.replace(tmp,path)
    finally:tmp.unlink(missing_ok=True)


def publish(unit, source, destination):
    rows=[];is_hls=unit['profile'] in ('hls','hls-int16');is_hls_int16=unit['profile']=='hls-int16'
    if is_hls_int16:
        expected_encoding={'dtype':'int16','scale':HLS_INT16_SCALE,'offset':HLS_INT16_OFFSET,
                           'qa_bands':['Fmask','source_valid_bits'],
                           'overflow_flag_bit':HLS_INT16_OVERFLOW_FLAG_BIT}
        if unit.get('recipe')!=HLS_INT16_RECIPE or unit.get('encoding')!=expected_encoding:
            raise ValueError('HLS Int16 recipe or scale/QA identity mismatch')
    import rasterio
    from rasterio.windows import Window
    from affine import Affine
    from .raw_download import grid_spec, same_hls_grid
    dest=Path(destination);dest.mkdir(parents=True,exist_ok=True)
    names=HLS_BANDS if is_hls else S1_BANDS
    expected_dtype='int16' if is_hls_int16 else 'float32'
    count=len(names);expected=Affine(*grid_spec(unit['grid'])['transform'])
    with rasterio.open(source) as ds:
        dims=unit['grid']['dimensions']
        expected_crs=rasterio.crs.CRS.from_user_input(grid_spec(unit['grid'])['crs'])
        crs_ok=ds.crs==expected_crs
        # S1 30m is requested on the HLS target grid, which EE also writes
        # with the canonical EPSG label instead of HLS's unnamed-datum WKT.
        if not crs_ok and unit['profile'] in ('hls','hls-int16','s1-30') and ds.crs:
            actual_grid={'crsWkt':ds.crs.to_wkt(),'dimensions':dims,
                         'affineTransform':unit['grid']['affineTransform']}
            crs_ok=same_hls_grid(unit['grid'],actual_grid)
        mismatches=[]
        if ds.transform!=expected:mismatches.append('transform')
        if not crs_ok:mismatches.append('crs')
        if (ds.width,ds.height)!=(dims['width'],dims['height']):mismatches.append('dimensions')
        if ds.count!=count*len(unit['scenes']):mismatches.append('bands')
        if any(t!=expected_dtype for t in ds.dtypes):mismatches.append('dtype')
        if mismatches:raise ValueError('Payload differs from frozen contract: '+','.join(mismatches))
        for atom in atoms(unit):
            scene_index=unit['scenes'].index(atom['scenes'][0]);g=atom['grid'];a=g['affineTransform']
            x,y=(~expected)*(a['translateX'],a['translateY'])
            window=Window(round(x),round(y),g['dimensions']['width'],g['dimensions']['height'])
            values=ds.read(list(range(scene_index*count+1,(scene_index+1)*count+1)),window=window)
            bits=values[names.index('source_valid_bits')]
            maxbits=255 if is_hls_int16 else 127 if is_hls else 3
            if not np.isfinite(bits).all() or np.any(bits!=np.floor(bits)) or np.any((bits<0)|(bits>maxbits)):
                raise ValueError('Invalid source validity bits')
            if is_hls_int16 and np.any((bits.astype('uint16')&(1<<7))!=0):
                raise ValueError('HLS Int16 overflow flag is set')
            if is_hls_int16:bits=bits&(127)
            if unit['profile']=='s1-30' and np.any(~np.isin(bits,[0,3])):
                raise ValueError('S1 30m requires common-support bits 0 or 3')
            bits=bits.astype('uint8');n=6 if is_hls else 2
            data=values[:n].copy()
            for b in range(n):
                valid=(bits&(1<<b))!=0
                if not np.isfinite(data[b][valid]).all():raise ValueError('Nonfinite source-valid pixel')
                if not is_hls and np.any(data[b][valid]<=0):raise ValueError('Nonpositive sigma0 power')
                data[b,~valid]=0 if is_hls_int16 else np.nan
            if is_hls:
                qa=values[6];valid=(bits&64)!=0
                if not np.isfinite(qa[valid]).all() or np.any((qa[valid]<0)|(qa[valid]>255)|(qa[valid]!=np.floor(qa[valid]))):
                    raise ValueError('Noninteger/out-of-range Fmask')
                qa=np.stack([np.where(valid,qa,0).astype('uint8'),bits]);qa_names=['Fmask','source_valid_bits'];data_names=list(names[:6])
            else:
                fraction=values[3]
                if not np.isfinite(fraction).all() or np.any((fraction<0)|(fraction>1+1e-6)) or np.any((fraction>0)!=((bits&3)==3)):
                    raise ValueError('Invalid common-support fraction')
                data=np.concatenate([data,fraction[None]],axis=0);data_names=['VV_power','VH_power','valid_fraction'];qa=bits[None];qa_names=['source_valid_bits']
            uid=atom['unit_id'];data_path=dest/(uid+'.tif');qa_path=dest/(uid+'.qa.tif')
            # A complete accepted atom is never rewritten during regrouped retries.
            if verified(atom,dest):
                rows.append(json.loads((dest/(uid+'.json')).read_text()));continue
            if is_hls_int16:
                encoding=unit['encoding']
                _write(data_path,data,g,data_names,None,scales=(encoding['scale'],)*n,
                       offsets=(encoding['offset'],)*n,
                       tags={'encoding':unit['recipe'],'scale':str(encoding['scale']),
                             'offset':str(encoding['offset']),'qa_bands':','.join(encoding['qa_bands']),
                             'qa_path_role':'authoritative validity and Fmask sidecar',
                             'invalid_fill_value':'0; distinguish from valid zero by source_valid_bits'})
            else:
                _write(data_path,data,g,data_names,np.nan)
            _write(qa_path,qa,g,qa_names,None)
            row=dict(identity=atom,data_path=str(data_path.resolve()),qa_path=str(qa_path.resolve()),
                     data_sha256=sha(data_path),qa_sha256=sha(qa_path),logical_transport_bytes=atom['logical_bytes'],
                     stored_bytes=data_path.stat().st_size+qa_path.stat().st_size,
                     valid_pixels=[int(((bits&(1<<b))!=0).sum()) for b in range(n)],
                     observed_data_bytes=sum(int(((bits&(1<<b))!=0).sum()) for b in range(n))*(2 if is_hls_int16 else 4),
                     source_support_status='observed' if np.any(bits&((1<<n)-1)) else 'source_empty')
            if is_hls_int16:row['encoding']=unit['encoding']
            receipt=dest/(uid+'.json');tmp=receipt.with_suffix('.tmp');tmp.write_text(json.dumps(row,sort_keys=True));tmp.replace(receipt)
            rows.append(row)
    return rows


def pending_units(units, accepted_ids, *, fresh_only):
    """Disjoint fresh trials; partial resume is split to missing canonical atoms."""
    result=[]
    for unit in units:
        pieces=atoms(unit)
        missing=[a for a in pieces if a['unit_id'] not in accepted_ids]
        if not missing:continue
        if len(missing)==len(pieces):result.append(unit)
        elif not fresh_only:result.extend(missing)
    return result
