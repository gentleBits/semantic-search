from tests.testroot import ensure


def pytest_configure(config):
    why = ensure(log=lambda m: print(m, flush=True))
    if why:
        print(f"tests · no test index ({why}): the tests that need one are skipped", flush=True)
