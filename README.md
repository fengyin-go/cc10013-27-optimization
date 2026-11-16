# API数据聚合采集中心

面向多源API接入、接口编排、数据聚合、响应校验、频率控制与数据转储的API数据聚合采集中心。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 接口管理 | `endpoint` | API端点 | 端点编号、接口地址、请求方法 |
| 接口编排 | `chain` | 调用链路 | 链路编号、端点序列、数据传递映射 |
| 频率控制 | `freq` | 频率规则 | 规则编号、目标端点、每秒上限 |
| 数据映射 | `mapper` | 映射规则 | 规则编号、源字段路径、目标字段名 |
| 响应校验 | `validator` | 校验规则 | 规则编号、关联端点、校验字段 |
| 失败重试 | `retry` | 重试策略 | 策略编号、关联端点、最大重试 |
| 数据聚合 | `aggregator` | 聚合任务 | 任务编号、输入端点列表、合并策略 |
| 凭证管理 | `auth` | 认证凭证 | 凭证编号、关联端点、认证类型 |
| 缓存策略 | `cache` | 缓存规则 | 规则编号、关联端点、缓存键模板 |
| 数据转储 | `dump` | 转储配置 | 配置编号、数据来源、存储类型 |
| 采集调度 | `schedule` | 调度计划 | 计划编号、关联链路、调度表达式 |
| 采集监控 | `monitor` | 监控指标 | 指标编号、关联端点、成功率阈值 |
| 数据转换 | `transform` | 转换脚本 | 脚本编号、输入数据源、转换表达式 |
| 数据清洗 | `cleanse` | 清洗规则 | 规则编号、目标字段、空值替换 |
| 限流配置 | `ratelimit` | 限流规则 | 规则编号、目标端点、令牌速率 |
| 采集日志 | `log` | 日志记录 | 记录编号、关联端点、请求时间 |
| 增量采集 | `delta` | 增量游标 | 游标编号、目标端点、增量字段 |
| 数据导出 | `export` | 导出通道 | 通道编号、输出数据集、目标系统 |

## 数据清洗重算流程

清洗的处理入口集中在 `backend/app/services/cleanse.py`，口径计算在
`cleanse_rules.py`：

1. **源记录冻结版本**：源记录入库时写入 `frozen_version`，之后重算只切换
   评估版本，不回写源记录。
2. **按去重窗口重算**：`POST /api/cleanse/recompute` 接收
   `rule_code`、`version`，可选 `batch`（只跑若干窗口）、`fail_window`（演练中断）。
   每个窗口只保留一条唯一结果，重复剔除记录写入清洗待处理队列。
3. **中断续做与幂等**：失败窗口会被记录，再次提交从失败窗口续做；已完成的
   窗口跳过，重复提交不会二次剔除。
4. **待处理队列**：`GET /api/cleanse/queue` 按规则/版本/窗口/批次筛选，
   `GET /api/cleanse/queue/export` 的每条记录都带前后版本差异。
5. **单一导出口径**：`GET /api/cleanse/dataset/export` 只读取一个规则版本的
   完整结果；未完成全部窗口时返回 409，不会导出半套口径。
6. **冲突取舍**：已发布窗口收到更晚更正会形成冲突，通过
   `POST /api/cleanse/conflicts/resolve` 选择保留原值或采用更正，取舍会重算受
   影响窗口的终态。
7. **版本差异**：`GET /api/cleanse/diff` 的字段级差异只列规则版本与去重数量，
   未被规则命中的字段不计入重复剔除；明细可回到目标字段原值。

样例：`CLEANSE-401` R4→R5 目标字段 `email` 共 826 条；`CLEANSE-402` R5
`phone_hash` 为 `input_rows=1240 / removed=17 / output_rows=1223`。

回归检查：`cd backend && .venv/bin/python -m app.check_cleanse`。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
