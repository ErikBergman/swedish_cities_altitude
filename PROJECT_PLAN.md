# Swedish Cities Altitude Project Plan

## Goal

Build a reproducible workflow that compares terrain characteristics for all Swedish `tätorter` by combining:

- polygon boundaries from `Tatorter_2023.gpkg`
- elevation rasters from Lantmäteriet's Markhöjdmodell

The final output should be one summary row per `tätort`, containing terrain statistics such as:

- minimum altitude
- maximum altitude
- mean altitude
- altitude range
- average slope
- additional slope distribution metrics if useful

The purpose is to analyze how terrain varies between Swedish urban areas without generating an unnecessary nationwide point table.

Storage constraint:
- the program must not require permanent storage of the full nationwide DEM
- temporary storage in the low single-digit GB range is acceptable
- the workflow should therefore be built around bounded caching and incremental processing

## Core Principle

Use polygon-based raster analysis rather than converting all of Sweden into a giant `(x, y) -> tätort / N/A` lookup table.

Why:

- the DEM already exists as a regular grid
- raster clipping is cheaper than building and storing a national point dataset
- most of Sweden lies outside tätorter, so an `N/A` grid would waste large amounts of storage and processing time
- min, max, and slope statistics can be computed directly from clipped DEM cells inside each polygon

## Data Inputs

### 1. Tätort boundaries

Source:
- `Tatorter_2023.gpkg`

Contents:
- `MULTIPOLYGON` geometry for each tätort
- names and administrative metadata such as municipality, county, population, year, and area

Expected role in the pipeline:
- defines the analysis zones
- provides metadata for the final summary table

### 2. Elevation data

Source:
- Lantmäteriet Markhöjdmodell via STAC API and GeoTIFF/COG assets

Expected properties:
- terrain elevation
- `1 m` grid resolution
- multiple tiles covering Sweden

Expected role in the pipeline:
- provides raster values for altitude calculations
- provides the base raster from which slope will be derived

Storage implication:
- the full set of DEM tiles intersecting all Swedish tätorter is large
- raw nationwide coverage should be treated as a remote source, not as a required local dataset
- only a small working set of tiles should exist on disk at any given time

### 3. Authentication and access model

Confirmed behavior:
- STAC catalog metadata is publicly readable
- actual DEM asset files on `dl1.lantmateriet.se` require Basic authentication

Confirmed access pattern:
- metadata discovery works anonymously from `https://api.lantmateriet.se/stac-hojd/v1`
- DEM downloads work with Basic auth using:
  - username: the Geotorget email address for a private-person account
  - password: the corresponding Geotorget password

Project rule:
- credentials must be supplied only through environment variables
- credentials must never be hardcoded in source files, notebooks, config files, or command history examples committed to the repository

Required environment variables:
- `LANTMATERIET_USERNAME`
- `LANTMATERIET_PASSWORD`

## Recommended Workflow

### Phase 1. Validate and inspect inputs

Tasks:
- confirm the CRS of `Tatorter_2023.gpkg`
- inspect the DEM tile CRS and resolution
- confirm whether DEM assets require authentication when downloaded or streamed
- verify that the DEM uses bare-earth terrain heights and not DSM canopy/building heights

Deliverable:
- a short input validation script or notebook

### Phase 2. Build a DEM acquisition layer

Tasks:
- query the Lantmäteriet STAC API for relevant collections
- find all DEM tiles intersecting Swedish tätort polygons
- download and cache tiles locally in small batches
- avoid repeated requests for the same tile
- read authentication credentials from environment variables only
- estimate the total dataset size before downloading any raster files
- keep only a bounded temporary tile cache on disk
- evict already-processed tiles when the cache budget is exceeded
- support a configurable cache budget, with the default target in the low single-digit GB range
- stop and require approval before downloading a batch that exceeds the active cache budget

Deliverable:
- a tile index table with:
  - tile id
  - source URL
  - projected bounding box
  - local cached path if currently downloaded
  - processing status

Why this matters:
- the analysis should not refetch the same tiles repeatedly
- tile management should be separate from terrain statistics
- storage use must remain bounded and predictable

### Phase 2A. STAC API workflow

The acquisition layer should follow this exact sequence:

1. Read the target `tätort` polygon from `Tatorter_2023.gpkg`
2. Determine which STAC collection covers the polygon
3. Fetch the collection's `items`
4. Read each item's:
   - `assets.data.href`
   - `assets.data.file:size`
   - `assets.data.proj:bbox` or `properties.proj:bbox`
5. Intersect the polygon with each tile bbox in projected coordinates
6. Sum the `file:size` values of intersecting tiles
7. Group intersecting tiles into a download batch that fits within the active cache budget
8. If a proposed batch exceeds the budget:
   - stop
   - report the tile count and total estimated download size
   - require explicit user approval before downloading
9. If the batch fits within the budget:
   - download the batch using Basic auth
   - store it under a deterministic cache path
10. Process the tiles immediately and update per-tätort summary statistics
11. Remove or evict processed tiles when the cache budget needs to be reclaimed

### Phase 2B. Lund PoC discovery result

The initial proof of concept should use the `tätort`:

- `tatort`: `Lund`
- `kommunnamn`: `Lund`
- `lannamn`: `Skåne`
- `tatortskod`: `1281TC105`

Known bounds:
- `EPSG:3006`: `(383406.64, 6172019.86, 390374.09, 6179223.02)`
- `EPSG:4326`: `(13.144634, 55.680609, 13.255445, 55.745084)`

Known STAC coverage:
- Lund is covered by collection `mhm-61_3`

Known tile estimate for Lund:
- intersecting tiles: `12`
- total raw DEM size: about `98.19 MB`

Implication:
- Lund is still small enough for a proof of concept
- Lund does not need to remain on disk after processing
- Lund should be handled as a temporary working set, not a permanent dataset

### Phase 2C. Nationwide storage estimate

Known nationwide estimate from STAC metadata:
- `2017` tätorter
- about `6970` unique DEM tiles intersect at least one tätort
- about `58.14 GB` of raw DEM tiles if all unique intersecting tiles were stored at once

Implication:
- storing all required raw tiles permanently is unnecessary and undesirable
- the nationwide pipeline must process tiles incrementally
- only intermediate summaries and a bounded temporary cache should remain local

### Phase 2D. Authentication implementation

The downloader should use Basic auth against `dl1.lantmateriet.se`.

Expected runtime behavior:
- read `LANTMATERIET_USERNAME`
- read `LANTMATERIET_PASSWORD`
- fail early with a clear error if either variable is missing
- never print the password

Expected validation check:
- perform a lightweight authenticated request before starting a larger download batch
- if authentication fails, stop before any tile processing begins

Example shell setup:

```bash
export LANTMATERIET_USERNAME="your-email@example.com"
export LANTMATERIET_PASSWORD="your-password"
```

Example authenticated test:

```bash
curl -I -u "$LANTMATERIET_USERNAME" \
  "https://dl1.lantmateriet.se/hojd/data/grid1m/61_3/55/61750_3850_25.tif"
```

### Phase 2E. Incremental processing model

The full pipeline should not depend on storing all DEM tiles locally.

Recommended model:

1. Build the complete tile index from STAC metadata
2. For each tile:
   - determine which tätorter intersect it
   - download the tile into the temporary cache
   - compute altitude and slope contributions for the intersecting tätorter
   - update persistent summary state
   - delete or evict the tile when it is no longer needed
3. Repeat until all tiles are processed

Persistent outputs should remain small:
- tile index metadata
- per-tätort running summary tables
- final CSV outputs

Temporary outputs should be bounded:
- raw DEM tile cache
- temporary slope rasters or working windows

The default implementation should aim for:
- a cache budget in the low single-digit GB range
- no requirement to keep previously processed raw tiles once summaries are updated

### Phase 3. Compute altitude statistics per tätort

Tasks:
- for each downloaded tile, identify the intersecting tätort polygons
- clip raster cells to those polygons
- calculate:
  - `min_altitude_m`
  - `max_altitude_m`
  - `mean_altitude_m`
  - `median_altitude_m`
  - `altitude_range_m`
  - optionally `std_altitude_m`
- update per-tätort running aggregates incrementally rather than requiring all DEM data at once

Deliverable:
- one CSV or GeoPackage table with one row per tätort

Notes:
- this is classic zonal statistics
- no national point-grid intermediate is required
- the implementation should be able to resume after partial progress if interrupted

### Phase 4. Compute slope raster

Tasks:
- derive slope from the DEM using the native DEM grid
- choose a slope unit:
  - degrees is usually easier to interpret
  - percent can be added if needed
- compute slope per DEM tile once within the temporary cache
- avoid retaining derived slope rasters permanently unless needed for debugging or reuse

Deliverable:
- temporary or optional cached slope rasters corresponding to DEM tiles

Why this matters:
- slope should be derived from terrain gradients, not from polygon shape
- caching prevents repeated expensive computation

### Phase 5. Compute slope statistics per tätort

Tasks:
- clip slope rasters by tätort polygons
- calculate:
  - `mean_slope_deg`
  - `median_slope_deg`
  - `max_slope_deg`
  - `p90_slope_deg`
  - optionally fraction of area above a chosen steepness threshold

Deliverable:
- a terrain summary table joined to the altitude table

### Phase 6. Compare and rank tätorter

Tasks:
- sort tätorter by:
  - highest max altitude
  - lowest min altitude
  - largest altitude range
  - steepest average slope
- optionally create separate outputs for:
  - all tätorter
  - top 20 steepest
  - top 20 greatest elevation range

Deliverable:
- final analysis CSV
- optional plots or maps

## Suggested File Structure

Recommended structure once implementation starts:

- `data/raw/`
  - original GeoPackage
- `data/cache/`
  - tile index
  - temporary DEM tile cache
  - optional temporary slope rasters
- `data/output/`
  - final CSV summaries
  - persistent intermediate summary tables if needed for resumability
- `src/`
  - acquisition, analysis, and utility modules
- `notebooks/`
  - exploratory validation only if needed

## Suggested Python Modules

### `src/config.py`

Purpose:
- centralize file paths, API URLs, CRS values, cache locations, and output paths

### `src/stac_client.py`

Purpose:
- query Lantmäteriet STAC collections and items
- build a tile index
- handle pagination
- keep STAC metadata discovery separate from raster authentication

### `src/tile_cache.py`

Purpose:
- download and reuse DEM tiles locally in a bounded cache
- track which tiles have already been fetched and processed
- evict processed tiles when the cache budget is reached
- use `LANTMATERIET_USERNAME` and `LANTMATERIET_PASSWORD` for Basic-auth downloads

### `src/aggregates.py`

Purpose:
- maintain persistent per-tätort running statistics
- support resumable processing without keeping all DEM tiles on disk

### `src/zonal_altitude.py`

Purpose:
- compute altitude statistics for each tätort from DEM tiles

### `src/slope.py`

Purpose:
- derive slope rasters from DEM tiles

### `src/zonal_slope.py`

Purpose:
- compute slope statistics for each tätort from cached slope rasters

### `src/run_pipeline.py`

Purpose:
- orchestrate the full workflow end to end

## Dependencies

These are the expected core dependencies when implementation begins.

### `geopandas`

Purpose:
- read and write GeoPackage and vector outputs
- manage tätort polygons as geospatial tables
- perform spatial filtering and joins at the vector level

### `shapely`

Purpose:
- geometric operations on polygons and bounding boxes
- intersection checks between tätorter and DEM tiles

### `pyogrio`

Purpose:
- fast vector file IO backend for GeoPandas
- useful for reading the GeoPackage efficiently

### `pyproj`

Purpose:
- CRS handling and coordinate transformation
- needed when matching STAC geometries with projected DEM bounds

### `rasterio`

Purpose:
- open DEM GeoTIFF/COG files
- mask rasters by polygon
- read raster windows efficiently
- write derived slope rasters if needed
- support tile-by-tile processing without full mosaics

### `numpy`

Purpose:
- numeric operations on raster arrays
- min, max, mean, percentile, and mask handling
- gradient-based slope calculations if implemented directly

### `pandas`

Purpose:
- tabular result assembly
- export of summary tables to CSV
- persist intermediate aggregate tables when resumability is needed

### `requests`

Purpose:
- convenient HTTP access to the STAC API
- easier authentication and retry handling than raw standard library calls
- handle Basic-auth downloads for DEM asset files

### `scipy` or `richdem` or `xdem` (optional)

Purpose:
- terrain derivatives such as slope

Notes:
- `scipy` is useful if slope is calculated from local gradients manually
- `richdem` or `xdem` may simplify terrain analysis, but they add more dependency weight
- if the project should stay minimal, slope can be derived with `numpy` and `rasterio`

### `tqdm` (optional)

Purpose:
- progress bars for long-running tile and tätort processing

## Dependency Strategy

Recommended environment strategy:

- do not use the global `base` environment
- create a dedicated project environment
- pin versions after the first confirmed working run

Reason:
- geospatial Python stacks are sensitive to version mismatches, especially around `numpy`, `pandas`, `gdal`, and `rasterio`

## Important Technical Decisions

### Use the DEM's native grid

Do not generate a synthetic point grid unless a separate research question requires it.

Reason:
- the DEM already defines the spatial sampling
- resampling early introduces avoidable complexity

### Prefer local tile caching

Remote streaming may work, but local caching is safer.

Reason:
- avoids repeated downloads
- reduces dependence on API availability
- makes reruns deterministic
- simplifies debugging

### Keep the cache bounded

Do not let temporary DEM storage grow without limit.

Rule:
- use a configurable cache budget
- target low single-digit GB storage by default
- evict processed tiles when space is needed

Reason:
- avoids requiring tens of gigabytes of permanent disk
- makes nationwide processing feasible on a normal laptop
- matches the intended tile-by-tile pipeline

### Keep credentials in environment variables only

Do not place service credentials in:
- source code
- notebooks
- plain-text config files committed to git
- project documentation examples with real values

Reason:
- reduces accidental credential leakage
- keeps local and CI execution patterns consistent
- matches the confirmed Basic-auth download flow

### Enforce the batch gate before download

The downloader must estimate batch size before downloading.

Rule:
- if a proposed download batch fits within the active cache budget, automatic download is allowed
- if a proposed batch exceeds the budget, the program must stop and require approval

Reason:
- prevents unexpectedly large temporary downloads
- keeps local storage predictable
- separates storage control from the total nationwide dataset size

### Compute slope once per tile

Do not recompute slope separately for every tätort.

Reason:
- many tätorter overlap the same DEM tiles
- tile-level preprocessing reduces duplicated work

### Keep outputs tidy and analysis-focused

Primary final output should be one row per tätort, not a giant coordinate dataset.

Recommended columns:
- `tatort`
- `kommunnamn`
- `lannamn`
- `area_ha`
- `bef`
- `min_altitude_m`
- `max_altitude_m`
- `mean_altitude_m`
- `median_altitude_m`
- `altitude_range_m`
- `mean_slope_deg`
- `median_slope_deg`
- `max_slope_deg`
- `p90_slope_deg`
- `dem_tiles_used`

## Expected Risks

### Authentication

The STAC catalog may be public while the raster assets require authentication.

Mitigation:
- confirm the required credential mechanism early
- build authentication support into the tile acquisition layer before full analysis

### CRS mismatches

The STAC collection extents may be exposed in geographic coordinates while DEM assets and tätort polygons may use projected coordinates.

Mitigation:
- normalize all processing to a single projected CRS before clipping

### Performance

Nationwide `1 m` DEM data is large.

Mitigation:
- use tile-level processing
- cache intermediate products
- avoid point-grid explosion
- avoid full-country local mosaics

### Memory usage

Large raster mosaics can exceed memory limits.

Mitigation:
- never build a full Sweden mosaic in memory
- process by tile and aggregate incrementally

### Disk usage

Nationwide raw tiles are much larger than the desired local storage footprint.

Mitigation:
- use a bounded temporary cache
- persist only summaries and metadata
- delete or evict tiles after their contribution has been absorbed

## First Implementation Milestone

The first milestone should be deliberately narrow:

1. read `Tatorter_2023.gpkg`
2. fetch or index DEM tiles for one known area such as Stockholm
3. verify authenticated access for one sample tile
4. download only a small temporary working set
5. compute `min`, `max`, and `mean` altitude for one tätort
6. compute mean slope for the same tätort
7. verify the result manually

Only after that works should the script be expanded to all Swedish tätorter.

## Final Deliverables

When the project is complete, the repository should contain:

- a reproducible environment specification
- a script or CLI that runs the full analysis
- bounded cached or indexed DEM tile management
- a final CSV of per-tätort terrain statistics
- a short methodology note explaining:
  - data sources
  - CRS assumptions
  - how slope was derived
  - how `nodata` and edge effects were handled
