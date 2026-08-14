"""P7.5 RAG + AI 质量基准包。

组成：
- bench_data.py      共享数据（24 子主题内容模板、标书质量配置、基准 query/GT）
- make_deep_bids.py  生成 5 份深度标书 → Milvus + MySQL 维度
- benchmark_rag.py   RAG 基准（Recall@5 / MRR / 拒答 / 维度感知）
- benchmark_ai.py    AI 评分基准（真实 DeepSeek，MAE / Kendall / 引用可验证）
- benchmark_intent.py 意图识别基准（真实 DeepSeek）
"""
