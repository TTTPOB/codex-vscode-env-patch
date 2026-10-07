import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import patch_release


class HistoricalReleaseTests(unittest.TestCase):
    def setUp(self):
        self.old = {
            "version": "26.917.62051",
            "targetPlatform": "linux-x64",
            "properties": [],
        }
        self.new = dict(self.old, version="26.1002.51308")

    def test_explicit_version_selects_history(self):
        self.assertEqual(
            patch_release.select_versions([self.new, self.old], self.old["version"], "stable"),
            [self.old],
        )
        self.assertEqual(
            patch_release.select_versions([self.new, self.old], "", "stable"), [self.new]
        )
        with self.assertRaisesRegex(RuntimeError, "No Marketplace version"):
            patch_release.select_versions([self.new], self.old["version"], "stable")

    def test_history_plan_and_publish_leave_latest_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            plan_file = directory / "plan.json"
            with patch.object(patch_release, "existing_release", return_value=None):
                patch_release.prepare([self.old], "owner/repo", plan_file, latest=False)
            self.assertFalse(json.loads(plan_file.read_text())["latest"])
            name = patch_release.asset_name(self.old)
            vsix = directory / name
            vsix.touch()
            vsix.with_suffix(".json").write_text(json.dumps({
                "asset": name, "patched_pairs": 1, "source": "https://example.test/source"
            }))
            with patch.object(patch_release, "existing_release", return_value=None), \
                    patch.object(patch_release, "gh") as gh:
                patch_release.publish([self.old], "owner/repo", directory, latest=False)
            edits = [call.args for call in gh.call_args_list if call.args[:2] == ("release", "edit")]
            self.assertEqual(len(edits), 1)
            self.assertIn("--latest=false", edits[0])


if __name__ == "__main__":
    unittest.main()
