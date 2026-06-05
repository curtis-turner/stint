"""Env config loader: ``--env prod`` reads connection params from a YAML file.

Search order:
  1. ``$PENSUM_CONFIG_DIR/<env>.yaml`` (if env var set)
  2. ``./.pensum/<env>.yaml``    (project-local; usually .gitignored)
  3. ``~/.pensum/envs/<env>.yaml`` (user-global)

Recognized keys (all optional; CLI flags override):
  url, dialect, auth, token_env, user_env, verify_ssl

The config is merged into the argparse namespace BEFORE the explicit flags
are applied, so explicit flags always win.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from pensum.exceptions import ConfigurationError

CONFIG_KEYS = ("url", "dialect", "auth", "token_env", "user_env", "verify_ssl")


def find_env_config(env_name: str) -> Path | None:
    """Return the first existing config file matching this env name, or None."""
    for candidate in _candidate_paths(env_name):
        if candidate.is_file():
            return candidate
    return None


def _candidate_paths(env_name: str) -> list[Path]:
    paths: list[Path] = []
    custom = os.environ.get("PENSUM_CONFIG_DIR")
    if custom:
        paths.append(Path(custom) / f"{env_name}.yaml")
    paths.append(Path.cwd() / ".pensum" / f"{env_name}.yaml")
    paths.append(Path.home() / ".pensum" / "envs" / f"{env_name}.yaml")
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
    auth: str | None,
    dialect: str | None,
    token_env: str | None,
    user_env: str | None,
    no_verify_ssl: bool,
) -> tuple[str | None, str | None, str | None, str, str, bool]:
    """Merge env-config values for any connection params the caller did not set.

    ``token_env`` / ``user_env`` are returned as concrete strings (never None):
    the YAML value wins over the default when no CLI flag is set, but
    something has to be returned for the env-var lookup.
    """
    cfg = load_env_config(env) if env else {}
    if not cfg:
        return url, auth, dialect, token_env or "PENSUM_TOKEN", user_env or "PENSUM_USER", no_verify_ssl
    if not url:
        url = cfg.get("url")
    if not auth:
        auth = cfg.get("auth")
    if not dialect:
        dialect = cfg.get("dialect")
    if token_env is None:
        token_env = cfg.get("token_env") or "PENSUM_TOKEN"
    if user_env is None:
        user_env = cfg.get("user_env") or "PENSUM_USER"
    if "verify_ssl" in cfg and not cfg["verify_ssl"] and not no_verify_ssl:
        no_verify_ssl = True
    return url, auth, dialect, token_env, user_env, no_verify_ssl


def require_resolved_connection(*, env: str | None, url: str | None, auth: str | None) -> None:
    """Raise SystemExit listing the connection params that are still missing."""
    missing = [k for k, v in (("url", url), ("auth", auth)) if not v]
    if not missing:
        return
    label = env or "<env>"
    raise SystemExit(
        f"missing required connection params: {missing}. "
        f"Provide via --{'/--'.join(missing)} or place a config at "
        f"./.pensum/{label}.yaml or ~/.pensum/envs/{label}.yaml."
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
