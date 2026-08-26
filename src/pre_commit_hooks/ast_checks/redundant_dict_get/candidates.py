from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Candidate:
    call: ast.Call
    receiver: str
    literal_key: str | None
    name_key: str | None

    @property
    def replacement_key(self) -> str:
        return repr(self.literal_key) if self.literal_key is not None else self.name_key or ""


def find_candidates(tree: ast.Module) -> list[Candidate]:
    return [candidate for node in ast.walk(tree) if (candidate := _candidate(node)) is not None]


def _candidate(node: ast.AST) -> Candidate | None:
    if not isinstance(node, ast.Call) or node.keywords or len(node.args) != 1:
        return None
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "get":
        return None
    if not isinstance(node.func.value, ast.Name):
        return None
    key = node.args[0]
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return Candidate(call=node, receiver=node.func.value.id, literal_key=key.value, name_key=None)
    if isinstance(key, ast.Name) and isinstance(key.ctx, ast.Load):
        return Candidate(call=node, receiver=node.func.value.id, literal_key=None, name_key=key.id)
    return None
