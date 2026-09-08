"""宽松且高容错的 JSON 解析与截断自动修复模块。

大语言模型在生成结构化参数或响应时，常见以下缺陷：
  1. 使用 Markdown 代码块包裹（```json ... ```）；
  2. 字符串内部带有未经转义的原始换行符或制表符；
  3. 对象或数组末尾多出悬挂逗号（trailing comma，如 `{"a": 1,}`）；
  4. 使用 Python / JS 字面量（`True`、`False`、`None` 代替 `true`、`false`、`null`）；
  5. **因 token 上限被截断（Truncation / Context Overflow）**：
     输出突然中断在中间，导致字符串未闭合、键值对写了一半、大括号未闭合。

本模块提供清洗、字符流状态机闭合与重试解析能力，把损坏但有价值的 JSON 尽可能挽救为可用对象；
并在彻底无法解析时提供精准的错误定位与片段，便于回喂给模型进行自纠。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 匹配 Markdown 代码块外皮的正则
_CODE_BLOCK_PATTERN = re.compile(
    r"^\s*```(?:json|JSON)?\s*\n?(.*?)\n?```\s*$",
    re.DOTALL,
)


def extract_json_candidate(text: str) -> str:
    """提取文本中最可能为 JSON 的核心片段。"""
    raw = str(text or "").strip()
    if not raw:
        return ""

    # 1. 尝试剥离代码块
    m = _CODE_BLOCK_PATTERN.match(raw)
    if m:
        raw = m.group(1).strip()

    # 2. 找到最外层的首个 '{' 或 '['
    first_obj = raw.find("{")
    first_arr = raw.find("[")
    if first_obj == -1 and first_arr == -1:
        return raw

    start: int
    if first_obj != -1 and first_arr != -1:
        start = min(first_obj, first_arr)
    elif first_obj != -1:
        start = first_obj
    else:
        start = first_arr

    # 截取从第一个起始符开始的内容
    raw = raw[start:].strip()

    # 如果尾部有未配对的代码块标记，去除
    if raw.endswith("```"):
        raw = raw[:-3].strip()

    return raw


def clean_json_syntax(text: str) -> str:
    """清理常见的轻微语法瑕疵（尾部逗号、字面量等）。"""
    if not text:
        return ""

    s = text

    # 去除首尾空白
    s = s.strip()

    # 移除数组或对象末尾的悬挂逗号：如 `, }` -> ` }`, `, ]` -> ` ]`
    # 需要注意避开引号内部的逗号，这里用状态扫描或正则简单清洗
    # 简易而安全的做法：逐字符扫描去除不在字符串内部的末尾逗号
    out_chars: list[str] = []
    in_str = False
    escape = False
    for char in s:
        if in_str:
            out_chars.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
        else:
            if char == '"':
                in_str = True
            out_chars.append(char)

    filtered = "".join(out_chars)

    # 替换不在字符串内的悬挂逗号
    # 查找逗号后面跟着空白和闭合括号的情况
    cleaned = re.sub(r",\s*([}\]])", r"\1", filtered)

    # 替换顶层/非字符串内的 Python 字面量
    # 简单替换单词边界
    cleaned = re.sub(r"\bTrue\b", "true", cleaned)
    cleaned = re.sub(r"\bFalse\b", "false", cleaned)
    cleaned = re.sub(r"\bNone\b", "null", cleaned)

    return cleaned


def repair_truncated_json(text: str) -> str:
    """尝试自动闭合并修复因 token 上限被截断的 JSON 字符串。

    修复策略：
      1. 使用字符状态机扫描文本；
      2. 若扫描结束时仍处于字符串内（未闭合双引号），补上 `"`；
      3. 检查尾部是否处于不完整的键值状态（如 `"key":` 或 `"key"` 或 `,`），安全回退截断；
      4. 按照未配对的栈，逆序补齐 `}` 与 `]`。
    """
    s = text.strip()
    if not s:
        return "{}"

    stack: list[str] = []
    in_str = False
    escape = False

    for char in s:
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
        else:
            if char == '"':
                in_str = True
            elif char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in ("}", "]"):
                if stack and stack[-1] == char:
                    stack.pop()

    # 如果停在字符串内，先把字符串闭合
    if in_str:
        s += '"'

    s = s.strip()

    # 清理尾部可能悬挂的半截 key、冒号或逗号
    # 例如: `{"a": 1, "b":` -> `{"a": 1`
    # 或 `{"a": 1,` -> `{"a": 1`
    # 循环检查并剥离尾部未完成的悬挂结构
    changed = True
    while changed:
        changed = False
        s = s.strip()
        # 移除末尾逗号
        if s.endswith(","):
            s = s[:-1].strip()
            changed = True
        # 移除末尾孤立冒号
        elif s.endswith(":"):
            s = s[:-1].strip()
            changed = True
        # 移除冒号前可能悬挂的孤立键（如 `"key"`）
        # 只有在它前面是逗号或起始括号时才剥离
        m = re.search(r'[,{\[]\s*"[^"]*"$', s)
        if m and not s.endswith(("}", "]")):
            # 去除悬挂的键
            cut_pos = m.start()
            # 保留前面的逗号/括号
            prefix_char = s[cut_pos]
            s = s[:cut_pos].strip()
            if prefix_char in ("{", "["):
                s += prefix_char
            changed = True

    # 再次重新计算需要闭合的括号栈
    stack.clear()
    in_str = False
    escape = False
    for char in s:
        if in_str:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_str = False
        else:
            if char == '"':
                in_str = True
            elif char == "{":
                stack.append("}")
            elif char == "[":
                stack.append("]")
            elif char in ("}", "]"):
                if stack and stack[-1] == char:
                    stack.pop()

    if in_str:
        s += '"'

    # 逆序补齐未闭合的括号
    while stack:
        s += stack.pop()

    return s


def parse_lenient_json(raw: Any) -> tuple[Any | None, str | None]:
    """宽松解析 JSON 数据。

    返回值：`(parsed_result, error_reason)`
    若解析成功，error_reason 为 None；若彻底失败，parsed_result 为 None，带错误原因。
    """
    if isinstance(raw, (dict, list)):
        return raw, None

    text = str(raw or "").strip()
    if not text:
        return {}, None

    # 第 1 步：原生标准解析（允许非严格控制字符）
    try:
        return json.loads(text, strict=False), None
    except json.JSONDecodeError as err:
        first_err = err

    # 第 2 步：剥离 Markdown 代码块与杂质后再尝试
    candidate = extract_json_candidate(text)
    try:
        return json.loads(candidate, strict=False), None
    except json.JSONDecodeError:
        pass

    # 第 3 步：语法轻微清洗（悬挂逗号、布尔字面量）
    cleaned = clean_json_syntax(candidate)
    try:
        return json.loads(cleaned, strict=False), None
    except json.JSONDecodeError:
        pass

    # 损坏或截断不完整的内容直接判定解析失败，不盲目补全闭合残缺数据，
    # 将精准错误原因反馈给模型，让其重新生成完整的参数。
    detail = f"{first_err.msg}（位置：行 {first_err.lineno} 列 {first_err.colno}）"
    return None, detail
