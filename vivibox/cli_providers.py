"""The command line's keys and models: vivibox auth and vivibox models, apart from cli.py to keep
it under the size limit."""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from . import image, keys, providers
from .providers import is_provider_key


def _yes(question: str) -> bool:
    """Yes without a terminal to ask in: a script that runs the import means it."""
    return not sys.stdin.isatty() or input(question).strip().lower() in ("y", "yes")


def cmd_auth(args: argparse.Namespace) -> int:
    if args.action == "list":
        stored = keys.list_keys()
        for provider, shown in stored.items():
            print(f"{provider:20} {shown}")
        if not stored:
            print(f"No keys in {keys.store()}. Add one with: vivibox auth set <provider>")
    elif args.action == "set":
        if not args.provider:
            raise keys.KeyStoreError("which provider? e.g. vivibox auth set deepseek")
        value = getpass.getpass(f"API key for {args.provider}: ") if sys.stdin.isatty() else sys.stdin.read()
        keys.set_key(args.provider, value)
        print(f"Stored {args.provider}: {keys.masked(keys.get_key(args.provider))} in {keys.store()}")
    elif args.action == "import":
        reading = providers.read_opencode(Path(args.provider or providers.DEFAULT_SOURCE))
        said = {"new": "", "replaces": "  (you have a different one)", "same": "  (same as yours, skipped)"}
        for f in reading.found:
            print(f"{f.kind:9} {f.name:20} {f.what}, key {f.key}{said[f.status]}")
        if reading.left:
            print(f"Left in the file, not for vivibox: {', '.join(reading.left)}.")
        new = [f for f in reading.found if f.status == "new"]
        differ = [f for f in reading.found if f.status == "replaces"]
        chosen = new if new and _yes(f"Import the {len(new)} new? [y/N] ") else []
        if differ and _yes(f"Overwrite yours with {', '.join(f.name for f in differ)}? [y/N] "):
            chosen += differ
        if not chosen:
            print("Nothing imported.")
            return 1
        providers.bring_over(chosen)
        print(f"Imported {', '.join(f.name for f in chosen)}; press k in vivibox to see them.")
    elif args.action == "rm":
        if not args.provider:
            raise keys.KeyStoreError("which provider? e.g. vivibox auth rm deepseek")
        print(f"Removed {args.provider}." if keys.remove(args.provider) else f"No key for {args.provider}.")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """Asks opencode which models it knows for the providers you have keys for. Display only."""
    providers = [args.provider] if args.provider else list(filter(is_provider_key, keys.list_keys()))
    if not providers:
        raise keys.KeyStoreError("no keys yet; add one with: vivibox auth set <provider>")
    env = []
    for provider in providers:
        # opencode lists a provider's models only when it has a key; a placeholder is enough to list.
        env += ["-e", f"{provider.upper().replace('-', '_').replace('.', '_')}_API_KEY=placeholder"]
    ref = image.image_ref()
    cmd = ["docker", "run", "--rm", "--tmpfs", f"/config:uid={os.getuid()},gid={os.getgid()}", *env, ref,
           "opencode", "models", *([args.provider] if args.provider else [])]  # fmt: skip
    return subprocess.run(cmd).returncode
