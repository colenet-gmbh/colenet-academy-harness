// The crew pane, opened by the /board pane's Crew button: one line per live session of the crew, who
// waits on whom first. It renders what `scripts/crew_view.py --json` prints (that script owns which
// sessions count and their order) and reads nothing else.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CrewEntry, CrewView } from '../../types'
import { clip } from './board'

type On = Parameters<Register>[0]
type $ = EngineInterface

const PANE = 'we-crew'
const REFRESH_MS = 30_000
// A document older than two refreshes shows its age in warning colour: the pane does not pass off old data as now.
const STALE_MS = 2 * REFRESH_MS

const view = atom({ plugin: 'we', key: 'crewView' } as const, null, { shape: 'crew-view-1' })
const isOpen = atom({ plugin: 'we', key: 'crewOpen' } as const, false)

// `7800` → `2h10`, as crew_view.py age_text.
export const ageText = (seconds: number | null) => {
  if (seconds === null) return '?'
  const minutes = Math.floor(Math.max(0, seconds) / 60)
  return `${Math.floor(minutes / 60)}h${String(minutes % 60).padStart(2, '0')}`
}

// `samu · coordinator · Q-Triage WA-3104 · since 0h12 · waits: human · 9.14.0`
export const crewLine = (e: CrewEntry) =>
  [
    e.name,
    e.role,
    e.doing ?? '-',
    `since ${ageText(e.age_s)}`,
    ...(e.waits ? [`waits: ${e.waiting_on ?? '?'}${e.blocked_kind ? ` (${e.blocked_kind})` : ''}`] : []),
    e.plugin,
  ].join(' · ')

// A worker listed outside its starter's group has no live starter.
const isOrphan = (e: CrewEntry) => e.role === 'worker' && e.group !== e.starter

async function loadCrew($: $, board?: string) {
  const at = await $.clock.now()
  try {
    const argv = ['python3', '-I', `${$.plugin.root}/scripts/crew_view.py`, '--json', ...(board ? ['--board', board] : [])]
    const run = await $.process.run(argv, { timeoutMs: 40_000 })
    const doc = JSON.parse(run.stdout || '{}') as { board?: string; generated_at?: string; sessions?: CrewEntry[]; error?: string }
    const next: CrewView = {
      ...(doc.board ? { board: doc.board } : {}),
      ...(doc.generated_at ? { generatedAt: doc.generated_at } : {}),
      sessions: doc.sessions ?? [],
      at,
      ...(run.exitCode !== 0 ? { error: `crew_view.py exit ${run.exitCode}` } : doc.error ? { error: doc.error } : {}),
    }
    await update($, view, () => next)
  } catch (err) {
    const error = err instanceof Error ? err.message : String(err)
    await update($, view, v => ({ ...(v ?? { sessions: [] }), at: v?.at ?? 0, error }))
  }
}

// The Crew button's work (a plugin's own $.command.run does not reach its own hook); `board` is the
// board file the /board pane shows.
export async function openCrew($: $, board?: string): Promise<string> {
  await update($, isOpen, () => true)
  const opened = await $.ui.open({ id: PANE, title: 'Crew' })
  await loadCrew($, board)
  return opened.isPlaced ? 'Crew pane opened.' : `Crew pane waits undrawn: ${opened.reason}`
}

// From the module's one session.start hook: the refresh while the pane is open.
export async function startCrew($: $) {
  $.clock.every(REFRESH_MS, () => {
    void read($, isOpen).then(open => {
      if (open) void read($, view).then(v => loadCrew($, v?.board))
    })
  })
}

export const registerCrew = (on: On) => {
  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    const v = await read($, view)
    const width = Math.max(30, e.props.bodyColumns ?? e.viewport?.columns ?? 80)
    if (!v) return <Text dimColor>Reading the crew…</Text>
    const now = await $.clock.now()
    const stale = now - v.at >= STALE_MS
    const stamp = v.generatedAt ? `as of ${v.generatedAt.slice(11, 16)} UTC` : 'not read yet'
    const waiting = v.sessions.filter(s => s.waits).length
    let orphansShown = false
    const rows = v.sessions.flatMap(s => {
      const head = isOrphan(s) && !orphansShown
      if (head) orphansShown = true
      const indent = s.role === 'worker' ? '└ ' : ''
      return [
        ...(head ? [<Text key="no-starter" dimColor>{'Workers without a live starter'}</Text>] : []),
        <Text key={`s-${s.session_id || s.name}`} color={s.waits ? 'warning' : undefined} bold={s.role !== 'worker'}>
          {clip(`${indent}${crewLine(s)}`, width)}
        </Text>,
      ]
    })
    return (
      <Box flexDirection="column">
        <Text bold>{clip(`Crew: ${v.sessions.length} live, ${waiting} waiting`, width)}</Text>
        {v.error && <Text color="error">{clip(`⚠ ${v.error}`, width)}</Text>}
        <Box flexDirection="column" marginTop={1}>{rows}</Box>
        <Box flexDirection="row" gap={2} marginTop={1}>
          <Button key="refresh" hotkey="r" label="Refresh" onPress={() => void loadCrew($, v.board)} />
          <Text color={stale ? 'warning' : undefined} dimColor={!stale}>{stale ? `${stamp} (stale)` : stamp}</Text>
        </Box>
      </Box>
    )
  })
}
