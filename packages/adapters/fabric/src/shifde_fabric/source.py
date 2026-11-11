"""Offline, immutable source packaging and explicit environment bindings."""

import base64
import copy
import json
import re
from typing import Any
from uuid import UUID, uuid5

from pydantic import TypeAdapter
from shifde_contracts.common import RelativePath
from shifde_core.hashing import digest
from shifde_core.native import FABRIC_PROFILE

ADAPTER_VERSION = "0.1.0"
TOOLCHAIN_DIGEST = digest(FABRIC_PROFILE)
ORIGIN = "https://api.fabric.microsoft.com"
COLLECTIONS = {"Lakehouse": "lakehouses", "Notebook": "notebooks", "Ontology": "ontologies"}


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def ordered(spec):
    result, visited = [], set()
    by_key = {item.key: item for item in spec.items}

    def visit(item):
        if item.key in visited:
            return
        for key in item.depends_on:
            visit(by_key[key])
        result.append(item)
        visited.add(item.key)

    for item in spec.items:
        visit(item)
    return result


def root(spec):
    return "" if spec.solution_root == "." else spec.solution_root + "/"


def scaffold_files(spec):
    files = {}
    for item in spec.items:
        path = root(spec) + item.definition_path
        if item.item_type == "Lakehouse":
            files[path] = json_bytes(
                {"description": "Managed Fabric Lakehouse", "enableSchemas": True}
            )
        elif item.item_type == "Notebook":
            files[path] = json_bytes(
                {
                    "nbformat": 4,
                    "nbformat_minor": 5,
                    "metadata": {"language_info": {"name": "python"}},
                    "cells": [
                        {
                            "cell_type": "code",
                            "execution_count": None,
                            "outputs": [],
                            "metadata": {},
                            "source": ["assert spark.range(1).count() == 1\n"],
                        }
                    ],
                }
            )
        else:
            files[path + "/database.tmdl"] = b"database Ontology\n\tcompatibilityLevel: 1000000\n"
            files[path + "/model.tmdl"] = b"model Ontology\n\nref namespace default\n"
            files[path + "/namespaces/default.tmdl"] = b"namespace default\n\tlineageTag: default\n"
    return files


def validate_source(files, spec):
    allowed = set()
    for item in spec.items:
        path = root(spec) + item.definition_path
        if item.item_type == "Ontology":
            parts = {
                name[len(path) + 1 :]: data
                for name, data in files.items()
                if name.startswith(path + "/")
            }
            if not {"database.tmdl", "model.tmdl", "namespaces/default.tmdl"} <= parts.keys():
                raise ValueError("ontology requires generation-2 TMDL definition parts")
            for name, data in parts.items():
                TypeAdapter(RelativePath).validate_python(name)
                if not name.endswith(".tmdl") or not data.decode("utf-8").strip():
                    raise ValueError("unsupported ontology definition part")
                allowed_tokens = {"{{fabric.workspace.id}}"} | {
                    "{{fabric.item." + key + ".id}}" for key in item.depends_on
                }
                remaining = data.decode("utf-8")
                for token in allowed_tokens:
                    remaining = remaining.replace(token, "")
                if "{{fabric" in remaining:
                    raise ValueError("unresolved ontology reference syntax")
                if re.search(
                    r"(?m)^\s*(?:type:\s*(?:explicit|Direct|singleColumn)|propertyScope:\s*none)\s*$",
                    data.decode("utf-8"),
                ):
                    raise ValueError("omit service-elided TMDL defaults in this source profile")
                if name.startswith("rules/") and re.search(
                    r"(?m)^\s*(?:///|synonym\b)", data.decode("utf-8")
                ):
                    raise ValueError("rule descriptions/synonyms are not preserved by Fabric")
                if any(
                    token not in allowed_tokens
                    for token in re.findall(r"\{\{fabric[^\n]*?\}\}", data.decode("utf-8"))
                ):
                    raise ValueError("ontology reference must identify a declared dependency")
                allowed.add(path + "/" + name)
        else:
            doc = json.loads(files[path])
            if not isinstance(doc, dict):
                raise ValueError("definition must be a JSON object")
            if item.item_type == "Lakehouse":
                if (
                    set(doc) - {"description", "enableSchemas"}
                    or not isinstance(doc.get("description", ""), str)
                    or len(doc.get("description", "")) > 256
                    or not isinstance(doc.get("enableSchemas", True), bool)
                ):
                    raise ValueError("unsupported Lakehouse definition")
            elif (
                not isinstance(doc, dict)
                or doc.get("nbformat") != 4
                or not isinstance(doc.get("metadata"), dict)
                or not isinstance(doc.get("cells"), list)
            ):
                raise ValueError("invalid ipynb definition")
            if (
                item.item_type == "Notebook"
                and "dependencies" in doc["metadata"]
                and not isinstance(doc["metadata"]["dependencies"], dict)
            ):
                raise ValueError("notebook dependencies must be an object")
            if item.item_type == "Notebook" and any(
                not isinstance(c, dict)
                or c.get("cell_type") not in {"code", "markdown", "raw"}
                or not isinstance(c.get("source"), (str, list))
                or isinstance(c["source"], list)
                and any(not isinstance(s, str) for s in c["source"])
                for c in doc["cells"]
            ):
                raise ValueError("invalid notebook cells")
            allowed.add(path)
    if set(files) != allowed:
        raise ValueError("source contains unmanaged files or overlapping item paths")
    placeholder = "00000000-0000-4000-8000-000000000000"
    for item in spec.items:
        rendered = definition(
            item,
            spec,
            files,
            placeholder,
            {i.key: "Item" for i in spec.items},
            {i.key: placeholder for i in spec.items},
            UUID(placeholder),
        )
        if rendered:
            canonical_definition(rendered, item.item_type)
            if len(json_bytes(rendered)) > 16 * 1024 * 1024 - 4096:
                raise ValueError("Fabric definition exceeds supported transport budget")


def bindings(spec, target):
    if target.host != ORIGIN:
        raise ValueError("Fabric requires the canonical public-cloud API origin")
    workspace = str(UUID(target.bindings[spec.workspace_binding]))
    names = {item.key: target.bindings[item.name_binding or item.key] for item in spec.items}
    if len(set(names.values())) != len(names) or any(
        not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value)
        for value in names.values()
    ):
        raise ValueError("Fabric item display names must be distinct and bounded")
    for item in spec.items:
        if item.item_type in {"Ontology", "Lakehouse"} and not re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_]{0,98}", names[item.key]
        ):
            raise ValueError(
                "Fabric Ontology/Lakehouse names must be ASCII identifiers under 100 characters"
            )
    if spec.bindings:
        raise ValueError("legacy spec.bindings are unsupported; use target bindings")
    return workspace, names


def definition(item, spec, files, workspace, names, ids, application_id):
    path = root(spec) + item.definition_path
    if item.item_type == "Notebook":
        doc = copy.deepcopy(json.loads(files[path]))
        if item.lakehouse:
            doc["metadata"].setdefault("dependencies", {})["lakehouse"] = {
                "default_lakehouse": ids[item.lakehouse],
                "default_lakehouse_name": names[item.lakehouse],
                "default_lakehouse_workspace_id": workspace,
                "known_lakehouses": [{"id": ids[item.lakehouse]}],
            }
        return {"format": "ipynb", "parts": [part("notebook-content.ipynb", json_bytes(doc))]}
    if item.item_type == "Ontology":
        parts = []
        for name, data in sorted(files.items()):
            if name.startswith(path + "/"):
                text = data.decode("utf-8").replace("{{fabric.workspace.id}}", workspace)
                for key in item.depends_on:
                    text = text.replace("{{fabric.item." + key + ".id}}", ids[key])
                if "{{fabric" in text:
                    raise ValueError("unresolved ontology environment reference")
                parts.append(part(name[len(path) + 1 :], text.encode()))
        parts.append(
            part(
                ".platform",
                json_bytes(
                    {
                        "metadata": {"type": "Ontology", "displayName": names[item.key]},
                        "config": {
                            "version": "2.0",
                            "logicalId": str(uuid5(application_id, item.key)),
                        },
                    }
                ),
            )
        )
        return {"parts": parts}
    return None


def part(path, data):
    return {"path": path, "payloadType": "InlineBase64", "payload": base64.b64encode(data).decode()}


def canonical_definition(value, item_type):
    parts = value["parts"]
    if len(parts) > 1000 or len({p["path"] for p in parts}) != len(parts):
        raise ValueError("invalid native definition parts")
    result: dict[str, Any] = {}
    for p in parts:
        if p["path"] == ".platform":
            continue
        if p["payloadType"] != "InlineBase64":
            raise ValueError("unsupported native payload type")
        data = base64.b64decode(p["payload"], validate=True)
        if item_type == "Notebook":
            if "notebook" in result:
                raise ValueError("Notebook definition must contain exactly one content part")
            doc = json.loads(data)
            # Fabric may add execution outputs/cell IDs; compare authored cells and dependencies.
            result["notebook"] = {
                "cells": [
                    {
                        "cell_type": c["cell_type"],
                        "source": "".join(c["source"]),
                        "metadata": c.get("metadata", {}),
                    }
                    for c in doc["cells"]
                ],
                "lakehouse": doc.get("metadata", {}).get("dependencies", {}).get("lakehouse"),
                "dependencies": {
                    k: v
                    for k, v in doc.get("metadata", {}).get("dependencies", {}).items()
                    if v is not None
                },
                "kernelspec": doc.get("metadata", {}).get("kernelspec"),
                "kernel_info": doc.get("metadata", {}).get("kernel_info"),
                "language_info": doc.get("metadata", {}).get("language_info"),
            }
        else:
            result[p["path"]] = data.decode("utf-8").replace("\r\n", "\n").strip()
    if not result:
        raise ValueError("native definition content is missing")
    return digest(result)
