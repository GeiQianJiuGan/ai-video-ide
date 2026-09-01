"""生成 `tauri/icons/*` —— 打包必需的图标，纯 Python，不依赖 Pillow 或 npx。

用法（在仓库根目录）：

    python scripts/make_icons.py            # 缺哪个生成哪个
    python scripts/make_icons.py --force    # 全部重画

为什么不是 `npx @tauri-apps/cli icon <源图>`：那条路要先有一张 1024px 的源图，
而这个仓库里没有；`tauri build` 又会在**编译之后、打包之时**才因为图标缺失失败，
那时候已经等了十分钟。所以图标由代码画出来，和构建脚本一起进版本库——
以后有了设计稿，把源图放成 `tauri/icons/source.png` 再跑一次 `--force` 即可
（有源图时优先用它，需要 Pillow 缩放）。

画的是什么：深色圆角底板 + 青色播放三角 + 一圈细环，取的是前端主题里那两个颜色
（`--color-base-0: #111316` / `--color-accent: #2dd4bf`）。图标要在 16px 下还认得出来，
所以只有一个主体形状，没有细节。
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ICONS = REPO_ROOT / "tauri" / "icons"
SOURCE = ICONS / "source.png"

#: tauri.conf.json 的 bundle.icon 列的就是这几个，少一个 `tauri build` 就失败。
PNG_TARGETS: dict[str, int] = {
    "32x32.png": 32,
    "128x128.png": 128,
    "128x128@2x.png": 256,
    "icon.png": 512,
}
#: Windows 的 .ico 与 macOS 的 .icns 里各自要塞哪些尺寸。
ICO_SIZES = (16, 32, 48, 64, 128, 256)
ICNS_ENTRIES = (("ic07", 128), ("ic08", 256), ("ic09", 512), ("ic10", 1024))

# ---- 配色（与 frontend/src/app/styles/index.css 的 token 同源）----
PLATE_TOP = (0x1B, 0x1E, 0x24)
PLATE_BOTTOM = (0x0E, 0x10, 0x13)
HAIRLINE = (0x2A, 0x2F, 0x37)
RING = (0x1C, 0x6B, 0x62)
GLYPH = (0x2D, 0xD4, 0xBF)

#: 每个像素在每个方向上取几个样本。小图标的斜边全靠它，大图标本身就够密，
#: 再 4×4 只是让纯 Python 的循环白跑几千万次。
def _supersample(size: int) -> int:
    return 4 if size <= 128 else 2


Rgba = tuple[int, int, int, int]


# ---------------------------------------------------------------- 形状（单位坐标）
def _rounded_rect(x: float, y: float, inset: float, radius: float) -> bool:
    """点在圆角矩形内？把点夹进「四角圆心构成的内矩形」再量距离，这一个式子就够。"""
    lo, hi = inset, 1.0 - inset
    if not (lo <= x <= hi and lo <= y <= hi):
        return False
    cx = min(max(x, lo + radius), hi - radius)
    cy = min(max(y, lo + radius), hi - radius)
    return math.hypot(x - cx, y - cy) <= radius


def _in_triangle(x: float, y: float, pts: tuple[tuple[float, float], ...]) -> bool:
    (ax, ay), (bx, by), (cx, cy) = pts
    d1 = (bx - ax) * (y - ay) - (by - ay) * (x - ax)
    d2 = (cx - bx) * (y - by) - (cy - by) * (x - bx)
    d3 = (ax - cx) * (y - cy) - (ay - cy) * (x - cx)
    return (d1 >= 0 and d2 >= 0 and d3 >= 0) or (d1 <= 0 and d2 <= 0 and d3 <= 0)


TRIANGLE = ((0.405, 0.305), (0.405, 0.695), (0.715, 0.5))


def _sample(x: float, y: float) -> Rgba:
    """一个采样点的颜色。图标是几层叠加，从下往上：底板 → 细边 → 环 → 三角。"""
    if not _rounded_rect(x, y, 0.035, 0.215):
        return (0, 0, 0, 0)

    t = (y - 0.035) / 0.93
    base = tuple(
        round(PLATE_TOP[i] + (PLATE_BOTTOM[i] - PLATE_TOP[i]) * max(0.0, min(1.0, t)))
        for i in range(3)
    )
    color: tuple[int, int, int] = base  # type: ignore[assignment]

    # 内侧一圈细边：让图标在浅色任务栏上也有轮廓。
    if not _rounded_rect(x, y, 0.055, 0.185):
        color = HAIRLINE

    r = math.hypot(x - 0.5, y - 0.5)
    if 0.298 <= r <= 0.340:
        color = RING

    if _in_triangle(x, y, TRIANGLE):
        color = GLYPH

    return (*color, 255)


def render(size: int) -> bytes:
    """按尺寸直接画（而不是缩放大图）：小图标的斜边这样才干净。返回 RGBA 字节流。"""
    out = bytearray()
    ss = _supersample(size)
    step = 1.0 / (size * ss)
    half = step / 2
    for py in range(size):
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                y = (py * ss + sy) * step + half
                for sx in range(ss):
                    x = (px * ss + sx) * step + half
                    r, g, b, a = _sample(x, y)
                    # 预乘再平均，否则透明区域的黑色会把边缘染暗。
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            if acc[3] == 0:
                out += b"\x00\x00\x00\x00"
                continue
            out += bytes(
                (
                    round(acc[0] / acc[3]),
                    round(acc[1] / acc[3]),
                    round(acc[2] / acc[3]),
                    round(acc[3] / (ss * ss)),
                )
            )
    return bytes(out)


# ---------------------------------------------------------------- 编码
def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(rgba: bytes, size: int) -> bytes:
    raw = bytearray()
    stride = size * 4
    for row in range(size):
        raw += b"\x00"  # filter type 0
        raw += rgba[row * stride : (row + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def encode_ico(pngs: dict[int, bytes]) -> bytes:
    """PNG 内嵌式 .ico（Vista 起的标准做法，`tauri-build` 用的 ico crate 认这个）。"""
    entries, blobs = bytearray(), bytearray()
    offset = 6 + 16 * len(pngs)
    for size in sorted(pngs):
        data = pngs[size]
        dim = 0 if size >= 256 else size  # 256 在 ICO 里写 0
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return struct.pack("<HHH", 0, 1, len(pngs)) + bytes(entries) + bytes(blobs)


def encode_icns(pngs: dict[int, bytes]) -> bytes:
    body = bytearray()
    for kind, size in ICNS_ENTRIES:
        data = pngs[size]
        body += kind.encode("ascii") + struct.pack(">I", 8 + len(data)) + data
    return b"icns" + struct.pack(">I", 8 + len(body)) + bytes(body)


# ---------------------------------------------------------------- 可选：用设计稿
def _from_source(size: int) -> bytes | None:
    """有 `tauri/icons/source.png` 时用它。缩放要 Pillow，没装就如实说一声再走画的那条路。"""
    if not SOURCE.is_file():
        return None
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        print(f"注意：发现 {SOURCE.name} 但没装 Pillow，仍按内置图形绘制")
        print('  · 想用设计稿：python -m pip install pillow 后重跑 --force')
        return None
    with Image.open(SOURCE) as img:
        return img.convert("RGBA").resize((size, size), Image.LANCZOS).tobytes()


def bitmap(size: int, cache: dict[int, bytes]) -> bytes:
    if size not in cache:
        cache[size] = _from_source(size) or render(size)
    return cache[size]


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 tauri/icons 下打包必需的图标")
    parser.add_argument("--force", action="store_true", help="已存在也重新生成")
    args = parser.parse_args()

    wanted = list(PNG_TARGETS) + ["icon.ico", "icon.icns"]
    missing = [n for n in wanted if not (ICONS / n).is_file()]
    if not missing and not args.force:
        print(f"图标齐了（{ICONS.relative_to(REPO_ROOT)}），加 --force 可以重画。")
        return 0

    ICONS.mkdir(parents=True, exist_ok=True)
    cache: dict[int, bytes] = {}
    sizes = set(PNG_TARGETS.values()) | set(ICO_SIZES) | {s for _, s in ICNS_ENTRIES}
    pngs: dict[int, bytes] = {}
    for size in sorted(sizes):
        pngs[size] = encode_png(bitmap(size, cache), size)
        print(f"  画好 {size}×{size}")

    for name, size in PNG_TARGETS.items():
        (ICONS / name).write_bytes(pngs[size])
    (ICONS / "icon.ico").write_bytes(encode_ico({s: pngs[s] for s in ICO_SIZES}))
    (ICONS / "icon.icns").write_bytes(encode_icns(pngs))

    for name in wanted:
        path = ICONS / name
        print(f"就位：{path.relative_to(REPO_ROOT)} · {path.stat().st_size / 1024:.1f} KB")
    print("\n完成。这些文件要提交进版本库——打包机不该为了一张图标再跑一遍生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
