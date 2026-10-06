"""
Script to build a brand -> model -> body type lookup from CarDekho.

Body type is the same for every variant of a model and is not part of the visible spec
tables scraped by get_variant_features.py; it only exists in the page's schema.org JSON-LD.
This script backfills it for existing per-brand snapshots without a full re-scrape:

1. Reads brand-model-variant data from brand_model_map.json
2. For each model, fetches one variant page over plain HTTP (no Selenium), falling back
   to the next variant if a page fails or has no body type
3. Saves body_types_{date}.json and optionally uploads it to Azure Blob Storage
   (same container as the per-brand files, when config.yaml is present)

Usage:
    python get_body_types.py --input brand_model_map.json
    python get_body_types.py --no-upload          # skip Azure upload
    python get_body_types.py --output-dir ./out   # where to write body_types_{date}.json
"""

import json
import time
import logging
import os
import urllib.request
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from get_variant_features import (
    BODY_TYPE_MAP,
    _script_dir,
    build_url,
    extract_body_type,
    load_config,
    normalize_body_type,
    upload_to_azure_blob,
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
MAX_VARIANTS_PER_MODEL = 3  # variant pages to try before giving up on a model


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('body_types_scraper.log'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


def fetch_page(url: str, timeout: int = 30) -> Tuple[str, str]:
    """Fetch a page over HTTP. Returns (final_url_after_redirects, html)."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.geturl(), response.read().decode("utf-8", errors="ignore")


def get_model_body_type(brand: str, model: str, variants: List[str], logger: logging.Logger) -> Dict:
    """
    Resolve the body type of a model from its first working variant page.

    Returns {body_type, body_type_raw, source_url, final_url} (body_type None if not found).
    """
    last_url = None
    for variant in variants[:MAX_VARIANTS_PER_MODEL]:
        url = build_url(brand, model, variant)
        last_url = url
        try:
            final_url, html = fetch_page(url)
        except Exception as e:
            logger.warning(f"Failed to fetch {url}: {e}")
            time.sleep(2)
            continue
        raw = extract_body_type(html)
        if raw:
            if final_url != url:
                logger.info(f"Redirected: {url} -> {final_url}")
            return {
                "body_type": normalize_body_type(raw),
                "body_type_raw": raw,
                "source_url": url,
                "final_url": final_url,
            }
        logger.warning(f"No body type in JSON-LD for {url}")
        time.sleep(1)
    return {"body_type": None, "body_type_raw": None, "source_url": last_url, "final_url": None}


def process_models(brand_model_map: Dict, logger: logging.Logger, delay: float = 1.0) -> Dict:
    """
    Resolve body type for every model.

    Returns {brand: {model: {body_type, body_type_raw, source_url, final_url}}}.
    """
    results = {}
    total_models = sum(len(models) for models in brand_model_map.values())
    processed = 0
    for brand, models in brand_model_map.items():
        results[brand] = {}
        for model, variants in models.items():
            processed += 1
            results[brand][model] = get_model_body_type(brand, model, variants, logger)
            logger.info(
                f"[{processed}/{total_models}] {brand} - {model}: "
                f"{results[brand][model]['body_type'] or 'NOT FOUND'}"
            )
            time.sleep(delay)  # Be respectful with delays between requests
    return results


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Build brand/model body type lookup from CarDekho")
    parser.add_argument(
        "--input", "-i",
        default="brand_model_map.json",
        help="Input JSON file with brand-model-variant map (default: brand_model_map.json)"
    )
    parser.add_argument(
        "--log-level", "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set the logging level (default: INFO)"
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Do not upload body_types_{date}.json to Azure Blob (even if config.yaml is present)"
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for body_types_{date}.json (default: script directory)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between models (default: 1.0)"
    )

    args = parser.parse_args()
    logger = setup_logging(args.log_level)

    with open(args.input, "r", encoding="utf-8") as f:
        brand_model_map = json.load(f)
    logger.info(f"Loaded {len(brand_model_map)} brands from {args.input}")

    config = load_config(logger)
    out_dir = args.output_dir or (config.get("output_dir") if config else None) or _script_dir()
    os.makedirs(out_dir, exist_ok=True)

    results = process_models(brand_model_map, logger, delay=args.delay)

    filename = f"body_types_{datetime.now().strftime('%Y-%m-%d')}.json"
    filepath = os.path.join(out_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    logger.info(f"Saved {filepath}")

    # Summary: coverage and any raw values not yet in BODY_TYPE_MAP
    entries = [entry for models in results.values() for entry in models.values()]
    missing = [f"{b} - {m}" for b, models in results.items() for m, e in models.items() if not e["body_type"]]
    unmapped = {e["body_type_raw"] for e in entries if e["body_type_raw"] and e["body_type_raw"].lower() not in BODY_TYPE_MAP}
    logger.info(f"Body type found for {len(entries) - len(missing)}/{len(entries)} models")
    logger.info(f"Body type counts: {dict(Counter(e['body_type'] for e in entries).most_common())}")
    if missing:
        logger.warning(f"Models without body type: {missing}")
    if unmapped:
        logger.warning(f"Raw body types not in BODY_TYPE_MAP (passed through title-cased): {sorted(unmapped)}")

    if config and not args.no_upload:
        upload_to_azure_blob(config, filepath, filename, logger)

    print(f"\nDone! Body types saved to {filepath}")


if __name__ == "__main__":
    main()
