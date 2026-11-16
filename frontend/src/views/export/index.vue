<template>
  <section class="page" data-module="export">
    <header class="page-head">
      <div>
        <h2>数据导出管理</h2>
        <p class="page-desc">维护导出通道；清洗数据导出只读取一个规则版本的完整结果，不拼接多套口径。</p>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" @click="exportRows">导出数据导出清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无数据导出数据，可先登记导出通道</td>
        </tr>
      </tbody>
    </table>

    <section class="panel">
      <div class="panel-head">
        <h3>清洗数据集导出</h3>
        <span class="hint">同一规则版本一份完整口径；未完成全部窗口时拒绝导出</span>
      </div>
      <div class="panel-actions">
        <label>
          规则编号
          <select v-model="datasetRule" @change="loadDatasetVersions">
            <option v-for="code in ruleCodes" :key="code" :value="code">{{ code }}</option>
          </select>
        </label>
        <label>
          规则版本
          <select v-model="datasetVersion">
            <option v-for="version in datasetVersions" :key="version.version" :value="version.version">
              {{ version.version }}（{{ version.status }}）
            </option>
          </select>
        </label>
        <button class="btn primary" type="button" @click="loadDataset">读取数据集</button>
        <button class="btn" type="button" @click="exportDataset">导出该版本</button>
      </div>
      <div v-if="datasetError" class="detail error-text">{{ datasetError }}</div>
      <div v-if="datasetCounts" class="detail">
        <div class="detail-grid">
          <div><span>规则版本</span>{{ datasetRule }} {{ datasetVersion }}</div>
          <div><span>目标字段</span>{{ datasetField }}</div>
          <div><span>输入 / 剔除 / 输出</span>{{ datasetCounts.input_rows }} / {{ datasetCounts.removed_rows }} / {{ datasetCounts.output_rows }}</div>
          <div><span>冻结版本</span>{{ datasetFrozen }}</div>
        </div>
        <table class="data-table">
          <thead>
            <tr><th>源记录</th><th>窗口</th><th>目标字段原值</th><th>清洗后</th><th>去重键</th><th>规则版本</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in datasetItems" :key="item.source_id">
              <td class="mono">{{ item.source_id }}</td>
              <td class="mono">{{ item.window }}</td>
              <td class="mono">{{ item.original_value || '（空值）' }}</td>
              <td class="mono">{{ item.cleaned_value }}</td>
              <td class="mono">{{ item.dedup_key }}</td>
              <td>{{ item.rule_version }}</td>
            </tr>
            <tr v-if="!datasetItems.length">
              <td colspan="6" class="empty-state">尚未读取数据集</td>
            </tr>
          </tbody>
        </table>
        <p class="muted">预览前 {{ datasetItems.length }} 条；导出文件包含该版本全部 {{ datasetCounts.output_rows }} 条输出。</p>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条数据导出记录</span>
      <span v-if="message" class="muted">{{ message }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { fetchJson, request } from '@/api/client'

type Row = Record<string, string | number | null>
type DatasetItem = {
  source_id: string
  window: string
  original_value: string | null
  cleaned_value: string
  dedup_key: string
  rule_version: string
}
type Counts = { input_rows: number; removed_rows: number; output_rows: number }

const ENDPOINT = '/api/export'
const columns = ["通道编号", "输出数据集", "目标系统", "导出格式", "传输协议", "频率限制", "加密开关", "完整性校验"]
const actions = ["提交校验", "启用通道", "停用通道"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const stats = [{"label": "导出通道总数", "value": 0}, {"label": "已启用通道", "value": 0}, {"label": "待校验通道", "value": 0}]
const ruleCodes = ['CLEANSE-401', 'CLEANSE-402']

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const message = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const datasetRule = ref('CLEANSE-402')
const datasetVersion = ref('R5')
const datasetVersions = ref<{ version: string; status: string }[]>([])
const datasetItems = ref<DatasetItem[]>([])
const datasetCounts = ref<Counts | null>(null)
const datasetField = ref('')
const datasetFrozen = ref('')
const datasetError = ref('')

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function loadDatasetVersions() {
  const payload = await fetchJson<{ rules: { rule_code: string; versions: { version: string; status: string }[] }[] }>(
    '/api/cleanse/versions',
  )
  const rule = payload.rules.find((item) => item.rule_code === datasetRule.value)
  datasetVersions.value = (rule?.versions ?? []).map((item) => ({ version: item.version, status: item.status }))
  if (!datasetVersions.value.some((item) => item.version === datasetVersion.value)) {
    datasetVersion.value = datasetVersions.value.at(-1)?.version ?? ''
  }
}

async function loadDataset() {
  datasetError.value = ''
  const query = new URLSearchParams({
    rule_code: datasetRule.value,
    version: datasetVersion.value,
    size: '20',
  }).toString()
  try {
    const payload = await fetchJson<{
      rule_version: string
      target_field: string
      frozen_version: string
      counts: Counts
      items: DatasetItem[]
    }>(`/api/cleanse/dataset?${query}`)
    datasetItems.value = payload.items
    datasetCounts.value = payload.counts
    datasetField.value = payload.target_field
    datasetFrozen.value = payload.frozen_version
  } catch (error) {
    datasetItems.value = []
    datasetCounts.value = null
    datasetError.value = error instanceof Error ? error.message : '清洗数据集读取失败'
  }
}

function exportDataset() {
  const query = new URLSearchParams({
    rule_code: datasetRule.value,
    version: datasetVersion.value,
  }).toString()
  window.open(`/api/cleanse/dataset/export?${query}`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('数据导出动作未生效，请稍后重试')
    }
    message.value = `${action}：${String(row['通道编号'] ?? '')}`
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据导出操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('导出通道列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据导出列表读取失败'
  }
}

onMounted(async () => {
  await reload()
  await loadDatasetVersions()
})
</script>
