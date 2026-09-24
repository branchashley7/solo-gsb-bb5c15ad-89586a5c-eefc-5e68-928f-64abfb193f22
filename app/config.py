"""运行期配置，全部可通过环境变量覆盖。"""
import os


class Settings:
    # SQLite 默认把数据文件放在 ./data 下，该目录由 Compose 挂载为命名卷
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL", "sqlite:///./data/coldchain.db"
    )
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))


settings = Settings()
