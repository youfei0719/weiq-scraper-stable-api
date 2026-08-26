"""Legacy CLI entry point for the unified WEIQ scraper runtime.

Keep ``python scraper.py`` working for existing users while ensuring every
launch path uses the same browser, authentication, and extraction logic.
"""

from scraper_runtime import StateStore, main

__all__ = ["StateStore", "main"]


if __name__ == "__main__":
    main()
