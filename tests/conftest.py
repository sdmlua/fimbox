"""Test-suite-wide logging setup.

Calls fimbox.logging_utils.configure_cli_logging() before any test runs so
that every module log call propagates to a single, consistently formatted
stream handler — same format used in production preprocess.log files.
Tests should `log = logging.getLogger(__name__)` and use `log.info(...)`
rather than print() so output is consistent with the package.
"""

from fimbox.logging_utils import configure_cli_logging

configure_cli_logging()
