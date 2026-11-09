import asyncio
import json
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from shifde_contracts.common import Target

from shifde_databricks.source import CLI_VERSION


class WorkspaceGateway(Protocol):
    async def get_app(self, name: str) -> dict | None: ...
    async def probe(self, warehouse_id: str) -> None: ...
    async def health_check(self, url: str) -> dict: ...


class BundleRunner(Protocol):
    async def check_version(self) -> str: ...
    async def execute(
        self, action: str, directory: Path, *, target: str, host: str, app_key: str | None = None
    ) -> dict: ...


class DatabricksWorkspaceGateway:
    def __init__(self, target: Target, *, client: WorkspaceClient | None = None):
        self.target = target
        self.client = client or WorkspaceClient(host=target.host)
        if self.client.config.host.rstrip("/") != target.host:
            raise ValueError("SDK workspace host does not match target")

    async def get_app(self, name: str) -> dict | None:
        try:
            result = await asyncio.to_thread(self.client.apps.get, name)
            return result.as_dict()
        except NotFound:
            return None

    async def probe(self, warehouse_id: str) -> None:
        await asyncio.to_thread(self.client.warehouses.get, warehouse_id)
        await asyncio.to_thread(lambda: next(iter(self.client.apps.list()), None))

    async def health_check(self, url: str) -> dict:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or not parsed.hostname.endswith(".databricksapps.com")
            or parsed.port not in (None, 443)
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("refusing to send credentials to an unrecognized application origin")
        headers = await asyncio.to_thread(self.client.config.authenticate)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=5), follow_redirects=False
        ) as client:
            response = await client.get(url.rstrip("/") + "/health", headers=headers)
            response.raise_for_status()
            return response.json()


def workspace_token(host: str) -> str:
    headers = WorkspaceClient(host=host).config.authenticate()
    authorization = headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise ValueError("Databricks bearer credentials are required")
    return authorization.removeprefix("Bearer ")


class DatabricksCLI:
    def __init__(
        self,
        executable: str = "databricks",
        *,
        token_provider: Callable[[str], str] = workspace_token,
        timeout: float = 900,
    ):
        self.executable, self.token_provider, self.timeout = executable, token_provider, timeout

    async def _run(self, args: list[str], directory: Path | None, env: dict[str, str]) -> bytes:
        executable = shutil.which(self.executable)
        if not executable:
            raise FileNotFoundError(
                "install the pinned Databricks CLI or provide its executable path"
            )
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            cwd=directory,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if process.returncode:
            # Raw CLI output may contain native metadata or credentials; never return it to an agent.
            raise RuntimeError(f"Databricks CLI command failed with exit code {process.returncode}")
        return stdout

    @staticmethod
    def _base_env() -> dict[str, str]:
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HOME",
            "USERPROFILE",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        return {key: value for key, value in os.environ.items() if key.upper() in allowed}

    async def check_version(self) -> str:
        output = await self._run(["--version"], None, self._base_env())
        match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", output.decode())
        if not match or match.group(1) != CLI_VERSION:
            raise ValueError(f"Databricks CLI {CLI_VERSION} is required")
        return match.group(1)

    async def execute(
        self, action: str, directory: Path, *, target: str, host: str, app_key: str | None = None
    ) -> dict:
        if action not in ("deploy", "run", "validate") or (action == "run" and not app_key):
            raise ValueError("unsupported bundle operation")
        await self.check_version()
        env = self._base_env()
        token = await asyncio.to_thread(self.token_provider, host)
        env.update(DATABRICKS_HOST=host, DATABRICKS_TOKEN=token, DATABRICKS_AUTH_TYPE="pat")
        args = ["bundle", action]
        if action == "run":
            args.append(app_key or "")
        args += ["--target", target, "--output", "json"]
        output = await self._run(args, directory, env)
        # Successful deployment output varies by command/version. Native SDK reads are authoritative.
        try:
            result = json.loads(output)
            return result if isinstance(result, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
