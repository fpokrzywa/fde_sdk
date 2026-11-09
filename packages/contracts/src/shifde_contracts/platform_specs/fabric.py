from typing import Literal

from pydantic import Field, model_validator

from shifde_contracts.common import Identifier, Model, RelativePath


class FabricItem(Model):
    key: Identifier
    item_type: Literal["Lakehouse", "Notebook"]
    definition_path: RelativePath
    depends_on: list[Identifier] = Field(default_factory=list)


class FabricApplicationSpec(Model):
    kind: Literal["fabric.solution/v1"] = "fabric.solution/v1"
    solution_root: RelativePath = "."
    items: list[FabricItem] = Field(min_length=1)
    bindings: dict[str, str] = Field(default_factory=dict)

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
        return self
