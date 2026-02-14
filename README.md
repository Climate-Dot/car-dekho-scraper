# CarDekho Scraper

Selenium-based scraper for:

1. Building a `brand -> model -> variants` catalog from CarDekho.
2. Scraping per-variant specifications from variant detail pages.
3. Writing combined and per-brand JSON outputs.
4. Optionally uploading per-brand files to Azure Blob Storage.

This README is written as an engineering handoff document so the codebase is predictable to run and modify.

## Repository Layout

- `get_brand_models.py`
  - Scrapes brands/models/variants from [cardekho.com/compare-cars](https://www.cardekho.com/compare-cars).
  - Output: `brand_model_map.json`.
- `get_variant_features.py`
  - Uses `brand_model_map.json` as input.
  - Builds variant URLs and extracts key specs + table sections from variant pages.
  - Outputs:
    - Combined file: `variant_features.json` (or `--output`).
    - Per-brand files: `{brand_name}_{YYYY-MM-DD}.json`.
  - Optional Azure upload for each per-brand file.
- `test_variant_features.py`
  - Random sample test runner (`--count`, default 10).
  - Output: `test_variant_features_results.json`.
- `test_specific_variants.py`
  - Fixed-case URL/spec extraction sanity test.
  - Output: `test_specific_variants_results.json`.
- `config.yaml`
  - Azure blob settings.

## Runtime Requirements

- Python 3.9+ (project currently has a `venv` with 3.9).
- Google Chrome installed.
- Internet access (live site scraping).
- Python dependencies from `requirements.txt`:
  - `selenium`
  - `webdriver-manager`
  - `PyYAML`
  - `azure-storage-blob`

## Setup

```bash
cd <repo-root>
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## End-to-End Workflow

### 1) Build brand/model/variant map

```bash
python3 get_brand_models.py --output brand_model_map.json --log-level INFO
```

### 2) Scrape variant features

```bash
python3 get_variant_features.py \
  --input brand_model_map.json \
  --output variant_features.json \
  --log-level INFO \
  --no-upload
```

### 3) Optional sample test run (10 random variants)

```bash
python3 test_variant_features.py --count 10 --seed 42
```

## CLI Reference

### `get_brand_models.py`

```bash
python3 get_brand_models.py [--output brand_model_map.json] [--log-level INFO]
```

- `--output`, `-o`: output map file path.
- `--log-level`, `-l`: `DEBUG|INFO|WARNING|ERROR`.

### `get_variant_features.py`

```bash
python3 get_variant_features.py \
  [--input brand_model_map.json] \
  [--output variant_features.json] \
  [--log-level INFO] \
  [--limit-models N] \
  [--output-dir PATH] \
  [--no-upload]
```

- `--input`, `-i`: source brand/model/variant map.
- `--output`, `-o`: combined output JSON.
- `--log-level`, `-l`: `DEBUG|INFO|WARNING|ERROR`.
- `--limit-models`, `-m`: processes first `N` models globally across the map.
- `--output-dir`: where per-brand `{brand}_{date}.json` files are written.
- `--no-upload`: skip Azure blob upload.

### `test_variant_features.py`

```bash
python3 test_variant_features.py [--input brand_model_map.json] [--count 10] [--seed 42]
```

- Randomly samples variants and writes only `test_variant_features_results.json`.
- This test script does not generate per-brand `{brand}_{date}.json` files.

## Data Contracts

### `brand_model_map.json`

```json
{
  "Maruti": {
    "Alto K10": [
      "STD (Petrol) 3.70 Lakh*",
      "LXI (Petrol) 4.45 Lakh*"
    ]
  }
}
```

### `variant_features.json`

```json
{
  "Maruti": {
    "Alto K10": {
      "STD (Petrol) 3.70 Lakh*": {
        "url": "https://www.cardekho.com/overview/...",
        "cleaned_variant_name": "STD",
        "key_specifications": {
          "Engine": "998 cc"
        },
        "table_sections": {
          "Engine & Transmission": {
            "Displacement": "998 cc"
          }
        }
      }
    }
  }
}
```

## Where Per-Brand Files Are Created

Per-brand write logic is in `get_variant_features.py`:

- Directory resolution: `out_dir = output_dir or _script_dir()`
- File naming:
  - `date_str = datetime.now().strftime("%Y-%m-%d")`
  - `brand_filename = f"{safe_brand}_{date_str}.json"`
- Write/upload block runs once per processed brand.

Important behavior:

- If you run `test_variant_features.py`, you only get `test_variant_features_results.json`.
- `{brand}_{date}.json` files are produced only by `get_variant_features.py`.

## `--limit-models` Behavior (Common Confusion)

`--limit-models N` limits the number of models, not variants.

If the first `N` models contain many variants, total processed variants can be much larger than `N`.  
Example from this repo’s current data: first 10 models contain 67 variants, so `--limit-models 10` processes 67 variants.

## Logs and Intermediate Saves

- Brand/model collection logs: `cardekho_scraper.log`
- Variant feature logs: `variant_features_scraper.log`
- Combined variant feature progress is saved every 10 processed variants.

## Azure Upload Configuration

`config.yaml`:

```yaml
azure:
  connection_string: "..."
  container_name: "cardekho-variant-features"
  blob_prefix: ""
```

Behavior:

- Upload is attempted only when config exists and `--no-upload` is not passed.
- Missing Azure deps or invalid config does not stop scraping; upload is skipped/logged.

## Operational Caveats

- Scraping selectors are tied to current CarDekho DOM classes/XPath and can break if the site changes.
- Variant URL generation is heuristic and relies on `clean_variant_name()`.
- Network latency and site throttling can increase runtime.
- The scraper includes fixed sleeps (`time.sleep`) for stability; this prioritizes reliability over speed.

## Suggested Improvement Backlog

1. Add `--limit-variants` for strict variant-count runs.
2. Add retry/backoff around page loads and transient Selenium failures.
3. Move XPaths/selectors to a constants module for easier maintenance.
4. Add unit tests for URL cleaning/building edge cases.
5. Add smoke tests that run without live network using captured HTML fixtures.
