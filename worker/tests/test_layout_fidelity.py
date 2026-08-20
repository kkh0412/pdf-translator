from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from worker.pdf_pipeline import (
    _caption_alignment,
    _equation_tex,
    _render_table_tex,
    _source_visual_width_ratio,
    _strip_structural_list_marker,
    build_latex,
)


class LayoutFidelityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.style = {
            "page_width_pt": 600.0,
            "page_height_pt": 800.0,
            "left_margin_pt": 50.0,
            "right_margin_pt": 50.0,
            "top_margin_pt": 46.0,
            "bottom_margin_pt": 48.0,
            "columns": 2,
            "column_gap_pt": 20.0,
            "body_size_pt": 10.0,
            "line_spacing": 1.04,
            "title_size_pt": 20.0,
            "section_size_pt": 12.0,
            "title_color": "#333333",
            "section_weight": "normal",
            "body_family": "serif",
            "heading_family": "serif",
            "title_family": "serif",
            "title_alignment": "left",
            "paragraph_indent_em": 1.0,
            "abstract_style": "normal",
        }

    def test_long_indivisible_equation_uses_shrink_only_macro(self) -> None:
        block = {
            "equation_latex": "x_{" + "a" * 220 + "}",
            "equation_lines": [],
            "equation_number": "",
            "flow_columns": 1,
            "column": "auto",
        }
        tex = _equation_tex(block)
        self.assertIn(r"\SourceFitDisplayMath{", tex)
        self.assertNotIn(r"\resizebox{0.98\linewidth}", tex)

    def test_figure_width_comes_from_source_geometry(self) -> None:
        block = {
            "bbox": [100, 200, 400, 500],
            "asset_bbox": [100, 200, 400, 500],
            "flow_columns": 2,
            "column": "column1",
        }
        # Source width = 180pt. Local source column = (500-20)/2 = 240pt.
        self.assertAlmostEqual(_source_visual_width_ratio(self.style, block), 0.75, places=3)

    def test_caption_alignment_uses_source_visual_geometry(self) -> None:
        visual = {"bbox": [100, 200, 500, 500], "asset_bbox": [100, 200, 500, 500]}
        centered_short = {"bbox": [220, 510, 380, 550]}
        left_multiline = {"bbox": [100, 510, 490, 590]}
        self.assertEqual(_caption_alignment(centered_short, visual), "center")
        self.assertEqual(_caption_alignment(left_multiline, visual), "left")

    def test_semantic_table_width_uses_source_geometry(self) -> None:
        block = {
            "bbox": [100, 200, 400, 500],
            "flow_columns": 2,
            "column": "column1",
            "table_header_rows": 1,
            "table_alignments": ["left"],
            "table_rows": [
                {
                    "cells": [
                        {
                            "source_text": "Header",
                            "translation_id": "t0",
                            "math_map": {},
                            "style": "bold",
                            "colspan": 1,
                            "align": "left",
                        }
                    ]
                }
            ],
        }
        tex = _render_table_tex(block, {"t0": "머리글"}, self.style)
        self.assertIn(r"\begin{minipage}{0.750\linewidth}", tex)
        self.assertIn(r"\begin{tabularx}{\linewidth}", tex)

    def test_structural_bullet_is_removed(self) -> None:
        self.assertEqual(_strip_structural_list_marker("• 항목"), "항목")
        self.assertEqual(_strip_structural_list_marker("• • 항목"), "항목")
        self.assertEqual(_strip_structural_list_marker("- 항목"), "항목")
        self.assertEqual(_strip_structural_list_marker("항목"), "항목")

    def test_generated_latex_preserves_visual_scale_caption_style_and_heading_gap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            asset = work / "assets" / "figure.pdf"
            blocks = [
                {
                    "id": "fig",
                    "kind": "figure",
                    "page": 0,
                    "flow_columns": 2,
                    "column": "column1",
                    "bbox": [100, 200, 400, 500],
                    "asset_bbox": [100, 200, 400, 500],
                    "asset": asset,
                },
                {
                    "id": "cap",
                    "kind": "caption",
                    "page": 0,
                    "flow_columns": 2,
                    "column": "column1",
                    "bbox": [100, 510, 400, 570],
                    "style": "italic",
                    "source_text": "Figure 1. Example caption.",
                    "math_map": {},
                    "source_font_size_pt": 8.0,
                    "source_font_family": "serif",
                },
                {
                    "id": "li",
                    "kind": "list_item",
                    "page": 0,
                    "flow_columns": 2,
                    "column": "column1",
                    "bbox": [100, 600, 400, 640],
                    "style": "normal",
                    "source_text": "• First point",
                    "math_map": {},
                },
                {
                    "id": "sec",
                    "kind": "section",
                    "page": 0,
                    "flow_columns": 2,
                    "column": "column1",
                    "bbox": [100, 650, 400, 690],
                    "style": "normal",
                    "source_text": "Methods",
                    "math_map": {},
                },
            ]
            translations = {
                "cap": "그림 1. 예시 캡션.",
                "li": "• 첫 번째 항목",
                "sec": "방법",
            }
            with mock.patch("worker.pdf_pipeline._copy_fonts"):
                tex = build_latex(self.style, blocks, translations, work)

        self.assertIn(r"\includegraphics[width=0.750\linewidth]", tex)
        self.assertIn(r"\begin{minipage}{0.750\linewidth}", tex)
        self.assertIn(r"\fontsize{7.52pt}{8.87pt}\selectfont", tex)
        self.assertIn(r"\textit{그림 1. 예시 캡션.}", tex)
        self.assertIn("\\item 첫 번째 항목", tex)
        self.assertNotIn("\\item •", tex)
        self.assertIn(r"\par\addvspace{0.92em}\noindent", tex)
        self.assertIn(r"\par\addvspace{0.68em}\noindent", tex)
        self.assertIn(r"\newcommand{\SourceFitDisplayMath}", tex)


if __name__ == "__main__":
    unittest.main()
