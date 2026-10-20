export const meta = {
  name: 'dialog-memory',
  description: '«Память диалога» 0.7–0.8: one pipeline stage per run (args.stage) — zone agents, then checks and fixes (max 3 rounds); no commits',
  whenToUse: 'Only on the owner\'s command, one stage at a time: contract, build-0.7, accept-0.7, docs-0.7, build-0.8, accept-0.8, docs-0.8 (CLAUDE.md, «Пайплайн «Память диалога»»)',
  phases: [
    { title: 'Work', detail: 'zone agents of the stage, in parallel' },
    { title: 'Check', detail: 'reviewer, or qa ∥ security ∥ design review at acceptance' },
    { title: 'Fix', detail: 'zone owners fix blocking findings' },
  ],
}

const STAGE = typeof args === 'string' ? args : args && args.stage
const MAX_ROUNDS = 3

const COMMON = `Feature «Память диалога» (dialog memory, a context manager) of the employee portal, branch feat/context.
Source of truth: docs/portal-design.md 0.7 — §5.7, §6, §7, §8, §8.4, §10 criteria 29–32, §11, §13 decisions 1–12 (\`git diff main -- docs/portal-design.md\` shows the change). Contract: docs/portal-api.md. UI concept: docs/portal-ui.md.
Follow CLAUDE.md, in particular section «Пайплайн «Память диалога» (0.7–0.8)» and its quality rules 1–12.
Background research (reference only; where it differs from the design, the design wins): .claude/notes/dialog-memory/plan.md, .claude/notes/dialog-memory/code-map.md.
Change files only in your zone. Never edit docs/portal-design.md: a gap or contradiction there goes to «Вопросы к владельцу» in your report. Ignore .claude/prompt.md (owner's file). Do not commit.
Your final text is the report: what was done, checks run and their result («проверено локально» vs «проверить на ВМ»), deviations from contract/concept, notes for other zones, «Вопросы к владельцу».`

const SCOPE = {
  '0.7': {
    backend: `Release 0.7 scope (items marked 0.7 in docs/portal-api.md): alembic migration after the current head adding the per-pair context mode (full / text_only / excluded); ONE shared estimate function used both by the context endpoint and by request assembly (quality rule 1); the context estimate endpoint; the pair-mode endpoint with 409 generation_in_progress while an answer is generated; excluded / text_only filtering in request assembly and in the history used for search-query rewriting, regeneration follows the choice; \`usage\` from the stream (stream_options.include_usage) logged as numbers «estimate vs actual», absence tolerated; feature flag in YAML and /api/config; portal/dev stub returns usage; head number updated in tests; the important tests from quality rules 1–7.`,
    frontend: `Release 0.7 scope (items marked 0.7 in docs/portal-ui.md): indicator in the Composer for chat, SQL, CoGIS and document questions (levels in words, percent in the hint), clicking it opens the panel; the memory panel as a hideable right column on all dialog screens — open on first visit, open/hidden state in localStorage, overlays the feed below 1440 px, yields its place to the source viewer and comes back, toggle in the title bar; list of pairs with the three-state choice and «не поместится» marks; dimmed excluded pairs in the feed; long-dialog notice from 80 %; refresh triggers per §5.7; flag off → no indicator and no panel; error texts from portal-ui.md. Visual check per quality rule 9 (both themes, 1280 and 1920 px, with source viewer, after reload).`,
    criteria: '29–30',
  },
  '0.8': {
    backend: `Release 0.8 scope (items marked 0.8 in docs/portal-api.md): alembic migration adding the dialog fields for the summary and carried text with its source (source deletion → link cleared); streaming summary draft endpoint (main model, minimal reasoning, one draft per user, 429 on overload, summary length limit from config, document dialogs include summary and requisites, last N pairs kept per config); apply / edit / cancel summary; the summary replaces the covered pairs in request assembly and goes with carried text as the head turn in the user role via data_block, never system, never dropped (quality rule 3); creating a chat / SQL / CoGIS dialog with carried text (summary or one answer verbatim); «Продолжить отсюда»; portal/dev stub streams a fake summary; the important tests from quality rules 1–7.`,
    frontend: `Release 0.8 scope (items marked 0.8 in docs/portal-ui.md): compression flow inside the right memory column (streamed draft, edit, «Применить», close without changes), the summary boundary line in the feed with a link to the summary, «Изменить» and «Отменить сжатие»; «Сжать раннюю часть» also in the long-dialog notice; transfer modal from the dialog Kebab, the history list and the panel (destination, what to take, editable text, note that files and knowledge-base findings are not transferred); «Сведения из «…»» notice in the new dialog; «Продолжить отсюда» on each answer; 409 / 429 texts from portal-ui.md. Visual check per quality rule 9.`,
    criteria: '31–32 and regression of 29–30',
  },
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
          issue: { type: 'string', description: 'what is wrong, with concrete input → wrong result' },
          fix: { type: 'string' },
        },
        required: ['agent', 'where', 'issue', 'fix'],
      },
    },
    minor: { type: 'array', items: { type: 'string' } },
    owner_questions: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
  required: ['verdict', 'blocking', 'minor', 'owner_questions', 'summary'],
})

function contractStage() {
  return {
    authors: [
      { agent: 'portal-architect', prompt: `${COMMON}
Stage «contract». Update docs/portal-api.md for BOTH releases in one revision, each item marked 0.7 or 0.8 so 0.7 can ship first; separate alembic revisions for 0.7 and 0.8 (check the current head in portal/backend/migrations/versions). Cover: config flag in /api/config and YAML keys; data model and migrations (columns, constraints, defaults, FK on delete); endpoints with request/response schemas and error codes — context estimate with per-pair breakdown and the exact budget formula consistent with dialogs/generation.py and dialogs/context.py fit_history, pair mode, summary draft (SSE), apply/edit/cancel summary, dialog creation with carried text, «Продолжить отсюда»; request assembly order (summary and carried text as a user-role data_block head turn, never system; filtering of excluded / text_only pairs and of pairs covered by the summary, also in the history used for query rewriting; budget accounting of the head turn; regeneration); concurrency (409 while generating, one draft per user, 429 on overload); usage parsing from the stream (include_usage, chunk with empty choices, absence tolerated, log line with numbers only); per-kind specifics (SQL/CoGIS plans, document reserve and summary/requisites); portal/dev stub behaviour; tests that must exist. Verify every fact against the code before writing it. Bump the document version and add a changelog entry in the document's style.` },
      { agent: 'ui-designer', prompt: `${COMMON}
Stage «contract». Update docs/portal-ui.md for BOTH releases in one revision, items marked 0.7 or 0.8. The memory panel is NOT a modal SidePage: it is a hideable right column (decisions 10–12 in portal-design §13) on all dialog screens (chat, SQL, CoGIS, document questions; not on knowledge base, users, profile) — 320 px, the feed narrows; open on first visit, state remembered in the browser like the theme; below 1440 px it overlays the feed; the source viewer (portal-ui §4.4) temporarily takes its place; toggle in the title bar; the compact indicator in the Composer is always visible and opens the panel. Update the frame and widths (§3.2, §3.3), keyboard/focus order (§9), own-components summary (§10). Cover: indicator levels, hint and states (loading, error, flag off); panel structure — level on top, pair list with the three-state choice, «не поместится» / «в пересказе» marks, empty states; feed changes (dimmed excluded pairs with label, summary boundary line, «Продолжить отсюда»); long-dialog notice from 80 % with «Сжать раннюю часть»; compression draft flow (streaming, edit, apply, cancel, errors 409/429/422); transfer modal and its entry points (dialog Kebab, history list, panel); «Сведения из «…»» notice; both themes; dictionary (keep «контекст» banned, add «память диалога», «пересказ» etc.); final Russian texts. Read the actual frontend (portal/frontend/src: Composer, MessageList, DialogScreen, DocumentsScreen, HistoryPanel, sql/SchemasPanel) so the concept fits existing components. Bump the document version and add a changelog entry in the document's style.` },
    ],
    checkers: [
      { agent: 'reviewer', rerun: true, prompt: `${COMMON}
Stage «contract» review. Read docs/portal-design.md 0.7 and the changes in docs/portal-api.md and docs/portal-ui.md (\`git diff main -- docs/portal-api.md docs/portal-ui.md\`). Check: (1) both documents fully and faithfully cover §5.7, §6, §7, §8, §8.4, §10 p.29–32, §13 — nothing missing, nothing beyond scope (no pinning, snapshots, auto-compression, transfer into document parsing); panel is a hideable right column per decisions 10–12; (2) contract and UI concept agree (endpoints, states, error codes, texts, 0.7 vs 0.8 split); (3) the contract matches the code it builds on (portal/backend/src/portal/dialogs/context.py, generation.py, llm/bifrost.py, tools, migrations head) — cite file:line; (4) security: summary and carried text only in the user role as data, isolation, limits; (5) minimalism. Blocking = would cause a wrong implementation, inconsistency, scope violation or security hole. Do not run make check at this stage — only documents changed.` },
    ],
    fixers: ['portal-architect', 'ui-designer'],
    stop: 'Владелец утверждает docs/portal-api.md и docs/portal-ui.md; затем коммит «docs: контракт и концепция «Память диалога»» и этап build-0.7.',
  }
}

function buildStage(rel) {
  const s = SCOPE[rel]
  return {
    authors: [
      { agent: 'backend-engineer', prompt: `${COMMON}
Stage «build-${rel}», zone portal/backend and portal/dev. ${s.backend}
The frontend is built in parallel against the same contract — do not change response shapes. Run your zone checks (ruff format --check, ruff check, mypy, pytest); test the migration upgrade on a database with data of the previous release (quality rule 6).` },
      { agent: 'frontend-engineer', prompt: `${COMMON}
Stage «build-${rel}», zone portal/frontend. ${s.frontend}
The backend is built in parallel against the same contract; use the API client types from the contract. Run type check, lint, unit tests and production build; check that the Russian words «контекст» and «токен» do not appear in UI strings (quality rule 8).` },
    ],
    checkers: [
      { agent: 'reviewer', rerun: true, prompt: `${COMMON}
Stage «build-${rel}» review. Review the stage changes in portal/backend, portal/dev and portal/frontend (\`git diff HEAD\` plus untracked files from \`git status\`). Run \`make check\` and the zone checks first — red is blocking. Then check against docs/portal-api.md and docs/portal-ui.md (only the ${rel} items) and go through the CLAUDE.md quality rules 1–12 for this feature one by one: for each — met / not met with file:line. Also: correctness with concrete inputs, races between generation and memory changes, owner filter in every new query, no message text in logs, minimalism (nothing beyond ${rel} scope).` },
    ],
    fixers: ['backend-engineer', 'frontend-engineer'],
    stop: `Коммит «feat(portal): память диалога ${rel}», затем этап accept-${rel}.`,
  }
}

function acceptStage(rel) {
  const s = SCOPE[rel]
  return {
    authors: [],
    checkers: [
      { agent: 'qa-engineer', rerun: true, prompt: `${COMMON}
Stage «accept-${rel}». Write and run Playwright end-to-end tests in portal/frontend/e2e on the stand portal/dev for portal-design §10 criteria ${s.criteria} (parts without [ВМ]); include isolation checks through the API for every new route (other user's dialog → not found) and the panel behaviour (hide/show survives reload, works on chat, SQL, CoGIS, documents). For [ВМ] parts write a step-by-step manual checklist. Each defect → blocking finding with the zone owner (backend-engineer / frontend-engineer; your own test bugs → qa-engineer).` },
      { agent: 'security-reviewer', rerun: true, prompt: `${COMMON}
Stage «accept-${rel}». Security review of the feature changes since main (\`git diff main -- portal/\`): isolation of every new route and query, prompt injection through summary / carried text / pair content (user role, data_block, closing-tag neutralization, never system), limits and overload (draft concurrency, summary length, 429), logs without message text, behaviour with the flag off. Blocking findings name the zone owner.` },
      { agent: 'ui-designer', rerun: false, prompt: `${COMMON}
Stage «accept-${rel}», design review. Compare the implemented UI in portal/frontend/src with docs/portal-ui.md (${rel} items): the right memory column (width, hide/show, overlay below 1440 px, yielding to the source viewer), indicator, pair choice, feed marks, notices, dialogs, texts, both themes, keyboard. Blocking = wrong behaviour, wrong or missing texts, missing states; cosmetics → minor. Name the zone owner (frontend-engineer).` },
    ],
    fixers: ['backend-engineer', 'frontend-engineer', 'qa-engineer'],
    stop: `Коммит «test(portal): приёмка памяти диалога ${rel}», затем этап docs-${rel}.`,
  }
}

function docsStage(rel) {
  return {
    authors: [
      { agent: 'docs-writer', prompt: `${COMMON}
Stage «docs-${rel}». Write docs/release-${rel}.md — a short, clear instruction for rolling release ${rel} onto the running service, in the format of the latest docs/release-*.md: variables, «Перед началом», numbered steps each with a check, rollback. It must cover: backup before the migration (\`make release-backup\`), rehearsal on a copy (\`make release-rehearse\`), the migration only adds fields, the first rollback line is the feature flag, code rollback after the migration only by restoring the backup (portal-design §8.4), new config keys and .env variables (or explicitly «нет»).${rel === '0.7' ? ' After the rollout: how to collect calibration pairs «оценка — факт» from the log and where to put the refined coefficients (config override).' : ' Precondition: calibration from 0.7 applied (§8.4); after the rollout — measuring summary latency on the GPU [ВМ].'} Update docs/user-guide.md (a short «Память диалога» section for employees, button names as in the UI, no words «контекст», «токен») and docs/runbook.md (flag, config keys, log lines). Every command — checked against the Makefile and scripts.` },
    ],
    checkers: [
      { agent: 'reviewer', rerun: true, prompt: `${COMMON}
Stage «docs-${rel}» review of docs/release-${rel}.md, docs/user-guide.md and docs/runbook.md changes (\`git diff HEAD -- docs/\` plus untracked). Every command, make target, variable and config key exists in the code (Makefile, scripts, portal/backend/config, deploy/.env.example); steps are in a safe order (backup before migration, check after each step, rollback path); the instruction is short and unambiguous; user-guide matches portal-ui.md texts. Do not run make check.` },
    ],
    fixers: ['docs-writer'],
    stop: rel === '0.7'
      ? 'Коммит «docs: релиз 0.7». Владелец сливает feat/context в main, раскатывает по docs/release-0.7.md и собирает калибровку на ВМ; затем этап build-0.8.'
      : 'Коммит «docs: релиз 0.8». Владелец сливает и раскатывает по docs/release-0.8.md после калибровки.',
  }
}

const STAGES = {
  contract: contractStage,
  'build-0.7': () => buildStage('0.7'),
  'accept-0.7': () => acceptStage('0.7'),
  'docs-0.7': () => docsStage('0.7'),
  'build-0.8': () => buildStage('0.8'),
  'accept-0.8': () => acceptStage('0.8'),
  'docs-0.8': () => docsStage('0.8'),
}

if (!STAGES[STAGE]) {
  return { error: `Unknown stage «${STAGE}». Pass args: {stage: one of ${Object.keys(STAGES).join(', ')}}` }
}
const stage = STAGES[STAGE]()
log(`Stage ${STAGE}`)

phase('Work')
const reports = {}
const work = await parallel(stage.authors.map(a => () =>
  agent(a.prompt, { label: a.agent, phase: 'Work', agentType: a.agent })))
stage.authors.forEach((a, i) => { reports[a.agent] = work[i] || '(no report: the agent failed)' })

const latest = {}
const rounds = []
let blocking = []
for (let round = 1; round <= MAX_ROUNDS; round++) {
  const prev = rounds[rounds.length - 1]
  const due = stage.checkers.filter(c => round === 1 || c.rerun || (prev && prev.failed.includes(c.agent)))
  const history = rounds.length ? `\nPrevious rounds (check that their blocking findings are fixed):\n${JSON.stringify(rounds, null, 1)}` : ''
  const reportText = Object.keys(reports).length
    ? `\nReports of the stage agents:\n${Object.entries(reports).map(([k, v]) => `### ${k}\n${v}`).join('\n\n')}`
    : ''
  const results = await parallel(due.map(c => () =>
    agent(`${c.prompt}\nRound ${round}.${history}${reportText}`,
      { label: `${c.agent}:r${round}`, phase: 'Check', agentType: c.agent, schema: FINDINGS(stage.fixers) })))
  due.forEach((c, i) => {
    latest[c.agent] = results[i] || {
      verdict: 'нужны исправления', minor: [], owner_questions: [], summary: 'checker failed',
      blocking: [{ agent: stage.fixers[0], where: c.agent, issue: 'checker agent failed — rerun the stage', fix: 'rerun' }],
    }
  })
  blocking = Object.values(latest).flatMap(r => r.blocking)
  const failed = Object.entries(latest).filter(([, r]) => r.blocking.length).map(([k]) => k)
  rounds.push({ round, failed, blocking })
  log(`Round ${round}: ${blocking.length} blocking`)
  if (!blocking.length || round === MAX_ROUNDS) break

  phase('Fix')
  const owners = [...new Set(blocking.map(b => b.agent))]
  const minor = Object.values(latest).flatMap(r => r.minor)
  const fixes = await parallel(owners.map(owner => () =>
    agent(`${COMMON}
Stage «${STAGE}», fix round ${round}. Fix the blocking findings addressed to you, in your zone only:
${JSON.stringify(blocking.filter(b => b.agent === owner), null, 1)}
All blocking findings, for consistency with other zones fixed in parallel:
${JSON.stringify(blocking, null, 1)}
Minor findings — fix only if cheap and in your zone: ${JSON.stringify(minor)}
Re-run your zone checks. Report per finding: how fixed, or why not.`,
      { label: `fix:${owner}:r${round}`, phase: 'Fix', agentType: owner })))
  owners.forEach((owner, i) => {
    reports[owner] = `${reports[owner] || ''}\n\n[Fix round ${round}]\n${fixes[i] || '(no report: the agent failed)'}`
  })
}

const checks = Object.values(latest)
return {
  stage: STAGE,
  verdict: blocking.length ? 'нужны исправления — показать владельцу' : 'можно коммитить',
  blocking,
  minor: checks.flatMap(r => r.minor),
  owner_questions: checks.flatMap(r => r.owner_questions),
  summaries: Object.fromEntries(Object.entries(latest).map(([k, r]) => [k, r.summary])),
  reports,
  rounds: rounds.map(r => ({ round: r.round, blocking: r.blocking.length })),
  next: stage.stop,
}
