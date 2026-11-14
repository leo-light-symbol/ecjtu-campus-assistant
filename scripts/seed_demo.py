"""一键导入演示数据：校园知识文档入库 + 校园地图点位写入。

用法：
    python scripts/seed_demo.py

脚本具备幂等性：
    - 已入库的同名文档会跳过，不会重复占用向量库；
    - 已存在的同名点位会更新坐标与描述，不会产生重复标记。

说明：
    点位坐标基于华东交通大学南区公开位置信息估算（WGS84），
    用于演示地图交互，精确位置请以学校官方平面图为准。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接以脚本方式运行，无需安装为包。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from app.models.database import Document, Point, SessionLocal, init_db
from app.services.rag_service import RAGService, process_document

SEED_DOCS_DIR = PROJECT_ROOT / "app" / "data" / "seed_docs"
CAMPUS_ADDRESS = "江西省南昌市经济技术开发区双港东大街808号华东交通大学南区"

# 演示点位：类型需与前端图标配色保持一致（食堂/教学楼/场馆/宿舍/快递点）。
DEMO_POINTS: list[dict[str, object]] = [
    {
        "name": "南区第一食堂",
        "type": "食堂",
        "address": f"{CAMPUS_ADDRESS}生活区",
        "longitude": 115.8646,
        "latitude": 28.7388,
        "description": "南区主要食堂之一，靠近学生宿舍，供应早中晚餐。",
        "opening_hours": "以现场公告为准",
    },
    {
        "name": "南区二食堂",
        "type": "食堂",
        "address": f"{CAMPUS_ADDRESS}生活区",
        "longitude": 115.8662,
        "latitude": 28.7385,
        "description": "南区第二食堂，用餐高峰集中在中午和傍晚。",
        "opening_hours": "以现场公告为准",
    },
    {
        "name": "学生第三食堂",
        "type": "食堂",
        "address": f"{CAMPUS_ADDRESS}学生公寓24号楼附近",
        "longitude": 115.8671,
        "latitude": 28.7402,
        "description": "位于学生公寓24号楼附近，方便宿舍区就餐。",
        "opening_hours": "以现场公告为准",
    },
    {
        "name": "第四食堂",
        "type": "食堂",
        "address": f"{CAMPUS_ADDRESS}南区",
        "longitude": 115.8638,
        "latitude": 28.7400,
        "description": "南区食堂之一，具体档口与营业安排以现场公告为准。",
        "opening_hours": "以现场公告为准",
    },
    {
        "name": "教3楼",
        "type": "教学楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8668,
        "latitude": 28.7394,
        "description": "南区公共课与专业课教学楼，上课前请确认教室编号。",
        "opening_hours": "教学时段开放",
    },
    {
        "name": "教16楼",
        "type": "教学楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8652,
        "latitude": 28.7400,
        "description": "南区教学楼，承担部分公共基础课程教学。",
        "opening_hours": "教学时段开放",
    },
    {
        "name": "教87楼",
        "type": "教学楼",
        "address": f"{CAMPUS_ADDRESS}教87号楼",
        "longitude": 115.8644,
        "latitude": 28.7396,
        "description": "南区办公楼之一，部分行政部门在此办公。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "图书馆（南馆）",
        "type": "场馆",
        "address": f"{CAMPUS_ADDRESS}图书馆",
        "longitude": 115.8659,
        "latitude": 28.7399,
        "description": "提供借阅、自习和电子文献检索服务，周四下午闭馆。",
        "opening_hours": "08:00—22:00（周四下午闭馆）",
    },
    {
        "name": "体育馆",
        "type": "场馆",
        "address": f"{CAMPUS_ADDRESS}体育区",
        "longitude": 115.8674,
        "latitude": 28.7388,
        "description": "室内体育场馆，部分时段需要预约，教学训练优先。",
        "opening_hours": "以场馆公告为准",
    },
    {
        "name": "南区田径场",
        "type": "场馆",
        "address": f"{CAMPUS_ADDRESS}体育区",
        "longitude": 115.8667,
        "latitude": 28.7380,
        "description": "室外田径场，适合跑步和日常锻炼。",
        "opening_hours": "室外场地全天开放",
    },
    {
        "name": "学生宿舍五号楼",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}学生宿舍区",
        "longitude": 115.8649,
        "latitude": 28.7383,
        "description": "南区学生宿舍楼，门禁时间以宿舍管理规定为准。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "学生公寓24号楼",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}学生公寓24号楼",
        "longitude": 115.8672,
        "latitude": 28.7406,
        "description": "南区学生公寓，楼下靠近学生第三食堂。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "南区收发室快递点",
        "type": "快递点",
        "address": f"{CAMPUS_ADDRESS}南区收发室旁",
        "longitude": 115.8642,
        "latitude": 28.7389,
        "description": "收发室旁设有快递代收点，取件请携带取件码。",
        "opening_hours": "09:00—19:00（以网点公告为准）",
    },
    {
        "name": "校内快递寄存点",
        "type": "快递点",
        "address": f"{CAMPUS_ADDRESS}校内",
        "longitude": 115.8657,
        "latitude": 28.7378,
        "description": "校内快递寄存分部，支持常见快递代收。",
        "opening_hours": "以网点公告为准",
    },
]

# 补充点位：覆盖南北区的食堂、停车点、教学楼、学院楼、宿舍楼。
# 坐标为按官方平面图布局估算的近似位置，精确位置可在后台校准。
MORE_POINTS: list[dict[str, object]] = [
    # ---------- 南区 ----------
    {
        "name": "南区正门停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}正门",
        "longitude": 115.8668,
        "latitude": 28.7359,
        "description": "南区正门附近的机动车停车区域。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "南区教学区停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8664,
        "latitude": 28.7391,
        "description": "教学区周边停车位，上下课时段车位较紧张。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "南区图书馆停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}图书馆附近",
        "longitude": 115.8655,
        "latitude": 28.7402,
        "description": "图书馆附近的停车区域，方便自习和借阅。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "南区体育场停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}体育区",
        "longitude": 115.8670,
        "latitude": 28.7376,
        "description": "体育场旁停车区域，赛事和活动期间较拥挤。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "南区公共教学楼",
        "type": "教学楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8659,
        "latitude": 28.7390,
        "description": "承担公共基础课教学的教学楼群。",
        "opening_hours": "教学时段开放",
    },
    {
        "name": "土木建筑学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8647,
        "latitude": 28.7399,
        "description": "土木建筑学院办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "电气与自动化工程学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8674,
        "latitude": 28.7398,
        "description": "电气与自动化工程学院办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "机电与车辆工程学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8680,
        "latitude": 28.7390,
        "description": "机电与车辆工程学院办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "信息工程学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8677,
        "latitude": 28.7383,
        "description": "信息工程学院办公、实验与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "经济管理学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8639,
        "latitude": 28.7393,
        "description": "经济管理学院办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "外国语学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8635,
        "latitude": 28.7388,
        "description": "外国语学院办公与语言教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "艺术学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}教学区",
        "longitude": 115.8632,
        "latitude": 28.7398,
        "description": "艺术学院办公与专业教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "南区学生宿舍1栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}学生宿舍区",
        "longitude": 115.8646,
        "latitude": 28.7381,
        "description": "南区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "南区学生宿舍2栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}学生宿舍区",
        "longitude": 115.8652,
        "latitude": 28.7378,
        "description": "南区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "南区学生宿舍3栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}学生宿舍区",
        "longitude": 115.8659,
        "latitude": 28.7376,
        "description": "南区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    # ---------- 北区 ----------
    {
        "name": "北区第一食堂",
        "type": "食堂",
        "address": f"{CAMPUS_ADDRESS}北区",
        "longitude": 115.8642,
        "latitude": 28.7472,
        "description": "北区学生食堂，靠近宿舍区。",
        "opening_hours": "以现场公告为准",
    },
    {
        "name": "北区第二食堂",
        "type": "食堂",
        "address": f"{CAMPUS_ADDRESS}北区",
        "longitude": 115.8663,
        "latitude": 28.7468,
        "description": "北区第二食堂，提供多种档口。",
        "opening_hours": "以现场公告为准",
    },
    {
        "name": "北区正门停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}北区正门",
        "longitude": 115.8651,
        "latitude": 28.7448,
        "description": "北区正门附近停车区域。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "北区体育场停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}北区体育区",
        "longitude": 115.8638,
        "latitude": 28.7492,
        "description": "北区体育场旁停车区域。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "北区宿舍区停车场",
        "type": "停车点",
        "address": f"{CAMPUS_ADDRESS}北区宿舍区",
        "longitude": 115.8668,
        "latitude": 28.7479,
        "description": "北区学生宿舍区周边停车位。",
        "opening_hours": "全天开放（按校内车辆管理规定）",
    },
    {
        "name": "北区教学楼",
        "type": "教学楼",
        "address": f"{CAMPUS_ADDRESS}北区教学区",
        "longitude": 115.8650,
        "latitude": 28.7475,
        "description": "北区公共教学楼，承担部分课程教学。",
        "opening_hours": "教学时段开放",
    },
    {
        "name": "软件学院（VR学院）楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}北区",
        "longitude": 115.8660,
        "latitude": 28.7482,
        "description": "软件学院（VR学院）办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "理学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}北区",
        "longitude": 115.8637,
        "latitude": 28.7479,
        "description": "理学院办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "人文社会科学学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}北区",
        "longitude": 115.8670,
        "latitude": 28.7472,
        "description": "人文社会科学学院办公与教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "国际学院楼",
        "type": "学院楼",
        "address": f"{CAMPUS_ADDRESS}北区",
        "longitude": 115.8633,
        "latitude": 28.7470,
        "description": "国际学院办公与国际项目教学场所。",
        "opening_hours": "工作日办公时间",
    },
    {
        "name": "北区学生宿舍1栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}北区宿舍区",
        "longitude": 115.8645,
        "latitude": 28.7486,
        "description": "北区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "北区学生宿舍2栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}北区宿舍区",
        "longitude": 115.8651,
        "latitude": 28.7489,
        "description": "北区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "北区学生宿舍3栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}北区宿舍区",
        "longitude": 115.8657,
        "latitude": 28.7492,
        "description": "北区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "北区学生宿舍4栋",
        "type": "宿舍",
        "address": f"{CAMPUS_ADDRESS}北区宿舍区",
        "longitude": 115.8662,
        "latitude": 28.7495,
        "description": "北区学生宿舍楼。",
        "opening_hours": "门禁以宿舍管理规定为准",
    },
    {
        "name": "北区田径场",
        "type": "场馆",
        "address": f"{CAMPUS_ADDRESS}北区体育区",
        "longitude": 115.8648,
        "latitude": 28.7498,
        "description": "北区室外田径场，适合跑步锻炼。",
        "opening_hours": "室外场地全天开放",
    },
    {
        "name": "北区快递服务点",
        "type": "快递点",
        "address": f"{CAMPUS_ADDRESS}北区生活区",
        "longitude": 115.8655,
        "latitude": 28.7463,
        "description": "北区快递代收点，取件请携带取件码。",
        "opening_hours": "以网点公告为准",
    },
]


def seed_points() -> tuple[int, int]:
    """写入或更新演示点位，返回（新增数，更新数）。"""
    created = 0
    updated = 0
    with SessionLocal() as db:
        for point_data in DEMO_POINTS + MORE_POINTS:
            existing = db.scalar(select(Point).where(Point.name == point_data["name"]))
            if existing is None:
                db.add(Point(**point_data))
                created += 1
                continue
            for field_name, value in point_data.items():
                setattr(existing, field_name, value)
            updated += 1
        db.commit()
    return created, updated


def seed_documents() -> tuple[int, int]:
    """把演示文档切片入库，返回（新增文档数，跳过数）。"""
    document_files = sorted(SEED_DOCS_DIR.glob("*.md"))
    if not document_files:
        print(f"未找到演示文档目录：{SEED_DOCS_DIR}")
        return 0, 0

    rag_service = RAGService()
    ingested = 0
    skipped = 0

    with SessionLocal() as db:
        for file_path in document_files:
            filename = file_path.name
            if db.scalar(select(Document).where(Document.filename == filename)) is not None:
                skipped += 1
                print(f"跳过已入库文档：{filename}")
                continue

            chunks = process_document(file_path)
            document = Document(filename=filename, chapter_count=len(chunks))
            db.add(document)
            db.flush()
            rag_service.add_chunks(chunks, filename, str(document.id))
            db.commit()
            ingested += 1
            print(f"已入库：{filename}（{len(chunks)} 个切片）")

    return ingested, skipped


def main() -> None:
    """执行数据库初始化与演示数据导入。"""
    init_db()
    created, updated = seed_points()
    print(f"点位导入完成：新增 {created} 个，更新 {updated} 个")

    ingested, skipped = seed_documents()
    print(f"文档导入完成：新增 {ingested} 篇，跳过 {skipped} 篇")
    print("演示数据准备完毕，可启动服务进行问答与地图演示。")


if __name__ == "__main__":
    main()
