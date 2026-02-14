"""
Test script with specific known variants to verify extraction works.
"""

import json
import sys
from get_variant_features import (
    setup_logging,
    create_browser_options,
    create_browser,
    build_url,
    clean_variant_name,
    extract_key_specifications
)


def test_specific_variants():
    """Test extraction on specific known variants."""
    logger = setup_logging("INFO")
    
    # Test with known working variants
    test_variants = [
        {
            'brand': 'Mahindra',
            'model': 'BE 6',
            'variant': 'Pack One (Electric) 18.90 Lakh*',
            'expected_url': 'https://www.cardekho.com/overview/Mahindra_BE_6/Mahindra_BE_6_Pack_One.htm'
        },
        {
            'brand': 'Mahindra',
            'model': 'BE 6',
            'variant': 'Pack One 7.2kw Charger (Electric) 19.40 Lakh*',
            'expected_url': 'https://www.cardekho.com/overview/Mahindra_BE_6/Mahindra_BE_6_Pack_One_7.2kw_Charger.htm'
        },
        {
            'brand': 'Maruti',
            'model': 'Alto K10',
            'variant': 'VXI (Petrol) 4.50 Lakh*',
            'expected_url': None  # We'll test if this works
        },
        {
            'brand': 'Tata',
            'model': 'Nexon',
            'variant': 'Fearless Plus S (Petrol) 8.15 Lakh*',
            'expected_url': None
        },
    ]
    
    browser_options = create_browser_options()
    browser = None
    results = {}
    
    try:
        browser = create_browser(browser_options)
        
        for i, tv in enumerate(test_variants, 1):
            brand = tv['brand']
            model = tv['model']
            variant = tv['variant']
            expected_url = tv.get('expected_url')
            
            logger.info(f"\n{'='*80}")
            logger.info(f"Test {i}/{len(test_variants)}: {brand} - {model} - {variant}")
            logger.info(f"{'='*80}")
            
            # Build URL
            url = build_url(brand, model, variant)
            logger.info(f"Generated URL: {url}")
            if expected_url:
                logger.info(f"Expected URL:  {expected_url}")
                if url != expected_url:
                    logger.warning(f"⚠ URL mismatch! Using generated URL.")
            logger.info(f"Cleaned variant name: {clean_variant_name(variant)}")
            
            # Extract specifications
            specs = extract_key_specifications(browser, url, logger)
            
            # Store results
            key = f"{brand}::{model}::{variant}"
            results[key] = {
                'url': url,
                'expected_url': expected_url,
                'cleaned_variant_name': clean_variant_name(variant),
                'specifications': specs if specs else {},
                'success': specs is not None,
                'spec_count': len(specs) if specs else 0
            }
            
            # Display results
            if specs:
                logger.info(f"✓ Successfully extracted {len(specs)} specifications:")
                for spec_name, spec_value in sorted(specs.items()):
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
        total_specs = sum(r['spec_count'] for r in results.values())
        logger.info(f"Total tested: {len(results)}")
        logger.info(f"Successful: {successful}")
        logger.info(f"Failed: {len(results) - successful}")
        logger.info(f"Success rate: {successful/len(results)*100:.1f}%")
        logger.info(f"Total specifications extracted: {total_specs}")
        
        # Save results
        output_file = "test_specific_variants_results.json"
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


if __name__ == "__main__":
    try:
        results = test_specific_variants()
        print(f"\n✓ Testing completed! Check the log file and results JSON for details.")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
