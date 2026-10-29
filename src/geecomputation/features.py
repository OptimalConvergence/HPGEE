"""HLS v002 reflectance is already floating point in the EE catalog.

No 0.0001 multiplication. Common6 uses S30 B8A, not broad B8.
"""
import numpy as np

COMMON6 = ('blue','green','red','nir','swir1','swir2')
RF_BANDS = ('post_NBR2','post_MIRBI','delta_NBR2','delta_MIRBI','delta_NIR','delta_SWIR1')
DEM_BANDS = ('elevation','slope','aspect_sin','aspect_cos')
HLS_COLLECTIONS = {'L30':'NASA/HLS/HLSL30/v002','S30':'NASA/HLS/HLSS30/v002'}
HLS_BANDS = {'L30':('B2','B3','B4','B5','B6','B7'), 'S30':('B2','B3','B4','B8A','B11','B12')}
SCENE_BANDS = COMMON6 + ('Fmask','clear','NBR2','MIRBI','time_ms')
DEM_COLLECTION = 'COPERNICUS/DEM/GLO30_2024_1'


def clear_fmask(fmask):
    qa = np.asarray(fmask,dtype=np.uint8)
    return ((qa & 30) == 0) & (((qa >> 6) & 3) != 3)


def spectral_indices(swir1,swir2):
    a,b = np.broadcast_arrays(np.asarray(swir1,dtype=float),np.asarray(swir2,dtype=float))
    total = a+b
    ratio = np.full(a.shape,np.nan)
    np.divide(a-b,total,out=ratio,where=np.abs(total)>1e-8)
    return ratio, 10*b-9.8*a+2


def min_nbr2_pixels(data,times,valid):
    data = np.asarray(data,dtype=float)
    if data.ndim < 3 or data.shape[-1] != 6 or len(times) != data.shape[0]:
        raise ValueError('expected (scene,...pixel,common6) and one time per scene')
    nbr2,_ = spectral_indices(data[...,4],data[...,5])
    usable = np.asarray(valid,dtype=bool) & np.isfinite(nbr2) & np.isfinite(data).all(axis=-1)
    if usable.shape != data.shape[:-1]: raise ValueError('valid mask shape does not match scenes/pixels')
    if data.shape[0]==0:
        return np.full(data.shape[1:],np.nan),np.full(data.shape[1:-1],np.nan)
    indices = np.argmin(np.where(usable,nbr2,np.inf),axis=0)
    chosen = np.take_along_axis(data,indices[None,...,None],axis=0)[0]
    picked_times = np.asarray(times,dtype=float)[indices]
    absent = ~usable.any(axis=0)
    return np.where(absent[...,None],np.nan,chosen),np.where(absent,np.nan,picked_times)


def rf_features(pre,post):
    pre,post = np.asarray(pre,dtype=float),np.asarray(post,dtype=float)
    pn,pm = spectral_indices(pre[...,4],pre[...,5]); qn,qm = spectral_indices(post[...,4],post[...,5])
    return np.stack([qn,qm,qn-pn,qm-pm,post[...,3]-pre[...,3],post[...,4]-pre[...,4]],axis=-1)


def region(cfg):
    import ee
    return ee.Geometry.Rectangle(list(cfg.bbox),proj='EPSG:4326',geodesic=False)


def prepare_hls(image,sensor):
    import ee
    image = ee.Image(image)
    reflectance = image.select(list(HLS_BANDS[sensor]),list(COMMON6)).toFloat()
    qa = image.select('Fmask').toUint8()
    clear = qa.bitwiseAnd(30).eq(0).And(qa.rightShift(6).bitwiseAnd(3).neq(3))
    common_valid = reflectance.mask().reduce(ee.Reducer.min()).And(clear)
    reflectance = reflectance.updateMask(common_valid)
    a,b = reflectance.select('swir1'),reflectance.select('swir2')
    # Image.normalizedDifference would mask negative input values.
    nbr2 = a.subtract(b).divide(a.add(b)).updateMask(a.add(b).abs().gt(1e-8)).rename('NBR2')
    mirbi = b.multiply(10).subtract(a.multiply(9.8)).add(2).rename('MIRBI')
    clock = ee.Image.constant(image.get('system:time_start')).toDouble().rename('time_ms').updateMask(common_valid)
    return (reflectance.addBands(qa).addBands(clear.rename('clear').toUint8())
            .addBands(nbr2).addBands(mirbi).addBands(clock).toDouble()
            .copyProperties(image,['system:index','system:time_start'])
            .set('source_collection',HLS_COLLECTIONS[sensor]).set('source_index',image.get('system:index')).set('sensor',sensor))


def hls_collection(cfg,geometry=None,start=None,end=None):
    import ee
    geometry = region(cfg) if geometry is None else geometry
    collections=[]
    for sensor,cid in HLS_COLLECTIONS.items():
        source=ee.ImageCollection(cid).filterBounds(geometry).filterDate(start or cfg.start,end or cfg.end)
        if cfg.hls_tile_ids:
            source=source.filter(ee.Filter.Or(*[ee.Filter.stringStartsWith('system:index',tile+'_') for tile in cfg.hls_tile_ids]))
        if cfg.hls_native_dimensions:
            def native_support(image):
                return image.set('native_grid_intersects_roi',native_tile_extent(image,cfg.hls_native_dimensions).intersects(geometry,1))
            source=source.map(native_support).filter(ee.Filter.eq('native_grid_intersects_roi',True))
        collections.append(source.map(lambda image,sensor=sensor: prepare_hls(image,sensor)))
    return collections[0].merge(collections[1]).sort('system:time_start')


def native_tile_extent(image,dimensions):
    """Storage extent from a source-band affine projection, not image.geometry.

    Dimensions must be independently verified for the frozen source collection;
    this rectangle does not prove observed/clear support inside it.
    """
    import ee
    width,height=dimensions
    return ee.Geometry.Rectangle([0,0,width,height],proj=ee.Image(image).select('B2').projection(),geodesic=False)


def min_nbr2_composite(collection,cfg):
    import ee
    # Empty collections remain masked/unknown; no fallback fabricated reflectance.
    template = ee.Image.constant([0]*len(SCENE_BANDS)).rename(list(SCENE_BANDS)).toDouble().updateMask(ee.Image(0))
    # The all-masked fallback and real images must have homogeneous EE band types.
    prepared = collection.map(lambda im: im.addBands(im.select('NBR2').multiply(-1).rename('quality_nbr2')).toDouble())
    template = template.addBands(ee.Image(0).rename('quality_nbr2').updateMask(ee.Image(0))).toDouble()
    result = ee.ImageCollection.fromImages([template]).merge(prepared).qualityMosaic('quality_nbr2')
    count = ee.ImageCollection.fromImages([template]).merge(prepared).select('nir').count().rename('valid_count').unmask(0).toUint16()
    return (result.select(list(SCENE_BANDS)).addBands(count)
            .setDefaultProjection(cfg.grid['crs'],cfg.grid['transform']))


def rf_image(pre,post):
    images = [post.select('NBR2'),post.select('MIRBI'),post.select('NBR2').subtract(pre.select('NBR2')),
              post.select('MIRBI').subtract(pre.select('MIRBI')),post.select('nir').subtract(pre.select('nir')),
              post.select('swir1').subtract(pre.select('swir1'))]
    import ee
    return ee.Image.cat(images).rename(list(RF_BANDS)).toFloat()


def feature_image(cfg):
    pre = min_nbr2_composite(hls_collection(cfg,start=cfg.pre_start,end=cfg.pre_end),cfg)
    post = min_nbr2_composite(hls_collection(cfg),cfg)
    core=rf_image(pre,post)
    return core.addBands(dem_image(cfg).select(list(DEM_BANDS))) if cfg.rf['feature_set']=='core6_dem' else core


def detection_bands(cfg):
    return RF_BANDS+DEM_BANDS if cfg.rf['feature_set']=='core6_dem' else RF_BANDS


def s1_power_composite(cfg,start=None,end=None):
    import ee
    if not cfg.s1: raise ValueError('S1 requires explicit orbit/pass configuration')
    collection = (ee.ImageCollection('COPERNICUS/S1_GRD').filterBounds(region(cfg))
                  .filterDate(start or cfg.start,end or cfg.end).filter(ee.Filter.eq('instrumentMode','IW'))
                  .filter(ee.Filter.eq('resolution_meters',10))
                  .filter(ee.Filter.eq('relativeOrbitNumber_start',cfg.s1['relative_orbit']))
                  .filter(ee.Filter.eq('orbitProperties_pass',cfg.s1['pass']))
                  .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV'))
                  .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VH')))
    power = collection.map(lambda im: ee.Image.constant(10).pow(im.select(['VV','VH']).divide(10)).rename(['VV_power','VH_power']).toDouble())
    template = ee.Image.constant([0,0]).rename(['VV_power','VH_power']).updateMask(ee.Image(0)).toDouble()
    return (ee.ImageCollection.fromImages([template]).merge(power).mean()
            .setDefaultProjection(cfg.grid['crs'],cfg.grid['transform']).set('relative_orbit',cfg.s1['relative_orbit'])
            .set('orbit_pass',cfg.s1['pass']).set('units','linear sigma0 power'))


def dem_image(cfg):
    import ee
    # Terrain requires a metre grid before slope calculation; DSM is not bare-earth truth.
    elevation = (ee.ImageCollection(DEM_COLLECTION).filterBounds(region(cfg)).select('DEM').mosaic()
                 .reproject(crs=cfg.grid['crs'],crsTransform=cfg.grid['transform']).rename('elevation'))
    terrain=ee.Terrain.products(elevation)
    aspect=terrain.select('aspect').multiply(np.pi/180)
    return elevation.addBands(terrain.select('slope')).addBands(aspect.sin().rename('aspect_sin')).addBands(aspect.cos().rename('aspect_cos'))
