"""Safe arithmetic evaluation for verifying numerical answers.

Parses with ``ast`` and evaluates only numeric literals, arithmetic operators
and a whitelist of math functions/constants. No names, attributes, calls to
anything else, subscripts or comprehensions are allowed, and exponent sizes are
bounded, so model-generated expressions cannot execute code or hang the worker.
"""

from __future__ import annotations

import ast
import math
import operator

_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "log2": math.log2, "exp": math.exp,
          "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin, "acos": math.acos,
          "atan": math.atan, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil,
          "radians": math.radians, "degrees": math.degrees, "factorial": math.factorial, "min": min, "max": max}
_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


class UnsafeExpression(ValueError):
    pass


def safe_eval(expr: str) -> float:
    if len(expr) > 500:
        raise UnsafeExpression("Expression too long")
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpression(f"Invalid expression: {exc.msg}") from exc
    return float(_eval(tree.body, 0))


def _eval(node: ast.AST, depth: int) -> float:
    if depth > 50:
        raise UnsafeExpression("Expression too deep")
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _eval(node.left, depth + 1), _eval(node.right, depth + 1)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e12):
            raise UnsafeExpression("Exponent too large")
        return _BIN[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand, depth + 1))
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS and not node.keywords:
        args = [_eval(a, depth + 1) for a in node.args]
        if node.func.id == "factorial" and (args[0] > 170 or args[0] != int(args[0])):
            raise UnsafeExpression("factorial argument too large or not integral")
        return _FUNCS[node.func.id](*args)
    raise UnsafeExpression(f"Disallowed syntax: {type(node).__name__}")


def numbers_match(a: float, b: float, tolerance: float | None = None) -> bool:
    tol = tolerance if tolerance is not None else max(1e-6, abs(b) * 0.01)
    return abs(a - b) <= tol
