import importlib.util
from datetime import date, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

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


class ContributionCalendarTests(unittest.TestCase):
    def fetch_calendar(self, length, *, first=date(2025, 10, 12), omit=None, today=None):
        last = first + timedelta(days=length - 1)
        html = "".join(
            f'<td id="day-{i}" data-date="{first + timedelta(days=i)}" data-level="0"></td>'
            f'<tool-tip for="day-{i}">No contributions</tool-tip>'
            for i in range(length) if i != omit
        ).encode()
        with patch.object(profile, "get", return_value=html) as get, \
                patch.object(profile, "date", wraps=date) as clock:
            clock.today.return_value = today if today is not None else last
            days = profile.contribution_days("chenj926")
            get.assert_called_once_with("https://github.com/users/chenj926/contributions")
            return days

    def test_native_window_accepts_every_day_of_the_week(self):
        # 52 complete weeks plus Sunday through the current weekday.
        for length in range(365, 372):
            with self.subTest(length=length):
                days = self.fetch_calendar(length)
                self.assertEqual(len(days), length)
                self.assertEqual(days[0]["date"], "2025-10-12")
                self.assertEqual(days[-1]["date"], str(date(2025, 10, 12) + timedelta(days=length - 1)))
                self.assertTrue(all(day["count"] == 0 for day in days))

    def test_existing_lower_bound_is_preserved(self):
        self.assertEqual(len(self.fetch_calendar(364)), 364)

    def test_out_of_range_windows_fail(self):
        for length in (363, 372):
            with self.subTest(length=length), self.assertRaisesRegex(ValueError, "complete one-year"):
                self.fetch_calendar(length)

    def test_missing_days_still_fail(self):
        for omitted in (1, 184, 367):
            with self.subTest(omitted=omitted), self.assertRaisesRegex(ValueError, "complete one-year"):
                self.fetch_calendar(369, omit=omitted)

    def test_freshness_tolerance_is_preserved(self):
        last = date(2026, 10, 15)
        for offset in (-2, 0, 2):
            with self.subTest(offset=offset):
                self.assertEqual(len(self.fetch_calendar(369, today=last + timedelta(days=offset))), 369)

    def test_stale_or_future_windows_still_fail(self):
        last = date(2026, 10, 15)
        for offset in (-3, 3):
            with self.subTest(offset=offset), self.assertRaisesRegex(ValueError, "stale contribution"):
                self.fetch_calendar(369, today=last + timedelta(days=offset))

    def test_window_crossing_leap_day_is_contiguous(self):
        days = self.fetch_calendar(369, first=date(2023, 10, 15))
        self.assertEqual(len(days), 369)
        self.assertIn("2024-02-29", [day["date"] for day in days])


if __name__ == "__main__":
    unittest.main()

