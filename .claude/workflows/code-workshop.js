export const meta = {
  name: 'code-workshop',
  description: 'Раздел «Код» 0.9–1.0: one pipeline stage per run (args.stage) — zone agents in waves, then checks and fixes (max 3 rounds); no commits',
  whenToUse: 'Only on the owner\'s command, one stage at a time: contract-0.9, sandbox-0.9, build-0.9, accept-0.9, eval-0.9, docs-0.9, contract-1.0, build-1.0, accept-1.0, docs-1.0 (CLAUDE.md, «Пайплайн «Код»»). args: {stage, notes?} — notes are short conclusions of earlier stages',
  phases: [
    { title: 'Work', detail: 'zone agents of the stage: waves run one after another, agents inside a wave in parallel' },
    { title: 'Check', detail: 'reviewer, or qa ∥ security ∥ design review at acceptance' },
    { title: 'Fix', detail: 'zone owners fix blocking findings' },
  ],
}

// Token economy (same as dialog-memory): Opus only where an error is expensive
// (contract, code, sandbox, security); Sonnet for mechanical work (docs, design
// comparison, prompt-diff review). One quality review per stage. Agents hand over short
// structured reports; re-checks look only at open findings and fixes.

const STAGE = typeof args === 'string' ? args : args && args.stage
const NOTES = (args && typeof args === 'object' && args.notes) || ''
const MAX_ROUNDS = 3
const LIGHT = 'sonnet'

const COMMON = `Feature «Код» (code workshop: projects with a Monaco editor and an assistant replacing the CoGIS helper) of the employee portal, branch feat/code.
Source of truth: docs/portal-design.md 0.9 — §3.3 (code-sandbox), §3.4, §5.4, §6, §7, §8.5, §9, §10 criteria 33–41, §11, §14 decisions 1–27 (\`git diff main -- docs/portal-design.md\` shows the change). Contract: docs/portal-api.md. UI concept: docs/portal-ui.md.
Follow CLAUDE.md, in particular section «Пайплайн «Код» (0.9–1.0)» and its quality rules 1–19.
Background (reference only; where it differs from the design, the design wins): .claude/notes/code-workshop/gptool.md (customer GPTool instruction as text), .claude/notes/code-workshop/cogis-js-html.md (CoGIS 13 JS/HTML documentation digest). Customer PDFs in .claude/notes/code-workshop/cogis-docs/ and real CoGIS DLLs in deploy/cogis-sdk/ are customer property: read if needed, never copy into tracked files or images.
Change files only in your zone. Never edit docs/portal-design.md: a gap or contradiction there goes to owner_questions. Ignore .claude/prompt.md (owner's file). Do not commit.
Save tokens: read only the sections and functions you need, run checks in short mode (\`pytest -q --tb=short\`, \`| tail -n 50\`), keep the report short — facts, file:line, no retelling of the documents.${NOTES ? `\nConclusions of earlier stages (owner decisions and carry-overs, binding):\n${NOTES}` : ''}`

const REPORT = {
  type: 'object',
  properties: {
    summary: { type: 'string', description: 'what was done, at most 8 short lines' },
    changed_files: { type: 'array', items: { type: 'string' } },
    checked_locally: { type: 'array', items: { type: 'string' }, description: 'check → result, one line each' },
    check_on_vm: { type: 'array', items: { type: 'string' }, description: '«проверить на ВМ» items' },
    deviations: { type: 'array', items: { type: 'string' }, description: 'deviations from contract/concept with reason' },
    notes_for_other_zones: { type: 'array', items: { type: 'string' } },
    owner_questions: { type: 'array', items: { type: 'string' }, description: 'question + options + your recommendation' },
  },
  required: ['summary', 'changed_files', 'checked_locally', 'check_on_vm', 'deviations', 'notes_for_other_zones', 'owner_questions'],
}

const EVAL_REPORT = {
  type: 'object',
  properties: {
    ...REPORT.properties,
    model: { type: 'string', description: 'real model on the VM or the stand stub' },
    shares_before: { type: 'string', description: 'built at once / after fixes / not built, with task count' },
    shares_after: { type: 'string', description: 'same after the prompt changes' },
    worst_tasks: { type: 'array', items: { type: 'string' }, description: 'task id — why it fails, one line each' },
  },
  required: [...REPORT.required, 'model', 'shares_before', 'shares_after', 'worst_tasks'],
}

const FIX_REPORT = {
  type: 'object',
  properties: {
    fixes: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          where: { type: 'string' },
          status: { type: 'string', enum: ['fixed', 'not fixed'] },
          how: { type: 'string', description: 'one line: the change, or why not fixed' },
        },
        required: ['where', 'status', 'how'],
      },
    },
    changed_files: { type: 'array', items: { type: 'string' } },
    checks: { type: 'string', description: 'zone checks after the fix, one line' },
    owner_questions: { type: 'array', items: { type: 'string' } },
  },
  required: ['fixes', 'changed_files', 'checks', 'owner_questions'],
}

const FINDINGS = fixers => ({
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['можно коммитить', 'нужны исправления'] },
    blocking: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          agent: { type: 'string', enum: fixers, description: 'zone owner who must fix it' },
          where: { type: 'string', description: 'file:line or screen/endpoint' },
          issue: { type: 'string', description: 'what is wrong, with concrete input → wrong result; 1–3 sentences' },
          fix: { type: 'string', description: '1–2 sentences' },
        },
        required: ['agent', 'where', 'issue', 'fix'],
      },
    },
    minor: { type: 'array', items: { type: 'string' }, description: 'one line each' },
    owner_questions: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string', description: 'at most 5 sentences' },
  },
  required: ['verdict', 'blocking', 'minor', 'owner_questions', 'summary'],
})

const DOCS_REPORT = {
  type: 'object',
  properties: {
    ...REPORT.properties,
    commands_checked: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          command: { type: 'string', description: 'command, make target, variable or config key used in the docs' },
          defined_in: { type: 'string', description: 'file:line where it is defined' },
        },
        required: ['command', 'defined_in'],
      },
    },
    unverified: { type: 'array', items: { type: 'string' }, description: 'anything in the docs you could not confirm in the code' },
    steps_justified: {
      type: 'array',
      description: 'every step and check of docs/release-<version>.md, incl. the «Установка с нуля» appendix',
      items: {
        type: 'object',
        properties: {
          step: { type: 'string', description: 'step number and a few words' },
          why: { type: 'string', enum: ['работа релиза', 'откат', 'критерий ВМ'] },
          ref: { type: 'string', description: 'portal-design § or criterion number' },
        },
        required: ['step', 'why', 'ref'],
      },
    },
  },
  required: [...REPORT.required, 'commands_checked', 'unverified', 'steps_justified'],
}

const CONTRACT = {
  '0.9': {
    architect: `Write the 0.9 part of docs/portal-api.md (items marked 0.9; 1.0 comes in a later stage — do not design it now). Cover: alembic migration after the current head (check portal/backend/migrations/versions) — projects, files, snapshots, builds, settings (packages, attached schema), the project assistant dialog kind and the migration of old CoGIS dialogs to a read-only list; endpoints with request/response schemas and error codes — projects CRUD, files CRUD with path/extension/size validation, wizard «Новый GPTool» (form schema, CoGIS parameter types, template rendering), ZIP import (only .cs, package names matched to the offline set) and export (sources + .csproj for Visual Studio), snapshots list/restore, build start/result, «Скачать для CoGIS» archive, assistant messages (SSE, actions write/explain/fix, selected fragment), proposed edit as a separate object with per-file accept/reject; the model answer format «files whole» and its parsing and validation; request assembly — current files, build messages, API reference selection with a budget, GPTool rules, attached schema, CoGIS documentation from the knowledge base, all untrusted content as user-role data_block, the shared estimate with dialog memory (dialogs/context.py) and the «project too large» outcome; the fix loop (≤2 extra builds, one final edit); the code-sandbox HTTP protocol (compile request, diagnostics with file/line/column/code/ru text, rules findings, DLL, SDK version, timeouts, concurrency, errors «сборка недоступна»); the exact GPTool rules list (from .claude/notes/code-workshop/gptool.md); the API reference format produced by \`make cogis-sdk\`; YAML keys (limits, sandbox, packages with versions and sha512, prompts file code_prompts.yaml, templates dir code_templates/), feature flags in /api/config; flag-off behaviour (old CoGIS helper API unchanged); portal/dev stub behaviour; tests that must exist. Verify every fact against the code before writing it. Bump the document version and add a changelog entry in the document's style.`,
    designer: `Write the 0.9 part of docs/portal-ui.md (items marked 0.9). Cover: the «Код» section in the rail (label «Код», screen title «Мастерская кода CoGIS», icon), project list and file tree, Monaco editor with tabs in both themes, the bottom «Сборка» panel (errors / warnings / GPTool rules, click to line, states: building, built, failed, unavailable), the right assistant column and how the dialog memory panel (0.7 right column) coexists with it, the wizard «Новый GPTool» (form for tools and parameters, Russian/English captions, database option, description), proposed edit as «было / стало» with accept all / per file / reject, fix-loop result marks, versions list and restore, project settings (packages from the offline set, attached schema from «Мои схемы»), import/export ZIP, «Скачать для CoGIS» with the installation note and the warning, read-only list of old CoGIS dialogs and transfer, flag-off (old CoGIS screen), empty states and errors; dictionary (no jargon: «сборка», «правка», «версия»); Monaco built-in strings that stay untranslated; final Russian texts. Read the actual frontend (portal/frontend/src: screens/CogisScreen.tsx, components/chat/DialogScreen, Composer, memory/) so the concept fits existing components. Bump the document version and add a changelog entry in the document's style.`,
    review: 'p.33–38, §3.3, §5.4 (0.9 parts), §7, §8.5, §14',
  },
  '1.0': {
    architect: `Write the 1.0 part of docs/portal-api.md (items marked 1.0). Cover: four project kinds (JS plugin button on \`api\`, HTML widget on Knockout, Map Catalog customization with type HTML/CSS/JavaScript, insertion place and client type, object card hooks) and their file rules; templates; the \`api\` type description built from documentation §7.10 (digest: .claude/notes/code-workshop/cogis-js-html.md; undocumented — untyped; known discrepancies marked); jQuery / Knockout / Leaflet files taken from the CoGIS client by \`make cogis-sdk\`; the preview — separate Caddy path with its own CSP, iframe sandbox without allow-same-origin, how the code gets into the frame, the \`api\` stub with fictitious layers and the call journal, console errors back to the editor, no network except preview libraries; export with an installation note per kind; prompt-assembly differences for JS/HTML; flag; tests. Verify every fact against the code. Bump the version and add a changelog entry.`,
    designer: `Write the 1.0 part of docs/portal-ui.md (items marked 1.0): kind choice when creating a project and the four templates, editor hints for \`api\`, the preview pane (layout, call journal, console, «что недоступно в предпросмотре»), export notes per kind (where to paste the code, what to choose in CoGIS settings), flag off; final Russian texts. Bump the version and add a changelog entry.`,
    review: 'p.39–41, §5.4 (1.0 part), §8.5 (preview), §14 items 20–23',
  },
}

const SCOPE = {
  '0.9': {
    backend: `Release 0.9 scope (items marked 0.9 in docs/portal-api.md): the alembic migration; projects, files, snapshots, settings; the wizard rendering templates from config/code_templates/ (owned by dotnet-engineer — do not edit them); ZIP import/export; the assistant dialog with the shared request assembly (quality rules 3, 6, 7) reading prompts from config/code_prompts.yaml (owned by codegen-evaluator — do not edit it); parsing and validating the «files whole» edit, per-file accept (rule 4); the code-sandbox client, the fix loop (≤2 extra builds), build results; the «Скачать для CoGIS» archive; old CoGIS dialogs as a read-only list and transfer into a new project; flags (rule 9); portal/dev stub answering with an edit in the assistant format; owner filter and «→ 404» tests on every new route (rule 5); logs with numbers only (rule 11).`,
    frontend: `Release 0.9 scope (items marked 0.9 in docs/portal-ui.md): the «Код» section with Monaco (pinned, served by the portal, lazy-loaded only in this section — compare the main bundle size before/after; CSP needs at most worker-src 'self', report it for infra-engineer), file tree and tabs, completion of CoGIS names from the API reference, build markers in lines, the «Сборка» panel, the assistant column and the dialog memory panel per the concept, the wizard, «было / стало» with per-file accept, versions, project settings, import/export, archive download with the warning, old CoGIS dialogs list, flag off → the old CoGIS screen. Texts only from portal-ui.md (rule 12). Visual check per rule 13 (both themes, 1280 and 1920 px).`,
    evaluator: `Release 0.9 scope: config/code_prompts.yaml (system prompts and rules for actions write / explain / fix, keys per the contract) and portal/eval/code/ — 20–30 tasks in YAML and the runner that uses the real portal request assembly and the code-sandbox (rule 6), with a summary of shares «built at once / after fixes / not built». Run it on the stand stub to prove the runner; if the VM model is reachable with the developer key from scripts/.env.dev, also run it on the real model with the stub SDK and mark the result «проверить на ВМ с настоящими DLL».`,
    criteria: '33–37',
  },
  '1.0': {
    backend: `Release 1.0 scope (items marked 1.0 in docs/portal-api.md): project kinds and file rules for JS/HTML, templates, export notes per kind, request assembly differences, flag, tests.`,
    frontend: `Release 1.0 scope (items marked 1.0 in docs/portal-ui.md): kind choice and templates, the \`api\` type description for Monaco, the preview pane with the isolated frame, the \`api\` stub with fictitious layers and the call journal, console errors, export notes, flag off. The preview isolation test (rule 14). Visual check per rule 13.`,
    evaluator: `Release 1.0 scope: prompts for JS/HTML kinds in config/code_prompts.yaml and new tasks in portal/eval/code/ for each of the four kinds; check by syntax and by expected \`api\` calls; run on the stand stub (and on the real model if reachable).`,
    criteria: '39–41 and regression of 33–37',
  },
}

function contractStage(rel) {
  const c = CONTRACT[rel]
  return {
    waves: [[
      { agent: 'portal-architect', prompt: `${COMMON}\nStage «contract-${rel}». ${c.architect}` },
      { agent: 'ui-designer', prompt: `${COMMON}\nStage «contract-${rel}». ${c.designer}` },
    ]],
    checkers: [
      { agent: 'reviewer', recheck: 'failed', prompt: `${COMMON}
Stage «contract-${rel}» review. Read docs/portal-design.md 0.9 and the changes in docs/portal-api.md and docs/portal-ui.md (\`git diff HEAD -- docs/portal-api.md docs/portal-ui.md\`). Check: (1) both documents fully and faithfully cover ${c.review} — nothing missing, nothing beyond scope (no running of the built code, no deployment to the CoGIS server, no shared projects, no language server${rel === '0.9' ? ', no JS/HTML yet' : ''}); (2) contract and UI concept agree (endpoints, states, error codes, texts); (3) the contract matches the code it builds on (dialogs/context.py, dialogs/generation.py, tools/, migrations head, frontend screens) — cite file:line; (4) security per §8.5 and quality rules 1–5${rel === '1.0' ? ', 14' : ''}; (5) minimalism. Blocking = would cause a wrong implementation, inconsistency, scope violation or security hole. Do not run make check at this stage — only documents changed.` },
    ],
    fixers: ['portal-architect', 'ui-designer'],
    stop: `Владелец утверждает docs/portal-api.md и docs/portal-ui.md (${rel}); затем коммит «docs: контракт и концепция раздела «Код» ${rel}» и этап ${rel === '0.9' ? 'sandbox-0.9' : 'build-1.0'}.`,
  }
}

function sandboxStage() {
  return {
    waves: [
      [{ agent: 'dotnet-engineer', prompt: `${COMMON}
Stage «sandbox-0.9», zone portal/code-sandbox (except Dockerfile) and portal/backend/config/code_templates/. Implement the code-sandbox service exactly per the contract: Roslyn compilation without MSBuild, analyzers and generators (rule 1); references — .NET 8 reference assemblies, SDK folder DLLs, CoGIS server libraries with priority over packages, offline-set package DLLs; Russian diagnostics; GPTool rules by the semantic model; the API reference extraction from metadata (used by \`make cogis-sdk\`); input validation, time limit with cancellation, concurrency limit, numbers-only logs (rule 11); the stub SDK with only the names from the customer instruction (rule 10); the wizard templates that compile with the stub SDK in every form combination and pass the rules. Run dotnet format --verify-no-changes, dotnet build -warnaserror, dotnet test (in a pinned dotnet/sdk container if the SDK is not installed). In notes_for_other_zones give infra-engineer the exact runtime needs (base image, ports, mounts, env, healthcheck, user, limits) and scripts-engineer the CLI of the API reference extraction and the expected SDK folder layout.` }],
      [
        { agent: 'infra-engineer', prompt: `${COMMON}
Stage «sandbox-0.9», zone deploy/, portal/code-sandbox/Dockerfile, the stand compose in portal/dev. Build on the dotnet-engineer report below. Add: portal/code-sandbox/Dockerfile on a pinned .NET 8 runtime image (multi-stage build, non-root user); the code-sandbox service in the portal profile with its own internal network shared only with portal-api, no host ports, read-only root fs, tmpfs work dir with a size limit, cap_drop ALL, no-new-privileges, memory / CPU / pids limits, read-only mounts of deploy/cogis-sdk/ and the offline package volume, no portal secrets, container log not kept (rule 11), healthcheck; portal-api does not depend on it; the stand runs it with the stub SDK (or the real DLLs if deploy/cogis-sdk/ has them); .gitignore / .dockerignore so customer DLLs never enter git or images (rule 10); CSP worker-src 'self' only if the contract says so. Run make check (docker compose config for all profiles).` },
        { agent: 'scripts-engineer', prompt: `${COMMON}
Stage «sandbox-0.9», zone scripts/ and Makefile. Build on the dotnet-engineer report below. Add: \`make cogis-sdk\` — validates the SDK folder (required DLLs, list of the CoGIS plugins folder), writes the manifest with versions and sha256, runs the API reference extraction in the sandbox image, prints the SDK version; the offline package set download from nuget.org with sha512 verification into the volume (part of release preparation, like model weights); \`install.sh --stage portal\` creates the volume and leaves the build flag off; \`make check\` also builds and tests portal/code-sandbox if a .NET SDK container is available. Run make check.` },
      ],
    ],
    checkers: [
      { agent: 'reviewer', recheck: 'failed', recheckExtra: 'Run `make check` first — red is blocking.', prompt: `${COMMON}
Stage «sandbox-0.9» review. Review the changes in portal/code-sandbox, portal/backend/config/code_templates, deploy/, portal/dev, scripts/, Makefile (\`git diff HEAD\` plus untracked files). Run \`make check\` and \`dotnet test\` in portal/code-sandbox first — red is blocking. Check against the contract's code-sandbox protocol and §3.3, §7, §8.5: quality rules 1, 2, 10, 11 one by one with file:line (met / not met); container isolation (network, mounts, privileges, limits, no secrets); input validation and time limits with concrete inputs; minimalism.` },
    ],
    fixers: ['dotnet-engineer', 'infra-engineer', 'scripts-engineer'],
    stop: 'Коммит «feat(portal): песочница сборки C#», затем этап build-0.9.',
  }
}

function buildStage(rel) {
  const s = SCOPE[rel]
  const first = [
    { agent: 'backend-engineer', prompt: `${COMMON}
Stage «build-${rel}», zone portal/backend (except config/code_prompts.yaml and config/code_templates/) and portal/dev. ${s.backend}
The frontend and the prompts are built in parallel against the same contract — do not change response shapes or prompt keys. Run your zone checks (ruff format --check, ruff check, mypy, pytest); test the migration upgrade on a database with data of the previous release (rule 8).` },
    { agent: 'frontend-engineer', prompt: `${COMMON}
Stage «build-${rel}», zone portal/frontend. ${s.frontend}
The backend is built in parallel against the same contract; use the API client types from the contract. Run type check, lint, unit tests and production build; check that «контекст», «токен», «билд», «патч» do not appear in UI strings (rule 12).` },
    { agent: 'codegen-evaluator', prompt: `${COMMON}
Stage «build-${rel}», zone portal/backend/config/code_prompts.yaml and portal/eval/code/. ${s.evaluator}` },
  ]
  const waves = [first]
  if (rel === '1.0') {
    waves.push([
      { agent: 'infra-engineer', prompt: `${COMMON}
Stage «build-1.0», zone deploy/ and the stand compose. Build on the reports below: the preview path in the Caddyfile with its own CSP (no relaxation of the portal CSP), frame headers for that path only, serving the preview libraries; stand parity. Run make check.` },
      { agent: 'scripts-engineer', prompt: `${COMMON}
Stage «build-1.0», zone scripts/ and Makefile. Build on the reports below: \`make cogis-sdk\` also takes jQuery, Knockout and Leaflet files from the CoGIS client, verifies them and records versions; never in git (rule 10). Run make check.` },
    ])
  }
  return {
    waves,
    checkers: [
      { agent: 'reviewer', recheck: 'failed', recheckExtra: 'Run `make check` first — red is blocking.', prompt: `${COMMON}
Stage «build-${rel}» review. Review the stage changes (\`git diff HEAD\` plus untracked files from \`git status\`). Run \`make check\` and the zone checks first — red is blocking. Then check against docs/portal-api.md and docs/portal-ui.md (only the ${rel} items) and go through the CLAUDE.md quality rules 1–17 of «Пайплайн «Код»» one by one (rules 18–19 are for the docs stage and the orchestrator): for each — met / not met / not applicable with file:line. Also: correctness with concrete inputs (edit parsing, path validation, fix loop limits, archive contents), races between generation, edits and builds, owner filter in every new query, numbers-only logs, prompts and eval sharing one request assembly, minimalism (nothing beyond ${rel} scope).` },
    ],
    fixers: rel === '1.0'
      ? ['backend-engineer', 'frontend-engineer', 'codegen-evaluator', 'infra-engineer', 'scripts-engineer']
      : ['backend-engineer', 'frontend-engineer', 'codegen-evaluator'],
    stop: `Коммит «feat(portal): раздел «Код» ${rel}», затем этап accept-${rel}.`,
  }
}

function acceptStage(rel) {
  const s = SCOPE[rel]
  return {
    waves: [],
    checkers: [
      { agent: 'qa-engineer', recheck: 'always', recheckExtra: 'Re-run the e2e suite of this feature; write new tests only where a fix needs one.', prompt: `${COMMON}
Stage «accept-${rel}». Write and run Playwright end-to-end tests in portal/frontend/e2e on the stand portal/dev for portal-design §10 criteria ${s.criteria} (parts without [ВМ]), with the real code-sandbox and the stub SDK; include isolation checks through the API for every new route (other user's project / file / version / build / archive → not found) and the flag-off behaviour.${rel === '1.0' ? ' Include the preview isolation test (rule 14).' : ''} For [ВМ] parts write a step-by-step manual checklist (real CoGIS DLLs, installing the archive in CoGIS, real model quality). Each defect → blocking finding with the zone owner (backend-engineer / frontend-engineer / dotnet-engineer / infra-engineer; your own test bugs → qa-engineer).` },
      { agent: 'security-reviewer', recheck: 'always', recheckExtra: 'Review the fix diffs for new security issues.', prompt: `${COMMON}
Stage «accept-${rel}». Security review of the feature changes since main (\`git diff main -- portal/ deploy/ scripts/ Makefile\`): the code-sandbox (no code execution at build time — analyzers, generators, MSBuild; input paths and limits; container network, mounts, privileges, limits, secrets; denial of service), isolation of every new route and query, prompt injection through project files, compiler messages, schema and knowledge-base documents (user role, data_block, never system), validation of paths and extensions in the model's edit, archive contents (no path traversal in ZIP import and export), customer property out of git and images, numbers-only logs, behaviour with the flags off${rel === '1.0' ? ', and the preview isolation (iframe sandbox without allow-same-origin, own CSP, no access to cookies / storage / API, no network) — blocking area' : ''}. Blocking findings name the zone owner.` },
      { agent: 'ui-designer', model: LIGHT, recheck: 'failed', prompt: `${COMMON}
Stage «accept-${rel}», design review. Compare the implemented UI in portal/frontend/src with docs/portal-ui.md (${rel} items): the «Код» screen layout, editor themes, build panel states, assistant column and memory panel, wizard, «было / стало», versions, archive note and warning${rel === '1.0' ? ', kinds, templates, preview pane and journal' : ''}, texts, both themes, keyboard. Blocking = wrong behaviour, wrong or missing texts, missing states; cosmetics → minor. Name the zone owner (frontend-engineer).` },
    ],
    fixers: ['backend-engineer', 'frontend-engineer', 'dotnet-engineer', 'infra-engineer', 'qa-engineer'],
    stop: `Коммит «test(portal): приёмка раздела «Код» ${rel}», затем этап ${rel === '0.9' ? 'eval-0.9 (или docs-0.9, если ВМ недоступна)' : 'docs-1.0'}.`,
  }
}

function evalStage() {
  return {
    waves: [[
      { agent: 'codegen-evaluator', schema: EVAL_REPORT, prompt: `${COMMON}
Stage «eval-0.9», zone portal/backend/config/code_prompts.yaml and portal/eval/code/. Run the control set on the real model (VM through Bifrost with the developer key from scripts/.env.dev) and the code-sandbox with the real CoGIS DLLs from deploy/cogis-sdk/ if present (otherwise the stub SDK — say so). Record shares «built at once / after fixes / not built» and the rules pass rate. Improve prompts and, if the contract allows it through config, the API reference budget; after each change re-run and keep only changes that do not worsen any task group. If the real model is unreachable, stop after the stub run and report it. No secrets in files or reports.` },
    ]],
    checkers: [
      { agent: 'reviewer', model: LIGHT, recheck: 'failed', prompt: `${COMMON}
Stage «eval-0.9» review, prompts only (\`git diff HEAD -- portal/backend/config/code_prompts.yaml portal/eval/code\`). Check: prompts still name all untrusted content as data and require the «files whole» format (rules 3, 4); no secrets or customer DLL content in tracked files (rule 10); the runner still uses the shared request assembly (rule 6); the before/after shares in the evaluator report are consistent with the summary file. Blocking only for these.` },
    ],
    fixers: ['codegen-evaluator'],
    stop: 'Коммит «feat(portal): промпты раздела «Код» по замеру». Владелец задаёт порог критерия 38; затем этап docs-0.9.',
  }
}

// Release instructions go to production: the writer proves every command against the code and
// justifies every step; one light Sonnet walkthrough checks completeness and absence of extras.
function docsStage(rel) {
  const prev = rel === '0.9' ? '0.8' : '0.9'
  const specific = rel === '0.9'
    ? 'the new code-sandbox service and the offline package volume (downloaded during preparation, sums verified), copying the CoGIS DLLs and the plugins folder listing from the CoGIS server into deploy/cogis-sdk/ (exact source paths, a copy command, the check), `make cogis-sdk`, the build flag off at release and switching it on after the check on real DLLs'
    : 'the Caddy preview path, copying jQuery / Knockout / Leaflet files from the CoGIS client and repeating `make cogis-sdk`'
  return {
    waves: [[
      { agent: 'docs-writer', model: LIGHT, schema: DOCS_REPORT, prompt: `${COMMON}
Stage «docs-${rel}». Follow your agent rules for docs/release-<version>.md and CLAUDE.md quality rule 18 exactly.
Write docs/release-${rel}.md — a short but complete instruction: from «${prev} is running» to «${rel} works and is accepted», in the format of the latest docs/release-*.md (variables, «Перед началом», numbered steps each with ONE check — command, expected result, what to do on mismatch — and «Откат»). It must cover (portal-design §8.5): backup before the migration (\`make release-backup\`), rehearsal on a copy (\`make release-rehearse\`), the migration only adds tables and fields, ${specific}, the first rollback line is the «Код» feature flag (old CoGIS helper returns), code rollback after the migration only by restoring the backup, new config keys and .env variables (or explicitly «нет»). Acceptance on the VM: only the [ВМ] parts of criteria ${rel === '0.9' ? '33–38' : '39–41'}, one check each. After the rollout: only mandatory actions. Appendix «Установка с нуля»: only the differences from docs/vpn-launch.md for a new VM${rel === '1.0' ? ' straight to 1.0' : ''}, each with the vpn-launch.md step it follows; do not copy vpn-launch.md. Nothing else: no «just in case» checks, no repeated checks, no optional steps, explanations at most one line.
Update docs/user-guide.md (a short «Код» section for employees, names exactly as in docs/portal-ui.md, no jargon) and docs/runbook.md (flags, \`make cogis-sdk\`, sandbox health, log lines).${rel === '0.9' ? ' Update docs/devops-request.md: nuget.org access during release preparation only.' : ''}
Self-check: every command, make target, variable, config key and log line — its definition with file:line in commands_checked (unconfirmed → remove or list in unverified); every step and check of the release instruction in steps_justified (a step you cannot justify — delete it). Safe order: backup before migration, a check after each step, a rollback path.` },
    ]],
    checkers: [
      { agent: 'reviewer', model: LIGHT, recheck: 'failed', prompt: `${COMMON}
Stage «docs-${rel}» review: walk through docs/release-${rel}.md as the administrator on the VM who has only this file. Blocking: (1) a missing action — something the release needs (portal-design §7, §8.5, the Makefile and install script, compose services, new config keys, external files from the CoGIS server) that the instruction does not do, or a step that depends on another document; (2) an extra action — a step or check that is not needed for the release to work, for rollback or for a [ВМ] criterion of ${rel}; a repeated check; (3) a step without a check, a check without an expected result or without the action on mismatch; (4) unsafe order (migration before backup, flag on before the check on real DLLs); (5) the «Установка с нуля» appendix misses a difference from docs/vpn-launch.md or copies it. Verify commands only against Makefile / scripts / config (do not run anything on the VM). Everything else → minor.` },
    ],
    fixers: ['docs-writer'],
    stop: rel === '0.9'
      ? 'Коммит «docs: релиз 0.9». Владелец сливает feat/code в main и раскатывает по docs/release-0.9.md (в нём — make cogis-sdk и включение сборки); затем contract-1.0.'
      : 'Коммит «docs: релиз 1.0». Владелец сливает и раскатывает по docs/release-1.0.md.',
  }
}

const STAGES = {
  'contract-0.9': () => contractStage('0.9'),
  'sandbox-0.9': sandboxStage,
  'build-0.9': () => buildStage('0.9'),
  'accept-0.9': () => acceptStage('0.9'),
  'eval-0.9': evalStage,
  'docs-0.9': () => docsStage('0.9'),
  'contract-1.0': () => contractStage('1.0'),
  'build-1.0': () => buildStage('1.0'),
  'accept-1.0': () => acceptStage('1.0'),
  'docs-1.0': () => docsStage('1.0'),
}

if (!STAGES[STAGE]) {
  return { error: `Unknown stage «${STAGE}». Pass args: {stage: one of ${Object.keys(STAGES).join(', ')}}` }
}
const stage = STAGES[STAGE]()
log(`Stage ${STAGE}`)

const opts = (base, model) => (model ? { ...base, model } : base)
const uniq = xs => [...new Set(xs.filter(Boolean))]
const short = (x, n) => (x.length > n ? `${x.slice(0, n)}…` : x)
const briefOf = rs => Object.entries(rs).map(([k, r]) => r
  ? `### ${k}\n${JSON.stringify({ summary: r.summary, changed_files: r.changed_files, deviations: r.deviations, notes_for_other_zones: r.notes_for_other_zones })}`
  : `### ${k}\n(no report: the agent failed)`).join('\n')

phase('Work')
const reports = {}
const authors = stage.waves.flat()
for (const wave of stage.waves) {
  // A later wave sees only the short reports of earlier waves, not their transcripts.
  const earlier = briefOf(reports)
  const results = await parallel(wave.map(a => () =>
    agent(earlier ? `${a.prompt}\nShort reports of earlier agents of this stage:\n${earlier}` : a.prompt,
      opts({ label: a.agent, phase: 'Work', agentType: a.agent, schema: a.schema || REPORT }, a.model))))
  wave.forEach((a, i) => { reports[a.agent] = results[i] })
}

const brief = briefOf(reports)
const latest = {}
const rounds = []
const minor = []
const questions = Object.values(reports).filter(Boolean).flatMap(r => r.owner_questions)
const authorFailed = Object.entries(reports).filter(([, r]) => !r).map(([k]) =>
  ({ agent: k, where: k, issue: 'author agent failed — rerun the stage', fix: 'rerun' }))
let blocking = []
let fixResults = []
let fixedFiles = []

for (let round = 1; round <= MAX_ROUNDS && stage.checkers.length; round++) {
  const due = round === 1
    ? stage.checkers
    : stage.checkers.filter(c => c.recheck === 'always' || latest[c.agent].blocking.length)
  const results = await parallel(due.map(c => () => {
    const prompt = round === 1
      ? `${c.prompt}\nRound 1.${brief ? `\nShort reports of the stage agents:\n${brief}` : ''}`
      : `${COMMON}
Stage «${STAGE}», re-check round ${round} as ${c.agent}. Do NOT re-review the whole stage. ${c.recheckExtra || ''}
1) Verify that each of your open findings below is fixed (otherwise keep it as blocking).
2) Look only at the files changed by the fixes for new blocking issues in your area.
Your open findings: ${JSON.stringify(latest[c.agent].blocking)}
Fix results: ${JSON.stringify(fixResults)}
Files changed by the fixes: ${JSON.stringify(fixedFiles)}
Report only what is still open or new; minor — only new ones.`
    return agent(prompt, opts({ label: `${c.agent}:r${round}`, phase: 'Check', agentType: c.agent, schema: FINDINGS(stage.fixers) }, c.model))
  }))
  due.forEach((c, i) => {
    const r = results[i] || {
      verdict: 'нужны исправления', minor: [], owner_questions: [], summary: 'checker failed',
      blocking: [{ agent: stage.fixers[0], where: c.agent, issue: 'checker agent failed — rerun the stage', fix: 'rerun' }],
    }
    latest[c.agent] = r
    minor.push(...r.minor)
    questions.push(...r.owner_questions)
  })
  blocking = Object.values(latest).flatMap(r => r.blocking)
  rounds.push({ round, checked: due.map(c => c.agent), blocking: blocking.length })
  log(`Round ${round}: ${blocking.length} blocking`)
  if (!blocking.length || round === MAX_ROUNDS) break

  phase('Fix')
  const owners = uniq(blocking.map(b => b.agent))
  const fixes = await parallel(owners.map(owner => () => {
    const others = blocking.filter(b => b.agent !== owner).map(b => `${b.agent}: ${b.where} — ${short(b.issue, 200)}`)
    const author = authors.find(a => a.agent === owner)
    return agent(`${COMMON}
Stage «${STAGE}», fix round ${round}. Fix the blocking findings addressed to you, in your zone only:
${JSON.stringify(blocking.filter(b => b.agent === owner), null, 1)}
${others.length ? `Fixed in parallel by other zones (keep consistent, do not touch):\n${others.join('\n')}\n` : ''}Re-run your zone checks. Report per finding: how fixed, or why not.`,
      opts({ label: `fix:${owner}:r${round}`, phase: 'Fix', agentType: owner, schema: FIX_REPORT }, author && author.model))
  }))
  fixResults = []
  fixedFiles = []
  owners.forEach((owner, i) => {
    const f = fixes[i]
    if (!f) { fixResults.push({ owner, status: 'fixer failed' }); return }
    fixResults.push(...f.fixes.map(x => ({ owner, ...x })), { owner, checks: f.checks })
    fixedFiles.push(...f.changed_files)
    questions.push(...f.owner_questions)
  })
  fixedFiles = uniq(fixedFiles)
}

blocking.push(...authorFailed)
const docs = STAGE.startsWith('docs-') ? reports['docs-writer'] : null
if (docs && docs.unverified && docs.unverified.length) {
  blocking.push(...docs.unverified.map(u => ({ agent: 'docs-writer', where: 'docs', issue: `unverified: ${u}`, fix: 'confirm in code or remove' })))
}
const evaluation = STAGE === 'eval-0.9' ? reports['codegen-evaluator'] : null

return {
  stage: STAGE,
  verdict: blocking.length ? 'нужны исправления — показать владельцу' : 'можно коммитить',
  blocking,
  minor: uniq(minor),
  owner_questions: uniq(questions),
  check_on_vm: uniq(Object.values(reports).filter(Boolean).flatMap(r => r.check_on_vm)),
  authors: Object.fromEntries(Object.entries(reports).map(([k, r]) => [k, r ? { summary: r.summary, checked_locally: r.checked_locally, deviations: r.deviations } : null])),
  checkers: Object.fromEntries(Object.entries(latest).map(([k, r]) => [k, r.summary])),
  evaluation: evaluation ? { model: evaluation.model, before: evaluation.shares_before, after: evaluation.shares_after, worst: evaluation.worst_tasks } : undefined,
  commands_checked: docs ? docs.commands_checked.length : undefined,
  rounds,
  next: stage.stop,
}
