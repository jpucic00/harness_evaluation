"""Reference cases for the template task: the visible tests plus hidden ones.

Never copied into the environment. The scorer runs each case's code against the
agent's workspace and compares the result with ``expected`` on the host, so the
agent cannot see or forge the expected values. The file name must end in _cases.py.
"""

from agenteval.suites.reference_cases import Case, Raises

SETUP = """
from clamp import clamp
"""

CASES = [
    # visible tests (same as repo/tests)
    Case("inside_range", "clamp(5, 0, 10)", 5),
    Case("below_range", "clamp(-3, 0, 10)", 0),
    # hidden tests: the ones that actually catch the bug and its edge cases
    Case("above_range", "clamp(42, 0, 10)", 10),
    Case("at_upper_bound", "clamp(10, 0, 10)", 10),
    Case("floats", "clamp(2.5, 0.0, 1.5)", 1.5),
    Case("empty_range", "clamp(3, 7, 7)", 7),
    Case("negative_range", "clamp(-50, -10, -5)", -10),
    Case("negative_above", "clamp(0, -10, -5)", -5),
    Case("many_values", "[clamp(v, 1, 3) for v in range(6)]", [1, 1, 2, 3, 3, 3]),
    Case("inverted_range", "clamp(1, 5, 0)", Raises("ValueError")),
]
