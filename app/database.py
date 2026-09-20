"""SQLite 引擎 / 会话 / 表初始化。"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from .models import Base

# 数据库文件位置：默认放在项目根目录 data/release.db，可用环境变量覆盖
DB_PATH = os.getenv("DB_PATH", os.path.join(os.getcwd(), "data", "release.db"))

# 确保父目录存在
db_dir = os.path.dirname(DB_PATH)
if db_dir:
    os.makedirs(db_dir, exist_ok=True)

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db() -> None:
    """启动时建表（已存在则跳过）。"""
    Base.metadata.create_all(bind=engine)


def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
