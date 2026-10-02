def pytest_configure(config):
    config.addinivalue_line("markers", "realtime: uses real time and real processes (no fake clock)")
