"""Deterministic vendor boundary, also served to test-only isolated worker images."""

import copy
import json
import re


class FakeSnowflake:
    def __init__(self):
        self.packages, self.applications, self.stages, self.versions, self.files = (
            {},
            {},
            {},
            {},
            {},
        )
        self.grants, self.mutations = set(), []
        self.fail_validation = self.fail_after_install = self.reject_upgrade = False
        self.health_ok = self.ui_ok = True
        self.counter = 0
        self.sql = []

    def created(self):
        self.counter += 1
        return f"2026-11-10T00:00:{self.counter:02d}+00:00"

    async def snapshot(self, names):
        return copy.deepcopy(
            dict(
                account="TEST_ACCOUNT",
                role="DEPLOY_ROLE",
                package=self.packages.get(names["package"]),
                application=self.applications.get(names["application"]),
                stage=self.stages.get(names["package"]),
                versions=self.versions.get(names["package"], []),
            )
        )

    async def execute(self, sql, *, warehouse=None):
        self.sql.append(sql)
        quoted = re.findall(r'"([^\"]+)"', sql)
        rows = [{"status": "Statement executed successfully"}]
        if sql.startswith("SELECT CURRENT_ACCOUNT()"):
            rows = [{"account": "TEST_ACCOUNT", "role": "DEPLOY_ROLE"}]
        elif sql.startswith("CREATE APPLICATION PACKAGE"):
            package = quoted[0]
            if package in self.packages:
                raise RuntimeError("package already exists")
            self.packages[package] = dict(
                name=package,
                created_on=self.created(),
                owner="DEPLOY_ROLE",
                comment="managed",
                distribution="INTERNAL",
            )
            self.mutations.append(("package", package))
        elif sql.startswith("CREATE SCHEMA"):
            self.mutations.append(("schema", quoted[0]))
        elif sql.startswith("CREATE STAGE"):
            package = quoted[0]
            self.stages.setdefault(
                package,
                dict(
                    name="RELEASES",
                    created_on=self.created(),
                    owner="DEPLOY_ROLE",
                    type="INTERNAL",
                    schema_name="SHIFDE",
                ),
            )
            self.mutations.append(("stage", package))
        elif sql.startswith("LIST "):
            location = sql[5:]
            rows = [{"name": location + "/" + name} for name in self.files.get(location, {})]
        elif sql.startswith("CALL SYSTEM$VALIDATE_NATIVE_APP_SETUP"):
            rows = [
                {
                    "validation": json.dumps(
                        {
                            "status": "FAIL" if self.fail_validation else "SUCCESS",
                            "errors": ["invalid setup"] if self.fail_validation else [],
                        }
                    )
                }
            ]
            self.mutations.append(("validate", quoted[0] if quoted else ""))
        elif sql.startswith("ALTER APPLICATION PACKAGE"):
            package, version = quoted[:2]
            patch = int(re.search(r"ADD PATCH (\d+)", sql)[1]) if "ADD PATCH" in sql else 0
            if any(
                v["version"] == version and v["patch"] == patch
                for v in self.versions.get(package, [])
            ):
                raise RuntimeError("immutable version already exists")
            label = re.search(r"LABEL = '([^']+)'", sql)[1]
            self.versions.setdefault(package, []).append(
                dict(
                    version=version,
                    patch=patch,
                    label=label,
                    created_on=self.created(),
                    dropped_on=None,
                    state="READY",
                )
            )
            self.mutations.append(("version", package, version, patch))
        elif sql.startswith("CREATE APPLICATION ") or sql.startswith("ALTER APPLICATION "):
            app_name = quoted[0]
            create = sql.startswith("CREATE")
            if create:
                if app_name in self.applications:
                    raise RuntimeError("application already exists")
                package, version = quoted[1:3]
            else:
                if self.reject_upgrade:
                    raise RuntimeError("incompatible upgrade")
                package = self.applications[app_name]["source"]
                version = quoted[1]
            patch = int(re.search(r"PATCH (\d+)", sql)[1])
            release = next(
                v for v in self.versions[package] if v["version"] == version and v["patch"] == patch
            )
            app = self.applications.setdefault(
                app_name,
                dict(
                    name=app_name,
                    created_on=self.created(),
                    source=package,
                    source_type="APPLICATION PACKAGE",
                    owner="DEPLOY_ROLE",
                    comment="managed",
                ),
            )
            app.update(
                version=version,
                patch=patch,
                label=release["label"],
                upgrade_state="COMPLETE",
                disablement_reasons=None,
            )
            self.mutations.append(("install" if create else "upgrade", app_name, version, patch))
            if self.fail_after_install:
                raise TimeoutError("connection lost after native acceptance")
        elif sql.startswith("GRANT APPLICATION ROLE"):
            self.grants.add((quoted[0], quoted[-1]))
            self.mutations.append(("grant", quoted[0]))
        elif sql.startswith("SELECT ") and "HEALTH" in sql:
            if not warehouse or (quoted[0], "DEPLOY_ROLE") not in self.grants:
                raise PermissionError("runtime warehouse or application role unavailable")
            rows = [{"status": "OK" if self.health_ok else "FAIL"}]
        elif sql.startswith("SHOW STREAMLITS"):
            rows = [{"name": "UI"}] if self.ui_ok else []
        else:
            raise AssertionError("unrecognized generated SQL: " + sql)
        return {"rows": rows, "query_id": f"query-{self.counter}"}

    async def upload(self, files, location):
        if self.files.get(location):
            raise AssertionError("upload must not overwrite a stage prefix")
        self.files[location] = dict(files)
        self.mutations.append(("upload", location))

    async def download(self, names, location):
        return {name: self.files[location][name] for name in names}
