"""
Script to ingest CarDekho per-brand variant files into Azure SQL.

This script:
1. Reads the latest {brand}_{date}.json file per brand (from Azure Blob, or a local directory)
2. Joins body type from the latest body_types_{date}.json (when the variant record has none)
3. Flattens each variant into one row with typed, normalized columns for the business fields
   (price, body type, dimensions, power/torque, fuel/EV fields) plus the raw specs JSON
4. Loads rows into cardekho.staging_variant_specs, then replaces the matching
   (snapshot_date, brand) rows in cardekho.fact_variant_specs in one transaction,
   so re-runs are idempotent and older snapshots are kept as history

Tables are created by sql/migrations/2026-10-06_create_cardekho_variant_specs.sql.

Config: config.yaml "azure" section for Blob, "database" section (server, database, username,
password) for SQL. --db-config can point at another config.yaml holding the "database" section.

Usage:
    python ingest_variant_specs.py --dry-run                      # transform + coverage report only
    python ingest_variant_specs.py --input-dir ./out --dry-run    # read local files instead of Blob
    python ingest_variant_specs.py --db-config /path/to/config.yaml
"""

import csv
import json
import logging
import os
import re
from collections import Counter
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

STAGING_TABLE = "cardekho.staging_variant_specs"
FACT_TABLE = "cardekho.fact_variant_specs"
REPLACEMENT_SCOPE = ["snapshot_date", "brand"]

# SQL Server rejects statements with more than 2100 parameters
MAX_STATEMENT_PARAMETERS = 2100
PARAMETER_HEADROOM = 10

BRAND_FILE_RE = re.compile(r"^(?P<brand>.+)_(?P<date>\d{4}-\d{2}-\d{2})\.json$")
BODY_TYPES_FILE_RE = re.compile(r"^body_types_(?P<date>\d{4}-\d{2}-\d{2})\.json$")
# "Top Selling\nGT Line (Petrol) 14.10 Lakh*" -> variant, fuel, amount, unit
VARIANT_LABEL_RE = re.compile(
    r"^(?P<variant>.*?)\s*\((?P<fuel>[^()]*)\)\s*(?P<amount>[\d.,]+)\s*(?P<unit>Lakh|Cr)\*?\s*$",
    re.DOTALL,
)
LABEL_PREFIX_RE = re.compile(r"^\s*(Top Selling|Recently Launched)\s*\n?\s*", re.IGNORECASE)
PRICE_MULTIPLIER = {"Lakh": 100_000, "Cr": 10_000_000}

ET = "Engine & Transmission"
FP = "Fuel & Performance"
DC = "Dimensions & Capacity"
KEY = None  # key_specifications (summary block at the top of the page)

# Business field -> ordered (section, key) lookups. The first non-empty value wins.
# Sections are explicit on purpose: some pages leak engine/dimension rows into unrelated
# sections (e.g. "Advance Internet Feature"), which must never be read.
FIELD_SOURCES = {
    "seating_capacity": [(DC, "Seating Capacity"), (KEY, "Seating Capacity")],
    "boot_space": [(DC, "Boot Space"), (DC, "Reported Boot Space"), (KEY, "Boot Space")],
    "ground_clearance_unladen": [
        (DC, "Ground Clearance Unladen"),
        (DC, "Reported Ground Clearance (Unladen)"),
        (KEY, "Ground Clearance"),
    ],
    "length": [(DC, "Length")],
    "width": [(DC, "Width")],
    "height": [(DC, "Height")],
    "max_power": [(ET, "Max Power"), (ET, "Motor Power"), (ET, "Power"), (KEY, "Power")],
    "max_torque": [(ET, "Max Torque"), (ET, "Torque"), (KEY, "Torque")],
    "transmission_type": [(ET, "Transmission Type"), (ET, "Transmission"), (KEY, "Transmission")],
    # EV only
    "range": [(ET, "Range"), (KEY, "Range")],
    "battery_capacity": [(ET, "Battery Capacity"), (KEY, "Battery Capacity")],
    "charging_port_type": [(ET, "Charging Port")],
    "charging_options": [(ET, "Charging Options"), (ET, "Charger Type")],
    "ac_charging_time": [
        (ET, "Charging Time (A.C)"),
        (KEY, "Charging Time AC"),
        (ET, "Charging Time (7.2 kW AC Fast Charger)"),
        (ET, "Charging Time (15 A Plug Point)"),
    ],
    "dc_charging_time": [
        (ET, "Charging Time (D.C)"),
        (KEY, "Charging Time DC"),
        (ET, "Charging Time (50 kW DC Fast Charger)"),
        (KEY, "Charging Time"),
    ],
}

# ICE only: tank and mileage keys depend on the fuel (CNG cars also list a petrol tank)
FUEL_TANK_SOURCES = {
    "Petrol": [(FP, "Petrol Fuel Tank Capacity"), (FP, "Petrol Fuel Tank Capacity (Litres)")],
    "Diesel": [(FP, "Diesel Fuel Tank Capacity")],
    "CNG": [(FP, "CNG Fuel Tank Capacity"), (FP, "Petrol Fuel Tank Capacity")],
}
FUEL_EFFICIENCY_SOURCES = {
    "Petrol": [(FP, "Petrol Mileage ARAI"), (FP, "Petrol Mileage (ARAI)"), (FP, "Petrol Mileage WLTP"), (KEY, "Mileage")],
    "Diesel": [(FP, "Diesel Mileage ARAI"), (KEY, "Mileage")],
    "CNG": [(FP, "CNG Mileage ARAI"), (KEY, "Mileage")],
}

COLUMNS = [
    "snapshot_date", "brand", "model", "variant", "variant_label", "is_top_selling",
    "fuel_type", "is_ev", "ex_showroom_price_inr", "body_type", "body_type_raw",
    "seating_capacity", "boot_space_l", "ground_clearance_unladen_mm",
    "length_mm", "width_mm", "height_mm",
    "max_power_bhp", "max_power", "max_torque_nm", "max_torque", "transmission_type",
    "fuel_tank_capacity_l", "fuel_efficiency", "fuel_efficiency_unit",
    "range_km", "battery_capacity_kwh", "charging_port_type", "charging_options",
    "ac_charging_time", "dc_charging_time",
    "source_url", "source_file", "specs_json",
]

# Business fields reported in the coverage summary, with the rows they apply to
COVERAGE_FIELDS = {
    "ex_showroom_price_inr": "all", "body_type": "all", "seating_capacity": "all",
    "boot_space_l": "all", "ground_clearance_unladen_mm": "all", "length_mm": "all",
    "width_mm": "all", "height_mm": "all", "max_power_bhp": "all", "max_torque_nm": "all",
    "transmission_type": "all", "fuel_tank_capacity_l": "ice", "fuel_efficiency": "ice",
    "range_km": "ev", "battery_capacity_kwh": "ev", "charging_port_type": "ev",
    "charging_options": "ev", "ac_charging_time": "ev", "dc_charging_time": "ev",
}


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('ingest_variant_specs.log'),
            logging.StreamHandler()
        ]
    )
    # Azure SDK logs every HTTP request/response at INFO
    logging.getLogger("azure").setLevel(logging.WARNING)
    return logging.getLogger(__name__)


def load_yaml(path: str) -> Dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required to read config files")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Value parsing
# ---------------------------------------------------------------------------

NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_number(value: Optional[str]) -> Optional[float]:
    """
    Parse the headline number from a spec string.

    - "113.98bhp@5000-5500rpm" -> 113.98 (only the part before "@"; rpm is not the value)
    - "113 - 157.57 bhp" / "6, 7" -> upper bound of the range (157.57 / 7)
    - "` Litres" -> None
    """
    if not value:
        return None
    head = value.split("@")[0]
    numbers = [float(n) for n in NUMBER_RE.findall(head)]
    return max(numbers) if numbers else None


def parse_battery_kwh(value: Optional[str]) -> Optional[float]:
    """Battery capacity in kWh; values that are actually voltages ("259.2v kWh") -> None."""
    if not value or re.search(r"\d\s*v\b", value, re.IGNORECASE):
        return None
    return parse_number(value)


def parse_fuel_efficiency(value: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """'19.4 kmpl' -> (19.4, 'kmpl'); '24 km/kg' -> (24.0, 'km/kg')."""
    number = parse_number(value)
    if number is None:
        return None, None
    unit = "km/kg" if "km/kg" in value.lower() else "kmpl" if "kmpl" in value.lower() else None
    return number, unit


def to_int(number: Optional[float]) -> Optional[int]:
    return int(round(number)) if number is not None else None


def parse_variant_label(label: str) -> Dict:
    """Split a variant label into display name, top-selling flag, fuel type and price in INR."""
    is_top_selling = bool(re.match(r"^\s*Top Selling", label, re.IGNORECASE))
    stripped = LABEL_PREFIX_RE.sub("", label)
    match = VARIANT_LABEL_RE.match(stripped)
    if not match:
        return {"variant": stripped.strip(), "is_top_selling": is_top_selling, "fuel_type": None, "price_inr": None}
    amount = float(match.group("amount").replace(",", ""))
    return {
        "variant": match.group("variant").strip(),
        "is_top_selling": is_top_selling,
        "fuel_type": match.group("fuel").strip(),
        "price_inr": int(round(amount * PRICE_MULTIPLIER[match.group("unit")])),
    }


def lookup(record: Dict, sources: List[Tuple[Optional[str], str]]) -> Optional[str]:
    """Return the first non-empty value from the given (section, key) sources."""
    for section, key in sources:
        if section is KEY:
            container = record.get("key_specifications") or {}
        else:
            container = (record.get("table_sections") or {}).get(section) or {}
        value = container.get(key)
        if value is not None and str(value).strip() not in ("", "-"):
            return str(value).strip()
    return None


def transform_variant(
    brand: str,
    model: str,
    label: str,
    record: Dict,
    snapshot_date: date,
    source_file: str,
    body_type_lookup: Dict,
) -> Dict:
    """Flatten one scraped variant record into a row keyed by COLUMNS."""
    parsed = parse_variant_label(label)
    is_ev = parsed["fuel_type"] == "Electric"

    def field(name):
        return lookup(record, FIELD_SOURCES[name])

    # Prefer body type captured by the scraper; fall back to the per-model backfill file
    body_type, body_type_raw = record.get("body_type"), record.get("body_type_raw")
    if not body_type:
        fallback = body_type_lookup.get(brand, {}).get(model, {})
        body_type, body_type_raw = fallback.get("body_type"), fallback.get("body_type_raw")

    max_power, max_torque = field("max_power"), field("max_torque")
    row = {
        "snapshot_date": snapshot_date,
        "brand": brand,
        "model": model,
        "variant": parsed["variant"],
        "variant_label": label,
        "is_top_selling": parsed["is_top_selling"],
        "fuel_type": parsed["fuel_type"],
        "is_ev": is_ev,
        "ex_showroom_price_inr": parsed["price_inr"],
        "body_type": body_type,
        "body_type_raw": body_type_raw,
        "seating_capacity": to_int(parse_number(field("seating_capacity"))),
        "boot_space_l": parse_number(field("boot_space")),
        "ground_clearance_unladen_mm": to_int(parse_number(field("ground_clearance_unladen"))),
        "length_mm": to_int(parse_number(field("length"))),
        "width_mm": to_int(parse_number(field("width"))),
        "height_mm": to_int(parse_number(field("height"))),
        "max_power_bhp": parse_number(max_power),
        "max_power": max_power,
        "max_torque_nm": parse_number(max_torque),
        "max_torque": max_torque,
        "transmission_type": field("transmission_type"),
        "fuel_tank_capacity_l": None,
        "fuel_efficiency": None,
        "fuel_efficiency_unit": None,
        "range_km": None,
        "battery_capacity_kwh": None,
        "charging_port_type": None,
        "charging_options": None,
        "ac_charging_time": None,
        "dc_charging_time": None,
        "source_url": record.get("url"),
        "source_file": source_file,
        "specs_json": json.dumps(record, ensure_ascii=False),
    }

    if is_ev:
        row.update({
            "range_km": to_int(parse_number(field("range"))),
            "battery_capacity_kwh": parse_battery_kwh(field("battery_capacity")),
            "charging_port_type": field("charging_port_type"),
            "charging_options": field("charging_options"),
            "ac_charging_time": field("ac_charging_time"),
            "dc_charging_time": field("dc_charging_time"),
        })
    elif parsed["fuel_type"] in FUEL_TANK_SOURCES:
        efficiency, unit = parse_fuel_efficiency(lookup(record, FUEL_EFFICIENCY_SOURCES[parsed["fuel_type"]]))
        row.update({
            "fuel_tank_capacity_l": parse_number(lookup(record, FUEL_TANK_SOURCES[parsed["fuel_type"]])),
            "fuel_efficiency": efficiency,
            "fuel_efficiency_unit": unit,
        })
    return row


# ---------------------------------------------------------------------------
# Sources (Azure Blob or local directory)
# ---------------------------------------------------------------------------

def select_latest_files(names: List[str], snapshot_date: Optional[str]) -> Tuple[Dict[str, Tuple[str, str]], Optional[str]]:
    """
    Pick the latest brand file per brand (or exactly snapshot_date) and the latest body types file.

    Returns ({brand_file_segment: (file_name, date)}, body_types_file_name or None).
    """
    brand_files: Dict[str, Tuple[str, str]] = {}
    body_types_file, body_types_date = None, ""
    for name in names:
        base = os.path.basename(name)
        body_match = BODY_TYPES_FILE_RE.match(base)
        if body_match:
            if body_match.group("date") > body_types_date:
                body_types_file, body_types_date = name, body_match.group("date")
            continue
        match = BRAND_FILE_RE.match(base)
        if not match:
            continue
        brand_segment, file_date = match.group("brand"), match.group("date")
        if snapshot_date and file_date != snapshot_date:
            continue
        if brand_segment not in brand_files or file_date > brand_files[brand_segment][1]:
            brand_files[brand_segment] = (name, file_date)
    return brand_files, body_types_file


class LocalSource:
    def __init__(self, directory: str):
        self.directory = directory

    def list_names(self) -> List[str]:
        return sorted(os.listdir(self.directory))

    def read_json(self, name: str):
        with open(os.path.join(self.directory, name), "r", encoding="utf-8") as f:
            return json.load(f)


class BlobSource:
    def __init__(self, azure_cfg: Dict):
        from azure.storage.blob import BlobServiceClient
        self.prefix = (azure_cfg.get("blob_prefix") or "").strip().rstrip("/")
        self.container = BlobServiceClient.from_connection_string(
            azure_cfg["connection_string"]
        ).get_container_client(azure_cfg["container_name"])

    def list_names(self) -> List[str]:
        return sorted(b.name for b in self.container.list_blobs(name_starts_with=self.prefix or None))

    def read_json(self, name: str):
        return json.loads(self.container.download_blob(name).readall())


def build_rows(source, snapshot_date: Optional[str], logger: logging.Logger) -> List[Dict]:
    brand_files, body_types_file = select_latest_files(source.list_names(), snapshot_date)
    if not brand_files:
        raise RuntimeError(f"No brand files found{' for ' + snapshot_date if snapshot_date else ''}")
    logger.info(f"Selected {len(brand_files)} brand files; dates: {dict(Counter(d for _, d in brand_files.values()))}")

    body_type_lookup = {}
    if body_types_file:
        body_type_lookup = source.read_json(body_types_file)
        logger.info(f"Body type fallback: {body_types_file}")
    else:
        logger.warning("No body_types_{date}.json found; body type only from variant records")

    rows = []
    for brand_segment, (name, file_date) in sorted(brand_files.items()):
        # Brand names are written with spaces replaced by "_" (e.g. Land_Rover); the
        # brand_model_map / body types file use the original names.
        brand = brand_segment.replace("_", " ")
        snapshot = datetime.strptime(file_date, "%Y-%m-%d").date()
        for model, variants in source.read_json(name).items():
            for label, record in variants.items():
                rows.append(transform_variant(brand, model, label, record, snapshot, os.path.basename(name), body_type_lookup))
    return rows


# ---------------------------------------------------------------------------
# Reporting and loading
# ---------------------------------------------------------------------------

def log_coverage(rows: List[Dict], logger: logging.Logger) -> None:
    ev_rows = sum(1 for r in rows if r["is_ev"])
    logger.info(f"Rows: {len(rows)} (EV {ev_rows}, ICE {len(rows) - ev_rows}); fuel types: {dict(Counter(r['fuel_type'] for r in rows))}")
    for column, scope in COVERAGE_FIELDS.items():
        applicable = [r for r in rows if scope == "all" or (scope == "ev") == r["is_ev"]]
        filled = sum(1 for r in applicable if r[column] is not None)
        pct = 100 * filled / len(applicable) if applicable else 0
        logger.info(f"  {column:28s} {filled:5d}/{len(applicable):<5d} {pct:5.1f}%  ({scope})")
    unparsed = [r["variant_label"] for r in rows if r["fuel_type"] is None or r["ex_showroom_price_inr"] is None]
    if unparsed:
        logger.warning(f"{len(unparsed)} variant labels without fuel/price: {unparsed[:10]}")


def write_csv(rows: List[Dict], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def connect(db_cfg: Dict):
    import pyodbc
    return pyodbc.connect(
        driver="{ODBC Driver 18 for SQL Server}",
        server=db_cfg["server"],
        database=db_cfg["database"],
        uid=db_cfg["username"],
        pwd=db_cfg["password"],
        timeout=60,  # serverless database may need to resume
    )


def load_rows(rows: List[Dict], db_cfg: Dict, logger: logging.Logger) -> None:
    """Stage rows, then replace the (snapshot_date, brand) scope in the fact table atomically."""
    conn = connect(db_cfg)
    cursor = conn.cursor()
    try:
        cursor.execute(f"TRUNCATE TABLE {STAGING_TABLE}")

        # Multi-row INSERT ... VALUES: per-row round trips dominate on this database
        column_count = len(COLUMNS) + 1  # + inserted_at
        per_statement = max(1, (MAX_STATEMENT_PARAMETERS - PARAMETER_HEADROOM) // column_count)
        column_list = ", ".join(f"[{c}]" for c in COLUMNS + ["inserted_at"])
        inserted_at = datetime.now()
        for start in range(0, len(rows), per_statement):
            chunk = rows[start:start + per_statement]
            placeholders = ", ".join("(" + ", ".join("?" * column_count) + ")" for _ in chunk)
            params = []
            for row in chunk:
                params.extend(row[c] for c in COLUMNS)
                params.append(inserted_at)
            cursor.execute(f"INSERT INTO {STAGING_TABLE} ({column_list}) VALUES {placeholders}", params)
            logger.info(f"  staged {min(start + len(chunk), len(rows))}/{len(rows)} rows")
        conn.commit()

        scope = " AND ".join(f"s.[{c}] = f.[{c}]" for c in REPLACEMENT_SCOPE)
        cursor.execute(
            f"DELETE f FROM {FACT_TABLE} f WHERE EXISTS (SELECT 1 FROM {STAGING_TABLE} s WHERE {scope})"
        )
        deleted = cursor.rowcount
        cursor.execute(f"INSERT INTO {FACT_TABLE} ({column_list}) SELECT {column_list} FROM {STAGING_TABLE}")
        inserted = cursor.rowcount
        conn.commit()
        logger.info(f"Replaced {deleted} existing rows with {inserted} rows in {FACT_TABLE}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    """Main entry point."""
    import argparse

    script_dir = os.path.dirname(os.path.abspath(__file__))
    parser = argparse.ArgumentParser(description="Ingest CarDekho variant files into Azure SQL")
    parser.add_argument(
        "--input-dir",
        default=None,
        help="Read brand/body type files from this directory instead of Azure Blob"
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Only ingest brand files with this date (YYYY-MM-DD). Default: latest file per brand"
    )
    parser.add_argument(
        "--db-config",
        default=os.path.join(script_dir, "config.yaml"),
        help="config.yaml holding the 'database' section (default: this repo's config.yaml)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Transform and report coverage only; do not write to the database"
    )
    parser.add_argument(
        "--csv",
        default=None,
        help="Also write the transformed rows to this CSV file"
    )
    parser.add_argument(
        "--log-level", "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set the logging level (default: INFO)"
    )
    args = parser.parse_args()
    logger = setup_logging(args.log_level)

    if args.input_dir:
        source = LocalSource(args.input_dir)
    else:
        source = BlobSource(load_yaml(os.path.join(script_dir, "config.yaml"))["azure"])

    rows = build_rows(source, args.snapshot_date, logger)
    log_coverage(rows, logger)
    if args.csv:
        write_csv(rows, args.csv)
        logger.info(f"Wrote {args.csv}")

    if args.dry_run:
        logger.info("Dry run: database not touched")
        return

    db_cfg = load_yaml(args.db_config).get("database")
    if not db_cfg:
        raise RuntimeError(f"No 'database' section in {args.db_config}")
    load_rows(rows, db_cfg, logger)


if __name__ == "__main__":
    main()
