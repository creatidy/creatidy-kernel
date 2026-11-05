# SPDX-License-Identifier: Apache-2.0
"""Generic operator profile loading: bounded literal values, optional generic SOPS."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from test_task_execution import live_environment

from creatidy_kernel.adapters.operator_profile import (
    ProfileError,
    configuration_fields,
    default_profile_path,
    load_profile,
    migrate_sops_profile,
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


# Issue #62 synthetic ciphertext safety contract: Kernel never writes the user
# profile, failed decryption leaves ciphertext byte-identical, and diagnostics
# stay fixed messages. The fake sops executable stands in for the user's own
# encrypted profile; no real profile, key or ciphertext is used.
def _fake_sops(tools: Path, script: str) -> Path:
    tools.mkdir(exist_ok=True)
    executable = tools / "sops"
    executable.write_text(f"#!{sys.executable}\n{script}")
    executable.chmod(0o700)
    return executable


def test_failed_decryption_leaves_ciphertext_bytes_untouched(tmp_path: Path) -> None:
    ciphertext = write_profile(tmp_path / "kernel.env.sops.yaml", "ENC[synthetic-ciphertext]\n")
    before = ciphertext.read_bytes()
    stat_before = ciphertext.stat()
    _fake_sops(
        tmp_path / "tools",
        "import sys\nsys.stderr.write('key unavailable\\n')\nsys.exit(1)\n",
    )
    with pytest.raises(ProfileError, match="profile decryption failed") as error:
        load_profile(ciphertext, sops=True, environment={"PATH": str(tmp_path / "tools")})
    assert ciphertext.read_bytes() == before
    stat_after = ciphertext.stat()
    assert (stat_after.st_mtime_ns, stat_after.st_mode, stat_after.st_size) == (
        stat_before.st_mtime_ns,
        stat_before.st_mode,
        stat_before.st_size,
    )
    assert "key unavailable" not in str(error.value)
    assert "ENC" not in str(error.value)


@pytest.mark.parametrize("output", ["", "not json at all ", '{"KEY": 1}', "[" * 33 + "]" * 33])
def test_corrupt_decrypted_output_refuses_and_preserves_the_profile(tmp_path: Path, output: str) -> None:
    ciphertext = write_profile(tmp_path / "kernel.env.sops.yaml", "ENC[synthetic-ciphertext]\n")
    before = ciphertext.read_bytes()
    stat_before = ciphertext.stat()
    _fake_sops(tmp_path / "tools", f"import sys\nsys.stdout.write({output!r})\n")
    with pytest.raises(ProfileError):
        load_profile(ciphertext, sops=True, environment={"PATH": str(tmp_path / "tools")})
    assert ciphertext.read_bytes() == before
    assert ciphertext.stat().st_mtime_ns == stat_before.st_mtime_ns


def test_oversized_decrypted_output_hits_the_documented_bound(tmp_path: Path) -> None:
    ciphertext = write_profile(tmp_path / "kernel.env.sops.yaml", "ENC[synthetic-ciphertext]\n")
    before = ciphertext.read_bytes()
    stat_before = ciphertext.stat()
    _fake_sops(
        tmp_path / "tools",
        "import sys\nsys.stdout.write('x' * (4 * 1024 * 1024 + 1))\n",
    )
    with pytest.raises(ProfileError, match="profile decryption failed"):
        load_profile(ciphertext, sops=True, environment={"PATH": str(tmp_path / "tools")})
    assert ciphertext.read_bytes() == before
    assert ciphertext.stat().st_mtime_ns == stat_before.st_mtime_ns


def test_successful_loading_never_writes_the_profile(tmp_path: Path) -> None:
    plaintext = write_profile(tmp_path / "kernel.env", "CREATIDY_KERNEL_ROUTER_URL=http://127.0.0.1:8765\n")
    stat_before = plaintext.stat()
    before = plaintext.read_bytes()
    assert load_profile(plaintext) == {"CREATIDY_KERNEL_ROUTER_URL": "http://127.0.0.1:8765"}
    ciphertext = write_profile(tmp_path / "kernel.env.sops.yaml", "ENC[synthetic-ciphertext]\n")
    encrypted_before = ciphertext.read_bytes()
    _fake_sops(tmp_path / "tools", 'import sys\nsys.stdout.write(\'{"HOME": "/home/operator"}\')\n')
    assert load_profile(ciphertext, sops=True, environment={"PATH": str(tmp_path / "tools")}) == {
        "HOME": "/home/operator"
    }
    assert plaintext.read_bytes() == before
    assert ciphertext.read_bytes() == encrypted_before
    assert plaintext.stat().st_mtime_ns == stat_before.st_mtime_ns


def test_migration_rejects_plaintext_rotation_before_staging(tmp_path: Path) -> None:
    source = write_profile(tmp_path / "source.json", "synthetic ciphertext")
    _fake_sops(
        tmp_path / "tools",
        f"import json\nprint(json.dumps({{'CREATIDY_KERNEL_FORGE_TOKEN': {SIMULATED!r}}}))\n",
    )
    with pytest.raises(ProfileError, match="invalid ciphertext"):
        migrate_sops_profile(source, tmp_path / "destination.json", environment={"PATH": str(tmp_path / "tools")})
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.json", "tools"]


def test_real_sops_age_profile_is_read_only_and_missing_key_refuses(tmp_path: Path) -> None:
    sops = shutil.which("sops")
    keygen = shutil.which("age-keygen")
    if sops is None or keygen is None:
        pytest.skip("real SOPS and age-keygen are required for encrypted-profile evidence")
    environment = {"HOME": str(tmp_path), "PATH": os.defpath, "LANG": "C.UTF-8", "SOPS_DISABLE_VERSION_CHECK": "1"}
    generated = subprocess.run([keygen], env=environment, capture_output=True, check=True, text=True)  # noqa: S603
    private_key = generated.stdout
    recipient = subprocess.run(  # noqa: S603
        [keygen, "-y"], input=private_key, env=environment, capture_output=True, check=True, text=True
    ).stdout.strip()
    expected = {"CREATIDY_KERNEL_FORGE_TOKEN": SIMULATED, "CREATIDY_KERNEL_ROUTER_KEY": ""}
    encrypted = subprocess.run(  # noqa: S603
        [sops, "--encrypt", "--age", recipient, "--input-type", "json", "--output-type", "json", "/dev/stdin"],
        input=json.dumps(expected),
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert SIMULATED not in encrypted
    profile = write_profile(tmp_path / "profile.json", encrypted)
    before, mtime = profile.read_bytes(), profile.stat().st_mtime_ns
    tool_environment = {**environment, "PATH": str(Path(sops).parent), "SOPS_AGE_KEY": private_key}
    assert load_profile(profile, sops=True, environment=tool_environment) == expected
    with pytest.raises(ProfileError) as error:
        load_profile(profile, sops=True, environment={**tool_environment, "SOPS_AGE_KEY": ""})
    assert SIMULATED not in str(error.value)
    assert profile.read_bytes() == before
    assert profile.stat().st_mtime_ns == mtime
    prefix_fixture = "ENC[AES256_GCM,synthetic-plaintext-not-ciphertext"
    selective = subprocess.run(  # noqa: S603 - local SOPS with a synthetic age recipient.
        [
            sops,
            "--encrypt",
            "--age",
            recipient,
            "--unencrypted-regex",
            "^CREATIDY_KERNEL_FORGE_TOKEN$",
            "--input-type",
            "json",
            "--output-type",
            "json",
            "/dev/stdin",
        ],
        input=json.dumps({"CREATIDY_KERNEL_FORGE_TOKEN": prefix_fixture}),
        env=environment,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert prefix_fixture in selective
    selective_profile = write_profile(tmp_path / "selective.json", selective)
    selective_before = selective_profile.read_bytes()
    selective_rotation = subprocess.run(  # noqa: S603 - synthetic local fixture only.
        [sops, "--rotate", "--output-type", "json", str(selective_profile)],
        env=tool_environment,
        capture_output=True,
        check=True,
        text=True,
    )
    rotated_document = json.loads(selective_rotation.stdout)
    assert rotated_document["CREATIDY_KERNEL_FORGE_TOKEN"] == prefix_fixture
    assert rotated_document["sops"]["unencrypted_regex"] == "^CREATIDY_KERNEL_FORGE_TOKEN$"
    refused = tmp_path / "selective-output.json"
    with patch("creatidy_kernel.adapters.operator_profile.tempfile.mkstemp") as staging:
        with pytest.raises(ProfileError, match="invalid ciphertext") as error:
            migrate_sops_profile(selective_profile, refused, environment=tool_environment)
        staging.assert_not_called()
    assert prefix_fixture not in str(error.value)
    assert not refused.exists() and selective_profile.read_bytes() == selective_before
    destination = tmp_path / "migrated.json"
    migrate_sops_profile(profile, destination, environment=tool_environment)
    migrated = destination.read_bytes()
    assert migrated != before and SIMULATED.encode() not in migrated
    assert load_profile(destination, sops=True, environment=tool_environment) == expected
    assert load_profile(profile, sops=True, environment=tool_environment) == expected
    with pytest.raises(ProfileError):
        migrate_sops_profile(profile, destination, environment=tool_environment)
    assert destination.read_bytes() == migrated
    interrupted = tmp_path / "interrupted.json"
    with patch("creatidy_kernel.adapters.operator_profile.os.link", side_effect=OSError("synthetic disk failure")):
        with pytest.raises(ProfileError, match="profile migration failed"):
            migrate_sops_profile(profile, interrupted, environment=tool_environment)
    assert not interrupted.exists()
    assert not list(tmp_path.glob(".kernel-profile-*.json"))
    staging = tmp_path / "staging-failure.json"
    with patch("creatidy_kernel.adapters.operator_profile.tempfile.mkstemp", side_effect=OSError("disk full")):
        with pytest.raises(ProfileError, match="profile migration failed") as failure:
            migrate_sops_profile(profile, staging, environment=tool_environment)
    assert SIMULATED not in str(failure.value) and not staging.exists()

    sync = os.fsync
    for failed_call in (1, 2):
        calls = 0

        def fail_sync(fd: int, target_call: int = failed_call) -> None:
            nonlocal calls
            calls += 1
            if calls == target_call:
                raise OSError("synthetic sync failure")
            sync(fd)

        target = tmp_path / f"sync-failure-{failed_call}.json"
        with patch("creatidy_kernel.adapters.operator_profile.os.fsync", side_effect=fail_sync):
            with pytest.raises(ProfileError, match="destination may require inspection") as failure:
                migrate_sops_profile(profile, target, environment=tool_environment)
        assert SIMULATED not in str(failure.value)
        assert target.exists() == (failed_call == 2)
        if target.exists():
            assert SIMULATED.encode() not in target.read_bytes()
            assert load_profile(target, sops=True, environment=tool_environment) == expected
        assert not list(tmp_path.glob(".kernel-profile-*.json"))
        assert profile.read_bytes() == before and profile.stat().st_mtime_ns == mtime
    with pytest.raises(ProfileError):
        migrate_sops_profile(
            profile, tmp_path / "missing-key.json", environment={**tool_environment, "SOPS_AGE_KEY": ""}
        )
    assert not (tmp_path / "missing-key.json").exists()
    assert profile.read_bytes() == before and profile.stat().st_mtime_ns == mtime
    installed_cli = os.environ.get("CREATIDY_TEST_INSTALLED_CLI")
    if installed_cli:
        failed = subprocess.run(  # noqa: S603
            [
                installed_cli,
                "config",
                "migrate-sops",
                "--source",
                str(selective_profile),
                "--destination",
                str(refused),
            ],
            env=tool_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert failed.returncode != 0 and not refused.exists()
        assert prefix_fixture not in failed.stdout + failed.stderr
        installed_destination = tmp_path / "installed.json"
        result = subprocess.run(  # noqa: S603
            [
                installed_cli,
                "config",
                "migrate-sops",
                "--source",
                str(profile),
                "--destination",
                str(installed_destination),
            ],
            env=tool_environment,
            capture_output=True,
            text=True,
            check=True,
        )
        assert SIMULATED not in result.stdout + result.stderr
        assert load_profile(installed_destination, sops=True, environment=tool_environment) == expected
        validate = subprocess.run(  # noqa: S603
            [installed_cli, "config", "validate", "--profile", str(installed_destination), "--sops", "--json"],
            env=tool_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert validate.returncode == 1  # The synthetic profile deliberately lacks runtime configuration.
        assert SIMULATED not in validate.stdout + validate.stderr
        rollback = subprocess.run(  # noqa: S603
            [installed_cli, "config", "validate", "--profile", str(profile), "--sops", "--json"],
            env=tool_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert rollback.returncode == validate.returncode
        assert SIMULATED not in rollback.stdout + rollback.stderr
        assert profile.read_bytes() == before and profile.stat().st_mtime_ns == mtime
