"""Tests for the external extractor bridge.

Every case uses an injected runner; no device, Frida or extractor is started.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from prism_sync.hosttools import (
    AndroidHashResult,
    ExtractorInconclusive,
    ExtractorUnavailable,
    extract_android_version_hash,
)

HASH = "d157435ece8674102c7fbd4867ce593e26cc5d22"
COMMAND = ["uv", "run", "prism-version-hash"]


def payload(**overrides: Any) -> dict[str, Any]:
    """Return a fully valid extractor payload, with optional overrides."""
    base: dict[str, Any] = {
        "device": "Redmi K30 Pro Zoom Edition",
        "package": "com.bandainamcoent.shinycolorsprism",
        "game_version": "2.20.0",
        "process_id": 25004,
        "frida_version": "17.27.4",
        "version_hash": HASH,
        "confidence": "high",
        "candidates": [
            {
                "value": HASH,
                "occurrences": 1,
                "nul_terminated": True,
                "uppercase_in_memory": False,
                "addresses": ["0x7c55cea296"],
                "format_valid": True,
                "conflicts_with_known": False,
                "known_version": None,
                "context": f"productionP{HASH}",
            }
        ],
        "evidence": {"header_hit_count": 1, "il2cpp_base": "0x7c5fa8a000"},
        "warnings": [],
        "extracted_at": "2026-11-22T10:38:59+00:00",
        "duration_seconds": 3.923,
    }
    base.update(overrides)
    return base


class FakeRunner:
    """Records the call and returns a canned CompletedProcess."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append({"argv": list(argv), **kwargs})
        if self._raises is not None:
            raise self._raises
        return subprocess.CompletedProcess(
            args=argv,
            returncode=self._returncode,
            stdout=self._stdout,
            stderr=self._stderr,
        )


def runner_with(data: dict[str, Any], **kwargs: Any) -> FakeRunner:
    """Build a runner that prints ``data`` as JSON."""
    return FakeRunner(stdout=json.dumps(data).encode("utf-8"), **kwargs)


# --------------------------------------------------------------------------
# success paths
# --------------------------------------------------------------------------


class TestSuccess:
    def test_returns_verified_hash(self) -> None:
        runner = runner_with(payload())

        result = extract_android_version_hash(COMMAND, runner=runner)

        assert result == AndroidHashResult(
            version_hash=HASH,
            game_version="2.20.0",
            device="Redmi K30 Pro Zoom Edition",
        )

    def test_null_game_version_is_not_a_failure(self) -> None:
        runner = runner_with(payload(game_version=None))

        result = extract_android_version_hash(COMMAND, runner=runner)

        assert result.version_hash == HASH
        assert result.game_version is None

    def test_missing_game_version_is_not_a_failure(self) -> None:
        data = payload()
        del data["game_version"]

        result = extract_android_version_hash(COMMAND, runner=runner_with(data))

        assert result.version_hash == HASH
        assert result.game_version is None

    def test_missing_device_is_not_a_failure(self) -> None:
        data = payload()
        del data["device"]

        result = extract_android_version_hash(COMMAND, runner=runner_with(data))

        assert result.version_hash == HASH
        assert result.device is None

    def test_unknown_keys_are_ignored(self) -> None:
        data = payload()
        data["future_field"] = {"nested": [1, 2, 3]}

        result = extract_android_version_hash(COMMAND, runner=runner_with(data))

        assert result.version_hash == HASH


class TestInvocation:
    def test_appends_json_flag(self) -> None:
        runner = runner_with(payload())

        extract_android_version_hash(COMMAND, runner=runner)

        assert runner.calls[0]["argv"] == [*COMMAND, "--json"]

    def test_uses_safe_subprocess_arguments(self) -> None:
        runner = runner_with(payload())

        extract_android_version_hash(COMMAND, timeout=42.5, runner=runner)

        call = runner.calls[0]
        assert call["capture_output"] is True
        assert call["text"] is False
        assert call["timeout"] == 42.5
        assert call["check"] is False
        assert call["shell"] is False

    def test_default_timeout(self) -> None:
        runner = runner_with(payload())

        extract_android_version_hash(COMMAND, runner=runner)

        assert runner.calls[0]["timeout"] == 300.0

    def test_does_not_mutate_the_caller_command(self) -> None:
        original = ["uv", "run", "prism-version-hash"]

        extract_android_version_hash(original, runner=runner_with(payload()))

        assert original == ["uv", "run", "prism-version-hash"]

    def test_empty_command_is_rejected(self) -> None:
        with pytest.raises(ExtractorUnavailable, match="must not be empty"):
            extract_android_version_hash([], runner=FakeRunner())


# --------------------------------------------------------------------------
# process failures
# --------------------------------------------------------------------------


class TestProcessFailures:
    def test_command_not_found(self) -> None:
        runner = FakeRunner(raises=FileNotFoundError("no such file"))

        with pytest.raises(ExtractorUnavailable, match="command not found"):
            extract_android_version_hash(["nope"], runner=runner)

    def test_os_error_on_start(self) -> None:
        runner = FakeRunner(raises=PermissionError("denied"))

        with pytest.raises(ExtractorUnavailable, match="cannot start extractor"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_timeout(self) -> None:
        runner = FakeRunner(raises=subprocess.TimeoutExpired(COMMAND, 300.0))

        with pytest.raises(ExtractorUnavailable, match="timed out"):
            extract_android_version_hash(COMMAND, runner=runner)

    @pytest.mark.parametrize("code", [1, 2, 137])
    def test_non_zero_exit_keeps_stderr(self, code: int) -> None:
        runner = FakeRunner(
            stderr=b"need Gadget to attach on jailed Android",
            returncode=code,
        )

        with pytest.raises(ExtractorUnavailable) as info:
            extract_android_version_hash(COMMAND, runner=runner)

        message = str(info.value)
        assert str(code) in message
        assert "need Gadget" in message

    def test_non_zero_exit_without_stderr_uses_stdout(self) -> None:
        runner = FakeRunner(stdout=b"boom", returncode=2)

        with pytest.raises(ExtractorUnavailable, match="boom"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_non_zero_exit_with_no_output(self) -> None:
        runner = FakeRunner(returncode=2)

        with pytest.raises(ExtractorUnavailable, match="no diagnostic output"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_zero_exit_still_validates(self) -> None:
        """Exit code 0 alone must never be trusted."""
        runner = runner_with(payload(confidence="none"))

        with pytest.raises(ExtractorInconclusive, match="confidence"):
            extract_android_version_hash(COMMAND, runner=runner)


# --------------------------------------------------------------------------
# output shape failures
# --------------------------------------------------------------------------


class TestOutputShape:
    def test_empty_stdout(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="no output"):
            extract_android_version_hash(COMMAND, runner=FakeRunner(stdout=b"   "))

    def test_none_stdout(self) -> None:
        runner = FakeRunner()
        runner._stdout = None  # type: ignore[assignment]

        with pytest.raises(ExtractorInconclusive, match="no output"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_invalid_json(self) -> None:
        runner = FakeRunner(stdout=b"{not json")

        with pytest.raises(ExtractorInconclusive, match="not valid JSON"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_top_level_is_a_list(self) -> None:
        runner = FakeRunner(stdout=b"[1, 2, 3]")

        with pytest.raises(ExtractorInconclusive, match="not a JSON object"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_top_level_is_a_string(self) -> None:
        runner = FakeRunner(stdout=b'"hello"')

        with pytest.raises(ExtractorInconclusive, match="not a JSON object"):
            extract_android_version_hash(COMMAND, runner=runner)

    def test_empty_object(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="version_hash"):
            extract_android_version_hash(COMMAND, runner=runner_with({}))


# --------------------------------------------------------------------------
# safety criteria, one by one
# --------------------------------------------------------------------------


class TestSafetyCriteria:
    def test_hash_missing(self) -> None:
        data = payload()
        del data["version_hash"]

        with pytest.raises(ExtractorInconclusive, match="missing or not a string"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    def test_hash_is_not_a_string(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="not a string"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(version_hash=12345))
            )

    @pytest.mark.parametrize(
        "bad_hash",
        [
            "d157435ece8674102c7fbd4867ce593e26cc5d2",  # 39 chars
            "d157435ece8674102c7fbd4867ce593e26cc5d22a",  # 41 chars
            "D157435ECE8674102C7FBD4867CE593E26CC5D22",  # upper case
            "g157435ece8674102c7fbd4867ce593e26cc5d22",  # non-hex
            "",
        ],
    )
    def test_hash_format(self, bad_hash: str) -> None:
        data = payload(version_hash=bad_hash)
        data["candidates"][0]["value"] = bad_hash

        with pytest.raises(ExtractorInconclusive, match="40 lower-case hex"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    @pytest.mark.parametrize("confidence", ["medium", "low", "none", None, "HIGH", 1])
    def test_confidence_must_be_high(self, confidence: Any) -> None:
        with pytest.raises(ExtractorInconclusive, match="confidence"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(confidence=confidence))
            )

    def test_candidates_missing(self) -> None:
        data = payload()
        del data["candidates"]

        with pytest.raises(ExtractorInconclusive, match="not a list"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    def test_candidates_not_a_list(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="not a list"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(candidates={"value": HASH}))
            )

    def test_candidates_empty(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="exactly 1 candidate, got 0"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(candidates=[]))
            )

    def test_candidates_two(self) -> None:
        first = payload()["candidates"][0]
        second = dict(first, value="0123456789abcdef0123456789abcdef01234567")

        with pytest.raises(ExtractorInconclusive, match="exactly 1 candidate, got 2"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(candidates=[first, second]))
            )

    def test_candidate_is_not_an_object(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="candidates\\[0\\]"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(candidates=[HASH]))
            )

    def test_candidate_value_mismatch(self) -> None:
        data = payload()
        data["candidates"][0]["value"] = "0123456789abcdef0123456789abcdef01234567"

        with pytest.raises(ExtractorInconclusive, match="does not match"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    @pytest.mark.parametrize("flag", [False, None, "true", 1])
    def test_format_valid_must_be_true(self, flag: Any) -> None:
        data = payload()
        data["candidates"][0]["format_valid"] = flag

        with pytest.raises(ExtractorInconclusive, match="format_valid"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    @pytest.mark.parametrize("flag", [False, None, "true", 1])
    def test_nul_terminated_must_be_true(self, flag: Any) -> None:
        data = payload()
        data["candidates"][0]["nul_terminated"] = flag

        with pytest.raises(ExtractorInconclusive, match="nul_terminated"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    def test_warnings_missing(self) -> None:
        data = payload()
        del data["warnings"]

        with pytest.raises(ExtractorInconclusive, match="not a list"):
            extract_android_version_hash(COMMAND, runner=runner_with(data))

    def test_warnings_not_a_list(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="not a list"):
            extract_android_version_hash(
                COMMAND, runner=runner_with(payload(warnings="none"))
            )

    def test_warnings_non_empty(self) -> None:
        with pytest.raises(ExtractorInconclusive, match="reported warnings"):
            extract_android_version_hash(
                COMMAND,
                runner=runner_with(payload(warnings=["device is not rooted"])),
            )


# --------------------------------------------------------------------------
# encoding
# --------------------------------------------------------------------------


class TestEncoding:
    def test_invalid_utf8_in_stderr_does_not_mask_the_failure(self) -> None:
        runner = FakeRunner(stderr=b"\xff\xfe broken \x83", returncode=2)

        with pytest.raises(ExtractorUnavailable) as info:
            extract_android_version_hash(COMMAND, runner=runner)

        assert "exited with status 2" in str(info.value)

    def test_utf8_bom_is_tolerated(self) -> None:
        data = json.dumps(payload()).encode("utf-8")
        runner = FakeRunner(stdout=b"\xef\xbb\xbf" + data)

        result = extract_android_version_hash(COMMAND, runner=runner)

        assert result.version_hash == HASH

    def test_non_ascii_in_payload(self) -> None:
        data = payload(device="小米 K30 Pro 变焦版")

        result = extract_android_version_hash(COMMAND, runner=runner_with(data))

        assert result.device == "小米 K30 Pro 变焦版"
