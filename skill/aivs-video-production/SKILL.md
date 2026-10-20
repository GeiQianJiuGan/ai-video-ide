---
name: aivs-video-production
description: Use the AI Video Studio open HTTP API to inspect projects, manage video assets, scenes and shots, and submit or inspect generation jobs. Applies when the user wants Codex to operate an AI Video Studio project, not when they only want a standalone video prompt.
---

# AI Video Studio production

Operate the user's AI Video Studio project through its `/api/v1` HTTP API. The project uses stable IDs, project-scoped permissions, an append-only generation history, and a project-wide ETag. Use the [API workflow reference](references/api-workflow.md) for endpoint selection and the project's live, permission-filtered OpenAPI for exact request schemas.

## Connect and discover

1. Read `AIVS_BASE_URL` from the environment. It must include `/api/v1`. Read the optional `AIVS_API_KEY` from the environment; never place a real key in prompts, commands, files, or output. A direct loopback connection in auto mode can be keyless, but a server deployment requires a Bearer key. If the URL or required key is missing, ask the user for a safe connection setup.
2. Use `python scripts/aivs_api.py GET /auth/me`, then `GET /integration/capabilities`, `GET /integration/operations`, and `GET /projects`. Check the identity's scopes and project IDs before choosing operations. Use `GET /integration/openapi.json` when exact fields or response shapes are needed. Do not infer access from a copied route catalog alone.
3. Select a project from the returned IDs. For existing data, search `/projects/{pid}/resources` and inspect the relevant detail or `/shots/{shot_id}/graph` before edits. Do not guess IDs, server paths, reference slots, or the project's duration unit.

The helper uses only Python's standard library, sends the key only as an Authorization header, prints response ETags to stderr, and prints a generated `Idempotency-Key` before every business write. Use `--body-file` for JSON requests and `--upload` for multipart assets. Run `python scripts/aivs_api.py --help` for syntax. Its paths are relative to `AIVS_BASE_URL` and must begin with `/`.

## Make changes

- Follow the user's requested scope. Inspect before editing and include the last read project's `ETag` as `--if-match` for changes to existing project content. A new business write gets a unique idempotency key; retain that key and the exact request bytes for any retry. Key or session management endpoints do not take idempotency keys.
- Upload local media through `/projects/{pid}/assets/upload`, then use the returned Asset ID. A reference image must be attached through the relevant appearance, location variant, prop, or shot context before using `@[标签](asset:ast_真实ID)` in a prompt. First and last frames use their own shot fields and `frame:first_frame` / `frame:last_frame` links.
- For hand-written shot prompts, send `manual_prompt: true`. Query `/shots/{shot_id}/context` or `/graph` after changing references and before generation. Read `/projects/{pid}/route` for the actual generation route and dependencies. `generate` permission is separate from `write`; generation can consume provider resources and should occur only when the user's task calls for it.
- Submit generation with a single POST to `/shots/{shot_id}/generate` (`{}` by default, `{"kind":"first_last_frame"}` when needed), store the returned Job ID, and poll `/jobs/{job_id}`. A scene-generation response can contain per-shot skips or errors; inspect every entry. Read `/shots/{shot_id}/versions` before adopting a version with `/versions/{version_id}/current`.
- Treat 409 as an uncertain or conflicting operation: inspect current resources and jobs before deciding whether anything remains to do. On 412, reread the project state and merge changes. Do not automatically change an idempotency key to retry an uncertain write, force-delete referenced assets, or bypass context checks. Report failures with status, API error code, and request ID, without exposing credentials.

For resource inheritance, PATCH clearing semantics, version rules, and endpoint examples, read [references/api-workflow.md](references/api-workflow.md). If working inside the source repository, the full maintained contract is in `docs/open-api/`.
