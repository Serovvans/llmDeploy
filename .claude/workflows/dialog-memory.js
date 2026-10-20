export const meta = {
  name: 'dialog-memory',
  description: '«Память диалога» 0.7–0.8: one pipeline stage per run (args.stage) — zone agents, then checks and fixes (max 3 rounds); no commits',
  whenToUse: 'Only on the owner\'s command, one stage at a time: contract, build-0.7, accept-0.7, docs-0.7, build-0.8, accept-0.8, docs-0.8 (CLAUDE.md, «Пайплайн «Память диалога»»). args: {stage, notes?} — notes are short conclusions of earlier stages',
  phases: [
    { title: 'Work', detail: 'zone agents of the stage, in parallel' },
    { title: 'Check', detail: 'reviewer, or qa ∥ security ∥ design review at acceptance' },
    { title: 'Fix', detail: 'zone owners fix blocking findings' },
  ],
}

// Token economy: Opus only where an error is expensive (contract, code, security);
// Sonnet for mechanical work (docs, design comparison). One quality review per stage,
// no separate critic for low-risk docs. Agents hand over short structured reports,
// re-checks look only at open findings and fixes, not at the whole stage again.

const STAGE = typeof args === 'string' ? args : args && args.stage
const NOTES = (args && typeof args === 'object' && args.notes) || ''
const MAX_ROUNDS = 3
const LIGHT = 'sonnet'

const COMMON = `Feature «Память диалога» (dialog memory, a context manager) of the employee portal, branch feat/context.
Source of truth: docs/portal-design.md 0.7 — §5.7, §6, §7, §8, §8.4, §10 criteria 29–32, §11, §13 decisions 1–12 (\`git diff main -- docs/portal-design.md\` shows the change). Contract: docs/portal-api.md. UI concept: docs/portal-ui.md.
Follow CLAUDE.md, in particular section «Пайплайн «Память диалога» (0.7–0.8)» and its quality rules 1–12.
Background research (reference only; where it differs from the design, the design wins): .claude/notes/dialog-memory/plan.md, .claude/notes/dialog-memory/code-map.md.
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
  },
  required: [...REPORT.required, 'commands_checked', 'unverified'],
}

// Checker fields: recheck — 'failed' (re-run only with own open findings) or 'always'
// (re-run after any fix, scoped to the fixes); recheckExtra — what to repeat on a re-check.
function contractStage() {
  return {
    authors: [
      { agent: 'portal-architect', prompt: `${COMMON}
Stage «contract». Update docs/portal-api.md for BOTH releases in one revision, each item marked 0.7 or 0.8 so 0.7 can ship first; separate alembic revisions for 0.7 and 0.8 (check the current head in portal/backend/migrations/versions). Cover: config flag in /api/config and YAML keys; data model and migrations (columns, constraints, defaults, FK on delete); endpoints with request/response schemas and error codes — context estimate with per-pair breakdown and the exact budget formula consistent with dialogs/generation.py and dialogs/context.py fit_history, pair mode, summary draft (SSE), apply/edit/cancel summary, dialog creation with carried text, «Продолжить отсюда»; request assembly order (summary and carried text as a user-role data_block head turn, never system; filtering of excluded / text_only pairs and of pairs covered by the summary, also in the history used for query rewriting; budget accounting of the head turn; regeneration); concurrency (409 while generating, one draft per user, 429 on overload); usage parsing from the stream (include_usage, chunk with empty choices, absence tolerated, log line with numbers only); per-kind specifics (SQL/CoGIS plans, document reserve and summary/requisites); portal/dev stub behaviour; tests that must exist. Verify every fact against the code before writing it. Bump the document version and add a changelog entry in the document's style.` },
      { agent: 'ui-designer', prompt: `${COMMON}
Stage «contract». Update docs/portal-ui.md for BOTH releases in one revision, items marked 0.7 or 0.8. The memory panel is NOT a modal SidePage: it is a hideable right column (decisions 10–12 in portal-design §13) on all dialog screens (chat, SQL, CoGIS, document questions; not on knowledge base, users, profile) — 320 px, the feed narrows; open on first visit, state remembered in the browser like the theme; below 1440 px it overlays the feed; the source viewer (portal-ui §4.4) temporarily takes its place; toggle in the title bar; the compact indicator in the Composer is always visible and opens the panel. Update the frame and widths (§3.2, §3.3), keyboard/focus order (§9), own-components summary (§10). Cover: indicator levels, hint and states (loading, error, flag off); panel structure — level on top, pair list with the three-state choice, «не поместится» / «в пересказе» marks, empty states; feed changes (dimmed excluded pairs with label, summary boundary line, «Продолжить отсюда»); long-dialog notice from 80 % with «Сжать раннюю часть»; compression draft flow (streaming, edit, apply, cancel, errors 409/429/422); transfer modal and its entry points (dialog Kebab, history list, panel); «Сведения из «…»» notice; both themes; dictionary (keep «контекст» banned, add «память диалога», «пересказ» etc.); final Russian texts. Read the actual frontend (portal/frontend/src: Composer, MessageList, DialogScreen, DocumentsScreen, HistoryPanel, sql/SchemasPanel) so the concept fits existing components. Bump the document version and add a changelog entry in the document's style.` },
    ],
    checkers: [
      { agent: 'reviewer', recheck: 'failed', prompt: `${COMMON}
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
      { agent: 'reviewer', recheck: 'failed', recheckExtra: 'Run `make check` first — red is blocking.', prompt: `${COMMON}
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
      { agent: 'qa-engineer', recheck: 'always', recheckExtra: 'Re-run the e2e suite of this feature; write new tests only where a fix needs one.', prompt: `${COMMON}
Stage «accept-${rel}». Write and run Playwright end-to-end tests in portal/frontend/e2e on the stand portal/dev for portal-design §10 criteria ${s.criteria} (parts without [ВМ]); include isolation checks through the API for every new route (other user's dialog → not found) and the panel behaviour (hide/show survives reload, works on chat, SQL, CoGIS, documents). For [ВМ] parts write a step-by-step manual checklist. Each defect → blocking finding with the zone owner (backend-engineer / frontend-engineer; your own test bugs → qa-engineer).` },
      { agent: 'security-reviewer', recheck: 'always', recheckExtra: 'Review the fix diffs for new security issues.', prompt: `${COMMON}
Stage «accept-${rel}». Security review of the feature changes since main (\`git diff main -- portal/\`): isolation of every new route and query, prompt injection through summary / carried text / pair content (user role, data_block, closing-tag neutralization, never system), limits and overload (draft concurrency, summary length, 429), logs without message text, behaviour with the flag off. Blocking findings name the zone owner.` },
      { agent: 'ui-designer', model: LIGHT, recheck: 'failed', prompt: `${COMMON}
Stage «accept-${rel}», design review. Compare the implemented UI in portal/frontend/src with docs/portal-ui.md (${rel} items): the right memory column (width, hide/show, overlay below 1440 px, yielding to the source viewer), indicator, pair choice, feed marks, notices, dialogs, texts, both themes, keyboard. Blocking = wrong behaviour, wrong or missing texts, missing states; cosmetics → minor. Name the zone owner (frontend-engineer).` },
    ],
    fixers: ['backend-engineer', 'frontend-engineer', 'qa-engineer'],
    stop: `Коммит «test(portal): приёмка памяти диалога ${rel}», затем этап docs-${rel}.`,
  }
}

// Docs are low-risk: no separate critic; the writer proves every command against the code.
function docsStage(rel) {
  return {
    authors: [
      { agent: 'docs-writer', model: LIGHT, schema: DOCS_REPORT, prompt: `${COMMON}
Stage «docs-${rel}». Write docs/release-${rel}.md — a short, clear instruction for rolling release ${rel} onto the running service, in the format of the latest docs/release-*.md: variables, «Перед началом», numbered steps each with a check, rollback. It must cover: backup before the migration (\`make release-backup\`), rehearsal on a copy (\`make release-rehearse\`), the migration only adds fields, the first rollback line is the feature flag, code rollback after the migration only by restoring the backup (portal-design §8.4), new config keys and .env variables (or explicitly «нет»).${rel === '0.7' ? ' After the rollout: how to collect calibration pairs «оценка — факт» from the log and where to put the refined coefficients (config override).' : ' Precondition: calibration from 0.7 applied (§8.4); after the rollout — measuring summary latency on the GPU [ВМ].'} Update docs/user-guide.md (a short «Память диалога» section for employees, button names exactly as in docs/portal-ui.md, no words «контекст», «токен») and docs/runbook.md (flag, config keys, log lines).
Self-check instead of a separate review: every command, make target, variable, config key and log line you mention — find its definition (Makefile, scripts, portal/backend config, deploy/.env.example, the logging call) and list it in commands_checked with file:line; anything you cannot confirm — remove it or put it in unverified. Steps must be in a safe order: backup before migration, a check after each step, a rollback path.` },
    ],
    checkers: [],
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

const opts = (base, model) => (model ? { ...base, model } : base)
const uniq = xs => [...new Set(xs.filter(Boolean))]
const short = (x, n) => (x.length > n ? `${x.slice(0, n)}…` : x)

phase('Work')
const reports = {}
const work = await parallel(stage.authors.map(a => () =>
  agent(a.prompt, opts({ label: a.agent, phase: 'Work', agentType: a.agent, schema: a.schema || REPORT }, a.model))))
stage.authors.forEach((a, i) => { reports[a.agent] = work[i] })

// What checkers see of the authors' work: the short report, not the full transcript.
const brief = Object.entries(reports).map(([k, r]) => r
  ? `### ${k}\n${JSON.stringify({ summary: r.summary, changed_files: r.changed_files, deviations: r.deviations, notes_for_other_zones: r.notes_for_other_zones })}`
  : `### ${k}\n(no report: the agent failed)`).join('\n')

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
    const fixer = stage.authors.find(a => a.agent === owner)
    return agent(`${COMMON}
Stage «${STAGE}», fix round ${round}. Fix the blocking findings addressed to you, in your zone only:
${JSON.stringify(blocking.filter(b => b.agent === owner), null, 1)}
${others.length ? `Fixed in parallel by other zones (keep consistent, do not touch):\n${others.join('\n')}\n` : ''}Re-run your zone checks. Report per finding: how fixed, or why not.`,
      opts({ label: `fix:${owner}:r${round}`, phase: 'Fix', agentType: owner, schema: FIX_REPORT }, fixer && fixer.model))
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
const docs = stage.checkers.length ? null : Object.values(reports)[0]
if (docs && docs.unverified && docs.unverified.length) {
  blocking.push(...docs.unverified.map(u => ({ agent: 'docs-writer', where: 'docs', issue: `unverified: ${u}`, fix: 'confirm in code or remove' })))
}

return {
  stage: STAGE,
  verdict: blocking.length ? 'нужны исправления — показать владельцу' : 'можно коммитить',
  blocking,
  minor: uniq(minor),
  owner_questions: uniq(questions),
  check_on_vm: uniq(Object.values(reports).filter(Boolean).flatMap(r => r.check_on_vm)),
  authors: Object.fromEntries(Object.entries(reports).map(([k, r]) => [k, r ? { summary: r.summary, checked_locally: r.checked_locally, deviations: r.deviations } : null])),
  checkers: Object.fromEntries(Object.entries(latest).map(([k, r]) => [k, r.summary])),
  commands_checked: docs ? docs.commands_checked.length : undefined,
  rounds,
  next: stage.stop,
}
