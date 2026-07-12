import unittest

import subflow


class VerifyHeuristicsTests(unittest.TestCase):
    def test_repeated_micro_cues_fail_verification(self):
        cues = [
            subflow.Cue(1, 0, 1000, "See you next time."),
            subflow.Cue(2, 1000, 1020, "See you next time."),
            subflow.Cue(3, 1020, 1040, "See you next time."),
        ]

        result = subflow.inspect_output(
            cues,
            media_duration_ms=2000,
            mode="source",
            target_language="ko",
        )

        self.assertIn(
            "suspicious_repeated_micro_cues",
            {error["code"] for error in result["errors"]},
        )

    def test_normal_repeated_cues_do_not_trigger(self):
        cues = [
            subflow.Cue(1, 0, 1000, "Again."),
            subflow.Cue(2, 1000, 2000, "Again."),
            subflow.Cue(3, 2000, 3000, "Again."),
        ]

        result = subflow.inspect_output(
            cues,
            media_duration_ms=3000,
            mode="source",
            target_language="ko",
        )

        self.assertNotIn(
            "suspicious_repeated_micro_cues",
            {error["code"] for error in result["errors"]},
        )
