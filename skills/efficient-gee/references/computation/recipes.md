# Implemented recipe contract

The v0 code is a bounded dataset-construction utility; it does not implement global production, event tracking or scientifically accepted burn dates.

- HLS `NASA/HLS/HLSL30/v002` and `NASA/HLS/HLSS30/v002`: L30 B2/B3/B4/B5/B6/B7 and S30 B2/B3/B4/B8A/B11/B12 become blue/green/red/nir/swir1/swir2. GEE catalog representation uses unitless reflectance; verify actual pixels before imposing a scale. Do not blindly apply the original file's 0.0001 scale twice.
- Fmask: exclude bits1–4 and high aerosol (bits6–7=3); retain water as a separately interpretable case. Preserve QA and valid counts. Negative reflectance is not silently masked by `normalizedDifference`; use explicit arithmetic and a nonzero-denominator mask.
- Monthly min-NBR2: select one scene per pixel using NBR2=(SWIR1−SWIR2)/(SWIR1+SWIR2), and carry all bands and acquisition time from that same scene. Empty collections remain masked with zero valid counts.
- MIRBI=10×SWIR2−9.8×SWIR1+2. RF features are post_NBR2, post_MIRBI, delta_NBR2, delta_MIRBI, delta_NIR, delta_SWIR1; delta=post−pre. The 100-tree RF is a simplified research baseline. MULTIPROBABILITY vote fractions are not calibrated probabilities or burn timestamps.
- Monthly extent training must not be silently applied to single-scene timing. Last confidently unburned and first confidently burned observations constrain a separate interval; composite winner time is not ignition or first burn.
- Sentinel-1: use IW VV/VH, fixed relative orbit and pass; average in linear power, not dB. Retain geometry/missingness and distinguish terrain correction from radiometric terrain flattening.
- DEM `COPERNICUS/DEM/GLO30_2024_1` is DSM. Define the metre-based grid before terrain calculation, and save static data once per grid.
- GEDI must retain footprint identity and acquisition time. Prefer native table/index products for shot joins; monthly raster timestamps do not replace footprint time. Later observations cannot be labeled pre-fire fuel. GEDI integration is not yet implemented in v0.

Official catalogs: [HLS L30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSL30_v002), [HLS S30](https://developers.google.com/earth-engine/datasets/catalog/NASA_HLS_HLSS30_v002), [S1 processing](https://developers.google.com/earth-engine/guides/sentinel1), [DEM](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_DEM_GLO30_2024_1).

## Native HLS footprint validation

An authenticated HLS v002 probe found S30 image.geometry() globally oversized for an antimeridian scene despite a local source-band affine. Explicit hls_native_dimensions=[3660,3660] builds the stored raster extent using B2.projection() and filters its intersection before spectral processing. Enable only after checking source dimensions and corner coordinates; this is storage support, not cloud-free coverage. One Rwanda ROI/month went from 217 to 9 scenes with all 12 composite bands and masks identical on the frozen 128×128 grid. This does not establish global correctness or a measured speedup. Defaults stay empty; do not infer complete enumeration from a scene cap or tile list.
