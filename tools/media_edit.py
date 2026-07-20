#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence


def transform(frame: Image.Image, operation: str) -> Image.Image:
    if operation == "flip-horizontal":
        return ImageOps.mirror(frame)
    if operation == "flip-vertical":
        return ImageOps.flip(frame)
    return frame.copy()


def edit_animated(source: Image.Image, output: Path, operation: str) -> None:
    # Pillow exposes composited frames here. Converting every frame to RGBA before
    # reordering avoids carrying GIF delta rectangles into the new animation.
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(source):
        frames.append(frame.convert("RGBA"))
        durations.append(frame.info.get("duration", source.info.get("duration", 100)))
    if operation == "reverse":
        frames.reverse()
        durations.reverse()
    else:
        frames = [transform(frame, operation) for frame in frames]
    output.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        output,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=source.info.get("loop", 0),
        disposal=2,
        optimize=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Flip or reverse images and GIFs without GIF trails")
    parser.add_argument("operation", choices=("flip-horizontal", "flip-vertical", "reverse"))
    parser.add_argument("input")
    parser.add_argument("output")
    args = parser.parse_args()
    output = Path(args.output)
    with Image.open(args.input) as source:
        if getattr(source, "is_animated", False):
            edit_animated(source, output, args.operation)
        else:
            if args.operation == "reverse":
                raise SystemExit("reverse is only meaningful for animated images")
            output.parent.mkdir(parents=True, exist_ok=True)
            result = transform(source.convert("RGBA"), args.operation)
            if output.suffix.lower() in {".jpg", ".jpeg"}:
                result = result.convert("RGB")
            result.save(output)


if __name__ == "__main__":
    main()
