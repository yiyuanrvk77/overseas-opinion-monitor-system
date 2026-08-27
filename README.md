# 社科院海外舆情监测系统

一套可单机部署的海外舆情监测工作台，依据《社科院海外监测系统需求优先级.xlsx》实现。当前活动批次可由 `data/public_demo_data.json` 提供公开网页小规模样本，并完整接入批次、专题、检索、知识库、图谱、报告、来源下钻和审计链。原 `trump家族分析.zip` 只保留为回归测试批次，不是产品专题。

## 一键运行

Windows 双击 `start.bat`，或在 PowerShell 中运行：

```powershell
.\start.ps1
```

Linux 上运行（详见 `deploy/DEPLOY-LINUX.md`）：

```bash
chmod +x start.sh && ./start.sh
```

首次运行会创建本地 Python 环境、安装依赖、构建前端，然后在以下地址启动产品。依赖和构建产物完整时，后续启动会跳过安装与构建，可在断网环境直接运行：

```text
http://127.0.0.1:8000
```

API 文档：`http://127.0.0.1:8000/docs`

## 开发模式

后端：

```powershell
.\.venv\Scripts\python -m uvicorn backend.api:app --host 127.0.0.1 --port 8000 --reload
```

前端：

```powershell
npm install
npm run dev
```

开发界面位于 `http://127.0.0.1:5173`，Vite 会将 `/api` 转发到本地后端。

## 公开数据采集与导入

采集器登记 X、Truth Social、Facebook、TikTok、YouTube、Instagram、Reuters、AP、The New York Times、The Wall Street Journal、CNN 和 BBC 共 12 个渠道。它只请求无需登录即可读取的 RSS、Atom 或公开网页元数据，不提交账号、Cookie，不绕过验证码、登录墙、付费墙或访问控制。无法读取的渠道仍会保存 HTTP 状态和受限原因，但不会伪造内容记录。

```powershell
python scripts\collect_public_web.py --timeout 18 --limit 24
python scripts\import_public_demo.py --db backend\data\opinion_monitor.db
```

采集输出采用临时文件原子替换，重复运行按来源 URL 去重；导入同样幂等。批次保存采集时间、渠道状态、命中数、来源链接、内容哈希和快照 SHA-256。当前专题为雄安新区、APEC 2026、习近平海外活动。具体样本数会随公开源更新，以 `/api/public-demo/status` 返回为准。

更完整的来源边界、接口和运行说明见 `docs/public-demo-implementation.md`。

## 回归测试批次

ZIP 快照被完整规范化为 134 条记录、11 类数据：

| 类别 | 数量 | 类别 | 数量 |
| --- | ---: | --- | ---: |
| 账号实体 | 10 | 人物实体 | 14 |
| 画像信号 | 10 | 逐帖内容 | 32 |
| 事件线索 | 15 | 关系圈层 | 5 |
| 商业与政治信号 | 8 | 来源台账 | 12 |
| 研究分析 | 9 | 口径冲突 | 7 |
| 生产字段缺口 | 12 | 合计 | 134 |

每条记录保留来源文件、证据性质、敏感级、内容哈希和批次信息。19 项冲突或缺口保持为质量问题，系统不会自动补造 URL、帖子 ID、情感标签或实时指标。

## 产品能力

- 本地 SQLite 数据底座、批次版本、来源指纹、质量问题和哈希审计链
- 12 个目标渠道的公开可达性采集与正式连接器注册表；授权未完成时明确显示“待配置”
- 人物、账号、内容、事件、来源和质量记录的可追溯浏览
- 版本化嵌入式知识库，持久化 256 维离线特征向量
- 词法、规则和离线特征向量的混合检索，以及带引用的检索式问答
- 人物、事件、传播、证据四种有向异构知识图谱
- 风险线索、认领/处置状态和审计记录；未运行正式模型时不生成虚构结论
- 人物、事件、议题、周报、月报及溯源报告模板
- 角色演示与第一层限制数据过滤
- 数据库完整性/外键校验、WAL 并发等待和原子一致性备份

正式工作台只保留运行总览、监测对象、采集与批次、知识库、知识图谱、分析研判、风险线索、报告中心和检索式问答 9 个业务页面。需求验收、质量台账和审计事件不作为正式导航页面，仅保留在技术文档、自动化测试和内部维护接口中。

## 事实边界

当前没有 12 个平台的正式账号授权、生成式大模型、语义嵌入模型、情感真值或多语种模型。公开采集器用于小规模可核验数据接入，不等同于供应商级实时连接器。因此：

- 正式连接器任务只能保存为草稿；公开试采运行与正式授权状态分别展示。
- “离线特征向量”不是大模型语义 embedding。
- 问答是检索摘要，不是大模型生成答案。
- 公开样本与 ZIP 都没有人工情感真值，本版不输出虚构情感比例。
- 角色切换用于演示数据过滤，生产部署必须接入统一身份、服务端授权和 HTTPS。
- 任务草稿和风险处置写操作仅允许核心课题组演示角色，研究员视角为只读。

## 测试

```powershell
.\.venv\Scripts\python -m unittest discover -s backend\tests -v
npm run build
```

Linux 上先安装测试依赖（额外包含测试客户端需要的 `httpx2`）：

```bash
./.venv/bin/python -m pip install -r backend/requirements-dev.txt
./.venv/bin/python -m unittest discover -s backend/tests -v
```

自动化测试覆盖批次幂等导入、134 条分类计数、角色过滤、混合检索、四类图谱、任务草稿持久化、预警处置、审计链、报告生成和无效请求回滚。

当前共 50 项自动化测试，还覆盖公开采集器过滤与幂等、活动批次隔离、服务端认证越权、审计区块脱敏、同数量数据替换、索引缺块自愈、损坏向量降级、无关问题拒答、并发读写、冷启动锁、审计检查点、静态交付和备份完整性。

## 数据库维护

校验当前数据库：

```powershell
.\.venv\Scripts\python -m backend.maintenance verify backend\data\opinion_monitor.db
```

创建并校验一致性备份：

```powershell
.\.venv\Scripts\python -m backend.maintenance backup backups\opinion_monitor.db
```

备份先写入同目录临时文件，通过完整性和外键检查后再原子替换目标文件。

## 工程结构

```text
backend/       FastAPI、SQLite、导入、检索、图谱和测试
src/           React 产品界面与 ZIP 规范化快照
docs/          产品技术文档
start.ps1      Windows 一键构建与启动
```

正式数据接入时应新增平台适配器并保持统一的批次、来源、证据和质量接口，不需要修改业务页面的数据边界。
