# 开放接口目录

由 `scripts/export_open_api.py` 从真实路由生成，请勿手工修改。

地址以 `/api/v1` 开头。管理员拥有所有权限；项目路径还检查 Key 的项目范围。
`—` 表示无需额外操作权限，是否需要登录以 OpenAPI 的 security 字段为准。

参数、请求体、枚举和响应结构见 [openapi.json](openapi.json)。

| 方法 | 路径 | 权限 | 功能 |
|---|---|---|---|
| GET | `/api/v1/auth/keys` | admin | List Keys |
| POST | `/api/v1/auth/keys` | admin | Issue Key |
| DELETE | `/api/v1/auth/keys/{key_id}` | admin | Revoke Key |
| GET | `/api/v1/auth/me` | — | Identity |
| POST | `/api/v1/auth/session` | — | Login |
| DELETE | `/api/v1/auth/session` | — | Logout |
| GET | `/api/v1/auth/status` | — | Auth Status |
| GET | `/api/v1/capabilities` | read | Global Capability Matrix |
| GET | `/api/v1/environment` | admin | Global Environment |
| GET | `/api/v1/fs/dirs` | admin | Fs Dirs |
| POST | `/api/v1/fs/mkdir` | admin | Fs Mkdir |
| GET | `/api/v1/fs/roots` | admin | Fs Roots |
| GET | `/api/v1/health` | — | Health |
| GET | `/api/v1/ingest/methods` | admin | Methods |
| GET | `/api/v1/integration/audit` | admin | Audit Log |
| GET | `/api/v1/integration/capabilities` | — | Integration Capabilities |
| GET | `/api/v1/integration/openapi.json` | — | Integration Schema |
| GET | `/api/v1/integration/operations` | — | Operations |
| GET | `/api/v1/library` | library, read | Library Status |
| PATCH | `/api/v1/library/appearances/{aid}` | library, write | Update Appearance |
| DELETE | `/api/v1/library/appearances/{aid}` | library, delete | Delete Appearance |
| POST | `/api/v1/library/appearances/{aid}/sheets` | library, write | Add Sheet |
| GET | `/api/v1/library/assets` | library, read | List Assets |
| POST | `/api/v1/library/assets/register` | admin | Register Asset |
| POST | `/api/v1/library/assets/upload` | library, write | Upload Asset |
| PATCH | `/api/v1/library/assets/{aid}` | library, write | Update Asset |
| DELETE | `/api/v1/library/assets/{aid}` | library, delete | Delete Asset |
| GET | `/api/v1/library/assets/{aid}/refs` | library, read | Asset Refs |
| GET | `/api/v1/library/assets/{asset_id}/content` | library, read | Library Asset Content |
| GET | `/api/v1/library/characters` | library, read | List Characters |
| POST | `/api/v1/library/characters` | library, write | Create Character |
| PATCH | `/api/v1/library/characters/{cid}` | library, write | Update Character |
| DELETE | `/api/v1/library/characters/{cid}` | library, delete | Delete Character |
| POST | `/api/v1/library/characters/{cid}/appearances` | library, write | Create Appearance |
| POST | `/api/v1/library/close` | admin | Close Library |
| POST | `/api/v1/library/configure` | admin | Configure |
| GET | `/api/v1/library/files/{rel}` | admin | Library File |
| DELETE | `/api/v1/library/location-references/{reference_id}` | library, delete | Delete Variant Reference |
| GET | `/api/v1/library/locations` | library, read | List Locations |
| POST | `/api/v1/library/locations` | library, write | Create Location |
| PATCH | `/api/v1/library/locations/{lid}` | library, write | Update Location |
| DELETE | `/api/v1/library/locations/{lid}` | library, delete | Delete Location |
| POST | `/api/v1/library/locations/{lid}/variants` | library, write | Create Variant |
| DELETE | `/api/v1/library/prop-references/{reference_id}` | library, delete | Delete Prop Reference |
| GET | `/api/v1/library/props` | library, read | List Props |
| POST | `/api/v1/library/props` | library, write | Create Prop |
| PATCH | `/api/v1/library/props/{prop_id}` | library, write | Update Prop |
| DELETE | `/api/v1/library/props/{prop_id}` | library, delete | Delete Prop |
| POST | `/api/v1/library/props/{prop_id}/references` | library, write | Add Prop Reference |
| DELETE | `/api/v1/library/sheets/{sheet_id}` | library, delete | Delete Sheet |
| GET | `/api/v1/library/tags` | library, read | List Tags |
| POST | `/api/v1/library/tags` | library, write | Create Tag |
| DELETE | `/api/v1/library/tags/{tid}` | library, delete | Delete Tag |
| POST | `/api/v1/library/tags/{tid}/attach` | library, write | Attach Tag |
| POST | `/api/v1/library/tags/{tid}/detach` | library, write | Detach Tag |
| PATCH | `/api/v1/library/variants/{vid}` | library, write | Update Variant |
| DELETE | `/api/v1/library/variants/{vid}` | library, delete | Delete Variant |
| GET | `/api/v1/library/variants/{vid}/references` | library, read | Variant References |
| POST | `/api/v1/library/variants/{vid}/references` | library, write | Add Variant Reference |
| GET | `/api/v1/onboarding` | admin | Get State |
| PATCH | `/api/v1/onboarding` | admin | Patch State |
| POST | `/api/v1/onboarding/demo` | admin | Create Demo |
| POST | `/api/v1/onboarding/demo/plan` | admin | Plan Demo |
| POST | `/api/v1/packages/import/project` | admin | Import Project |
| POST | `/api/v1/packages/inspect` | admin | Inspect |
| POST | `/api/v1/packages/staged/discard` | admin | Discard Staged |
| POST | `/api/v1/packages/upload` | admin | Upload |
| POST | `/api/v1/projects` | write | Create Project |
| GET | `/api/v1/projects` | read | List Projects |
| POST | `/api/v1/projects/open` | admin | Open Project |
| GET | `/api/v1/projects/recent` | admin | Recent Projects |
| POST | `/api/v1/projects/recent/forget` | admin | Forget Recent |
| PATCH | `/api/v1/projects/{pid}` | write | Patch Project |
| DELETE | `/api/v1/projects/{pid}` | delete | Archive Project |
| GET | `/api/v1/projects/{pid}` | read | Get Project |
| POST | `/api/v1/projects/{pid}/adopt` | library, write | Adopt Into Project |
| POST | `/api/v1/projects/{pid}/adopt/plan` | library, write | Adopt Plan |
| GET | `/api/v1/projects/{pid}/appearances/{aid}` | read | Get Appearance |
| PATCH | `/api/v1/projects/{pid}/appearances/{aid}` | write | Update Appearance |
| DELETE | `/api/v1/projects/{pid}/appearances/{aid}` | delete | Delete Appearance |
| POST | `/api/v1/projects/{pid}/appearances/{aid}/default` | write | Set Default Appearance |
| POST | `/api/v1/projects/{pid}/appearances/{aid}/revert/{field}` | write | Revert Field |
| GET | `/api/v1/projects/{pid}/appearances/{aid}/sheets` | read | List Sheets |
| POST | `/api/v1/projects/{pid}/appearances/{aid}/sheets` | write | Add Sheet |
| DELETE | `/api/v1/projects/{pid}/appearances/{aid}/sheets/{sheet_id}` | delete | Delete Sheet |
| GET | `/api/v1/projects/{pid}/assets` | read | List Assets |
| POST | `/api/v1/projects/{pid}/assets/link` | admin | Link Asset |
| GET | `/api/v1/projects/{pid}/assets/orphans` | read | Orphans |
| POST | `/api/v1/projects/{pid}/assets/register` | admin | Register Asset |
| GET | `/api/v1/projects/{pid}/assets/undescribed` | read | Undescribed |
| POST | `/api/v1/projects/{pid}/assets/upload` | write | Upload Asset |
| PATCH | `/api/v1/projects/{pid}/assets/{asset_id}` | write | Update Asset |
| DELETE | `/api/v1/projects/{pid}/assets/{asset_id}` | delete | Delete Asset |
| GET | `/api/v1/projects/{pid}/assets/{asset_id}/content` | read | Asset Content |
| POST | `/api/v1/projects/{pid}/assets/{asset_id}/proxy` | write | Ensure Proxy |
| GET | `/api/v1/projects/{pid}/assets/{asset_id}/refs` | read | Refs Of |
| GET | `/api/v1/projects/{pid}/assets/{ident}` | read | 读取 assets 单条资源 |
| POST | `/api/v1/projects/{pid}/breakdown/apply` | admin | Apply Breakdown |
| POST | `/api/v1/projects/{pid}/breakdown/propose` | generate | Propose Breakdown |
| GET | `/api/v1/projects/{pid}/capabilities` | read | Capability Matrix |
| GET | `/api/v1/projects/{pid}/characters` | read | List Characters |
| POST | `/api/v1/projects/{pid}/characters` | write | Create Character |
| PATCH | `/api/v1/projects/{pid}/characters/{cid}` | write | Update Character |
| DELETE | `/api/v1/projects/{pid}/characters/{cid}` | delete | Delete Character |
| GET | `/api/v1/projects/{pid}/characters/{cid}/appearances` | read | List Appearances |
| POST | `/api/v1/projects/{pid}/characters/{cid}/appearances` | write | Create Appearance |
| GET | `/api/v1/projects/{pid}/characters/{ident}` | read | 读取 characters 单条资源 |
| DELETE | `/api/v1/projects/{pid}/clips/{clip_id}` | delete | Delete Clip |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/audio-track` | admin | Move Clip To Audio Track |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/blank-duration` | admin | Resize Blank Clip |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/clear` | admin | Clear Clip |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/detach-audio` | admin | Detach Audio |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/isolate-audio-selection` | admin | Isolate Audio Selection |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/mix` | admin | Set Mix |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/move` | admin | Move Clip |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/new-audio-track` | admin | Move Clip To New Audio Track |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/split` | admin | Split Clip |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/trim` | admin | Trim Clip |
| POST | `/api/v1/projects/{pid}/clips/{clip_id}/version` | admin | Replace Version |
| POST | `/api/v1/projects/{pid}/close` | admin | Close Project |
| POST | `/api/v1/projects/{pid}/describe/plan` | admin | Plan |
| POST | `/api/v1/projects/{pid}/describe/suggest` | admin | Suggest |
| GET | `/api/v1/projects/{pid}/director` | admin | History |
| DELETE | `/api/v1/projects/{pid}/director` | admin | Clear |
| POST | `/api/v1/projects/{pid}/director/apply` | admin | Apply |
| POST | `/api/v1/projects/{pid}/director/attach` | admin | Attach |
| POST | `/api/v1/projects/{pid}/director/autopilot` | admin | Autopilot |
| POST | `/api/v1/projects/{pid}/director/chat` | admin | Chat |
| POST | `/api/v1/projects/{pid}/director/chat/stream` | admin | Chat Stream |
| POST | `/api/v1/projects/{pid}/dub/plan` | admin | Plan |
| POST | `/api/v1/projects/{pid}/dub/run` | generate | Run |
| POST | `/api/v1/projects/{pid}/export` | admin | Export |
| GET | `/api/v1/projects/{pid}/export/command` | admin | Export Command |
| GET | `/api/v1/projects/{pid}/exports` | read | List Exports |
| POST | `/api/v1/projects/{pid}/exports/open-folder` | admin | Open Export Folder |
| GET | `/api/v1/projects/{pid}/files/{rel}` | admin | Project File |
| GET | `/api/v1/projects/{pid}/flow` | read | Flow Graph |
| POST | `/api/v1/projects/{pid}/images/generate` | generate | Generate |
| POST | `/api/v1/projects/{pid}/images/plan` | admin | Plan |
| GET | `/api/v1/projects/{pid}/images/skills` | read | Skills |
| POST | `/api/v1/projects/{pid}/ingest/plan` | admin | Plan |
| POST | `/api/v1/projects/{pid}/ingest/register` | admin | Register |
| POST | `/api/v1/projects/{pid}/ingest/run` | admin | Run |
| GET | `/api/v1/projects/{pid}/jobs` | read | List Jobs |
| GET | `/api/v1/projects/{pid}/jobs/{ident}` | read | 读取 jobs 单条资源 |
| DELETE | `/api/v1/projects/{pid}/jobs/{job_id}` | delete | Delete Job |
| POST | `/api/v1/projects/{pid}/jobs/{job_id}/cancel` | generate | Cancel Job |
| PUT | `/api/v1/projects/{pid}/jobs/{job_id}/priority` | generate | Set Priority |
| POST | `/api/v1/projects/{pid}/jobs/{job_id}/retry` | generate | Retry Job |
| GET | `/api/v1/projects/{pid}/links` | read | List Links |
| PUT | `/api/v1/projects/{pid}/links` | write | Set Link |
| DELETE | `/api/v1/projects/{pid}/links/{link_id}` | delete | Delete Link |
| GET | `/api/v1/projects/{pid}/locations` | read | List Locations |
| POST | `/api/v1/projects/{pid}/locations` | write | Create Location |
| GET | `/api/v1/projects/{pid}/locations/{ident}` | read | 读取 locations 单条资源 |
| PATCH | `/api/v1/projects/{pid}/locations/{lid}` | write | Update Location |
| DELETE | `/api/v1/projects/{pid}/locations/{lid}` | delete | Delete Location |
| POST | `/api/v1/projects/{pid}/locations/{lid}/variants` | write | Create Variant |
| GET | `/api/v1/projects/{pid}/overview` | read | Summary |
| GET | `/api/v1/projects/{pid}/overview/activity` | read | Activity |
| GET | `/api/v1/projects/{pid}/overview/continuity` | read | Continuity |
| GET | `/api/v1/projects/{pid}/overview/environment` | read | Environment |
| GET | `/api/v1/projects/{pid}/overview/workflows` | read | Workflow Health |
| POST | `/api/v1/projects/{pid}/package` | admin | Export Project |
| GET | `/api/v1/projects/{pid}/package/download` | admin | Download Project |
| POST | `/api/v1/projects/{pid}/package/plan` | admin | Plan Project |
| POST | `/api/v1/projects/{pid}/packages/import/scene` | admin | Import Scene |
| POST | `/api/v1/projects/{pid}/packages/import/scene/plan` | admin | Plan Scene Import |
| GET | `/api/v1/projects/{pid}/preset` | read | Get Project Preset |
| PUT | `/api/v1/projects/{pid}/preset` | write | Set Project Preset |
| GET | `/api/v1/projects/{pid}/props` | read | List Props |
| POST | `/api/v1/projects/{pid}/props` | write | Create Prop |
| GET | `/api/v1/projects/{pid}/props/{ident}` | read | 读取 props 单条资源 |
| PATCH | `/api/v1/projects/{pid}/props/{prop_id}` | write | Update Prop |
| DELETE | `/api/v1/projects/{pid}/props/{prop_id}` | delete | Delete Prop |
| GET | `/api/v1/projects/{pid}/props/{prop_id}/references` | read | Prop References |
| POST | `/api/v1/projects/{pid}/props/{prop_id}/references` | write | Add Prop Reference |
| DELETE | `/api/v1/projects/{pid}/props/{prop_id}/references/{ref_id}` | delete | Delete Prop Reference |
| GET | `/api/v1/projects/{pid}/queue` | read | Queue State |
| POST | `/api/v1/projects/{pid}/queue/batches/{batch_id}/cancel` | generate | Cancel Batch |
| POST | `/api/v1/projects/{pid}/queue/batches/{batch_id}/retry` | generate | Retry Batch |
| POST | `/api/v1/projects/{pid}/queue/cancel-all` | generate | Cancel All |
| POST | `/api/v1/projects/{pid}/queue/clear-failed` | generate | Clear Failed |
| POST | `/api/v1/projects/{pid}/queue/pause` | generate | Pause Queue |
| POST | `/api/v1/projects/{pid}/queue/resume` | generate | Resume Queue |
| POST | `/api/v1/projects/{pid}/queue/retry-failed` | generate | Retry Failed |
| POST | `/api/v1/projects/{pid}/refine/plan` | generate | Plan |
| POST | `/api/v1/projects/{pid}/refine/run` | generate | Run |
| GET | `/api/v1/projects/{pid}/resources` | read | Resource Search |
| GET | `/api/v1/projects/{pid}/route` | read | Get Project Route |
| GET | `/api/v1/projects/{pid}/scene-node-options` | read | Scene Node Options |
| GET | `/api/v1/projects/{pid}/scenes` | read | List Scenes |
| POST | `/api/v1/projects/{pid}/scenes` | write | Create Scene |
| PUT | `/api/v1/projects/{pid}/scenes/order` | write | Reorder Scenes |
| POST | `/api/v1/projects/{pid}/scenes/{scene_id}/generate` | generate | Enqueue Scene |
| GET | `/api/v1/projects/{pid}/scenes/{sid}` | read | Get Scene |
| PATCH | `/api/v1/projects/{pid}/scenes/{sid}` | write | Update Scene |
| DELETE | `/api/v1/projects/{pid}/scenes/{sid}` | delete | Delete Scene |
| PUT | `/api/v1/projects/{pid}/scenes/{sid}/cast` | write | Set Scene Cast |
| PUT | `/api/v1/projects/{pid}/scenes/{sid}/locations` | write | Set Scene Locations |
| POST | `/api/v1/projects/{pid}/scenes/{sid}/package` | admin | Export Scene |
| GET | `/api/v1/projects/{pid}/scenes/{sid}/package/download` | admin | Download Scene |
| POST | `/api/v1/projects/{pid}/scenes/{sid}/package/plan` | admin | Plan Scene |
| POST | `/api/v1/projects/{pid}/scenes/{sid}/shots` | write | Create Shot |
| PUT | `/api/v1/projects/{pid}/scenes/{sid}/shots/order` | write | Reorder Shots |
| GET | `/api/v1/projects/{pid}/scenes/{sid}/videos` | read | Scene Videos |
| POST | `/api/v1/projects/{pid}/sequence/plan` | admin | Plan |
| POST | `/api/v1/projects/{pid}/sequence/run` | generate | Run |
| POST | `/api/v1/projects/{pid}/sequence/transitions/plan` | admin | Transition Plan |
| POST | `/api/v1/projects/{pid}/sequence/transitions/run` | generate | Transition Run |
| GET | `/api/v1/projects/{pid}/shot-links` | read | List Shot Links |
| PUT | `/api/v1/projects/{pid}/shot-links` | write | Set Shot Link |
| DELETE | `/api/v1/projects/{pid}/shot-links/{link_id}` | delete | Delete Shot Link |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}` | read | Get Shot |
| PATCH | `/api/v1/projects/{pid}/shots/{shot_id}` | write | Update Shot |
| DELETE | `/api/v1/projects/{pid}/shots/{shot_id}` | delete | Delete Shot |
| DELETE | `/api/v1/projects/{pid}/shots/{shot_id}/audio-current` | delete | Mute |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}/audio-versions` | read | Audio Versions |
| POST | `/api/v1/projects/{pid}/shots/{shot_id}/audio/import` | admin | Import Audio |
| PUT | `/api/v1/projects/{pid}/shots/{shot_id}/cast` | write | Set Shot Cast |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}/context` | read | Resolve Context |
| POST | `/api/v1/projects/{pid}/shots/{shot_id}/context/override` | write | Override Context |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}/context/snapshot` | read | Snapshot Context |
| POST | `/api/v1/projects/{pid}/shots/{shot_id}/generate` | generate | Enqueue Shot |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}/graph` | read | Shot Graph |
| POST | `/api/v1/projects/{pid}/shots/{shot_id}/move` | write | Move Shot |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}/params` | read | Resolve Params |
| PUT | `/api/v1/projects/{pid}/shots/{shot_id}/props` | write | Set Shot Props |
| POST | `/api/v1/projects/{pid}/shots/{shot_id}/split` | write | Split Shot |
| GET | `/api/v1/projects/{pid}/shots/{shot_id}/versions` | read | List Versions |
| POST | `/api/v1/projects/{pid}/shots/{shot_id}/versions` | write | Add Manual Version |
| GET | `/api/v1/projects/{pid}/story` | read | Get Story |
| PATCH | `/api/v1/projects/{pid}/story` | write | Save Story |
| GET | `/api/v1/projects/{pid}/storyboard` | read | Storyboard |
| POST | `/api/v1/projects/{pid}/storyboard/posters` | generate | Extract Posters |
| GET | `/api/v1/projects/{pid}/timeline` | read | Get Timeline |
| POST | `/api/v1/projects/{pid}/timeline/assemble` | admin | Auto Assemble |
| POST | `/api/v1/projects/{pid}/timeline/assemble/plan` | admin | Assemble Plan |
| POST | `/api/v1/projects/{pid}/timeline/redo` | admin | Redo |
| POST | `/api/v1/projects/{pid}/timeline/undo` | admin | Undo |
| POST | `/api/v1/projects/{pid}/tracks` | admin | Add Track |
| PATCH | `/api/v1/projects/{pid}/tracks/{track_id}` | admin | Update Track |
| DELETE | `/api/v1/projects/{pid}/tracks/{track_id}` | delete | Delete Track |
| POST | `/api/v1/projects/{pid}/tracks/{track_id}/blank-clips` | admin | Add Blank Clip |
| POST | `/api/v1/projects/{pid}/tracks/{track_id}/clips` | admin | Add Clip |
| GET | `/api/v1/projects/{pid}/transitions` | read | List Transitions |
| POST | `/api/v1/projects/{pid}/transitions` | admin | Add Transition |
| DELETE | `/api/v1/projects/{pid}/transitions/{tid}` | delete | Delete Transition |
| GET | `/api/v1/projects/{pid}/variants/{ident}` | read | 读取 variants 单条资源 |
| PATCH | `/api/v1/projects/{pid}/variants/{vid}` | write | Update Variant |
| DELETE | `/api/v1/projects/{pid}/variants/{vid}` | delete | Delete Variant |
| GET | `/api/v1/projects/{pid}/variants/{vid}/references` | read | Variant References |
| POST | `/api/v1/projects/{pid}/variants/{vid}/references` | write | Add Variant Reference |
| DELETE | `/api/v1/projects/{pid}/variants/{vid}/references/{ref_id}` | delete | Delete Variant Reference |
| GET | `/api/v1/projects/{pid}/variants/{vid}/usage` | read | Variant Usage |
| POST | `/api/v1/projects/{pid}/versions/{version_id}/current` | write | Set Current Version |
| GET | `/api/v1/projects/{pid}/versions/{version_id}/lineage` | read | Lineage |
| GET | `/api/v1/projects/{pid}/workflow-bindings` | read | Get Project Workflow Bindings |
| PUT | `/api/v1/projects/{pid}/workflow-bindings` | admin | Set Project Workflow Bindings |
| GET | `/api/v1/projects/{pid}/workflows` | read | List Workflows |
| POST | `/api/v1/projects/{pid}/workflows` | admin | Import Workflow |
| GET | `/api/v1/projects/{pid}/workflows/{wid}` | admin | Get Workflow |
| PATCH | `/api/v1/projects/{pid}/workflows/{wid}` | admin | Update Workflow |
| DELETE | `/api/v1/projects/{pid}/workflows/{wid}` | admin | Delete Workflow |
| PUT | `/api/v1/projects/{pid}/workflows/{wid}/bindings` | admin | Bind Workflow |
| POST | `/api/v1/projects/{pid}/workflows/{wid}/default` | admin | Set Default |
| POST | `/api/v1/projects/{pid}/workflows/{wid}/validate` | admin | Validate Workflow |
| GET | `/api/v1/refine/kinds` | admin | Kinds |
| GET | `/api/v1/settings` | admin | Read |
| PATCH | `/api/v1/settings` | admin | Patch |
| POST | `/api/v1/settings/models` | admin | Models |
| GET | `/api/v1/settings/presets` | admin | List Presets |
| POST | `/api/v1/settings/presets` | admin | Upload Preset |
| POST | `/api/v1/settings/presets/upload` | admin | Upload Preset File |
| DELETE | `/api/v1/settings/presets/{name}` | admin | Delete Preset |
| POST | `/api/v1/settings/probe` | admin | Probe |
| GET | `/api/v1/system/deps` | admin | Deps |
| GET | `/api/v1/workflows` | read | List Global Workflows |
| POST | `/api/v1/workflows` | admin | Import Global Workflow |
| GET | `/api/v1/workflows/{wid}` | admin | Get Global Workflow |
| PATCH | `/api/v1/workflows/{wid}` | admin | Update Global Workflow |
| DELETE | `/api/v1/workflows/{wid}` | admin | Delete Global Workflow |
| PUT | `/api/v1/workflows/{wid}/bindings` | admin | Bind Global Workflow |
| POST | `/api/v1/workflows/{wid}/default` | admin | Set Global Default |
| POST | `/api/v1/workflows/{wid}/validate` | admin | Validate Global Workflow |
