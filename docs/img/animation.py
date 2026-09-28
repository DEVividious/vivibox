"""Compose real Textual terminal captures into a small, captioned GIF."""

from __future__ import annotations

import html
import re


def scene(svg: str, title: str, detail: str, index: int, count: int) -> str:
    """Captions sit outside the real UI and describe the demo, never product controls."""
    view = re.search(r'viewBox="([^"]+)"', svg).group(1)
    _, _, width, height = map(float, view.split())
    caption_height = 124
    svg = svg.replace("<svg ", f'<svg y="{caption_height}" width="{width:g}" height="{height:g}" ', 1)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:g} {height + caption_height:g}">'
        f'<rect width="{width:g}" height="{height + caption_height:g}" fill="#0d1117"/>'
        '<g fill="#e6edf3" font-family="DejaVu Sans, sans-serif">'
        f'<text x="28" y="45" font-size="30" font-weight="600">'
        f"{index:02d} / {count:02d} · {html.escape(title)}</text>"
        f'<text x="28" y="84" font-size="23">{html.escape(detail)}</text></g>{svg}</svg>'
    )


async def render_gif(frames, target) -> None:
    """Rasterize the same real TUI captures for GitHub, with a shared palette and short fades.

    Optional recording dependencies only: Pillow and Playwright. No app dependencies change.
    Use VIVIBOX_DEMO_BROWSER to select an installed Chromium executable.
    """
    import io
    import os
    from pathlib import Path

    from PIL import Image
    from playwright.async_api import async_playwright

    pictures = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=os.environ.get("VIVIBOX_DEMO_BROWSER"))
        page = await browser.new_page(device_scale_factor=1)
        # No network or remote fonts in a documentation recording.
        await page.route("**/*", lambda route: route.abort())
        for frame in frames:
            svg = scene(frame[0], frame[2], frame[3], len(pictures) + 1, len(frames))
            svg = re.sub(r"@font-face\s*\{[^}]*\}", "", svg)
            svg = svg.replace("Fira Code", "DejaVu Sans Mono")
            await page.set_viewport_size({"width": 1280, "height": 1100})
            await page.set_content(
                "<style>body { margin: 0; background: #0d1117; } "
                "svg { display: block; } body > svg { width: 1000px; }</style>" + svg
            )
            await page.evaluate("document.fonts.ready")
            png = await page.locator("body > svg").screenshot()
            pictures.append(Image.open(io.BytesIO(png)).convert("RGB"))
        await browser.close()

    # One palette avoids colour flicker between scenes; no dithering keeps small text sharp.
    atlas = Image.new("RGB", (pictures[0].width, pictures[0].height * len(pictures)))
    for i, picture in enumerate(pictures):
        atlas.paste(picture, (0, i * picture.height))
    palette = atlas.quantize(colors=128)
    output, durations = [], []
    for i, picture in enumerate(pictures):
        output.append(picture.quantize(palette=palette, dither=Image.Dither.NONE))
        durations.append(round(frames[i][1] * 1000) - 200)
        following = pictures[(i + 1) % len(pictures)]
        for weight in (0.25, 0.5, 0.75, 1.0):
            blended = Image.blend(picture, following, weight)
            output.append(blended.quantize(palette=palette, dither=Image.Dither.NONE))
            durations.append(50)
    output[0].save(target, save_all=True, append_images=output[1:], duration=durations, loop=0, optimize=True)
    # Keep reviewable PNGs outside the repo only when requested.
    if folder := os.environ.get("VIVIBOX_DEMO_FRAMES"):
        path = Path(folder)
        path.mkdir(parents=True, exist_ok=True)
        for i, picture in enumerate(pictures, 1):
            picture.save(path / f"scene-{i}.png")
    with Image.open(target) as result:
        duration = sum(result.seek(i) or result.info["duration"] for i in range(result.n_frames))
    assert duration == round(sum(f[1] for f in frames) * 1000)
    print(f"{target.name}: {target.stat().st_size:,} bytes, {duration / 1000:g}s")
