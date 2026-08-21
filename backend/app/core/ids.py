"""ULID 主键，带类型前缀。时间可排序，便于按创建顺序天然排序。"""

from __future__ import annotations

from ulid import ULID

PREFIX = {
    "project": "prj",
    "character": "chr",
    "appearance": "app",
    "sheet_version": "shv",
    "location": "loc",
    "location_variant": "var",
    "location_reference": "lrf",
    "prop": "prp",
    "prop_reference": "prf",
    "story": "sty",
    "scene": "scn",
    "shot": "sht",
    "prompt_version": "prm",
    "generation_version": "gen",
    "job": "job",
    "workflow": "wf",
    "asset": "ast",
    "timeline": "tml",
    "track": "trk",
    "timeline_clip": "tcl",
    "transition": "trn",
    "clip_effect": "eff",
    "subtitle": "sub",
    "clip": "clp",
    "import_session": "imp",
    "shot_cast": "scs",
    "shot_prop": "spp",
    "scene_cast": "scc",
    "scene_location": "scl",
    "asset_ref": "arf",
    "export_record": "exp",
    "request": "req",
    # --- 幕流程图（场景之间的衔接 + AI 导演会话） ---
    "scene_link": "lnk",
    "director_turn": "dtn",
    # --- 应用级素材库（独立目录 + library.db，不属于任何工程） ---
    "library": "lib",
    "library_asset": "las",
    "library_asset_ref": "lar",
    "library_character": "lch",
    "library_appearance": "lap",
    "library_sheet": "lsh",
    "library_location": "llc",
    "library_location_variant": "lvr",
    "library_location_reference": "llr",
    "library_prop": "lpp",
    "library_prop_reference": "lpr",
    "library_tag": "ltg",
    "library_tag_link": "ltl",
}


def new_id(kind: str) -> str:
    """生成带前缀的 ULID，例如 new_id("shot") -> 'sht_01J...'。"""
    prefix = PREFIX.get(kind)
    if prefix is None:
        raise ValueError(f"未登记的 id 类型: {kind}（请在 app/core/ids.py PREFIX 中登记）")
    return f"{prefix}_{ULID()}"


def kind_of(ident: str) -> str | None:
    """从 id 反推类型，用于日志与错误定位。"""
    head = ident.split("_", 1)[0]
    for kind, prefix in PREFIX.items():
        if prefix == head:
            return kind
    return None
