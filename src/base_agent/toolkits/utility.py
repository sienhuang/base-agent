"""Small deterministic tools that require no external infrastructure."""

import ast
import operator
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base_agent.tools import FunctionTool, tool

type Number = int | float
type BinaryOperation = Callable[[Number, Number], Number]
type UnaryOperation = Callable[[Number], Number]

_BINARY_OPERATIONS: dict[type[ast.operator], BinaryOperation] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATIONS: dict[type[ast.unaryop], UnaryOperation] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_MAX_EXPRESSION_LENGTH = 200
_MAX_ABSOLUTE_VALUE = 10**100
_MAX_EXPONENT = 100


def utility_tools() -> tuple[FunctionTool, ...]:
    """Build safe date/time and arithmetic tools."""

    @tool
    def current_datetime(timezone: str = "UTC") -> dict[str, str]:
        """Return the current date and time in an IANA timezone such as Asia/Shanghai."""
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {timezone}") from exc
        value = datetime.now(zone)
        return {
            "timezone": timezone,
            "iso8601": value.isoformat(),
            "date": value.date().isoformat(),
            "time": value.time().isoformat(timespec="seconds"),
        }

    @tool
    def calculate(expression: str) -> dict[str, str | Number]:
        """Evaluate bounded arithmetic using numbers, parentheses, and + - * / // % **."""
        if not expression.strip():
            raise ValueError("expression must not be blank")
        if len(expression) > _MAX_EXPRESSION_LENGTH:
            raise ValueError(
                f"expression exceeds {_MAX_EXPRESSION_LENGTH} characters"
            )
        try:
            tree = ast.parse(expression, mode="eval")
        except SyntaxError as exc:
            raise ValueError("invalid arithmetic expression") from exc
        result = _evaluate(tree.body)
        return {"expression": expression, "result": result}

    return current_datetime, calculate


def _evaluate(node: ast.expr) -> Number:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("only integer and floating-point constants are allowed")
        return _bounded(value)
    if isinstance(node, ast.UnaryOp):
        unary_operation = _UNARY_OPERATIONS.get(type(node.op))
        if unary_operation is None:
            raise ValueError("unsupported unary operator")
        return _bounded(unary_operation(_evaluate(node.operand)))
    if isinstance(node, ast.BinOp):
        binary_operation = _BINARY_OPERATIONS.get(type(node.op))
        if binary_operation is None:
            raise ValueError("unsupported binary operator")
        left = _evaluate(node.left)
        right = _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ValueError(f"exponent must be between -{_MAX_EXPONENT} and {_MAX_EXPONENT}")
        try:
            return _bounded(binary_operation(left, right))
        except ZeroDivisionError as exc:
            raise ValueError("division by zero") from exc
        except OverflowError as exc:
            raise ValueError("arithmetic result is too large") from exc
    raise ValueError("expression contains unsupported syntax")


def _bounded(value: Number) -> Number:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("arithmetic result must be a real number")
    if abs(value) > _MAX_ABSOLUTE_VALUE:
        raise ValueError("arithmetic result is too large")
    return value
