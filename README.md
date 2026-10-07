# 处置冷链样品温控偏差基础服务

本项目提供极地科考站服务端应用共用的基础能力，用于登记科考机构、站点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。具体的物流、样品、能源、医疗和许可业务可在这些边界上扩展自己的状态、规则和接口。

## 冷链判定项目（coldchain）

针对转机等待期间 -80°C 微生物样品升温这类温控偏差，`polar_station_foundation.coldchain` 在基础边界上实现完整的处置链：

- **发运锁定**：`create_plan` 在发运时锁定包装组合（含跨批次拼箱）、运输分段、允许温区、累计暴露预算（越限分钟数与度·分钟）、传感器校准档案（偏移、漂移率、有效期）和处置规则（各判定结论允许的处置决定），计划一经创建不可修改；
- **证据归一**：`record_readings` 借助可注入时钟记录到达时间，把不同时区（IANA 名或 UTC±HH:MM）的裸时间归一为 UTC，按校准档案修正温度并标记超有效期读数；同一记录器同一时刻的重复数据幂等去重，冲突数据拒绝入库；
- **判断版本**：`compute_assessment` 归并多记录器的重叠读数（阶梯保持、最差值优先），识别缺口、漂移、迟到证据及其影响范围，逐段核算暴露对预算的消耗；证据未变时不重复建版，后补证据只能形成新的判断版本；
- **双角色处置**：科研（researcher）与质量（quality）两个互相独立的角色分别审批，双方到齐后取更严格的决定生成唯一生效版本（生效指针单独维护，旧版本只被取代、不被改写）；只能基于最新判断版本审批；
- **撤回与义务**：`withdraw_disposition` 保留被引用决定的原貌，登记撤回事件并自动生成隔离与通知义务，`complete_obligation` 逐条核销；
- **报告引用**：`create_report` 引用某个处置版本，被引用的决定没有任何更新路径，不会被暗中改写；
- **处置记录**：`disposition_record` 汇总每一段的暴露怎样消耗预算、共享包装影响了哪些样品、撤回后仍有哪些隔离和通知责任未完成。

### 离线验收（冷链）

    PYTHONPATH=src python3 -m polar_station_foundation.coldchain.acceptance

验收复现转机升温偏差：三台记录器分属不同时区与校准区间，其中一台数据迟到。输出一行 status 为 ok 的 JSON，并以退出码 0 结束；关键断言包括首版判断为证据不足、迟到证据触发第二版超预算判断、旧版本审批被拒绝、被引用决定保持原样、严格决定胜出、义务逐条核销。

### HTTP 服务（冷链）

    PYTHONPATH=src python3 -m polar_station_foundation.coldchain.api --database coldchain.sqlite3 --host 127.0.0.1 --port 8081

在基础路由之外增加 `/coldchain/plans`、`/coldchain/readings`、`/coldchain/assessments`、`/coldchain/approvals`、`/coldchain/withdrawals`、`/coldchain/obligations/complete`、`/coldchain/reports` 与 `/coldchain/disposition-record` 等接口，写入同样通过 X-Actor-Id 标识操作者。

## 目录

- src/polar_station_foundation/：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- src/polar_station_foundation/coldchain/：冷链判定项目的表结构、纯计算核心、领域服务、HTTP 路由和偏差处置验收；
- tests/：基础规则、事务边界、接口路由和端到端验收测试。

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

验收命令会在临时 SQLite 数据库中登记科考机构、操作者、站点和业务资料，核对幂等回执与审计链，成功时输出一行 status 为 ok 的 JSON 并以退出码 0 结束。

## HTTP 服务

    PYTHONPATH=src python3 -m polar_station_foundation.api --database polar_station.sqlite3 --host 127.0.0.1 --port 8080

健康检查使用 GET /health。写入接口通过 X-Actor-Id 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
