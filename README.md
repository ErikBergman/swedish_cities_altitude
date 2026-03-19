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
python3 check_lund_dem_access.py
```

The script will:

- read the Lund tatort polygon from `Tatorter_2023.gpkg`
- find intersecting DEM tiles through the public STAC metadata
- verify authenticated access to one sample DEM asset
- stop before download if the estimated raw dataset exceeds `50 MB`
