import json
import time
import logging
import argparse
from typing import Dict, List, Optional

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
            logging.FileHandler('cardekho_scraper.log'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


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


def extract_brands_and_models(logger: logging.Logger) -> Dict[str, Dict[str, List[str]]]:
    """Extract brands and models from CarDekho compare page."""
    browser_options = create_browser_options()
    browser = None
    
    try:
        browser = create_browser(browser_options)
        browser.get("https://www.cardekho.com/compare-cars")
        time.sleep(2)
        
        element = browser.find_element(By.XPATH, '//input[@placeholder="Select Brand/Model"]')
        element.click()
        time.sleep(5)
        wait = WebDriverWait(browser, 10)

        # Extract all brands (parent <li> elements)
        brand_elements = wait.until(
            EC.presence_of_all_elements_located(
                (By.XPATH, '//li[contains(@class, "gs_ta_group")]')
            )
        )

        # Create a hashmap to store brands and models
        brand_model_map = {}

        # Extract brand names and models
        for brand in brand_elements:
            brand_name_data_dict = json.loads(brand.get_attribute("data-gs-ta-val"))
            elem_name = brand_name_data_dict.get("text")
            brand_slug = brand_name_data_dict.get("brandSlug")
            
            if not brand_slug:  # if brand_slug is none its a brand and not a model
                if not brand_model_map.get(elem_name):
                    brand_model_map[elem_name] = {}
            else:  # otherwise it's a model
                brand_model_map[brand_slug][elem_name] = []

        logger.info(f"Found {len(brand_model_map)} brands with models")
        return brand_model_map
        
    except Exception as e:
        logger.error(f"Error extracting brands and models: {e}")
        return {}
    finally:
        if browser:
            try:
                browser.quit()
            except Exception as e:
                logger.error(f"Error closing browser: {e}")


def fetch_variants_for_single_model(brand: str, model: str, logger: logging.Logger) -> List[str]:
    """Fetch variants for a single model using a fresh browser session."""
    browser_options = create_browser_options()
    browser = None
    
    try:
        browser = create_browser(browser_options)
        browser.get("https://www.cardekho.com/compare-cars")
        time.sleep(5)
        wait = WebDriverWait(browser, 10)
        
        # Click on the search input to open dropdown
        search_box = browser.find_element(
            By.XPATH, '//input[@placeholder="Select Brand/Model"]'
        )
        search_box.click()
        time.sleep(3)
        
        # Find and click the model element
        model_element = wait.until(
            EC.element_to_be_clickable(
                (
                    By.XPATH,
                    f'//li[contains(@class, "gs_ta_group") and @data-gs-ta-val[contains(., "{model}")]]',
                )
            )
        )
        model_element.click()
        time.sleep(3)

        # Extract variant names from the page
        variant_elements = wait.until(
            EC.presence_of_all_elements_located(
                (By.XPATH, '//li[starts-with(@class, "gs_ta_choice")]')
            )
        )

        # Store variant names in the list
        variants = [
            variant.text.strip()
            for variant in variant_elements
            if variant.text.strip()
        ]
        
        logger.debug(f"Variants found for {brand} - {model}: {variants}")
        logger.info(f"Successfully fetched {len(variants)} variants for {brand} - {model}")
        return variants
        
    except Exception as e:
        logger.error(f"Could not fetch variants for {brand} - {model}: {e}")
        return []
    finally:
        if browser:
            try:
                browser.quit()
            except Exception as e:
                logger.error(f"Error closing browser for {brand} - {model}: {e}")


def fetch_variants_for_models(brand_model_map: Dict[str, Dict[str, List[str]]], logger: logging.Logger) -> Dict[str, Dict[str, List[str]]]:
    """Fetch variants for all models using fresh browser sessions for each model."""
    total_models = sum(len(models) for models in brand_model_map.values())
    current_model = 0
    
    for brand, models in brand_model_map.items():
        for model in models:
            current_model += 1
            logger.info(f"Fetching variants for {brand} - {model} ({current_model}/{total_models})")
            
            # Use fresh browser session for each model
            variants = fetch_variants_for_single_model(brand, model, logger)
            brand_model_map[brand][model] = variants
            
            # Add a small delay between requests to be respectful
            time.sleep(2)
    
    return brand_model_map


def save_to_json(data: Dict, output_file: str, logger: logging.Logger) -> bool:
    """Save data to JSON file."""
    try:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Data saved successfully to {output_file}")
        return True
    except Exception as e:
        logger.error(f"Error saving data to JSON: {e}")
        return False


def scrape_cardekho_data(output_file: str = "brand_model_map.json", log_level: str = "INFO") -> Dict[str, Dict[str, List[str]]]:
    """Main function to scrape CarDekho data."""
    logger = setup_logging(log_level)
    
    logger.info("Starting CarDekho scraper...")
    
    # Extract brands and models
    logger.info("Step 1: Extracting brands and models...")
    brand_model_map = extract_brands_and_models(logger)
    
    if not brand_model_map:
        logger.error("Failed to extract brands and models. Exiting.")
        return {}
    
    # Fetch variants for each model
    logger.info("Step 2: Fetching variants for each model...")
    brand_model_map = fetch_variants_for_models(brand_model_map, logger)
    
    # Save to JSON
    logger.info("Step 3: Saving data to JSON...")
    save_to_json(brand_model_map, output_file, logger)
    
    logger.info("CarDekho scraping completed successfully!")
    return brand_model_map


def main():
    """Entry point for the script."""
    parser = argparse.ArgumentParser(description="Scrape CarDekho brand, model, and variant data")
    parser.add_argument(
        "--output", 
        "-o", 
        default="brand_model_map.json",
        help="Output JSON file path (default: brand_model_map.json)"
    )
    parser.add_argument(
        "--log-level",
        "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set the logging level (default: INFO)"
    )
    
    args = parser.parse_args()
    
    try:
        result = scrape_cardekho_data(args.output, args.log_level)
        if result:
            print(f"\nScraping completed successfully!")
            print(f"Data saved to: {args.output}")
            print(f"Total brands scraped: {len(result)}")
        else:
            print("\nScraping failed. Check the log file for details.")
            
    except KeyboardInterrupt:
        print("\nScraping interrupted by user.")
    except Exception as e:
        print(f"\nUnexpected error: {e}")


if __name__ == "__main__":
    main()