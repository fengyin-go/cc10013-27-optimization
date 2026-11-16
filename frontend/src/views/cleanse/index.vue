<template>
  <section class="page" data-module="cleanse">
    <header class="page-head">
      <div>
        <h2>数据清洗管理</h2>
        <p class="page-desc">
          源记录冻结清洗版本，按去重窗口重算出唯一结果；重复剔除记录进入清洗待处理队列，
          数据导出只读取同一规则版本的一份完整口径。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="recompute()">重算当前版本</button>
        <button class="btn" type="button" @click="exportDataset">导出清洗数据集</button>
        <button class="btn" type="button" @click="exportQueue">导出待处理队列</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in cards" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <section class="panel">
      <div class="panel-head">
        <h3>规则版本与处理量</h3>
        <span class="hint">差异列对比上一版本；处理量按去重窗口汇总</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>规则编号</th>
            <th>目标字段</th>
            <th>规则版本</th>
            <th>去重键</th>
            <th>状态</th>
            <th>已完成窗口</th>
            <th>输入</th>
            <th>重复剔除</th>
            <th>输出</th>
            <th>与上一版差异（剔除/输出）</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="version in versionRows"
            :key="`${version.rule_code}-${version.version}`"
            :class="{ selected: selected.ruleCode === version.rule_code && selected.version === version.version }"
          >
            <td>{{ version.rule_code }}</td>
            <td>{{ version.target_field }}</td>
            <td>{{ version.version }}</td>
            <td>{{ version.dedup_key }}</td>
            <td>
              <span class="tag" :class="version.window_done === version.window_total ? 'ok' : 'warn'">
                {{ version.window_done === version.window_total ? '已完成' : '处理中' }}
              </span>
              <span v-if="version.failed_windows.length" class="tag bad">中断</span>
            </td>
            <td>{{ version.window_done }} / {{ version.window_total }}</td>
            <td>{{ version.input_rows }}</td>
            <td>{{ version.removed_rows }}</td>
            <td>{{ version.output_rows }}</td>
            <td>
              <template v-if="version.delta_vs_previous">
                {{ signed(version.delta_vs_previous.removed_rows) }} /
                {{ signed(version.delta_vs_previous.output_rows) }}
              </template>
              <span v-else class="muted">基线</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click="selectVersion(version)">查看窗口</button>
              <button class="link" type="button" @click="recompute(version)">重算</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>去重窗口：{{ selected.ruleCode }} {{ selected.version }}</h3>
        <div class="panel-actions">
          <label>
            批次（窗口数）
            <input v-model="batchSize" type="number" min="1" max="8" placeholder="全部" />
          </label>
          <label>
            中断窗口
            <select v-model="failWindow">
              <option value="">不模拟中断</option>
              <option v-for="window in windowRows" :key="window.window" :value="window.window">
                {{ window.window }}
              </option>
            </select>
          </label>
          <button class="btn" type="button" @click="recompute(selectedRow)">按设置重算</button>
        </div>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>去重窗口</th><th>状态</th><th>输入</th><th>重复剔除</th><th>输出</th><th>批次</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="window in windowRows" :key="window.window">
            <td class="mono">{{ window.window }}</td>
            <td>
              <span class="tag" :class="window.status === '已完成' ? 'ok' : window.status === '失败' ? 'bad' : 'warn'">
                {{ window.status }}
              </span>
            </td>
            <td>{{ window.input_rows }}</td>
            <td>{{ window.removed_rows }}</td>
            <td>{{ window.output_rows }}</td>
            <td class="mono">{{ window.run_id ?? '—' }}</td>
            <td>{{ window.error ?? '—' }}</td>
          </tr>
          <tr v-if="!windowRows.length">
            <td colspan="7" class="empty-state">暂无窗口数据</td>
          </tr>
        </tbody>
      </table>
      <div v-if="selectedRow" class="detail inline-actions">
        <button class="btn" type="button" @click="loadDiff">
          对比 {{ previousLabel }} → {{ selected.version }}
        </button>
        <span class="muted">字段差异只保留规则版本与去重数量，未命中字段不计入重复剔除。</span>
      </div>
      <div v-if="diff" class="detail">
        <h4>版本差异 {{ diff.from_version }} → {{ diff.to_version }}（{{ diff.target_field }}）</h4>
        <div class="detail-grid">
          <div v-for="field in diff.changed_fields" :key="field.field">
            <span>{{ field.field }}</span>
            {{ field.before }} → {{ field.after }}
          </div>
          <div>
            <span>输入 / 剔除 / 输出</span>
            {{ diff.counts.before.input_rows }} / {{ diff.counts.before.removed_rows }} /
            {{ diff.counts.before.output_rows }} → {{ diff.counts.after.input_rows }} /
            {{ diff.counts.after.removed_rows }} / {{ diff.counts.after.output_rows }}
          </div>
          <div>
            <span>新增去重剔除</span>
            {{ diff.new_removal_count }} 条
          </div>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th>源记录</th><th>窗口</th><th>目标字段原值</th><th>变更前</th><th>变更后</th><th>去重键（前 → 后）</th><th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in diff.items.slice(0, 12)" :key="item.source_id">
              <td class="mono">{{ item.source_id }}</td>
              <td class="mono">{{ item.window }}</td>
              <td class="mono">{{ item.original_value || '（空值）' }}</td>
              <td class="mono">{{ item.before_value }}</td>
              <td class="mono">{{ item.after_value }}</td>
              <td class="mono">{{ item.before_dedup_key }} → {{ item.after_dedup_key }}</td>
              <td>{{ item.change }}</td>
            </tr>
          </tbody>
        </table>
        <p class="muted" v-if="diff.items.length > 12">仅展示前 12 条，完整差异可导出待处理队列查看。</p>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>清洗待处理队列</h3>
        <span class="hint">按重算结果筛出重复剔除记录，详情跟随版本差异</span>
      </div>
      <form class="filter-bar" @submit.prevent="loadQueue(1)">
        <label v-for="field in queueFilters" :key="field.key" class="filter-item">
          <span>{{ field.label }}</span>
          <select v-model="queueQuery[field.key]">
            <option value="">全部</option>
            <option v-for="option in field.options" :key="option" :value="option">{{ option }}</option>
          </select>
        </label>
        <button class="btn" type="submit">筛选</button>
        <button class="btn ghost" type="button" @click="resetQueue">重置</button>
      </form>
      <table class="data-table">
        <thead>
          <tr>
            <th>队列编号</th><th>规则</th><th>版本</th><th>窗口</th><th>源记录</th>
            <th>目标字段原值</th><th>去重键</th><th>保留记录</th><th>状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in queueRows"
            :key="item.queue_id"
            :class="{ selected: selectedQueue === item.queue_id }"
            @click="selectQueueItem(item)"
          >
            <td class="mono">{{ item.queue_id }}</td>
            <td>{{ item.rule_code }}</td>
            <td>{{ item.version }}</td>
            <td class="mono">{{ item.window }}</td>
            <td class="mono">{{ item.source_id }}</td>
            <td class="mono">{{ item.original_value || '（空值）' }}</td>
            <td class="mono">{{ item.dedup_key }}</td>
            <td class="mono">{{ item.kept_source_id }}</td>
            <td>
              <span class="tag" :class="item.status === '已处理' ? 'ok' : 'warn'">{{ item.status }}</span>
            </td>
            <td class="row-actions">
              <button class="link" type="button" @click.stop="toggleQueueItem(item)">
                {{ item.status === '已处理' ? '恢复待处理' : '标记已处理' }}
              </button>
            </td>
          </tr>
          <tr v-if="!queueRows.length">
            <td colspan="10" class="empty-state">当前筛选条件下没有重复剔除记录</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ queueTotal }} 条待处理队列记录</span>
        <span class="inline-actions">
          <button class="link" type="button" :disabled="queuePage <= 1" @click="loadQueue(queuePage - 1)">
            上一页
          </button>
          <span>第 {{ queuePage }} 页</span>
          <button class="link" type="button" :disabled="queuePage * queueSize >= queueTotal" @click="loadQueue(queuePage + 1)">
            下一页
          </button>
        </span>
      </footer>
      <div v-if="queueDetail" class="detail">
        <h4>剔除详情 {{ queueDetail.queue_id }}</h4>
        <div class="detail-grid">
          <div><span>目标字段</span>{{ queueDetail.target_field }}</div>
          <div><span>目标字段原值</span><span class="mono">{{ queueDetail.original_value || '（空值）' }}</span></div>
          <div><span>输入值</span><span class="mono">{{ queueDetail.input_value }}</span></div>
          <div><span>清洗后</span><span class="mono">{{ queueDetail.cleaned_value }}</span></div>
          <div><span>剔除原因</span>{{ queueDetail.removal_reason }}</div>
          <div><span>冻结版本</span>{{ queueDetail.frozen_version }}</div>
          <div>
            <span>版本差异</span>
            {{ queueDetail.version_diff.from_version }} → {{ queueDetail.version_diff.to_version }}
          </div>
          <div v-for="field in queueDetail.version_diff.changed_fields" :key="field.field">
            <span>{{ field.field }}</span>{{ field.before }} → {{ field.after }}
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>已发布窗口的更晚更正</h3>
        <span class="hint">取舍会改变终态；默认保留原值，可切换采用更正</span>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>规则</th><th>版本</th><th>源记录</th><th>窗口</th><th>目标字段原值</th>
            <th>更正后</th><th>更正时间</th><th>窗口状态</th><th>当前取舍</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in conflicts" :key="`${item.source_id}-${item.version}`">
            <td>{{ item.rule_code }}</td>
            <td>{{ item.version }}</td>
            <td class="mono">{{ item.source_id }}</td>
            <td class="mono">{{ item.window }}</td>
            <td class="mono">{{ item.original_value || '（空值）' }}</td>
            <td class="mono">{{ item.corrected_value }}</td>
            <td class="mono">{{ item.corrected_at }} / 发布 {{ item.published_at }}</td>
            <td>{{ item.window_state }}</td>
            <td>{{ item.decision }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="resolveConflict(item, '保留原值')">保留原值</button>
              <button class="link" type="button" @click="resolveConflict(item, '采用更正')">采用更正</button>
            </td>
          </tr>
          <tr v-if="!conflicts.length">
            <td colspan="10" class="empty-state">没有更晚更正冲突</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>清洗规则清单</h3>
        <span class="hint">源记录冻结版本后，规则清单只读</span>
      </div>
      <table class="data-table">
        <thead>
          <tr><th v-for="column in columns" :key="column">{{ column }}</th><th>状态</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="String(row.id)">
            <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
            <td>{{ row.status }}</td>
          </tr>
          <tr v-if="!rows.length">
            <td :colspan="columns.length + 1" class="empty-state">暂无数据清洗规则</td>
          </tr>
        </tbody>
      </table>
      <footer class="page-foot">
        <span>共 {{ total }} 条数据清洗规则版本</span>
      </footer>
    </section>

    <footer class="page-foot">
      <span v-if="message" class="muted">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>
type Counts = { input_rows: number; removed_rows: number; output_rows: number }
type VersionRow = {
  rule_code: string
  target_field: string
  version: string
  dedup_key: string
  window_total: number
  window_done: number
  failed_windows: string[]
  input_rows: number
  removed_rows: number
  output_rows: number
  delta_vs_previous: Counts | null
}
type OverviewPayload = {
  cards: { label: string; value: number }[]
  rules: { rule_code: string; versions: VersionRow[] }[]
}
type WindowRow = {
  window: string
  status: string
  input_rows: number
  removed_rows: number
  output_rows: number
  run_id: string | null
  error: string | null
}
type QueueItem = {
  queue_id: string
  rule_code: string
  version: string
  window: string
  source_id: string
  target_field: string
  original_value: string | null
  input_value: string
  cleaned_value: string
  dedup_key: string
  kept_source_id: string | null
  removal_reason: string
  frozen_version: string
  status: string
  version_diff: {
    from_version: string | null
    to_version: string
    changed_fields: { field: string; before: string | number | null; after: string | number | null }[]
  }
}
type DiffPayload = {
  from_version: string
  to_version: string
  target_field: string
  counts: { before: Counts; after: Counts }
  changed_fields: { field: string; before: string | number; after: string | number }[]
  new_removal_count: number
  items: {
    source_id: string
    window: string
    original_value: string | null
    before_value: string
    after_value: string
    before_dedup_key: string
    after_dedup_key: string
    change: string
  }[]
}
type ConflictItem = {
  rule_code: string
  version: string
  source_id: string
  window: string
  original_value: string | null
  corrected_value: string
  corrected_at: string
  published_at: string
  window_state: string
  decision: string
}

const ENDPOINT = '/api/cleanse'
const columns = ['规则编号', '目标字段', '空值替换', '格式规范化', '异常值剔除', '去重键', '规则版本', '生效日期']
const queueFilters = [
  { key: 'rule_code', label: '规则编号', options: ['CLEANSE-401', 'CLEANSE-402'] },
  { key: 'version', label: '规则版本', options: ['R4', 'R5'] },
  { key: 'status', label: '处理状态', options: ['待处理', '已处理'] },
]

const cards = ref<{ label: string; value: number }[]>([])
const versionRows = ref<VersionRow[]>([])
const windowRows = ref<WindowRow[]>([])
const rows = ref<Row[]>([])
const total = ref(0)
const queueRows = ref<QueueItem[]>([])
const queueTotal = ref(0)
const queuePage = ref(1)
const queueSize = 10
const queueQuery = ref<Record<string, string>>({})
const queueDetail = ref<QueueItem | null>(null)
const selectedQueue = ref('')
const diff = ref<DiffPayload | null>(null)
const conflicts = ref<ConflictItem[]>([])
const selected = ref<{ ruleCode: string; version: string }>({ ruleCode: 'CLEANSE-401', version: 'R5' })
const batchSize = ref('')
const failWindow = ref('')
const message = ref('')
const errorMessage = ref('')

const selectedRow = computed(
  () =>
    versionRows.value.find(
      (row) => row.rule_code === selected.value.ruleCode && row.version === selected.value.version,
    ) ?? null,
)
const previousLabel = computed(() => (selected.value.version === 'R5' ? 'R4' : '—'))

function signed(value: number) {
  return value > 0 ? `+${value}` : `${value}`
}

function selectVersion(row: { rule_code: string; version: string }) {
  selected.value = { ruleCode: row.rule_code, version: row.version }
  diff.value = null
  void loadWindows()
}

async function loadOverview() {
  const payload = await fetchJson<OverviewPayload>(`${ENDPOINT}/versions`)
  cards.value = payload.cards
  versionRows.value = payload.rules.flatMap((rule) => rule.versions)
}

async function loadWindows() {
  const query = new URLSearchParams({
    rule_code: selected.value.ruleCode,
    version: selected.value.version,
  }).toString()
  const payload = await fetchJson<{ items: WindowRow[] }>(`${ENDPOINT}/windows?${query}`)
  windowRows.value = payload.items
}

async function reloadRules() {
  const payload = await fetchJson<{ items: Row[]; total: number }>(`${ENDPOINT}?size=200`)
  rows.value = payload.items ?? []
  total.value = payload.total ?? rows.value.length
}

async function loadQueue(page = 1) {
  queuePage.value = page
  const params: Record<string, string> = { page: String(page), size: String(queueSize) }
  for (const [key, value] of Object.entries(queueQuery.value)) {
    if (value) params[key] = value
  }
  const payload = await fetchJson<{ items: QueueItem[]; total: number }>(
    `${ENDPOINT}/queue?${new URLSearchParams(params).toString()}`,
  )
  queueRows.value = payload.items ?? []
  queueTotal.value = payload.total ?? queueRows.value.length
  selectedQueue.value = ''
  queueDetail.value = null
}

async function loadConflicts() {
  const payload = await fetchJson<{ items: ConflictItem[] }>(`${ENDPOINT}/conflicts`)
  conflicts.value = payload.items ?? []
}

function resetQueue() {
  queueQuery.value = {}
  void loadQueue(1)
}

function selectQueueItem(item: QueueItem) {
  selectedQueue.value = item.queue_id
  queueDetail.value = null
  void loadQueueDetail(item.queue_id)
}

async function loadQueueDetail(queueId: string) {
  try {
    queueDetail.value = await fetchJson<QueueItem>(
      `${ENDPOINT}/queue/${encodeURIComponent(queueId)}`,
    )
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '剔除详情读取失败'
  }
}

async function toggleQueueItem(item: QueueItem) {
  const action = item.status === '已处理' ? '恢复待处理' : '标记已处理'
  await postAction(`${ENDPOINT}/queue/${encodeURIComponent(item.queue_id)}/actions`, { action })
  await reloadAll()
}

async function loadDiff() {
  if (!selectedRow.value || selected.value.version === 'R4') {
    errorMessage.value = 'R4 没有更早的规则版本可供对比'
    return
  }
  const query = new URLSearchParams({
    rule_code: selected.value.ruleCode,
    from_version: previousLabel.value,
    to_version: selected.value.version,
  }).toString()
  try {
    diff.value = await fetchJson<DiffPayload>(`${ENDPOINT}/diff?${query}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本差异读取失败'
  }
}

async function recompute(target?: { rule_code?: string; version?: string } | null) {
  errorMessage.value = ''
  message.value = ''
  const ruleCode = target?.rule_code ?? selected.value.ruleCode
  const version = target?.version ?? selected.value.version
  const values: Record<string, unknown> = { rule_code: ruleCode, version }
  if (batchSize.value) values.batch = Number(batchSize.value)
  if (failWindow.value) values.fail_window = failWindow.value
  try {
    const response = await request(`${ENDPOINT}/recompute`, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail ?? '重算请求未生效')
    message.value = payload.message
    if (!payload.ok) errorMessage.value = ''
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算失败'
  }
}

async function resolveConflict(item: ConflictItem, decision: string) {
  await postAction(`${ENDPOINT}/conflicts/resolve`, {
    source_id: item.source_id,
    version: item.version,
    decision,
  })
  await reloadAll()
}

async function postAction(path: string, values: Record<string, unknown>) {
  errorMessage.value = ''
  try {
    const response = await request(path, { method: 'POST', body: JSON.stringify({ values }) })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message ?? payload.detail ?? '操作未生效')
    message.value = payload.message
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function exportDataset() {
  const query = new URLSearchParams({
    rule_code: selected.value.ruleCode,
    version: selected.value.version,
  }).toString()
  window.open(`${ENDPOINT}/dataset/export?${query}`, '_blank')
}

function exportQueue() {
  const query = new URLSearchParams(queueQuery.value).toString()
  window.open(`${ENDPOINT}/queue/export?${query}`, '_blank')
}

async function reloadAll() {
  try {
    await Promise.all([loadOverview(), loadWindows(), reloadRules(), loadQueue(queuePage.value), loadConflicts()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗数据读取失败'
  }
}

onMounted(reloadAll)
</script>
