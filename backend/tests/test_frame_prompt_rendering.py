"""制帧提示词绑定与完整结构回归。"""

from app.generation.providers.base import Picture, PictureBook, VideoRequest
from app.generation.renderers import render_prompt


def test_frame_prompt_rebinds_slots_without_nested_fields():
    book = PictureBook(
        items=[Picture(index=2, role="first_frame"), Picture(index=1, role="last_frame")]
    )
    req = VideoRequest(
        mode="first_last_frame",
        duration=6,
        extra={"frame_pipeline": True},
        prompt=(
            "integrated_multimodal_description: [Shot 1] Start in Picture 1. "
            "[Shot 2] At 00:03.000, the camera cuts to Picture 2.\n"
            "overall_soundscape: Footsteps.\nnon_diegetic_music: N/A"
        ),
    )
    text = render_prompt("minimax-h3", req, book)
    assert text.count("integrated_multimodal_description:") == 1
    assert "Start in <Picture 2>" in text
    assert "cuts to <Picture 1>" in text
    assert "<Picture 1> (from [Shot 2])" in text
    assert "6.00-second" in text


def test_plain_frame_draft_starts_at_local_shot_one():
    req = VideoRequest(
        mode="image2video", prompt="She opens the door.", shot_no=12, extra={"frame_pipeline": True}
    )
    text = render_prompt(
        "minimax-h3", req, PictureBook(items=[Picture(index=1, role="first_frame")])
    )
    assert "[Shot 12]" not in text
    assert "[Shot 1]" in text
    assert "non_diegetic_music:\nN/A" in text
