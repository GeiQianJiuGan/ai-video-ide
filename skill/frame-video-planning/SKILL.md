---
name: frame-video-planning
description: 为制帧视频制作规划镜头剧情、有序画面及逐帧故事、静态图片提示词和独立视频提示词。用于先制图再生成视频的叙事规划，不用于角色定妆图或纯文本直出。
---

# 制帧视频叙事规划

直出与制帧共用剧本、幕、镜头和故事主线。直出把镜头剧情落成视频导演意图；制帧还要把同一镜的故事拆成若干可制作的静态画面，再写连接这些状态的视频动作。不要为切换制作方式重写故事。

## 镜头计划

每镜保留中文 `description`、人物及道具原名、模型无关的 `intent` 和秒数。制帧时另外输出：

- `frames`：按剧情顺序排列，每项包含 `title`、`role`、中文 `story` 和图片 `prompt`。
- `video_prompt`：独立、完整的视频描述，写清从开场状态开始的动作顺序、走位、运镜、结束状态及声音。不要只写“让图片动起来”或“接上图”。

`role` 为 `first`（开场）、`key`（关键状态）或 `last`（收尾）。第一项必须是唯一的 `first`；有 `last` 时必须是最后一项，至多一个。数量由叙事需要决定：简单动作可只要开场；需要锁定结局时用开场与收尾；关键转折可增加中间画面。当前完整创作任务每镜最多八个画面。多个独立动作、机位硬切或时空跳跃应拆成多个镜头。

## 每张图也要讲故事

`story` 回答“这张图对应镜头故事的哪一步、谁在做什么、人物或道具处于什么状态、如何承接前后画面”。用具体的可见状态表达情绪，不能只有“开场”“高潮”这样的标签。连续画面应有可辨识的情节变化，保持人物、服装、道具归属、光线、视线与运动方向一致。

图片 `prompt` 描述一个瞬间的完整单幅画面：主体姿态和表情、位置关系、道具状态、景别、机位、场景和光线。每张图可独立制作，不依赖“同上一张”。不要把“先推门再走到桌前”整段动作塞进一张图，不要四视图、拼图、字幕或时间轴；画面允许人物。与角色/场景/道具设定图区分：设定图用于制帧参考，帧图本身是成片构图。

视频 `video_prompt` 负责时间中的变化，不能与选定首尾状态矛盾。画面与制作描述沿用工程语言约定，对白保留原文并写明说话人；无对白不编台词。只写同期环境声、动作音效和必要对白，不写配乐、BGM 或负向提示词。

## AI Video Studio 的执行边界

当前视频执行只接收单首帧或首尾帧。`key` 是叙事和制图参考，不会自动提交为中间关键帧；不要宣称多张图都会驱动一次视频生成。必须精确表现某个中间状态时，将动作拆为多个镜头，再分别制作视频。

完整分镜任务会在采用后创建帧卡、保存逐帧故事，填入独立视频草稿并关联计划中的首尾帧卡。生成候选图片、采用图片和生成视频仍是后续操作。只有通过当前预设/工作流能力检查后才能提交；缺少首尾帧入口时调整计划或选择合适路由，不能静默降级。不要捏造资产 ID 或声称规划已经产出了图片/视频。

逐帧剧情、图片提示词与候选制作在「叙事规划 → 制作图片」（剧本下方）。直出与制帧使用同一个分镜页面，区别是实际视频输入：直出使用角色、场景等设定图，制帧使用制作图片中已采用的帧图。不要为制帧设计另一套分镜布局。

制作图片时按角色、场景、道具添加当前设定图，按优先级顺序排列。实际采用数量取决于图片 API 或工作流容量；保留前面的图片、舍弃超出的尾部，并显示或核查实际采用与舍弃的清单。不要删除超出容量的资产，也不要把未声明的 API 容量猜成固定数字。

参考图少于工作流容量时，未填的媒体入口及相关连线从本次提交图中摘除，不沿用工作流原来的示例图片；原预设和种子、尺寸等标量默认值保留。

已有镜头可用 `POST /projects/{pid}/shots/{sid}/frame-plan/propose` 生成待审阅计划，确认具体内容后通过对应的 `frame-plan/apply` 采用，回传提案中的 `shot_updated_at` 和 `frame_card_ids`。已有空白画面须保持数量、顺序与名称；采用时原位填入，不重建画面 ID。已有剧情、提示词、参考图、候选或任务时直接编辑现有草稿，不覆盖它们。镜头或画面位置变化导致采用失败时重新读取现状并规划。

本技能是模型无关的规划方法，不是视频渲染规范，不要填入 `intent.skill` 或项目的视频模型配置。完整分镜任务输出上述结构；讨论和局部修改可用同一方法审阅方案，只有当前工具明确支持的字段才提交。通过 API 操作项目时，先读取项目与现有草稿，再使用当前接口的并发控制，不覆盖已制作的候选和历史版本。

## 示例：发现来信（五秒，首尾帧）

镜头剧情：阿岚推门进入客厅，看见桌上的信，停步望向信封。

```json
{
  "frames": [
    {
      "title": "门口发现桌上的信",
      "role": "first",
      "story": "阿岚停在半开的门边，手握门把；桌上的信封尚未被触碰，为发现来信建立空间关系。",
      "prompt": "Single medium-wide shot at eye level. Alan in a blue coat stands at the half-open living-room door, right hand on the handle, looking toward an untouched folded envelope on the table at screen right. Warm table lamp, rainy night beyond the doorway."
    },
    {
      "title": "停步凝视来信",
      "role": "last",
      "story": "阿岚已走近桌边并停步，双手垂在身侧，目光落在原处的信封上，尚未拿起它，承接下一镜拆信。",
      "prompt": "Single medium-wide shot at eye level in the same living room. Alan in a blue coat stands beside the table at screen right, hands lowered, eyes fixed on an untouched folded envelope. The door is behind her at screen left. Warm table lamp and rainy night lighting."
    }
  ],
  "video_prompt": "Starting with Alan at the half-open door, she releases the handle, walks toward the table at screen right and stops beside it. Her gaze settles on the envelope; she does not touch it. A slow gentle push-in preserves the movement direction and room layout, ending with her hands lowered beside the table. Sound: rain outside, a quiet door creak and two soft footsteps. No dialogue or background music."
}
```
