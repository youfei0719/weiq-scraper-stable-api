import py_compile


def test_cli_compile():
    py_compile.compile("scraper.py", doraise=True)
    py_compile.compile("scraper_runtime.py", doraise=True)
    py_compile.compile("cloud_api.py", doraise=True)
