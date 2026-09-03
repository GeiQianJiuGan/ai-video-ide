"""AI 协作栏的附件：一份 Word / Excel / PPT → 一段能填进输入框的纯文本。

这个文件盯的是**边界**，不是「抽得漂不漂亮」：

  1. **抽不了的直接说抽不了**（硬约束 4）：`.pdf` / `.doc` 报四要素错误并给出
     「另存为 .docx / .txt」这条出路，绝不返回一段乱码充数；
  2. **表格不许串列**：Excel 里空着的那一格必须占位，否则「第 3 镜的时长」会被
     读成别的东西；
  3. **什么都不落**：抽完库里一条记录都不多——它只是输入框里的一段草稿；
  4. **不要求配好 LLM**：默认 `llm_provider="none"`，附件照样抽得出来（硬约束 2）。
     用户得先看见抽出来什么，才决定发不发。

样例文件全部在内存里拼（Office 那三种就是 zip + xml），不往仓库塞二进制。
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.conftest import error_of

API = "/api/v1"

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
SS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"


def _zip(members: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        for path, text in members.items():
            zf.writestr(path, text)
    return buf.getvalue()


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def docx(body: str) -> bytes:
    doc = f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    return _zip({"word/document.xml": doc})


def attach(client: TestClient, pid: str, name: str, data: bytes) -> object:
    return client.post(
        f"{API}/projects/{pid}/director/attach",
        files={"file": (name, data, "application/octet-stream")},
    )


def xlsx() -> bytes:
    """一张「分镜」表：表头三格走共享字符串，第二行故意空着 B 列。"""
    shared = f'<sst xmlns="{SS}"><si><t>镜号</t></si><si><t>时长</t></si><si><t>内容</t></si></sst>'
    sheet = (
        f'<worksheet xmlns="{SS}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c></row>'
        '<row r="2"><c r="A2"><v>1</v></c>'
        '<c r="C2" t="inlineStr"><is><t>雨夜追车</t></is></c></row>'
        "</sheetData></worksheet>"
    )
    book = (
        f'<workbook xmlns="{SS}" xmlns:r="{REL}"><sheets>'
        '<sheet name="分镜" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    rels = (
        f'<Relationships xmlns="{PKG}">'
        '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'
    )
    return _zip(
        {
            "xl/sharedStrings.xml": shared,
            "xl/worksheets/sheet1.xml": sheet,
            "xl/workbook.xml": book,
            "xl/_rels/workbook.xml.rels": rels,
        }
    )


def pptx(*pages: str) -> bytes:
    members = {
        f"ppt/slides/slide{n}.xml": f'<sld xmlns:a="{A}"><a:p><a:r><a:t>{text}</a:t>'
        "</a:r></a:p></sld>"
        for n, text in enumerate(pages, start=1)
    }
    return _zip(members)


def test_docx_keeps_paragraphs_and_tables(client: TestClient, pid: str) -> None:
    """段落一行一条；表格一行一条，单元格用 ` | ` 连起来——分镜表最常见的样子。"""
    table = (
        "<w:tbl><w:tr>"
        f"<w:tc>{_p('1')}</w:tc><w:tc>{_p('3s')}</w:tc><w:tc>{_p('推镜')}</w:tc>"
        "</w:tr></w:tbl>"
    )
    resp = attach(client, pid, "剧本.docx", docx(_p("第一幕 雨夜") + _p("阿岚推门进来。") + table))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "docx"
    assert body["kind_label"] == "Word 文档"
    assert body["truncated"] is False
    assert body["chars"] == len(body["text"])
    lines = body["text"].splitlines()
    assert "第一幕 雨夜" in lines
    assert "阿岚推门进来。" in lines
    assert "1 | 3s | 推镜" in lines


def test_xlsx_never_shifts_columns(client: TestClient, pid: str) -> None:
    """空着的 B2 必须占一格。串了列，「第 3 镜的时长」就会被读成内容。"""
    resp = attach(client, pid, "分镜表.xlsx", xlsx())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind_label"] == "Excel 表格"
    lines = body["text"].splitlines()
    assert "## 分镜" in lines
    assert "镜号\t时长\t内容" in lines
    assert "1\t\t雨夜追车" in lines
    assert any("原始值" in note for note in body["notes"]), body["notes"]


def test_pptx_marks_page_numbers(client: TestClient, pid: str) -> None:
    resp = attach(client, pid, "提案.pptx", pptx("整片结构", "第二幕：雨夜追车"))
    assert resp.status_code == 200, resp.text
    lines = resp.json()["text"].splitlines()
    assert lines[0] == "## 第 1 页"
    assert "整片结构" in lines
    assert "## 第 2 页" in lines


def test_csv_in_gb18030_says_so(client: TestClient, pid: str) -> None:
    """中文 Windows 导出的 csv 多半是 GB18030。读得出来，但必须说清是按什么读的。"""
    raw = "镜号,内容\n1,雨夜追车\n".encode("gb18030")
    resp = attach(client, pid, "分镜.csv", raw)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "1\t雨夜追车" in body["text"].splitlines()
    assert any("gb18030" in note for note in body["notes"]), body["notes"]


def test_pdf_is_refused_with_a_way_out(client: TestClient, pid: str) -> None:
    """抽不了就说抽不了，并给出出路——绝不返回一段乱码让人以为 AI 看到了那份文档。"""
    resp = attach(client, pid, "剧本.pdf", b"%PDF-1.7 blah")
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert ".pdf" in err["title"]
    assert any("另存为" in s for s in err["suggestions"]), err["suggestions"]


def test_old_office_and_unknown_suffix_both_explain_themselves(
    client: TestClient, pid: str
) -> None:
    doc = error_of(attach(client, pid, "旧剧本.doc", b"\xd0\xcf\x11\xe0blah"))
    assert any(".docx" in s for s in doc["suggestions"]), doc["suggestions"]
    weird = attach(client, pid, "素材.psd", b"8BPS")
    assert weird.status_code == 422
    err = error_of(weird)
    assert "不认识" in err["title"]
    assert any(".docx" in s or ".txt" in s for s in err["suggestions"])


def test_truncation_is_reported(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """超长必须截断，而且必须说出来——少了半份文档，用户有权在发送之前就知道。"""
    monkeypatch.setattr(settings, "director_attach_max_chars", 500)
    resp = attach(client, pid, "长剧本.txt", ("第一幕 雨夜追车。\n" * 400).encode())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["truncated"] is True
    assert body["chars"] <= 500
    assert any("只取了前" in note for note in body["notes"]), body["notes"]


def test_too_big_is_refused_before_parsing(
    client: TestClient, pid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "director_attach_max_mb", 1)
    resp = attach(client, pid, "整部片子.docx", b"x" * (2 * 1024 * 1024))
    assert resp.status_code == 422
    err = error_of(resp)
    assert "太大" in err["title"]
    assert any("AIVS_DIRECTOR_ATTACH_MAX_MB" in s for s in err["suggestions"])


def test_a_docx_without_text_says_so(client: TestClient, pid: str) -> None:
    """整份都是图的文档能打开却一个字都没有。那不是成功，是一条要说清的失败。"""
    resp = attach(client, pid, "全是图.docx", docx(_p("") + _p("   ")))
    assert resp.status_code == 422
    err = error_of(resp)
    assert "没有文字" in err["title"]


def test_a_broken_zip_names_the_problem(client: TestClient, pid: str) -> None:
    resp = attach(client, pid, "坏的.docx", b"not a zip at all")
    assert resp.status_code == 422
    assert "打不开" in error_of(resp)["title"]


def test_attach_persists_nothing_and_needs_no_llm(client: TestClient, pid: str) -> None:
    """默认 `llm_provider="none"`：附件照样抽得出来，而库里一条记录都不多。"""
    before = client.get(f"{API}/projects/{pid}/director")
    assert before.status_code == 200
    assert before.json()["llm"]["configured"] is False
    assert attach(client, pid, "剧本.txt", "第一幕 雨夜\n".encode()).status_code == 200
    after = client.get(f"{API}/projects/{pid}/director").json()
    assert after["turns"] == []
    assert len(after["turns"]) == len(before.json()["turns"])


def test_history_publishes_what_attach_accepts(client: TestClient, pid: str) -> None:
    """前端的 `accept` 只认后端这一份，别在界面上再写第二张后缀清单。"""
    info = client.get(f"{API}/projects/{pid}/director").json()["attach"]
    assert ".docx" in info["accept"] and ".xlsx" in info["accept"]
    assert ".pdf" not in info["accept"]
    assert info["max_mb"] == settings.director_attach_max_mb
    assert info["max_chars"] == settings.director_attach_max_chars
    assert any(k["suffix"] == ".docx" and k["label"] for k in info["kinds"])


def test_attach_needs_an_open_project(client: TestClient) -> None:
    resp = attach(client, "prj_notopen", "剧本.txt", b"hi")
    assert resp.status_code == 404
    error_of(resp)
