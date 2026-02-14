"""
Script to scrape key specifications from CarDekho variant pages.

This script:
1. Reads brand-model-variant data from a JSON file
2. Cleans variant names to create URL-friendly format
3. Builds CarDekho URLs for each variant
4. Scrapes key specifications and table sections (with icon check/cross -> Yes/No)
5. Saves combined results to a JSON file and per-brand files as {brand_name}_{date}.json
6. Optionally uploads per-brand files to Azure Blob Storage (when config.yaml is present)

Config: place config.yaml in the same directory with an "azure" section (connection_string,
container_name, optional blob_prefix). See config.yaml.example or config.yaml.

Usage:
    python get_variant_features.py --input brand_model_map.json --output variant_features.json
    python get_variant_features.py --no-upload          # skip Azure upload
    python get_variant_features.py --output-dir ./out   # where to write per-brand files
"""

import json
import time
import re
import logging
import os
from datetime import datetime
from typing import Dict, List, Optional, Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore

try:
    from azure.storage.blob import BlobServiceClient
    AZURE_AVAILABLE = True
except ImportError:
    BlobServiceClient = None  # type: ignore
    AZURE_AVAILABLE = False

from selenium import webdriver
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait


def setup_logging(log_level: str = "INFO") -> logging.Logger:
    """Setup logging configuration."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('variant_features_scraper.log'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


def _script_dir() -> str:
    """Return the directory containing this script (for config.yaml lookup)."""
    return os.path.dirname(os.path.abspath(__file__))


def load_config(logger: Optional[logging.Logger] = None) -> Optional[Dict[str, Any]]:
    """
    Load config from config.yaml in the same directory as this script.
    Returns None if config.yaml is missing or invalid.
    """
    if yaml is None:
        if logger:
            logger.debug("PyYAML not installed; config.yaml will not be loaded")
        return None
    config_path = os.path.join(_script_dir(), "config.yaml")
    if not os.path.isfile(config_path):
        if logger:
            logger.debug(f"Config file not found: {config_path}")
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if logger:
            logger.info(f"Loaded config from {config_path}")
        return config
    except Exception as e:
        if logger:
            logger.warning(f"Failed to load config.yaml: {e}")
        return None


def sanitize_brand_for_filename(brand: str) -> str:
    """Convert brand name to a safe filename segment (e.g. 'Maruti Suzuki' -> 'Maruti_Suzuki')."""
    return re.sub(r"[^\w\-.]", "_", brand).strip("_").replace(" ", "_") or "unknown"


def upload_to_azure_blob(
    config: Dict[str, Any],
    local_file_path: str,
    blob_name: str,
    logger: logging.Logger,
) -> bool:
    """
    Upload a local file to Azure Blob Storage using settings from config.
    Expects config['azure'] with connection_string and container_name (and optional blob_prefix).
    Returns True on success, False otherwise.
    """
    if not AZURE_AVAILABLE:
        logger.warning("azure-storage-blob not installed; skipping upload")
        return False
    azure_cfg = config.get("azure") or {}
    connection_string = azure_cfg.get("connection_string")
    container_name = azure_cfg.get("container_name")
    if not connection_string or not container_name:
        logger.debug("Azure config missing connection_string or container_name; skipping upload")
        return False
    prefix = (azure_cfg.get("blob_prefix") or "").strip().rstrip("/")
    if prefix:
        blob_name = f"{prefix}/{blob_name}"
    if not os.path.isfile(local_file_path):
        logger.warning(f"Cannot upload: file not found {local_file_path}")
        return False
    try:
        client = BlobServiceClient.from_connection_string(connection_string)
        container = client.get_container_client(container_name)
        try:
            container.get_container_properties()
        except Exception:
            container.create_container()
            logger.info(f"Created container: {container_name}")
        with open(local_file_path, "rb") as f:
            container.upload_blob(name=blob_name, data=f, overwrite=True)
        logger.info(f"Uploaded to Azure Blob: {blob_name}")
        return True
    except Exception as e:
        logger.error(f"Azure Blob upload failed for {blob_name}: {e}")
        return False


def create_browser_options() -> webdriver.ChromeOptions:
    """Create and configure Chrome browser options."""
    browser_options = webdriver.ChromeOptions()
    browser_options.browser_version = "stable"
    browser_prefs = {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
    }
    browser_options.add_experimental_option("prefs", browser_prefs)
    browser_options.add_argument("--disable-notifications")
    return browser_options


def create_browser(options: webdriver.ChromeOptions) -> webdriver.Chrome:
    """Create a new Chrome browser instance."""
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), 
        options=options
    )


def clean_variant_name(variant: str) -> str:
    """
    Clean variant name to create URL-friendly format.
    
    Examples:
    - "Pack One (Electric) 18.90 Lakh*" -> "Pack_One"
    - "Pack One 7.2kw Charger (Electric) 19.40 Lakh*" -> "Pack_One_7.2kw_Charger"
    - "Top Selling\nVXI (Petrol) 4.50 Lakh*" -> "VXI"
    - "Pack Two 79kwh (Electric) 23.50 Lakh*" -> "Pack_Two_79kwh"
    - "LXI S-CNG (CNG) 4.82 Lakh*" -> "LXI_S_CNG"
    """
    # Remove "Top Selling", "Recently Launched" and newlines
    variant = re.sub(r'(Top Selling|Recently Launched)\s*\n?\s*', '', variant, flags=re.IGNORECASE)
    
    # Remove price information (everything after the last closing parenthesis or from "Lakh" onwards)
    variant = re.sub(r'\s*\([^)]*\)\s*.*$', '', variant)  # Remove (Electric) 18.90 Lakh*
    variant = re.sub(r'\s+\d+\.?\d*\s*Lakh.*$', '', variant)  # Remove price if still present
    
    # Remove any remaining parentheses and their contents
    variant = re.sub(r'\([^)]*\)', '', variant)
    
    # Remove asterisks and other special characters that might be in price
    variant = re.sub(r'[*]+', '', variant)
    
    # Replace hyphens with underscores (e.g., S-CNG -> S_CNG)
    variant = variant.replace('-', '_')
    
    # Clean up whitespace (replace multiple spaces/newlines with single space)
    variant = re.sub(r'\s+', ' ', variant)
    variant = variant.strip()
    
    # Replace spaces with underscores
    variant = variant.replace(' ', '_')
    
    # Remove any trailing underscores
    variant = variant.rstrip('_')
    
    return variant


def build_url(brand: str, model: str, variant: str) -> str:
    """
    Build CarDekho URL for a variant.
    
    Format: https://www.cardekho.com/overview/{Brand}_{Model}/{Brand}_{Model}_{Variant}.htm
    
    Note: Some variants might not have individual pages or might use different formats.
    This function generates the standard format, but the extraction function should handle 404s.
    """
    # Clean brand and model names for URL
    brand_clean = brand.replace(' ', '_')
    model_clean = model.replace(' ', '_')
    variant_clean = clean_variant_name(variant)
    
    url = f"https://www.cardekho.com/overview/{brand_clean}_{model_clean}/{brand_clean}_{model_clean}_{variant_clean}.htm"
    return url


def extract_table_sections(browser: webdriver.Chrome, url: str, logger: logging.Logger) -> Dict[str, Dict[str, str]]:
    """
    Extract table data sections from the variant page.
    
    This function extracts detailed specifications from various table sections on CarDekho variant pages.
    Each section contains key-value pairs in table format (<tr> rows with 2 <td> elements).
    
    Sections to extract:
    - Engine & Transmission: Engine details, transmission type, gearbox, etc.
    - Fuel & Performance: Fuel type, mileage, tank capacity, emissions
    - Suspension, Steering & Brakes: Suspension type, steering, brakes, performance metrics
    - Dimensions & Capacity: Length, width, height, boot space, seating capacity
    - Comfort & Convenience: Parking sensors, power windows, climate control, etc.
    - Interior: Upholstery, interior features, storage, etc.
    - Exterior: Mirrors, tyres, wheels, antenna, etc.
    - Safety: Airbags, safety ratings, safety features
    - Entertainment & Communication: Audio system, touchscreen, connectivity features
    - Advance Internet Feature: Internet connectivity, smart features (may be "Advanced" on some pages)
    
    Strategy:
    1. First, find all tables on the page and their associated section headings
    2. For each target section, try multiple search strategies:
       a. Find section heading by text (h2, h3, div, th elements)
       b. Try variations of section names (e.g., "Advance" vs "Advanced")
       c. Find the closest table following the heading
    3. Extract key-value pairs from each table row
    
    Returns a dictionary with section names as keys and key-value pairs as values.
    """
    sections_data = {}
    
    # Define section names with their possible variations
    # Some sections might be named slightly differently on different pages
    section_configs = {
        "Engine & Transmission": ["Engine & Transmission", "Engine and Transmission", "Engine &amp; Transmission"],
        "Fuel & Performance": ["Fuel & Performance", "Fuel and Performance", "Fuel &amp; Performance"],
        "Suspension, Steering & Brakes": ["Suspension, Steering & Brakes", "Suspension, Steering and Brakes"],
        "Dimensions & Capacity": ["Dimensions & Capacity", "Dimensions and Capacity", "Dimensions &amp; Capacity"],
        "Comfort & Convenience": ["Comfort & Convenience", "Comfort and Convenience", "Comfort &amp; Convenience"],
        "Interior": ["Interior"],  # Usually just "Interior"
        "Exterior": ["Exterior"],  # Usually just "Exterior"
        "Safety": ["Safety"],  # Usually just "Safety"
        "Entertainment & Communication": ["Entertainment & Communication", "Entertainment and Communication", 
                                         "Entertainment &amp; Communication"],
        "Advance Internet Feature": ["Advance Internet Feature", "Advanced Internet Feature", 
                                     "Advance Internet Features", "Advanced Internet Features"]
    }
    
    try:
        # Strategy 1: Find all tables first and try to match them to sections
        # This helps when section headings are not clearly visible
        all_tables = browser.find_elements(By.XPATH, '//table')
        logger.debug(f"Found {len(all_tables)} tables on the page")
        
        # Strategy 2: For each target section, find it using multiple approaches
        for canonical_section_name, section_name_variations in section_configs.items():
            try:
                section_element = None
                table = None
                
                # Approach 1: Find section heading by searching for text in various HTML elements
                # Try different HTML tags that might contain section headings
                heading_tags = ['h2', 'h3', 'h4', 'div', 'th', 'span', 'strong', 'b']
                
                for tag in heading_tags:
                    for variation in section_name_variations:
                        try:
                            # Case-insensitive search using translate() for XPath
                            pattern = f'//{tag}[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "{variation.lower()}")]'
                            elements = browser.find_elements(By.XPATH, pattern)
                            
                            for elem in elements:
                                elem_text = elem.text.strip().lower()
                                # Check if the element text contains our section name
                                if any(var.lower() in elem_text for var in section_name_variations):
                                    # Additional check: make sure it's not just a partial match in a longer text
                                    # The section name should be a significant part of the element text
                                    if len(elem_text) < len(variation) * 2:  # Heading should be roughly the section name
                                        section_element = elem
                                        logger.debug(f"Found section heading for '{canonical_section_name}' using {tag} tag")
                                        break
                            
                            if section_element:
                                break
                        except Exception as e:
                            logger.debug(f"Error searching for {canonical_section_name} in {tag}: {e}")
                            continue
                    
                    if section_element:
                        break
                
                # Approach 2: If heading not found, try searching in parent containers
                # Sometimes the heading text is split across multiple elements
                if not section_element:
                    for variation in section_name_variations:
                        try:
                            # Look for elements containing the section name anywhere in their text
                            pattern = f'//*[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "{variation.lower()}")]'
                            elements = browser.find_elements(By.XPATH, pattern)
                            
                            for elem in elements:
                                elem_text = elem.text.strip().lower()
                                # Check if this looks like a section heading (not too long, contains section name)
                                if variation.lower() in elem_text and len(elem_text) < 100:
                                    # Check if it's likely a heading (short text, might be in heading-like element)
                                    tag_name = elem.tag_name.lower()
                                    if tag_name in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'th'] or 'heading' in tag_name:
                                        section_element = elem
                                        logger.debug(f"Found section heading for '{canonical_section_name}' in container")
                                        break
                        except:
                            continue
                
                # Approach 3: Find the table associated with the section heading
                if section_element:
                    # Method 1: Look for table in the same parent container
                    try:
                        parent = section_element.find_element(By.XPATH, './..')
                        tables = parent.find_elements(By.XPATH, './/table')
                        if tables:
                            # Find the table that comes after the heading
                            for t in tables:
                                try:
                                    heading_y = section_element.location['y']
                                    table_y = t.location['y']
                                    if table_y >= heading_y:  # Table is below or at heading position
                                        table = t
                                        break
                                except:
                                    # If location check fails, use first table in parent
                                    table = tables[0]
                                    break
                    except:
                        pass
                    
                    # Method 2: Look for table as following sibling
                    if not table:
                        try:
                            table = section_element.find_element(By.XPATH, './following-sibling::table[1]')
                        except:
                            pass
                    
                    # Method 3: Find closest table after the heading using document position
                    if not table:
                        try:
                            heading_y = section_element.location['y']
                            closest_table = None
                            min_distance = float('inf')
                            
                            for t in all_tables:
                                try:
                                    table_y = t.location['y']
                                    # Only consider tables that come after the heading
                                    if table_y >= heading_y:
                                        distance = table_y - heading_y
                                        # Prefer tables that are close but not too far (within 1000px)
                                        if distance < min_distance and distance < 1000:
                                            min_distance = distance
                                            closest_table = t
                                except:
                                    continue
                            
                            if closest_table:
                                table = closest_table
                        except:
                            pass
                    
                    # Method 4: Look for table in following elements
                    if not table:
                        try:
                            # Find the next table element that follows our heading in document order
                            following_tables = browser.find_elements(
                                By.XPATH, 
                                f'//table[preceding::*[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "{section_name_variations[0].lower()}")]]'
                            )
                            if following_tables:
                                # Use the first table that follows the heading
                                table = following_tables[0]
                        except:
                            pass
                
                # Approach 4: If we still don't have a table, try to find tables by content
                # Some sections might not have clear headings, but we can identify them by table content
                if not table:
                    # Look for tables that might contain section-specific keywords
                    section_keywords = {
                        "Engine & Transmission": ["engine", "transmission", "gearbox", "displacement", "cylinders"],
                        "Fuel & Performance": ["fuel", "mileage", "tank", "emission", "performance"],
                        "Suspension, Steering & Brakes": ["suspension", "steering", "brake", "turning radius"],
                        "Dimensions & Capacity": ["length", "width", "height", "boot space", "seating capacity"],
                        "Comfort & Convenience": ["parking", "power windows", "climate", "convenience"],
                        "Interior": ["upholstery", "interior", "fabric", "leather", "dashboard"],
                        "Exterior": ["mirror", "tyre", "wheel", "antenna", "exterior"],
                        "Safety": ["airbag", "safety", "ncap", "rating"],
                        "Entertainment & Communication": ["speaker", "touchscreen", "audio", "connectivity", "entertainment"],
                        "Advance Internet Feature": ["internet", "connectivity", "smart", "app", "remote"]
                    }
                    
                    if canonical_section_name in section_keywords:
                        keywords = section_keywords[canonical_section_name]
                        for t in all_tables:
                            try:
                                table_text = t.text.lower()
                                # Check if table contains keywords for this section
                                if any(keyword in table_text for keyword in keywords):
                                    # Make sure it's not already assigned to another section
                                    if t not in [sections_data.get(s, {}).get('_table_element') for s in sections_data]:
                                        table = t
                                        logger.debug(f"Found table for '{canonical_section_name}' by content matching")
                                        break
                            except:
                                continue
                
                # Extract key-value pairs from the table
                if table:
                    section_data = {}
                    try:
                        rows = table.find_elements(By.XPATH, './/tr')
                        logger.debug(f"Found {len(rows)} rows in table for '{canonical_section_name}'")
                        
                        for row in rows:
                            try:
                                cells = row.find_elements(By.XPATH, './/td')
                                if len(cells) >= 2:
                                    # First cell is the key, second is the value
                                    key = cells[0].text.strip()
                                    
                                    # Extract value from second cell
                                    # Check if the cell contains icon elements (checkmarks/crosses)
                                    # Some tables use icons instead of text: ✓ (checkmark) = Yes, ✗ (cross) = No
                                    value_cell = cells[1]
                                    
                                    # Check for icon elements that indicate Yes/No features
                                    # Common icon classes used on CarDekho:
                                    # - icon-check, icon-tick, icon-checkmark = Yes (feature available)
                                    # - icon-deletearrow, icon-cross, icon-close, icon-remove = No (feature not available)
                                    try:
                                        # Look for check/tick icons (feature available = Yes)
                                        check_icon_patterns = [
                                            './/i[contains(@class, "icon-check")]',
                                            './/i[contains(@class, "icon-tick")]',
                                            './/i[contains(@class, "icon-checkmark")]',
                                            './/i[contains(@class, "check")]',
                                            './/span[contains(@class, "icon-check")]',
                                            './/span[contains(@class, "icon-tick")]',
                                        ]
                                        
                                        # Look for cross/delete icons (feature not available = No)
                                        delete_icon_patterns = [
                                            './/i[contains(@class, "icon-deletearrow")]',
                                            './/i[contains(@class, "icon-cross")]',
                                            './/i[contains(@class, "icon-close")]',
                                            './/i[contains(@class, "icon-remove")]',
                                            './/i[contains(@class, "delete")]',
                                            './/span[contains(@class, "icon-deletearrow")]',
                                            './/span[contains(@class, "icon-cross")]',
                                        ]
                                        
                                        # Check for check icons first
                                        has_check = False
                                        for pattern in check_icon_patterns:
                                            try:
                                                icons = value_cell.find_elements(By.XPATH, pattern)
                                                if icons:
                                                    has_check = True
                                                    break
                                            except:
                                                continue
                                        
                                        # Check for delete/cross icons
                                        has_delete = False
                                        if not has_check:  # Only check if no check icon found
                                            for pattern in delete_icon_patterns:
                                                try:
                                                    icons = value_cell.find_elements(By.XPATH, pattern)
                                                    if icons:
                                                        has_delete = True
                                                        break
                                                except:
                                                    continue
                                        
                                        # Set value based on icon found
                                        if has_check:
                                            # Feature is available (checkmark icon found)
                                            value = "Yes"
                                        elif has_delete:
                                            # Feature is not available (cross icon found)
                                            value = "No"
                                        else:
                                            # No icons found, use text content
                                            value = value_cell.text.strip()
                                    except Exception as icon_error:
                                        # If icon detection fails, fall back to text
                                        logger.debug(f"Icon detection failed, using text: {icon_error}")
                                        value = value_cell.text.strip()
                                    
                                    # Clean up the key and value
                                    key = re.sub(r':\s*$', '', key).strip()  # Remove trailing colon
                                    key = re.sub(r'\s+', ' ', key)  # Normalize whitespace
                                    value = re.sub(r'\s+', ' ', value).strip()  # Normalize whitespace
                                    
                                    # Skip empty values, rows where key == value, and very long values
                                    # Also skip if key looks like a section heading (too long or contains "&")
                                    if (key and value and 
                                        key.lower() != value.lower() and 
                                        len(value) < 500 and
                                        len(key) < 100):  # Reasonable key length
                                        section_data[key] = value
                            except Exception as e:
                                logger.debug(f"Error processing row in {canonical_section_name}: {e}")
                                continue
                        
                        if section_data:
                            sections_data[canonical_section_name] = section_data
                            logger.debug(f"✓ Extracted {len(section_data)} items from '{canonical_section_name}'")
                        else:
                            logger.debug(f"Table found for '{canonical_section_name}' but no data extracted")
                    except Exception as e:
                        logger.debug(f"Error extracting data from table for {canonical_section_name}: {e}")
                else:
                    logger.debug(f"✗ Could not find table for section: {canonical_section_name}")
                    
            except Exception as e:
                logger.debug(f"Error processing section {canonical_section_name}: {e}")
                continue
        
        logger.info(f"Extracted {len(sections_data)} table sections: {list(sections_data.keys())}")
        return sections_data
        
    except Exception as e:
        logger.warning(f"Error extracting table sections from {url}: {e}")
        return {}


def extract_key_specifications(browser: webdriver.Chrome, url: str, logger: logging.Logger) -> Optional[Dict]:
    """
    Extract ALL key specifications and table sections from the variant page dynamically.
    
    This is the main extraction function that:
    1. Loads the CarDekho variant page
    2. Validates the page loaded correctly (not 404)
    3. Extracts key specifications from the overview section (top of page)
    4. Extracts detailed table sections (Engine & Transmission, etc.)
    
    Different cars have different specifications:
    - Electric cars: Range, Battery Capacity, Charging Time AC/DC, Power, Boot Space
    - Petrol/Diesel cars: Engine, Transmission, Fuel, Power, Mileage, Airbags, etc.
    
    Also extracts table sections:
    - Engine & Transmission, Fuel & Performance, Suspension/Steering/Brakes, etc.
    
    Returns a dictionary with:
    - 'key_specifications': Dict of key specifications from overview section (quick specs at top)
    - 'table_sections': Dict of table sections with their key-value pairs (detailed specs)
    
    Returns None if page is not found (404) or cannot be accessed.
    """
    try:
        # Step 1: Load the page
        logger.debug(f"Fetching URL: {url}")
        browser.get(url)
        time.sleep(5)  # Wait for page to load - CarDekho pages can be slow to render
        
        # Step 2: Wait for page to be fully loaded (JavaScript complete)
        # This ensures all dynamic content is rendered before we try to extract data
        try:
            WebDriverWait(browser, 10).until(
                lambda driver: driver.execute_script('return document.readyState') == 'complete'
            )
        except:
            # If timeout, continue anyway - page might still be usable
            pass
        
        # Step 3: Validate page loaded successfully (not a 404 error page)
        # We need to be careful here because "404" might appear in content (e.g., "404 HP")
        page_title = browser.title.lower()
        current_url = browser.current_url.lower()
        
        # Check for clear 404 indicators in URL
        if '404' in current_url or '/404' in current_url or '/error' in current_url:
            logger.warning(f"Page not found (404) for {url} - redirected to: {current_url}")
            return None
        
        # Check page source for 404 indicators (be very careful - "404" might appear in content)
        # Only check for clear 404 page indicators, not just the number "404"
        page_source_lower = browser.page_source.lower()
        
        # Look for clear 404 page patterns (not just the number 404 which might be in specs)
        clear_404_patterns = [
            'page not found',
            '404 error',
            'this page does not exist',
            'the page you are looking for cannot be found',
            'error 404',
            '404 - page not found'
        ]
        
        # Check if we see a clear 404 message (not just the number)
        has_404_message = any(pattern in page_source_lower for pattern in clear_404_patterns)
        
        # Also check if main content area is missing (strong indicator of 404)
        try:
            main_element = browser.find_element(By.XPATH, '//main')
            has_main_content = len(main_element.text.strip()) > 100  # Has substantial content
        except:
            has_main_content = False
        
        # Only flag as 404 if we have clear 404 message AND no main content
        if has_404_message and not has_main_content:
            logger.warning(f"Page appears to be 404 for {url}")
            return None
        
        # Check if we're on a CarDekho page (should have cardekho in URL or title)
        if 'cardekho' not in current_url and 'cardekho' not in page_title:
            logger.warning(f"Not on CarDekho page for {url} - current URL: {current_url}")
            return None
        
        # Step 4: Extract key specifications from the overview section (top of page)
        # This section shows quick specs like Engine, Power, Transmission, Mileage, etc.
        wait = WebDriverWait(browser, 10)
        specs = {}
        
        try:
            # Find the Key Specification section (overview table at top of page)
            # This is different from the detailed table sections - it's a quick summary
            # The structure varies, so we try multiple XPath patterns to find it
            
            key_spec_section = None
            
            # Strategy: Try multiple XPath selectors in order of reliability
            # We prioritize specific paths that have worked in testing, then fall back to generic patterns
            selectors_to_try = [
                # User's specific XPath structure (most reliable based on testing)
                '//*[@id="rf01"]//main//section[1]//table',
                # Simplified version of user's XPath
                '//main//section[1]//table',
                # Look for the div container the user mentioned
                '//*[@id="rf01"]/div[1]/div/main/div/div[1]/section[1]/div/div[1]/div[1]//table',
                # Look for section containing "Key Specification" or "overview" text
                '//section[.//text()[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "key specification")]]//table',
                '//section[.//text()[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "overview")]]//table',
                # Look for overview section by class
                '//section[contains(@class, "overview")]//table',
                '//div[contains(@class, "overview")]//table',
                # Look for specification tables
                '//table[contains(@class, "spec")]',
                '//div[contains(@class, "spec")]//table',
            ]
            
            for selector in selectors_to_try:
                try:
                    elements = browser.find_elements(By.XPATH, selector)
                    if elements:
                        # Check if this element contains specification data
                        for elem in elements:
                            text = elem.text.lower()
                            # Check if it looks like a specification table
                            if any(keyword in text for keyword in ['range', 'power', 'engine', 'transmission', 'mileage', 'battery', 'fuel']):
                                key_spec_section = elem
                                logger.debug(f"Found Key Specification section using: {selector}")
                                break
                        if key_spec_section:
                            break
                except Exception as e:
                    logger.debug(f"Selector {selector} failed: {e}")
                    continue
            
            # If still not found, try to find by looking for the table structure
            if not key_spec_section:
                try:
                    # Look for the first section in main that contains a table
                    main_section = browser.find_element(By.XPATH, '//main//section[1]')
                    tables = main_section.find_elements(By.XPATH, './/table')
                    if tables:
                        key_spec_section = tables[0]
                        logger.debug("Found table in first main section")
                except:
                    pass
            
            if not key_spec_section:
                logger.debug(f"Could not find Key Specification section for {url}")
                # Try one more approach: look for any table with specification-like content
                try:
                    all_tables = browser.find_elements(By.XPATH, '//table')
                    for table in all_tables:
                        rows = table.find_elements(By.XPATH, './/tr')
                        if len(rows) >= 2:  # Has at least 2 rows (likely a spec table)
                            # Check if it has the structure we expect (td elements)
                            first_row = rows[0]
                            cells = first_row.find_elements(By.XPATH, './/td | .//th')
                            if len(cells) >= 2:
                                key_spec_section = table
                                logger.debug("Found specification table by structure")
                                break
                except Exception as e:
                    logger.debug(f"Final table search failed: {e}")
            
            if not key_spec_section:
                logger.warning(f"Could not locate Key Specification section for {url}")
                # Debug: Try to see what sections are available
                try:
                    main_sections = browser.find_elements(By.XPATH, '//main//section')
                    logger.debug(f"Found {len(main_sections)} sections in main")
                    for i, sec in enumerate(main_sections[:3]):  # Check first 3 sections
                        logger.debug(f"Section {i+1} text preview: {sec.text[:100]}...")
                except:
                    pass
                return None
            
            # Step 5: Extract key-value pairs from the Key Specification table
            # The structure is typically: <tr><td>Spec Name</td><td>Spec Value</td></tr>
            # Each row contains one specification (e.g., "Engine: 998 cc")
            try:
                # Method 1: Extract from table rows (most common structure)
                rows = key_spec_section.find_elements(By.XPATH, './/tr')
                
                if rows:
                    logger.debug(f"Found {len(rows)} rows in Key Specification table")
                    for row in rows:
                        try:
                            cells = row.find_elements(By.XPATH, './/td | .//th')
                            if len(cells) >= 2:
                                # First cell is the label, second is the value
                                label = cells[0].text.strip()
                                value = cells[1].text.strip()
                                
                                # Skip empty values, headers, and rows where label == value
                                if label and value and label.lower() != value.lower():
                                    # Clean up the label (remove colons, extra spaces, newlines)
                                    label = re.sub(r':\s*$', '', label).strip()
                                    label = re.sub(r'\s+', ' ', label)  # Normalize whitespace
                                    # Clean up the value
                                    value = re.sub(r'\s+', ' ', value).strip()
                                    
                                    # Skip if it looks like a header row
                                    if label and value and len(value) < 200:  # Reasonable value length
                                        specs[label] = value
                        except Exception as e:
                            logger.debug(f"Error processing row: {e}")
                            continue
                
                # If we didn't find specs in table rows, try alternative structures
                if not specs:
                    logger.debug("No specs from table rows, trying alternative structures")
                    
                    # Try to find definition lists (dl/dt/dd structure)
                    try:
                        dt_elements = key_spec_section.find_elements(By.XPATH, './/dt | .//div[contains(@class, "label")] | .//span[contains(@class, "label")]')
                        dd_elements = key_spec_section.find_elements(By.XPATH, './/dd | .//div[contains(@class, "value")] | .//span[contains(@class, "value")]')
                        
                        if dt_elements and dd_elements:
                            for i, dt in enumerate(dt_elements):
                                if i < len(dd_elements):
                                    label = dt.text.strip()
                                    value = dd_elements[i].text.strip()
                                    if label and value:
                                        label = re.sub(r':\s*$', '', label).strip()
                                        specs[label] = value
                    except Exception as e:
                        logger.debug(f"Error processing definition list: {e}")
                    
                    # Try div-based structure (div with label and value classes)
                    if not specs:
                        try:
                            spec_items = key_spec_section.find_elements(By.XPATH, './/div[contains(@class, "row")] | .//div[contains(@class, "item")] | .//div[contains(@class, "spec")]')
                            for item in spec_items:
                                labels = item.find_elements(By.XPATH, './/div[contains(@class, "label")] | .//span[contains(@class, "label")] | .//strong | .//b')
                                values = item.find_elements(By.XPATH, './/div[contains(@class, "value")] | .//span[contains(@class, "value")]')
                                
                                if labels and values:
                                    label = labels[0].text.strip()
                                    value = values[0].text.strip()
                                    if label and value:
                                        label = re.sub(r':\s*$', '', label).strip()
                                        specs[label] = value
                        except Exception as e:
                            logger.debug(f"Error processing div structure: {e}")
                    
                    # Alternative: Extract from text content using regex patterns
                    if not specs:
                        section_text = key_spec_section.text
                        # Pattern to match "Label: Value" or "Label | Value"
                        pattern = r'([A-Za-z\s&()]+(?:NCAP|Airbag|Airbags|Capacity|Time|Space|Mileage|Power|Torque|Engine|Transmission|Fuel|Rating|Range|Battery|Charging|Boot)?)\s*[:\|]\s*([^\n]+)'
                        matches = re.findall(pattern, section_text, re.IGNORECASE)
                        
                        for match in matches:
                            label = match[0].strip()
                            value = match[1].strip()
                            # Clean up value (remove extra text after first meaningful value)
                            value = re.split(r'\s{2,}|\n', value)[0].strip()
                            if label and value and len(value) < 100:  # Avoid capturing too much text
                                label = re.sub(r':\s*$', '', label).strip()
                                specs[label] = value
                
            except Exception as e:
                logger.debug(f"Error extracting from Key Specification section: {e}")
                # Fallback: try to extract from page source
                try:
                    page_source = browser.page_source
                    # Look for table cells in the Key Specification area
                    # Pattern: <td>Label</td><td>Value</td> or similar
                    pattern = r'<t[dh][^>]*>([^<]+)</t[dh]>\s*<t[dh][^>]*>([^<]+)</t[dh]>'
                    matches = re.findall(pattern, page_source, re.IGNORECASE)
                    
                    for match in matches:
                        label = re.sub(r'<[^>]+>', '', match[0]).strip()
                        value = re.sub(r'<[^>]+>', '', match[1]).strip()
                        if label and value and len(value) < 100:
                            label = re.sub(r':\s*$', '', label).strip()
                            specs[label] = value
                except Exception as e2:
                    logger.debug(f"Error in fallback extraction: {e2}")
            
            logger.debug(f"Extracted {len(specs)} key specs for {url}: {list(specs.keys())}")
            
            # Step 6: Extract detailed table sections (Engine & Transmission, Fuel & Performance, etc.)
            # These are the detailed specification tables further down the page
            # Each section contains multiple key-value pairs in table format
            logger.debug(f"Extracting detailed table sections from {url}")
            table_sections = extract_table_sections(browser, url, logger)
            logger.debug(f"Extracted {len(table_sections)} table sections: {list(table_sections.keys())}")
            
            # Return both key specifications (quick overview) and table sections (detailed specs)
            return {
                'key_specifications': specs if specs else {},  # Overview specs from top of page
                'table_sections': table_sections  # Detailed specs from tables further down
            }
            
        except Exception as e:
            logger.warning(f"Could not extract specifications from {url}: {e}")
            return None
            
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None


def process_variants(
    brand_model_map: Dict,
    logger: logging.Logger,
    output_file: str = "variant_features.json",
    limit_models: int = None,
    output_dir: Optional[str] = None,
    upload_to_azure: bool = True,
) -> Dict:
    """
    Process all variants, build URLs, and scrape key specifications and table sections.
    
    This is the main orchestration function that:
    1. Iterates through all brands, models, and variants
    2. Builds CarDekho URLs for each variant
    3. Scrapes key specifications and detailed table sections
    4. Saves results to JSON (combined file + per-brand files {brand_name}_{date}.json)
    5. Optionally uploads per-brand files to Azure Blob Storage (when config.yaml is present)
    
    Args:
        brand_model_map: Dictionary with structure {brand: {model: [variants]}}
        logger: Logger instance for logging progress and errors
        output_file: Output JSON file path for combined results (legacy)
        limit_models: Limit processing to first N models (for testing). If None, processes all.
        output_dir: Directory for per-brand files. Defaults to same directory as this script.
        upload_to_azure: If True, upload per-brand files to Azure Blob when config is present.
    
    Returns:
        Dictionary with structure {brand: {model: {variant: {url, cleaned_variant_name, 
                                                           key_specifications, table_sections}}}}
    """
    # Directory where per-brand files are written (default: script directory)
    out_dir = output_dir or _script_dir()
    os.makedirs(out_dir, exist_ok=True)
    
    # Load Azure/config once (for per-brand uploads)
    config = load_config(logger) if upload_to_azure else None
    
    # Initialize browser options (disable notifications, etc.)
    browser_options = create_browser_options()
    browser = None
    
    results = {}
    total_variants = 0
    processed_variants = 0
    models_processed = 0
    
    # Step 1: Count total variants we'll process (respecting limit_models if set)
    # This is for progress reporting
    for brand, models in brand_model_map.items():
        for model, variants in models.items():
            if limit_models is None or models_processed < limit_models:
                total_variants += len(variants)
                models_processed += 1
            else:
                break
        if limit_models is not None and models_processed >= limit_models:
            break
    
    logger.info(f"Total variants to process: {total_variants} (limited to {limit_models} models)" if limit_models else f"Total variants to process: {total_variants}")
    
    try:
        # Step 2: Create browser instance (will be reused for all variants)
        browser = create_browser(browser_options)
        models_processed = 0  # Reset counter for actual processing
        
        # Step 3: Iterate through all brands, models, and variants
        for brand, models in brand_model_map.items():
            if brand not in results:
                results[brand] = {}
            
            for model, variants in models.items():
                # Check if we've reached the model limit (for testing)
                if limit_models is not None and models_processed >= limit_models:
                    logger.info(f"Reached model limit ({limit_models}), stopping processing")
                    break
                
                if model not in results[brand]:
                    results[brand][model] = {}
                
                logger.info(f"Processing {brand} - {model} ({len(variants)} variants)")
                models_processed += 1
                
                # Process each variant for this model
                for variant in variants:
                    processed_variants += 1
                    logger.info(f"Processing variant {processed_variants}/{total_variants}: {brand} - {model} - {variant}")
                    
                    # Step 4: Build CarDekho URL for this variant
                    # The URL format is: /overview/{Brand}_{Model}/{Brand}_{Model}_{Variant}.htm
                    url = build_url(brand, model, variant)
                    logger.debug(f"Built URL: {url}")
                    
                    # Step 5: Extract specifications and table sections from the page
                    # This function loads the page, validates it, and extracts all data
                    extracted_data = extract_key_specifications(browser, url, logger)
                    
                    # Step 6: Store results in the results dictionary
                    # Structure: results[brand][model][variant] = {url, cleaned_variant_name, key_specifications, table_sections}
                    if extracted_data:
                        results[brand][model][variant] = {
                            'url': url,  # The CarDekho URL for this variant
                            'cleaned_variant_name': clean_variant_name(variant),  # URL-friendly variant name
                            'key_specifications': extracted_data.get('key_specifications', {}),  # Overview specs
                            'table_sections': extracted_data.get('table_sections', {})  # Detailed table sections
                        }
                    else:
                        # If extraction failed (404, etc.), still store the entry with empty data
                        results[brand][model][variant] = {
                            'url': url,
                            'cleaned_variant_name': clean_variant_name(variant),
                            'key_specifications': {},
                            'table_sections': {}
                        }
                    
                    # Step 7: Save progress periodically (every 10 variants)
                    # This ensures we don't lose data if the script crashes
                    if processed_variants % 10 == 0:
                        with open(output_file, 'w', encoding='utf-8') as f:
                            json.dump(results, f, indent=2, ensure_ascii=False)
                        logger.info(f"Progress saved: {processed_variants}/{total_variants} variants processed")
                    
                    # Step 8: Be respectful with delays between requests
                    # Prevents overwhelming the server and getting blocked
                    time.sleep(2)
            
            # Step 9: Per-brand file and Azure upload (one file per brand: {brand_name}_{date}.json)
            if results.get(brand):
                date_str = datetime.now().strftime("%Y-%m-%d")
                safe_brand = sanitize_brand_for_filename(brand)
                brand_filename = f"{safe_brand}_{date_str}.json"
                brand_filepath = os.path.join(out_dir, brand_filename)
                try:
                    with open(brand_filepath, "w", encoding="utf-8") as f:
                        json.dump(results[brand], f, indent=2, ensure_ascii=False)
                    logger.info(f"Saved per-brand file: {brand_filepath}")
                    if config and upload_to_azure:
                        upload_to_azure_blob(config, brand_filepath, brand_filename, logger)
                except Exception as e:
                    logger.error(f"Failed to save/upload brand file for {brand}: {e}")
            
            # Break outer loop if we've reached the limit
            if limit_models is not None and models_processed >= limit_models:
                break
        
        # Final save (combined file for backward compatibility)
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        
        logger.info(f"Completed processing {processed_variants} variants")
        return results
        
    except Exception as e:
        logger.error(f"Error in process_variants: {e}")
        # Save partial results
        if results:
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, indent=2, ensure_ascii=False)
        return results
    finally:
        if browser:
            try:
                browser.quit()
            except Exception as e:
                logger.error(f"Error closing browser: {e}")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Scrape variant features from CarDekho")
    parser.add_argument(
        "--input", "-i",
        default="brand_model_map.json",
        help="Input JSON file with brand-model-variant map (default: brand_model_map.json)"
    )
    parser.add_argument(
        "--output", "-o",
        default="variant_features.json",
        help="Output JSON file for variant features (default: variant_features.json)"
    )
    parser.add_argument(
        "--log-level", "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set the logging level (default: INFO)"
    )
    parser.add_argument(
        "--limit-models", "-m",
        type=int,
        default=None,
        help="Limit processing to first N models (for testing). Default: process all."
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Do not upload per-brand files to Azure Blob (even if config.yaml is present)"
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for per-brand files {brand_name}_{date}.json (default: script directory)"
    )
    
    args = parser.parse_args()
    
    logger = setup_logging(args.log_level)
    
    try:
        # Load brand-model-variant map
        logger.info(f"Loading data from {args.input}")
        with open(args.input, 'r', encoding='utf-8') as f:
            brand_model_map = json.load(f)
        
        logger.info(f"Loaded {len(brand_model_map)} brands")
        
        # Optional: output_dir from config.yaml
        config = load_config(logger)
        output_dir = args.output_dir or (config.get("output_dir") if config else None)
        
        # Process variants (saves combined + per-brand files, uploads to Azure when config present)
        results = process_variants(
            brand_model_map,
            logger,
            output_file=args.output,
            limit_models=args.limit_models,
            output_dir=output_dir,
            upload_to_azure=not args.no_upload,
        )
        
        logger.info(f"Results saved to {args.output} and per-brand files to output directory")
        print(f"\nScraping completed! Results saved to {args.output}")
        
    except FileNotFoundError:
        logger.error(f"Input file {args.input} not found")
        print(f"Error: Input file {args.input} not found")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
