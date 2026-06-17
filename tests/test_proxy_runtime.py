import importlib


def _reload_runtime(monkeypatch, proxy_server=None, proxy_username=None, proxy_password=None, proxy_bypass=None):
    for key in ("WEIQ_PROXY_SERVER", "WEIQ_PROXY_USERNAME", "WEIQ_PROXY_PASSWORD", "WEIQ_PROXY_BYPASS"):
        monkeypatch.delenv(key, raising=False)
    if proxy_server is not None:
        monkeypatch.setenv("WEIQ_PROXY_SERVER", proxy_server)
    if proxy_username is not None:
        monkeypatch.setenv("WEIQ_PROXY_USERNAME", proxy_username)
    if proxy_password is not None:
        monkeypatch.setenv("WEIQ_PROXY_PASSWORD", proxy_password)
    if proxy_bypass is not None:
        monkeypatch.setenv("WEIQ_PROXY_BYPASS", proxy_bypass)
    import scraper_runtime

    return importlib.reload(scraper_runtime)


def test_no_proxy_keeps_playwright_launch_without_proxy(monkeypatch):
    runtime = _reload_runtime(monkeypatch)
    kwargs = runtime.get_playwright_launch_kwargs(headless=True)
    assert kwargs == {"headless": True}


def test_proxy_present_adds_playwright_launch_proxy(monkeypatch):
    runtime = _reload_runtime(
        monkeypatch,
        proxy_server="http://proxy.example.com:8080",
        proxy_username="user1",
        proxy_password="secret1",
        proxy_bypass=".weiq.com,localhost",
    )
    kwargs = runtime.get_playwright_launch_kwargs(headless=False)
    assert kwargs["headless"] is False
    assert kwargs["proxy"]["server"] == "http://proxy.example.com:8080"
    assert kwargs["proxy"]["username"] == "user1"
    assert kwargs["proxy"]["password"] == "secret1"
    assert kwargs["proxy"]["bypass"] == ".weiq.com,localhost"
