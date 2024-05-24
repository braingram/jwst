def pytest_addoption(parser):
    parser.addoption("--report-crds-context", action="store_true",
                     help="Report CRDS context in test suite header")


def pytest_report_header(config):
    """Add CRDS_CONTEXT to pytest report header"""
    lines = []

    import glob
    fns = glob.glob("/etc/*-release")
    if fns:
        lines.append("==== system info ====")
        for fn in fns:
            with open(fn, "r") as f:
                lines.extend(f.readlines())
        lines.append("== end system info ==")

    import numpy as np
    lines.append(f"np.double precision    : {np.finfo(np.double).precision}")
    lines.append(f"np.longdouble precision: {np.finfo(np.longdouble).precision}")

    if config.getoption("report_crds_context"):
        from stpipe.crds_client import get_context_used
        lines.append(f"crds_context: {get_context_used('jwst')}")


    return lines
