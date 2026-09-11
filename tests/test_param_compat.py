"""The loader/CLI parameter boundary must work without legacy Python imports."""
import argparse
from collections import UserDict
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("param_compat", Path(__file__).resolve().parents[1] /
                                             "firmwire/util/param.py")
param = importlib.util.module_from_spec(spec)
spec.loader.exec_module(param)


class ParamCompatibilityTests(unittest.TestCase):
    def validator(self):
        return param.ParamValidator("mtk_").build_params(UserDict({
            "cores": {"type": int, "default": 2},
            "mode": {"choices": ["native", "rehosted"], "default": "native"}}))

    def test_mapping_and_defaults(self):
        self.assertEqual(self.validator().parse_from_dict({}),
                         {"mtk_cores": 2, "mtk_mode": "native"})

    def test_explicit_values(self):
        self.assertEqual(self.validator().parse_from_dict({"cores": "4"})["mtk_cores"], 4)

    def test_invalid_value_raises_without_process_exit(self):
        with self.assertRaises(param.ParamValidationError):
            self.validator().parse_from_dict({"cores": "not-an-int"})

    def test_copy_to_cli_parser(self):
        parser = argparse.ArgumentParser()
        self.validator().copy_params_to_parser(parser)
        self.assertEqual(parser.parse_args(["--mtk_cores", "4"]).mtk_cores, 4)


if __name__ == "__main__":
    unittest.main()
