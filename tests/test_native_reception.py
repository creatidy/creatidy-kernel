# SPDX-License-Identifier: Apache-2.0
"""Explicit receipt rejects missing native setup even when pytest would skip."""

import pytest

from tools.native_reception import Receipt

pytest_plugins = ["pytester"]


@pytest.mark.parametrize(
    "body",
    [
        "import pytest\npytest.skip('incomplete pin', allow_module_level=True)",
        "import pytest\ndef test_native(): pytest.skip('mismatched native version')",
        "",
    ],
)
def test_explicit_native_receipt_refuses_unreceived_cells(pytester: pytest.Pytester, body: str) -> None:
    pytester.makepyfile(test_native=body)
    result = pytester.runpytest("-q", plugins=[Receipt()])
    assert result.ret != pytest.ExitCode.OK
    assert any("Native reception UNPROVED" in line for line in result.errlines)


def test_explicit_native_receipt_requires_actual_passed_cells(pytester: pytest.Pytester) -> None:
    pytester.makepyfile("def test_received(): assert True")
    result = pytester.runpytest("-q", plugins=[Receipt()])
    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1)
