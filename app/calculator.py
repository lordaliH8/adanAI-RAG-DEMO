"""Bounded, deterministic Decimal arithmetic for the native calculator tool."""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DecimalException, ROUND_FLOOR, ROUND_HALF_UP, localcontext


MAX_EXPRESSION_LENGTH = 1000
MAX_AST_NODES = 200
MAX_AST_DEPTH = 24
MAX_MAGNITUDE = Decimal("1e24")
MAX_POWER = 12
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d{1,3}))?\Z", re.ASCII)
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


class CalculatorError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _reject(code: str, message: str):
    raise CalculatorError(code, message)


def _bounded(value: Decimal) -> Decimal:
    if not value.is_finite() or value.copy_abs() > MAX_MAGNITUDE:
        _reject("number_out_of_range", "بزرگی عدد از محدوده مجاز محاسبه خارج است.")
    if not value.is_zero() and value.adjusted() < -100:
        _reject("number_out_of_range", "عدد از محدوده دقت مجاز کوچک‌تر است.")
    return value


def calculate(expression: str, precision: int = 2, unit: str = "") -> dict:
    """Calculate arithmetic with + - * / // % **, min, max and abs only.

    Power accepts integer exponents between -12 and 12; each intermediate value
    is bounded by 1e24. Precision selects decimal places (0–8). Decimal work is
    performed at 60 significant digits and rounded only at the final boundary.
    """
    if not isinstance(expression, str) or not expression.strip():
        _reject("invalid_expression", "عبارت محاسبه باید متن غیرخالی باشد.")
    if len(expression) > MAX_EXPRESSION_LENGTH:
        _reject("expression_too_complex", "عبارت محاسبه بیش از حد طولانی است.")
    if isinstance(precision, bool) or not isinstance(precision, int) or not 0 <= precision <= 8:
        _reject("invalid_precision", "تعداد رقم اعشار باید عدد صحیح بین صفر و هشت باشد.")
    if not isinstance(unit, str) or len(unit) > 40 or any(ord(c) < 32 for c in unit):
        _reject("invalid_unit", "واحد محاسبه معتبر نیست.")
    original = expression.strip()
    normalized = original.translate(_DIGITS)
    try:
        tree = ast.parse(normalized, mode="eval")
    except (SyntaxError, ValueError, RecursionError):
        _reject("invalid_expression", "ساختار عبارت محاسبه معتبر نیست.")
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        _reject("expression_too_complex", "عبارت محاسبه بیش از حد پیچیده است.")

    def evaluate(node: ast.AST, depth: int = 0) -> Decimal:
        if depth > MAX_AST_DEPTH:
            _reject("expression_too_complex", "تو در تویی عبارت محاسبه بیش از حد است.")
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                _reject("unsupported_expression", "فقط مقدار عددی در محاسبه مجاز است.")
            literal = (ast.get_source_segment(normalized, node) or "").replace("_", "")
            match = _NUMBER.fullmatch(literal)
            if not match or len(re.sub(r"\D", "", literal.split("e")[0].split("E")[0])) > 80:
                _reject("number_out_of_range", "قالب یا طول عدد در محدوده مجاز نیست.")
            if match.group(1) and abs(int(match.group(1))) > 100:
                _reject("number_out_of_range", "توان نمایش عدد از محدوده مجاز خارج است.")
            return _bounded(Decimal(literal))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand, depth + 1)
            return _bounded(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)):
                _reject("unsupported_expression", "این عملگر در ابزار محاسبه مجاز نیست.")
            left, right = evaluate(node.left, depth + 1), evaluate(node.right, depth + 1)
            if isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)) and right.is_zero():
                _reject("division_by_zero", "تقسیم بر صفر امکان‌پذیر نیست.")
            if isinstance(node.op, ast.Add):
                result = left + right
            elif isinstance(node.op, ast.Sub):
                result = left - right
            elif isinstance(node.op, ast.Mult):
                result = left * right
            elif isinstance(node.op, ast.Div):
                result = left / right
            elif isinstance(node.op, (ast.FloorDiv, ast.Mod)):
                quotient = (left / right).to_integral_value(rounding=ROUND_FLOOR)
                result = quotient if isinstance(node.op, ast.FloorDiv) else left - quotient * right
            else:
                if right != right.to_integral_value() or right.copy_abs() > MAX_POWER:
                    _reject("exponent_out_of_range", "توان باید عدد صحیح بین منفی دوازده و دوازده باشد.")
                if left.is_zero() and right < 0:
                    _reject("division_by_zero", "توان منفی برای صفر مجاز نیست.")
                if left.is_zero() and right.is_zero():
                    _reject("invalid_expression", "عبارت صفر به توان صفر در این ابزار تعریف نشده است.")
                result = left ** int(right)
            return _bounded(result)
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in {"min", "max", "abs"} or node.keywords:
                _reject("unsupported_expression", "فراخوانی این تابع در محاسبه مجاز نیست.")
            if not 1 <= len(node.args) <= 20 or (node.func.id == "abs" and len(node.args) != 1):
                _reject("invalid_arguments", "تعداد ورودی‌های تابع محاسبه معتبر نیست.")
            values = [evaluate(arg, depth + 1) for arg in node.args]
            result = values[0].copy_abs() if node.func.id == "abs" else (min(values) if node.func.id == "min" else max(values))
            return _bounded(result)
        _reject("unsupported_expression", "ساختار عبارت فقط باید شامل عدد، عملگر یا تابع مجاز باشد.")

    try:
        with localcontext() as context:
            context.prec = 60
            context.Emax = 100
            context.Emin = -100
            value = evaluate(tree.body)
            quantum = Decimal(1).scaleb(-precision)
            result = value.quantize(quantum, rounding=ROUND_HALF_UP)
            if result.is_zero():
                result = result.copy_abs()
            rendered = format(result, f".{precision}f")
    except CalculatorError:
        raise
    except (DecimalException, OverflowError, ValueError, RecursionError):
        _reject("arithmetic_error", "محاسبه در محدوده عددی مجاز قابل انجام نیست.")
    return {"expression": original, "value": rendered, "unit": unit.strip(), "precision": precision}


def validate_currency_expression(expression, unit, *, irr_values=(), prior_results=(),
                                 budget_amount=None, budget_currency=None, scalar_values=(), requested_currency=None):
    """Reject provable IRR/IRT label mismatches; never rewrite arithmetic.

    Unknown or ambiguous literals/operations are left to the generic calculator
    and grounding checks. This is a conservative unit check, not a price oracle.
    """
    def currency(value):
        label = str(value or "").strip().lower().replace("\u200c", "")
        return "IRR" if label in {"irr", "rial", "rials", "ریال"} else "IRT" if label in {"irt", "toman", "tomans", "تومان"} else None
    wanted = currency(unit) or (currency(requested_currency) if str(unit or "").strip() == "" else None)
    if not wanted or not isinstance(expression, str):
        return
    known = {}
    def number(value):
        if value is None or isinstance(value, bool):
            return None
        try:
            value = Decimal(str(value).translate(_DIGITS).replace(",", "").replace("٬", "").replace("٫", "."))
            return value if value.is_finite() else None
        except (DecimalException, ValueError):
            return None
    def register(value, dimension):
        value = number(value)
        if value is not None and dimension:
            known.setdefault(abs(value), set()).add(dimension)
    for value in irr_values:
        register(value, "IRR")
    for result in prior_results:
        register(result.get("value"), currency(result.get("unit")))
    register(budget_amount, currency(budget_currency))
    scalars = {Decimal(0), Decimal(1), Decimal(10), Decimal(100)}
    scalars.update(value for item in scalar_values if (value := number(item)) is not None)
    normalized = expression.translate(_DIGITS)
    try:
        tree = ast.parse(normalized, mode="eval")
        if len(list(ast.walk(tree))) > MAX_AST_NODES:
            return
    except (SyntaxError, ValueError, RecursionError):
        return  # calculate() owns syntax, complexity and arbitrary-code errors.
    def infer(node, depth=0):
        if depth > MAX_AST_DEPTH:
            return None, None
        if isinstance(node, ast.Constant) and not isinstance(node.value, bool) and isinstance(node.value, (int, float)):
            literal = (ast.get_source_segment(normalized, node) or "").replace("_", "")
            if len(literal) > 100:
                return None, None
            value = number(literal)
            dimensions = known.get(abs(value), set()) if value is not None else set()
            if len(dimensions) > 1 or (dimensions and value in scalars):
                return None, value
            return next(iter(dimensions), "scalar"), value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            dimension, value = infer(node.operand, depth+1)
            return dimension, -value if value is not None and isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, lv = infer(node.left, depth+1)
            right, rv = infer(node.right, depth+1)
            if "mixed" in (left, right):
                return "mixed", None
            value = None
            try:
                if lv is not None and rv is not None:
                    with localcontext() as context:
                        context.prec = 60
                        if isinstance(node.op, ast.Add): value = lv+rv
                        elif isinstance(node.op, ast.Sub): value = lv-rv
                        elif isinstance(node.op, ast.Mult): value = lv*rv
                        elif isinstance(node.op, ast.Div) and rv: value = lv/rv
                    if value is not None and (not value.is_finite() or abs(value) > MAX_MAGNITUDE): value = None
            except DecimalException:
                value = None
            if isinstance(node.op, (ast.Add, ast.Sub)):
                if left in {"IRR", "IRT"} and right in {"IRR", "IRT"} and left != right:
                    return "mixed", value
                if left == right: return left, value
                if rv == 0 and right == "scalar": return left, value
                if lv == 0 and left == "scalar": return right, value
            elif isinstance(node.op, ast.Mult):
                if left == "scalar": left, right, lv, rv = right, left, rv, lv
                if right == "scalar":
                    if left == "IRR" and rv == Decimal("0.1") and wanted == "IRT": return "IRT", value
                    if left == "IRT" and rv == 10 and wanted == "IRR": return "IRR", value
                    return left, value
            elif isinstance(node.op, ast.Div):
                if right == "scalar":
                    if left == "IRR" and rv == 10 and wanted == "IRT": return "IRT", value
                    return left, value
                if left == right and left in {"IRR", "IRT"}: return "scalar", value
            return None, value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"min", "max", "abs"} and not node.keywords:
            values = [infer(arg, depth+1) for arg in node.args]
            dimensions = {dimension for dimension, value in values if not (dimension == "scalar" and value == 0)}
            if "mixed" in dimensions or {"IRR", "IRT"}.issubset(dimensions): return "mixed", None
            return (next(iter(dimensions)), None) if len(dimensions) == 1 else (None, None)
        return None, None
    try:
        actual, _ = infer(tree.body)
    except (RecursionError, DecimalException, ValueError):
        return
    if actual == "mixed" or (actual in {"IRR", "IRT"} and actual != wanted):
        _reject("currency_unit_mismatch", "واحد عبارت با واحد خروجی سازگار نیست. ورودی ستون‌های *_irr ریال است؛ برای تومان عبارت کاملِ مبلغ ریالی را بر10 تقسیم کن، مانند (جمع مبلغ‌های ریالی)/10. نتایج قبلیِ تومان دوباره تبدیل نمی‌شوند؛ فقط برچسب واحد، تبدیل انجام نمی‌دهد.")


CALCULATOR_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "محاسبه قطعی با عدد اعشاری؛ + - * / // %، توان صحیح محدود و min/max/abs. برای درصد تغییر از (new-old)/old*100 استفاده کن. ورودی باید فقط عبارت عددی باشد. اعداد ستون‌های SQL با پسوند _irr ریال‌اند؛ برای خروجی تومان/IRT، کل عبارت ریالی را بر10 تقسیم کن. نتایج قبلی تومان، تومان باقی می‌مانند. ابزار تبدیل خودکار ندارد و ناسازگاری واحد شناخته‌شده را رد می‌کند.",
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "عبارت عددی با اعداد واقعی دریافت‌شده از SQL و قواعد سند"},
                "precision": {"type": "integer", "minimum": 0, "maximum": 8, "description": "تعداد رقم اعشار خروجی؛ اعشار واقعی تومان را حفظ کن"},
                "unit": {"type": "string", "description": "واحد خروجی، مانند تومان، درصد یا روز"},
            },
            "required": ["expression", "precision", "unit"],
            "additionalProperties": False,
        },
    },
}
