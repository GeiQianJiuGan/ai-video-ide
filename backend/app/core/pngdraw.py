"""纯 Python 画占位 PNG（演示工程的素材图靠它）。

为什么不用 Pillow：理由和 `scripts/make_icons.py` 完全一样——打包机不必为了几张
占位图多装一个二进制轮子。`struct` + `zlib` 手写一张 8bit RGBA PNG 就够了。

**刻意不画文字**：字形轮廓超出这几十行能覆盖的范围，而且「这张图是谁」本来就不该
靠图上的字说清——那句话是 `asset.description`，也是模型唯一看得到的东西
（见 CLAUDE.md 的「素材描述」段）。这里只画一块辨识度够的色卡：底色 + 边框 +
一条斜带，色相由调用方给，于是三个角色 / 两个地点 / 两个道具在缩略图里一眼分得开。
"""

from __future__ import annotations

import struct
import zlib

Rgb = tuple[int, int, int]


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(rgba: bytes, width: int, height: int) -> bytes:
    """8bit RGBA、filter 0 的最小 PNG。`rgba` 必须正好是 width*height*4 字节。"""
    stride = width * 4
    if len(rgba) != stride * height:
        raise ValueError(f"像素数据长度不对：{len(rgba)} != {stride * height}")
    raw = bytearray()
    for row in range(height):
        raw += b"\x00"  # filter type 0
        raw += rgba[row * stride : (row + 1) * stride]
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


def _hsv(hue: float, sat: float, val: float) -> Rgb:
    """HSV → RGB。只为了「给我一个色相，别的我不管」这一个用法。"""
    hue = hue % 360.0
    chroma = val * sat
    second = chroma * (1 - abs((hue / 60.0) % 2 - 1))
    match int(hue // 60) % 6:
        case 0:
            r, g, b = chroma, second, 0.0
        case 1:
            r, g, b = second, chroma, 0.0
        case 2:
            r, g, b = 0.0, chroma, second
        case 3:
            r, g, b = 0.0, second, chroma
        case 4:
            r, g, b = second, 0.0, chroma
        case _:
            r, g, b = chroma, 0.0, second
    m = val - chroma
    return (round((r + m) * 255), round((g + m) * 255), round((b + m) * 255))


def card(width: int, height: int, hue: float) -> bytes:
    """一张占位色卡的 PNG 字节。`hue` 是 0–360 的色相，每个对象给一个不同的值。"""
    width, height = max(8, int(width)), max(8, int(height))
    base = _hsv(hue, 0.32, 0.24)
    band = _hsv(hue, 0.46, 0.62)
    edge = _hsv(hue, 0.28, 0.82)
    border = max(2, min(width, height) // 32)
    px = bytearray(width * height * 4)
    for y in range(height):
        for x in range(width):
            on_border = x < border or y < border or x >= width - border or y >= height - border
            # 一条 45° 斜带：同一个式子在任何尺寸下都成立，不用先算几何。
            diag = (x + y) % max(24, (width + height) // 12) < max(6, width // 24)
            color = edge if on_border else band if diag else base
            i = (y * width + x) * 4
            px[i], px[i + 1], px[i + 2], px[i + 3] = color[0], color[1], color[2], 255
    return encode_png(bytes(px), width, height)
