import { expect, mock, test } from 'claude-code/testing'

import { ageText, crewLine } from './crew'
import { paint } from './paint'

const BOARD = JSON.stringify({ updated: '2026-11-20T21:00:00Z', updated_by: 'samu', sessions: [{ name: 'samu', role: 'master' }] })

const entry = (name: string, extra: Record<string, unknown>) => ({
  name, session_id: `s-${name}`, kind: 'interactive', role: 'master', state: 'idle', doing: null, keys: [], waiting_on: null,
  blocked_kind: null, since: null, age_s: 600, plugin: '9.14.0', starter: null, group: name, waits: false, ...extra,
})

// crew_view.py --json, already ordered: waiting first, workers under their starter, the orphan last.
const DOC = JSON.stringify({
  schema_version: 1,
  generated_at: '2026-11-20T21:10:00Z',
  board: '/home/me/.claude/we-state/board.json',
  sessions: [
    entry('samu', { role: 'coordinator', doing: 'Q-Triage WA-3104', waiting_on: 'human', waits: true, age_s: 720 }),
    entry('lead', { doing: 'epic WA-1', age_s: 7800 }),
    entry('w-old', { role: 'worker', kind: 'background', doing: 'WA-2', waiting_on: 'master', blocked_kind: 'chat', waits: true, starter: 'lead', group: 'lead' }),
    entry('w-lost', { role: 'worker', kind: 'background', starter: null, group: '(no starter)' }),
  ],
})

const RUN = (stdout: string) => ({ value: { exitCode: 0, stdout, stderr: '', isStdoutTruncated: false, isStderrTruncated: false } })

test('crewLine reads like the brief: name, role, work, age, wait, version', () => {
  expect(ageText(7800)).toBe('2h10')
  expect(ageText(null)).toBe('?')
  expect(crewLine(entry('samu', { role: 'coordinator', doing: 'Q-Triage WA-3104', waiting_on: 'human', waits: true, age_s: 720 }) as never)).toBe(
    'samu · coordinator · Q-Triage WA-3104 · since 0h12 · waits: human · 9.14.0',
  )
})

test('/board: Crew opens a pane with one line per live session, waiting first, workers under their starter', async ($, on) => {
  const clock = mock.clock(on, { now: Date.parse('2026-11-20T21:10:30Z') })
  mock.store(on)
  on('env.get', () => ({ value: '/home/me' }))
  on('fs.exists', (_$, e) => ({ value: !String((e as { path?: unknown }).path).endsWith('.jsonl') }))
  on('fs.stat', () => ({ value: { kind: 'file', size: 1, mtimeMs: 1, isLink: false } }))
  on('fs.read', () => ({ value: BOARD }))
  const opened: string[] = []
  on('ui.open', (_$, e) => {
    opened.push(String((e as { id?: unknown }).id))
    return { value: { isPlaced: true } }
  })
  on('ui.log', () => ({ value: undefined }))
  on('ui.toast', () => ({ value: undefined }))
  const ran: string[][] = []
  let hangs = false
  on('process.run', (_$, e) => {
    ran.push(e.argv)
    if (hangs) throw new Error('claude agents hangs')
    if (String(e.argv[2] ?? '').endsWith('/crew_view.py')) return RUN(DOC)
    if (e.argv[0] === 'python3') return RUN('{"sessions":[]}')
    return RUN('')
  })
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('command.register', () => ({ value: { command: 'board' } }))

  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
  await $.command.run({ command: 'board', args: '', origin: { kind: 'composer' } } as never)
  await clock.settle()
  const board = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-board', props: { bodyColumns: 100 } as never })
  await board.press({ key: 'crew' })
  await board.unmount()
  await clock.settle()

  expect(opened).toContain('we-crew')
  const call = ran.find(a => String(a[2] ?? '').endsWith('/crew_view.py'))!
  expect(call.slice(3)).toEqual(['--json', '--board', '/home/me/.claude/we-state/board.json'])

  const ui = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-crew', props: { bodyColumns: 100 } as never })
  const lines = paint((await ui.drawn()) as never, 100).map(l => l.trimEnd())
  await ui.unmount()
  expect(lines.filter(Boolean)).toEqual([
    'Crew: 4 live, 2 waiting',
    'samu · coordinator · Q-Triage WA-3104 · since 0h12 · waits: human · 9.14.0',
    'lead · master · epic WA-1 · since 2h10 · 9.14.0',
    '└ w-old · worker · WA-2 · since 0h10 · waits: master (chat) · 9.14.0',
    'Workers without a live starter',
    '└ w-lost · worker · - · since 0h10 · 9.14.0',
    '[ Refresh ]  as of 21:10 UTC',
  ])

  // Two refreshes later without a fresh read, the stamp says so.
  hangs = true
  await clock.advance(61_000)
  const late = await $.ui.mount({ plugin: 'we', surface: 'terminal', component: 'Pane', requestId: 'we-crew', props: { bodyColumns: 100 } as never })
  const after = paint((await late.drawn()) as never, 100).map(l => l.trimEnd())
  await late.unmount()
  expect(after).toContain('[ Refresh ]  as of 21:10 UTC (stale)')
})
