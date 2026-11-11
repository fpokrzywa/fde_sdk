"""Bounded official connector queries and hook-free Snowflake CLI stage copies."""

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from shifde_snowflake.source import CLI_VERSION, identifier, literal, quoted


class SnowflakeClient:
    def __init__(
        self,
        target,
        credential_provider,
        *,
        cli="/opt/snowcli/bin/snow",
        query_tag="shifde-native-app",
    ):
        self.target, self.credential_provider, self.cli = target, credential_provider, cli
        self.query_tag = query_tag
        host = urlsplit(target.host).hostname
        if not host or not host.endswith((".snowflakecomputing.com", ".snowflakecomputing.cn")):
            raise ValueError("unsupported Snowflake account host")
        self.host = host

    def _options(self):
        auth = self.credential_provider()
        profile = auth.get("snowflake")
        if not profile or profile.get("host") != self.host:
            raise PermissionError("Snowflake credential profile mismatch")
        return auth, profile

    def _query(self, sql, warehouse=None):
        import snowflake.connector

        auth, profile = self._options()
        options = dict(
            account=profile["account"],
            host=self.host,
            role=identifier(profile["role"]),
            authenticator="oauth",
            token=auth["bearer"],
            login_timeout=10,
            network_timeout=30,
            client_session_keep_alive=False,
            session_parameters={
                "STATEMENT_TIMEOUT_IN_SECONDS": 120,
                "STATEMENT_QUEUED_TIMEOUT_IN_SECONDS": 30,
                "ABORT_DETACHED_QUERY": True,
                "QUERY_TAG": self.query_tag,
                "TIMEZONE": "UTC",
            },
        )
        if profile.get("user"):
            options["user"] = profile["user"]
        if warehouse:
            options["warehouse"] = identifier(warehouse)
        options["ocsp_fail_open"] = False
        with snowflake.connector.connect(**options) as connection:
            with connection.cursor(snowflake.connector.DictCursor) as cursor:
                cursor.execute(sql, timeout=120)
                rows = cursor.fetchmany(1001)
                if len(rows) > 1000:
                    raise ValueError("native metadata result exceeded its bound")

                def value(v):
                    if isinstance(v, (str, int, float, bool, type(None), dict, list)):
                        return v
                    return v.isoformat() if hasattr(v, "isoformat") else str(v)

                normalized = [{str(k).lower(): value(v) for k, v in row.items()} for row in rows]
                return {"rows": normalized, "query_id": cursor.sfqid or ""}

    async def execute(self, sql, *, warehouse=None):
        return await asyncio.to_thread(self._query, sql, warehouse)

    async def snapshot(self, names):
        session = (
            await self.execute("SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_ROLE() AS ROLE")
        )["rows"][0]

        async def exact(sql, name):
            rows = (await self.execute(sql))["rows"]
            return next((row for row in rows if row["name"] == name), None)

        package = await exact(
            "SHOW APPLICATION PACKAGES LIKE " + literal(names["package"]), names["package"]
        )
        application = await exact(
            "SHOW APPLICATIONS LIKE " + literal(names["application"]), names["application"]
        )
        versions = (
            (
                await self.execute(
                    "SHOW VERSIONS IN APPLICATION PACKAGE " + quoted(names["package"])
                )
            )["rows"]
            if package
            else []
        )
        stages = (
            (await self.execute("SHOW STAGES IN DATABASE " + quoted(names["package"])))["rows"]
            if package
            else []
        )
        stage = next(
            (s for s in stages if s["name"] == "RELEASES" and s.get("schema_name") == "SHIFDE"),
            None,
        )
        return {
            "account": session["account"],
            "role": identifier(session["role"]),
            "package": package,
            "application": application,
            "stage": stage,
            "versions": versions,
        }

    def _copy(self, source, destination):
        auth, profile = self._options()
        with tempfile.TemporaryDirectory(prefix="shifde-snowcli-") as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("[cli]\n", encoding="utf-8")
            config.chmod(0o600)
            token_file = Path(temporary) / "oauth-token"
            token_file.write_text(auth["bearer"], encoding="utf-8")
            token_file.chmod(0o600)
            environment = {
                k: os.environ[k]
                for k in ("PATH", "SystemRoot", "WINDIR", "SSL_CERT_FILE")
                if k in os.environ
            }
            environment.update(
                HOME=temporary, USERPROFILE=temporary, TMPDIR=temporary, TEMP=temporary
            )
            version = subprocess.run(
                [self.cli, "--version"],
                env=environment,
                cwd=temporary,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if version.returncode or CLI_VERSION not in version.stdout:
                raise ValueError("Snowflake CLI version mismatch")
            argv = [
                self.cli,
                "--config-file",
                str(config),
                "stage",
                "copy",
                source,
                destination,
                "--no-overwrite",
                "--no-auto-compress",
                "--no-refresh",
                "--temporary-connection",
                "--account",
                profile["account"],
                "--host",
                self.host,
                "--role",
                identifier(profile["role"]),
                "--authenticator",
                "oauth",
                "--token-file-path",
                str(token_file),
                "--format",
                "JSON",
            ]
            if profile.get("user"):
                argv.extend(["--user", profile["user"]])
            result = subprocess.run(
                argv, env=environment, cwd=temporary, capture_output=True, timeout=120
            )
            if result.returncode:
                raise RuntimeError("Snowflake stage transfer failed")
            return json.loads(result.stdout)

    async def upload(self, files, location):
        with tempfile.TemporaryDirectory(prefix="shifde-snow-source-") as temporary:
            for name, data in files.items():
                path = Path(temporary) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                parent = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
                await asyncio.to_thread(self._copy, str(path), location + "/" + parent)

    async def download(self, names, location):
        files: dict[str, bytes] = {}
        with tempfile.TemporaryDirectory(prefix="shifde-snow-check-") as temporary:
            for name in names:
                folder = Path(temporary) / str(len(files))
                folder.mkdir()
                await asyncio.to_thread(self._copy, location + "/" + name, str(folder))
                outputs = list(folder.rglob("*"))
                path = next(
                    (p for p in outputs if p.is_file() and p.name == name.rsplit("/", 1)[-1]), None
                )
                if path is None or path.stat().st_size > 20 * 1024 * 1024:
                    raise ValueError("staged source could not be verified")
                files[name] = path.read_bytes()
        return files
