"""生成应用图标（packaging/icon.ico），供 PyInstaller 与窗口图标使用。

画一个深色圆角方块 + 绿色终端提示符 + 光标，纯代码生成，不依赖设计资源。

用法::

    .venv\\Scripts\\python.exe packaging\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

#: 与界面主题一致的配色
BG = (13, 17, 23, 255)          # #0d1117
BORDER = (48, 54, 61, 255)      # #30363d
GREEN = (63, 185, 80, 255)      # #3fb950
BLUE = (88, 166, 255, 255)      # #58a6ff
CURSOR = (230, 237, 243, 255)   # #e6edf3

SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int) -> Image.Image:
    # 先按 4 倍分辨率绘制再缩小，边缘更顺滑
    scale = 4
    canvas = size * scale

    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    margin = max(1, canvas // 32)
    radius = canvas // 5

    draw.rounded_rectangle(
        (margin, margin, canvas - margin, canvas - margin),
        radius=radius,
        fill=BG,
        outline=BORDER,
        width=max(1, canvas // 48),
    )

    # 提示符 ">"：两段折线
    stroke = max(2, canvas // 12)
    prompt_left = canvas * 0.24
    prompt_mid = canvas * 0.42
    prompt_top = canvas * 0.34
    prompt_bottom = canvas * 0.60

    draw.line(
        [(prompt_left, prompt_top), (prompt_mid, (prompt_top + prompt_bottom) / 2)],
        fill=GREEN,
        width=stroke,
        joint="curve",
    )
    draw.line(
        [(prompt_mid, (prompt_top + prompt_bottom) / 2), (prompt_left, prompt_bottom)],
        fill=GREEN,
        width=stroke,
        joint="curve",
    )

    # 下面的下划线（"_"）
    draw.line(
        [(canvas * 0.50, prompt_bottom), (canvas * 0.76, prompt_bottom)],
        fill=BLUE,
        width=stroke,
    )

    # 光标方块
    draw.rectangle(
        (
            canvas * 0.58,
            canvas * 0.26,
            canvas * 0.68,
            canvas * 0.72,
        ),
        fill=CURSOR,
    )

    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    target = Path(__file__).resolve().parent / "icon.ico"
    frames = [draw_icon(size) for size in SIZES]

    frames[-1].save(target, format="ICO", sizes=[(size, size) for size in SIZES])

    # 顺带导出一张 PNG，README 里可以直接用
    png = target.with_suffix(".png")
    frames[-1].save(png)

    print(f"已生成 {target}（{target.stat().st_size} 字节）与 {png.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
