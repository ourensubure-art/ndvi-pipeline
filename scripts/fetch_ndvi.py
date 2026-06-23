"""
NDVI 周度拉取脚本
- 从 Google Earth Engine 拉 Sentinel-2 数据
- 计算上一周的 NDVI 统计
- 写入 Supabase ndvi_weekly 表
"""
import os
import json
import datetime as dt
import ee
from supabase import create_client

# ============ 配置 ============
PASTURE_ID = "pasture_001"
PASTURE_COORDS = [
    [107.8685, 39.1126],
    [107.8987, 39.1126],
    [107.8987, 39.1360],
    [107.8685, 39.1360],
    [107.8685, 39.1126],  # 闭合
]
SCALE = 10                  # Sentinel-2 原生分辨率
CLOUD_THRESHOLD = 60        # 整景云量阈值 %
TABLE_NAME = "ndvi_weekly"


# ============ 初始化 ============
def init_ee():
    sa_key_json = os.environ["GEE_SA_KEY"]
    key_data = json.loads(sa_key_json)
    credentials = ee.ServiceAccountCredentials(
        key_data["client_email"],
        key_data=sa_key_json,
    )
    ee.Initialize(credentials)
    print(f"✅ GEE 已连接：{key_data['client_email']}")


def init_supabase():
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    print(f"✅ Supabase 已连接：{url}")
    return create_client(url, key)


# ============ NDVI 计算 ============
def mask_s2_clouds(image):
    """用 SCL 波段去云、去阴影、去雪。"""
    scl = image.select("SCL")
    mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return image.updateMask(mask)


def compute_ndvi_for_week(geometry, start_str, end_str):
    """计算指定时间范围内的 NDVI 平均/标准差/像素数/影像数。"""
    collection = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geometry)
        .filterDate(start_str, end_str)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", CLOUD_THRESHOLD))
        .map(mask_s2_clouds)
    )

    image_count = collection.size().getInfo()
    print(f"   匹配影像数：{image_count}")

    if image_count == 0:
        return {
            "ndvi_mean": None,
            "ndvi_std": None,
            "pixel_count": 0,
            "image_count": 0,
        }

    composite = collection.median()
    ndvi = composite.normalizedDifference(["B8", "B4"]).rename("NDVI")

    reducer = (
        ee.Reducer.mean()
        .combine(ee.Reducer.stdDev(), sharedInputs=True)
        .combine(ee.Reducer.count(), sharedInputs=True)
    )

    stats = ndvi.reduceRegion(
        reducer=reducer,
        geometry=geometry,
        scale=SCALE,
        maxPixels=int(1e9),
    ).getInfo()

    return {
        "ndvi_mean": stats.get("NDVI_mean"),
        "ndvi_std": stats.get("NDVI_stdDev"),
        "pixel_count": int(stats.get("NDVI_count") or 0),
        "image_count": image_count,
    }


# ============ 时间窗口 ============
def get_target_week():
    """返回上一个完整周的 [周一, 下周一)。"""
    today = dt.date.today()
    this_monday = today - dt.timedelta(days=today.weekday())
    week_start = this_monday - dt.timedelta(days=7)
    week_end = this_monday
    return week_start, week_end


# ============ 主流程 ============
def main():
    init_ee()
    sb = init_supabase()

    week_start, week_end = get_target_week()
    print(f"\n📅 目标周：{week_start} ~ {week_end}")

    geom = ee.Geometry.Polygon([PASTURE_COORDS])
    stats = compute_ndvi_for_week(geom, str(week_start), str(week_end))
    print(f"📊 计算结果：{stats}")

    iso_year, iso_week, _ = week_start.isocalendar()
    doy = week_start.timetuple().tm_yday

    row = {
        "pasture_id": PASTURE_ID,
        "week_start": str(week_start),
        "week_end": str(week_end),
        "year": iso_year,
        "week_of_year": iso_week,
        "doy": doy,
        "ndvi_mean": stats["ndvi_mean"],
        "ndvi_std": stats["ndvi_std"],
        "pixel_count": stats["pixel_count"],
        "image_count": stats["image_count"],
    }

    existing = (
        sb.table(TABLE_NAME)
        .select("id")
        .eq("pasture_id", PASTURE_ID)
        .eq("week_start", str(week_start))
        .execute()
    )

    if existing.data:
        row_id = existing.data[0]["id"]
        sb.table(TABLE_NAME).update(row).eq("id", row_id).execute()
        print(f"\n✅ 已更新 id={row_id}")
    else:
        sb.table(TABLE_NAME).insert(row).execute()
        print(f"\n✅ 已新增一行")


if __name__ == "__main__":
    main()
