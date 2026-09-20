# -*- coding: utf-8 -*-
"""
主流程入口
----------
核心链路：检索厦大就业网（站点多维筛选）-> 详情页补专业 -> 匹配打分
          -> 全量入库 -> 按用户条件筛选 -> 导出 Excel

抓取策略说明（2026-09-20 改造）：
    旧版本用「种子职位 ID ±N 双向枚举」抓数据，那是错的——厦大就业网
    自带公开的检索接口 /job/search，支持城市/学历/工作性质/薪资/发布
    时间/需求专业等 13 个筛选维度。现在改为：
        第 1 段  按条件遍历检索页（约 16 页拿到厦门全部本科岗位）
        第 2 段  对每个职位抓详情页补「需求专业」
        第 3 段  本地关键词加权打分（站点算不出综合匹配度，这部分保留）
    详见 spiders/xmu_career.py 头部说明。

设计要点：
    入库的是**全集**，导出的才是**筛选集**。
    这样调筛选条件时不需要重新抓取（抓一次要几分钟，重筛是毫秒级），
    用 --skip-crawl 就能在已有数据上反复试条件。

用法：
    python main.py                          # 检索 + 打分 + 默认筛选 + 导出
    python main.py --search-major 不限       # 放宽专业限制，拉全量
    python main.py --skip-crawl --city 厦门  # 不联网，重新筛一遍
    python main.py --stats                  # 只看数据库概览
    python main.py -h                       # 查看全部参数
"""
import sys
import logging
import argparse
import importlib.util
from datetime import datetime

# 第三方依赖清单：(import 名, pip 包名, 用途)
REQUIRED_DEPS = [
    ("requests", "requests", "网络请求"),
    ("bs4", "beautifulsoup4", "HTML 解析"),
    ("lxml", "lxml", "HTML 解析后端"),
    ("pandas", "pandas", "数据处理与导出"),
    ("openpyxl", "openpyxl", "Excel 读写引擎"),
]


def check_dependencies() -> None:
    """
    启动前的依赖自检。

    为什么必须放在所有 import 之前：
        core.exporter 在导入时就会 `import pandas`，如果缺包，用户看到的是一段
        ModuleNotFoundError 的 traceback，信息量几乎为零——既不知道哪个解释器在跑，
        也不知道该怎么修。这里提前拦住，直接告诉用户当前解释器和修复命令。
    """
    missing = []
    for module_name, package_name, purpose in REQUIRED_DEPS:
        try:
            __import__(module_name)
        except ImportError:
            missing.append((module_name, package_name, purpose))

    if not missing:
        return

    print("=" * 62)
    print("  [启动失败] 当前 Python 环境缺少运行依赖")
    print("=" * 62)
    print(f"  正在使用的解释器：{sys.executable}")
    print()
    print("  缺少以下模块：")
    for module_name, package_name, purpose in missing:
        print(f"    - {module_name:<10} pip 包名 {package_name:<16} {purpose}")
    print()
    print("  【推荐】用项目自带的虚拟环境（依赖已装好，无需配置）")
    print("      在本目录下执行：")
    print("      run.bat --skip-crawl --city 厦门 --min-score 16 --level S,A")
    print()

    # 备选方案只在当前解释器确实有 pip 时才给出，
    # 否则用户照抄一条注定失败的命令，反而多踩一次坑。
    if importlib.util.find_spec("pip") is not None:
        print("  【备选】给当前这个解释器补装依赖：")
        print(f'      "{sys.executable}" -m pip install -r requirements.txt')
    else:
        print("  【注意】当前解释器没有安装 pip，无法自行补装依赖，")
        print("          请直接使用上面推荐的 run.bat。")
    print()
    print("  为什么不能直接用 python main.py：见「运行说明.md」的《常见报错》一节。")
    print("=" * 62)
    sys.exit(1)


check_dependencies()

# 以下导入依赖上面的自检，缺包时不会执行到这里
from config import TARGET_CITY, MATCH_LEVELS, SEARCH_MAX_PAGES  # noqa: E402
from core.storage import JobStorage  # noqa: E402
from core.matcher import JobMatcher  # noqa: E402
from core.filters import JobFilter  # noqa: E402
from core.exporter import ExcelExporter  # noqa: E402
from spiders.xmu_career import XmuCareerSpider, SearchProfile  # noqa: E402


EXAMPLES = """\
使用示例
--------
  # 首次运行：按默认口径检索（厦门+本科+全职+数学/统计类）-> 打分 -> 入库 -> 导出
  python main.py --city 厦门

  # 切换抓取策略后建议先清库：旧版按职位 ID 枚举抓到的数据城市随机，
  # 混在库里会污染结果（实测出现过「深圳 11 条、北京 6 条」）
  python main.py --reset-db --city 厦门

  # 放宽专业限制，把厦门所有本科岗位都拉下来（约 320 条，16 页）
  python main.py --search-major 不限

  # 只要近一周发布的
  python main.py --search-time 近1周

  # 快跑一遍看看流程（只抓 2 页列表 + 前 10 条详情）
  python main.py --max-pages 2 --detail-limit 10

  # 不抓详情页（快，但缺「需求专业」，匹配分不准）
  python main.py --no-detail

  # 筛选条件反复调：不重新爬（秒级，不联网）
  python main.py --skip-crawl --city 厦门 --min-score 16 --level S,A

  # 把一套条件存成方案，以后直接复用
  python main.py --save-search xiamen.json
  python main.py --search-file xiamen.json
  python main.py --city 厦门 --min-score 16 --level S,A --save-filter filter.json
  python main.py --skip-crawl --filter-file filter.json

  # 看数据库概览
  python main.py --stats
"""


def setup_logging(verbose: bool = False):
    """配置日志格式"""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def split_csv(text: str):
    """把 '厦门, 福州' 这类逗号分隔参数拆成列表"""
    if not text:
        return []
    return [x.strip() for x in str(text).split(",") if x.strip()]


def build_filter(args) -> JobFilter:
    """
    组装筛选条件。

    优先级：命令行显式参数 > --filter-file 指定的方案文件 > 代码默认值。
    因此所有命令行参数的 default 都是 None，只有用户真的写了才覆盖文件里的值。
    """
    job_filter = JobFilter.load(args.filter_file) if args.filter_file else JobFilter()

    if args.city is not None:
        job_filter.city = split_csv(args.city)
    if args.min_score is not None:
        job_filter.min_score = args.min_score
    if args.level is not None:
        job_filter.levels = [x.upper() for x in split_csv(args.level)]
    if args.keyword is not None:
        job_filter.keyword = args.keyword.strip()
    if args.major is not None:
        job_filter.major = args.major.strip()
    if args.education is not None:
        job_filter.education = args.education.strip()
    if args.salary_min is not None:
        job_filter.salary_min = args.salary_min
    if args.exclude is not None:
        job_filter.exclude = split_csv(args.exclude)
    if args.top is not None:
        job_filter.top = args.top
    if args.all_master:
        job_filter.bachelor_only = False

    return job_filter


def build_profile(args) -> SearchProfile:
    """
    组装站点检索条件。

    优先级：命令行显式参数 > --search-file 指定的方案文件 > 代码默认值。
    与 build_filter 同样的设计：所有命令行参数 default 都是 None，
    只有用户真的写了才覆盖文件里的值。
    """
    profile = (SearchProfile.load(args.search_file) if args.search_file
               else SearchProfile.for_luge())

    if args.search_city is not None:
        profile.city = args.search_city.strip()
    if args.search_education is not None:
        profile.education = args.search_education.strip()
    if args.search_category is not None:
        profile.category = args.search_category.strip()
    if args.search_time is not None:
        profile.time_range = args.search_time.strip()
    if args.search_major is not None:
        raw = args.search_major.strip()
        # 「不限」= 不加专业条件，把该城市该学历的岗位全拉下来
        profile.majors = [] if raw in ("不限", "all", "*", "") else split_csv(raw)
    if args.search_salary_min is not None:
        profile.salary_min = args.search_salary_min
    if args.max_pages is not None:
        profile.max_pages = args.max_pages

    return profile


def build_export_name(job_filter: JobFilter, explicit: str = None) -> str:
    """
    根据筛选条件生成导出文件名。

    为什么要带条件而不是只用时间戳：
        调筛选条件时会连着跑好几次，如果文件名只精确到分钟，后一次会把
        前一次的结果覆盖掉。带上条件标签后，不同条件的结果各存一份，
        而且看文件名就知道这份清单是哪套条件下跑出来的。
    """
    if explicit:
        return explicit if explicit.lower().endswith(".xlsx") else explicit + ".xlsx"

    parts = ["-".join(job_filter.city) if job_filter.city else TARGET_CITY]
    if job_filter.levels:
        parts.append("".join(job_filter.levels))
    if job_filter.min_score:
        parts.append(f"min{job_filter.min_score}")
    parts.append(datetime.now().strftime("%Y%m%d_%H%M%S"))
    return "岗位清单_" + "_".join(parts) + ".xlsx"


def print_banner(profile: SearchProfile, skip_crawl: bool,
                 fetch_detail: bool = True):
    """打印启动信息"""
    print("=" * 62)
    print("  厦门在招岗位爬取与匹配筛选系统")
    print(f"  数据来源：厦门大学就业信息网 | 目标城市：{TARGET_CITY}")
    if skip_crawl:
        print("  模式：跳过抓取（在库中已有数据上重新筛选）")
    else:
        print("  检索条件：" + " | ".join(profile.describe()))
        if not fetch_detail:
            print("  ★ 已关闭详情页抓取：缺少「需求专业」，匹配分仅供参考")
    print("=" * 62)


def run_pipeline(job_filter: JobFilter, profile: SearchProfile,
                 skip_crawl: bool = False, out_name: str = None,
                 fetch_detail: bool = True, detail_limit: int = None):
    """执行完整流水线"""
    storage = JobStorage()
    exporter = ExcelExporter()

    total_steps = 3 if skip_crawl else 5
    counter = {"n": 0}

    def step(title: str):
        counter["n"] += 1
        print(f"\n[{counter['n']}/{total_steps}] {title} ...")

    # ---------- 取数据 ----------
    if skip_crawl:
        step("载入库中数据")
        total = storage.count()
        print(f"      库中现有 {total} 条岗位")
        if total == 0:
            print("      库为空。请先运行一次抓取：python main.py")
            return
    else:
        # 第 1 段 + 第 2 段：站点检索 + 详情页补专业
        step("检索岗位（使用站点自带的多维筛选）")
        print("      检索条件：" + " | ".join(profile.describe()))
        spider = XmuCareerSpider(
            profile=profile,
            fetch_detail=fetch_detail,
            detail_limit=detail_limit,
        )
        raw_jobs = spider.run()

        print(f"      取到 {len(raw_jobs)} 条岗位"
              f"（列表请求 {spider.stats['list_requests']} 次，"
              f"详情请求 {spider.stats['detail_requests']} 次）")
        if fetch_detail:
            print(f"      其中取到「需求专业」的 {spider.stats['detail_major_found']} 条")
        if not raw_jobs:
            print("      未取到任何岗位，流程结束。")
            print("      排查建议：确认检索条件拼写（城市/学历/性质用中文名），"
                  "或先跑 python -m spiders.xmu_career 自测。")
            return

        step("专业匹配度打分")
        matcher = JobMatcher()
        scored_jobs = matcher.score_jobs(raw_jobs)

        dist = {}
        for j in scored_jobs:
            dist[j.match_level] = dist.get(j.match_level, 0) + 1
        label_map = {lv: lb for _, lv, lb in MATCH_LEVELS}
        print("      等级分布：", end="")
        print(" | ".join(
            f"{lv}({label_map.get(lv, '')})={dist.get(lv, 0)}"
            for lv in ["S", "A", "B", "C"]
        ))

        step("入库（按 公司+岗位 去重）")
        inserted = storage.save_jobs(scored_jobs)
        print(f"      新增 {inserted} 条，重复跳过 {len(scored_jobs) - inserted} 条")
        print(f"      库中累计 {storage.count()} 条（入库为全集，筛选只影响导出结果）")

    # ---------- 条件筛选 ----------
    step("按条件筛选")
    print("      条件：" + " | ".join(job_filter.describe()))
    rows = storage.query_jobs(job_filter=job_filter)
    print(f"      命中 {len(rows)} 条 / 库中共 {storage.count()} 条")

    if not rows:
        print("\n      筛选结果为 0 条，已跳过导出。")
        print("      可尝试放宽条件：调低 --min-score、去掉 --city，"
              "或用 python main.py --stats 看看库里实际有哪些数据。")
        return

    # ---------- 导出 ----------
    step("导出 Excel")
    out_path = exporter.export(
        rows, filename=build_export_name(job_filter, out_name)
    )
    print(f"      已导出：{out_path}")

    # ---------- 结果预览 ----------
    preview_n = min(10, len(rows))
    print("\n" + "-" * 62)
    print(f"  筛选结果预览（前 {preview_n} 条 / 共 {len(rows)} 条）")
    print("-" * 62)
    for job in rows[:preview_n]:
        print(f"  [{job['match_level']}] {job['match_score']:>3}分  "
              f"{job['company']} - {job['title']}")
        print(f"        城市：{job['city'] or '未标注'} | "
              f"薪资：{job['salary'] or '未标注'} | "
              f"命中：{job['hit_keywords'] or '无'}")
    print("-" * 62)

    # ---------- 城市分布（帮忙判断条件是否过窄）----------
    city_dist = {}
    for job in rows:
        key = (job["city"] or "未标注")
        city_dist[key] = city_dist.get(key, 0) + 1
    top_cities = sorted(city_dist.items(), key=lambda x: -x[1])[:5]
    print("\n  结果城市分布：" + " | ".join(f"{c}={n}" for c, n in top_cities))

    # 城市筛选是有损的，明确告知丢掉了多少，避免用户以为库里就这么点数据
    if job_filter.city:
        excluded = storage.count() - len(rows)
        if excluded > 0:
            print(f"  已按城市筛选，另有 {excluded} 条非目标城市岗位未列入"
                  "（去掉 --city 可一并看到）")


def show_stats():
    """查看库中数据概览"""
    storage = JobStorage()
    total = storage.count()

    if total == 0:
        print("数据库为空，请先运行：python main.py")
        return

    print("=" * 50)
    print(f"  数据库统计（共 {total} 条岗位）")
    print("=" * 50)

    label_map = {lv: lb for _, lv, lb in MATCH_LEVELS}
    for level, cnt in storage.count_by_level().items():
        print(f"  {level} 级（{label_map.get(level, '')}）：{cnt} 条")

    print("\n  城市分布（前 8）：")
    for city, cnt in storage.count_by_city():
        print(f"    {city or '未标注'}：{cnt} 条")

    print("\n  Top 10 高匹配岗位：")
    for job in storage.query_jobs(limit=10):
        print(f"  [{job['match_level']}] {job['match_score']:>3}分  "
              f"{job['company']} - {job['title']}")

    print("\n  提示：加 --skip-crawl 可在这些数据上直接套用筛选条件，无需重新爬取。")


def rescore_all():
    """
    按当前打分规则重算库中全部岗位的匹配分（不联网）。

    为什么需要单独一个入口：save_jobs 用 INSERT OR IGNORE 去重，
    改完 config 的权重表直接重跑，已有记录的分数不会变。
    以前只能清库重抓（约 9 分钟联网），现在秒级完成。

    这是「数据与算法解耦」的直接收益：原始数据不动，只重算派生字段。
    """
    storage = JobStorage()
    total = storage.count()
    if total == 0:
        print("数据库为空，无需重打分。请先运行：python main.py")
        return

    print("=" * 62)
    print("  重新打分（按当前 config 权重表，不联网）")
    print(f"  库中 {total} 条")
    print("=" * 62)

    jobs = storage.load_all_jobs()

    before = {}
    for j in jobs:
        before[j.match_level] = before.get(j.match_level, 0) + 1

    scored = JobMatcher().score_jobs(jobs)
    updated = storage.update_match_scores(scored)

    after = {}
    for j in scored:
        after[j.match_level] = after.get(j.match_level, 0) + 1

    label_map = {lv: lb for _, lv, lb in MATCH_LEVELS}
    print(f"\n  已更新 {updated} 条，等级分布变化：")
    for lv in ["S", "A", "B", "C"]:
        b, a = before.get(lv, 0), after.get(lv, 0)
        mark = "  " if b == a else ("+" if a > b else "-")
        print(f"    {lv}({label_map.get(lv, '')})  {b:>4}  ->  {a:>4}   {mark}")

    # 顺带报出本次识别到的数据缺失条数（不是错误，是数据质量指标）
    missing = sum(1 for j in scored
                  if any("专业字段缺失" in h for h in (j.hit_keywords or [])))
    if missing:
        print(f"\n  注：{missing} 条岗位的「需求专业」字段为空，"
              "已标记为【专业字段缺失】而非判为不匹配。")

    print("\n  完成。查看新结果：python main.py --skip-crawl --min-score 8")


def build_parser():
    parser = argparse.ArgumentParser(
        description="厦门在招岗位爬取与匹配筛选系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )

    # ---- 运行模式 ----
    mode = parser.add_argument_group("运行模式")
    mode.add_argument(
        "--skip-crawl", action="store_true",
        help="跳过抓取，直接在库中已有数据上重新筛选（秒级，不联网）"
    )
    mode.add_argument(
        "--stats", action="store_true",
        help="只查看数据库概览，不执行筛选与导出"
    )
    mode.add_argument(
        "--reset-db", action="store_true",
        help="抓取前清空数据库（旧版 ID 枚举抓到的数据城市随机，建议清一次重抓）"
    )
    mode.add_argument(
        "--rescore", action="store_true",
        help="不联网，按当前打分规则重算库中已有岗位的匹配分"
             "（改了 config 的权重表后用这个更新，不必重抓）"
    )
    mode.add_argument(
        "-v", "--verbose", action="store_true",
        help="输出调试日志"
    )

    # ---- 站点检索条件（决定「向站点要哪一批岗位」）----
    # 这些参数直接映射到厦大就业网 /job/search 的筛选字段，
    # 在抓取阶段就把范围收敛好，比先抓全国再本地过滤高效得多。
    s2 = parser.add_argument_group("站点检索条件（抓取阶段生效，用中文名）")
    s2.add_argument(
        "--search-city", metavar="城市",
        help="检索城市名称，如「厦门」「福州」「福建」（默认 厦门）"
    )
    s2.add_argument(
        "--search-education", metavar="学历",
        help="学历要求：不限/本科/硕士/博士（默认 本科）"
    )
    s2.add_argument(
        "--search-category", metavar="性质",
        help="工作性质：不限/全职/实习（默认 全职）"
    )
    s2.add_argument(
        "--search-major", metavar="专业",
        help="需求专业，多个用逗号分隔，如「信息与计算科学,数学类」；"
             "填「不限」表示不加专业条件（默认 信息与计算科学,数学类,统计学）"
    )
    s2.add_argument(
        "--search-time", metavar="时段",
        help="发布时间：不限/近1天/近3天/近1周/近2周/近1月/近2月（默认 不限）"
    )
    s2.add_argument(
        "--search-salary-min", type=int, metavar="元",
        help="站点侧最低月薪，如 8000"
    )
    s2.add_argument(
        "--max-pages", type=int, metavar="N",
        help=f"最多翻 N 页列表（每页 20 条，默认上限 {SEARCH_MAX_PAGES}）"
    )
    s2.add_argument(
        "--no-detail", action="store_true",
        help="不抓详情页（快，但缺「需求专业」，匹配分不准）"
    )
    s2.add_argument(
        "--detail-limit", type=int, metavar="N",
        help="只对前 N 条抓详情页，用于快速试跑"
    )
    s2.add_argument(
        "--search-file", metavar="FILE",
        help="从 JSON 文件载入检索方案（命令行参数仍可覆盖）"
    )
    s2.add_argument(
        "--save-search", metavar="FILE",
        help="把本次检索条件保存为 JSON 方案文件"
    )

    # ---- 筛选条件 ----
    f = parser.add_argument_group("筛选条件（不填则使用默认值）")
    f.add_argument("--city", metavar="城市",
                   help="城市，多个用逗号分隔，如「厦门」或「厦门,福州」")
    f.add_argument("--min-score", type=int, metavar="N",
                   help="最低匹配分（S≥25 / A≥16 / B≥8）")
    f.add_argument("--level", metavar="等级",
                   help="匹配等级，多个用逗号分隔，如「S,A」")
    f.add_argument("--keyword", metavar="词",
                   help="关键词，匹配公司名或岗位名")
    f.add_argument("--major", metavar="词",
                   help="专业要求必须包含的字样，如「数学」")
    f.add_argument("--education", metavar="词",
                   help="学历要求必须包含的字样，如「本科」")
    f.add_argument("--salary-min", type=int, metavar="元",
                   help="最低月薪（元），如 8000。薪资未标注的岗位不会被剔除")
    f.add_argument("--exclude", metavar="词",
                   help="排除关键词，多个用逗号分隔，如「销售,客服」")
    f.add_argument("--top", type=int, metavar="N",
                   help="最多输出 N 条")
    f.add_argument("--all-master", action="store_true",
                   help="不剔除「仅招硕士/博士」的岗位（默认剔除）")

    # ---- 方案文件 ----
    s = parser.add_argument_group("筛选方案（存下来反复用）")
    s.add_argument("--save-filter", metavar="FILE",
                   help="把本次筛选条件保存为 JSON 方案文件")
    s.add_argument("--filter-file", metavar="FILE",
                   help="从 JSON 方案文件载入条件（命令行参数仍可覆盖）")
    s.add_argument("--out", metavar="NAME",
                   help="指定导出文件名（默认按条件自动命名）")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.stats:
        show_stats()
        return 0

    if args.rescore:
        rescore_all()
        return 0

    if args.reset_db:
        removed = JobStorage().clear()
        print(f"已清空数据库：删除 {removed} 条历史岗位。")

    job_filter = build_filter(args)
    profile = build_profile(args)

    if args.save_filter:
        path = job_filter.save(args.save_filter)
        print(f"筛选方案已保存：{path}")
    if args.save_search:
        path = profile.save(args.save_search)
        print(f"检索方案已保存：{path}")

    fetch_detail = not args.no_detail
    print_banner(profile, args.skip_crawl, fetch_detail)
    run_pipeline(job_filter, profile, args.skip_crawl, args.out,
                 fetch_detail, args.detail_limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
