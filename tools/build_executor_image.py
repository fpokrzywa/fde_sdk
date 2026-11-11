"""Build the locked Linux/amd64 executor image; optional push is loopback-only for CI."""

import argparse
import json
import re
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="shifde-executor:dev")
    parser.add_argument("--push-local", action="store_true")
    args = parser.parse_args()
    output = Path(".tools/executor-wheels")
    output.mkdir(parents=True, exist_ok=True)
    for package in (
        "shifde-contracts",
        "shifde-core",
        "shifde-databricks",
        "shifde-snowflake",
        "shifde-fabric",
        "shifde-executor",
    ):
        subprocess.run(
            [
                "uv",
                "build",
                "--package",
                package,
                "--wheel",
                "--out-dir",
                str(output),
                "--link-mode",
                "copy",
            ],
            check=True,
        )
    subprocess.run(
        [
            "uv",
            "export",
            "--package",
            "shifde-executor",
            "--no-dev",
            "--no-editable",
            "--no-emit-workspace",
            "--frozen",
            "--output-file",
            str(output / "requirements.txt"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-f",
            "infra/containers/executor.Dockerfile",
            "-t",
            args.tag,
            ".",
        ],
        check=True,
    )
    if args.push_local:
        if not re.fullmatch(r"(?:localhost|127\.0\.0\.1):[0-9]+/[a-z0-9_./:-]+", args.tag):
            raise ValueError("this helper only pushes to an explicit loopback test registry")
        subprocess.run(["docker", "push", args.tag], check=True)
    document = json.loads(subprocess.check_output(["docker", "image", "inspect", args.tag]))[0]
    digests = document["RepoDigests"]
    if not digests:
        raise RuntimeError(
            "engine did not retain a repository digest; use a loopback registry with --push-local"
        )
    print("Executor image: " + digests[0])


if __name__ == "__main__":
    main()
