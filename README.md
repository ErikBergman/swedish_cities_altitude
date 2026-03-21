# swedish_cities_altitude

PoC for verifying authenticated access to Lantmateriet DEM tiles for Swedish tatorter.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set credentials through environment variables:

```bash
export LANTMATERIET_USERNAME="your-email@example.com"
export LANTMATERIET_PASSWORD="your-password"
```

## PoC

Run the Lund access check:

```bash
python3 check_lund_access.py
```

The script will:

- read the Lund tatort polygon from `Tatorter_2023.gpkg`
- find intersecting DEM tiles through the public STAC metadata
- verify authenticated access to one sample DEM asset
- plan temporary download batches within a cache budget
- show how Lund can be processed without permanently storing the full dataset

Download the planned Lund batch into a temporary cache directory:

```bash
python3 download_lund_batch.py --cache-budget-mb 1024
```

The downloader will:

- reuse the same Lund batch planning
- create a temporary cache directory unless `--cache-dir` is provided
- download one batch of tiles with Basic auth
- skip tiles that already exist at the expected size

Summarize Lund altitude from downloaded tiles:

```bash
python3 summarize_lund_altitude.py --cache-dir ./tmp/lund_cache
```

The summary script will:

- read the Lund tatort polygon
- scan the downloaded `.tif` tiles in the cache directory
- clip them to the tatort boundary
- report `min`, `max`, and altitude range
- compute normalized relief from elevation percentiles
- compute RMS slope in degrees
- report a combined hilliness score

Run the nationwide comparison with Rich progress and resumable checkpoints:

```bash
python compare_cities.py --cache-budget-mb 5000
```

The comparison script will:

- authenticate once with the Lantmäteriet DEM service
- select all Swedish tätorter by default
- discover and download each tätort's tiles in bounded batches
- process each tätort incrementally without keeping all tiles permanently
- checkpoint progress in `./.state/compare_cities.sqlite`
- persist per-tile elevation chunks in `./.state/compare_cities_chunks`
- persist the discovered per-tätort tile plan in SQLite so STAC preflight does not need to be rebuilt on every restart
- resume cleanly after interruption without redoing completed tätorter or tiles
- show a Rich progress bar with rolling ETAs during processing
- print a top-ranked summary table in the terminal
- write the full result set to `./tmp/all_tatorter_hilliness.csv`

You can override the checkpoint locations if needed:

```bash
python compare_cities.py \
  --state-db ./tmp/compare_cities.sqlite \
  --chunk-root ./tmp/compare_cities_chunks \
  --work-root ./tmp/compare_cities_cache
```

You can also run filtered subsets for testing:

```bash
python compare_cities.py --tatort Lund --tatort Malmö
python compare_cities.py --kommun Stockholm --limit 25
python compare_cities.py --offset 500 --limit 100
python compare_cities.py --min-population 10000
python compare_cities.py --min-population 10000 --max-population 50000
```
