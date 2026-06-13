#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration loading and secret resolution for wallet-autocapture.

This module is the single source of truth for reading ``config.json`` and for
turning secret *references* (an environment-variable name or a file path) into
the actual secret value. No personal data lives here -- everything comes from
the user's own ``config.json`` (which is git-ignored) and from their
environment / secret files.

Only the Python standard library is used.
"""
import json
import os


def load_config(path="config.json"):
    """Load and parse the JSON config file.

    Args:
        path: Path to the config file. Relative paths are resolved against the
            current working directory, so callers typically pass an absolute or
            project-rooted path.

    Returns:
        dict: The parsed configuration.

    Raises:
        FileNotFoundError: If ``path`` does not exist, with a hint to run the
            setup wizard.
        ValueError: If the file is not valid JSON.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            "Copy config.example.json to config.json and run setup_wizard.py "
            "to fill in your Wallet account/label/category UUIDs."
        )
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Config file {path} is not valid JSON: {e}") from e


def resolve_secret(env_name, file_path):
    """Resolve a secret from an environment variable or a file, in that order.

    The environment variable wins when both are set, because env vars are the
    more ephemeral / explicit source. ``~`` in ``file_path`` is expanded so a
    config can point at e.g. ``~/.wallet_token`` portably.

    Args:
        env_name: Name of an environment variable that may hold the secret
            (may be ``None`` / empty to skip the env lookup).
        file_path: Path to a file whose (stripped) contents are the secret
            (may be ``None`` / empty to skip the file lookup).

    Returns:
        str: The secret value, or an empty string if neither source yields one.
    """
    if env_name:
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    if file_path:
        expanded = os.path.expanduser(file_path)
        if os.path.exists(expanded):
            with open(expanded, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
    return ""


def get_token(cfg):
    """Resolve the Wallet API bearer token from the config's ``wallet`` block.

    The token is read from the file named by ``cfg["wallet"]["token_file"]``
    (default ``.wallet_token``) and/or the ``WALLET_API_TOKEN`` environment
    variable. The token file path is resolved relative to the config's own
    directory is NOT assumed here -- the caller passes whatever path the config
    holds. The env var ``WALLET_API_TOKEN`` always takes precedence.

    Args:
        cfg: A loaded config dict (see :func:`load_config`).

    Returns:
        str: The bearer token, or an empty string if none is available.
    """
    wallet = cfg.get("wallet", {}) or {}
    token_file = wallet.get("token_file", ".wallet_token")
    # Token always wins from the dedicated env var; fall back to the token file.
    return resolve_secret("WALLET_API_TOKEN", token_file)
