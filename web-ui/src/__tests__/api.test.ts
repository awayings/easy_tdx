/**
 * api.ts 行为自检（Node 内置 test runner，fetch 打桩，无 DOM 依赖）。
 *
 * 运行：node --test src/__tests__/api.test.ts
 *
 * 覆盖：
 * 1. fetchRankList 的 market 归一化——北交所(market=2)必须映射为 'BJ'
 *    （与 fetchBoardMembers 同口径；错标 'SZ' 会导致榜单点开个股用错市场拉行情）。
 * 2. runLlmChatWithPolling 的可选 AbortSignal——signal 中止后循环立即以
 *    AbortError 退出，不再继续轮询（弹窗卸载清理依赖此行为）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { fetchRankList, runLlmChatWithPolling } from '../api.ts'
import type { TaskState } from '../types.ts'

/** 临时替换 globalThis.fetch，返回可编程响应序列。 */
function stubFetch(handler: (url: string, init?: RequestInit) => unknown): {
  calls: string[]
  restore: () => void
} {
  const calls: string[] = []
  const original = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input instanceof URL ? input : input)
    calls.push(url)
    const body = handler(url, init)
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }) as typeof fetch
  return {
    calls,
    restore: () => {
      globalThis.fetch = original
    },
  }
}

// ── 1. fetchRankList 北交所映射 ───────────────────────────────────────────────

test('fetchRankList: market=2（北交所）归一化为 BJ', async () => {
  const stub = stubFetch(() => ({
    data: [
      { close: 10.5, pre_close: 9.55, market: 2, code: '920002', name: '测试北交所' },
      { close: 8.0, pre_close: 8.8, market: 0, code: '000001', name: '平安银行' },
      { close: 20.0, pre_close: 19.0, market: 1, code: '600519', name: '贵州茅台' },
    ],
  }))
  try {
    const rows = await fetchRankList('DESC', 3)
    assert.equal(rows[0].market, 'BJ', 'market=2 应映射为 BJ（旧码错标 SZ）')
    assert.equal(rows[1].market, 'SZ', 'market=0 应映射为 SZ')
    assert.equal(rows[2].market, 'SH', 'market=1 应映射为 SH')
    // 顺带核对涨跌幅口径：close/pre-1
    assert.ok(Math.abs((rows[0].change_pct ?? 0) - ((10.5 / 9.55 - 1) * 100)) < 1e-6)
  } finally {
    stub.restore()
  }
})

// ── 2. runLlmChatWithPolling 的 AbortSignal ──────────────────────────────────

const runningState: TaskState = { status: 'running' } as unknown as TaskState

test('runLlmChatWithPolling: signal 中止后立即退出并抛 AbortError', async () => {
  const stub = stubFetch((url) => {
    if (url.includes('/llm/chat/async')) return { task_id: 't1' }
    return runningState
  })
  try {
    const ctrl = new AbortController()
    const promise = runLlmChatWithPolling(
      'ping',
      null,
      () => ctrl.abort(), // 第一次轮询即中止
      5, // intervalMs
      30_000, // timeoutMs（远大于中止所需时间，确保超时兜底不先触发）
      ctrl.signal,
    )
    await assert.rejects(promise, (e: unknown) => (e as Error).name === 'AbortError')
    // 中止后不应继续轮询：任务查询次数应极少（≤3）
    const polls = stub.calls.filter((u) => u.includes('/llm/chat/tasks/')).length
    assert.ok(polls <= 3, `中止后应停止轮询，实际轮询 ${polls} 次`)
  } finally {
    stub.restore()
  }
})

test('runLlmChatWithPolling: 未中止时正常返回 done', async () => {
  let polls = 0
  const stub = stubFetch((url) => {
    if (url.includes('/llm/chat/async')) return { task_id: 't2' }
    polls += 1
    if (polls < 3) return runningState
    return { status: 'done', result: { reply: 'ok', model: 'm', provider: 'p' } }
  })
  try {
    const state = await runLlmChatWithPolling('ping', null, undefined, 1, 30_000)
    assert.equal(state.status, 'done')
    assert.equal((state.result as { reply?: string } | null)?.reply, 'ok')
  } finally {
    stub.restore()
  }
})
