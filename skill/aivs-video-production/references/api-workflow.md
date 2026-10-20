# API workflow reference

All paths below are relative to `AIVS_BASE_URL`, which ends in `/api/v1`. The permission-filtered `/integration/openapi.json` is authoritative for the connected server's field schemas. `/integration/operations` lists callable methods. Required scopes include `read`, `write`, `delete`, `generate`, `library`, and `admin`; project restrictions still apply.

## Find the correct object

| Need | Endpoint | Note |
|---|---|---|
| Registered projects | `GET /projects` | Returns `{items,total,limit,offset}`. Use IDs from `items`. |
| Project details and duration unit | `GET /projects/{pid}` | `duration_unit` can be `frames` or `seconds`. |
| Search resource rows | `GET /projects/{pid}/resources?resource=shots&limit=50&offset=0` | `resource` accepts `characters`, `appearances`, `locations`, `variants`, `props`, `assets`, `scenes`, `shots`, `jobs`; rows can contain raw JSON strings. |
| Shot and effective references | `GET /projects/{pid}/shots/{shot_id}/graph` | Includes `shot`, `context`, `versions`, `current_version`. |
| Generation setup | `GET /projects/{pid}/route` | Check capabilities and missing configuration before submitting. |

The data chain is Project → Scene (幕) → Shot (镜头) → Job → GenerationVersion. A Location is scene material, not a Scene. Character → Appearance → sheet/reference Asset; Location → Variant → reference Asset. Existing list endpoints can return bare arrays; use each endpoint's schema instead of assuming a common pagination shape.

## Typical edit and generation

1. Create or select a project, then read its `duration_unit` and `fps`. For a new project, check the live OpenAPI schema for required fields.
2. Upload an image: `POST /projects/{pid}/assets/upload` as multipart `file`, optional `kind=upload`. Give it a visual description through `PATCH /projects/{pid}/assets/{asset_id}`. Use `--upload C:/path/image.png` in the helper.
3. Create a character at `POST /projects/{pid}/characters` with `name` and optional `default_asset_id`; retrieve its default Appearance via `GET /projects/{pid}/characters/{cid}/appearances`. Create a Location with `POST /locations`, a Variant with `POST /locations/{lid}/variants`, and a Scene with `POST /scenes` as needed.
4. Set the Scene cast via `PUT /scenes/{sid}/cast` with `appearance_ids`, create a Shot via `POST /scenes/{sid}/shots`, and update it via `PATCH /shots/{shot_id}`. The Shot `prompt` can link an effective reference with `@[人物](asset:ast_真实ID)`; `first_frame_asset_id` and `last_frame_asset_id` attach actual frame assets. Set `manual_prompt: true` for hand-written prompt text.
5. If a reference is only for this Shot, use `POST /shots/{shot_id}/context/override` with `{"action":"add","asset_id":"ast_真实ID","label":"正面"}`. Inspect `/context` for effective and omitted references; do not assume display labels correspond to model Picture numbers.
6. Confirm `/route` and `/context`, then `POST /shots/{shot_id}/generate` with `{}`. For a first and last frame job, use `{"kind":"first_last_frame"}`. Save the returned job ID and query `GET /jobs/{job_id}` until `done`, `failed`, or `cancelled`. Do not enqueue a second time merely because polling timed out. `POST /scenes/{scene_id}/generate` enqueues a whole scene and can report skipped shots.
7. Read `GET /shots/{shot_id}/versions`; adopt a selected version with `POST /versions/{version_id}/current`. Existing generated versions cannot be overwritten. A finished video may instead be uploaded as an Asset and added through `POST /shots/{shot_id}/versions` with `asset_id` and `kind=video`.

## Concurrency and failure

- Existing-resource edits use the `ETag` from the latest read as `If-Match`; it is project-wide, so unrelated writes can make it stale. On 412, reread and merge.
- Send a unique 1–128 character ASCII `Idempotency-Key` for each business write. For a retry, reuse the same key, method, path, query, body bytes, and upload content. A completed retry may return `Idempotency-Replayed: true`; a 409 for an in-progress or interrupted request leaves the result uncertain, so query the object or job before any further write.
- A typical API error is `{"error":{"code":"...","title":"...","detail":"...","suggestions":[],"related_ids":{}}}`. Use `X-Request-ID` for diagnosis. 401/403 mean authentication, permission, or project scope problems; 422 means a schema or field error; 503 can mean a provider is unavailable.
- Most PATCH bodies leave fields unchanged when omitted or null. Clear text and frame slots with an empty string. Clearing a Shot prompt restores Scene inheritance. `Scene.params` replaces the whole object. Deletions can fail while references exist; inspect references before unlinking or deleting. Project deletion archives the project.

The source repository's `docs/open-api/README.md`, `resources.md`, `workflows.md`, `permissions.md`, and `errors.md` explain edge cases. Prefer the connected server's filtered OpenAPI when it differs from a local checkout.
