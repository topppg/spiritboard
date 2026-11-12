"""Entry point for the bundled macOS dashboard server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from spiritboard.launcher import Instance, LaunchError, default_data_dir
from spiritboard.startup import ENVIRONMENT_SELECTORS, load_startup


def announce(url: str, instance_id: str | None) -> None:
    if instance_id is None:
        raise LaunchError("The dashboard has no verified instance identity.")
    uuid.UUID(instance_id)
    print(
        "Spiritboard app ready: " + json.dumps({"url": url, "instance_id": instance_id}),
        flush=True,
    )


def main(argv: list[str]) -> int:
    if argv and argv[0] == "input-event":
        from spiritboard.input_receive import main as receive

        return receive(argv)
    parser = argparse.ArgumentParser(prog="spiritboard-backend")
    parser.add_argument("--data-dir", type=Path, help="open this dashboard's saved setup")
    args = parser.parse_args(argv)
    directory = (args.data_dir or default_data_dir()).expanduser().absolute()
    try:
        if args.data_dir is not None and not directory.is_dir():
            raise LaunchError("The selected data folder is missing. Choose its current location.")
        if (directory / "instance.lock").is_file():
            with Instance(directory, {}) as instance:
                if not instance.owner:
                    url = instance.existing_url(match_settings=False)
                    announce(url, instance.instance_id)
                    return 0
        saved = load_startup(directory)
        if saved is None and args.data_dir is not None:
            raise LaunchError(
                "This folder has no saved startup settings. Start it once with "
                "spiritboard serve and its usual options, then reopen the app."
            )
        from spiritboard import cli

        if saved is None:
            return cli.main(
                ["serve", "--no-open", "--port", "0", "--data-dir", str(directory)],
                on_ready=announce,
            )
        arguments, environment = saved
        keys = ENVIRONMENT_SELECTORS
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ.update(environment)
            return cli.main(
                ["serve", *arguments, "--data-dir", str(directory), "--no-open"],
                on_ready=announce,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    except LaunchError as error:
        print(f"Spiritboard could not connect: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
