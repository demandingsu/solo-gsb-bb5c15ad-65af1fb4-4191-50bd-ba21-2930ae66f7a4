"""ORM 模型：发布批次 / 节点 / 依赖边。"""
import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


class Release(Base):
    __tablename__ = "releases"

    id = Column(Integer, primary_key=True)
    version = Column(String(128), unique=True, nullable=False, index=True)
    note = Column(Text, nullable=True)

    # running / rolled_back / completed
    status = Column(String(32), nullable=False, default="running")

    # 失败回报的节点 key（未失败为 None）
    failed_node_key = Column(String(128), nullable=True)
    failed_message = Column(Text, nullable=True)

    total_layers = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    nodes = relationship(
        "Node",
        back_populates="release",
        cascade="all, delete-orphan",
        order_by="Node.order_index",
    )


class Node(Base):
    __tablename__ = "nodes"
    __table_args__ = (
        UniqueConstraint("release_id", "node_key", name="uq_node_release_key"),
        Index("ix_nodes_release_layer", "release_id", "layer"),
    )

    id = Column(Integer, primary_key=True)
    release_id = Column(
        Integer, ForeignKey("releases.id", ondelete="CASCADE"), nullable=False
    )

    node_key = Column(String(128), nullable=False)
    name = Column(String(256), nullable=True)
    layer = Column(Integer, nullable=False, default=0)
    order_index = Column(Integer, nullable=False, default=0)
    depends_on = Column(JSON, nullable=False, default=list)

    # pending / success / failed / rolled_back
    status = Column(String(32), nullable=False, default="pending")
    error_message = Column(Text, nullable=True)

    reported_at = Column(DateTime, nullable=True)
    rolled_back_at = Column(DateTime, nullable=True)

    release = relationship("Release", back_populates="nodes")


class Edge(Base):
    """依赖边：node_key 依赖 depends_on_key（depends_on_key 必须先执行）。"""

    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint(
            "release_id", "node_key", "depends_on_key", name="uq_edge_triple"
        ),
    )

    id = Column(Integer, primary_key=True)
    release_id = Column(
        Integer, ForeignKey("releases.id", ondelete="CASCADE"), nullable=False
    )
    node_key = Column(String(128), nullable=False)
    depends_on_key = Column(String(128), nullable=False)
