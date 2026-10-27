import pytest

from app.calculator import CALCULATOR_TOOL, CalculatorError, calculate, validate_currency_expression


def test_decimal_arithmetic_and_half_up():
    assert calculate("0.1 + 0.2")["value"] == "0.30"
    assert calculate("2.675")["value"] == "2.68"
    assert calculate("-2.675")["value"] == "-2.68"
    assert calculate("-0.001")["value"] == "0.00"


def test_business_compensation_cap_and_percentage_change():
    result = calculate("2000000 * min(10 * 2, 10) / 100", 0, "تومان")
    assert result == {"expression": "2000000 * min(10 * 2, 10) / 100", "value": "200000", "unit": "تومان", "precision": 0}
    assert calculate("(709010250 - 819708000) / 819708000 * 100", 2, "درصد")["value"] == "-13.50"


def test_functions_persian_digits_and_bounded_integer_powers():
    assert calculate("max(abs(-۲), min(۳, ۴)) + ۲ ** -۲", 2)["value"] == "3.25"
    assert calculate("1e3 + 1_000", 0)["value"] == "2000"
    assert calculate("-7 // 3", 0)["value"] == "-3"
    assert calculate("-7 % 3", 0)["value"] == "2"


@pytest.mark.parametrize("expression", ["__import__('os').system('id')", "(1).__class__", "[x for x in [1]]", "sum([1,2])", "True + 1", "'1'", "1 << 2", "min(x=1)", "lambda: 1", "x", "2j"])
def test_rejects_executable_or_non_numeric_expressions(expression):
    with pytest.raises(CalculatorError):
        calculate(expression)


@pytest.mark.parametrize("expression", ["1/0", "1//0", "1%0", "0**-1"])
def test_zero_division_is_controlled(expression):
    with pytest.raises(CalculatorError) as error:
        calculate(expression)
    assert error.value.code == "division_by_zero"
    assert error.value.message


@pytest.mark.parametrize("expression", ["2**1000000", "2**0.5", "1e25", "1e-101", "10**12 * 10**12 * 10", "(" * 300 + "1" + ")" * 300, "+".join(["1"] * 300), "1" * 1001])
def test_resource_and_magnitude_limits(expression):
    with pytest.raises(CalculatorError):
        calculate(expression)


@pytest.mark.parametrize("precision", [True, -1, 9, 2.5, "2"])
def test_precision_validation(precision):
    with pytest.raises(CalculatorError) as error:
        calculate("1", precision)
    assert error.value.code == "invalid_precision"


def test_tool_contract_matches_function_interface():
    function = CALCULATOR_TOOL["function"]
    assert function["name"] == "calculate"
    assert function["strict"] is True
    assert set(function["parameters"]["required"]) == {"expression", "precision", "unit"}
    assert function["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("expression,unit,extra", [
    ("(10800000+10800000)/10", "تومان", {}),
    ("2*(10800000+10800000)/10", "IRT", {}),
    ("10800000+10800000", "ریال", {}),
    ("501/10", "تومان", {}),
    ("(10800000-501)/501*100", "درصد", {}),
    ("20900000-2160000", "تومان", {"prior_results": [{"value": "20900000", "unit": "IRT"}, {"value": "2160000", "unit": "تومان"}]}),
    ("25000000-(10800000+10800000)/10", "تومان", {"budget_amount": 25000000, "budget_currency": "IRT"}),
])
def test_known_currency_dimensions_accept_conversion_scalars_percent_and_prior_outputs(expression, unit, extra):
    validate_currency_expression(expression, unit, irr_values=[10800000, 501], scalar_values=[2], **extra)
    if expression == "501/10":
        assert calculate(expression, 1, unit)["value"] == "50.1"


@pytest.mark.parametrize("expression,unit,extra", [
    ("10800000+10800000", "تومان", {}),
    ("2*10800000", "IRT", {}),
    ("25000000-10800000", "تومان", {"budget_amount": 25000000, "budget_currency": "IRT"}),
    ("10800000+10800000", "", {"requested_currency": "IRT"}),
])
def test_provable_known_currency_mismatch_is_rejected_without_rewriting(expression, unit, extra):
    with pytest.raises(CalculatorError) as caught:
        validate_currency_expression(expression, unit, irr_values=[10800000], scalar_values=[2], **extra)
    assert caught.value.code == "currency_unit_mismatch"
    assert "/10" not in expression  # no automatic rewrite occurred


def test_currency_guard_is_conservative_for_unknown_and_ambiguous_money_values():
    validate_currency_expression("12345+7", "تومان", irr_values=[])
    validate_currency_expression("10800000", "تومان", irr_values=[10800000],
        prior_results=[{"value": "10800000", "unit": "تومان"}])
