# SPDX-License-Identifier: Apache-2.0
"""Explicit proof runs must receive selected native cells, never successful skips."""

import sys

import pytest


class Receipt:
    skipped: int = 0
    received: int = 0

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            self.skipped += 1
        if report.when == "call" and report.passed:
            self.received += 1

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        if self.skipped or not self.received:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            print("Native reception UNPROVED: skipped or no received cells", file=sys.stderr)


def pytest_configure(config: pytest.Config) -> None:
    config.pluginmanager.register(Receipt(), "native-reception-receipt")
