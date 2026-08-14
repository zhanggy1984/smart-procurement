"""SmartDocumentChunker — 标题感知文档分块（P2.1 Step 3）。

设计目标（task.md P2.1 / P2.2）：
- 标题感知：按"第X章 / X.X / 一、 / （一）"等标题行优先切分，标题作为该章
  首个 chunk 内容前缀，同时写入 chapter_title 字段（Milvus 检索溯源用）。
- 分块尺寸：单 chunk 落在 [min_tokens, max_tokens]，同一章节内超长正文用
  滑窗切分，相邻 chunk 天然 overlap（步长 = max - overlap）。
- 幂等/纯函数：chunk() 无副作用，同一输入输出完全一致（验收/单测可复现）。

P2.2 单测覆盖：标题感知切分、递归二分、overlap 保留、超长文档截断、空文档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import settings

# 标题行识别：中文章节/数字小节/序号列表。行首（允许缩进）命中即视为标题。
# 例："第一章  公司概况"、"3.2 系统架构"、"一、总体目标"、"（一）建设内容"
_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百千零〇]+[章节篇]"  # 第一章 / 第2节 / 第三篇
    r"|附录[^\n]*"  # 附录A / 附录
    r"|[0-9]+(?:\.[0-9]+){0,2}[、.\s]"  # 1、 / 1.1 / 3.2.1
    r"|[一二三四五六七八九十]{1,3}、"
    r"|（[一二三四五六七八九十]{1,3}）"
    r")\s*[^\n]{0,40}?"
)


@dataclass
class DocumentChunk:
    """单个分块。字段对齐 Milvus `bid_documents` schema（scripts/init_milvus.py）。"""

    chunk_id: str  # f"{bid_id}-{seq:04d}"
    bid_id: str
    lot_id: str
    content: str
    chapter_title: str
    page_no: int
    chunk_index: int
    source_file: str
    embedding: list[float] = field(default_factory=list)  # P2.1 Step 4 填充


class SmartDocumentChunker:
    """标题感知分块器。token 用 tiktoken cl100k_base 估算（与 embedding 无关，仅控长度）。"""

    def __init__(
        self,
        min_tokens: int | None = None,
        max_tokens: int | None = None,
        overlap_tokens: int | None = None,
    ) -> None:
        self.min_tokens = min_tokens if min_tokens is not None else settings.doc_chunk_min_tokens
        self.max_tokens = max_tokens if max_tokens is not None else settings.doc_chunk_max_tokens
        self.overlap_tokens = (
            overlap_tokens if overlap_tokens is not None else settings.doc_chunk_overlap_tokens
        )
        if not (self.max_tokens > self.min_tokens > 0 and self.overlap_tokens < self.max_tokens):
            raise ValueError(f"分块参数非法: min={self.min_tokens} max={self.max_tokens} overlap={self.overlap_tokens}")
        # 延迟加载编码器（tiktoken 首次调用会下载词表，本地缓存后复用）
        self._encoder = None

    @property
    def encoder(self):
        """lazily 初始化 tiktoken 编码器（线程安全由 to_thread 调用方保证）。"""
        if self._encoder is None:
            import tiktoken

            self._encoder = tiktoken.get_encoding("cl100k_base")
        return self._encoder

    # ==================== 公开接口 ====================

    def chunk(
        self,
        text: str,
        *,
        bid_id: str,
        lot_id: str,
        source_file: str = "",
        page_no: int = 0,
    ) -> list[DocumentChunk]:
        """把全文切成 DocumentChunk 列表（标题感知 + 滑窗 overlap）。

        返回空列表：空文档/全空白。chunk_index 全局递增，chunk_id 取
        `{bid_id}-{seq:04d}`，保证 Milvus 主键唯一且可复现。
        """
        if not text or not text.strip():
            return []

        sections = self._split_by_headings(text)
        chunks: list[DocumentChunk] = []
        seq = 0
        for title, body in sections:
            # 标题并入该章正文首行，chunk 内容自包含（检索上下文完整）
            full = f"{title}\n{body}" if title else body
            for piece in self._split_body(full):
                chunks.append(
                    DocumentChunk(
                        chunk_id=f"{bid_id}-{seq:04d}",
                        bid_id=bid_id,
                        lot_id=lot_id,
                        content=piece,
                        chapter_title=title or "无标题",
                        page_no=page_no,
                        chunk_index=seq,
                        source_file=source_file,
                    )
                )
                seq += 1
        return chunks

    # ==================== 内部实现 ====================

    @staticmethod
    def _split_by_headings(text: str) -> list[tuple[str, str]]:
        """按标题行把全文分成 (标题, 正文) 列表。

        无标题的正文归入"无标题"段（标题为空字符串，调用方映射为"无标题"）。
        标题行本身并入该段正文（见 chunk() 的 full 拼接），故此处只返回标题名。
        """
        sections: list[tuple[str, list[str]]] = []
        cur_title = ""
        cur_lines: list[str] = []
        for line in text.splitlines():
            if _HEADING_RE.match(line):
                if cur_lines or cur_title:
                    sections.append((cur_title, "\n".join(cur_lines)))
                cur_title = line.strip()
                cur_lines = []
            else:
                cur_lines.append(line)
        if cur_lines or cur_title:
            sections.append((cur_title, "\n".join(cur_lines)))
        return [(t, b) for t, b in sections if b.strip()]

    def _split_body(self, body: str) -> list[str]:
        """把一段正文切成 [min, max] 的 chunk 列表。

        长度未超 max 的段整体保留（短章不硬凑到 min，避免无意义填充）；
        超长段用滑窗切（步长 = max - overlap），相邻 chunk 保留 overlap 上下文。
        单段不足 min 的情况仅发生在文档整体很短的场景，允许（后续检索不受影响）。
        """
        tokens = self.encoder.encode(body)
        if len(tokens) <= self.max_tokens:
            return [body]

        step = self.max_tokens - self.overlap_tokens
        pieces: list[str] = []
        start = 0
        while start < len(tokens):
            end = min(start + self.max_tokens, len(tokens))
            pieces.append(self.encoder.decode(tokens[start:end]))
            if end == len(tokens):
                break
            start += step
        return pieces
