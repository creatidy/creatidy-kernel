# SPDX-License-Identifier: Apache-2.0
"""Generic operator profile loading: bounded literal values, optional generic SOPS."""

import json
import os
import sys
from pathlib import Path

import pytest
from test_task_execution import live_environment

from creatidy_kernel.adapters.operator_profile import (
    ProfileError,
    configuration_fields,
    default_profile_path,
    load_profile,
    parse_dotenv,
    parse_json_profile,
    profile_template,
    unknown_fields,
)

SIMULATED = "synthetic-profile-value"  # a placeholder payload for the fake SOPS output.


def write_profile(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_dotenv_parsing_is_bounded_literal_and_unique() -> None:
    values = parse_dotenv(
        "# comment\n"
        "CREATIDY_KERNEL_ROUTER_URL=http://127.0.0.1:8765\n"
        "\n"
        "CREATIDY_KERNEL_RUNTIME_BINDING=zai/glm-5.3/low\n"
        "HOME=/home/operator\n"
    )
    assert values == {
        "CREATIDY_KERNEL_ROUTER_URL": "http://127.0.0.1:8765",
        "CREATIDY_KERNEL_RUNTIME_BINDING": "zai/glm-5.3/low",
        "HOME": "/home/operator",
    }
    assert (
        parse_dotenv("CREATIDY_KERNEL_ROUTER_KEY=a=b$c `safe`")[  # noqa: S608 - literal value, never evaluated.
            "CREATIDY_KERNEL_ROUTER_KEY"
        ]
        == "a=b$c `safe`"
    )


@pytest.mark.parametrize(
    "text",
    [
        "CREATIDY_KERNEL_ROUTER_URL=http://a\nCREATIDY_KERNEL_ROUTER_URL=http://b\n",
        "lowercase-key=1\n",
        "NO_SEPARATOR\n",
        "=empty-key\n",
    ],
)
def test_invalid_dotenv_shapes_are_refused(text: str) -> None:
    with pytest.raises(ProfileError, match="KEY=VALUE"):
        parse_dotenv(text)


def test_profile_size_is_bounded() -> None:
    with pytest.raises(ProfileError, match="size limit"):
        parse_dotenv("KEY=" + "x" * (256 * 1024 + 1))


def test_json_profile_requires_a_string_object(tmp_path: Path) -> None:
    assert parse_json_profile(json.dumps({"HOME": "/h"})) == {"HOME": "/h"}
    for text in ("[]", json.dumps({"HOME": 3}), json.dumps({"bad-key": "x"}), "not json"):
        with pytest.raises(ProfileError):
            parse_json_profile(text)


def test_load_profile_reads_plaintext_and_refuses_missing_or_indirect_files(tmp_path: Path) -> None:
    profile = write_profile(tmp_path / "kernel.env", "HOME=/home/operator\n")
    assert load_profile(profile) == {"HOME": "/home/operator"}
    for missing in (tmp_path / "absent.env",):
        with pytest.raises(ProfileError, match="plain file"):
            load_profile(missing)
    linked = tmp_path / "linked.env"
    linked.symlink_to(profile)
    with pytest.raises(ProfileError, match="plain file"):
        load_profile(linked)
    relative = Path("relative.env")
    with pytest.raises(ProfileError, match="plain file"):
        load_profile(relative)


def test_sops_path_uses_user_supplied_binary_key_and_profile(tmp_path: Path) -> None:
    profile = write_profile(tmp_path / "kernel.env.sops.yaml", "encrypted\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_sops = tools / "sops"
    fake_sops.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

assert sys.argv[1:] == ["--decrypt", "--output-type", "json", {str(profile)!r}]
assert os.environ["SOPS_AGE_KEY_FILE"] == "/user/supplied/key.txt", "the user's SOPS environment is passed through"
assert "AMBIENT" not in json.dumps(dict(os.environ))
print(json.dumps({{"CREATIDY_KERNEL_FORGE_TOKEN": {SIMULATED!r}}}))
"""
    )
    fake_sops.chmod(0o700)
    values = load_profile(
        profile,
        sops=True,
        environment={"PATH": str(tools), "SOPS_AGE_KEY_FILE": "/user/supplied/key.txt", "AMBIENT": "value"},
    )
    assert values == {"CREATIDY_KERNEL_FORGE_TOKEN": SIMULATED}


def test_sops_failures_are_safe_and_closed(tmp_path: Path) -> None:
    profile = write_profile(tmp_path / "kernel.env.sops.yaml", "encrypted\n")
    with pytest.raises(ProfileError, match="sops executable unavailable"):
        load_profile(profile, sops=True, environment={"PATH": str(tmp_path / "no-tools")})

    tools = tmp_path / "tools"
    tools.mkdir()
    failing = tools / "sops"
    failing.write_text(f"#!{sys.executable}\nimport sys\nsys.stderr.write({SIMULATED!r})\nsys.exit(1)\n")
    failing.chmod(0o700)
    with pytest.raises(ProfileError, match="profile decryption failed") as error:
        load_profile(profile, sops=True, environment={"PATH": str(tools)})
    assert SIMULATED not in str(error.value)


def test_unknown_and_configuration_fields_are_reported_by_name_only() -> None:
    environment = live_environment("zai/glm-5.3/low")
    environment["KERNEL_DEPLOYMENT_HINT"] = "owner-specific"
    assert unknown_fields(environment) == ("KERNEL_DEPLOYMENT_HINT",)
    assert unknown_fields(live_environment("zai/glm-5.3/low")) == ()
    assert configuration_fields(live_environment("zai/glm-5.3/low")) == ()
    missing = configuration_fields({"CREATIDY_KERNEL_ROUTER_URL": "http://127.0.0.1:8765"})
    assert "CREATIDY_KERNEL_FORGE_TOKEN" in missing
    invalid = configuration_fields({**environment, "CREATIDY_KERNEL_ROUTER_URL": "https://user:pass@x.invalid"})
    assert "CREATIDY_KERNEL_ROUTER_URL" in invalid
    assert SIMULATED not in str(missing) + str(invalid)


def test_default_profile_location_and_template_are_user_local_and_generic() -> None:
    assert default_profile_path({"XDG_CONFIG_HOME": "/cfg"}) == Path("/cfg/creatidy-kernel/kernel.env")
    assert default_profile_path({}) == Path.home() / ".config" / "creatidy-kernel" / "kernel.env"
    template = profile_template()
    assert "CREATIDY_KERNEL_ROUTER_URL=REPLACE_ROUTER_ORIGIN" in template
    assert "forgejo.creatidy.com" not in template
    assert "creatidy-onprem" not in template
    assert SIMULATED not in template
    parsed = parse_dotenv(template)
    assert parsed["CREATIDY_KERNEL_ROUTER_URL"].startswith("REPLACE_")
    assert os.environ.get("PATH", "") in parsed["PATH"] or parsed["PATH"] == os.defpath
