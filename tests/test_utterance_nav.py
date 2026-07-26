import unittest

from fronts.desktop.pages.meeting import next_utterance_ms, prev_utterance_ms

class DummyUtterance:
    def __init__(self, start):
        self.start = start

class UtteranceNavTests(unittest.TestCase):
    def setUp(self):
        self.utterances = [
            DummyUtterance(0.0),
            DummyUtterance(2.5),
            DummyUtterance(5.0),
            DummyUtterance(10.0),
        ]

    def test_next_utterance(self):
        self.assertEqual(next_utterance_ms(0, self.utterances), 2500)
        self.assertEqual(next_utterance_ms(2400, self.utterances), 2500)
        self.assertEqual(next_utterance_ms(2500, self.utterances), 5000)
        self.assertEqual(next_utterance_ms(10000, self.utterances), None)

    def test_prev_utterance(self):
        self.assertEqual(prev_utterance_ms(10000, self.utterances), 5000)
        self.assertEqual(prev_utterance_ms(2600, self.utterances), 2500)
        self.assertEqual(prev_utterance_ms(2500, self.utterances), 0)
        self.assertEqual(prev_utterance_ms(0, self.utterances), None)

if __name__ == "__main__":
    unittest.main()
