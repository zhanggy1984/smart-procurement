# AI辅助评审系统 — 任务拆分

> 基于 solution.md v4，目标 1 人全职约 4 个月（~86 人天）。
> 部署方式：本机 Docker Compose 一键启动全部中间件 + 应用容器。

---

## P0：项目脚手架 + Docker 环境（6d）

### P0.1 Poetry 项目初始化（1d）
- 创建 `smart-procurement/` 目录结构（`app/`、`tests/`、`scripts/`、`frontend/`）
- `poetry init` + `pyproject.toml` 依赖声明（FastAPI、SQLAlchemy、neo4j、pymilvus、minio-py、openai、arq 等）
- `.env.example` 模板（不含密钥，含所有必填项和默认值）
- `app/__init__.py` + `app/main.py` 最小 FastAPI 应用（`/health/live` 端点）
- **验收**：`poetry install` 无报错，`uvicorn app.main:app` 可启动并访问 `/health/live`

### P0.2 Docker Compose 中间件编排（2d）
- `docker-compose.yml`：MySQL 8.0、Neo4j 5.x、Milvus 2.4、MinIO、Redis 7 共 5 个中间件容器
- 容器依赖拓扑：MySQL/Redis/Neo4j 为硬依赖（`depends_on + condition: service_healthy`），Milvus/MinIO 为软依赖
- 每个容器配置 healthcheck、资源限制（`mem_limit`、`cpus`）、非 root 用户运行
- 挂载卷：MySQL 数据、Neo4j 数据、Milvus 数据、MinIO 数据、Redis 数据均映射到 `./data/` 目录
- `app/core/config.py`：pydantic-settings 从 `.env` 加载全部配置
- **验收**：`docker compose up -d` 后 `docker compose ps` 全部 healthy（120s 内），FastAPI `/health/ready` 返回四库连通状态

### P0.3 FastAPI App + arq Worker 容器化（2d）
- FastAPI App Dockerfile：Python 3.11+、非 root 用户、readonly 挂载代码
- arq Worker Dockerfile：共享 App 镜像，仅启动命令不同（`arq app.tasks.worker.WorkerSettings`）
- `app/core/database.py`：SQLAlchemy async engine（`pool_size=20, max_overflow=10, pool_pre_ping=True`）
- `app/core/neo4j.py`：Neo4j driver 单例
- `app/core/milvus.py`：Milvus client 单例 + `collection.load()` 启动预热
- `app/main.py` lifespan：启动时校验 MySQL/Neo4j/Milvus/Redis 连通性 + DeepSeek API key，失败 `exit(1)`
- Docker Compose 加入 FastAPI App 和 arq Worker 两个服务容器
- **验收**：`docker compose up -d` 全栈 8 容器全部 healthy

### P0.4 数据库初始化 + 迁移框架（1d）
- Alembic 初始化（`alembic init`），配置 `asyncmy` 驱动
- 第 1 个 migration：创建全部 21 张 MySQL 表（DDL 来自 solution.md 1.6 节）
- Neo4j 启动脚本：幂等创建索引（节点索引 + 关系索引）+ constraint
- Milvus Collection 创建脚本：`bid_documents` schema + IVF_FLAT 索引
- **验收**：`alembic upgrade head` 建表成功，Neo4j Browser 可查索引，Milvus `list_collections()` 含 `bid_documents`

---

## P1：知识图谱 + CRUD API（12.5d）

### P1.1 知识图谱数据初始化（2d）
- `scripts/generate_synthetic_data.py`：Python 假数据生态（faker + mimesis + networkx）
  - 30 专家（姓名、单位、地区、专业标签、从业年限）+ 20 供应商（企业名、信用代码、法人、行业、规模）
  - 5 项目 + 15 标段（含预算、评分维度配置、专家遴选参数）
  - 冲突关系网络：networkx 控制密度 8%-15%，生成 EMPLOYED_BY / HOLDS_SHARE / SAME_ORGANIZATION / RELATIVE_EMPLOYED + 供应商关联 AFFILIATE_OF / SAME_CONTROLLER
- `scripts/validate_synthetic_data.py`：质量门禁（标签多样性 ≥5、冲突密度 8%-15%、每标段可用专家 ≥3、标书 ≥2000 字、至少 1 对 SAME_CONTROLLER + 1 对 BID_TOGETHER）
- Neo4j 数据导入脚本 + MySQL 数据导入脚本
- **验收**：`python scripts/generate_synthetic_data.py --projects 5 --experts 30 --suppliers 20` 执行成功，validation 全部通过

### P1.2 认证授权 API（2d）
- `app/models/user.py`：User SQLAlchemy 模型
- `app/services/user_service.py`：`create_user`、`authenticate`、`get_user`
- `app/api/v1/auth.py`：`POST /api/v1/auth/login`（返回 JWT access_token + refresh_token）
- `app/api/deps.py`：`get_current_user`（JWT 校验）+ `RequireRole`（角色鉴权）
- `app/core/crypto.py`：Fernet 身份证号加密 + SHA256 哈希
- 密码复杂度校验（≥8 位 + 大小写 + 数字）+ bcrypt 存储
- **验收**：`POST /api/v1/auth/login` 正确密码→200+JWT，错误密码→401

### P1.3 项目管理 CRUD API（2d）
- `app/models/project.py`、`app/schemas/project.py`
- `app/services/project_service.py`
- `app/api/v1/projects.py`：
  - `POST /api/v1/projects`（创建项目，校验 region 为受控值）
  - `GET /api/v1/projects/{id}`（项目详情）
  - `POST /api/v1/projects/{id}/lots`（创建标段，校验 `SUM(lot.budget) ≤ project.budget`）
  - `POST /api/v1/lots/{id}/dimensions`（配置评分维度，校验 `SUM(weight)=1.0 ± 0.001`）
  - `POST /api/v1/lots/{id}/expert-criteria`（配置专家遴选，校验 `expert_count ≥ min_experts_per_dimension`）
- Neo4j 同步：Outbox Pattern（创建 Project→Neo4j 节点 + CONTAINS_LOT 关系）
- **验收**：Swagger 测试 CRUD 全链路，创建项目→创建标段→配置维度→配置遴选参数，Neo4j Browser 可见节点和关系

### P1.4 专家 + 供应商管理 API（3d）
- `app/models/expert.py`、`app/models/supplier.py`
- `app/services/expert_service.py`、`app/services/supplier_service.py`
- `app/api/v1/experts.py`：
  - `POST /api/v1/experts/import`（Excel 批量导入）
  - `PUT /api/v1/experts/{id}/status`（启用/停用/拉黑）
  - `DELETE /api/v1/experts/{id}`（逻辑删除→INACTIVE）
- `app/api/v1/suppliers.py`：
  - `POST /api/v1/suppliers/import`（Excel 批量导入）
  - `PUT /api/v1/suppliers/{id}/status`（拉黑/逻辑删除）
- `app/api/v1/conflicts.py`：
  - `POST /api/v1/conflicts/import`（企查查 CSV 导入，含冷数据唤醒逻辑）
- 供应商黑名单级联：关联未定标评审→SUSPENDED，未封存标书→DISQUALIFIED，已定标项目不受影响
- Neo4j 同步：EXPERT_CREATED / SUPPLIER_CREATED / CONFLICT_IMPORTED / SUPPLIER_BLACKLISTED 事件→Outbox→Neo4j
- **验收**：导入 30 专家 + 20 供应商 + 企查查 conflict CSV，Neo4j Cypher 确认节点+关系正确

### P1.5 标书管理 API（2d）
- `app/models/bid_document.py`（含 `parsing_step` checkpoint 字段）
- `app/services/bid_document_service.py`
- `app/api/v1/bids.py`：
  - `POST /api/v1/lots/{id}/bids`（multipart 上传，上限 50MB，magic bytes 校验 PDF/DOCX）
  - `GET /api/v1/bids/{id}`（含结构化数据）
  - `GET /api/v1/bids/{id}/status`（解析进度，含 parsing_step）
  - `POST /api/v1/bids/{id}/retry-parse`（解析失败手动重试）
- MinIO 集成：上传文件→MinIO，生成预签名 URL（30min 有效）
- **验收**：上传 3 份标书 PDF→MySQL bid_document 有记录→MinIO 可下载，状态从 SUBMITTED 开始流转

### P1.6 Outbox 事件系统（1.5d）
- `app/services/outbox.py`：`write_with_outbox()`（MySQL 事务内写入 outbox_event）
- arq 后台 worker 消费 outbox：`SELECT FOR UPDATE SKIP LOCKED` 拉取 PENDING → 同步 Neo4j/Milvus → 标记 PROCESSED/FAILED
- Reconciliation job：每小时扫描 FAILED 记录，MERGE 语义幂等重放
- **验收**：创建 Expert → outbox_event INSERT → Neo4j 自动出现对应节点，FAILED 记录 1h 后 reconciliation 修复

---

## P2：文档解析 + RAG 索引（11d）

### P2.1 标书异步解析流水线（4d）
- `app/tasks/document_ingest.py`：arq job，7 步 checkpoint 流水线
  - Step 1：pdfplumber/python-docx 提取全文
  - Step 2：规则提取报价/工期/人员等结构化字段→MySQL
  - Step 3：SmartDocumentChunker 标题感知分块（500-1000 tokens，overlap 100）
  - Step 4：BGE-M3 Embedding（1024 维，dev 模式 sentence-transformers 直连，通过 `asyncio.to_thread()` 卸载）
  - Step 5：Milvus 批量入库
  - Step 6：Neo4j 同步节点+关系
  - Step 7：`bid_document.status → PARSED`，`parsing_step → NULL`
- 每步完成后 UPDATE `parsing_step` 字段
- `max_retries=3, retry_delay=60s`，失败→PARSE_FAILED
- 后台定时任务：扫描 `parsing_step > 0 AND updated_at > 30min` 的僵尸记录→自动标记 PARSE_FAILED
- DOCX 压缩炸弹防御：解压比≤100:1，解压上限 200MB
- **验收**：上传 3 份标书 PDF→自动解析完成→`parsing_step=NULL, status=PARSED`，结构化数据准确率 ≥95%

### P2.2 Milvus 向量检索（2d）
- `app/ai/rag/embedder.py`：BGE-M3 编码（dev 模式直连，prod 预留 HTTP 端点）
- `app/ai/rag/retriever.py`：多路召回
  - 路1：Milvus 向量语义检索（`filter='lot_id=="LOT-01" && bid_id=="BID-001"', top_k=20, metric="IP"`）
  - 路2：关键词精确匹配（正则匹配评分标准关键术语）
  - 路3：MySQL 精确查结构化数据
- RRF 融合排序（k=60，融合向量+关键词两路→Top-8）
- **验收**：`Recall@5 ≥ 0.85`（30 条评审 query），维度感知检索 `Recall@5` 高于无维度 ≥10%

### P2.3 多轮对话管理（2.5d）
- `app/services/conversation_service.py`：
  - `add_message()`：追加消息，维护 `turn_number` 和 `dim_turn_number`
  - `get_context()`：组装当前维度上下文（最近 3 轮原文 + 历史摘要）
  - `maybe_summarize()`：第 4 轮触发→DeepSeek 摘要压缩前一阶段→存入 `conversation_message`（`message_type=SUMMARY`）
- 上下文窗口控制：≤8000 tokens（含 ~1000 安全边际）
- 摘要 LLM 调用失败→保留最近 3 轮原文兜底 + 前端提示
- **验收**：单维度连续追问 10 轮→前后对话连贯，摘要正确生成，token 预算不超标

### P2.4 空结果与降级处理（1.5d）
- 向量检索无结果→`event:thinking`→"该标书正在解析中，请稍后再试"
- 全部 chunk 低于阈值（IP<0.5）→"未找到与该问题相关的依据"
- BGE-M3 不可用（prod 模式）→"AI 推理引擎暂不可用"，评分降级为纯人工
- Milvus 超时（10s）→降级为关键词路+MySQL 精确查
- Neo4j 超时→supplier 信息从 MySQL 回退
- MySQL 超时→503"核心数据暂不可用"
- **验收**：构造 Milvus/Neo4j 超时场景，各降级路径正确触发

### P2.5 标书结构化对比检索（1d）
- 跨标书对比检索接口（评后汇总用）：同一维度检索不同供应商标书，结果分别标注来源
- **验收**：输入相同 query→两份标书分别返回 chunks→标注 supplier_id 来源

---

## P3：AI 辅助评审核心（13d）

### P3.1 DeepSeek Client + 容错（2d）
- `app/ai/llm/deepseek_client.py`：
  - `chat_stream()`：SSE 流式调用，temperature=0.3
  - 断路器：连续 N 次超时/5xx→熔断 30s→半开探测（N 由 `DEEPSEEK_CIRCUIT_BREAKER_THRESHOLD` 配置，默认 5）
  - 重试：429→1s/2s/4s 退避；502/503→0.5s/1s/3s（少 1 次）；401/403→不重试
- **验收**：正常流式返回 + 构造 429/502→正确重试 + 连续 5 次 503→熔断 OPEN

### P3.2 Prompt 模板管理（1.5d）
- `app/ai/llm/prompts.py`：System Prompt 模板
  - 评分模式：角色设定 + 评分维度与标尺（rubric）+ 标书 chunks（`<bid_content>` 标签隔离）+ 结构化数据
  - 对话模式：角色设定 + 上下文 + 历史对话
- 意图标记：LLM 首个 token 输出 `[INTENT: SCORE_REQUEST/TECH_DETAIL/GENERAL]`
- Prompt 注入防御：`<bid_content>` 标签隔离，忽略"忽略上述要求"等对抗文本
- **验收**：30 条标注 prompt 测试集→意图识别准确率 ≥90%

### P3.3 AI 评标打分 API（3d）
- `app/api/v1/reviews.py`：
  - `POST /api/v1/reviews`（创建评审工作台，校验 bid.status=FROZEN）
  - `POST /api/v1/reviews/{id}/score`（SSE 流式评分，每事件带 `id:` 序号）
  - `POST /api/v1/reviews/{id}/chat`（SSE 流式对话）
- SSE 事件流：`thinking→source→thought→score/citation→done`
- `event:price_calc`：报价维度走纯公式（SQL RANK/数学公式），不走 AI
- 多轮对话：同一 review_id 追加上下文
- 评分幂等：前端生成 `X-Idempotency-Key`（UUID v4），后端检查去重
- **验收**：完整评审对话 P95<20s，SSE 无断流，评分 MAE≤2.0（满分 10）

### P3.4 评审工作台业务逻辑（2d）
- `app/services/review_service.py`：评审核心编排
  - `score_dimension()`：并行查三层（MySQL+Neo4j+Milvus）→构建 Prompt→DeepSeek 流式返回
  - 维度暂存（DRAFT）→全部维度完成后提交（CONFIRMED/MANUAL_ADJUSTED）
  - 提交后不可回改（review 级锁定）
- 报价评审剥离：最低价法 SQL RANK/综合评分法纯公式，前端展示公式+结果+数据来源
- **验收**：维度暂存后回到工作台可看到 DRAFT 状态→提交全部→所有维度统一锁定

### P3.5 评审收尾 API（2d）
- `POST /api/v1/lots/{id}/complete-review`：结束评审→自动触发深度围串标检测→生成报告 PDF→lot.status→EVALUATED
- `GET /api/v1/lots/{id}/summary/report`：下载评审总结报告
- `POST /api/v1/projects/{id}/submit-for-award`：推送定标结果→project.status→AWARDED→触发归档
- 归档 job（arq 后台）：专家画像重算 + 供应商共投关系 MERGE + 评分标准区分度校准 + 跨项目关联
- AI 综合评标总结（PM 评后汇总页）+ 落标原因 AI 摘要（供应商结果页）
- **验收**：lot EVALUATED→生成报告→project AWARDED→归档 job 完成→expert_profile 更新

### P3.6 评审对话 SSE 完整实现（2.5d）
- SSE 断流恢复：每条事件 `id:` 字段→前端 `Last-Event-ID`→后端从断点续推
- 断流超时>30s→返回 `event:reset`→前端全量重拉该轮 messages
- `X-Request-ID`（UUID7）中间件：所有下游调用携带该 ID，structlog 记录
- AI 不可用降级 UI：断路器 OPEN→返回 503→前端切换纯人工评审表单
- **验收**：断流后自动重连成功，事件不丢失，断路器 OPEN 后降级正确

---

## P4：专家匹配 + 回避检测 + 自申报（9d）

### P4.1 受控词表 + LLM 标签翻译（1.5d）
- 受控词表管理：预设标签库（教育信息化/软件开发/系统集成/网络安全/...），管理员维护
- LLM 标签翻译：项目描述→DeepSeek→受控词表内的专业标签列表
- LLM 不可用降级：PM 手动从多选下拉框选择标签，匹配结果标注 `match_mode: MANUAL_TAG_SELECTION`
- **验收**：输入"某市教育局智慧校园平台采购"→LLM 输出 ["教育信息化","软件开发","系统集成"]，全部在词表内

### P4.2 专家匹配算法（3d）
- `app/services/expert_match_service.py`：5 步匹配流程
  - Step 1：Neo4j 候选搜索（LLM标签+项目地区精确匹配，Top-20）
  - Step 2：获取投标供应商列表
  - Step 3：逐专家冲突检测（4 条回避路径→Neo4j，合并为一条批量 Cypher，15 专家 asyncio.gather 并行）
  - Step 4：多维加权排序（specialization_match×0.40 + experience×0.30 + review_quality×0.20 + region×0.10）
  - Step 5：维度覆盖检查（标签→维度映射，不足的从备选池补入，底线≥1 标签命中）
- `POST /api/v1/lots/{id}/match-experts`（前置校验 lot.status=UNDER_REVIEW，拒绝 ABANDONED/DISQUALIFIED）
- `GET /api/v1/lots/{id}/match-experts`（查看匹配结果+维度分配详情）
- 结果落库 `lot_expert_assignment`（状态：PENDING_DECLARATION）
- **验收**：4 种回避冲突 100% 召回 + 误报率<10%，可用专家<expert_count→INSUFFICIENT_EXPERTS 告警

### P4.3 专家回避申报（2.5d）
- `app/services/expert_declaration_service.py`
- `GET /api/v1/experts/me/assignments`：我的任务列表
- `GET /api/v1/experts/assignments/{id}/declaration`：获取待申报供应商列表
- `POST /api/v1/experts/assignments/{id}/declare`：提交回避申报（逐供应商确认/申报）
  - 全部确认无冲突→`assignment.status→IN_PROGRESS`→可进入评审
  - 申报冲突关系→写入 `expert_conflict_declaration`→Neo4j 关系同步→`assignment.status→CONFLICT_DECLARED`→自动补匹配
- 站内信通知：专家收到回避申报通知
- **验收**：两条路径均走通（无冲突→评审，申报冲突→补匹配→新专家收到通知）

### P4.4 站内信通知系统（2d）
- `app/services/notification_service.py`：`send()`、`query()`、`get_unread_count()`、`mark_read()`、`mark_all_read()`
- API：`GET /api/v1/notifications`（分页+筛选）、`GET /notifications/unread-count`、`PUT /notifications/{id}/read`、`PUT /notifications/read-all`
- 触发时机：回避申报、评审分配、偏差告警、全部完成、退回重评、供应商黑名单、围串标初筛、定标结果、供应商质疑（共 9 种）
- 前端铃铛图标 + 未读红点 + HTTP 轮询（30s）
- **验收**：专家分配标段→通知生成→前端铃铛红点+1→点击跳转回避申报页

---

## P5：围串标检测 P1（9.5d）

### P5.1 围串标初筛（3d）
- `POST /api/v1/lots/{id}/close-bidding`：PM 点击关闭投标
  - 校验有效标书数（PARSED+PARSING≥3，<3→ABANDONED）
  - PARSING 状态标书等待完成或 PM 强制截断
  - SELECT FOR UPDATE 锁 lot 行防并发
- 初筛三检（不走 AI）：关系图谱粗检（Neo4j）+ 报价异常初检（MySQL）+ 标书 chunk 级高相似段落对判定（Milvus + FAISS，命中对数 ≥7 判定围串标组合；P5.1 回归：原"标书级平均向量 cosine"对同主题专业标书区分度不足）
- 风险评分：LOW(≤25) 自动通过/MEDIUM+(>25) PM 待办确认
- PRE_SCREEN 通过→标书 FROZEN→lot→UNDER_REVIEW→启动专家匹配
- **验收**：3 家正常投标→LOW 自动通过；1 对 SAME_CONTROLLER→MEDIUM 待办

### P5.2 深度检测 — 标书语义相似度（2d）
- `app/services/fraud_detection_service.py`
- FAISS 批量 cosine 相似度（20% 采样替代 N² 次 Milvus 网络往返）
- 输出：整体相似度矩阵 + 高相似度段落对（IP>0.85）
- **验收**：2 组围串标 100% 命中 + 2 组正常 0% 误报

### P5.3 深度检测 — 关系图谱 + 报价模式（2d）
- 供应商关联检测：Neo4j Cypher（AFFILIATE_OF/SAME_CONTROLLER/BID_TOGETHER，含 `min()` 上限保护）
- 报价异常检测：报价集中度（差异<1%）、陪标模式（第一名异常低）
- 综合风险评分：text×0.40 + graph×0.35 + price×0.25，四级分类（LOW/MEDIUM/HIGH/CRITICAL）
- **验收**：同一控制人+异常相似度+价格集中→HIGH/CRITICAL 正确触发

### P5.4 围串标报告 + 前端呈现（2.5d）
- 模板报告（LOW/MEDIUM 等级，自动生成）
- LLM 报告（HIGH/CRITICAL 等级，PM 手动触发）
- PM 待办页：风险评分+热力图+相似段落对照+供应商关系力导向图
- **验收**：报告包含风险评分+关键证据+建议措施，前端热力图+关系图正确渲染

---

## P6：前端开发（17.5d）

### P6.1 Vue 3 脚手架 + 公共组件（2d）
- Vite + Vue 3 + Element Plus + Pinia + Vue Router + axios
- 登录页 + 顶部导航栏（铃铛通知 + 未读红点 + 角色路由守卫）+ 布局框架
- **验收**：`npm run dev` 启动，四个角色各看到对应菜单

### P6.2 管理员端（2d）
- 用户管理页（列表+创建/编辑弹窗）
- 专家导入页（Excel 拖拽上传+校验预览+确认导入）
- 供应商导入页
- 工商信息导入页（企查查 CSV→预览匹配结果→确认导入）
- ✅ 系统配置页（LLM参数/回避规则/偏差阈值，即时生效，改完不重启）—— 2026-08-13 完成（11 项配置，2 项孤儿标注未接入）
- ~~操作日志查询页~~ —— 2026-08-13 用户决定不做，从计划移除（前端已删）
- **验收**：6 个页面可访问 + loading/empty/error 三态覆盖

### P6.3 项目经理端（4d）
- 项目列表页 + 新建/编辑弹窗
- 项目详情页（页内 Tab：基本信息→标段管理→评分维度配置）
- 标书列表页（上传标书+解析状态+下载原件）
- 专家匹配页（一键匹配→查看结果+维度分配→无需确认自动落库）
- 评审进度页（各标段各专家各维度进度一览）
- 偏差处理页（偏差预警列表+发起重评）
- 围串标待办页（MEDIUM+初筛结果待确认）
- 评标汇总页（评分汇总+生成报告PDF+提交定标+归档）
- **验收**：8 个页面可访问 + 三态覆盖

### P6.4 评审专家端（4d）
- 我的任务页（待申报+待评审+已评审，分 tab）
- 回避申报页（逐供应商确认/申报冲突关系）
- 评审工作台（核心页面，三栏布局）：
  - 左栏：评分维度面板（仅显示被分配的维度），点击触发 AI 评分
  - 中栏：AI 对话区（SSE 流式 Markdown 渲染，DOMPurify 净化），[保存]/[修改]/[追问] 快捷按钮
  - 右栏：证据溯源面板（chunk 原文+章节+页码，PDF 跳转链接）
  - 「提交全部评分」按钮→统一锁定
- 评审历史页（个人历史列表+评分统计）
- 首次评审引导：随机化评审顺序 + 评分锚定提示
- **验收**：4 个页面可访问 + 三态覆盖 + SSE 异常场景（断流重连/超时降级/接口报错）处置正确

### P6.5 供应商端（3.5d）
- 招标市场列表页（筛选：类型/地区/预算）+ 项目详情+标段列表
- 标段详情页（预算+评分维度+「参与投标」按钮→跳转上传）
- 标书上传页（拖拽 PDF/DOCX + 进度条 + SHA256 校验 + 解析状态）
- 标书详情页（结构化信息+解析结果）
- 投标结果列表页（状态标签：评审中/已中标/未中标）
- 结果详情页（中标：通知+后续步骤；未中标：排名+各维度得分+落标原因）
- 质疑入口（预留）
- **验收**：7 个页面可访问 + 三态覆盖

### P6.6 降级 UI + Empty State（1.5d）
- AI 不可用降级：红色 Banner"AI 辅助评分暂不可用"+ 左侧标书原文预览 + 右侧纯人工评分表单
- AI 恢复后→"切换回 AI 辅助模式"按钮（专家主动触发）
- 各角色 Empty State 引导文案（按 4.2 节设计）
- **验收**：断 DeepSeek→降级 UI 正确渲染，AI 恢复→可切回

### P6.7 联调 + 端到端冒烟（0.5d）✅ 完成
- 前端所有 API 对接后端联调
- SSE 全链路联调（正常流+断流重连+超时降级）
- **验收**：全栈可走通完整业务流程（登录→创建项目→上传标书→关闭投标→匹配专家→回避申报→评审→定标→供应商查看结果）✅ 2026-08-12 浏览器全链路实测通过（LOT-013 后半程：匹配→回避→评审→定标 PRJ-005=AWARDED→供应商 supplier_08 查看「恭喜中标」）；API 覆盖率静态对照全对齐+补接 match-experts/submit-for-award；SSE 正常流实测+修 2 bug（score 解析兜底正则、去意图前缀）

---

## P7：集成测试 + 交付准备（8d）

### P7.1 合成数据质量门禁（1d）

- `scripts/validate_synthetic_data.py`：全部断言通过
- 3 个演示场景数据准备：正常评审 / 冲突回避 / 围串标检测

**数据质量校验项**：

| 校验项 | 断言 |
|--------|------|
| 专业标签多样性 | `len(all_tags) ≥ 5` 个专业大类 |
| 冲突密度 | 8% ≤ `len(conflicts) / len(experts)` ≤ 15% |
| 每标段可用专家 | 每个标段至少 3 个无冲突专家 |
| 标书内容质量 | 每份标书 `≥ 2000` 字 |
| 围串标测试数据 | 至少 1 对 SAME_CONTROLLER + 1 对 BID_TOGETHER |
| 回避冲突覆盖 | 4 种回避类型各至少 1 条（EMPLOYED_BY / HOLDS_SHARE / SAME_ORGANIZATION / RELATIVE_EMPLOYED） |
| 评分维度完整性 | 每个标段 4-5 个维度，权重和 = 1.0 ± 0.001 |
| 投标供应商数 | 每标段 ≥ 3 家投标供应商 |
| 专家状态分布 | ACTIVE ≥ 80%，含少量 INACTIVE/BLACKLISTED |
| 供应商状态 | 含至少 1 个 blacklisted 供应商（用于级联测试） |

**3 个演示场景数据**：

```
场景1（正常评审）: 3 供应商无关联，5 专家无冲突 → 正常匹配 + AI评审 + 定标
场景2（冲突回避）: 1 专家持股供应商A + 1 专家同单位冲突 → 被排除 + 备选补入
场景3（围串标）: 2 供应商同一控制人 + 标书高相似度 → HIGH 风险 + PM 废标
```

- **验收**：一键生成脚本可重复执行，质量门禁全部通过，3 个场景数据各自就绪

### P7.2 单元测试（2d）

**覆盖范围**：

| 层级 | 测试对象 | 用例数（最低） | 备注 |
|------|---------|-------------|------|
| `ai/llm/` | DeepSeekClient 重试/断路器 | 6 | VCR.py 录制回放，覆盖 429/502/503/401 + 正常流 + 断路器 OPEN |
| `ai/llm/` | Prompt 模板 | 4 | System Prompt 组装正确性：rubric 注入、`<bid_content>` 隔离、意图标记 |
| `ai/rag/` | SmartDocumentChunker | 5 | 标题感知切分、递归二分、overlap 保留、超长文档截断、空文档 |
| `ai/rag/` | RRF 融合排序 | 3 | 两路融合正确性、k=60 参数、同分去重 |
| `ai/rag/` | BGE-M3 Embedder | 2 | 编码维度=1024、批处理正确性 |
| `ai/graph/` | ConflictDetector | 6 | 4 种冲突各 1 条 + 全部无冲突 + 供应商关联检测 |
| `services/` | ExpertMatchService | 5 | 正常匹配、不足补人、备选耗尽、全部冲突不可用、LLM 降级手动标签 |
| `services/` | ReviewService | 6 | 创建评审、SSE 评分流、追问、维度暂存、提交锁定、幂等保护 |
| `services/` | FraudDetectionService | 5 | 初筛 LOW/MEDIUM、深度检测 LOW/HIGH/CRITICAL、报价异常 |
| `services/` | ConversationService | 4 | 消息追加、上下文组装、摘要触发、摘要失败兜底 |
| `services/` | ExpertDeclarationService | 3 | 全部确认、申报冲突、重复申报拒绝 |
| `services/` | NotificationService | 4 | 发送、分页查询、标记已读、全部已读 |
| `services/` | OutboxService | 4 | 事务内写入、消费 PENDING、重试 FAILED、reconciliation 幂等 |
| `services/` | UserService | 3 | 认证成功、密码错误、token 校验 |
| `services/` | BidDocumentService | 4 | 上传校验、解析 checkpoint 推进、PARSE_FAILED 重试、僵尸扫描 |
| `adapters/` | DataSourceAdapter | 2 | synthetic 模式返回、real 模式切换 |

- **总计**：≥ 62 个单元测试用例
- DeepSeek API → VCR.py 录制回放（减少 API 调用成本）
- 覆盖率 ≥75%（services + ai + adapters 层）
- **验收**：`pytest tests/unit/ -v --cov=app/services --cov=app/ai --cov=app/adapters --cov-report=term-missing` 全部通过，覆盖率达标

### P7.3 集成测试（2d）

**12 个核心 API + 22 条错误路径**：

| # | API | 成功路径 | 错误路径 |
|---|-----|---------|---------|
| 1 | `POST /api/v1/auth/login` | 正确凭据→200+JWT（access_token 30min + refresh_token 7d） | 错误密码→401；缺失 username→422；缺失 password→422 |
| 2 | `POST /api/v1/projects` | 创建项目→201，Neo4j 可见 Project 节点 | 未认证→401；SUM(lot)超预算→422；region 非法值→422 |
| 3 | `POST /api/v1/projects/{id}/lots` | 创建标段→201，Neo4j 可见 CONTAINS_LOT 关系 | 超项目预算→422；project 不存在→404 |
| 4 | `POST /api/v1/lots/{id}/dimensions` | 配置 5 个维度→201，权重和=1.0 | 权重和≠1.0→422；维度名为空→422 |
| 5 | `POST /api/v1/lots/{id}/expert-criteria` | 配置遴选参数→201 | 权重和≠1.0→422；expert_count < min_experts_per_dimension→422 |
| 6 | `POST /api/v1/lots/{id}/bids` | 上传 PDF→201，返回 bid_id，status=SUBMITTED | 超 50MB→413；非 PDF/DOCX magic bytes→415；lot 非 BIDDING→400 |
| 7 | `POST /api/v1/lots/{id}/close-bidding` | 3 家投标→初筛 LOW→lot→PRE_SCREEN | 有效标书<3→ABANDONED；lot 非 BIDDING→400；无标书→400 |
| 8 | `POST /api/v1/lots/{id}/match-experts` | 返回 Top-5 专家+维度分配→落库 assignment | lot 非 UNDER_REVIEW→400；可用专家<3→409；lot=ABANDONED→400 |
| 9 | `POST /api/v1/experts/assignments/{id}/declare` | 全部确认→assignment→IN_PROGRESS | 重复申报→409；非本人 assignment→403 |
| 10 | `POST /api/v1/reviews` | 创建评审工作台→201，返回 review_id | bid 未 FROZEN→400；非本人 assignment→403 |
| 11 | `POST /api/v1/reviews/{id}/score` (SSE) | 8 个事件按序推送（thinking→source→thought→score→citation→done） | stream_token 过期→401；SSE 断流 5s 内自动重连 |
| 12 | `POST /api/v1/reviews/{id}/chat` (SSE) | 追问→追加对话历史→流式回答正确 | stream_token 过期→401 |
| 13 | `POST /api/v1/lots/{id}/complete-review` | 生成报告→lot→EVALUATED | 有维度未完成→400；lot 非 UNDER_REVIEW→400 |
| 14 | `POST /api/v1/projects/{id}/submit-for-award` | 推送定标→project→AWARDED→触发归档 | 有 lot 未 EVALUATED/ABANDONED/DISQUALIFIED→400 |
| 15 | `POST /api/v1/experts/import` | Excel 30 行→全部入库→Neo4j 可见 Expert 节点 | 空文件→400；格式错误→422 |
| 16 | `POST /api/v1/suppliers/import` | Excel 20 行→全部入库→Neo4j 可见 Supplier 节点 | 空文件→400；格式错误→422 |
| 17 | `POST /api/v1/conflicts/import` | CSV 50 行→正确匹配到专家+供应商→写入 Neo4j | CSV 格式错误→422；空文件→400 |
| 18 | `PUT /api/v1/suppliers/{id}/status` | 拉黑供应商→关联评审 SUSPENDED+未封存标书 DISQUALIFIED | 非管理员→403；supplier 不存在→404 |
| 19 | `GET /api/v1/notifications` | 分页查询→返回通知列表+未读计数 | 未认证→401 |
| 20 | `PUT /api/v1/notifications/read-all` | 全部已读→未读数归零 | 未认证→401 |

**跨存储一致性测试**：

| 场景 | 验证方式 |
|------|---------|
| MySQL 写入 Expert → Outbox → Neo4j 同步 | 创建后 5s 内 Neo4j 可见对应 Expert 节点 |
| 企查查 CSV 导入 conflict → Neo4j 关系 | Neo4j Cypher 确认 EMPLOYED_BY/HOLDS_SHARE 关系存在 |
| 标书解析完成 → Milvus chunks | `collection.query(expr='bid_id=="BID-001"')` 返回 chunk 数 > 0 |
| Outbox FAILED → reconciliation 修复 | 手动标记 outbox_event FAILED→1h 内 reconciliation 重放成功 |
| 供应商黑名单 → 级联 SQL | AWARDED 项目关联评审不变，非 AWARDED 项目评审→SUSPENDED |
| 定标归档 → expert_profile 重算 | `review_count` +1，`avg_return_rate` 正确重算 |

**降级路径测试**：

| 场景 | 模拟方式 | 预期行为 |
|------|---------|---------|
| DeepSeek 断路器 OPEN | 连续伪造 5 次 503 响应 | 返回 503 + `"AI 评分暂不可用"`，前端切换人工模式 |
| Milvus 超时（10s） | 注入 11s 延迟 | 降级为关键词路+MySQL 精确查，提示"语义检索暂不可用" |
| Neo4j 超时（8s） | 注入 9s 延迟 | supplier 信息从 MySQL 回退，评审继续 |
| MySQL 超时（5s） | 注入 6s 延迟 | 返回 503 + "核心数据暂不可用" |
| BGE-M3 不可用（prod 模式） | 关闭 BGE-M3 容器 | 返回"AI 推理引擎暂不可用"，评分降级为纯人工 |
| 全部 chunk IP<0.5 | 用无关 query 检索 | 返回"未找到与该问题相关的依据" |
| 断路器半开探测 | 连续成功 1 次 | 断路器自动 CLOSE，AI 功能恢复 |

- pytest + httpx AsyncClient + 指向本地 Docker Compose 环境
- **验收**：20 个 API × 22+ 条错误路径全部通过，跨存储一致性 6 场景通过，降级路径 7 场景通过

### P7.4 E2E 测试（1d）

**3 条核心业务流 + 3 条错误恢复流**：

```
E2E-1 正常评审流（12 步）：
  管理员导入专家+供应商 → PM 创建项目(LOT-01) → 配置 5 个评分维度 →
  配置专家遴选(5人/维度≥2人) → 3 供应商登录上传标书 →
  PM 关闭投标(初筛 LOW) → PM 匹配专家(5人+维度分配) →
  5 专家收到回避申报通知 → 全部确认无冲突 →
  专家1 评审 BID-001 技术方案(AI 建议 24.5→保存) →
  专家1 评审 BID-001 项目团队(AI 建议 15.0→手动改为 16.0→保存) →
  全部维度完成→提交(MANUAL_ADJUSTED) →
  PM 查看汇总(偏差无异常)→结束评审→提交定标 →
  供应商查看结果(中标/未中标)
  
E2E-2 冲突回避流（10 步）：
  管理员导入冲突数据(专家A持股供应商X 5%) → PM 创建项目 →
  3 供应商投标(LOT-01，含供应商X) → PM 匹配专家 →
  专家A 因持股冲突被排除(available=false) → 备选专家B 递补 →
  专家A 不可见该标段，专家B 收到回避申报通知 →
  专家B 确认无冲突 → 正常评审 → 提交 → 定标

E2E-3 围串标检测流（8 步）：
  PM 创建项目 → 3 供应商投标(含同一控制人A和B) →
  PM 关闭投标 → 初筛 MEDIUM(26-50)→PM 待办确认排除嫌疑→放行 →
  专家匹配+回避申报+评审 → PM 结束评审 →
  深度检测触发(CHUNK 交叉相似度 + 关系图谱 SAME_CONTROLLER + 报价集中度) →
  HIGH 风险(51-75)→PM 标记"废标"→对应标书 DISQUALIFIED

E2E-4 AI 不可用降级流（6 步）：
  DeepSeek API 断路 → 专家进入评审工作台 →
  顶部红色 Banner "AI 辅助评分暂不可用" → 左侧标书原文预览 →
  右侧纯人工评分表单(手动输入分数+评语) →
  报价维度公式自动计算(不受 AI 影响) →
  AI 恢复后"切换回 AI 辅助模式"按钮可用 → 专家点击切换

E2E-5 供应商黑名单级联流（5 步）：
  供应商A 正在参与 LOT-01 评审(UNDER_REVIEW) + LOT-02 已定标(AWARDED) →
  管理员拉黑供应商A →
  LOT-01 关联评审→SUSPENDED(含 DRAFT/CONFIRMED/MANUAL_ADJUSTED) →
  LOT-02(AWARDED)关联评审不受影响 →
  PM 收到黑名单通知

E2E-6 专家回避申报→自动补匹配流（6 步）：
  PM 匹配 5 专家 → 专家A 在申报页申报"曾在供应商B 任职" →
  assignment→CONFLICT_DECLARED → 系统自动从备选池补入专家F →
  专家F 收到回避申报通知 → 确认无冲突 → 进入评审
```

- Playwright 脚本，CI 手动触发
- **验收**：6 条 E2E 流全部通过，每条流可重复执行无随机失败

### P7.5 RAG + AI 质量基准测试（0.5d）

**RAG 检索质量**：

| 指标 | 目标 | 测试方法 |
|------|------|---------|
| Recall@5 | ≥ 0.85 | 30 条评审 query × 人工标注 ground truth chunks |
| MRR | ≥ 0.75 | 同上数据集 |
| 拒答触发准确率 | ≥ 95% | 10 条不相关 query 全部触发 IP<0.5 拒答 |
| 维度感知提升 | Recall@5 高于无维度检索 ≥ 10% | 对照组：维度名+标准 vs 仅 query |

**AI 评分准确性**：

| 指标 | 目标 | 测试方法 |
|------|------|---------|
| 评分 MAE | ≤ 2.0（满分 10） | 3 份标书 × 5 维度 = 15 点，3 人交叉标注 ground truth |
| Kendall's tau | ≥ 0.7 | AI 子项排名 vs 人工标注排名 |
| 引用可验证率 | ≥ 80% | 人工逐条审核 citation 原文是否支持所述评分理由 |

**意图识别准确率**：

| 指标 | 目标 | 测试方法 |
|------|------|---------|
| 分类准确率 | ≥ 90% | 30 条标注 prompt 测试集（SCORE_REQUEST 10 / TECH_DETAIL 10 / GENERAL 10），LLM 输出 `[INTENT: xxx]` 与 ground truth 比对 |

- **验收**：所有指标达标，不达标的条目记录 issue

### P7.6 核心链路 SLA 压测（0.5d）

| 路径 | P50 | P95 | 压测条件 |
|------|-----|-----|---------|
| 标书解析（3000 字） | 60s | 180s | 单份标书，docker stats 监控内存 |
| 专家匹配（15 候选） | 1s | 3s | 30 专家/5 投标供应商 |
| 回避申报提交 | 0.5s | 2s | 3 供应商逐项申报 |
| AI 评分首 token | 3s | 8s | DeepSeek API，temperature=0.3，max_tokens=2048 |
| AI 评分完整流 | 12s | 20s | 5 子项评分+理由+citations+SSE 8 事件 |
| 围串标深度检测（3 标书） | 8s | 30s | FAISS 批量 + Neo4j Cypher + 报价计算 |
| 登录→JWT 签发 | 0.2s | 0.5s | bcrypt 校验 |
| Outbox 同步延迟 | 1s | 5s | MySQL INSERT 到 Neo4j 可见 |

- 压测工具：`pytest-benchmark` 或 `k6` 简单脚本
- **验收**：所有 P50/P95 达标，P95 超标路径记录原因和优化方向

### P7.7 一键部署脚本 + README（1d）

- `docker compose up -d` 一键启动全栈 8 容器（MySQL/Neo4j/Milvus/MinIO/Redis/FastAPI App/arq Worker/BGE-M3）
- `.env.example` 模板 + 配置说明（每项附注释）
- 合成数据生成命令：`python scripts/generate_synthetic_data.py --projects 5 --experts 30 --suppliers 20`
- 健康检查：`curl localhost:8000/health/ready` → `{"status":"ok","mysql":"ok","neo4j":"ok","milvus":"ok","redis":"ok"}`
- 演示脚本：`scripts/demo.sh`（正常评审/冲突回避/围串标检测 3 个场景的 curl 命令序列）
- README.md：
  - 项目简介（1 段话 + 架构图）
  - 快速开始（3 步：配 .env → docker compose up → 生成数据）
  - 技术栈一览
  - 演示场景脚本说明
  - 目录结构说明
  - 开发指南（Poetry 虚拟环境 + pre-commit + 本地调试）
- **验收**：新机器拉取代码后 3 步可跑起来，`demo.sh` 可完整执行 3 个演示场景

---

## 总工时统计

| Phase | 内容 | 工时 |
|-------|------|------|
| P0 | 脚手架 + Docker 环境 | 6d |
| P1 | 知识图谱 + CRUD API | 12.5d |
| P2 | 文档解析 + RAG 索引 | 11d |
| P3 | AI 辅助评审核心 | 13d |
| P4 | 专家匹配 + 回避检测 | 9d |
| P5 | 围串标检测 P1 | 9.5d |
| P6 | 前端开发 | 17.5d |
| P7 | 集成测试 + 交付准备 | 8d |
| **合计** | | **~87 人天** |

> 1 人全职约 4 个月。压缩到 P0-P4（核心能力）约 2.5-3 个月。
