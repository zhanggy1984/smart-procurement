"""请求级 LLM 硬失败标记单测（七环回流 B 方案 · sp 侧）。

背景：sp 两个 SSE 接口恒返 200，LLM 异常被业务吞成 error 帧/降级帧后生成器正常结束
⇒ 观测出口只看到 200 ⇒ root 记 ok ⇒ 平台按「故障已被业务吸收」把回流候选切掉（环② 断）。
故失败出口须在业务侧置硬失败标记，观测中间件出口据此记 root=error。

**mock 边界必须落在 `chat_stream` 的重试环内部**（`self._client.chat.completions.create`）：
置位点长在 `attempts >= len(schedule)` 之后，若把整个 `chat_stream` 换成桩，置位点根本
不执行 ⇒ 用例只验了「没置位」，形同虚设。

四条核心语义：
- 重试耗尽（最终失败）⇒ 置位（否则环② 断，本机制无意义）
- **重试后成功 ⇒ 必须仍未置位**（否则「首次失败 → 重试成功」被误标 error，是假红）
- 上下文缺失 ⇒ 打日志暴露（否则置位静默空转，表现成「trace 记 ok」——正是要治的病）
- 出口分支优先级：断连 > LLM 硬失败 > HTTP 状态码 > ok
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from app import obs as obs_mod
from app.ai.llm import deepseek_client as dc
from app.core import middleware as mw


class _FakeTimeout(Exception):
    """类名含 timeout ⇒ llm_error_type 归 llm_timeout（平台白名单词），且无 status_code。"""


def _fake_completions(fail_times: int, text: str = "完整回答"):
    """假 completions：前 fail_times 次抛 _FakeTimeout，之后返回一次性流。

    返 (obj, state)：state["n"] 记录真实调用次数，用于断言重试确实发生。
    """
    state = {"n": 0}

    async def _create(**_kw):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise _FakeTimeout("上游超时")
        chunk = SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(delta=SimpleNamespace(content=text))],
        )

        async def _aiter():
            yield chunk

        return _aiter()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=_create))), state


@pytest.fixture
def health():
    """模拟 obs 中间件：请求进入时置入可变 dict，退出时复位。"""
    h: dict = {"hard_fail": False, "error_type": None}
    # 变量名避开 `token`：pre-commit 的 secrets 检查按名匹配，会把 contextvar 的
    # reset handle 误判成凭据（同族误报已记 memory；改名是既有处置惯例）
    cv_handle = obs_mod.llm_health_var.set(h)
    yield h
    obs_mod.llm_health_var.reset(cv_handle)


@pytest.fixture
def no_sleep(monkeypatch):
    """退避等待置零：只保留「重试环」结构，不真睡 0.5+1+3 秒。

    只换 `sleep` 一个属性、不整模块替换——`dc.asyncio` 与测试进程是同一个模块对象，
    整体替换会连带打掉 `DeepSeekClient.__init__` 里的 `asyncio.Lock()`。
    """

    async def _noop(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)


def _client_with(fake) -> dc.DeepSeekClient:
    c = dc.DeepSeekClient()
    c._client = fake
    return c


def _drive(coro) -> None:
    """把「消费到耗尽」跑完（chat_stream 是异步生成器）。"""
    with pytest.raises(_FakeTimeout):
        asyncio.run(coro)


def test_retry_exhausted_marks_hard_fail(monkeypatch, health, no_sleep):
    """重试耗尽 ⇒ 置位，error_type 取自异常分型（平台白名单词）。

    5xx 类退避 3 次 ⇒ 共 4 次尝试；用户最终拿到的是 error 帧/降级话术。
    """
    fake, state = _fake_completions(fail_times=99)
    client = _client_with(fake)

    async def _run():
        async for _ in client.chat_stream([], temperature=0.0, max_tokens=16):
            pass

    _drive(_run())
    assert state["n"] == 4, "5xx 退避序列 3 段 ⇒ 首试 + 3 次重试"
    assert health["hard_fail"] is True, "重试耗尽必须置位，否则 root 记 ok、环② 断"
    assert health["error_type"] == "llm_timeout"


def test_retry_then_success_must_stay_ok(monkeypatch, health, no_sleep):
    """首次失败 → 重试成功：用户拿到完整回答，root 必须仍为 ok。

    这是「不误标」这一维的**唯一**直接判据：sp 的置位点在重试环末尾（与 gq 的
    「先标记再撤销」不同形），若把置位错放到 except 开头，本条转红。
    """
    fake, state = _fake_completions(fail_times=1)
    client = _client_with(fake)

    async def _run():
        out = []
        async for piece in client.chat_stream([], temperature=0.0, max_tokens=16):
            out.append(piece)
        return out

    out = asyncio.run(_run())
    assert state["n"] == 2, "首次失败应触发一次重试"
    assert out == [("完整回答", None)]
    assert health["hard_fail"] is False, "重试成功 ⇒ 交付未降级，不得标记（假红）"
    assert health["error_type"] is None


def test_mark_without_context_is_loud(caplog):
    """上下文缺失（中间件未按序置入）⇒ 打日志，不静默空转。

    静默空转的后果与不修本机制相同（trace 记 ok），故必须能被发现；本用例同时锁住
    「不得抛异常打断业务」——观测边带故障不拦服务。
    """
    assert obs_mod.llm_health_var.get() is None, "本用例前提：无请求上下文"
    with caplog.at_level(logging.ERROR, logger="app.obs"):
        obs_mod.mark_llm_hard_fail("llm_timeout")  # 不抛异常 = 通过

    assert any("llm_health 上下文缺失" in r.getMessage() for r in caplog.records), \
        "置位丢失必须 fail-loud，否则表现为「trace 记 ok」而无人察觉"


def test_end_request_passes_only_supported_kwargs(monkeypatch):
    """出口只传**镜像内 sdk 确定支持**的形参。

    回归锁：曾因多传 `input=` 抛 TypeError 被 app.obs 的兜底吞成 debug 日志
    ⇒ 一条 request 事件都不产出（root 恒不到）。本用例按旧版 sdk 的签名（无 input）
    起桩，多传任何 kwarg 都会 TypeError ⇒ 转红。
    """
    seen = {}

    class _OldSdk:
        # 旧版镜像签名：无 input
        def end_request(self, status, *, error_type=None, error_msg=None):
            seen.update(status=status, error_type=error_type)

    monkeypatch.setattr(obs_mod, "obs", lambda: _OldSdk())
    obs_mod.end_request("error", error_type="llm_timeout")

    assert seen == {"status": "error", "error_type": "llm_timeout"}


class _FakeEndSdk:
    """假 obs_sdk 的 end_request 侧：只记收口调用。"""

    def __init__(self) -> None:
        self.ends: list[dict] = []

    def end_request(self, status, *, error_type=None, error_msg=None, input=None):
        self.ends.append(
            {"status": status, "error_type": error_type, "error_msg": error_msg, "input": input}
        )


@pytest.fixture
def end_sdk(monkeypatch):
    fake = _FakeEndSdk()
    monkeypatch.setattr(obs_mod, "obs", lambda: fake)
    return fake


def test_obs_finish_hard_fail_beats_200(end_sdk):
    """出口分支：HTTP 200 + LLM 硬失败 ⇒ 记 error（root 终态由这一句决定，环② 的入口）。"""
    mw._obs_finish(
        SimpleNamespace(status_code=200),
        health={"hard_fail": True, "error_type": "llm_timeout"},
    )

    assert len(end_sdk.ends) == 1
    assert end_sdk.ends[0]["status"] == "error", "SSE 恒 200，状态码判不出，必须靠 health 记 error"
    assert end_sdk.ends[0]["error_type"] == "llm_timeout"


def test_obs_finish_normal_request_stays_ok(end_sdk):
    """反例（不可省）：正常请求仍记 ok —— 防「一律记 error」式的过度修。

    只验正例的话，「硬失败记 error」与「所有请求都记 error」两种实现在断言上不可区分。
    """
    mw._obs_finish(
        SimpleNamespace(status_code=200),
        health={"hard_fail": False, "error_type": None},
    )

    assert end_sdk.ends[0]["status"] == "ok"
    assert end_sdk.ends[0]["error_type"] is None


def test_obs_finish_aborted_beats_hard_fail(end_sdk):
    """断连优先级最高：用户中断 + LLM 硬失败同时成立 ⇒ 记 CLIENT_DISCONNECT。

    「未拿到完整响应」比「LLM 失败」更贴近事实（断连时失败可能还没发生），故排第一。
    """
    mw._obs_finish(
        SimpleNamespace(status_code=200),
        aborted=True,
        health={"hard_fail": True, "error_type": "llm_timeout"},
    )

    assert end_sdk.ends[0]["status"] == "error"
    assert end_sdk.ends[0]["error_type"] == "CLIENT_DISCONNECT"


def test_obs_finish_http_code_still_recorded(end_sdk):
    """非 2xx 且无 LLM 硬失败 ⇒ 仍按 HTTP_{code}（既有行为不回退）。"""
    mw._obs_finish(
        SimpleNamespace(status_code=503),
        health={"hard_fail": False, "error_type": None},
    )

    assert end_sdk.ends[0]["status"] == "error"
    assert end_sdk.ends[0]["error_type"] == "HTTP_503"
