"""Fetch the full ResourceVersion through the game's gRPC GetVersion call.

The version is returned in the gRPC **initial metadata** as
``x-resource-version``.  The response body uses a custom framing and is not
needed; it is decrypted only for diagnostics.

The gRPC call requires the current application version and version hash.  They
cannot be discovered from the resource version itself, so they live in
``version_profiles.json`` (or CLI overrides).  The resource version check is
fully automatic once the profile is current.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import grpc
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .constants import DEFAULT_GRPC_TARGET
from .manifest import decode_full_version
from .models import LocalVersion, VersionCheck, VersionProfile
from .selection import Selection

__all__ = [
    "GrpcVersionProvider",
    "LocalVersion",
    "VersionCheck",
    "VersionError",
    "VersionProfile",
    "check_version",
    "read_local_version",
    "resolve_remote_version",
    "write_local_version",
]

LOGGER = logging.getLogger(__name__)


def _write_json_atomic(
    path: str | Path, value: object, *, pretty: bool = False
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    target.write_text(text + "\n", encoding="utf-8")


METHOD_PATH = "/api.GameService/GetVersion"
IV_LENGTH = 16

# Recovered from CryptoKeyGenerator.Create128 -> Cryptographer..ctor.
# ASCII: "LJtLwtZ+gNgHu1a8"
AES_KEY = bytes.fromhex("4c4a744c77745a2b674e674875316138")

DEFAULT_PROFILE_NAME = "default"

VERSION_MARKER_NAME = ".prism-version.json"
MAX_PROFILE_BYTES = 1_000_000

VERSION_HASH_HINT = (
    "The game version/version hash may be stale. Update version_profiles.json "
    "or run: prism-auto profile set <game-version> --version-hash <hash>"
)


class VersionError(RuntimeError):
    """Raised when the remote or local version cannot be determined."""


def aes_ctr_crypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-128-CTR with a 16-byte big-endian counter."""
    if len(key) != 16:
        raise ValueError(f"unexpected AES key length: {len(key)}")
    if len(iv) != IV_LENGTH:
        raise ValueError(f"unexpected IV length: {len(iv)}")
    cipher = Cipher(algorithms.AES(key), modes.CTR(iv))
    operation = cipher.encryptor()
    return operation.update(data) + operation.finalize()


def encrypt_body(plaintext: bytes) -> bytes:
    """Confirmed layout: IV || ciphertext, no length prefix."""
    iv = os.urandom(IV_LENGTH)
    return iv + aes_ctr_crypt(AES_KEY, iv, plaintext)


def decrypt_body(blob: bytes) -> bytes:
    if len(blob) < IV_LENGTH:
        raise ValueError(f"encrypted body too short: {len(blob)}")
    iv, ciphertext = blob[:IV_LENGTH], blob[IV_LENGTH:]
    return aes_ctr_crypt(AES_KEY, iv, ciphertext)


def serialize_request(_request: bytes) -> bytes:
    """GetVersionArgs is empty; encrypt an empty plaintext."""
    return encrypt_body(b"")


def deserialize_response(blob: bytes) -> bytes:
    """Decrypt the response body for diagnostics; do not parse the framing."""
    return decrypt_body(blob)


def _metadata_value(item: object) -> tuple[str, str] | None:
    if isinstance(item, tuple) and len(item) == 2:
        key, value = item
    else:
        key = getattr(item, "key", None)
        value = getattr(item, "value", None)
    if key is None or value is None:
        return None
    if isinstance(key, bytes):
        key = key.decode("utf-8", errors="replace")
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(key), str(value)


def extract_resource_version(metadata: Iterable[object]) -> str | None:
    """Return ``x-resource-version`` from gRPC metadata."""
    for item in metadata:
        pair = _metadata_value(item)
        if pair is None:
            continue
        key, value = pair
        if key.lower() == "x-resource-version":
            return value
    return None


def fetch_resource_version(
    profile: VersionProfile,
    *,
    timeout: float = 10.0,
    attempts: int = 3,
    retry_delay: float = 2.0,
    verbose: bool = False,
) -> str:
    """Call GetVersion and return the full resource version.

    Raises:
        VersionError: all attempts failed, or the server returned only the
            simple version (which usually means the profile's headers are
            stale or the platform header is wrong).
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if verbose:
                LOGGER.info("GetVersion attempt %d/%d", attempt, attempts)
                LOGGER.info("target: %s", profile.target)
                for key, value in profile.metadata():
                    LOGGER.info("header %s=%r", key, value)

            credentials = grpc.ssl_channel_credentials()
            with grpc.secure_channel(profile.target, credentials) as channel:
                call = channel.unary_unary(
                    METHOD_PATH,
                    request_serializer=serialize_request,
                    response_deserializer=deserialize_response,
                )
                response, call_obj = call.with_call(
                    b"",
                    metadata=profile.metadata(),
                    timeout=timeout,
                )
                initial_metadata = list(call_obj.initial_metadata())
                if verbose:
                    for key, value in initial_metadata:
                        LOGGER.info("response metadata %s=%s", key, value)
                    LOGGER.info("decrypted body length: %d", len(response))

                version = extract_resource_version(initial_metadata)
                if version is None:
                    raise VersionError(
                        "x-resource-version was not present in the response metadata"
                    )
                if "@" not in version:
                    raise VersionError(
                        "server returned only the simple version "
                        f"({version!r}); check x-os-name=android and "
                        "x-application-platform=google, and make sure the "
                        "profile's game version/version hash are current"
                    )
                decode_full_version(version)
                return version

        except grpc.RpcError as exc:
            last_error = exc
            LOGGER.warning(
                "GetVersion attempt %d/%d gRPC error: %s %s",
                attempt,
                attempts,
                exc.code(),
                exc.details(),
            )
        except (TimeoutError, OSError) as exc:
            last_error = exc
            LOGGER.warning(
                "GetVersion attempt %d/%d network error: %s",
                attempt,
                attempts,
                exc,
            )
        except VersionError as exc:
            last_error = exc
            LOGGER.warning(
                "GetVersion attempt %d/%d version error: %s",
                attempt,
                attempts,
                exc,
            )
        except Exception as exc:
            last_error = exc
            LOGGER.warning(
                "GetVersion attempt %d/%d unexpected error: %s",
                attempt,
                attempts,
                exc,
            )

        if attempt < attempts:
            time.sleep(retry_delay)

    raise VersionError(
        f"GetVersion failed after {attempts} attempt(s): {last_error}"
        f"{_version_error_hint(last_error)}"
    ) from last_error


def _version_error_hint(error: Exception | None) -> str:
    if error is not None and "invalid version hash" in str(error).lower():
        return f"\nHint: {VERSION_HASH_HINT}"
    return ""


def _profile_from_dict(name: str, data: object) -> VersionProfile:
    if not isinstance(data, dict):
        raise VersionError(f"profile {name!r} must be a JSON object")
    game_version = data.get("gameVersion")
    version_hash = data.get("versionHash")
    if not isinstance(game_version, str) or not game_version:
        raise VersionError(f"profile {name!r} is missing gameVersion")
    if not isinstance(version_hash, str) or not version_hash:
        raise VersionError(f"profile {name!r} is missing versionHash")
    return VersionProfile(
        game_version=game_version,
        version_hash=version_hash,
        target=str(data.get("target", DEFAULT_GRPC_TARGET)),
        resource_target=str(data.get("resourceTarget", "production")),
        application_platform=str(data.get("applicationPlatform", "google")),
        os_name=str(data.get("osName", "android")),
    )


def user_config_dir() -> Path:
    """Return the per-user configuration directory for prism-auto."""
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = Path(base) if base else Path.home() / ".config"
    return root / "prism-auto"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_profiles_path(
    path: str | Path | None = None,
    *,
    for_write: bool = False,
) -> Path:
    """Find the profile file, preferring the current directory.

    An explicit *path* always wins.  Otherwise ``./version_profiles.json``,
    the project root and the user config directory are checked in that order.
    When no file exists, the user config path is returned (and used by
    ``profile set`` unless ``--profiles-file`` is given).
    """
    if path is not None:
        return Path(path)
    candidates = [
        Path.cwd() / "version_profiles.json",
        project_root() / "version_profiles.json",
        user_config_dir() / "version_profiles.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[-1]


def parse_profiles(
    raw: object,
    source: str = "profiles",
) -> tuple[dict[str, VersionProfile], str]:
    """Parse a profile document.

    Accepted shapes::

        {"gameVersion": "...", "versionHash": "..."}

        {
            "default": "2.19.1",
            "profiles": {"2.19.1": {"gameVersion": "...", "versionHash": "..."}},
        }
    """
    if not isinstance(raw, dict):
        raise VersionError(f"profile data is not a JSON object: {source}")

    if "gameVersion" in raw and "versionHash" in raw:
        # A single profile object is also accepted.
        return {"default": _profile_from_dict("default", raw)}, "default"

    if "profiles" in raw:
        profiles_data = raw["profiles"]
        default_name = raw.get("default", DEFAULT_PROFILE_NAME)
    else:
        profiles_data = raw
        default_name = DEFAULT_PROFILE_NAME

    if not isinstance(profiles_data, dict) or not profiles_data:
        raise VersionError(f"profile data has no profiles: {source}")

    profiles = {
        str(name): _profile_from_dict(str(name), data)
        for name, data in profiles_data.items()
    }
    if not isinstance(default_name, str) or default_name not in profiles:
        default_name = next(iter(profiles))
    return profiles, default_name


def load_profiles(
    path: str | Path,
) -> tuple[dict[str, VersionProfile], str]:
    """Load version profiles from an explicit JSON file.

    The library ships no built-in game version or version hash, so callers
    must always name a profile file.  There is no fallback: an absent file or
    an unreadable payload raises instead of silently returning a profile that
    may already be out of date.

    Args:
        path: Path to the profile document.

    Returns:
        A ``(profiles, default_name)`` pair.

    Raises:
        TypeError: If ``path`` is ``None``.
        VersionError: If the file is missing, unreadable or malformed.
    """
    if path is None:
        raise TypeError(
            "load_profiles() requires an explicit profile file path; "
            "the library provides no built-in game version"
        )

    profile_path = Path(path)
    if not profile_path.is_file():
        raise VersionError(f"profile file not found: {profile_path}")

    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise VersionError(f"cannot read profile file {profile_path}: {exc}") from exc

    return parse_profiles(raw, str(profile_path))


def load_profiles_from_bytes(
    data: bytes,
    source: str = "profile data",
) -> tuple[dict[str, VersionProfile], str]:
    if len(data) > MAX_PROFILE_BYTES:
        raise VersionError(f"profile data is too large: {len(data)} bytes")
    try:
        raw = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise VersionError(f"cannot parse profile data from {source}: {exc}") from exc
    return parse_profiles(raw, source)


def fetch_profile_bytes(
    url: str,
    *,
    timeout: float = 30.0,
    trust_env: bool = True,
    sha256: str | None = None,
) -> bytes:
    """Fetch a remote profile document, optionally pinned by SHA-256."""
    import httpx

    try:
        with (
            httpx.Client(
                timeout=httpx.Timeout(timeout),
                trust_env=trust_env,
                follow_redirects=True,
                headers={"Accept": "application/json"},
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > MAX_PROFILE_BYTES:
                    raise VersionError(
                        f"profile URL response is too large: {total}+ bytes"
                    )
                chunks.append(chunk)
            data = b"".join(chunks)
    except VersionError:
        raise
    except httpx.HTTPError as exc:
        raise VersionError(f"cannot fetch profile URL {url}: {exc}") from exc

    if sha256:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != sha256.strip().lower():
            raise VersionError(
                f"profile URL SHA-256 mismatch: expected {sha256}, got {actual}"
            )
    return data


def profile_to_dict(profile: VersionProfile) -> dict[str, str]:
    return {
        "gameVersion": profile.game_version,
        "versionHash": profile.version_hash,
        "target": profile.target,
        "resourceTarget": profile.resource_target,
        "applicationPlatform": profile.application_platform,
        "osName": profile.os_name,
    }


def profiles_to_dict(
    profiles: dict[str, VersionProfile],
    default_name: str,
) -> dict[str, object]:
    return {
        "default": default_name,
        "profiles": {
            name: profile_to_dict(profile) for name, profile in profiles.items()
        },
    }


def save_profiles(
    path: str | Path,
    profiles: dict[str, VersionProfile],
    default_name: str,
) -> None:
    if not profiles:
        raise VersionError("cannot save an empty profile set")
    if default_name not in profiles:
        default_name = next(iter(profiles))
    _write_json_atomic(
        path,
        profiles_to_dict(profiles, default_name),
        pretty=True,
    )


def apply_overrides(
    profile: VersionProfile,
    *,
    game_version: str | None = None,
    version_hash: str | None = None,
    target: str | None = None,
    resource_target: str | None = None,
) -> VersionProfile:
    return replace(
        profile,
        game_version=game_version or profile.game_version,
        version_hash=version_hash or profile.version_hash,
        target=target or profile.target,
        resource_target=resource_target or profile.resource_target,
    )


def read_local_version(output: str | Path) -> LocalVersion | None:
    """Read the local version marker from the output directory."""
    path = Path(output) / VERSION_MARKER_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        game_version = data["gameVersion"]
        resource_version = data["resourceVersion"]
    except Exception as exc:
        LOGGER.warning("ignoring corrupt version marker %s: %s", path, exc)
        return None
    if not isinstance(game_version, str) or not isinstance(resource_version, str):
        LOGGER.warning("ignoring version marker with invalid fields: %s", path)
        return None
    selection = data.get("selection")
    return LocalVersion(
        game_version=game_version,
        resource_version=resource_version,
        catalog_fingerprint=data.get("catalogFingerprint"),
        synced_at=data.get("syncedAt"),
        selection=selection if isinstance(selection, dict) else None,
    )


def write_local_version(
    output: str | Path,
    *,
    game_version: str,
    resource_version: str,
    catalog_fingerprint: str | None,
    selection: dict[str, object] | None = None,
) -> None:
    """Write the local version marker to the output directory."""
    _write_json_atomic(
        Path(output) / VERSION_MARKER_NAME,
        {
            "gameVersion": game_version,
            "resourceVersion": resource_version,
            "catalogFingerprint": catalog_fingerprint,
            "syncedAt": datetime.now(UTC).isoformat(),
            "selection": selection,
        },
    )


def check_version(
    profile: VersionProfile,
    output: str | Path,
    remote_resource_version: str,
    selection: Selection | None = None,
) -> VersionCheck:
    """Compare a remote resource version and selection with the local marker."""
    try:
        full = decode_full_version(remote_resource_version)
    except Exception as exc:
        raise VersionError(
            f"remote resource version is invalid: {remote_resource_version!r}: {exc}"
        ) from exc

    local = read_local_version(output)
    if local is None:
        return VersionCheck(
            game_version=profile.game_version,
            remote_resource_version=remote_resource_version,
            remote_simple=full.simple,
            local=None,
            up_to_date=False,
            reason="no local version marker",
        )

    if local.game_version != profile.game_version:
        reason = (
            f"game version changed: local {local.game_version!r}, "
            f"remote {profile.game_version!r}"
        )
        up_to_date = False
    elif local.resource_version != remote_resource_version:
        reason = (
            f"resource version changed: local {local.resource_version!r}, "
            f"remote {remote_resource_version!r}"
        )
        up_to_date = False
    else:
        current_selection = (
            selection.to_dict() if selection is not None and selection.active else None
        )
        if local.selection is None and current_selection is None:
            reason = "up to date"
            up_to_date = True
        elif local.selection is None:
            # The last sync was a full sync, so the selected subset is already
            # present on disk.
            reason = "up to date"
            up_to_date = True
        elif current_selection is None:
            reason = "local sync was filtered; a full sync is required"
            up_to_date = False
        elif local.selection != current_selection:
            reason = "selection changed"
            up_to_date = False
        else:
            reason = "up to date"
            up_to_date = True

    return VersionCheck(
        game_version=profile.game_version,
        remote_resource_version=remote_resource_version,
        remote_simple=full.simple,
        local=local,
        up_to_date=up_to_date,
        reason=reason,
    )


def resolve_remote_version(
    profile: VersionProfile,
    *,
    resource_version: str | None = None,
    fetcher: Callable[[VersionProfile], str] | None = None,
    timeout: float = 10.0,
    attempts: int = 3,
    retry_delay: float = 2.0,
    verbose: bool = False,
) -> str:
    """Use an explicit version or call the gRPC fetcher."""
    if resource_version:
        decode_full_version(resource_version)
        return resource_version

    if fetcher is not None:
        version = fetcher(profile)
    else:
        version = fetch_resource_version(
            profile,
            timeout=timeout,
            attempts=attempts,
            retry_delay=retry_delay,
            verbose=verbose,
        )
    decode_full_version(version)
    return version


class GrpcVersionProvider:
    """Resolve the remote resource version through the game's gRPC call.

    Implements: VersionProvider
    """

    def __init__(
        self,
        profile: VersionProfile,
        *,
        resource_version: str | None = None,
        fetcher: Callable[[VersionProfile], str] | None = None,
        timeout: float = 10.0,
        attempts: int = 3,
        retry_delay: float = 2.0,
        verbose: bool = False,
    ) -> None:
        """Initialize the gRPC version provider."""
        self.profile = profile
        self.resource_version = resource_version
        self.fetcher = fetcher
        self.timeout = timeout
        self.attempts = attempts
        self.retry_delay = retry_delay
        self.verbose = verbose

    def get_current(self, _profile: str | None = None) -> str:
        """Get the current remote resource version."""
        return resolve_remote_version(
            self.profile,
            resource_version=self.resource_version,
            fetcher=self.fetcher,
            timeout=self.timeout,
            attempts=self.attempts,
            retry_delay=self.retry_delay,
            verbose=self.verbose,
        )

    def check_local(
        self,
        profile: VersionProfile,
        output: str | Path,
        remote_resource_version: str,
        selection: Selection | None = None,
    ) -> VersionCheck:
        """Check the local version against the remote version."""
        return check_version(profile, output, remote_resource_version, selection)

    def write_local(
        self,
        output: str | Path,
        *,
        game_version: str,
        resource_version: str,
        catalog_fingerprint: str | None,
        selection: dict[str, object] | None = None,
    ) -> None:
        """Write the local version marker."""
        write_local_version(
            output,
            game_version=game_version,
            resource_version=resource_version,
            catalog_fingerprint=catalog_fingerprint,
            selection=selection,
        )
