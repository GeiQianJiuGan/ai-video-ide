"""附件抽文本：把 .docx / .xlsx / .pptx / .csv 之类变成一段能读的纯文本。

AI 协作栏那条输入框只收文字，可导演手上的东西常常是一份 Word 剧本或一张 Excel 分镜表。
让他自己复制粘贴一遍不是不行，几十页就不行了——所以这里做「文件 → 文字」这一步。

**零新依赖**：docx / xlsx / pptx 就是 zip 里一堆 xml，标准库的 `zipfile` + `xml.etree`
够用了。为了取里面那点文字装 `python-docx` + `openpyxl`，打包那侧还要多担两份
hiddenimports 的风险（见 docs/06），不值得。理由同 `core/pngdraw.py` 不装 Pillow。

四条刻意的取舍：

  1. **只抽文字**。字体、颜色、嵌图对「让 AI 看懂这份剧本」没有帮助，嵌图还会把请求
     撑到几十 MB。
  2. **抽不了的直接说抽不了**（硬约束 4）：`.pdf` / `.doc` 是二进制排版格式，标准库
     拆不开，于是报四要素错误并给出「另存为 .docx / .txt」这条出路——绝不返回一段乱码
     充数，那会让用户以为 AI 看到的就是他那份文档。
  3. **超长必须截断，而且必须说出来**（`truncated`）。模型的上下文有上限，一份三百页
     的文档整份塞进去只会把用户那句话挤掉。
  4. **表格保留列的位置**：空单元格补空串再用制表符连起来。少一列就串行，那时候
     「第 3 镜的时长」会被读成别的东西。

抽出来的文字**不落库、不落盘、不出网**：它只是填进输入框的一段草稿，用户看得见、
改得动，按下发送才跟着那句话一起走（见 `services/director.py::attach`）。
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

log = get_logger("doctext")

#: 抽出来的文字上限（字符）。真正生效的那个由 `settings.director_attach_max_chars` 给。
DEFAULT_LIMIT = 20000

#: 单个 zip 成员解开之后的上限。zip 炸弹不是这里的威胁模型（文件是用户自己选的），
#: 但一份 200 MB 的 sharedStrings 会把内存吃光，这个数挡的是那种。
MEMBER_MAX = 64 * 1024 * 1024

#: 能抽的：后缀 → 人话名字。**前端那个 `accept` 也来自这张表**（`GET /projects/{pid}/director`
#: 把它带出去），别在前端再写第二份。
KINDS: dict[str, str] = {
    ".docx": "Word 文档",
    ".xlsx": "Excel 表格",
    ".pptx": "PowerPoint 演示",
    ".txt": "纯文本",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".csv": "CSV 表格",
    ".tsv": "TSV 表格",
    ".json": "JSON",
    ".srt": "字幕",
    ".vtt": "字幕",
}
_OLD_OFFICE = (
    "这是 Office 97-2003 的二进制格式，不是 zip + xml，标准库读不出里面的文字。",
    [
        "在 Word / Excel / PowerPoint / WPS 里打开它，另存为 .docx / .xlsx / .pptx 再传",
        "或者只把要给它看的那几段直接粘进输入框",
    ],
)
_WPS = (
    "这是 WPS 自己的格式，标准库读不了。",
    ["在 WPS 里另存为 .docx / .xlsx 再传", "或者只把要给它看的那几段直接粘进输入框"],
)
_APPLE = (
    "这是 iWork 的格式，里面是苹果自己的结构，标准库读不了。",
    ["在 Pages / Numbers / Keynote 里导出成 .docx / .xlsx / .pptx 再传", "或者导出成 .txt"],
)

#: 抽不了的：后缀 → （为什么, 怎么办）。**绝不返回一段乱码充数**。
REFUSED: dict[str, tuple[str, list[str]]] = {
    ".pdf": (
        "PDF 是排版格式，里面的字可能是文本、也可能是图或曲线，标准库抽不出可靠的内容。",
        ["在 Word / WPS 里打开它，另存为 .docx 再传", "或者在阅读器里全选复制，粘进输入框"],
    ),
    ".doc": _OLD_OFFICE,
    ".xls": _OLD_OFFICE,
    ".ppt": _OLD_OFFICE,
    ".wps": _WPS,
    ".et": _WPS,
    ".dps": _WPS,
    ".pages": _APPLE,
    ".numbers": _APPLE,
    ".key": _APPLE,
}


@dataclass
class Extracted:
    """抽出来的东西。

    `truncated` 与 `notes` 必须一路带到界面上——凭什么少了半份文档、为什么表格里的日期
    是一串数字，用户有权在按下发送之前就看到。
    """

    filename: str
    kind: str
    kind_label: str
    text: str
    chars: int
    truncated: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "text": self.text,
            "chars": self.chars,
            "truncated": self.truncated,
            "notes": list(self.notes),
        }


def accept_attr() -> str:
    """`<input accept=…>` 那一串。口径只有 `KINDS` 一处，前端不写死后缀清单。"""
    return ",".join(sorted(KINDS))


def extract(filename: str, data: bytes, limit: int = DEFAULT_LIMIT) -> Extracted:
    """一份附件 → 一段纯文本。抽不了就抛四要素错误，绝不返回半份东西充数。"""
    name = (filename or "").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].strip()
    suffix = f".{name.rsplit('.', 1)[-1].lower()}" if "." in name else ""
    if suffix in REFUSED:
        why, how = REFUSED[suffix]
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            f"读不了 {suffix} 这种附件",
            why,
            how,
            {"filename": name, "supported": sorted(KINDS)},
        )
    if suffix not in KINDS:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "不认识这种附件",
            f"{name or '这个文件'} 的后缀不在能抽文字的清单里。",
            [f"能读的是：{'、'.join(sorted(KINDS))}", "别的格式请先另存为 .docx 或 .txt"],
            {"filename": name, "supported": sorted(KINDS)},
        )
    if not data:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "附件是空的",
            f"{name} 里一个字节都没有。",
            ["确认文件没有传坏，重新选一次"],
            {"filename": name},
        )

    notes: list[str] = []
    if suffix == ".docx":
        lines = _docx(data, name)
    elif suffix == ".xlsx":
        lines = _xlsx(data, name, notes)
    elif suffix == ".pptx":
        lines = _pptx(data, name, notes)
    else:
        lines = _plain(data, suffix, notes)
    text, truncated = _join(lines, max(500, int(limit)))
    if truncated:
        notes.append(f"太长了，只取了前 {len(text)} 个字符；剩下的没带上，需要就分几次传。")
    if not text.strip():
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "这份附件里没有文字",
            f"{name} 能打开，但一个字都没抽到（多半整份都是图片或图表）。",
            ["把要给它看的那几段直接粘进输入框", "确认文档里的字不是截图"],
            {"filename": name},
        )
    log.info("doctext.extracted", filename=name, chars=len(text), truncated=truncated)
    return Extracted(
        filename=name,
        kind=suffix.lstrip("."),
        kind_label=KINDS[suffix],
        text=text,
        chars=len(text),
        truncated=truncated,
        notes=notes,
    )


def _join(lines: Iterator[str], limit: int) -> tuple[str, bool]:
    """一行一行攒到上限为止。连续空行压成一行——空行占的是模型的上下文。"""
    out: list[str] = []
    total = 0
    blank = 0
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            blank += 1
            if blank > 1 or not out:
                continue
        else:
            blank = 0
        if total + len(line) + 1 > limit:
            return "\n".join(out).rstrip(), True
        out.append(line)
        total += len(line) + 1
    return "\n".join(out).rstrip(), False


def _zip(data: bytes, name: str) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "附件打不开",
            f"{name} 不是一个完整的 Office 文件（zip 结构读不出来）：{exc}",
            ["确认文件没有传坏，重新选一次", "如果它其实是别的格式，请用对应的后缀重存一份"],
            {"filename": name},
        ) from exc


def _member(zf: zipfile.ZipFile, path: str, name: str) -> bytes:
    """读 zip 里的一个成员。缺了就说清缺的是哪个，别把 KeyError 抛到用户脸上。"""
    try:
        info = zf.getinfo(path)
    except KeyError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "附件的结构不对",
            f"{name} 里没有 {path}——它可能不是 Office 存出来的文件。",
            ["用 Word / Excel 打开它再另存一次", "或者把文字直接粘进输入框"],
            {"filename": name, "member": path},
        ) from exc
    if info.file_size > MEMBER_MAX:
        size = info.file_size / 1024 / 1024
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "附件里有一块太大了",
            f"{name} 的 {path} 解开有 {size:.0f} MB，超过 {MEMBER_MAX // 1024 // 1024} MB 不读。",
            ["把要给它看的那部分另存成一份小文件", "或者只粘贴文字"],
            {"filename": name, "member": path, "bytes": info.file_size},
        )
    return zf.read(path)


def _xml(raw: bytes, name: str, part: str) -> ET.Element:
    """xml → 树。`xml.etree` 默认不解析外部实体，这里也不需要更多。"""
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AppError(
            ErrorCode.VALIDATION_ERROR,
            "附件读到一半坏了",
            f"{name} 的 {part} 不是合法的 xml：{exc}",
            ["用 Word / Excel 打开它再另存一次", "或者把文字直接粘进输入框"],
            {"filename": name, "member": part},
        ) from exc


# --- Word ---

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx(data: bytes, name: str) -> Iterator[str]:
    """一段一行。

    **页眉 / 页脚 / 批注刻意不读**：它们每页都重复，抽进正文只会把同一行复制几十遍。
    """
    with _zip(data, name) as zf:
        root = _xml(_member(zf, "word/document.xml", name), name, "word/document.xml")
    body = root.find(f"{_W}body")
    if body is None:
        return
    yield from _docx_block(body)


def _docx_block(node: ET.Element) -> Iterator[str]:
    for child in node:
        if child.tag == f"{_W}p":
            yield _docx_para(child)
        elif child.tag == f"{_W}tbl":
            yield from _docx_table(child)
        elif child.tag in (f"{_W}sdt", f"{_W}sdtContent"):  # 内容控件：里面还是段落与表格
            yield from _docx_block(child)


def _docx_para(p: ET.Element) -> str:
    out: list[str] = []
    for node in p.iter():
        if node.tag == f"{_W}t":
            out.append(node.text or "")
        elif node.tag == f"{_W}tab":
            out.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            out.append(" ")  # 段内换行压成空格：行的粒度留给段落
    return "".join(out)


def _docx_table(tbl: ET.Element) -> Iterator[str]:
    """一行一行，单元格用 ` | ` 连起来——Word 里的表格是分镜表最常见的样子。"""
    for row in tbl.findall(f"{_W}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{_W}tc"):
            paras = [_docx_para(p).strip() for p in cell.findall(f"{_W}p")]
            cells.append(" ".join(t for t in paras if t))
        line = " | ".join(cells)
        if line.strip(" |"):
            yield line


# --- Excel ---

_SS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _xlsx(data: bytes, name: str, notes: list[str]) -> Iterator[str]:
    """每张表一段，行用制表符分列。**空单元格补空串**：少一列就串行。"""
    with _zip(data, name) as zf:
        members = set(zf.namelist())
        shared = _xlsx_shared(zf, name) if "xl/sharedStrings.xml" in members else []
        sheets = _xlsx_sheets(zf, members, name)
        if not sheets:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这份表格里没有工作表",
                f"{name} 打开了，但里面一张表都没有。",
                ["确认文件没有传坏", "或者把那几行直接粘进输入框"],
                {"filename": name},
            )
        notes.append("单元格取的是存着的原始值：日期与公式结果就是表里那个数，不做格式化。")
        if len(sheets) > 1:
            notes.append(f"{len(sheets)} 张工作表都读了，每张前面有一行「## 表名」。")
        for title, path in sheets:
            yield f"## {title}"
            empty = True
            for line in _xlsx_rows(zf, path, shared, name):
                empty = False
                yield line
            if empty:
                yield "（这张表是空的）"
            yield ""


def _xlsx_sheets(zf: zipfile.ZipFile, members: set[str], name: str) -> list[tuple[str, str]]:
    """工作表清单：名字与顺序照 `xl/workbook.xml`，文件路径靠 rels 映射。

    清单或 rels 缺了也不放弃——退回按 `sheet1.xml` / `sheet2.xml` 的文件名排，
    宁可表名不好看，也别一个字都读不出来。
    """
    fallback = sorted(
        p for p in members if p.startswith("xl/worksheets/sheet") and p.endswith(".xml")
    )
    guess = [(p.rsplit("/", 1)[-1][:-4], p) for p in fallback]
    if "xl/workbook.xml" not in members:
        return guess
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in members:
        part = "xl/_rels/workbook.xml.rels"
        for rel in _xml(_member(zf, part, name), name, part):
            rid, target = rel.get("Id"), (rel.get("Target") or "").lstrip("/")
            if rid and target:
                rels[rid] = f"xl/{target.removeprefix('xl/')}"
    out: list[tuple[str, str]] = []
    book = _xml(_member(zf, "xl/workbook.xml", name), name, "xl/workbook.xml")
    for n, sheet in enumerate(book.iterfind(f"{_SS}sheets/{_SS}sheet"), start=1):
        path = rels.get(sheet.get(f"{_REL}id") or "") or f"xl/worksheets/sheet{n}.xml"
        if path in members:
            out.append((sheet.get("name") or f"Sheet{n}", path))
    return out or guess


def _xlsx_rows(zf: zipfile.ZipFile, path: str, shared: list[str], name: str) -> Iterator[str]:
    root = _xml(_member(zf, path, name), name, path)
    for row in root.iterfind(f"{_SS}sheetData/{_SS}row"):
        cells: list[str] = []
        for c in row.findall(f"{_SS}c"):
            at = _col_index(c.get("r") or "")
            if at < 0:
                at = len(cells)
            while len(cells) <= at:
                cells.append("")
            cells[at] = _xlsx_value(c, shared)
        line = "\t".join(cells).rstrip()
        if line.strip():
            yield line


def _col_index(ref: str) -> int:
    """`B3` → 1（第二列）。认不出来时回 -1，由调用方按「接在后面」处理。"""
    letters = "".join(ch for ch in ref.upper() if ch.isalpha())
    if not letters:
        return -1
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _xlsx_value(c: ET.Element, shared: list[str]) -> str:
    kind = c.get("t") or "n"
    if kind == "inlineStr":
        node = c.find(f"{_SS}is")
        return _xlsx_text(node) if node is not None else ""
    v = c.find(f"{_SS}v")
    raw = (v.text or "") if v is not None else ""
    if kind == "s":  # 共享字符串表里的下标
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "b":
        return "TRUE" if raw.strip() in ("1", "true", "TRUE") else "FALSE"
    return raw  # 数字 / 公式结果 / `#REF!` 之类的错误值都原样给


def _xlsx_shared(zf: zipfile.ZipFile, name: str) -> list[str]:
    part = "xl/sharedStrings.xml"
    root = _xml(_member(zf, part, name), name, part)
    return [_xlsx_text(si) for si in root.findall(f"{_SS}si")]


def _xlsx_text(node: ET.Element) -> str:
    """一格里的文字。`rPh`（日文注音）不要——它是同一段话的读音，抽进来等于重复一遍。"""
    out: list[str] = []
    for child in node:
        if child.tag == f"{_SS}t":
            out.append(child.text or "")
        elif child.tag == f"{_SS}r":
            out.extend(sub.text or "" for sub in child.findall(f"{_SS}t"))
    return "".join(out)


# --- PowerPoint ---

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx(data: bytes, name: str, notes: list[str]) -> Iterator[str]:
    """一页一段。**备注页与图上的字不读**：前者不是要给人看的内容，后者是像素。"""
    with _zip(data, name) as zf:
        slides = sorted(
            (p for p in zf.namelist() if p.startswith("ppt/slides/slide") and p.endswith(".xml")),
            key=_slide_no,
        )
        if not slides:
            raise AppError(
                ErrorCode.VALIDATION_ERROR,
                "这份演示里没有幻灯片",
                f"{name} 打开了，但一页都没有。",
                ["确认文件没有传坏", "或者把要给它看的那几页文字粘进输入框"],
                {"filename": name},
            )
        notes.append("只读了每页上的文字，备注页与图片里的字不算。")
        for n, path in enumerate(slides, start=1):
            root = _xml(_member(zf, path, name), name, path)
            yield f"## 第 {n} 页"
            for para in root.iter(f"{_A}p"):
                line = "".join(t.text or "" for t in para.iter(f"{_A}t")).strip()
                if line:
                    yield line
            yield ""


def _slide_no(path: str) -> tuple[int, str]:
    """`slide10.xml` 要排在 `slide9.xml` 后面，所以按数字排而不是按字符串。"""
    digits = "".join(ch for ch in path.rsplit("/", 1)[-1] if ch.isdigit())
    return (int(digits) if digits else 0, path)


# --- 纯文本族 ---

#: 解码顺序。中文 Windows 上导出的 csv 多半是 GB18030，先试 UTF-8 才不会把它认错。
_ENCODINGS = ("utf-8-sig", "gb18030")


def _plain(data: bytes, suffix: str, notes: list[str]) -> Iterator[str]:
    text, encoding = _decode(data)
    if encoding != "utf-8":
        notes.append(f"这个文件不是 UTF-8，按 {encoding} 读的；出现乱码请存成 UTF-8 再传。")
    if suffix in (".csv", ".tsv"):
        yield from _table(text, "\t" if suffix == ".tsv" else ",")
        return
    yield from text.splitlines()


def _decode(data: bytes) -> tuple[str, str]:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # UTF-16 的 BOM：先认它，否则会被当成 GB18030
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), "utf-8" if enc == "utf-8-sig" else enc
        except UnicodeDecodeError:
            continue
    # 一种都不成时用替换字符兜底，并如实说出来——半份能读的文档比「整份读不了」有用。
    return data.decode("utf-8", errors="replace"), "utf-8（个别字符读不出，已用 ? 代替）"


def _table(text: str, delimiter: str) -> Iterator[str]:
    """csv / tsv 一律转成制表符分列，与 xlsx 那侧同一个形状。"""
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        line = "\t".join(cell.strip() for cell in row).rstrip()
        if line.strip():
            yield line
