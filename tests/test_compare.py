import unittest

from scripts.compare import find_divergences


def result(scenario_id, sdk, status):
    return {
        "scenario_id": scenario_id,
        "sdk": sdk,
        "sdk_version": "test",
        "status": status,
    }


class FindDivergencesTests(unittest.TestCase):
    def test_pass_and_skip_is_not_a_divergence(self):
        results = [
            result("C-201", "python", "pass"),
            result("C-201", "java", "skip"),
        ]

        self.assertEqual([], find_divergences(results))

    def test_matching_participants_are_not_a_divergence(self):
        results = [
            result("D-400", "python", "pass"),
            result("D-400", "java", "pass"),
        ]

        self.assertEqual([], find_divergences(results))

    def test_disagreeing_participants_are_a_divergence(self):
        results = [
            result("D-401", "python", "pass"),
            result("D-401", "java", "fail"),
        ]

        self.assertEqual(["D-401"], find_divergences(results))

    def test_failure_and_skip_is_not_a_divergence(self):
        results = [
            result("C-230", "python", "fail"),
            result("C-230", "java", "skip"),
        ]

        self.assertEqual([], find_divergences(results))


if __name__ == "__main__":
    unittest.main()
