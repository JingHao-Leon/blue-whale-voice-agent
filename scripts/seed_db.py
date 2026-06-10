"""Seed sample visitors so you can demo returning-visitor recognition right away."""
import asyncio
from datetime import datetime, timedelta

from sqlmodel.ext.asyncio.session import AsyncSession

from app.database import Visitor, engine, init_db


SEED = [
    # plate, contact_name, company, reason, days_ago
    ("沪A12345", "张师傅", "蓝色鲸鱼科技", "送货", 7),
    ("沪A12345", "张师傅", "蓝色鲸鱼科技", "送货", 14),
    ("沪A12345", "张师傅", "蓝色鲸鱼科技", "送货", 30),
    ("沪B88888", "李经理", "蓝色鲸鱼科技", "面试", 5),
    ("京C66666", "王女士", "蓝色鲸鱼科技", "拜访王总", 3),
    ("粤B99999", "陈师傅", "蓝色鲸鱼科技", "拉货", 2),
    ("沪A12345", "张师傅", "蓝色鲸鱼科技", "送货", 1),
]


async def main() -> None:
    await init_db()
    async with AsyncSession(engine()) as session:
        for plate, name, company, reason, days_ago in SEED:
            v = Visitor(
                plate=plate,
                contact_name=name,
                company=company,
                reason=reason,
                phone="13800000000",
                duration="2小时",
                is_returning=True,
                call_sid=f"seed-{plate}-{days_ago}",
                started_at=datetime.utcnow() - timedelta(days=days_ago),
                ended_at=datetime.utcnow() - timedelta(days=days_ago) + timedelta(seconds=15),
                duration_seconds=15.0,
            )
            session.add(v)
        await session.commit()
    print(f"Seeded {len(SEED)} visitor rows.")


if __name__ == "__main__":
    asyncio.run(main())
