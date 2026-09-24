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
import json
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
from config import (  # noqa: E402
    TARGET_CITY, MATCH_LEVELS, SEARCH_MAX_PAGES, VERSION, VERSION_DATE,
    DETAIL_REFRESH_TTL_DAYS, MARK_MISSING_INACTIVE, INCREMENTAL_ENABLED,
)
from core.storage import JobStorage  # noqa: E402
from core.ods import OdsRepository, new_batch_id  # noqa: E402
from core.incremental import (  # noqa: E402
    plan_detail_fetch, summarize_plan, REASON_NEW, REASON_CHANGED,
)
from core.dwd import DwdRepository  # noqa: E402
from core.matcher import JobMatcher  # noqa: E402
from core.filters import JobFilter  # noqa: E402
from core.exporter import ExcelExporter  # noqa: E402
from spiders.xmu_career import (  # noqa: E402
    XmuCareerSpider, SearchProfile, profile_scope, describe_scope,
)


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

数据分层与增量（2026-09-21 新增）
-----------------------------------
  # 默认就是增量：第一次跑会全量抓详情（约 8 分钟），
  # 之后每次只抓「新增 / 信息有变 / 缺专业字段 / 超期」的岗位，通常 1 分钟内
  python main.py --city 厦门

  # 看 ODS 贴源层存了什么、历次批次跑得怎么样
  python main.py --ods-stats

  # 看某个岗位的历史轨迹：薪资/专业要求什么时候变的
  # （职位 ID 就是原文链接 /job/view/id/ 后面那串数字）
  python main.py --ods-history 2401083

  # 看最近 7 天有哪些岗位信息发生了变化
  python main.py --ods-changes
  python main.py --ods-changes 30         # 看最近 30 天

  # 从 ODS 离线重建最新状态层（不联网、几秒钟）
  python main.py --rebuild-state

  # 怀疑数据没更新时，强制全部重抓详情
  python main.py --full-refresh --search-major 不限

  # 只要还在招的岗位（排除本批列表未再出现的）
  python main.py --skip-crawl --active-only --min-score 16
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
                 fetch_detail: bool = True, incremental: bool = True):
    """打印启动信息"""
    print("=" * 62)
    print(f"  厦门在招岗位爬取与匹配筛选系统  v{VERSION}")
    print(f"  数据来源：厦门大学就业信息网 | 目标城市：{TARGET_CITY}")
    print("  数据分层：ODS 贴源快照 -> 最新状态层 -> Excel 清单")
    if skip_crawl:
        print("  模式：跳过抓取（在库中已有数据上重新筛选，不联网）")
    else:
        if incremental:
            print("  模式：增量抓取（只对新增/有变化的岗位重抓详情）")
        else:
            print("  模式：全量抓取（每个岗位都重新抓详情，最慢但最全）")
        print("  检索条件：" + " | ".join(profile.describe()))
        if not fetch_detail:
            print("  ★ 已关闭详情页抓取：缺少「需求专业」，匹配分仅供参考")
    print("=" * 62)


def _last_ok_scope(ods):
    """
    上一批「成功批次」的检索口径；没有可比对象时返回 None。

    只看 status='ok' 的批次：失败批次可能连列表页都没抓完，
    它记下的口径不足以代表库里数据的来源。当前这一批此刻还是
    running（close_batch 在这之后才执行），所以天然不会被选中。
    """
    for batch in ods.recent_batches(limit=20):
        if batch.get("status") != "ok":
            continue
        scope = profile_scope(batch.get("profile_json") or "")
        if scope:
            return scope
    return None


def _can_judge_missing(profile: SearchProfile, spider, ods) -> tuple:
    """
    判断本批「列表里没再出现的岗位」能否认定为已下架。

    :return: (能否判定, 不能判定的原因)

    为什么必须判断（一个很容易犯的逻辑错误）：
        「这次列表里没有它」不等于「它下架了」。
        若检索条件是「近1周」，一周前发布的岗位本来就不会出现，
        但它们完全可能还在招。
        只有同时满足下面三条，「没出现」才等价于「下架」：
            1. 检索时间范围不限 —— 否则结果本身就是按时间截断的
            2. 翻页没有触顶     —— 否则可能只是没翻完
            3. 检索口径与建库口径一致 —— 否则"少掉的那些"根本没被问过
        不满足时只记录「本批未出现」，不碰在架状态。
        宁可漏判也不错杀：错的在架状态会让人以为岗位没了而放弃投递。

    【第 3 条为什么必须加】（2026-09-24 实测踩到）
        前两条成立、口径却不一致时，这套判定会**静默地把大半个库标死**：
        用默认的三专业口径跑了一次，列表从 294 条掉到 24 条，
        270 个岗位被记为「本批未再出现」，其中 266 条真被标成了下架。
        全程没有任何报错——因为从程序的角度看，两条前提都成立。
        而 README 里推荐的首跑命令 `python main.py --city 厦门` 用的
        正是这个更窄的默认口径，也就是说这个坑是随时会踩的。
        做法是拿本批口径去和上一批成功批次比对，不一致就只记录不判定。
    """
    if profile.time_range not in ("不限", "", "0", None):
        return False, f"检索条件限定为「{profile.time_range}」，老岗位不出现属正常"

    max_pages = profile.max_pages or SEARCH_MAX_PAGES
    total_pages = spider.stats.get("total_pages") or 1
    if total_pages >= max_pages:
        return False, f"翻页达到上限 {max_pages} 页，列表可能被截断"

    prev = _last_ok_scope(ods)
    if prev is None:
        return False, "库里没有可比较的历史检索口径，无法确认口径一致"
    current = profile_scope(profile)
    if prev != current:
        return False, (f"本批检索口径与上一批不同 —— 本批[{describe_scope(profile)}] "
                       f"/ 上批[{describe_scope(prev)}]")

    return True, ""


def run_pipeline(job_filter: JobFilter, profile: SearchProfile,
                 skip_crawl: bool = False, out_name: str = None,
                 fetch_detail: bool = True, detail_limit: int = None,
                 incremental: bool = True, full_refresh: bool = False,
                 detail_ttl: int = None, active_only: bool = False):
    """
    执行完整流水线。

    抓取模式下的分层流程（方括号里是它落在哪一层）：
        [1] 检索列表页         站点 -> 内存
        [2] 增量判定           对比 ODS 历史，决定谁需要重抓详情
        [3] 补详情页 + 回填    能省则省
        [4] 写入 ODS 贴源层    原样追加，不加工        [ODS]
        [5] 刷新最新状态层     打分 + 按 job_key 更新  [DWD]
        [6] 按条件筛选
        [7] 导出 Excel                                [ADS]

    为什么把「写 ODS」排在「刷新状态层」之前：
        顺序反了，一旦刷新中途出错，最新状态层里已经有了数据、ODS 里
        却没有对应快照，两边对不上——增量判定会以为这些岗位是新的，
        下一批又全量抓一遍。先写 ODS（只追加，基本不会失败），再刷新
        派生层，中间出错也能用 --rebuild-state 从 ODS 补回来。
    """
    storage = JobStorage()
    ods = OdsRepository()
    exporter = ExcelExporter()

    total_steps = 3 if skip_crawl else 7
    counter = {"n": 0}

    def step(title: str):
        counter["n"] += 1
        print(f"\n[{counter['n']}/{total_steps}] {title} ...")

    plan = None

    if skip_crawl:
        step("载入库中数据")
        total = storage.count()
        print(f"      最新状态层现有 {total} 条岗位"
              f"（在架 {storage.count_active()} 条）")
        print(f"      ODS 贴源层累计 {ods.count_jobs()} 个岗位 / "
              f"{ods.count_snapshots()} 条快照")
        if total == 0:
            print("      库为空。请先运行一次抓取：python main.py")
            return
    else:
        # ---------- 开批次 ----------
        batch_id = new_batch_id()
        mode = "full" if (full_refresh or not incremental) else "incremental"
        ods.open_batch(batch_id, mode,
                       json.dumps(profile.to_dict(), ensure_ascii=False))
        print(f"      抓取批次：{batch_id}（模式 {mode}）")

        # ---------- [1] 列表页 ----------
        step("检索岗位（使用站点自带的多维筛选）")
        print("      检索条件：" + " | ".join(profile.describe()))
        spider = XmuCareerSpider(
            profile=profile,
            fetch_detail=fetch_detail,
            detail_limit=detail_limit,
        )
        raw_jobs = spider.search(profile)
        if not raw_jobs:
            ods.close_batch(batch_id, status="failed", note="列表页未取到任何岗位")
            print("      未取到任何岗位，流程结束。")
            print("      排查建议：确认检索条件拼写（城市/学历/性质用中文名），"
                  "或先跑 python -m spiders.xmu_career 自测。")
            return
        print(f"      列表页取到 {len(raw_jobs)} 条岗位"
              f"（{spider.stats['total_pages']} 页，"
              f"请求 {spider.stats['list_requests']} 次）")

        # ---------- [2] 增量判定 ----------
        if fetch_detail:
            step("增量判定（对比 ODS 历史，决定哪些岗位需要重抓详情）")
            states = ods.latest_states()
            print(f"      ODS 中已记录 {len(states)} 个岗位的历史")
            plan = plan_detail_fetch(
                raw_jobs, states,
                enabled=(incremental and not full_refresh),
                ttl_days=detail_ttl,
            )
            for line in summarize_plan(plan):
                print("      " + line)

        # ---------- [3] 补详情 ----------
        step("补详情页（需求专业 / 截止时间）")
        if fetch_detail:
            spider.enrich_by_plan(plan)
        else:
            for job in raw_jobs:
                job.detail_fetched = False
            print("      已关闭详情抓取：缺「需求专业」，匹配分仅供参考")
        spider.finalize(raw_jobs)
        print(f"      详情请求 {spider.stats['detail_requests']} 次"
              f"（增量省下 {spider.stats['detail_skipped']} 次）"
              f"，取到专业字段 {spider.stats['detail_major_found']} 条")

        # ---------- [4] 写 ODS ----------
        step("写入 ODS 贴源层（原样追加，不做业务加工）")
        written = ods.save_snapshots(batch_id, raw_jobs)
        print(f"      本批写入 {written} 条快照；"
              f"ODS 累计 {ods.count_snapshots()} 条 / "
              f"{ods.count_jobs()} 个岗位")

        # ---------- [5] 刷新最新状态层 ----------
        step("刷新最新状态层（打分 + 按 job_key 更新）")
        scored_jobs = JobMatcher().score_jobs(raw_jobs)

        dist = {}
        for j in scored_jobs:
            dist[j.match_level] = dist.get(j.match_level, 0) + 1
        label_map = {lv: lb for _, lv, lb in MATCH_LEVELS}
        print("      等级分布：", end="")
        print(" | ".join(
            f"{lv}({label_map.get(lv, '')})={dist.get(lv, 0)}"
            for lv in ["S", "A", "B", "C"]
        ))

        saved = storage.save_jobs_batch(batch_id, scored_jobs)
        print(f"      新增 {saved['inserted']} 条 | 更新 {saved['updated']} 条"
              f"（其中 {saved['changed']} 条内容有变化）")
        print(f"      最新状态层累计 {storage.count()} 条"
              f"（在架 {storage.count_active()} 条）")

        # ---------- 失效判定 ----------
        if plan and plan.missing_keys:
            can_judge, why = _can_judge_missing(profile, spider, ods)
            if MARK_MISSING_INACTIVE and can_judge:
                marked = storage.mark_missing_inactive(batch_id, plan.missing_keys)
                print(f"      {len(plan.missing_keys)} 条历史岗位本批未再出现，"
                      f"已标记为失效 {marked} 条")
            else:
                print(f"      {len(plan.missing_keys)} 条历史岗位本批未再出现，"
                      f"本次不判定下架（{why}）")

        # ---------- 收批次台账 ----------
        reasons = plan.count_by_reason() if plan else {}
        ods.close_batch(
            batch_id, status="ok",
            list_pages=spider.stats["total_pages"],
            list_items=len(raw_jobs),
            new_jobs=reasons.get(REASON_NEW, 0),
            changed_jobs=reasons.get(REASON_CHANGED, 0),
            unchanged_jobs=plan.skipped_count if plan else 0,
            missing_jobs=len(plan.missing_keys) if plan else 0,
            detail_requests=spider.stats["detail_requests"],
            detail_skipped=spider.stats["detail_skipped"],
        )

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
    """查看数据源概览（最新状态层 + ODS 贴源层）"""
    storage = JobStorage()
    ods = OdsRepository()
    total = storage.count()

    if total == 0:
        if ods.count_jobs():
            print(f"最新状态层为空，但 ODS 里存有 {ods.count_jobs()} 个岗位的"
                  f"{ods.count_snapshots()} 条历史快照。")
            print("可以用它离线重建（不联网、几秒钟）：")
            print("    python main.py --rebuild-state")
        elif ods.count_snapshots() == 0:
            print("数据库为空，请先运行：python main.py")
        return

    label_map = {lv: lb for _, lv, lb in MATCH_LEVELS}

    print("=" * 58)
    print(f"  最新状态层：{total} 条岗位（在架 {storage.count_active()} 条）")
    print("=" * 58)

    for level, cnt in storage.count_by_level().items():
        print(f"  {level} 级（{label_map.get(level, '')}）：{cnt} 条")

    print("\n  城市分布（前 8）：")
    for city, cnt in storage.count_by_city():
        print(f"    {city or '未标注'}：{cnt} 条")

    life = storage.lifecycle_stats(days=7)
    print("\n  生命周期（分层后才有这些口径）：")
    print(f"    最近 7 天新增：{life['new_in_days']} 条")
    print(f"    抓取内容变化过：{life['changed']} 条")
    print(f"    已标记失效：{life['inactive']} 条")
    if life["first_seen_at"]:
        print(f"    数据跨越：{life['first_seen_at']} ~ {life['last_seen_at']}")

    print("\n  Top 10 高匹配岗位：")
    for job in storage.query_jobs(limit=10):
        print(f"  [{job['match_level']}] {job['match_score']:>3}分  "
              f"{job['company']} - {job['title']}")

    _print_ods_overview(ods)

    print("\n  提示：加 --skip-crawl 可在这些数据上直接套用筛选条件，无需重新爬取。")


def _print_ods_overview(ods: OdsRepository) -> None:
    """打印 ODS 贴源层概览"""
    stats = ods.stats()
    print("\n  ODS 贴源层（原始快照，只追加、不加工）：")
    print(f"    {stats['snapshots']} 条快照 / 覆盖 {stats['jobs']} 个岗位 / "
          f"{stats['batches']} 个抓取批次")
    if stats["first_batch_at"]:
        print(f"    时间范围：{stats['first_batch_at']} ~ {stats['last_batch_at']}")
    if stats["changed_jobs"]:
        print(f"    其中 {stats['changed_jobs']} 个岗位的抓取内容发生过变化"
              "（用 --ods-changes 查看具体是哪变了）")


def _disp_pad(text: str, width: int, align: str = "right") -> str:
    """
    按「显示宽度」对齐，而不是按字符个数。

    为什么不能用 f"{'状态':>6}"：中文一个字在终端占两列，Python 却按
    一个字符算宽度，于是表头和数据行错位。批次台账里一旦出现
    status=failed（6 字符），前一行末尾就会被挤成 `0failed`，
    看起来像程序出错，其实是排版问题。
    这里用 east_asian_width 判断宽字符（W/F 记为 2 列）再补空格。
    """
    import unicodedata

    shown = sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
                for c in str(text))
    gap = max(width - shown, 0)
    if align == "left":
        return str(text) + " " * gap
    return " " * gap + str(text)


def show_ods_stats(limit: int = 8):
    """查看 ODS 贴源层与抓取批次台账"""
    ods = OdsRepository()
    stats = ods.stats()

    if stats["snapshots"] == 0:
        print("ODS 贴源层为空。先跑一次抓取：python main.py")
        return

    print("=" * 62)
    print("  ODS 贴源层（每次抓取的原始快照，只追加）")
    print("=" * 62)
    print(f"  快照总数    ：{stats['snapshots']} 条")
    print(f"  覆盖岗位    ：{stats['jobs']} 个")
    print(f"  抓取批次    ：{stats['batches']} 个")
    print(f"  含详情的快照：{stats['snapshots_with_detail']} 条")
    print(f"  内容变化过的岗位：{stats['changed_jobs']} 个")
    if stats["first_batch_at"]:
        print(f"  数据时间范围：{stats['first_batch_at']} ~ {stats['last_batch_at']}")

    batches = ods.recent_batches(limit=limit)
    if batches:
        print(f"\n  最近 {len(batches)} 个批次：")
        header = (
            _disp_pad("批次号", 18, "left") + _disp_pad("模式", 14, "left")
            + _disp_pad("列表", 7) + _disp_pad("新增", 7)
            + _disp_pad("有变", 7) + _disp_pad("免抓详情", 11)
            + "  " + _disp_pad("状态", 8, "left")
        )
        print("    " + header)
        for b in batches:
            row = (
                _disp_pad(b["batch_id"], 18, "left")
                + _disp_pad(b["mode"] or "", 14, "left")
                + _disp_pad(b["list_items"] or 0, 7)
                + _disp_pad(b["new_jobs"] or 0, 7)
                + _disp_pad(b["changed_jobs"] or 0, 7)
                + _disp_pad(b["detail_skipped"] or 0, 11)
                + "  " + _disp_pad(b["status"] or "", 8, "left")
            )
            print("    " + row)

    print("\n  说明：")
    print("    「列表」= 本批从列表页取到的岗位数")
    print("    「新增」= 首次见到的岗位数（first_seen）")
    print("    「有变」= 列表字段与历史快照不一致、因而重抓了详情的岗位数")
    print("    「免抓详情」= 增量判定认定无变化、跳过详情请求的岗位数")
    print("\n  用 --ods-history <职位ID> 看某个岗位的历史，"
          "--ods-changes 看最近发生的变化。")


def show_ods_history(key: str):
    """查看某个岗位的全部抓取快照，看清它是怎么变的"""
    from config import ODS_SOURCE_KEY

    ods = OdsRepository()
    normalized = key if ":" in key else f"{ODS_SOURCE_KEY}:{key}"
    rows = ods.history_of(normalized)

    if not rows:
        print(f"ODS 中没有 {normalized} 的记录。")
        print("可用 --ods-stats 看看有哪些岗位，或确认职位 ID 是否正确"
              "（职位 ID 就是原文链接 /job/view/id/ 后面那串数字）。")
        return

    first = rows[0]
    print("=" * 70)
    print(f"  {first['company']} - {first['title']}")
    print(f"  job_key：{normalized}（共 {len(rows)} 条快照）")
    print("=" * 70)
    print(f"  {'#':<3}{'抓取时间':<21}{'详情':<5}{'内容指纹':<18}{'变化':<5}"
          f"{'薪资':<14}{'专业要求':<20}")

    prev_hash = None
    for i, r in enumerate(rows, 1):
        changed = ""
        if prev_hash is not None and prev_hash != r["content_hash"]:
            changed = "★变"
        prev_hash = r["content_hash"]
        major = (r["major_requirement"] or "")[:18]
        print(f"  {i:<3}{(r['crawled_at'] or ''):<21}"
              f"{('是' if r['detail_fetched'] else '否'):<5}"
              f"{(r['content_hash'] or ''):<18}{changed:<5}"
              f"{(r['salary'] or ''):<14}{major:<20}")

    print("\n  说明：「详情=否」表示该次是增量跳过、字段来自历史快照回填。")
    print("        「★变」表示这一条相对上一条的内容指纹发生了变化。")


def show_ods_changes(days: int = 7):
    """查看最近 N 天内岗位信息发生的变化"""
    ods = OdsRepository()
    rows = ods.recent_changes(days=days)

    if not rows:
        print(f"最近 {days} 天内没有检测到岗位信息变化。")
        print("说明：变化是按「内容指纹」判定的，只有公司名/标题/城市/薪资/"
              "学历/发布日期/专业要求/截止时间 这些字段真的不同才算。")
        return

    print("=" * 74)
    print(f"  最近 {days} 天内的岗位信息变化（共 {len(rows)} 处）")
    print("=" * 74)
    for r in rows:
        print(f"\n  {r['crawled_at']}  {r['company']} - {r['title']}")
        print(f"    job_key：{r['job_key']} | 薪资：{r['salary'] or '未标注'}"
              f" | 专业：{(r['major_requirement'] or '未标注')[:40]}")

    print("\n  提示：想看某个岗位完整的历史轨迹，"
          "用 --ods-history <职位ID>。")


def build_dwd():
    """
    从 ODS 重建 DWD 清洗层（不联网）。

    DWD 是纯派生层：薪资数值区间、城市三级、学历枚举、经验年限、技能标签
    全部由 ODS 的原始文本推导得出。所以清洗规则改了直接重跑这里即可，
    秒级完成，不需要重新抓数据，也不会动 ODS 一个字节。
    """
    ods = OdsRepository()
    if ods.count_jobs() == 0:
        print("ODS 贴源层为空，无法构建 DWD。先跑一次抓取：run.bat")
        return

    dwd = DwdRepository()
    print("=" * 62)
    print("  构建 DWD 明细层（只读 ODS，不联网）")
    print(f"  数据源：{ods.count_jobs()} 个岗位 / {ods.count_snapshots()} 条快照")
    print("=" * 62)

    stats = dwd.build()

    print(f"\n  dwd_job_detail：{stats['jobs']} 行")
    print(f"    薪资解析成功  ：{stats['salary_parsed']} 条"
          f"（{stats['salary_parsed_rate']}%）")
    print(f"    学历归一成功  ：{stats['education_level']} 条"
          f"（{stats.get('education_level_rate', 0)}%）")
    print(f"    城市拆出市级  ：{stats.get('city_name', 0)} 条")
    print(f"    城市拆出区县  ：{stats['city_district']} 条"
          f"（{stats['city_district_rate']}%）")
    print(f"    经验有站点值  ：{stats['experience_known']} 条"
          f"（{stats.get('experience_known_rate', 0)}%）")

    print(f"\n  dwd_job_skill：{stats['skills']} 行 / "
          f"{stats['distinct_skills']} 个去重技能")
    print(f"    平均每岗位标签数：{stats['avg_skills']}")
    print(f"    抽到标签的岗位  ：{stats['jobs_with_skill']} 个"
          f"（{stats['jobs_without_skill']} 个零标签）")

    print("\n  提示：技能热度榜用 --dwd-stats 查看；"
          "清洗规则改在 core/dwd.py，改完重跑本命令即可。")


def show_dwd_stats(top: int = 15):
    """查看 DWD 清洗层的质量与分布"""
    dwd = DwdRepository()
    total = dwd.count_details()

    if total == 0:
        print("DWD 明细层为空。先构建：python main.py --build-dwd")
        return

    stats = dwd.summarize()

    print("=" * 68)
    print("  DWD 明细层（由 ODS 清洗而来，可随时重算）")
    print("=" * 68)
    print(f"  明细行数      ：{stats['jobs']}")
    print(f"  薪资解析成功  ：{stats['salary_parsed']}（{stats['salary_parsed_rate']}%）")
    print(f"  学历归一成功  ：{stats['education_level']}"
          f"（{stats.get('education_level_rate', 0)}%）")
    print(f"  城市拆出区县  ：{stats['city_district']}"
          f"（{stats['city_district_rate']}%）")
    print(f"  经验有站点值  ：{stats['experience_known']}"
          f"（{stats.get('experience_known_rate', 0)}%）")
    print(f"  技能标签      ：{stats['skills']} 行 / "
          f"{stats['distinct_skills']} 个去重技能"
          f"（均值 {stats['avg_skills']}/岗位）")

    rows = dwd.salary_by_city(limit=8)
    if rows:
        print(f"\n  按城市看薪资（前 {len(rows)} 个城市，单位：元/月）：")
        header = (_disp_pad("城市", 12, "left") + _disp_pad("岗位数", 8)
                  + _disp_pad("平均月薪", 10) + _disp_pad("最低", 8)
                  + _disp_pad("最高", 8))
        print("    " + header)
        for r in rows:
            print("    " + _disp_pad(r["city_name"] or "未知", 12, "left")
                  + _disp_pad(r["job_count"], 8)
                  + _disp_pad(int(r["avg_salary"] or 0), 10)
                  + _disp_pad(r["min_salary"] or 0, 8)
                  + _disp_pad(r["max_salary"] or 0, 8))

    top_rows = dwd.top_skills(limit=top)
    if top_rows:
        print(f"\n  技能热度 TOP{len(top_rows)}（按要求该技能的岗位数）：")
        header = (_disp_pad("技能", 22, "left") + _disp_pad("类别", 12, "left")
                  + _disp_pad("岗位数", 8))
        print("    " + header)
        for r in top_rows:
            print("    " + _disp_pad(r["skill_name"], 22, "left")
                  + _disp_pad(r["skill_category"], 12, "left")
                  + _disp_pad(r["job_count"], 8))


def rebuild_state():
    """
    从 ODS 重放历史，重建最新状态层（不联网）。

    「ODS 是稳定数据源」最直接的体现：派生层坏了、口径改了、
    误删了，都可以从原始快照重新算出来，不必再花 8 分钟联网重抓。
    """
    storage = JobStorage()
    ods = OdsRepository()

    if ods.count_jobs() == 0:
        print("ODS 贴源层为空，无法重建。先跑一次抓取：python main.py")
        return

    print("=" * 58)
    print("  从 ODS 重建最新状态层（不联网，不消耗站点请求）")
    print(f"  ODS：{ods.count_jobs()} 个岗位 / {ods.count_snapshots()} 条快照")
    print("=" * 58)

    before = storage.count()
    result = storage.rebuild_from_ods(ods, batch_id="rebuild")
    print(f"\n  重放快照 {result['snapshots']} 条")
    print(f"  最新状态层：{before} 条 -> {storage.count()} 条")

    # 重建只还原原始字段，匹配分是派生指标，需要按当前规则重算一遍
    print("\n  原始字段已还原，正在按当前打分规则重算匹配分 ...")
    rescore_all()



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
    # 版本号唯一来源是 config.VERSION，这里不重复写死。
    # 用 argparse 内建的 version action，直接打印并退出，
    # 不需要在 main() 里为它单独加分支。
    parser.add_argument(
        "--version", action="version",
        version=f"job_matcher {VERSION}（{VERSION_DATE}）"
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
        help="清空最新状态层（jobs 表）。若 ODS 里还有历史快照，"
             "会自动从 ODS 离线重建，不会真的丢数据"
    )
    mode.add_argument(
        "--reset-ods", action="store_true",
        help="清空 ODS 贴源层（丢弃全部历史快照与批次台账）。"
             "这是真的丢历史，只在确认历史不可信时使用"
    )
    mode.add_argument(
        "--ods-stats", action="store_true",
        help="只看 ODS 贴源层与抓取批次台账"
    )
    mode.add_argument(
        "--ods-history", metavar="职位ID",
        help="看某个岗位在 ODS 里的全部抓取快照，以及它何时发生了变化"
    )
    mode.add_argument(
        "--ods-changes", nargs="?", type=int, const=7, metavar="天数",
        help="看最近 N 天内岗位信息发生了哪些变化（默认 7 天）"
    )
    mode.add_argument(
        "--rebuild-state", action="store_true",
        help="从 ODS 重放历史重建最新状态层（不联网、不消耗站点请求）"
    )
    mode.add_argument(
        "--build-dwd", action="store_true",
        help="从 ODS 重算 DWD 明细层（薪资/城市/学历/经验清洗 + 技能标签抽取），"
             "不联网、不消耗站点请求，可反复重跑"
    )
    mode.add_argument(
        "--dwd-stats", action="store_true",
        help="查看 DWD 明细层的构建结果（字段填充率、薪资解析率、技能标签榜）"
    )
    mode.add_argument(
        "--close-orphan-batches", action="store_true",
        help="把长时间挂在 running 的批次收尾为 failed"
             "（进程被强杀时会留下这种批次，收尾后台账才可信）"
    )
    mode.add_argument(
        "--no-incremental", action="store_true",
        help="关闭增量，全部岗位重抓详情（排查「数据没更新」时用）"
    )
    mode.add_argument(
        "--full-refresh", action="store_true",
        help="强制全量刷新：忽略历史，本批所有岗位重抓详情并写入新快照"
    )
    mode.add_argument(
        "--detail-ttl", type=int, metavar="天数",
        help=f"详情字段的可信天数，超期强制重抓（默认 {DETAIL_REFRESH_TTL_DAYS} 天）"
    )
    mode.add_argument(
        "--active-only", action="store_true",
        help="排除已标记失效（本批列表未再出现）的岗位"
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

    # ---- 只读模式：看一眼就走，不改数据、不联网 ----
    if args.stats:
        show_stats()
        return 0

    if args.ods_stats:
        show_ods_stats()
        return 0

    if args.ods_history:
        show_ods_history(args.ods_history)
        return 0

    if args.ods_changes is not None:
        show_ods_changes(args.ods_changes)
        return 0

    if args.close_orphan_batches:
        ods = OdsRepository()
        orphans = ods.orphan_batches()
        if not orphans:
            print("没有需要收尾的孤儿批次。")
        else:
            closed = ods.close_orphan_batches()
            print(f"已收尾 {len(closed)} 个孤儿批次（标记为 failed）：")
            for b in orphans:
                print(f"  {b['batch_id']}  开始于 {b['started_at']}"
                      f"  模式 {b['mode']}")
        return 0

    if args.rebuild_state:
        rebuild_state()
        return 0

    if args.build_dwd:
        build_dwd()
        return 0

    if args.dwd_stats:
        show_dwd_stats()
        return 0

    if args.rescore:
        rescore_all()
        return 0

    # ---- 维护模式：清库 ----
    if args.reset_ods:
        ods = OdsRepository()
        snapshots = ods.count_snapshots()
        ods.clear()
        print(f"已清空 ODS 贴源层：删除 {snapshots} 条快照与全部批次台账。")
        print("★ 这是真的丢历史。最新状态层未受影响，"
              "但下次抓取时所有岗位都会被当成新岗位、重新抓一遍详情。")

    if args.reset_db:
        storage = JobStorage()
        removed = storage.clear()
        print(f"已清空最新状态层：删除 {removed} 条岗位。")

        ods = OdsRepository()
        if ods.count_jobs():
            print(f"检测到 ODS 里还有 {ods.count_jobs()} 个岗位的历史快照，"
                  "正在离线重建（不联网）...")
            result = storage.rebuild_from_ods(ods, batch_id="rebuild")
            print(f"重建完成：重放 {result['snapshots']} 条最新快照，"
                  f"还原 {storage.count()} 条岗位。")
            print("（若想连历史一起清掉、彻底重抓，加上 --reset-ods 再跑一次）")
            rescore_all()

    job_filter = build_filter(args)
    profile = build_profile(args)

    if args.save_filter:
        path = job_filter.save(args.save_filter)
        print(f"筛选方案已保存：{path}")
    if args.save_search:
        path = profile.save(args.save_search)
        print(f"检索方案已保存：{path}")

    fetch_detail = not args.no_detail
    incremental = not (args.no_incremental or args.full_refresh)
    print_banner(profile, args.skip_crawl, fetch_detail, incremental)
    run_pipeline(
        job_filter, profile, args.skip_crawl, args.out,
        fetch_detail, args.detail_limit,
        incremental=not args.no_incremental,
        full_refresh=args.full_refresh,
        detail_ttl=args.detail_ttl,
        active_only=args.active_only,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
