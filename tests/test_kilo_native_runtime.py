# SPDX-License-Identifier: Apache-2.0
"""Optional executable receipt: real native sessions, deterministic local model endpoint."""

import os
import time
from pathlib import Path

import pytest
from native_kilo_fixture import native_kilo
from test_kilo_review import ready
from test_product_review import request

from creatidy_kernel.adapters.kilo_review import NativeKiloReview
from creatidy_kernel.adapters.sqlite_store import SQLiteProgramStore
from creatidy_kernel.ports.verification import ProductReviewStatus, dispatch_product_review


def test_real_native_cold_review_session(tmp_path: Path) -> None:
    binary = os.environ.get("CREATIDY_KILO_NATIVE_TEST_BINARY")
    if not binary:
        pytest.skip("explicit pinned native test binary not supplied; no auto-download")
    with native_kilo(tmp_path / "native", Path(binary)) as (origin, calls):
        with SQLiteProgramStore(tmp_path / "kernel.db") as store:
            ready(store)
            dispatcher = NativeKiloReview(
                store,
                origin,
                tmp_path,
                "product-review",
                lambda _: "Exact test diff and approved criteria; fixture only.",
                lambda: int(time.time()),
                freshness_seconds=900,
                timeout_seconds=30,
            )
            result = dispatch_product_review(store, request(), dispatcher, now=int(time.time()))
            assert result.status is ProductReviewStatus.APPROVED
            assert result.receipt is not None and result.receipt.reviewer_session != "ses_fixture"
            assert result.receipt.reviewer_session.startswith("ses_")
            assert calls
