def pytest_addoption(parser):
    parser.addoption("--report-crds-context", action="store_true",
                     help="Report CRDS context in test suite header")


def pytest_report_header(config):
    """Add CRDS_CONTEXT to pytest report header"""
    lines = []

    if config.getoption("report_crds_context"):
        from stpipe.crds_client import get_context_used
        lines.append(f"crds_context: {get_context_used('jwst')}")
    import stcal
    lines.append(f"stcal: {stcal.__version__}")
    import stdatamodels
    lines.append(f"stdatamodels: {stdatamodels.__version__}")
    return lines
