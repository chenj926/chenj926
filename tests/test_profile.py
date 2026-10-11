import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("profile", Path(__file__).resolve().parents[1] / "scripts/update_profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class NotebookTests(unittest.TestCase):
    def test_outputs_markdown_and_metadata_have_no_weight(self):
        nb = {"metadata": {"language_info": {"name": "python"}, "large": "x"*100000}, "cells": [
            {"cell_type": "markdown", "source": ["# A very long report"*1000]},
            {"cell_type": "code", "source": ["print('hello')\n"],
             "outputs": [{"data": {"image/png": "A"*1000000}}]},
        ]}
        self.assertEqual(profile.notebook_source(nb), ("Python", len("print('hello')\n".encode()), 1))

    def test_unicode_source_is_measured_in_utf8_bytes(self):
        source = "name = '你好'\n\nprint(name)\n"
        nb = {"metadata": {"kernelspec": {"language": "python"}},
              "cells": [{"cell_type": "code", "source": source}]}
        self.assertEqual(profile.notebook_source(nb), ("Python", len(source.encode()), 2))

    def test_r_kernel_and_legacy_notebook(self):
        nb = {"metadata": {"language_info": {"name": "R"}}, "worksheets": [
            {"cells": [{"cell_type": "code", "input": ["x <- 1\n"]}]}]}
        self.assertEqual(profile.notebook_source(nb), ("R", 7, 1))

    def test_unknown_kernel_is_not_guessed_as_python(self):
        self.assertEqual(profile.notebook_source({"cells": [{"cell_type": "code", "source": "1+1"}]}),
                         ("Jupyter Notebook", 3, 1))


class ContributionTests(unittest.TestCase):
    def test_accessible_tooltips_supply_actual_counts(self):
        parser = profile.ContributionParser()
        parser.feed('<td id="a" data-date="2026-10-09" data-level="0"></td>'
                    '<tool-tip for="a">No contributions on October 9th.</tool-tip>'
                    '<td id="b" data-date="2026-10-10" data-level="4"></td>'
                    '<tool-tip for="b">1,234 contributions on October 10th.</tool-tip>')
        self.assertEqual([day["count"] for day in parser.days()], [0, 1234])

    def test_missing_tooltip_fails_instead_of_fabricating_counts(self):
        parser = profile.ContributionParser()
        parser.feed('<td id="a" data-date="2026-10-09" data-level="2"></td>')
        with self.assertRaises(ValueError):
            parser.days()

    def test_duplicate_dates_fail(self):
        parser = profile.ContributionParser()
        parser.feed('<td id="a" data-date="2026-10-09" data-level="0"></td><tool-tip for="a">No contributions</tool-tip>'
                    '<td id="b" data-date="2026-10-09" data-level="0"></td><tool-tip for="b">No contributions</tool-tip>')
        with self.assertRaises(ValueError):
            parser.days()


if __name__ == "__main__":
    unittest.main()
