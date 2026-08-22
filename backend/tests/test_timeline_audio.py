"""时间线的音频轨与裁剪（Step 8 的补完）。

这一轮补上的是「时间线编辑太简陋」里剩下的几件事，其中三件在后端有真正的边界：

  1. **拆声音是真的抽一份音频**——AI 生成的视频绝大多数根本没有音轨，让音频轨指回
     原视频只会得到一条「看着有片段却一点声音都没有」的假轨。没有音轨时必须明说，
     而不是造一条静音轨糊过去；
  2. **音频轨之间可以叠加**——同一个时间段被占住时自动开新的音频轨（A2 / A3），
     「叠加」在数据上就是「同一时间多条轨道各有一段」；
  3. **拖左边缘裁剪是一次请求**——`start` 与 `in_point` 必须一起改完，否则中间会出现
     一个错的状态，撤销栈上还会占两格（撤一次只回到一半）；
  4. **撤销必须连轨道一起回滚**——拆声音会自动建轨道，快照只存片段的话，撤销之后
     会剩下一堆挂在已经不存在的轨道上的片段，`_shape` 把它们悄悄跳过，用户看到的
     就是「撤销把我的片段吃掉了」。

前半段用真 FFmpeg 走 service（和 tests/test_frames.py 一样：aiosqlite 的连接要和调用
在同一个事件循环里建），后半段的轨道 / 裁剪 / 混音走 TestClient。两种不要混在一个测试里。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import ffmpeg as ffmpeg_tool
from app.core.errors import AppError
from app.services.assets import assets as asset_service
from app.services.audio import Probe, audio
from app.services.projects import projects
from app.services.timeline import timeline as timeline_service
from tests.conftest import error_of, upload_png


def ffmpeg_or_skip() -> str:
    located = ffmpeg_tool.locate("ffmpeg")
    if not located.available:
        pytest.skip("这台机器上没有 FFmpeg（跑一次 scripts/fetch_ffmpeg.py）")
    return located.path or "ffmpeg"


async def make_project(tmp_path: Path) -> str:
    proj = await projects.create(str(tmp_path / "film"), "音频轨测试片", 320, 240, 25.0, "frames")
    return proj.id


async def make_clip(
    binary: str, target: Path, seconds: float = 2.0, *, with_audio: bool = True
) -> Path:
    """用 lavfi 造一段真视频。`with_audio=False` 造一段无声的（AI 成片的常态）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    args = [binary, "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=64x64:rate=10"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    args += ["-pix_fmt", "yuv420p", "-shortest", str(target)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    assert target.is_file(), (  # noqa: ASYNC240 - 测试里的本地文件检查
        f"造样片失败：{(stderr or b'').decode('utf-8', 'replace')[-600:]}"
    )
    return target


async def video_track(pid: str) -> dict[str, Any]:
    timeline = await timeline_service.get(pid)
    return next(t for t in timeline["tracks"] if t["kind"] == "video")


async def place(pid: str, asset_id: str, *, start: float = 0.0) -> dict[str, Any]:
    """把一段素材放到视频轨上，返回那个片段。"""
    track = await video_track(pid)
    out = await timeline_service.add_clip(pid, track["id"], asset_id, start=start)
    return next(c for c in (await video_track(pid))["clips"] if c["id"] == out["clip_id"])


def tracks_of(timeline: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [t for t in timeline["tracks"] if t["kind"] == kind]


# --- 拆声音（真 FFmpeg） ---


async def test_detach_audio_makes_an_independent_clip_and_mutes_the_picture(
    tmp_path: Path,
) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4"))
    )
    clip = await place(pid, src["id"])

    out = await timeline_service.detach_audio(pid, clip["id"])
    assert out["created_track"] is False, "A1 是空的，不该为它另开一条轨道"
    assert out["track_name"] == "A1"

    audio_track = next(t for t in out["timeline"]["tracks"] if t["id"] == out["track_id"])
    detached = next(c for c in audio_track["clips"] if c["id"] == out["audio_clip_id"])
    assert (detached["start"], detached["duration"]) == (clip["start"], clip["duration"]), (
        "拆出来的声音必须和画面对齐，否则一放就错位"
    )
    assert detached["source_clip_id"] == clip["id"]
    assert detached["source_missing"] is False
    assert detached["track_kind"] == "audio"

    picture = next(c for c in (await video_track(pid))["clips"] if c["id"] == clip["id"])
    assert picture["muted"] == 1, "声音已经挪到音频轨上了，画面自己不能再出一遍"
    assert picture["detached_audio_clip_id"] == out["audio_clip_id"], (
        "源片段要能指回那段声音，前端不该自己配对"
    )

    [asset] = await asset_service.list_assets(pid, kind="clip_audio")
    assert asset["id"] == out["asset_id"]
    assert asset["path"].startswith("cache/audio/"), (
        "拆出来的音频是临时资源，落 cache/ 而不是 assets/（见 KIND_DIR['clip_audio']）"
    )
    assert (projects.get(pid).dir / asset["path"]).stat().st_size > 0

    ledger = {a["id"] for a in await asset_service.list_assets(pid)}
    assert src["id"] in ledger
    assert asset["id"] not in ledger, "临时音频不进资产总账"
    assert asset["id"] not in {a["id"] for a in await asset_service.orphans(pid)}, (
        "它从来没有 AssetRef，算进孤儿只会把「可以回收的文件」这份清单刷满"
    )


async def test_detaching_twice_is_a_conflict_not_a_second_copy(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4"))
    )
    clip = await place(pid, src["id"])
    first = await timeline_service.detach_audio(pid, clip["id"])

    with pytest.raises(AppError) as caught:
        await timeline_service.detach_audio(pid, clip["id"])
    err = caught.value
    assert err.code == "CONFLICT"
    assert err.related_ids["audio_clip_id"] == first["audio_clip_id"], (
        "要能直接跳到已经拆出来的那一段，而不是让人自己去音频轨上找"
    )
    assert err.suggestions


async def test_overlapping_detaches_stack_onto_a_new_audio_track(tmp_path: Path) -> None:
    """A1 在这个时间段占着 → 自动开 A2。「音频轨之间可以叠加」就是这么实现的。"""
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    first_src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4", 2.0))
    )
    second_src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第二幕.mp4", 2.5))
    )
    early = await place(pid, first_src["id"], start=0.0)
    overlapping = await place(pid, second_src["id"], start=1.0)

    await timeline_service.detach_audio(pid, early["id"])
    second = await timeline_service.detach_audio(pid, overlapping["id"])
    assert second["created_track"] is True
    assert second["track_name"] == "A2"

    audio_tracks = tracks_of(second["timeline"], "audio")
    assert [t["name"] for t in audio_tracks] == ["A1", "A2"]
    assert [len(t["clips"]) for t in audio_tracks] == [1, 1], "两段声音各占一条轨道，才叠得起来"


async def test_a_silent_video_says_so_instead_of_making_a_silent_track(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid,
        "generated_video",
        str(await make_clip(binary, tmp_path / "raw" / "无声.mp4", with_audio=False)),
    )
    clip = await place(pid, src["id"])

    with pytest.raises(AppError) as caught:
        await timeline_service.detach_audio(pid, clip["id"])
    err = caught.value
    assert err.code == "VALIDATION_ERROR"
    assert "没有声音" in err.title
    assert any("导入" in s for s in err.suggestions), "拆不出来时要指出另一条路：导入一段音频"
    assert await asset_service.list_assets(pid, kind="clip_audio") == [], "失败不该留下半个文件"
    assert tracks_of(await timeline_service.get(pid), "audio")[0]["clips"] == []
    picture = next(c for c in (await video_track(pid))["clips"] if c["id"] == clip["id"])
    assert picture["muted"] == 0, "没拆成功就不能把画面静音——那样连原来的声音都没了"


async def test_extracting_the_same_audio_twice_reuses_the_file(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4"))
    )
    first = await audio.extract(pid, src["id"])
    again = await audio.extract(pid, src["id"])
    assert first["reused"] is False
    assert again["id"] == first["id"]
    assert again["reused"] is True, "同一段素材反复拆时不该攒出一堆一模一样的 m4a"
    assert len(await asset_service.list_assets(pid, kind="clip_audio")) == 1


async def test_deleting_the_video_takes_its_detached_audio_with_it(tmp_path: Path) -> None:
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4"))
    )
    extracted = await audio.extract(pid, src["id"])
    root = projects.get(pid).dir
    assert (root / extracted["path"]).is_file()

    out = await asset_service.delete(pid, src["id"])
    assert [r["id"] for r in out["derived_removed"]] == [extracted["id"]], (
        "源成片一删，从它拆的声音也得删——而且要在返回值里说出来，绝不静默连带删除"
    )
    assert not (root / extracted["path"]).exists()
    assert await asset_service.list_assets(pid, kind="clip_audio") == []


async def test_deleting_only_the_audio_keeps_the_video(tmp_path: Path) -> None:
    """反过来不成立：删掉拆出来的声音不动成片，需要时再拆一次就有。"""
    binary = ffmpeg_or_skip()
    pid = await make_project(tmp_path)
    src = await asset_service.register_path(
        pid, "generated_video", str(await make_clip(binary, tmp_path / "raw" / "第一幕.mp4"))
    )
    extracted = await audio.extract(pid, src["id"])
    out = await asset_service.delete(pid, extracted["id"])
    assert out["derived_removed"] == []
    assert (projects.get(pid).dir / src["path"]).is_file()
    again = await audio.extract(pid, src["id"])
    assert again["reused"] is False, "文件已经跟着登记一起删了，应该重新拆一份"


async def test_without_ffmpeg_detaching_fails_loudly(tmp_path: Path, no_ffmpeg: None) -> None:
    pid = await make_project(tmp_path)
    fake = tmp_path / "raw" / "第一幕.mp4"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"fake-mp4-bytes")
    src = await asset_service.register_path(pid, "generated_video", str(fake))

    with pytest.raises(AppError) as caught:
        await audio.extract(pid, src["id"])
    assert caught.value.code == "FFMPEG_MISSING"
    assert caught.value.suggestions


async def test_peek_never_raises_so_export_can_degrade_to_a_warning(
    tmp_path: Path, no_ffmpeg: None
) -> None:
    """探测失败不是崩溃：导出预检要把它变成一条警告，而不是断言「这段没有声音」。"""
    fake = tmp_path / "raw" / "第一幕.mp4"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_bytes(b"fake-mp4-bytes")
    assert await audio.peek(fake) is None, "ffprobe 不在 → 不知道，不是「没有」"
    assert await audio.peek(tmp_path / "根本不存在.mp4") is None


# --- 轨道、裁剪与混音（REST 层） ---


@pytest.fixture
def fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """把探测换成一个只看后缀的假实现。

    REST 层的「成片」是 1×1 PNG，「配乐」是随手造的 `.m4a`——真 ffprobe 看的是内容，
    在装了 FFmpeg 的机器上会说「这个 m4a 没有声音」，在没装的机器上又什么都不知道，
    同一套断言两台机器结论相反（测试约定里明确不许这样）。这里把探测钉死成后缀判断，
    让轨道 / 裁剪 / 混音的行为可测；真抽取的用例在上半段用真 FFmpeg 跑。
    时长一律回 `None`：add_clip 「长度必须能证明」那条规则才测得到。
    """

    async def peek(path: Path) -> Probe | None:
        if path.suffix.lower() in {".m4a", ".mp3", ".wav"}:
            return Probe(has_audio=True, duration=None, codec="aac")
        return Probe(has_audio=False, duration=None, codec=None)

    monkeypatch.setattr(audio, "peek", peek)


def make_clips(client: TestClient, pid: str, *durations: float) -> list[dict[str, Any]]:
    """造几个有当前版本的镜头并装配，返回视频轨上的片段。"""
    scene = client.post(f"/api/v1/projects/{pid}/scenes", json={"title": "第一幕"}).json()
    for i, duration in enumerate(durations, start=1):
        shot = client.post(
            f"/api/v1/projects/{pid}/scenes/{scene['id']}/shots", json={"title": f"S{i}"}
        ).json()
        resp = client.post(
            f"/api/v1/projects/{pid}/shots/{shot['id']}/versions",
            json={
                "asset_id": upload_png(client, pid, "generated_video", f"clip{i}.png"),
                "kind": "video",
                "duration": duration,
            },
        )
        assert resp.status_code == 201, resp.text
    resp = client.post(f"/api/v1/projects/{pid}/timeline/assemble", json={"replace": True})
    assert resp.status_code == 200, resp.text
    video = next(t for t in resp.json()["timeline"]["tracks"] if t["kind"] == "video")
    return list(video["clips"])


def track_named(client: TestClient, pid: str, name: str) -> dict[str, Any]:
    timeline = client.get(f"/api/v1/projects/{pid}/timeline").json()
    return next(t for t in timeline["tracks"] if t["name"] == name)


def clip_in(timeline: dict[str, Any], clip_id: str) -> dict[str, Any]:
    return next(c for t in timeline["tracks"] for c in t["clips"] if c["id"] == clip_id)


def add_music(client: TestClient, pid: str, *, duration: float | None, start: float = 0.0) -> Any:
    audio_track = track_named(client, pid, "A1")
    return client.post(
        f"/api/v1/projects/{pid}/tracks/{audio_track['id']}/clips",
        json={
            "asset_id": upload_png(client, pid, "audio", "bgm.m4a"),
            "start": start,
            "duration": duration,
        },
    )


def test_left_edge_trim_moves_start_and_in_point_in_one_step(client: TestClient, pid: str) -> None:
    """拖左边缘：`in_point` 与 `start` 一起改，一次请求、一格撤销。"""
    clips = make_clips(client, pid, 4.0, 4.0)
    resp = client.post(
        f"/api/v1/projects/{pid}/clips/{clips[1]['id']}/trim",
        json={"in_point": 1.0, "out_point": 4.0, "start": 5.0, "ripple": False},
    )
    assert resp.status_code == 200, resp.text
    edited = clip_in(resp.json(), clips[1]["id"])
    assert (edited["in_point"], edited["duration"], edited["start"]) == (1.0, 3.0, 5.0), (
        "往右拖左边缘：从源素材的第 1 秒开始，在时间线的第 5 秒落位，长度 3 秒"
    )

    undone = client.post(f"/api/v1/projects/{pid}/timeline/undo")
    assert undone.status_code == 200, undone.text
    back = clip_in(undone.json(), clips[1]["id"])
    assert (back["in_point"], back["duration"], back["start"]) == (0.0, 4.0, 4.0), (
        "一次拖动只该占撤销栈一格，撤一次就整段回去"
    )


def test_trim_start_snaps_to_the_neighbour_edge(client: TestClient, pid: str) -> None:
    clips = make_clips(client, pid, 4.0, 4.0)
    resp = client.post(
        f"/api/v1/projects/{pid}/clips/{clips[1]['id']}/trim",
        json={"in_point": 0.05, "out_point": 4.0, "start": 4.05, "ripple": False},
    )
    assert resp.status_code == 200, resp.text
    assert clip_in(resp.json(), clips[1]["id"])["start"] == 4.0, (
        "手拖的坐标永远差那么几毫秒，落位要吸附到前一段的末尾"
    )


def test_tracks_are_auto_numbered_and_the_last_video_track_is_protected(
    client: TestClient, pid: str
) -> None:
    added = client.post(f"/api/v1/projects/{pid}/tracks", json={"kind": "audio"})
    assert added.status_code == 201, added.text
    assert added.json()["track"]["name"] == "A2", "A1 占着就该叫 A2"
    assert [t["name"] for t in added.json()["timeline"]["tracks"]] == ["V1", "A1", "A2"]

    named = client.post(f"/api/v1/projects/{pid}/tracks", json={"kind": "audio", "name": "配乐"})
    assert named.status_code == 201, named.text
    assert named.json()["track"]["name"] == "配乐"

    bad = client.post(f"/api/v1/projects/{pid}/tracks", json={"kind": "光轨"})
    assert bad.status_code == 422
    assert error_of(bad)["code"] == "VALIDATION_ERROR"

    video = track_named(client, pid, "V1")
    refused = client.delete(f"/api/v1/projects/{pid}/tracks/{video['id']}")
    assert refused.status_code == 409
    assert "唯一的视频轨" in error_of(refused)["title"]

    renamed = client.patch(
        f"/api/v1/projects/{pid}/tracks/{video['id']}", json={"name": "主画面", "muted": True}
    )
    assert renamed.status_code == 200, renamed.text
    assert track_named(client, pid, "主画面")["muted"] == 1

    gone = client.delete(f"/api/v1/projects/{pid}/tracks/{added.json()['track']['id']}")
    assert gone.status_code == 200, gone.text
    assert [t["name"] for t in gone.json()["tracks"]] == ["主画面", "A1", "配乐"]

    missing = client.delete(f"/api/v1/projects/{pid}/tracks/trk_nope")
    assert missing.status_code == 404
    assert error_of(missing)["code"] == "NOT_FOUND"


def test_deleting_a_track_with_clips_asks_first_then_undo_brings_it_all_back(
    client: TestClient, pid: str
) -> None:
    """删非空轨道要先问一句；撤销必须把轨道**和它上面的片段**一起还回来。

    快照只存片段的话，这里撤销之后片段会挂在一条已经不存在的轨道上，`_shape` 把它们
    悄悄跳过——用户看到的就是「撤销把我的片段吃掉了」。
    """
    clips = make_clips(client, pid, 4.0)
    video = track_named(client, pid, "V1")
    spare = client.post(f"/api/v1/projects/{pid}/tracks", json={"kind": "video"})
    assert spare.status_code == 201, spare.text
    assert spare.json()["track"]["name"] == "V2"

    refused = client.delete(f"/api/v1/projects/{pid}/tracks/{video['id']}")
    assert refused.status_code == 409
    err = error_of(refused)
    assert err["related_ids"]["confirm"] == "force", "需要确认不是失败：告诉前端重放时加哪个开关"
    assert err["related_ids"]["clips"] == 1

    forced = client.delete(f"/api/v1/projects/{pid}/tracks/{video['id']}?force=true")
    assert forced.status_code == 200, forced.text
    assert [t["name"] for t in forced.json()["tracks"]] == ["A1", "V2"]
    assert forced.json()["duration_total"] == 0.0

    undone = client.post(f"/api/v1/projects/{pid}/timeline/undo")
    assert undone.status_code == 200, undone.text
    restored = next((t for t in undone.json()["tracks"] if t["name"] == "V1"), None)
    assert restored is not None, "轨道也要回来"
    assert [c["id"] for c in restored["clips"]] == [clips[0]["id"]], "片段必须跟着轨道一起回来"
    assert undone.json()["duration_total"] == 4.0


def test_mix_controls_are_validated(client: TestClient, pid: str) -> None:
    clips = make_clips(client, pid, 4.0)
    ok = client.post(
        f"/api/v1/projects/{pid}/clips/{clips[0]['id']}/mix", json={"volume": 0.5, "muted": True}
    )
    assert ok.status_code == 200, ok.text
    clip = clip_in(ok.json(), clips[0]["id"])
    assert (clip["volume"], clip["muted"]) == (0.5, 1)

    loud = client.post(f"/api/v1/projects/{pid}/clips/{clips[0]['id']}/mix", json={"volume": 9.0})
    assert loud.status_code == 422
    assert "音量" in error_of(loud)["title"]
    assert (
        clip_in(client.get(f"/api/v1/projects/{pid}/timeline").json(), clips[0]["id"])["volume"]
        == 0.5
    ), "被拒绝的请求不能留下半个改动"


def test_detaching_only_works_on_a_video_clip(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    """装配出来的片段指向一张 PNG（测试里的假成片）：拆声音必须明确拒绝，不留残迹。"""
    clips = make_clips(client, pid, 4.0)
    resp = client.post(f"/api/v1/projects/{pid}/clips/{clips[0]['id']}/detach-audio")
    assert resp.status_code == 422
    err = error_of(resp)
    assert err["code"] == "VALIDATION_ERROR"
    assert "不是视频" in err["title"]
    assert track_named(client, pid, "A1")["clips"] == []
    assert (
        clip_in(client.get(f"/api/v1/projects/{pid}/timeline").json(), clips[0]["id"])["muted"] == 0
    )

    music = add_music(client, pid, duration=3.0)
    assert music.status_code == 201, music.text
    on_audio = client.post(f"/api/v1/projects/{pid}/clips/{music.json()['clip_id']}/detach-audio")
    assert on_audio.status_code == 422
    assert "只有视频轨" in error_of(on_audio)["title"]


def test_adding_an_audio_clip_needs_a_length_it_can_prove(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    """长度不给、资产上没记、ffprobe 也探不出来时报错——绝不随便填 4 秒。"""
    guessed = add_music(client, pid, duration=None)
    assert guessed.status_code == 422
    assert "多长" in error_of(guessed)["title"]
    assert track_named(client, pid, "A1")["clips"] == []

    placed = add_music(client, pid, duration=6.0)
    assert placed.status_code == 201, placed.text
    clip = clip_in(placed.json()["timeline"], placed.json()["clip_id"])
    assert (clip["duration"], clip["out_point"], clip["label"]) == (6.0, 6.0, "bgm.m4a")
    assert (clip["track_kind"], clip["volume"], clip["muted"]) == ("audio", 1.0, 0)


def test_a_soundless_file_is_refused_on_an_audio_track(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    audio_track = track_named(client, pid, "A1")
    resp = client.post(
        f"/api/v1/projects/{pid}/tracks/{audio_track['id']}/clips",
        json={
            "asset_id": upload_png(client, pid, "upload", "封面.png"),
            "duration": 3.0,
        },
    )
    assert resp.status_code == 422
    err = error_of(resp)
    assert "没有声音" in err["title"]
    assert any("拆出声音" in s for s in err["suggestions"]), "要指出视频的声音该怎么进音频轨"


def test_export_says_the_gap_will_be_closed(client: TestClient, pid: str, fake_probe: None) -> None:
    """轨道区和预览器把空档画出来，导出却用 concat 合掉——这个差别必须说出来。"""
    ffmpeg_or_skip()
    clips = make_clips(client, pid, 2.0, 2.0)
    moved = client.post(f"/api/v1/projects/{pid}/clips/{clips[1]['id']}/move", json={"start": 5.0})
    assert moved.status_code == 200, moved.text

    plan = client.get(f"/api/v1/projects/{pid}/export/command").json()
    note = next((w for w in plan["warnings"] if "空档" in w), None)
    assert note is not None, "预览里有 3 秒黑场、导出后没有，不说清就会被当成 bug"
    assert "3.00 秒" in note
    assert "对不上" not in note, "没有音频轨片段时不必扯到音画对不上"

    assert add_music(client, pid, duration=2.0).status_code == 201
    again = client.get(f"/api/v1/projects/{pid}/export/command").json()
    assert any("对不上" in w for w in again["warnings"]), (
        "音频按绝对位置放，画面合掉空档之后就错位了——有音频轨时要多提这一句"
    )


def test_export_mixes_the_audio_tracks_and_warns_about_what_it_drops(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    ffmpeg_or_skip()  # 命令行要真的拼出来才有得断言
    make_clips(client, pid, 2.0, 2.0)
    assert add_music(client, pid, duration=6.0).status_code == 201

    plan = client.get(f"/api/v1/projects/{pid}/export/command")
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert (body["clips"], body["audio_clips"]) == (2, 1)
    assert "adelay" in body["command"], "音频轨必须按它在时间线上的位置延迟"
    assert "-c:a aac" in body["command"], "音频轨必须真的进成片，否则拆声音毫无意义"
    assert "amix" not in body["command"], "只有一路声音时不必混"
    assert "-t 4.000" in body["command"], "混音时导出长度收在画面上"
    assert any("比画面长" in w for w in body["warnings"]), (
        "声音比画面长会被截断，这件事不该悄悄发生"
    )

    audio_track = track_named(client, pid, "A1")
    muted = client.patch(f"/api/v1/projects/{pid}/tracks/{audio_track['id']}", json={"muted": True})
    assert muted.status_code == 200, muted.text
    quiet = client.get(f"/api/v1/projects/{pid}/export/command").json()
    assert quiet["audio_clips"] == 0
    assert "-c:a" not in quiet["command"], "一段声音都没有时回到从前的样子，不塞静音轨"
    assert any("静音" in w for w in quiet["warnings"]), "被丢掉的那一段必须说出来"


def test_export_mixes_two_overlapping_audio_tracks(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    ffmpeg_or_skip()
    make_clips(client, pid, 4.0)
    assert add_music(client, pid, duration=2.0).status_code == 201
    second = client.post(f"/api/v1/projects/{pid}/tracks", json={"kind": "audio"})
    assert second.status_code == 201, second.text
    overlap = client.post(
        f"/api/v1/projects/{pid}/tracks/{second.json()['track']['id']}/clips",
        json={
            "asset_id": upload_png(client, pid, "audio", "音效.m4a"),
            "duration": 2.0,
            "start": 1.0,
        },
    )
    assert overlap.status_code == 201, overlap.text

    plan = client.get(f"/api/v1/projects/{pid}/export/command").json()
    assert plan["audio_clips"] == 2
    assert "amix=inputs=2:normalize=0" in plan["command"], (
        "叠加是音频轨存在的意义；normalize=0：加一条配乐不该把对白自动压小"
    )
    assert "adelay=1000:all=1" in plan["command"], "第二条从第 1 秒开始"
    assert plan["warnings"] == [], "都在画面长度内，没什么要提醒的"


def test_export_pins_the_program_audio_to_stereo_48k(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    """成片的声音格式定死，不跟着素材变。

    这条不是洁癖：单声道 + 192k 会让 FFmpeg 原生 aac 编码器在 `amix` 之后**卡死**
    （不退出、不报错、内存一路涨），而生成出来的视频多半是单声道。收成立体声同时
    解决「声道数取决于第一路素材」——两条都在 `EXPORT_AUDIO_FORMAT` 的注释里。
    """
    ffmpeg_or_skip()
    make_clips(client, pid, 2.0)
    assert add_music(client, pid, duration=2.0).status_code == 201

    plan = client.get(f"/api/v1/projects/{pid}/export/command").json()
    assert "channel_layouts=stereo" in plan["command"], "混音后必须收口成立体声"
    assert "sample_rates=48000" in plan["command"], "采样率也不能跟着素材变"
    assert plan["command"].index("aformat") < plan["command"].index("-map"), (
        "aformat 必须在 filter_complex 里，不是输出参数"
    )


def test_export_still_refuses_a_missing_audio_file(
    client: TestClient, pid: str, fake_probe: None
) -> None:
    """音频轨上的文件不在了也要在起进程之前拦住，和视频片段一个标准。"""
    make_clips(client, pid, 2.0)
    placed = add_music(client, pid, duration=2.0)
    assert placed.status_code == 201, placed.text
    asset = next(a for a in client.get(f"/api/v1/projects/{pid}/assets?kind=audio").json())
    (projects.get(pid).dir / asset["path"]).unlink()

    plan = client.get(f"/api/v1/projects/{pid}/export/command")
    assert plan.status_code == 400
    err = error_of(plan)
    assert err["code"] == "MISSING_ASSET"
    assert err["related_ids"]["clip_ids"] == [placed.json()["clip_id"]]
