"""观测上报中央门（§11.3 smart-procurement 接入）。

sp 日志体系是 structlog PrintLogger 直出（不经 stdlib logging），SDK 的 stdlib
Handler 收不到 → 日志走 SDK 的 structlog processor（`obs_sdk.structlog_processor`，
由 core/logging.setup_logging 在 processors 链 JSONRenderer 前插入本仓不重复实现）。

本模块是 request 出入口 / llm_call / summarize 旁路的统一观测缝（观测 seam）：
- 所有打点 helper 在**模块内**经 obs() gate 现取 sdk → 测试单点替换 app.obs.obs 即覆盖全链；
- 未启用（OBS_* 三要素缺一）或包缺失 → obs() 返 None → 各 helper 零动作直通，业务无侵入；
- sdk 自身故障（record/init 抛异常）一律吞掉，观测边带不炸业务。
"""
from __future__ import annotations

import contextvars
import logging
import time
from typing import Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# 观测豁免路径：健康探针（容器 healthcheck 15s 一轮的纯噪声）+ 登录/刷新认证面（非业务端点，
# 对齐 §11.3 request 覆盖「全量业务端点，豁免 health/login」裁决）。其余 /api/v1/* 全量打点。
OBS_EXEMPT_PATHS = frozenset({
    "/health/live",
    "/health/ready",
    "/api/auth/login",
    "/api/auth/refresh",
})


def obs():
    """惰性取 obs_sdk 模块：未启用（三要素缺一）或未安装时返回 None（观测边带不阻塞业务）。"""
    if not settings.obs_ready:
        return None
    try:
        import obs_sdk
    except ImportError:
        logger.warning("[obs] obs_sdk 未安装，观测边带关闭（OBS_ENABLED=true 但包缺失）")
        return None
    return obs_sdk


def init_obs() -> None:
    """装配 obs_sdk（lifespan：setup_logging 已把 structlog processor 插链后调用）。

    log_mode="structlog"：init 不挂 stdlib Handler（sp 日志不经 stdlib），structlog
    转发由 processor 链承载。重复 init 抛错由 sdk 保证（进程单例）。失败仅告警不拦启动。
    """
    sdk = obs()
    if sdk is None:
        return
    try:
        sdk.init(
            "smart-procurement",
            kafka_servers=settings.obs_kafka_servers,
            topic=settings.obs_kafka_topic,
            sasl_username=settings.obs_kafka_sasl_username or None,
            sasl_password=settings.obs_kafka_sasl_password or None,
            flush_batch=settings.obs_flush_batch,
            flush_interval_s=settings.obs_flush_interval_s,
            log_mode="structlog",
        )
        logger.info("[obs] obs_sdk 已初始化 topic=%s", settings.obs_kafka_topic)
    except Exception as e:  # 观测边带故障不拦服务启动
        logger.warning("[obs] obs_sdk init 失败（观测边带关闭）: %s", e)


def shutdown_obs() -> None:
    """收尾（lifespan 关闭段）：终刷剩余事件后关线程（幂等：未 init 也安全）。"""
    sdk = obs()
    if sdk is None:
        return
    try:
        sdk.shutdown()
    except Exception as e:
        logger.warning("[obs] obs_sdk shutdown 异常: %s", e)


def begin_request(*, method: str, path: str, trace_id: Optional[str] = None) -> bool:
    """request 入口（RequestIDMiddleware 调用，豁免路径已由调用方短路）。返 True=已建 span。"""
    sdk = obs()
    if sdk is None:
        return False
    try:
        sdk.begin_request(method=method, path=path, trace_id=trace_id)
        return True
    except Exception:  # 观测故障不炸请求
        logger.debug("[obs] begin_request 异常 %s %s", method, path, exc_info=True)
        return False


# 请求级 LLM 健康标记与观测入参：中间件建请求上下文时置一个**可变 dict**，下游失败出口
# 就地改它。持 dict 而非标量——contextvar 的写在子任务上下文里发生，标量赋值传不回中间件；
# dict 按引用跨 context 拷贝共享，故子任务写入对入口可见。业务侧拿不到 request 对象
# （sp 两个 SSE 路由均无 request 形参），这是唯一可用的载体。
llm_health_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "llm_health", default=None
)


def mark_llm_hard_fail(error_type: str) -> None:
    """记「本轮最终没拿到 LLM 结果」，由请求出口据此记 root=error。

    SSE 接口恒返 200：LLM 异常被业务吞成 error 帧后生成器正常结束，出口只看到 200，
    平台会按「故障已被业务吸收」把回流候选切掉（环② 断）。故失败出口须在业务侧置位。

    一轮内多次硬失败只取首次（先发生的更具代表性）。上下文缺失（后台任务/中间件未按序
    置入）时**打日志暴露**而不是静默返回——静默的后果与不实现本机制相同（trace 记 ok），
    必须能被发现；且只记日志、不抛异常，观测边带故障不拦业务。
    """
    health = llm_health_var.get()
    if health is None:
        logger.error(
            "[obs] llm_health 上下文缺失，LLM 硬失败标记丢失（error_type=%s）；"
            "生产出现即中间件未按序置入 context", error_type,
        )
        return
    if not health["hard_fail"]:
        health["hard_fail"] = True
        health["error_type"] = error_type


def mark_llm_hard_fail_from_exc(exc: Exception) -> None:
    """按异常分型置位（error_type 复用平台白名单分类器，不引入新词）。"""
    mark_llm_hard_fail(llm_error_type(exc))


def end_request(status: str, *, error_type: Optional[str] = None,
                error_msg: Optional[str] = None,
                obs_input: Optional[dict] = None) -> None:
    """request 出口（中间件收口：ok / HTTP_{code} / CLIENT_DISCONNECT）。

    `obs_input` = 本请求入参现场，平台据此算 root_input_hash（环③ 建簇键，为空则该行
    不建簇：cluster_job 的 Fork A）。形参名不用 `input` 以避开内置名遮蔽，透传时映射。

    ⚠️ 该 kwarg **依赖镜像内 obs_sdk 支持**：历史 sp 镜像烤入的是旧版（无 `input`），
    多传一个 kwarg 抛 TypeError 且被下面兜底吞成 debug ⇒ **一条 request 事件都不产出**
    （root 恒不到，全 agent 观测哑掉）。2026-09-17 已重建镜像（obs_sdk 0.1.2）并核过
    容器内签名；**再动此处前先 `docker exec sp-app python -c "import inspect, obs_sdk;
    print(inspect.signature(obs_sdk.end_request))"` 确认形参仍在**（读容器，不读宿主源码）。
    """
    sdk = obs()
    if sdk is None:
        return
    try:
        sdk.end_request(status, error_type=error_type, error_msg=error_msg,
                        input=obs_input)
    except Exception:
        logger.debug("[obs] end_request 异常 status=%s", status, exc_info=True)


def llm_start() -> float:
    """llm_call 打点起点（time.monotonic()）。"""
    return time.monotonic()


def _ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def llm_error_type(exc: Exception) -> str:
    """异常 → llm_call error_type（平台错误分类白名单值域，**非自由字符串**）。

    openai 异常自带 status_code 时仅 429 单列（对应白名单 llm_rate_limit），其余码位
    （含 auth/permission 类的 401/403）在白名单无对应词，统一归 llm_other——原始码值由
    error_msg 保留。无状态码按类名关键词归并。CircuitOpenError（熔断前置拒绝）
    不打点，由 request HTTP_503 反映（对齐 cs：熔断拒绝非 LLM 调用）。
    """
    code = getattr(exc, "status_code", None)
    if code:
        return "llm_rate_limit" if code == 429 else "llm_other"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "llm_timeout"
    if "connect" in name:
        return "llm_connection"
    if "rate" in name:
        return "llm_rate_limit"
    return "llm_other"


def record_llm_ok(started: float, usage: Optional[dict] = None) -> None:
    """成功出口：llm_call ok + usage（模型取 settings.deepseek_model，统一层配置）。"""
    sdk = obs()
    if sdk is None:
        return
    try:
        sdk.record_llm(settings.deepseek_model, "ok", duration_ms=_ms(started), usage=usage)
    except Exception:
        logger.debug("[obs] record_llm ok 异常", exc_info=True)


def record_llm_error(started: float, exc: Exception, *, error_type: Optional[str] = None) -> None:
    """失败出口（§2.4 先记再抛）：error_type 缺省由 llm_error_type 分类；msg 截 512。"""
    sdk = obs()
    if sdk is None:
        return
    try:
        sdk.record_llm(
            settings.deepseek_model, "error", duration_ms=_ms(started),
            error_type=error_type or llm_error_type(exc),
            error_msg=str(exc)[:512],
        )
    except Exception:
        logger.debug("[obs] record_llm error 异常", exc_info=True)
