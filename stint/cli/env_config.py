"""Env config loader: ``--env prod`` reads connection params from a YAML file.

Search order:
  1. ``$STINT_CONFIG_DIR/<env>.yaml`` (if env var set)
  2. ``./.stint/<env>.yaml``    (project-local; usually .gitignored)
  3. ``~/.stint/envs/<env>.yaml`` (user-global)

Recognized keys (all optional; CLI flags override):
  url, dialect, auth, token_env, user_env, verify_ssl

The config is merged into the argparse namespace BEFORE the explicit flags
are applied, so explicit flags always win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

import yaml

from stint.exceptions import ConfigurationError

CONFIG_KEYS = ("url", "dialect", "auth", "token_env", "user_env", "verify_ssl")

LiteralStr = TypeVar("LiteralStr", bound=str)

# Connection enums. Defined once here; CLI modules import these so the param
# annotations and the resolved values share a single source of truth.
AuthMode = Literal["pat", "basic", "api-token"]
DialectName = Literal["jira_cloud"]

_AUTH_MODES: tuple[AuthMode, ...] = ("pat", "basic", "api-token")
_DIALECT_NAMES: tuple[DialectName, ...] = ("jira_cloud",)


def _validate_literal(value: str | None, allowed: tuple[LiteralStr, ...], field: str) -> LiteralStr | None:
    """Confirm a config-sourced string is one of `allowed`, narrowing to its type, else raise."""
    if value is None:
        return None
    if value not in allowed:
        raise ConfigurationError(f"invalid {field} {value!r}; expected one of {sorted(allowed)}")
    return cast("LiteralStr", value)


def find_env_config(env_name: str) -> Path | None:
    """Return the first existing config file matching this env name, or None."""
    for candidate in _candidate_paths(env_name):
        if candidate.is_file():
            return candidate
    return None


def _candidate_paths(env_name: str) -> list[Path]:
    paths: list[Path] = []
    custom = os.environ.get("STINT_CONFIG_DIR")
    if custom:
        paths.append(Path(custom) / f"{env_name}.yaml")
    paths.append(Path.cwd() / ".stint" / f"{env_name}.yaml")
    paths.append(Path.home() / ".stint" / "envs" / f"{env_name}.yaml")
    return paths


def load_env_config(env_name: str) -> dict[str, Any]:
    """Read the YAML file for `env_name`. Empty dict if no config found.
    Raises ConfigurationError on malformed YAML or unknown keys."""
    path = find_env_config(env_name)
    if path is None:
        return {}
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigurationError(f"env config {path!s} is not valid YAML: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"env config {path!s} must be a mapping, got {type(raw).__name__}")
    unknown = set(raw) - set(CONFIG_KEYS)
    if unknown:
        raise ConfigurationError(
            f"env config {path!s} has unknown keys {sorted(unknown)}; recognized: {list(CONFIG_KEYS)}"
        )
    return raw


def resolve_connection(
    *,
    env: str | None,
    url: str | None,
    auth: AuthMode | None,
    dialect: DialectName | None,
    token_env: str | None,
    user_env: str | None,
    no_verify_ssl: bool,
) -> tuple[str | None, AuthMode | None, DialectName | None, str, str, bool]:
    """Merge env-config values for any connection params the caller did not set.

    ``auth`` / ``dialect`` sourced from YAML are validated against their allowed
    values, so a config typo raises instead of leaking an invalid string.

    ``token_env`` / ``user_env`` are returned as concrete strings (never None):
    the YAML value wins over the default when no CLI flag is set, but
    something has to be returned for the env-var lookup.
    """
    cfg = load_env_config(env) if env else {}
    if not cfg:
        return url, auth, dialect, token_env or "STINT_TOKEN", user_env or "STINT_USER", no_verify_ssl
    if not url:
        url = cfg.get("url")
    if not auth:
        auth = _validate_literal(cfg.get("auth"), _AUTH_MODES, "auth")
    if not dialect:
        dialect = _validate_literal(cfg.get("dialect"), _DIALECT_NAMES, "dialect")
    if token_env is None:
        token_env = cfg.get("token_env") or "STINT_TOKEN"
    if user_env is None:
        user_env = cfg.get("user_env") or "STINT_USER"
    if "verify_ssl" in cfg and not cfg["verify_ssl"] and not no_verify_ssl:
        no_verify_ssl = True
    return url, auth, dialect, token_env, user_env, no_verify_ssl


def require_resolved_connection(*, env: str | None, url: str | None, auth: AuthMode | None) -> tuple[str, AuthMode]:
    """Return (url, auth) once both are present; raise SystemExit listing what is missing.

    Returning the values lets callers rebind ``url, auth`` to their non-optional
    types, so the connection params flow into ``create_engine`` / ``_build_auth``
    without a separate None-check at each call site.
    """
    if url and auth:
        return url, auth
    missing = [k for k, v in (("url", url), ("auth", auth)) if not v]
    label = env or "<env>"
    raise SystemExit(
        f"missing required connection params: {missing}. "
        f"Provide via --{'/--'.join(missing)} or place a config at "
        f"./.stint/{label}.yaml or ~/.stint/envs/{label}.yaml."
    )


def apply_env_defaults(args: Any, env_name: str | None) -> None:
    """Fill in argparse `args` from the env config IF a flag was not set on
    the command line. Mutates `args` in place.

    Detection of "set on the command line" vs "argparse default" is approximate:
    we check for falsy values (None, empty string, default constant). For
    `verify_ssl`, the default is True; we treat a True value as
    "use config if config disagrees", since the CLI flag is `--no-verify-ssl`
    (negative flag).
    """
    if not env_name:
        return
    cfg = load_env_config(env_name)
    if not cfg:
        return
    for key in ("url", "dialect", "auth", "token_env", "user_env"):
        if key in cfg and not getattr(args, key, None):
            setattr(args, key, cfg[key])
    # verify_ssl semantics: the CLI uses --no-verify-ssl (sets no_verify_ssl=True).
    # The config uses verify_ssl: bool. If config says False, set no_verify_ssl.
    if "verify_ssl" in cfg and not cfg["verify_ssl"]:
        if not getattr(args, "no_verify_ssl", False):
            args.no_verify_ssl = True
