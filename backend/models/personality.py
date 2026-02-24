"""性格模型"""

from datetime import datetime
from typing import List

from sqlalchemy import Boolean, DateTime, String, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base


class Personality(Base):
    """性格表 - 存储 AI 性格预设"""

    __tablename__ = "personalities"

    id: Mapped[str] = mapped_column(String(50), primary_key=True)              # 性格唯一标识，如 "grumpy"
    name: Mapped[str] = mapped_column(String(100), nullable=False)             # 显示名称，如 "暴躁哥"
    description: Mapped[str] = mapped_column(Text, nullable=False)             # 性格描述
    traits: Mapped[List[str]] = mapped_column(JSON, nullable=False)            # 性格特征列表，如 ["暴躁", "嘴硬心软"]
    speaking_style: Mapped[str] = mapped_column(Text, nullable=False)          # 说话风格描述
    icon: Mapped[str] = mapped_column(String(50), nullable=False, default="Smile")  # 前端图标名
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # 是否内置性格（内置不可删除）
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)    # 是否启用
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)   # 创建时间，自动填入
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow           # 更新时间，每次修改自动刷新
    )

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "traits": self.traits,
            "speaking_style": self.speaking_style,
            "icon": self.icon,
            "is_builtin": self.is_builtin,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
