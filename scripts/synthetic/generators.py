"""合成数据生成器（P1.1）。

纯数据层，不触碰数据库：所有生成器输出内存 dict，由
generate_synthetic_data.py 落盘为 JSON。种子确定性保证可重复。

演示场景编排（P7.1 三个场景在这里埋入，P1.1 起就位）：
- PRJ-001 场景1 正常评审：LOT-001 三家投标供应商无关联、无冲突
- PRJ-002 场景2 冲突回避：EXP-005 持股 SUP-010 + EXP-006/EXP-007 同单位
- PRJ-003 场景3 围串标：SUP-012/SUP-013 同一控制人 + 高相似标书
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

import bcrypt
import networkx as nx
from faker import Faker

from scripts.synthetic.common import (
    DIMENSION_TEMPLATES,
    EXPERT_ORGANIZATIONS,
    EXPERT_TAGS,
    EXPERT_STATUSES,
    MANDATORY_DIMENSION,
    PROJECT_TYPES,
    REGIONS,
    SUPPLIER_INDUSTRIES,
    SUPPLIER_SCALES,
    USERNAME_PREFIXES,
    gen_id,
)

# 统一初始密码（满足复杂度：≥8 位 + 大小写 + 数字），脚本末尾打印提示
INITIAL_PASSWORD = "Smart@2026"

# ==================== 标书内容生成 ====================
# 章节句子池：按章节主题渲染专业真实内容（P6.4 评审工作台三栏化需要可评审
# 正文：AI 检索、证据引用、追问对话都依赖正文质量）。替代早期 faker 乱词段落。
# 变量由 render_bid_content 上下文提供：
#   {supplier_name} {project_name} {lot_name} {industry}
#   {bid_amount} {duration} {team_size} {quality_cert} {warranty_months}
# 报价/工期/团队明文措辞对齐 app/tasks/document_ingest.py 的结构化提取正则，
# 保证 import/强化脚本可规则提取。
_SENTENCE_POOLS: dict[str, list[str]] = {
    "第一章  公司概况": [
        "{supplier_name}（以下简称“我方”）是一家专业从事{industry}业务的企业，成立于2008年，注册资本5000万元，现有员工120人。",
        "公司已通过{quality_cert}质量管理体系认证，建立了覆盖研发、实施、运维全流程的质量管理体系。",
        "公司主营业务涵盖{industry}相关产品研发、系统集成与运营服务，先后服务教育、政务、金融等行业客户百余家。",
        "我司核心团队稳定，技术骨干平均从业年限超过8年，具备大型信息化项目的总体规划与落地能力。",
        "公司在全国设有多个区域服务中心，能够提供本地化的快速响应与实施交付支持。",
        "近年来公司营收稳步增长，市场信誉良好，连续多年获得行业年度优秀解决方案供应商称号。",
        "公司重视技术创新，每年研发投入占营收比例超过10%，拥有多项软件著作权与专利。",
        "我司在{industry}领域深耕多年，对行业业务痛点与最佳实践有深入理解，能够提供贴合实际的解决方案。",
        "公司成立于2005年，是{industry}领域的高新技术企业，内部治理与风险内控体系完善，经营稳健可持续。",
        "公司员工总数超过百人，其中研发与技术岗位占比超过七成，形成了梯队合理、结构稳定的专业队伍。",
        "公司先后为教育、政务、金融等行业交付多个{industry}信息化项目，积累了跨行业的落地经验。",
        "公司构建售前咨询、项目实施、售后运维三大服务体系，可为采购人提供覆盖全生命周期的支撑。",
        "公司坚持客户至上理念，长期服务国内重点行业客户，客户续约率与满意度持续保持在较高水平。",
        "公司研发过程管理成熟，项目立项、评审、结项均有规范流程，保障交付质量稳定可控。",
        "公司与多所高校及科研机构建立合作，持续跟踪行业前沿技术，保持方案与产品的先进性。",
        "公司持有软件企业认定与多项行业资质，具备承接大型信息化项目的综合工程能力。",
        "公司在重点区域设有分支机构与本地服务团队，可快速响应现场需求并提供长期驻场服务。",
        "公司高度重视信息与数据安全，建立了覆盖组织、制度、技术三个层面的安全管理体系。",
        "公司积极参与行业标准编制与交流，在{industry}领域的专业能力获得主管部门与合作伙伴认可。",
        "公司拥有自主知识产权的核心产品与平台组件，可基于成熟产品快速构建贴合业务的可扩展方案。",
        "公司坚持技术立企，持续加大研发投入，在人工智能与大数据等新技术方向不断布局。",
        "公司构建了覆盖硬件、网络、云服务等环节的合作伙伴生态，保障方案交付资源充足。",
        "公司设有客户成功团队，通过定期回访与价值共创，持续提升客户合作体验与粘性。",
        "公司建立了规范的项目管理流程，严格执行需求、设计、开发、测试、验收各阶段的质量控制。",
    ],
    "第二章  项目理解与需求分析": [
        "通过对{project_name}建设目标与业务现状的深入调研，我方认为本项目核心在于构建统一、开放、可扩展的信息化支撑平台。",
        "本标段{lot_name}重点关注系统的高可用性、易用性与数据安全，对日均并发访问量提出了较高要求。",
        "结合同类项目经验，我方识别出用户在流程协同、数据共享、权限管控三方面的突出诉求，并在方案中逐一响应。",
        "项目需求呈现多角色、多场景、多渠道特征，需在统一架构下兼顾不同业务线的差异化需求。",
        "我方充分理解本项目工期紧、质量要求高的特点，将以成熟的方案与标准化交付流程保障按期高质量完成。",
        "通过实地调研与需求访谈，我方梳理出核心业务流程12项、关键接口38个，全部纳入实施方案。",
        "需求分析阶段我方将组建专项小组，与采购人共同开展需求确认，确保方案与业务实际高度契合。",
        "本项目对系统的可用性要求达到99.9%以上，我方将从架构与运维两个层面予以保障。",
        "本项目涉及与采购人多个存量系统的数据对接，我方将统一规划接口标准与同步策略，保障数据一致。",
        "系统需与上级平台实现互联互通，我方将按统一接口规范完成对接联调与数据共享。",
        "系统需满足高峰时段的并发访问，我方将从容量规划、缓存与数据库优化多维度保障性能指标。",
        "项目涉及的数据须满足网络安全等级保护要求，我方将同步落实安全合规措施并配合测评备案。",
        "系统需支持多部门、多角色分级使用，我方将按职能划分权限模型，兼顾灵活授权与安全管控。",
        "本项目充分考虑国产软硬件生态适配，关键组件兼容主流国产操作系统与数据库，满足自主可控要求。",
        "系统需提供完善的容灾与备份能力，我方将设计同城与异地备份方案，保障极端场景下的业务连续性。",
        "项目交付将配套完整操作培训与使用手册，面向不同角色分层开展培训，确保系统上线即用。",
        "系统建成后需具备清晰的验收标准与可量化指标，我方将在实施初期即与采购人共同明确验收基线。",
        "我方将提供稳定的运维保障体系，明确巡检计划、故障分级与响应时效，确保系统长期稳定运行。",
        "系统采用模块化设计，支持后续功能持续扩展与平滑升级，避免因架构封闭导致的重复建设。",
        "我方将针对高频场景开展专项性能调优，通过压测验证容量水位，提前消除潜在性能瓶颈。",
        "系统设计注重易用性与操作体验，界面交互贴合业务人员习惯，降低培训成本与使用门槛。",
        "本项目对历史数据迁移有明确要求，我方将制定清洗、转换与校验方案，保障迁移质量。",
        "系统需提供完整操作日志与审计追溯能力，关键业务操作留痕可查，满足监管与内控要求。",
        "我方将建立与采购人常态化的沟通协作机制，需求变更与进度风险及时同步，保障各方信息对称。",
    ],
    "第三章  系统架构方案": [
        "系统采用微服务架构，基于 Spring Cloud 全家桶实现服务注册、配置中心、网关统一与链路追踪，支持水平扩展。",
        "整体分为接入层、应用层、服务层与数据层，各层职责边界清晰，通过消息队列实现业务解耦与异步削峰。",
        "前端采用前后端分离架构，Vue 技术栈组件化开发，适配 PC 端与移动端，支持主流浏览器。",
        "数据层采用 MySQL 主从复制与 Redis 缓存集群，结合读写分离提升高并发场景下的查询性能。",
        "系统提供标准 RESTful API 与开放接口网关，支持与采购人现有系统平滑对接与数据交换。",
        "架构设计预留容器化部署与弹性伸缩能力，支持基于 Kubernetes 的自动扩缩容。",
        "关键业务模块采用双活部署，故障时可秒级切换，保障业务连续性。",
        "系统建设遵循高内聚低耦合原则，模块间通过接口交互，便于后续迭代升级与功能扩展。",
        "系统引入统一API网关，集中实现鉴权、限流、路由与灰度发布，对外提供一致、安全的服务入口。",
        "服务通过注册中心实现自动注册与发现，配合配置中心集中管理环境配置，支持动态调整与热更新。",
        "对跨服务的强一致业务采用分布式事务方案，配合最终一致性的消息补偿机制，保障数据可靠。",
        "系统选用成熟消息队列实现异步解耦与削峰填谷，关键消息支持持久化与重试，保障不丢失。",
        "热点数据采用多级缓存策略，合理设置缓存粒度与失效机制，显著降低数据库压力与响应时延。",
        "服务间调用引入限流、熔断与降级机制，保障单点故障不扩散，整体可用性持续稳定。",
        "系统部署全链路追踪与日志聚合能力，故障可快速定位到具体服务与调用链，提升排障效率。",
        "系统支持容器化部署与标准化编排，配合CI/CD流水线实现自动化构建、测试与发布，缩短交付周期。",
        "系统提供完善监控告警体系，覆盖基础设施、中间件与应用层指标，异常秒级感知、分钟级定位。",
        "系统具备容量规划与弹性伸缩能力，可根据业务水位自动调整资源，兼顾成本与性能平衡。",
        "核心数据采用主备与集群架构，配合定期演练的灾备切换流程，保障极端故障下的快速恢复。",
        "系统技术选型兼顾成熟度与开放性，支持国产化数据库与中间件替换，降低技术绑定风险。",
        "系统内置标准化接口规范与数据字典，保障各子系统间协作有序，便于第三方系统集成接入。",
        "系统设计充分考虑安全架构，在网关、应用、数据各层设置纵深防御，防范外部攻击与越权访问。",
        "系统支持多环境隔离与灰度发布，新版本可小流量验证后逐步放量，降低变更风险。",
        "系统采用分层架构与依赖倒置原则，核心业务逻辑与底层实现解耦，提升可测试性与可维护性。",
    ],
    "第四章  安全方案": [
        "系统安全设计参照等保三级要求，网络层面划分安全域，部署防火墙、入侵检测与日志审计设备。",
        "应用层面实现基于 RBAC 的细粒度权限控制、登录双因子认证与全量操作审计日志。",
        "敏感数据采用加密存储，关键业务数据传输启用 TLS 加密，防止数据泄露与篡改。",
        "平台提供统一身份认证与单点登录，支持与采购人现有认证体系对接。",
        "建立安全应急响应机制，制定数据备份与灾难恢复方案，每日增量备份、每周全量备份。",
        "系统定期开展安全漏洞扫描与渗透测试，上线前完成安全加固与基线核查。",
        "运维层面实行最小权限原则，账号权限按角色最小化分配，操作留痕可追溯。",
        "数据备份异地保存，关键数据支持时间点恢复，最大数据丢失量不超过24小时。",
        "系统对个人敏感信息实施脱敏处理，分级分类管控数据访问权限，防止敏感数据越权使用。",
        "系统采用密钥管理服务统一管理加密密钥，密钥轮换与销毁流程规范，保障加密体系安全可控。",
        "网络边界部署Web应用防火墙与入侵防御设备，实时拦截恶意流量与已知攻击特征。",
        "系统具备防DDoS攻击能力，通过流量清洗与限速策略保障服务在攻击场景下的可用性。",
        "系统建立常态化漏洞管理流程，定期扫描评估并修复漏洞，高危漏洞限时闭环处置。",
        "系统对账号、角色、菜单与数据权限实行统一管理，权限申请、审批、变更流程全程可溯。",
        "系统提供完整安全审计功能，关键操作全程留痕，审计日志独立存储、防篡改、可追溯。",
        "系统依据商用密码应用安全性评估要求落实密码技术应用，配合完成密评与等保测评工作。",
        "系统制定并演练安全应急响应预案，明确事件分级与处置流程，保障事件快速处置、影响可控。",
        "系统采用零信任安全理念，对访问主体持续验证、最小授权，降低内部横向移动风险。",
        "系统对数据传输与存储采取分层加密，密钥与数据分离管理，兼顾安全性与性能开销。",
        "系统设置安全基线并纳入上线前检查，定期开展安全巡检与配置核查，防止配置漂移。",
        "系统对供应链软件实施来源核验与安全评估，降低第三方组件引入的供应链安全风险。",
        "系统强化账号生命周期管理，离职与转岗账号及时回收，弱口令与默认口令全面治理。",
        "系统提供隐私保护机制，遵循最小必要原则收集与使用数据，保障个人信息权益。",
        "系统严格落实等保三级控制项，从技术与管理双维度满足合规要求，保障测评顺利通过。",
    ],
    "第五章  实施计划": [
        "项目计划工期{duration}个日历天，分为需求确认、详细设计、开发实现、测试部署与试运行五个阶段。",
        "实施过程采用敏捷迭代模式，每两周一个迭代周期，持续集成持续交付保障进度可控。",
        "项目启动即成立联合实施团队，制定详细里程碑计划，每周输出进度周报并召开例会。",
        "开发阶段每完成一个迭代即组织评审与验收，确保阶段性成果符合预期。",
        "测试阶段覆盖功能、性能、安全与兼容性测试，上线前完成压力测试并出具报告。",
        "试运行期间安排专人驻场保障，收集问题清单并闭环整改，具备条件后正式验收。",
        "针对关键里程碑设置双周滚动检查，出现偏差及时纠偏，确保按期交付。",
        "实施文档全程同步维护，交付物包含实施方案、设计文档、测试报告与操作手册。",
        "项目启动即召开启动会，明确各方职责与沟通机制，确认项目目标、范围与验收标准。",
        "需求阶段结束后实行需求冻结管理，变更统一走评审流程，控制范围蔓延对进度的影响。",
        "设计阶段组织设计方案评审，关键架构与接口设计经专家把关后方可进入开发。",
        "开发过程推行代码走查与单元测试，建立质量门禁，未达标的代码不得合入主线。",
        "系统完成开发后开展集成测试与用户验收测试，问题闭环整改后进入试运行阶段。",
        "上线切换安排在业务低峰期执行，制定详尽切换清单与回退预案，全程保障切换平稳无感。",
        "系统上线采用灰度发布与回退预案，新版本异常时可快速回滚，保障业务连续性。",
        "项目制定风险管理计划，对进度、质量、人员、依赖等风险提前识别并跟踪化解。",
        "项目明确资源保障计划，关键岗位双人备份，保障实施过程中的人员稳定性。",
        "项目建立周报、例会与里程碑评审机制，进度偏差及时预警并采取纠偏措施。",
        "项目实行变更管理制度，范围、计划、需求的变更统一评估影响并经审批后执行。",
        "项目制定详细测试计划，覆盖功能、性能、安全、兼容性等维度，保障交付质量。",
        "项目交付包含完整竣工文档，涵盖设计、测试、部署、运维、培训等全部资料。",
        "项目实施采用分阶段交付策略，每个阶段产出可验证成果，降低整体交付风险。",
        "项目设定明确里程碑与交付物清单，节点成果经采购人确认后推进下一阶段。",
        "项目上线前进行完整演练，含数据迁移演练、切换演练与回退演练，确保上线过程平稳。",
    ],
    "第六章  项目团队配置": [
        "项目团队共{team_size}人，其中项目经理1名、系统架构师2名、开发工程师若干、测试与实施人员配备齐全。",
        "项目经理具备PMP认证与多个大型项目实施经验，专职负责本项目组织与协调。",
        "核心开发人员均具备3年以上{industry}领域项目经验，熟悉主流技术栈与开发规范。",
        "团队按模块划分小组，设立前端、后端、测试、运维四个专项小组，职责清晰、协同高效。",
        "项目组实施人员具备丰富的现场实施经验，能够高效完成部署、培训与上线支持。",
        "团队内部建立知识共享与代码评审机制，保障交付质量与人员稳定性。",
        "针对关键岗位设置AB角机制，核心人员变动时能够快速补位，降低交付风险。",
        "我司承诺项目核心成员在实施周期内不随意更换，如确需调整需提前报采购人同意。",
        "项目配置专职项目总监负责总体协调与资源保障，对项目成功交付承担最终责任。",
        "项目配备业务顾问与需求分析师，深入梳理业务规则，保障需求理解的准确与完整。",
        "项目配置数据库管理员负责数据模型设计与性能调优，保障数据层稳定高效。",
        "项目配置专职安全工程师，负责安全设计评审、加固与合规检查，保障系统安全可靠。",
        "项目配置独立测试负责人，制定测试策略并组织测试执行，把控交付质量关口。",
        "项目配备UI与UX设计人员，依据业务场景设计友好交互界面，提升用户体验。",
        "项目实施期间安排核心人员驻场，现场快速响应需求变更与问题处置。",
        "项目对关键岗位设置备份人员，重要节点AB角色互备，防范人员波动风险。",
        "项目建立人员绩效考核与激励制度，保障团队投入度与交付质量。",
        "项目建立岗位技能矩阵，识别能力差距并制定针对性培训计划，持续提升团队能力。",
        "项目明确内外部沟通机制，定期例会同步进展，问题快速上升与闭环。",
        "项目注重知识管理，沉淀需求、设计、配置、问题处理等知识库，降低人员依赖。",
        "项目统一文档规范与模板，交付文档标准一致，便于采购人查阅与验收。",
        "项目对团队成员开展项目背景与行业知识培训，保障方案设计与实施贴合业务。",
        "项目设置专职配置管理员，统一管理版本、变更与配置基线，保障交付物可追溯。",
        "项目团队统一执行研发与交付规范，代码与文档双评审，保障交付成果质量一致可控。",
    ],
    "第七章  质量保障与售后服务": [
        "我司提供质保期{warranty_months}个月，质保期内免费提供故障修复、版本升级与定期巡检服务。",
        "建立三级售后响应机制：7×24小时热线支持、2小时内远程响应、紧急故障4小时内到场处理。",
        "质保期内每季度进行一次系统巡检，输出巡检报告并跟进问题整改。",
        "提供详细的运维手册与培训计划，面向采购人管理人员、操作人员分级开展培训。",
        "系统故障按严重程度分级响应，重大故障优先处理并事后出具分析报告。",
        "质保期满后可提供优惠的续保服务，保障系统长期稳定运行。",
        "售后团队对客户反馈建立工单闭环管理，响应及时率与解决率纳入内部考核。",
        "我司承诺服务期内免费提供系统优化建议与安全加固补丁更新。",
        "我司提供明确的服务级别协议，对响应时间、解决时间等指标作出量化承诺并接受监督考核。",
        "售后支持覆盖电话、远程、现场多种方式，按故障等级提供差异化响应保障。",
        "我司为关键硬件与系统提供备件保障与备机方案，降低故障导致的业务中断时长。",
        "我司建设完善的服务知识库，常见问题与处理方案沉淀共享，提升服务响应效率。",
        "我司定期开展系统健康检查，提前发现隐患并出具报告，做到防患于未然。",
        "我司持续提供系统优化建议，结合运行数据提出性能与体验改进方案供采购人决策。",
        "我司规范版本管理流程，版本发布前充分测试并留存记录，保障升级安全可回退。",
        "我司及时提供安全补丁与漏洞修复，重大漏洞优先处置并主动向采购人通报。",
        "我司定期组织应急演练，验证应急预案的可执行性，提升突发故障的处置能力。",
        "我司定期开展客户满意度调查，针对反馈制定改进措施并跟踪落实效果。",
        "我司提供定期回访服务，主动了解系统运行情况与业务变化，前瞻性提供服务支持。",
        "我司提供标准的服务交付规范，每次服务均记录工单并输出处理报告，服务过程全程可查。",
        "我司提供系统停运维护的窗口协调服务，重大维护提前通知并安排错峰实施。",
        "我司支持后续数据迁移与系统扩容服务，保障业务发展过程中的系统演进需求。",
        "我司按约定周期输出系统运行报告，客观呈现运行指标与改进成效，便于采购人掌握系统态势。",
        "我司设立服务热线与专属服务经理，形成统一服务入口，保障沟通渠道畅通高效。",
    ],
    "第八章  商务承诺": [
        "我方承诺投标总报价：{bid_amount:,}元，包含全部软硬件、实施、培训与质保费用，无任何隐形收费。",
        "本报价基于成熟方案与标准化交付流程测算，价格公允且具备竞争力。",
        "我方承诺严格按照合同约定交付内容与工期履约，如违约愿承担相应责任。",
        "合同签订后我方将缴纳履约保证金，保障项目如期保质完成。",
        "我方承诺项目成果的知识产权与数据归属符合采购人要求。",
        "在同等条件下，我方将优先响应采购人后续的扩展需求与新增功能开发。",
        "我方愿意接受采购人与监理单位对项目实施过程的全过程监督。",
        "本投标文件所提供信息真实有效，如有不实我方愿承担法律后果。",
        "我方接受合同约定的付款节点与付款方式，并按约定提供合规发票与结算资料。",
        "我方同意按合同约定缴纳质保金，质保期满且无质量问题后申请无息退还。",
        "我方提供合规的发票与财务资料，配合采购人完成项目决算与资产入账等工作。",
        "我方将按合同约定的免责条款执行，因不可抗力等法定情形导致的延误按约定处理。",
        "我方承诺不将合同主要义务转包或违规分包，保障项目实施的连续性。",
        "我方对接触到的采购人信息严格保密，签订保密协议，未经许可不向第三方披露。",
        "我方遵守廉洁从业要求，配合采购人及监管部门开展廉洁监督与审计。",
        "合同争议解决方式、适用法律及管辖约定，我方同意按合同条款执行。",
        "我方在合同生效后按约定时限启动项目，并完成相关备案与进场手续。",
        "我方按合同约定组织验收，提供验收所需全部资料并配合验收程序。",
        "我方按合同列明交付清单交付全部成果物，保证交付内容完整无遗漏。",
        "我方明确服务周期与费用构成，报价清单明细完整，无隐藏收费项目。",
        "我方接受项目全过程审计与监理，配合提供相关材料与解释说明。",
        "我方承诺报价在合同期内保持有效，除合同约定的调整情形外不作变更。",
        "我方将按照合同约定履行开票义务，确保发票金额、内容与合同一致。",
        "我方愿与采购人就合同未尽事宜友好协商，形成补充协议后共同遵守。",
    ],
}


# 场景3 围串标共享段落种子：SUP-012/SUP-013 用同一 seed 生成段落序列一致的标书
# （模块级供 enrich_synthetic_bids.py 重生成时保持一致，避免强化后围串标特征丢失）
SCENE3_SHARED_SEED = 20260701


def render_bid_content(
    fake: Faker,
    supplier_name: str,
    project_name: str,
    lot_name: str,
    industry: str,
    *,
    bid_amount: int | None = None,
    duration: int | None = None,
    team_size: int | None = None,
    quality_cert: str | None = None,
    warranty_months: int | None = None,
    shared_seed: int | None = None,
    bid_seed: int | None = None,
) -> str:
    """渲染一份标书全文（章节模板化专业内容，≥2000 字）。

    结构化数值（报价/工期/团队/资质/质保）由调用方传入并嵌入正文，保证
    bid_document 结构化字段与正文一致（AI 评分/证据引用/规则提取同源）。
    None 时从 para_fake 随机兜底（仅兼容旧调用，主链路请显式传入）。

    shared_seed: 场景3 高相似度用。传入时用独立 faker 实例 + 固定种子选择
    句子序列，两份标书段落完全一致、仅结构化数值/公司名/项目名不同——
    既满足高相似度又不逐字相同（比母版整体复制更接近真实围标场景）。

    bid_seed: 每标书专属 seed。不同标书从句子池取到不同子集，避免共享句子池
    导致跨标书同句重复、相似度虚高（P5.2 回归根因：BGE-M3 对同句相似度 ~0.99
    远超 0.85 阈值，需让正常标书正文真正差异化）。
    """
    para_fake = fake
    if shared_seed is not None:
        para_fake = Faker("zh_CN")
        para_fake.seed_instance(shared_seed)
    elif bid_seed is not None:
        para_fake = Faker("zh_CN")
        para_fake.seed_instance(bid_seed)

    ctx = {
        "supplier_name": supplier_name,
        "project_name": project_name,
        "lot_name": lot_name,
        "industry": industry,
        "bid_amount": bid_amount if bid_amount is not None else para_fake.random_int(1_000_000, 9_000_000),
        "duration": duration if duration is not None else para_fake.random_int(90, 300),
        "team_size": team_size if team_size is not None else para_fake.random_int(5, 30),
        "quality_cert": quality_cert or para_fake.random_element(["ISO9001", "ISO27001", "CMMI3"]),
        "warranty_months": warranty_months if warranty_months is not None else para_fake.random_element([24, 36, 48]),
    }

    parts: list[str] = []
    for title, pool in _SENTENCE_POOLS.items():
        parts.append(f"{title}\n")
        # 每章 2-3 段、每段 2-3 句；无放回取样（unique=True）避免同段重复句子，
        # 专属 seed 保证不同标书取到不同句子子集
        for _ in range(para_fake.random_int(2, 3)):
            k = para_fake.random_int(2, 3)
            chosen = para_fake.random_elements(elements=pool, length=k, unique=True)
            parts.append("".join(s.format(**ctx) for s in chosen) + "\n\n")
    text = "".join(parts)
    # 字数兜底：从全池取（专属 seed 下按序确定），不固定第三章池避免跨标书同句重复
    all_sentences = [s for pool in _SENTENCE_POOLS.values() for s in pool]
    while len(text.strip()) < 2000:
        text += f"\n{para_fake.random_element(all_sentences).format(**ctx)}\n"
    return text


# ==================== 生成上下文 ====================
@dataclass
class GenResult:
    """全部生成数据的聚合容器，import/validate 脚本消费。"""

    seed: int
    users: list[dict] = field(default_factory=list)
    experts: list[dict] = field(default_factory=list)
    suppliers: list[dict] = field(default_factory=list)
    projects: list[dict] = field(default_factory=list)
    lots: list[dict] = field(default_factory=list)
    dimensions: list[dict] = field(default_factory=list)  # 含 criteria 子项
    bids: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)  # 专家回避关系
    supplier_links: list[dict] = field(default_factory=list)  # 供应商关联
    bid_contents: dict[str, str] = field(default_factory=dict)  # {bid_id: 全文}
    bid_content_dir: str = ""


# ==================== 实体生成器 ====================
def generate_experts(rng: random.Random, fake: Faker, n: int) -> tuple[list[dict], list[dict]]:
    """生成 n 个专家 + 对应 users 账号。

    返回 (experts, users)。专家专业标签 1-3 个从受控词表选取。
    状态分布：ACTIVE ≥80%，含少量 INACTIVE / BLACKLISTED。
    """
    experts: list[dict] = []
    users: list[dict] = []
    tags_pool = EXPERT_TAGS.copy()

    for i in range(1, n + 1):
        expert_id = gen_id("expert", i)
        user_id = gen_id("user", 100 + i)  # U-101 起为专家账号
        tag_count = rng.randint(1, 3)
        # 打乱后取前 tag_count 个，保证标签来自受控词表
        chosen = rng.sample(EXPERT_TAGS, tag_count)

        # 状态：前 80% 保证 ACTIVE，剩余 20% 确定性分配（保证各至少 1 个
        # INACTIVE + BLACKLISTED，满足门禁"含少量 INACTIVE/BLACKLISTED"），
        # 余量随机。若用纯 rng.choice，随机可能抽不到 BLACKLISTED（门禁 FAIL）。
        if i <= int(n * 0.8):
            status = "ACTIVE"
        else:
            remainder_idx = i - int(n * 0.8)
            if remainder_idx == 1:
                status = "INACTIVE"
            elif remainder_idx == 2:
                status = "BLACKLISTED"
            else:
                status = rng.choice(["ACTIVE", "INACTIVE", "BLACKLISTED"])

        id_num = fake.ssn()  # 身份证号，导入时加密/哈希
        experts.append(
            {
                "expert_id": expert_id,
                "user_id": user_id,
                "name": fake.name(),
                "organization": rng.choice(EXPERT_ORGANIZATIONS),
                "region": rng.choice(REGIONS),
                "experience": rng.randint(5, 35),
                "email": f"{expert_id.lower()}@example.com",
                "phone": fake.phone_number(),
                "id_number": id_num,
                "status": status,
                "specializations": chosen,
            }
        )
        users.append(
            {
                "user_id": user_id,
                "username": f"{USERNAME_PREFIXES['REVIEW_EXPERT']}_{i:02d}",
                "password_hash": bcrypt.hashpw(INITIAL_PASSWORD.encode(), bcrypt.gensalt()).decode(),
                "role": "REVIEW_EXPERT",
                "display_name": experts[-1]["name"],
                "email": experts[-1]["email"],
                "phone": experts[-1]["phone"],
                "is_active": status != "BLACKLISTED",
                # 自查 #6：合成演示账号免首登强改（验收/演示脚本零影响）
                "must_change_password": False,
            }
        )
    return experts, users


def generate_suppliers(rng: random.Random, fake: Faker, n: int) -> tuple[list[dict], list[dict]]:
    """生成 n 个供应商 + 对应 users 账号（供应商登录）。

    至少 1 个 blacklisted（P7.1 门禁：供黑名单级联测试）。
    供应商 name 用「地域 + 行业短词 + 组织形式」保证企业名风格统一。
    """
    suppliers: list[dict] = []
    users: list[dict] = []
    industry_short = {
        "软件和信息技术服务业": ["智联", "数通", "创想"],
        "通信设备制造业": ["华信", "通达", "立讯"],
        "安防设备制造业": ["安盾", "泰安", "卫士"],
        "信息系统集成服务": ["合创", "中软", "科讯"],
        "大数据服务": ["云帆", "数聚", "慧算"],
        "云计算服务": ["云启", "浪潮", "天翼"],
    }

    for i in range(1, n + 1):
        supplier_id = gen_id("supplier", i)
        user_id = gen_id("user", 200 + i)  # U-201 起为供应商账号
        industry = rng.choice(SUPPLIER_INDUSTRIES)
        region_prefix = rng.choice(["华东", "华南", "华北", "华中", "西南", "西北", "东北"])
        name_part = rng.choice(industry_short[industry])
        scale = rng.choice(SUPPLIER_SCALES)

        # 至少 1 个 blacklisted：编号 5 强制
        blacklisted = i == 5

        suppliers.append(
            {
                "supplier_id": supplier_id,
                "name": f"{region_prefix}{name_part}{industry.split('服务')[0][:2]}有限公司",
                # 统一社会信用代码：18 位数字（仿真，不校验校验位）
                "uniform_credit_code": str(rng.randint(10**17, 10**18 - 1)),
                "legal_person": fake.name(),
                "industry": industry,
                "scale": scale,
                "blacklisted": blacklisted,
                "status": "INACTIVE" if blacklisted else "ACTIVE",
            }
        )
        users.append(
            {
                "user_id": user_id,
                "username": f"{USERNAME_PREFIXES['SUPPLIER']}_{i:02d}",
                "password_hash": bcrypt.hashpw(INITIAL_PASSWORD.encode(), bcrypt.gensalt()).decode(),
                "role": "SUPPLIER",
                "display_name": suppliers[-1]["name"],
                "email": f"{supplier_id.lower()}@example.com",
                "phone": fake.phone_number(),
                "is_active": not blacklisted,
                # 自查 #6：合成演示账号免首登强改（验收/演示脚本零影响）
                "must_change_password": False,
            }
        )
    return suppliers, users


def generate_projects(
    rng: random.Random, fake: Faker, n_projects: int
) -> tuple[list[dict], list[dict]]:
    """生成 n 个项目 + 每个项目 3 个标段。

    返回 (projects, lots)。每项目 3 标段预算之和 ≤ 项目预算。
    前 3 个项目分别编排三个演示场景（scene 字段仅用于数据组织，非数据库字段）。
    """
    projects: list[dict] = []
    lots: list[dict] = []

    scene_templates = [
        {"name": "某市教育局智慧校园平台建设项目", "type": "SERVICE", "scene": "NORMAL"},
        {"name": "某市政务云平台安全加固项目", "type": "SERVICE", "scene": "CONFLICT"},
        {"name": "某省电子政务外网系统集成项目", "type": "ENGINEERING", "scene": "FRAUD"},
    ]

    lot_name_pool = [
        "核心业务软件标段", "平台基础设施标段", "安全防护标段",
        "数据治理标段", "运维服务标段", "移动应用标段",
    ]

    for p in range(1, n_projects + 1):
        project_id = gen_id("project", p)
        if p <= len(scene_templates):
            tmpl = scene_templates[p - 1]
            name = tmpl["name"]
            ptype = tmpl["type"]
            scene = tmpl["scene"]
        else:
            name = f"{rng.choice(REGIONS)}{fake.city()}信息化项目{p}"
            ptype = rng.choice(PROJECT_TYPES)
            scene = "RANDOM"

        # 预算：项目 1000-5000 万，3 标段平分（留余量）
        budget = rng.randint(10_000_000, 50_000_000)
        lot_budget_total = int(budget * 0.95)
        b1 = rng.randint(lot_budget_total // 4, lot_budget_total // 2)
        b2 = rng.randint((lot_budget_total - b1) // 2, (lot_budget_total - b1) - 100_000)
        b3 = lot_budget_total - b1 - b2

        projects.append(
            {
                "project_id": project_id,
                "project_code": f"PRJ2026{p:03d}",
                "name": name,
                "type": ptype,
                "region": rng.choice(REGIONS),
                "budget": budget,
                "status": "BIDDING",
                "scene": scene,
            }
        )

        for s in range(1, 4):
            lot_id = gen_id("lot", (p - 1) * 3 + s)
            lots.append(
                {
                    "lot_id": lot_id,
                    "project_id": project_id,
                    "lot_code": f"LOT-{p:02d}-{s}",
                    "name": rng.choice(lot_name_pool),
                    "budget": [b1, b2, b3][s - 1],
                    "status": "BIDDING",
                    # 专家遴选参数（solution.md 默认值）
                    "expert_criteria": {
                        "expert_count": 5,
                        "min_experts_per_dimension": 2,
                        "weight_specialization": 0.40,
                        "weight_experience": 0.30,
                        "weight_review_quality": 0.20,
                        "weight_region": 0.10,
                        "min_experience": 5,
                    },
                }
            )
    return projects, lots


def _normalize_weights(raw_weights: list[float]) -> list[float]:
    """将权重列表按比例归一化到和=1.0，round 到 3 位小数（Numeric(4,3)）。

    归一化 round 后最后一项兜底修正，保证 sum == 1.0（P1.3 API 同款校验）。
    """
    total = sum(raw_weights)
    normalized = [round(w / total, 3) for w in raw_weights]
    # 最后一项兜底：1 - 前几项之和，消除 round 累积误差
    normalized[-1] = round(1.0 - sum(normalized[:-1]), 3)
    assert abs(sum(normalized) - 1.0) <= 0.001, f"归一化后权重和错误: {sum(normalized)}"
    return normalized


def generate_dimensions(rng: random.Random, lots: list[dict]) -> list[dict]:
    """为每个标段生成 4-5 个评分维度（必含报价维度），权重和 = 1.0。

    维度从 DIMENSION_TEMPLATES 选取：报价必选，其余随机抽 3-4 个。
    权重按所选维度比例归一化（固定模板权重在少选维度时和不为 1.0）。
    """
    dimensions: list[dict] = []
    non_mandatory = [t for t in DIMENSION_TEMPLATES if t["name"] != MANDATORY_DIMENSION]
    mandatory = next(t for t in DIMENSION_TEMPLATES if t["name"] == MANDATORY_DIMENSION)

    for lot in lots:
        count = rng.randint(3, 4)  # 报价 + 3~4 个 = 4~5 个维度
        chosen = [mandatory] + rng.sample(non_mandatory, count)
        rng.shuffle(chosen)

        # 维度数 4-5、权重和=1.0 是质量门禁，先归一化再落盘
        weights = _normalize_weights([t["weight"] for t in chosen])

        for idx, (tmpl, weight) in enumerate(zip(chosen, weights), start=1):
            # 维度/子项 ID 带 lot 前缀便于跨存储追溯（Neo4j/MySQL 一致）
            dim_id = f"DIM-{lot['lot_id']}-{idx}"
            criteria = []
            for cidx, (cname, cmax, rubric) in enumerate(tmpl["criteria"], start=1):
                criteria.append(
                    {
                        "criterion_id": f"CRI-{lot['lot_id']}-{idx}-{cidx}",
                        "name": cname,
                        "description": f"{tmpl['name']}子项：{cname}",
                        "scoring_rubric": rubric,
                        "max_score": cmax,
                        "sort_order": cidx,
                    }
                )
            dimensions.append(
                {
                    "dimension_id": dim_id,
                    "lot_id": lot["lot_id"],
                    "name": tmpl["name"],
                    "max_score": tmpl["max_score"],
                    "weight": weight,
                    "sort_order": idx,
                    "criteria": criteria,
                }
            )
    return dimensions


def build_conflict_network(
    rng: random.Random,
    fake: Faker,
    experts: list[dict],
    suppliers: list[dict],
) -> list[dict]:
    """构建专家回避冲突网络（networkx 控制密度 8%-15%）。

    覆盖 4 种回避类型各 ≥1 条 + 埋入场景2 冲突：
    - HOLDS_SHARE:       EXP-005 → SUP-010（场景2 持股）
    - EMPLOYED_BY:       EXP-005 → SUP-010（同一人任职，压缩冲突专家数）
    - SAME_ORGANIZATION: EXP-006 ↔ EXP-007（场景2 同单位）
    - RELATIVE_EMPLOYED: EXP-008 → SUP-012（配偶任职）

    冲突专家 = EXP-005..008 共 4 人，关系条数 4/30 = 13.3%（默认 30 专家达标）。
    密度自适应：专家数 n 变化时，networkx 按 8%-15% 目标补边。
    自检：专家→冲突供应商最短路径 ≤1（solution.md 1.5 回避判定口径）。
    """
    conflicts: list[dict] = [
        {
            "relation_type": "HOLDS_SHARE",
            "expert_id": "EXP-005",
            "supplier_id": "SUP-010",
            "ratio": 0.05,
        },
        {
            "relation_type": "EMPLOYED_BY",
            "expert_id": "EXP-005",
            "supplier_id": "SUP-010",
            "role": "技术总监",
            "start_date": "2018-01-01",
            "end_date": None,  # 当前任职 → 回避
        },
        {
            "relation_type": "SAME_ORGANIZATION",
            "expert_a_id": "EXP-006",
            "expert_b_id": "EXP-007",
            "period": "2015-2020",
        },
        {
            "relation_type": "RELATIVE_EMPLOYED",
            "expert_id": "EXP-008",
            "supplier_id": "SUP-012",
            "relation_type_detail": "配偶",
            "relative_name": fake.name(),
        },
    ]

    # networkx 建图（节点 = 专家/供应商，边 = 冲突关系）
    G = nx.Graph()
    for e in experts:
        G.add_node(e["expert_id"])
    for s in suppliers:
        G.add_node(s["supplier_id"])
    for c in conflicts:
        if c["relation_type"] == "SAME_ORGANIZATION":
            G.add_edge(c["expert_a_id"], c["expert_b_id"], type=c["relation_type"])
        else:
            G.add_edge(c["expert_id"], c["supplier_id"], type=c["relation_type"])

    # 密度自适应补边：目标落在 8%-15%（关系条数/专家数）
    n = len(experts)
    min_total = max(len(conflicts), math.ceil(n * 0.08))
    max_total = int(n * 0.15)
    if max_total < min_total:
        # 专家数过少（<27）时显式冲突已超 15% 上限，门禁必然失败，属数据规模与门禁的内在矛盾
        print(f"[warn] 专家数 {n} 时 4 条显式冲突密度 {len(conflicts)/n:.1%} 超 15%（默认 30 专家达标）")
    target = rng.randint(min_total, max_total)
    occupied_experts = {c.get("expert_a_id") or c.get("expert_id") for c in conflicts}
    occupied_experts.update(c.get("expert_b_id") for c in conflicts if "expert_b_id" in c)

    while len(conflicts) < target:
        etype = rng.choice(["EMPLOYED_BY", "RELATIVE_EMPLOYED"])
        candidates = [e["expert_id"] for e in experts if e["expert_id"] not in occupied_experts]
        if not candidates:
            break
        exp_id = rng.choice(candidates)
        sup_id = rng.choice([s["supplier_id"] for s in suppliers])
        occupied_experts.add(exp_id)
        if etype == "EMPLOYED_BY":
            conflicts.append(
                {
                    "relation_type": "EMPLOYED_BY",
                    "expert_id": exp_id,
                    "supplier_id": sup_id,
                    "role": rng.choice(["技术总监", "项目经理", "研发工程师"]),
                    "start_date": f"20{rng.randint(10, 20)}-01-01",
                    "end_date": None,
                }
            )
        else:
            conflicts.append(
                {
                    "relation_type": "RELATIVE_EMPLOYED",
                    "expert_id": exp_id,
                    "supplier_id": sup_id,
                    "relation_type_detail": rng.choice(["配偶", "直系亲属"]),
                    "relative_name": fake.name(),
                }
            )
        G.add_edge(exp_id, sup_id, type=etype)

    # 自检 1：4 种回避类型覆盖
    covered = {c["relation_type"] for c in conflicts}
    assert {"EMPLOYED_BY", "HOLDS_SHARE", "SAME_ORGANIZATION", "RELATIVE_EMPLOYED"} <= covered, covered
    # 自检 2：每条专家→供应商冲突在图中最短路径 ≤1（回避触发口径）
    for c in conflicts:
        if c["relation_type"] == "SAME_ORGANIZATION":
            assert nx.shortest_path_length(G, c["expert_a_id"], c["expert_b_id"]) <= 1
        else:
            assert nx.shortest_path_length(G, c["expert_id"], c["supplier_id"]) <= 1
    return conflicts


def build_supplier_links(
    rng: random.Random,
    suppliers: list[dict],
    projects: list[dict],
) -> list[dict]:
    """构建供应商关联关系（围串标信号，不影响专家回避）。

    保证：≥1 对 SAME_CONTROLLER（场景3）+ ≥1 对 BID_TOGETHER + 若干 AFFILIATE_OF。
    """
    links: list[dict] = []

    # 场景3：SUP-012 / SUP-013 同一实际控制人
    links.append(
        {"relation_type": "SAME_CONTROLLER", "supplier_a_id": "SUP-012", "supplier_b_id": "SUP-013"}
    )
    # BID_TOGETHER：同一控制人历史共同投标（与 SAME_CONTROLLER 配对，增强围标信号）
    links.append(
        {
            "relation_type": "BID_TOGETHER",
            "supplier_a_id": "SUP-012",
            "supplier_b_id": "SUP-013",
            "project_id": "PRJ-001",
            "times": 3,
        }
    )

    # 随机补充 AFFILIATE_OF（1-2 条，不干扰场景1 的 SUP-001..003）
    affil_pool = [s["supplier_id"] for s in suppliers if s["supplier_id"] not in {"SUP-001", "SUP-002", "SUP-003", "SUP-012", "SUP-013"}]
    for _ in range(rng.randint(1, 2)):
        if len(affil_pool) < 2:
            break
        a, b = rng.sample(affil_pool, 2)
        links.append(
            {
                "relation_type": "AFFILIATE_OF",
                "supplier_a_id": a,
                "supplier_b_id": b,
                "relation_type_detail": rng.choice(["母子公司", "同一集团"]),
            }
        )
    return links


def generate_bids(
    rng: random.Random,
    fake: Faker,
    lots: list[dict],
    suppliers: list[dict],
    projects: list[dict],
) -> tuple[list[dict], dict[str, str]]:
    """生成投标关系（bid_document 记录）+ 标书全文。

    返回 (bids, bid_contents)。bid_contents: {bid_id: 全文文本}。
    - 每个标段 ≥3 家投标供应商（场景标段用指定集合）
    - 场景3：SUP-012/SUP-013 标书高相似度（共享母版）
    """
    bids: list[dict] = []
    bid_contents: dict[str, str] = {}
    supplier_ids = [s["supplier_id"] for s in suppliers]

    # 场景标段投标集合（P7.1 场景编排）
    scene_bidders = {
        "LOT-001": ["SUP-001", "SUP-002", "SUP-003"],  # 场景1：无关联
        "LOT-004": ["SUP-010", "SUP-006", "SUP-007"],  # 场景2：含被持股 SUP-010
        "LOT-007": ["SUP-012", "SUP-013", "SUP-008"],  # 场景3：同一控制人
    }

    seq = 0
    for lot in lots:
        lot_id = lot["lot_id"]
        project = next(p for p in projects if p["project_id"] == lot["project_id"])
        if lot_id in scene_bidders:
            chosen = scene_bidders[lot_id]
        else:
            # 其他标段随机 3-4 家（避开场景1 三家，避免污染正常评审数据）
            pool = [s for s in supplier_ids if s not in {"SUP-001", "SUP-002", "SUP-003"}]
            chosen = rng.sample(pool, rng.randint(3, 4))

        for sup_id in chosen:
            seq += 1
            bid_id = gen_id("bid", seq)
            supplier = next(s for s in suppliers if s["supplier_id"] == sup_id)
            # 预算附近随机报价（±15%）
            base = lot["budget"]
            bid_amount = rng.randint(int(base * 0.85), int(base * 1.15))

            bid_obj = {
                "bid_id": bid_id,
                "lot_id": lot_id,
                "supplier_id": sup_id,
                "bid_amount": bid_amount,
                "duration": rng.randint(90, 300),
                "team_size": rng.randint(5, 30),
                "status": "SUBMITTED",
                "structured_data": {
                    "quality_cert": rng.choice(["ISO9001", "ISO27001", "CMMI3", "CMMI5"]),
                    "warranty_months": rng.choice([24, 36, 48]),
                },
            }
            bids.append(bid_obj)

            # 场景3：同一控制人的两家供应商共享段落种子（高相似度标书）
            common = dict(
                bid_amount=bid_amount,
                duration=bid_obj["duration"],
                team_size=bid_obj["team_size"],
                quality_cert=bid_obj["structured_data"]["quality_cert"],
                warranty_months=bid_obj["structured_data"]["warranty_months"],
            )
            if lot_id == "LOT-007" and sup_id in ("SUP-012", "SUP-013"):
                bid_contents[bid_id] = render_bid_content(
                    fake, supplier["name"], project["name"], lot["name"], supplier["industry"],
                    shared_seed=SCENE3_SHARED_SEED, **common,
                )
            else:
                bid_contents[bid_id] = render_bid_content(
                    fake, supplier["name"], project["name"], lot["name"], supplier["industry"],
                    bid_seed=int(bid_id.split("-")[1]), **common,
                )
    return bids, bid_contents


def generate_admin_users(rng: random.Random, fake: Faker) -> list[dict]:
    """生成 ADMIN + PROJECT_MANAGER 账号（2 个 PM）。"""
    users: list[dict] = []
    users.append(
        {
            "user_id": gen_id("user", 1),
            "username": USERNAME_PREFIXES["ADMIN"],
            "password_hash": bcrypt.hashpw(INITIAL_PASSWORD.encode(), bcrypt.gensalt()).decode(),
            "role": "ADMIN",
            "display_name": "系统管理员",
            "email": "admin@example.com",
            "phone": fake.phone_number(),
            "is_active": True,
            # 自查 #6：合成演示账号免首登强改（验收/演示脚本零影响）
            "must_change_password": False,
        }
    )
    for i in range(1, 3):
        users.append(
            {
                "user_id": gen_id("user", 1 + i),
                "username": f"{USERNAME_PREFIXES['PROJECT_MANAGER']}{i}",
                "password_hash": bcrypt.hashpw(INITIAL_PASSWORD.encode(), bcrypt.gensalt()).decode(),
                "role": "PROJECT_MANAGER",
                "display_name": f"项目经理{i}",
                "email": f"pm{i}@example.com",
                "phone": fake.phone_number(),
                "is_active": True,
                # 自查 #6：合成演示账号免首登强改（验收/演示脚本零影响）
                "must_change_password": False,
            }
        )
    return users


def generate_all(*, seed: int, n_projects: int, n_experts: int, n_suppliers: int) -> GenResult:
    """统一入口：生成全套合成数据，返回聚合结果。"""
    rng = random.Random(seed)
    fake = Faker("zh_CN")
    Faker.seed(seed)

    result = GenResult(seed=seed)

    # 管理账号
    result.users.extend(generate_admin_users(rng, fake))

    # 专家 + 供应商
    experts, expert_users = generate_experts(rng, fake, n_experts)
    result.experts = experts
    result.users.extend(expert_users)
    suppliers, supplier_users = generate_suppliers(rng, fake, n_suppliers)
    result.suppliers = suppliers
    result.users.extend(supplier_users)

    # 项目 + 标段 + 维度
    projects, lots = generate_projects(rng, fake, n_projects)
    result.projects = projects
    result.lots = lots
    result.dimensions = generate_dimensions(rng, lots)

    # 冲突网络 + 供应商关联
    result.conflicts = build_conflict_network(rng, fake, experts, suppliers)
    result.supplier_links = build_supplier_links(rng, suppliers, projects)

    # 投标 + 标书全文
    result.bids, bid_contents = generate_bids(rng, fake, lots, suppliers, projects)

    # 标书内容随 bids 一起返回（import 时写盘）
    result.bid_contents = bid_contents
    return result
