"""
Test script to verify variant feature extraction on 10 random variants.
"""

import json
import random
import sys
from get_variant_features import (
    setup_logging,
    create_browser_options,
    create_browser,
    build_url,
    clean_variant_name,
    extract_key_specifications
)


def get_random_variants(brand_model_map, count=10):
    """Get random variants from the brand-model map."""
    all_variants = []
    
    for brand, models in brand_model_map.items():
        for model, variants in models.items():
            for variant in variants:
                all_variants.append({
                    'brand': brand,
                    'model': model,
                    'variant': variant
                })
    
    # Randomly select variants
    if len(all_variants) < count:
        return all_variants
    
    return random.sample(all_variants, count)


def test_variants(brand_model_map, test_count=10):
    """Test extraction on random variants."""
    logger = setup_logging("INFO")
    
    # Get random variants
    test_variants = get_random_variants(brand_model_map, test_count)
    
    logger.info(f"Testing {len(test_variants)} random variants:")
    for i, tv in enumerate(test_variants, 1):
        logger.info(f"  {i}. {tv['brand']} - {tv['model']} - {tv['variant']}")
    
    browser_options = create_browser_options()
    browser = None
    results = {}
    
    try:
        browser = create_browser(browser_options)
        
        for i, tv in enumerate(test_variants, 1):
            brand = tv['brand']
            model = tv['model']
            variant = tv['variant']
            
            logger.info(f"\n{'='*80}")
            logger.info(f"Test {i}/{len(test_variants)}: {brand} - {model} - {variant}")
            logger.info(f"{'='*80}")
            
            # Build URL
            url = build_url(brand, model, variant)
            logger.info(f"URL: {url}")
            logger.info(f"Cleaned variant name: {clean_variant_name(variant)}")
            
            # Extract specifications
            specs = extract_key_specifications(browser, url, logger)
            
            # Store results
            key = f"{brand}::{model}::{variant}"
            results[key] = {
                'url': url,
                'cleaned_variant_name': clean_variant_name(variant),
                'specifications': specs if specs else {},
                'success': specs is not None
            }
            
            # Display results
            if specs:
                logger.info(f"✓ Successfully extracted {len(specs)} specifications:")
                for spec_name, spec_value in specs.items():
                    logger.info(f"  - {spec_name}: {spec_value}")
            else:
                logger.warning("✗ Failed to extract specifications")
            
            # Small delay between requests
            import time
            time.sleep(2)
        
        # Summary
        logger.info(f"\n{'='*80}")
        logger.info("TEST SUMMARY")
        logger.info(f"{'='*80}")
        successful = sum(1 for r in results.values() if r['success'])
        logger.info(f"Total tested: {len(results)}")
        logger.info(f"Successful: {successful}")
        logger.info(f"Failed: {len(results) - successful}")
        logger.info(f"Success rate: {successful/len(results)*100:.1f}%")
        
        # Save results
        output_file = "test_variant_features_results.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        logger.info(f"\nResults saved to: {output_file}")
        
        return results
        
    except Exception as e:
        logger.error(f"Error during testing: {e}")
        import traceback
        traceback.print_exc()
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
    
    parser = argparse.ArgumentParser(description="Test variant feature extraction on random variants")
    parser.add_argument(
        "--input", "-i",
        default="brand_model_map.json",
        help="Input JSON file with brand-model-variant map (default: brand_model_map.json)"
    )
    parser.add_argument(
        "--count", "-c",
        type=int,
        default=10,
        help="Number of random variants to test (default: 10)"
    )
    parser.add_argument(
        "--seed", "-s",
        type=int,
        default=None,
        help="Random seed for reproducibility (optional)"
    )
    
    args = parser.parse_args()
    
    if args.seed:
        random.seed(args.seed)
    
    try:
        # Load brand-model-variant map
        print(f"Loading data from {args.input}...")
        with open(args.input, 'r', encoding='utf-8') as f:
            brand_model_map = json.load(f)
        
        print(f"Loaded {len(brand_model_map)} brands")
        
        # Run tests
        results = test_variants(brand_model_map, args.count)
        
        print(f"\n✓ Testing completed! Check the log file and results JSON for details.")
        
    except FileNotFoundError:
        print(f"Error: Input file {args.input} not found")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
