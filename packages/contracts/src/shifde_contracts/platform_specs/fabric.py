from typing import Literal

from pydantic import Field, model_validator

from shifde_contracts.common import Identifier, Model, RelativePath


class FabricItem(Model):
    key: Identifier
    item_type: Literal["Lakehouse", "Notebook", "Ontology"]
    definition_path: RelativePath
    depends_on: list[Identifier] = Field(default_factory=list)
    name_binding: Identifier | None = None
    lakehouse: Identifier | None = None


class FabricSolutionSpec(Model):
    solution_root: RelativePath = "."
    items: list[FabricItem] = Field(min_length=1, max_length=16)
    bindings: dict[str, str] = Field(default_factory=dict)
    workspace_binding: Literal["workspace"] = "workspace"
    verification_notebook: Identifier | None = None

    @model_validator(mode="after")
    def validate_graph(self):
        graph = {item.key: item.depends_on for item in self.items}
        if len(graph) != len(self.items):
            raise ValueError("item keys must be unique")
        visited, visiting = set(), set()

        def visit(key: str):
            if key not in graph:
                raise ValueError(f"unknown dependency: {key}")
            if key in visiting:
                raise ValueError("cyclic item dependencies")
            if key in visited:
                return
            visiting.add(key)
            for dependency in graph[key]:
                visit(dependency)
            visiting.remove(key)
            visited.add(key)

        for key in graph:
            visit(key)
        by_key = {item.key: item for item in self.items}
        paths = [item.definition_path for item in self.items]
        if (
            "." in paths
            or len(set(paths)) != len(paths)
            or any(a.startswith(b + "/") for a in paths for b in paths if a != b)
        ):
            raise ValueError("Fabric item definition paths must not overlap")
        if len({item.name_binding or item.key for item in self.items}) != len(self.items):
            raise ValueError("item name bindings must be unique")
        for item in self.items:
            if item.lakehouse and (
                item.item_type != "Notebook"
                or item.lakehouse not in item.depends_on
                or by_key[item.lakehouse].item_type != "Lakehouse"
            ):
                raise ValueError("notebook lakehouse must be a declared Lakehouse dependency")
        if self.verification_notebook and (
            self.verification_notebook not in by_key
            or by_key[self.verification_notebook].item_type != "Notebook"
        ):
            raise ValueError("verification_notebook must identify a declared Notebook")
        return self


class FabricApplicationSpec(FabricSolutionSpec):
    kind: Literal["fabric.solution/v1"] = "fabric.solution/v1"

    @model_validator(mode="after")
    def supported_items(self):
        if any(item.item_type == "Ontology" for item in self.items):
            raise ValueError("Ontology requires fabric.iq/v1")
        return self


class FabricIQApplicationSpec(FabricSolutionSpec):
    kind: Literal["fabric.iq/v1"] = "fabric.iq/v1"

    @model_validator(mode="after")
    def requires_ontology(self):
        if not any(item.item_type == "Ontology" for item in self.items):
            raise ValueError("Fabric IQ requires at least one Ontology")
        return self
