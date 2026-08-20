from __future__ import annotations

import unittest

from worker.pdf_pipeline import _translation_integrity_errors


class PdfPipelineTranslationIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.items = [
            {
                "id": "p1-b1",
                "kind": "paragraph",
                "text": "The value [[MATH_0]] is invariant.",
            }
        ]

    def test_sentence_final_math_is_structurally_valid(self) -> None:
        errors = _translation_integrity_errors(
            self.items,
            {"p1-b1": "불변인 값은 [[MATH_0]]."},
        )
        self.assertEqual(errors, [])

    def test_missing_inline_math_is_rejected_before_render(self) -> None:
        errors = _translation_integrity_errors(
            self.items,
            {"p1-b1": "이 값은 불변이다."},
        )
        self.assertTrue(any("inline-math placeholders changed" in item for item in errors))

    def test_internal_math_transport_marker_is_rejected(self) -> None:
        errors = _translation_integrity_errors(
            self.items,
            {"p1-b1": "§math{x} [[MATH_0]]"},
        )
        self.assertTrue(any("internal math transport marker" in item for item in errors))


if __name__ == "__main__":
    unittest.main()
