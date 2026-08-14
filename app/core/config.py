"""应用配置 — 使用 pydantic-settings 从 .env / 环境变量加载。

所有配置项集中在 Settings 类中，供各模块统一引用 settings 单例。
配置项来源优先级：环境变量 > .env 文件 > 默认值。
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局配置项。新增配置项在此声明即可，自动从环境变量 / .env 读取。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ========== 运行模式 ==========
    app_name: str = "AI 智能评标系统"
    app_version: str = "0.1.0"
    datasource_mode: str = "synthetic"  # synthetic | real
    debug: bool = False
    log_level: str = "INFO"

    # ========== MySQL ==========
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "smart_procurement"
    mysql_user: str = "smart"
    mysql_password: str = "smart_procurement_dev"
    mysql_root_password: str = "root_dev_pass"
    mysql_url: str | None = None  # 显式连接串，优先于分项配置

    # ========== Neo4j ==========
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_dev_pass"

    # ========== Milvus ==========
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_collection: str = "bid_documents"

    # ========== MinIO ==========
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minio_dev_pass"
    minio_bucket: str = "bid-files"
    minio_presign_expiry_seconds: int = 1800

    # ========== Redis ==========
    redis_url: str = "redis://localhost:6379/0"

    # ========== AI 服务 ==========
    deepseek_api_key: str = "sk-xxx"
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    deepseek_enabled: bool = True  # False => LLM 停用降级纯人工（P6.6 验收/降配开关）
    deepseek_timeout: float = 60.0
    deepseek_max_retries: int = 3
    deepseek_circuit_breaker_threshold: int = 5
    bge_m3_endpoint: str = ""  # 空 => dev 模式 sentence-transformers 直连
    bge_m3_model: str = "BAAI/bge-m3"

    # ========== 文档解析 / RAG（P2.1） ==========
    doc_chunk_min_tokens: int = 500
    doc_chunk_max_tokens: int = 1000
    doc_chunk_overlap_tokens: int = 100
    doc_zombie_timeout_minutes: int = 30  # PARSING 超过该时长视为僵尸
    doc_parse_max_retries: int = 3
    doc_parse_retry_delay_seconds: int = 60

    # ========== 安全 ==========
    jwt_secret_key: str = "change-me-in-production"
    jwt_access_token_expire_minutes: int = 30
    jwt_refresh_token_expire_days: int = 7
    fernet_key: str = ""  # 空 => 启动时自动生成（生产必填）

    # ========== 业务参数 ==========
    conflict_employment_years: int = 3
    review_deviation_threshold: float = 0.15
    fraud_auto_pass_threshold: float = 25
    fraud_critical_threshold: float = 75

    @property
    def database_url(self) -> str:
        """SQLAlchemy async 连接串。显式 MYSQL_URL 优先，否则按分项拼接。"""
        if self.mysql_url:
            return self.mysql_url
        return (
            f"mysql+asyncmy://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
        )

    @property
    def minio_full_endpoint(self) -> str:
        """MinIO SDK 需要的完整端点（http:// 前缀）。"""
        if self.minio_endpoint.startswith("http"):
            return self.minio_endpoint
        return f"http://{self.minio_endpoint}"


@lru_cache
def get_settings() -> Settings:
    """全局单例，避免重复解析 .env。"""
    return Settings()


settings = get_settings()
