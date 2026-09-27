"""Compose real terminal captures into a captioned, script-free SVG tour."""

from __future__ import annotations

import html
import re


def animated(frames: list[tuple[str, float, str, str]]) -> str:
    """Hold readable scenes, fading the next over the opaque previous scene.

    The first scene is also the fallback for reduced motion and viewers without CSS animation.
    Captions are outside the captured UI; they describe the demo, not product controls.
    """
    total = sum(seconds for _, seconds, _, _ in frames)
    view = re.search(r'viewBox="([^"]+)"', frames[0][0]).group(1)
    _, _, width, height = map(float, view.split())
    caption_height = 124
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} '
        f'{height + caption_height:g}" role="img" aria-labelledby="tour-title tour-desc">',
        '<title id="tour-title">A task in vivibox</title>',
        '<desc id="tour-desc">An illustrative tour from a task description through plan approval, '
        "implementation, verification and review to acceptance. Other tasks run alongside it. "
        "Times are compressed and costs are examples.</desc>",
        "<style>",
        ".scene { opacity: 0; } .scene:first-of-type { opacity: 1; }",
        ".caption { font-family: system-ui, sans-serif; fill: #e6edf3; }",
    ]
    at = 0.0
    animations = []
    for i, (_, seconds, _, _) in enumerate(frames):
        if i:
            start = 100 * (at - 0.35) / total
            full = 100 * at / total
            end = 100 * (at + seconds) / total
            if i == len(frames) - 1:
                hold = 100 * (total - 0.35) / total
                tail = f"{hold:.5f}% {{ opacity: 1; }} 100% {{ opacity: 0; }}"
            else:
                tail = f"{end:.5f}% {{ opacity: 1; }} {end + 0.00001:.5f}%, 100% {{ opacity: 0; }}"
            parts.append(
                f"@keyframes scene-{i} {{ 0%, {start:.5f}% {{ opacity: 0; }} "
                f"{full:.5f}% {{ opacity: 1; }} {tail} }}"
            )
            animations.append(f"#scene-{i} {{ animation: scene-{i} {total:g}s linear infinite; }}")
        at += seconds
    parts.extend(["@media (prefers-reduced-motion: no-preference) {", *animations, "}", "</style>"])
    for i, (svg, _, title, detail) in enumerate(frames):
        # Rich IDs and CSS classes must be unique even when two captures look identical.
        svg = re.sub(r"terminal-\d+", lambda m, i=i: f"tour-{i}-{m[0]}", svg)
        svg = svg.replace("<svg ", f'<svg y="{caption_height}" width="{width:g}" height="{height:g}" ', 1)
        parts.extend(
            [
                f'<g class="scene" id="scene-{i}">',
                f'<rect width="{width:g}" height="{height + caption_height:g}" fill="#0d1117"/>',
                f'<text class="caption" x="28" y="45" font-size="30" font-weight="600">'
                f"{i + 1:02d} / {len(frames):02d} · {html.escape(title)}</text>",
                f'<text class="caption" x="28" y="84" font-size="23">{html.escape(detail)}</text>',
                svg,
                "</g>",
            ]
        )
    parts.append("</svg>")
    return "\n".join(parts)
