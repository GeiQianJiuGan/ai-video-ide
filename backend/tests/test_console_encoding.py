"""控制台编码不是 UTF-8 时（英文 Windows、PyInstaller sidecar），中文日志不能让后端崩掉。"""

from __future__ import annotations

import io
import sys

import pytest

from app.main import _utf8_console


def test_chinese_log_survives_a_non_utf8_console(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stderr", console)
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="ascii"))

    _utf8_console()
    print("工程目录 /root/蛊真人", file=sys.stderr)
    sys.stderr.flush()

    assert raw.getvalue().decode("utf-8").strip() == "工程目录 /root/蛊真人"
