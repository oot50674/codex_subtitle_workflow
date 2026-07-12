import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "subflow.py"
SPEC = importlib.util.spec_from_file_location("subflow", MODULE_PATH)
assert SPEC and SPEC.loader
SUBFLOW = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SUBFLOW
SPEC.loader.exec_module(SUBFLOW)


class LineWrappingTests(unittest.TestCase):
    def test_split_lines_hard_limit_balances_long_text_into_two_lines(self):
        text = "one two three four five six seven eight nine ten eleven twelve"
        wrapped = SUBFLOW.split_lines(text, 12, max_lines=2)

        self.assertEqual(wrapped.count("\n"), 1)
        self.assertEqual(wrapped.replace("\n", " "), text)

    def test_split_lines_hard_limit_collapses_existing_line_breaks(self):
        text = "첫 번째 문장입니다.\n두 번째 문장입니다.\n세 번째 문장입니다."
        wrapped = SUBFLOW.split_lines(text, 10, max_lines=2)

        self.assertEqual(len(wrapped.splitlines()), 2)
        self.assertEqual(wrapped.replace("\n", " "), text.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
