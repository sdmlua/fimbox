#!/usr/bin/env python3
"""Render the module workflow diagrams for the fimbox READMEs.

Every ``*.mmd`` file in this folder is a plain, human-editable Mermaid
flowchart (no styling boilerplate inside). This script renders each one to
``svg/<name>.svg`` with the shared look defined in ``mermaid-config.json``
(white boxes, black borders/text/arrows, 18px font) and then rewrites the
arrow markers into open stick/barb arrowheads, which Mermaid cannot produce
natively. With ``--png`` it additionally exports a 500 DPI PNG into
``png/<name>.png`` for slides and papers.

Usage:
    python3 workflows/generate_workflows.py              # all diagrams -> svg/
    python3 workflows/generate_workflows.py streamflow.mmd   # just one
    python3 workflows/generate_workflows.py --png        # also 500 DPI PNGs

Requirements: Python 3 and Node.js >= 18 (``npx`` fetches
@mermaid-js/mermaid-cli, and its headless browser, on first run).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "mermaid-config.json"
CSS = HERE / "open-arrowheads.css"
SVG_DIR = HERE / "svg"
PNG_DIR = HERE / "png"

PNG_DPI = 500
PNG_SCALE = round(PNG_DPI / 96, 2)  # mermaid renders at 96 DPI x scale

# Open (unfilled) barb arrowheads. End markers point right, start markers
# point left; refX pins the barb tip to the end of the edge path.
OPEN_END_PATH = "M 0 0 L 10 5 L 0 10"
OPEN_START_PATH = "M 10 0 L 0 5 L 10 10"


def open_arrowheads(svg_text: str) -> str:
    """Replace Mermaid's filled triangle markers with open barbs.

    The style is written as inline attributes so it survives any renderer,
    including ones that strip or override embedded CSS.
    """

    def fix(match: re.Match) -> str:
        tag = match.group(0)
        if "pointStart" in tag:
            d, ref = OPEN_START_PATH, "1"
        else:
            d, ref = OPEN_END_PATH, "9"
        tag = re.sub(r'refX="[^"]*"', f'refX="{ref}"', tag)
        if "overflow=" not in tag:
            tag = tag.replace("<marker ", '<marker overflow="visible" ', 1)
        return re.sub(
            r'<path d="[^"]*" class="arrowMarkerPath"[^/]*/>',
            f'<path d="{d}" class="arrowMarkerPath" fill="none" stroke="#000000" '
            'style="fill:none;stroke:#000000;stroke-width:1.4;'
            'stroke-linecap:round;stroke-linejoin:round;"/>',
            tag,
        )

    return re.sub(
        r"<marker [^>]*point(?:End|Start)[^>]*>.*?</marker>",
        fix,
        svg_text,
        flags=re.S,
    )


def mmdc(*args: str) -> None:
    subprocess.run(["npx", "-y", "@mermaid-js/mermaid-cli", *args, "-q"], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "sources",
        nargs="*",
        help="specific .mmd files to render (default: every .mmd in this folder)",
    )
    parser.add_argument(
        "--png",
        action="store_true",
        help=f"also export {PNG_DPI} DPI PNGs into png/",
    )
    args = parser.parse_args()

    if not shutil.which("npx"):
        sys.exit("npx not found. Install Node.js >= 18 (https://nodejs.org) first.")

    sources = (
        [
            (HERE / s).resolve() if not Path(s).is_file() else Path(s).resolve()
            for s in args.sources
        ]
        if args.sources
        else sorted(HERE.glob("*.mmd"))
    )
    if not sources:
        sys.exit(f"no .mmd files found in {HERE}")

    SVG_DIR.mkdir(exist_ok=True)
    for src in sources:
        svg_out = SVG_DIR / f"{src.stem}.svg"
        mmdc("-i", str(src), "-o", str(svg_out), "-b", "white", "-c", str(CONFIG))
        svg_out.write_text(open_arrowheads(svg_out.read_text()))
        print(f"svg: {svg_out.relative_to(HERE.parent)}")

        if args.png:
            PNG_DIR.mkdir(exist_ok=True)
            png_out = PNG_DIR / f"{src.stem}.png"
            mmdc(
                "-i",
                str(src),
                "-o",
                str(png_out),
                "-b",
                "white",
                "-c",
                str(CONFIG),
                "-C",
                str(CSS),
                "-s",
                str(PNG_SCALE),
            )
            print(f"png ({PNG_DPI} DPI): {png_out.relative_to(HERE.parent)}")


if __name__ == "__main__":
    main()
