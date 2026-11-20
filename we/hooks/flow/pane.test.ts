import { expect, mock, test } from 'claude-code/testing'

const STORY = 'docs/plans/PROJ-12-story.md'
const answers: [RegExp, string][] = [
  [/^git rev-parse --show-toplevel/, '/repo\n'],
  [/^git grep -l -E \^ticket: PROJ-10\$/, 'origin/main:docs/plans/channels-epic.md\n'],
  [/^git grep -h -m1/, '# Epic: Channels\n'],
  [/^git grep -l -E \^epic: \(PROJ-10\|channels\)\$/, `origin/main:${STORY}\norigin/main:docs/plans/PROJ-14-story.md\n`],
  [/^git grep -E -e/, [
    `origin/main:${STORY}:story: PROJ-12`, `origin/main:${STORY}:epic: PROJ-10`, `origin/main:${STORY}:status: approved`,
    `origin/main:${STORY}:# Plan: Discord groups`,
    'origin/main:docs/plans/PROJ-14-story.md:story: PROJ-14', 'origin/main:docs/plans/PROJ-14-story.md:epic: PROJ-10',
    'origin/main:docs/plans/PROJ-14-story.md:status: approved', 'origin/main:docs/plans/PROJ-14-story.md:# Plan: Guild identity',
  ].join('\n')],
  [/^git for-each-ref/, 'main\norigin/feat/PROJ-14-guild\n'],
  [/^git worktree list/, 'worktree /repo\nbranch refs/heads/main\n'],
  [/^git rev-list --count/, '3\n'],
  [/^gh pr list/, JSON.stringify([
    { number: 5072, state: 'MERGED', headRefName: 'feat/PROJ-12-discord', title: 'x', autoMergeRequest: null, statusCheckRollup: [] },
    { number: 5090, state: 'OPEN', headRefName: 'feat/PROJ-14-guild', title: 'y', autoMergeRequest: null,
      statusCheckRollup: [{ name: 'CI Summary', status: 'COMPLETED', conclusion: 'FAILURE' }, { name: 'Claude Review', status: 'IN_PROGRESS', conclusion: null }] },
  ])],
]

test('/flow PROJ-10 fills the pane after the command returned', async ($, on) => {
  const clock = mock.clock(on)
  mock.store(on)
  on('process.run', (_$, e) => {
    const line = e.argv.join(' ')
    const hit = answers.find(([pattern]) => pattern.test(line))
    return { value: { exitCode: 0, stdout: hit?.[1] ?? '', stderr: '', isStdoutTruncated: false, isStderrTruncated: false } }
  })
  // The Board button opens the board pane, which looks for its file.
  on('env.get', () => ({ value: '/home/me' }))
  on('fs.exists', () => ({ value: false }))
  const toasts: string[] = []
  on('ui.toast', (_$, e) => {
    toasts.push(String((e as { text?: unknown }).text))
    return { value: undefined }
  })
  const opened: string[] = []
  on('ui.open', (_$, e) => {
    opened.push(String((e as { id?: unknown }).id))
    return { value: { isPlaced: true } }
  })
  on('ui.status', () => ({ value: undefined }))
  const logged: string[] = []
  on('ui.log', (_$, e) => {
    logged.push(String((e as { text?: unknown }).text))
    return { value: undefined }
  })
  const ran: string[] = []
  on('command.run', { command: 'we:ci-review' }, (_$, e) => {
    ran.push(`${e.command} ${e.args}`)
    return { text: 'ok' }
  })
  on('command.run', { command: 'we:standup' }, (_$, e) => {
    ran.push(`${e.command} ${e.args}`)
    return { text: 'ok' }
  })

  const answer = await $.command.run({ command: 'flow', args: 'PROJ-10', origin: { kind: 'composer' } } as never)
  expect(answer.text).toContain('PROJ-10')
  await clock.settle()
  expect(logged).toEqual([])

  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'we', surface, component: 'Pane', requestId: 'we-flow', props: { bodyColumns: 70 } as never })
    expect(await ui.find({ type: 'Text', text: /CI red/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /PROJ-12/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /1\/2 merged/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /✗ CI Summary/ })).toBeDefined()
    expect(await ui.find({ type: 'Text', text: /Claude running/ })).toBeDefined()
    await ui.press({ key: 'PROJ-14-ci' })
    await ui.press({ key: 'PROJ-14-standup' })
    await ui.press({ key: 'board' })
    await ui.unmount()
  }
  expect(ran).toEqual(['we:ci-review 5090', 'we:standup PROJ-14', 'we:ci-review 5090', 'we:standup PROJ-14'])
  await clock.settle()
  expect(toasts).toEqual([])
  expect(opened).toEqual(['we-flow', 'we-board', 'we-board'])
})

// flow_intervals.py --snapshot's document, as far as Crew flow reads it.
const SNAP = (generatedAt: string) =>
  JSON.stringify({
    schema_version: 1,
    generated_at: generatedAt,
    project: 'acme/app',
    header: ['one probe every 300 s', 'Not counted: …', 'CI wait: gh check runs, fetched_at 2026-11-20T21:58:00Z'],
    now: [
      { who: 'ABC-1-build', group: 'workers', starter: 'lead', kind: 'waiting:ci', since: '2026-11-20T21:40:00Z', waiting_on: 'ci', key: 'ABC-1', label: null },
      { who: 'lead', group: 'masters', starter: null, kind: 'working', since: '2026-11-20T21:50:00Z', waiting_on: null, key: 'ABC-1', label: null },
      { who: 'ABC-9', group: 'stories', starter: null, kind: 'stage:in_review', since: '2026-11-20T18:00:00Z', waiting_on: null, key: 'ABC-9', label: 'waits on acceptance' },
    ],
    ci: { prs: [{ number: 7, title: 'ABC-1 export', heads: [{ pr: 7, start: '2026-11-20T21:00:00Z', end: '2026-11-20T22:00:00Z', queue_s: 2400, run_s: 900, open: true }] }] },
    gaps: [{ reason: 'no data: sampler 2026-11-19' }],
    page: '/home/me/.claude/we-state/flow/flow-acme-app.html',
  })

test('Flow from the board: Crew flow from the last snapshot, a stale one refreshed once in the background', async ($, on) => {
  const clock = mock.clock(on, { now: Date.parse('2026-11-20T22:00:00Z') })
  mock.store(on)
  const path = '/home/me/.claude/we-state/flow/flow-acme-app.json'
  let snapshot = SNAP('2026-11-20T21:57:00Z') // 3 min old: fresh
  const ran: string[] = []
  on('process.run', (_$, e) => {
    const line = e.argv.join(' ')
    ran.push(line)
    if (line.endsWith('--snapshot-path')) return RUN_OK(`${path}\n`)
    if (line.includes('--snapshot')) {
      snapshot = SNAP('2026-11-20T22:20:00Z')
      return RUN_OK(path)
    }
    return RUN_OK('')
  })
  on('fs.exists', () => ({ value: true }))
  on('fs.read', () => ({ value: snapshot }))
  on('env.get', () => ({ value: '/home/me' }))
  on('session.id', () => ({ value: 's1' }))
  on('session.messages', () => ({ value: [] }))
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('ui.log', () => ({ value: undefined }))

  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: { command: 'flow' } }))
  on('ui.status', () => ({ value: undefined }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  // No key in this session: the pane still opens, Crew flow on top.
  const answer = await $.command.run({ command: 'flow', args: '', origin: { kind: 'composer' } } as never)
  expect(answer.text).toContain('Crew flow')
  await clock.settle()
  const draw = async () => {
    const ui = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-flow', props: { bodyColumns: 100 } as never })
    const texts: string[] = []
    for (const re of [/Crew flow/, /ABC-1-build/, /lead  works/, /ABC-9  waits on acceptance/, /#7  queue 40 min · run 15 min/, /gap: no data/, /Swimlanes: \/home\/me/]) {
      const hit = await ui.find({ type: 'Text', text: re })
      texts.push(hit ? String(re) : `missing ${re}`)
    }
    const head = await ui.find({ type: 'Text', text: /Crew flow · acme\/app · snapshot \d+ min old/ })
    await ui.unmount()
    return { texts, head }
  }
  const fresh = await draw()
  expect(fresh.texts.filter(t => t.startsWith('missing'))).toEqual([])
  expect(fresh.head).toBeDefined()
  expect(ran.filter(l => l.includes('--snapshot ') || l.endsWith('--snapshot'))).toEqual([])

  // 20 min later the snapshot is stale: the pane's 2-minute timer starts one background run (no
  // second while the first counts), then the section redraws from it.
  await clock.advance(20 * 60_000)
  await clock.settle()
  const runs = ran.filter(l => / --since 1d --snapshot$/.test(l))
  expect(runs.length).toBe(1)
  const ui = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-flow', props: { bodyColumns: 100 } as never })
  expect(await ui.find({ type: 'Text', text: /snapshot 0 min old/ })).toBeDefined()
  await ui.unmount()
})

const RUN_OK = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })
