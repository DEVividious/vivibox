"""Check README links and the links in its documentation targets.

    uv run python docs/check_links.py [--external]

Uses markdown-it-py, already installed with Textual. External checks issue GET requests and
follow redirects; they do not use GitHub credentials or change repository visibility.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable("table")


def links(path: Path):
    for token in MARKDOWN.parse(path.read_text()):
        for child in token.children or []:
            if child.type in ("link_open", "image"):
                yield child.attrGet("href" if child.type == "link_open" else "src")


def anchors(path: Path) -> set[str]:
    tokens = MARKDOWN.parse(path.read_text())
    counts: Counter = Counter()
    found = set()
    for i, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        text = "".join(t.content for t in tokens[i + 1].children or [] if t.type in ("text", "code_inline"))
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        found.add(f"{slug}-{counts[slug]}" if counts[slug] else slug)
        counts[slug] += 1
    return found


def local(source: Path, url: str) -> Path:
    path = unquote(urlsplit(url).path)
    return (source.parent / path).resolve() if path else source


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--external", action="store_true")
    args = parser.parse_args()
    readme = ROOT / "README.md"
    documents = {readme}
    for url in links(readme):
        if not urlsplit(url).scheme and (target := local(readme, url)).suffix == ".md":
            documents.add(target)
    failures, checked, external = [], 0, set()
    for source in sorted(documents):
        if not source.is_file():
            failures.append(f"missing document: {source.relative_to(ROOT)}")
            continue
        for url in links(source):
            parsed = urlsplit(url)
            checked += 1
            if parsed.scheme:
                if parsed.scheme in ("http", "https"):
                    external.add(url)
                continue
            target = local(source, url)
            if not target.exists():
                failures.append(f"{source.relative_to(ROOT)}: missing {url}")
            elif (
                parsed.fragment and target.suffix == ".md" and unquote(parsed.fragment) not in anchors(target)
            ):
                failures.append(f"{source.relative_to(ROOT)}: missing anchor {url}")
    if args.external:
        for url in sorted(external):
            try:
                request = Request(url, headers={"User-Agent": "vivibox-docs-link-check"})
                with urlopen(request, timeout=20) as response:
                    print(f"HTTP {response.status}: {url}")
            except Exception as error:
                failures.append(f"{url}: {error}")
    print(f"Checked {checked} links in {len(documents)} documents; {len(external)} external URLs.")
    for failure in failures:
        print(f"FAIL: {failure}")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
