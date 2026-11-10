"""字体子集脚本的纯文本处理回归；不需要 fontTools 或网络。"""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('subset_font', ROOT / 'scripts' / 'subset_font.py')
subset_font = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subset_font)


class SubsetFontTests(unittest.TestCase):
    def test_replace_face_accepts_either_quote_style(self):
        for quote in ('"', "'"):
            html = f"@font-face {{ font-family: {quote}Nebulove{quote}; src: url(x.ttf); }}\nbody{{}}"
            with self.subTest(quote=quote):
                out = subset_font.replace_face(html, 'Nebulove', 'n.woff2')
                self.assertIn('static/fonts/n.woff2', out)
                self.assertNotIn('x.ttf', out)
                self.assertTrue(out.endswith('body{}'))
        with self.assertRaises(ValueError):
            subset_font.replace_face('@font-face { font-family: "Other"; }', 'Nebulove', 'n.woff2')

    def test_script_and_style_tags_are_matched_case_insensitively(self):
        html = ('<html><body><P>正文</P><SCRIPT>var a = "脚本字";</SCRIPT>'
                '<STYLE>.x::after{content:"样式"}</STYLE></body></html>')
        with patch.object(Path, 'read_text', return_value=html):
            chars = subset_font.collect_chars()
        for ch in '正文脚本字样式':
            self.assertIn(ch, chars)


if __name__ == '__main__':
    unittest.main()
