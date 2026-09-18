#!/usr/bin/env python3
"""Subset the Nebulove handwriting font into a separately cacheable WOFF2 file.

移植自 lingyicute/Me 的 scripts/subset_font.py，适配本项目单文件介绍页：
index.html 内联全部 CSS / JS，@font-face 块直接在 index.html 中维护。

Optional maintenance/CI tool, not a runtime build requirement.
Run: pip install fonttools brotli && python scripts/subset_font.py
Use --force to refresh the upstream font, or NEBULOVE_FONT=/path/to/font.ttf offline.
"""
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FONT_URL = "https://raw.githubusercontent.com/lingyicute/Nebulove/main/Nebulove.ttf"
HTML_PATH = ROOT / "index.html"
CACHE_PATH = ROOT / "scripts/font-subset-cache.json"
OUTPUT = ROOT / "static/fonts/nebulove-subset.woff2"


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.chars = set()

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "script", "style"}:
            self.ignored += 1
        if not self.ignored:
            for name, value in attrs:
                if name in {"alt", "title", "aria-label", "placeholder"} and value:
                    self.chars.update(value)

    def handle_endtag(self, tag):
        if tag in {"head", "script", "style"}:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, text):
        if not self.ignored:
            self.chars.update(text)


def collect_chars():
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = VisibleText()
    parser.feed(html)
    chars = parser.chars
    # 本页大量文案由 <script> 动态渲染（renderTunnels、seedData、日志与状态文本），
    # 整站手写体下这些字符也必须进入子集；base64 与 SVG path 均为纯 ASCII，
    # 不会被误收，因此直接收集 script 块中的全部非 ASCII 字符。
    for block in re.findall(r"<script\b.*?</script>", html, flags=re.S):
        chars.update(c for c in block if ord(c) > 126)
    # Avoid subsetting Chinese code comments, SVG paths or base64 blobs as visible text.
    css = "\n".join(re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.S))
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for content in re.findall(r'content:\s*[\'"]([^\'"]*)[\'"]', css):
        chars.update(content)
    chars.update(chr(c) for c in range(32, 127))  # Includes dynamic versions / English UI.
    chars.update("：，。！？；“”‘’（）【】—…·《》×＝÷＋－\u200d\ufe0e\ufe0f")
    return chars


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def subset(font, chars):
    from fontTools.subset import Options, Subsetter

    subsetter = Subsetter(options=Options())
    subsetter.populate(unicodes=sorted(ord(c) for c in chars))
    subsetter.subset(font)
    font.flavor = "woff2"
    font.recalcTimestamp = False
    output = io.BytesIO()
    font.save(output)
    return output.getvalue()


def replace_face(html, family, filename):
    for match in re.finditer(r"@font-face\s*\{[^}]*\}", html):
        if re.search(r'font-family:\s*"' + re.escape(family) + r'"\s*;', match[0]):
            face = f'''@font-face{{
  font-family: "{family}";
  src: url("static/fonts/{filename}") format("woff2");
  font-display: swap;
}}'''
            return html[:match.start()] + face + html[match.end():]
    raise ValueError(f"Missing font-face: {family}")


def main():
    from fontTools.ttLib import TTFont

    chars = collect_chars()
    local_font = os.environ.get("NEBULOVE_FONT")
    source_key = digest(Path(local_font)) if local_font else FONT_URL
    fingerprint = hashlib.sha256((
        source_key + digest(Path(__file__))
        + json.dumps(sorted(ord(c) for c in chars))
    ).encode()).hexdigest()
    html = HTML_PATH.read_text(encoding="utf-8")
    if "--force" not in sys.argv and CACHE_PATH.exists():
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("fingerprint") == fingerprint and (
            OUTPUT.exists() and OUTPUT.name in html
            and cached.get("outputs", {}).get(OUTPUT.name) == digest(OUTPUT)
        ):
            print("Visible charset unchanged; WOFF2 subset is current.")
            return

    if local_font:
        nebulove = TTFont(local_font)
    else:
        print("Downloading Nebulove source font...")
        with urllib.request.urlopen(FONT_URL, timeout=90) as response:
            nebulove = TTFont(io.BytesIO(response.read()))
    data = subset(nebulove, chars)

    new_html = replace_face(html, "Nebulove", OUTPUT.name)
    assert new_html.count("@font-face") == html.count("@font-face")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_bytes(data)
    print(f"{OUTPUT.name}: {len(data):,} bytes")
    HTML_PATH.write_text(new_html, encoding="utf-8")
    CACHE_PATH.write_text(json.dumps({
        "fingerprint": fingerprint,
        "source": FONT_URL,
        "charset_count": len(chars),
        "outputs": {OUTPUT.name: digest(OUTPUT)},
    }, indent=2) + "\n", encoding="utf-8")
    print("Updated external subset; no font data embedded in render-blocking CSS.")


if __name__ == "__main__":
    main()
