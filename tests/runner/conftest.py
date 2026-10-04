def pytest_configure(config):
    config.addinivalue_line("markers", "realtime: uses real time and real processes (no fake clock)")
    config.addinivalue_line("markers", "onlab: needs a real host named by FLIGHTCTL_CONFORMANCE_TARGET")
