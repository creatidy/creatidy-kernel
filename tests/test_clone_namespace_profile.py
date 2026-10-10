# SPDX-License-Identifier: Apache-2.0
"""OCI predicate shape/arithmetic only; no native filter-enforcement receipt."""

import json
from pathlib import Path
from typing import cast

import pytest

from tools.codex_oci_proof.boundary import write_seccomp_profile
from tools.oci_worker_poc.ociimage import SECCOMP_DENY_NAMES, seccomp_profile_json

EXPECTED_BITS = (0x10000000, 0x00020000, 0x40000000, 0x20000000, 0x08000000, 0x04000000, 0x02000000)


def test_exact_namespace_rules_preserve_the_existing_profile() -> None:
    profile = cast(dict[str, object], json.loads(seccomp_profile_json()))
    assert profile == {
        "defaultAction": "SCMP_ACT_ALLOW",
        "architectures": ["SCMP_ARCH_X86_64", "SCMP_ARCH_X86", "SCMP_ARCH_X32"],
        "syscalls": [
            {"names": list(SECCOMP_DENY_NAMES), "action": "SCMP_ACT_ERRNO", "errnoRet": 1},
            *[
                {
                    "names": ["clone"],
                    "action": "SCMP_ACT_ERRNO",
                    "errnoRet": 1,
                    "args": [{"index": 0, "value": bit, "valueTwo": bit, "op": "SCMP_CMP_MASKED_EQ"}],
                }
                for bit in EXPECTED_BITS
            ],
        ],
    }


def test_codex_clone3_compatibility_does_not_drop_namespace_predicates(tmp_path: Path) -> None:
    expected = cast(dict[str, object], json.loads(seccomp_profile_json()))
    groups = cast(list[dict[str, object]], expected["syscalls"])
    names = cast(list[str], groups[0]["names"])
    names.remove("clone3")
    groups.append({"names": ["clone3"], "action": "SCMP_ACT_ERRNO", "errnoRet": 38})
    actual = json.loads(write_seccomp_profile(tmp_path / "seccomp.json").read_text())
    assert actual == expected


@pytest.mark.parametrize("extra", (0, 17, 0x013D0F00, 0x80000000, 1 << 32))
def test_every_namespace_subset_is_denied_without_denying_unrelated_flags(extra: int) -> None:
    profile = cast(dict[str, object], json.loads(seccomp_profile_json()))
    groups = cast(list[dict[str, object]], profile["syscalls"])
    rules = [group for group in groups if group["names"] == ["clone"]]
    for subset in range(128):
        namespace = sum(bit for index, bit in enumerate(EXPECTED_BITS) if subset & (1 << index))
        flags = namespace | extra
        args = [cast(list[dict[str, object]], group["args"])[0] for group in rules]
        matched = any((flags & cast(int, arg["value"])) == arg["valueTwo"] for arg in args)
        assert matched == bool(subset)
