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
