"""Render the 1200x630 social preview card used in Open Graph / Twitter meta tags.

Usage:
    uv run scripts/generate_social_preview.py --font path/to/JetBrainsMono[wght].ttf \\
        --output docs/assets/social-preview.png
"""

import argparse

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 1200, 630
BACKGROUND = "#282c34"
ACCENT = "#61afef"
TITLE_COLOR = "#dcdfe4"
MUTED = "#7f848e"
GREEN = "#98c379"

TITLE = "gpiod-stepper-motors"
DESCRIPTION = (
    "A small Python library for driving stepper motors from a\n"
    "Raspberry Pi, using libgpiod and hardware PWM."
)
FOOTER = "kiers-neely/gpiod-stepper-motors"


def load_font(path: str, size: int, weight: float) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size)
    try:
        font.set_variation_by_axes([weight])
    except (AttributeError, OSError):
        pass  # static (non-variable) font file; nothing to set
    return font


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--font", required=True, help="Path to a JetBrains Mono .ttf file"
    )
    parser.add_argument(
        "--output", default="docs/assets/social-preview.png", help="Output PNG path"
    )
    args = parser.parse_args()

    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    draw.rectangle([(0, 0), (WIDTH, 10)], fill=ACCENT)

    margin = 90
    dot_y = 150
    for i, color in enumerate(["#e06c75", "#e5c07b", GREEN]):
        draw.ellipse(
            [(margin + i * 28, dot_y), (margin + i * 28 + 16, dot_y + 16)], fill=color
        )

    title_font = load_font(args.font, 64, 700)
    desc_font = load_font(args.font, 28, 400)
    footer_font = load_font(args.font, 24, 500)

    draw.text((margin, 210), f"$ {TITLE}", font=title_font, fill=TITLE_COLOR)
    draw.text((margin, 300), DESCRIPTION, font=desc_font, fill=MUTED, spacing=14)
    draw.text((margin, HEIGHT - 80), FOOTER, font=footer_font, fill=GREEN)

    image.save(args.output)
    print(f"Saved social preview to {args.output}")


if __name__ == "__main__":
    main()
