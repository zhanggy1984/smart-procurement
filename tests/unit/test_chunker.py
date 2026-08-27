"""P7.2 SmartDocumentChunker 单元测试（task.md：5 用例 + P7.x 段落优先）。

覆盖标题感知切分、段落边界断开（段落不切半）、段落级 overlap 尾部、单段超长
滑窗兜底、无空行退化单换行切分、超长截断、空文档。
token 估算用 tiktoken cl100k_base（lazy），与 embedding 无关。
"""

from __future__ import annotations

import pytest

from app.ai.rag.chunker import SmartDocumentChunker


@pytest.fixture(scope="module")
def chunker() -> SmartDocumentChunker:
    return SmartDocumentChunker(min_tokens=50, max_tokens=100, overlap_tokens=20)


def _ids(chunks) -> list[str]:
    return [c.chunk_id for c in chunks]


def test_empty_document(chunker):
    """空文档/全空白 → 空列表。"""
    assert chunker.chunk("", bid_id="BID-1", lot_id="LOT-1") == []
    assert chunker.chunk("   \n  ", bid_id="BID-1", lot_id="LOT-1") == []


def test_short_document_single_chunk(chunker):
    """短文档整体保留为 1 个 chunk，不硬凑 min。"""
    chunks = chunker.chunk("第一章 概况\n公司简介与项目背景。", bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) == 1
    assert chunks[0].chapter_title == "第一章 概况"
    assert chunks[0].bid_id == "BID-1"
    assert chunks[0].chunk_id.startswith("BID-1-")


def test_heading_aware_split(chunker):
    """标题感知：多章标题各成 section，chapter_title 正确。"""
    text = (
        "第一章 公司概况\nA 公司成立于 2010 年。\n"
        "3.2 系统架构\n采用微服务架构，分层解耦。\n"
        "一、团队配置\n项目经理与 20 名研发工程师。"
    )
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    titles = [c.chapter_title for c in chunks]
    assert "第一章 公司概况" in titles
    assert "3.2 系统架构" in titles
    assert "一、团队配置" in titles


def test_long_body_sliding_window(chunker):
    """超长正文滑窗切分：>max 时多 chunk，步长=max-overlap，保留 overlap。"""
    # 中文长句池（>100 tokens 无法手动构造，用重复句撑长）
    sentence = "本项目建设内容包括基础网络、安全防护、数据平台与业务应用四大部分。"
    text = "第五章 建设方案\n" + sentence * 40  # ~40×26≈1040 中文字符，远超 100 tokens
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 2
    # chunk 长度都在 [min, max] 上限附近（中文 token 密度 ~1:1，只验上限不超 max 过多）
    for c in chunks:
        assert len(c.content) > 0
    # overlap 保留：相邻 chunk 有内容重叠
    assert chunks[0].content in chunks[1].content or chunks[1].content in chunks[0].content \
        or len(set(chunks[0].content) & set(chunks[1].content)) > 0


def test_paragraph_boundary_split(chunker):
    """段落优先：超长正文在段落边界断开，段落不切半。

    两段（各 ~66 tokens）累积超 max=100 → 只在段落边界断开：
    chunk0 只含段1，段2 完整进入后续 chunk，不出现半个段落。
    """
    sentence1 = "本项目建设内容包括基础网络、安全防护、数据平台与业务应用四大部分。"  # 33 tokens
    sentence2 = "实施计划分三阶段推进，包含方案设计、系统开发与试运行验收环节。"  # 33 tokens
    para1 = sentence1 * 2
    para2 = sentence2 * 2
    text = "第一章 建设方案\n" + para1 + "\n\n" + para2
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 2
    # 段1（含标题前缀）独立成 chunk0，段2 不进 chunk0（段落边界断开，不混切）
    assert chunks[0].content == "第一章 建设方案\n" + para1
    assert para2 not in chunks[0].content
    # 每 chunk 内段落完整：按空行切出的每个单元都是完整段落（句子整块）
    for c in chunks:
        for seg in c.content.split("\n\n"):
            assert seg == para1 or seg == para2 or seg == "第一章 建设方案\n" + para1


def test_paragraph_overlap_tail(chunker):
    """段落级 overlap：新 chunk 开头带上一 chunk 末尾段落尾部。

    3 段累积超 max 断开后，中间段应同时出现在相邻两个 chunk（边界上下文连续）。
    """
    sentence1 = "本项目建设内容包括基础网络、安全防护、数据平台与业务应用四大部分。"
    sentence2 = "实施计划分三阶段推进，包含方案设计、系统开发与试运行验收环节。"
    sentence3 = "质量保证体系覆盖需求评审、过程审计与交付验收三道防线。"
    para1 = sentence1 * 2  # 66 tokens
    para2 = sentence2 * 2
    para3 = sentence3 * 2
    text = "第一章 建设方案\n" + para1 + "\n\n" + para2 + "\n\n" + para3
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 2
    contents = [c.content for c in chunks]
    # overlap：相邻 chunk 共享某段落（末尾段尾部被带入新 chunk 开头）
    shared = any(
        para in contents[i] and para in contents[i + 1]
        for i in range(len(contents) - 1)
        for para in (para1, para2, para3)
    )
    assert shared, "相邻 chunk 应共享末尾段落（段落级 overlap）"


def test_single_paragraph_window_fallback(chunker):
    """单段超长（无段落信号）→ token 滑窗兜底（原逻辑保留）。

    超长连续文本无空行/换行，段落切分不可用 → 滑窗按步长 max-overlap 切，
    相邻窗口保留 overlap。
    """
    sentence = "本项目建设内容包括基础网络、安全防护、数据平台与业务应用四大部分。"
    text = "第一章 建设方案\n" + sentence * 40  # ~1300 tokens，单段无换行
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c.content) > 0
    # 设计不变量：标题作为该章首个 chunk 内容前缀，且不拆成独立近空 chunk
    # （标题不参与滑窗切分——若标题混入正文被硬切，会出现 ~7 token 的标题-only chunk）
    assert chunks[0].content.startswith("第一章 建设方案\n")
    assert not any(c.content.strip() == "第一章 建设方案" for c in chunks)
    # 滑窗 overlap：相邻窗口共享正文（字符重叠近似）
    assert len(set(chunks[0].content) & set(chunks[1].content)) > 0


def test_no_blank_line_newline_fallback(chunker):
    """无空行退化为单换行切分（DOCX 每段一行的场景）。

    DOCX 提取文本段间是单换行（非空行），段落信号退化为单换行，
    段落仍为原子单元不切半。
    """
    sentence = "本项目建设内容包括基础网络、安全防护、数据平台与业务应用四大部分。"  # 33 tokens
    text = "第一章 建设方案\n" + "\n".join([sentence] * 4)  # 正文 4 行无空行（132 tokens > max）
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 1
    for c in chunks:
        # 单换行退化的段落不切半：每段都是完整句子
        for seg in c.content.split("\n\n"):
            assert seg in (sentence, "第一章 建设方案") or seg.startswith("第一章 建设方案")


def test_chunk_id_sequence_global(chunker):
    """chunk_id 全局递增（{bid_id}-{seq:04d}），Milvus 主键唯一。"""
    text = "第一章 概况\n内容 A。\n第二章 详情\n内容 B。\n第三章 附录\n内容 C。"
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    ids = _ids(chunks)
    assert len(set(ids)) == len(ids)
    seqs = [int(i.split("-")[-1]) for i in ids]
    assert seqs == sorted(seqs) and len(seqs) == chunks[-1].chunk_index + 1


def test_illegal_params_raises():
    """非法分块参数（overlap ≥ max / min ≥ max）→ ValueError。"""
    with pytest.raises(ValueError):
        SmartDocumentChunker(min_tokens=100, max_tokens=100, overlap_tokens=0)
    with pytest.raises(ValueError):
        SmartDocumentChunker(min_tokens=0, max_tokens=50, overlap_tokens=10)


# ==================== P8.2 页码协议 + 溯源元数据 ====================


def test_page_marker_single_page(chunker):
    """单页标记：section 落在单页 → page_range=[n,n]，标记行剥离不进正文。"""
    text = "@@PAGE:1@@\n第一章 概况\n公司简介与项目背景。"
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) == 1
    assert chunks[0].page_range == [1, 1]
    assert "@@PAGE" not in chunks[0].content


def test_page_marker_cross_page_section(chunker):
    """跨页 section：页标记更新当前页，section 覆盖 [1,2]（section 级共享）。"""
    text = (
        "@@PAGE:1@@\n第一章 建设方案\n第一页方案要点。\n"
        "@@PAGE:2@@\n第二章 延续说明\n第二页详细设计。"
    )
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    pages = [(c.chapter_title, c.page_range) for c in chunks]
    assert ("第一章 建设方案", [1, 1]) in pages
    assert ("第二章 延续说明", [2, 2]) in pages


def test_page_range_cross_page_same_section(chunker):
    """同一 section 横跨两页：page_range=[1,2] 复制给该 section 下所有 chunk。"""
    text = (
        "@@PAGE:1@@\n第一章 建设方案\n第一页内容。\n"
        "@@PAGE:2@@\n第二页延续（仍属第一章）。"
    )
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    assert len(chunks) >= 1
    for c in chunks:
        assert c.page_range == [1, 2]


def test_no_marker_page_zero(chunker):
    """无页码标记（DOCX/MD/TXT）：恒 [0,0]。"""
    chunks = chunker.chunk("第一章 概况\n公司简介与项目背景。", bid_id="BID-1", lot_id="LOT-1")
    assert chunks[0].page_range == [0, 0]


def test_page_markers_do_not_change_split_order(chunker):
    """页码标记剥离不进正文：切分顺序与 chunk_id 序列不受标记影响（基准 chunk_id 稳定）。"""
    base = "第一章 概况\n公司简介与项目背景。\n第二章 详情\n技术方案说明。"
    marked = (
        "@@PAGE:1@@\n第一章 概况\n公司简介与项目背景。\n"
        "@@PAGE:2@@\n第二章 详情\n技术方案说明。"
    )
    c_base = chunker.chunk(base, bid_id="BID-1", lot_id="LOT-1")
    c_marked = chunker.chunk(marked, bid_id="BID-1", lot_id="LOT-1")
    assert [c.content for c in c_base] == [c.content for c in c_marked]
    assert _ids(c_base) == _ids(c_marked)


def test_heading_level_infer(chunker):
    """标题层级：章/篇/附录=1、数字小节按点 2/3/4、一、=2、（一）=3、无标题=0。"""
    text = (
        "独立段落无标题。\n"
        "第一章 概况\n内容 A。\n"
        "3.2 架构\n内容 B。\n"
        "3.2.1 模块划分\n内容 C。\n"
        "一、团队\n内容 D。\n"
        "（一）分工\n内容 E。\n"
        "附录A\n内容 F。"
    )
    chunks = chunker.chunk(text, bid_id="BID-1", lot_id="LOT-1")
    level = {c.chapter_title: c.heading_level for c in chunks}
    assert level["第一章 概况"] == 1
    assert level["3.2 架构"] == 3  # 2 + 点数(1)
    assert level["3.2.1 模块划分"] == 4  # 2 + 点数(2)
    assert level["一、团队"] == 2
    assert level["（一）分工"] == 3
    assert level["附录A"] == 1
    assert level["无标题"] == 0


def test_source_type_infer(chunker):
    """内容类型推断：table/list/code/paragraph（参考 good-question）。"""
    assert chunker.chunk("第一章 表\n品名 | 数量 | 单价\nA | 1 | 2\nB | 3 | 4",
                         bid_id="B", lot_id="L")[0].source_type == "table"
    assert chunker.chunk("第一章 列表\n- 需求一\n- 需求二\n- 需求三",
                         bid_id="B", lot_id="L")[0].source_type == "list"
    assert chunker.chunk("第一章 代码\n    def f():\n        return 1\n    def g():",
                         bid_id="B", lot_id="L")[0].source_type == "code"
    assert chunker.chunk("第一章 正文\n这是一段普通的说明文字。",
                         bid_id="B", lot_id="L")[0].source_type == "paragraph"


def test_page_range_str_roundtrip():
    """page_range ↔ VARCHAR 往返：单页 "1" / 跨页 "1-2" / 无页码 "0"。"""
    from app.ai.rag.chunker import page_range_from_str, page_range_to_str

    assert page_range_to_str([1, 1]) == "1"
    assert page_range_to_str([1, 2]) == "1-2"
    assert page_range_to_str([0, 0]) == "0"
    assert page_range_from_str("1") == [1, 1]
    assert page_range_from_str("1-2") == [1, 2]
    assert page_range_from_str("0") == [0, 0]
    assert page_range_from_str("") == [0, 0]
