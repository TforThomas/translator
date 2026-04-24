from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./backend/data/omni.db")

# SQLite 不支持连接池，使用简单配置
# 如果使用 PostgreSQL/MySQL，可以添加 pool_size 等参数
engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True  # 检查连接有效性
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)

Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
