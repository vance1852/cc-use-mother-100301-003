# 处置冷链样品温控偏差基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

当前已内置冷链判定项目：针对转机等待期间 -80℃ 微生物样品短暂升温、多个记录器时区与校准区间不一致的偏差场景，提供从发运锁定、读数归并、暴露评估到双角色审批与处置记录的完整闭环。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、冷链判定服务、HTTP 路由和离线验收；
- tests/：基础规则、事务边界、接口路由、冷链判定和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests -v

## 构建检查

    python3 -m compileall -q src tests

## 离线验收

    PYTHONPATH=src python3 -m polar_station_foundation.acceptance

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，核对幂等回执与审计链；随后演练一次完整的冷链偏差处置：锁定发运方案、归集三个记录器（不同时区与校准区间）的读数、形成两个评估版本、科研与质量角色分歧后达成一致、生成报告、撤回决定并跟踪后续责任。成功时输出一行 status 为 ok 的 JSON（含 foundation 与 coldchain 两部分结果）并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 冷链判定项目

### 角色

在基础角色之外新增两个互相独立的判断角色：

- researcher（科研负责人）：基于评估提交科研侧判断；
- quality（质量负责人）：基于评估提交质量侧判断，并负责报告与撤回。

### 流程与接口

1. `POST /coldchain/plans` — 发运时锁定批次方案：包装组合（样品与记录器归属）、运输分段时间窗、允许温区与累计暴露预算（分钟）、记录器时区与校准有效期、读数保持时长 max_hold_minutes、处置规则（超预算或缺口叠加升温时的建议结论）。方案一旦锁定不可更改，重复锁定同一批次的不同内容会被拒绝。
2. `POST /coldchain/readings` — 归集记录器读数：无后缀时间按记录器时区归一到 UTC，按校准偏移修正温度，超出校准有效期的读数标记漂移；同一记录器同一时刻的重复读数幂等，内容不同则冲突。
3. `POST /coldchain/assessments` — 归并同一包装内多个记录器的重叠读数（取最坏温度），按运输分段计算每段暴露消耗的预算，并识别缺口（无覆盖区间）、漂移（校准失效读数）与迟到证据（晚于上一版评估到达的读数）及其影响的样品。输入不变时不产生新版本；后补记录只形成新的评估版本，旧版本保留可查（`GET /coldchain/assessment`）。
4. `POST /coldchain/determinations` — 科研与质量角色各自基于最新评估提交 continue_use / restrict_use / destroy 判断；双方一致才产生唯一的生效版本，不一致记为分歧且不影响既有生效决定；基于过期评估的判断会被拒绝。
5. `POST /coldchain/reports` — 引用当前生效决定生成处置报告，报告保存决定快照，之后的撤回或新决定都不会改写它。
6. `POST /coldchain/withdrawals` — 撤回生效决定，自动生成隔离与通知两条后续责任；`POST /coldchain/obligations/complete` 逐条销记。
7. `GET /coldchain/disposition?site_id=&batch_id=` — 最终处置记录：每一段暴露怎样消耗预算（分区累计与剩余）、共享包装影响了哪些样品、决定版本历史、报告快照，以及撤回后仍未完成的隔离和通知责任。

所有写入接口都支持 request_id 幂等，关键状态变化写入哈希串联的审计日志。
