"""Arithmetic over an AST allowlist.

`eval()` on model-generated input is remote code execution with extra steps:
`__import__('os').system(...)` is a valid Python expression. This walks the AST
and rejects any node type not on the allowlist, so there is no reachable path
to a name, an attribute or a call.
"""

from __future__ import annotations

import ast
import operator
from typing import Any

from langchain_core.tools import tool
from pydantic import BaseModel, Field

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

MAX_EXPONENT = 64  # 2**10**9 is a denial of service, not a calculation


class CalculatorArgs(BaseModel):
    expression: str = Field(
        description="Arithmetic expression, e.g. '(1240 - 980) / 980 * 100'. "
        "Numbers and + - * / // % ** and parentheses only.",
    )


def _eval(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError(f"only numbers are allowed, got {node.value!r}")
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > MAX_EXPONENT:
            raise ValueError(f"exponent above {MAX_EXPONENT} is rejected")
        if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right == 0:
            raise ValueError("division by zero")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval(node.operand))
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


@tool("calculator", args_schema=CalculatorArgs)
def calculator(expression: str) -> str:
    """Evaluate an arithmetic expression. Use for any number the answer depends on
    rather than computing it in your head, which is where models quietly go wrong."""
    if len(expression) > 200:
        raise ValueError("expression too long")
    value = _eval(ast.parse(expression, mode="eval"))
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{expression} = {value}"
