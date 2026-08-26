import scraper
import scraper_runtime


def test_legacy_scraper_entrypoint_delegates_to_unified_runtime() -> None:
    """Prevent legacy CLI usage from reviving the retired crawler."""
    assert scraper.main is scraper_runtime.main
    assert scraper.StateStore is scraper_runtime.StateStore
