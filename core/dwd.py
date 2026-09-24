# -*- coding: utf-8 -*-
"""
DWD 明细清洗层（Data Warehouse Detail）
=======================================

一句话职责：**把 ODS 里"人读得懂但机器算不了"的文本，拆成可聚合的列。**

────────────────────────────────────────────────────────────────
【为什么需要这一层】
────────────────────────────────────────────────────────────────
ODS 存的是站点原样值：

    salary      "7000-10000" / "10-15万/年" / "面议"
    city        "福建省厦门市思明区" / "福建省厦门市"
    education   "本科" / "本科及以上" / "其他/无"
    experience  "应届毕业生" / "不限" / "1-3年"

这些值能存，不能算。"厦门的数据岗薪资分布如何"这种问题，
在原始文本上没法用 SQL 回答——必须先把区间拆成数值列。

DWD 做四件事：
    薪资  拆成 salary_min / salary_max / salary_avg（统一折算成月薪）
    城市  拆成 city_province / city_name / city_district 三级
    学历  归一成枚举（不限/大专/本科/硕士/博士，多值取最高）
    经验  映射成年限区间（experience_min / _max / _unlimited）

再加一件事（步骤 4）：**技能标签抽取**，产出 dwd_job_skill 明细表。

────────────────────────────────────────────────────────────────
【三条设计约定】
────────────────────────────────────────────────────────────────
1. **只读 ODS，绝不回写**
   DWD 是纯派生层：清洗规则改了随时 `--build-dwd` 重算（几秒钟），
   不碰 ODS 一个字节。这样「原始事实」与「加工结果」彻底分离，
   与 ODS 的 append-only 约定不冲突。

2. **全量重建，不做增量更新**
   DWD 表没有独立价值——它 100% 由 ODS 推导得出。所以每次构建先清空
   自己的两张表再整批写入。代价在几百行量级可以忽略，换来的是
   "同一批数据跑两次结果完全一样"这件事**天然成立**，
   不需要额外写一套去重/合并逻辑去维护幂等性。

3. **未知就是未知，不要假装知道**
   薪资解析失败 → salary_parsed = 0，而不是填 0 元；
   城市拆不出区县 → 留空，而不是填"市辖区"这个占位串；
   经验没写 → 三个字段全 NULL / 0，而不是当作"不限"。
   这与项目里「信息缺失 ≠ 不满足条件」是同一条原则。

────────────────────────────────────────────────────────────────
【关于规划里的「详情正文」】
────────────────────────────────────────────────────────────────
规划要求技能抽取从「岗位名称、专业要求、详情正文」三处取词。
实测（2026-09-24，抓 6 个详情页确认）：该站点的「职位详情」是一个
**空标签页**，页面上只有标签名、没有任何正文文本。

同一区域真正有值的是站点用下拉框维护的结构化字段，其中
「职能类别」是现成的职能分类（"内容运营" / "业务拓展" / "税务专员/助理"）。
本模块因此把取词来源定为：

    岗位名称 / 需求专业 / 职能类别 / 语言要求

站点维护的分类比从自由正文里猜词更可靠。这条偏差已写进 P1 验收报告。
"""
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from config import (
    DB_PATH,
    EDUCATION_ALIASES,
    EDUCATION_LEVELS,
    SALARY_BANDS,
    SALARY_MONTHS_PER_YEAR,
    SKILL_DICT,
    CITY_PLACEHOLDER_DISTRICTS,
)
from core.dbutil import connect


# 清洗规则版本。规则（而不是解析器）发生变化时 +1，写进 dwd_job_detail，
# 便于回答"这张表是哪一版口径算出来的"。
DWD_VERSION = 1


# ===================================================================
# 建表语句
# ===================================================================
CREATE_DETAIL_SQL = """
CREATE TABLE IF NOT EXISTS dwd_job_detail (
    job_key           TEXT PRIMARY KEY,   -- 与 ODS / jobs 对齐的稳定主键
    source_job_id     TEXT,
    company           TEXT,
    title             TEXT,

    -- ---- 薪资（统一折算成月薪；salary_period 记录原始计薪周期）----
    salary_raw        TEXT,
    salary_min        INTEGER,
    salary_max        INTEGER,
    salary_avg        REAL,
    salary_period     TEXT,               -- 月 / 年 / ''(未解析)
    salary_parsed     INTEGER NOT NULL DEFAULT 0,

    -- ---- 城市（三级拆分）----
    city_raw          TEXT,
    city_province     TEXT,
    city_name         TEXT,
    city_district     TEXT,

    -- ---- 学历（枚举归一，多值取最高）----
    education_raw     TEXT,
    education_level   TEXT,

    -- ---- 经验（年限区间；unlimited=站点明确写"不限"）----
    experience_raw    TEXT,
    experience_min    INTEGER,
    experience_max    INTEGER,
    experience_unlimited INTEGER NOT NULL DEFAULT 0,

    -- ---- 站点原样带过来的维度（不归一，留给 DIM 层做）----
    industry          TEXT,
    company_nature    TEXT,
    company_scale     TEXT,
    job_category      TEXT,
    language_req      TEXT,
    headcount         TEXT,
    headcount_num     INTEGER,

    publish_date      TEXT,
    deadline          TEXT,
    url               TEXT,

    -- ---- 血缘 ----
    dwd_version       INTEGER NOT NULL DEFAULT 0,
    built_at          TEXT
);
"""

CREATE_SKILL_SQL = """
CREATE TABLE IF NOT EXISTS dwd_job_skill (
    job_key        TEXT NOT NULL,
    skill_id       TEXT NOT NULL,
    skill_name     TEXT NOT NULL,
    skill_category TEXT,
    -- 命中的字段，按优先级排列（title > job_category > major > language）
    -- 逗号分隔，如 "title,major_requirement"
    source_field   TEXT,
    -- 命中的别名原文，用于解释"这个标签凭什么打给它"
    hit_text       TEXT,
    built_at       TEXT,
    PRIMARY KEY (job_key, skill_id)
);
"""

CREATE_INDEX_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_dwd_city ON dwd_job_detail(city_name);",
    "CREATE INDEX IF NOT EXISTS idx_dwd_salary ON dwd_job_detail(salary_min);",
    "CREATE INDEX IF NOT EXISTS idx_dwd_edu ON dwd_job_detail(education_level);",
    "CREATE INDEX IF NOT EXISTS idx_dwd_skill ON dwd_job_skill(skill_id);",
]


# ===================================================================
# 一、薪资解析
# ===================================================================
# 明确表示「薪资未知」的写法。注意这些不能当成 0 元——
# 0 元的月薪会污染均值与分位数，而"面议"只是没写。
SALARY_UNKNOWN_MARKERS = (
    "面议", "待定", "negotiable", "详见", "另议", "薪资面谈",
)

# 非月薪的计薪单位。本项目不做时薪/日薪折算——
# 折算需要假设工时，而站点从没给过工时，硬折算等于编数据。
# 实测本数据集未出现这类写法，留着是为了脏数据时不至于把
# "200元/天" 记成"月薪 200 元"。
SALARY_NON_MONTHLY_MARKERS = ("小时", "日薪", "/天", "周薪", "/周", "时薪")


@dataclass
class SalaryParts:
    """
    薪资解析结果。三个数值列**统一是月薪**。

    :param period: 原始计薪周期（"月" / "年" / ""）。
        它保留的是"站点怎么写"，而三个数值列是"折成什么"。
        两者都留着，是因为下游既要用数值做聚合，
        也要能回答"这条是月薪还是年薪"。
    """
    raw: str = ""
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_avg: Optional[float] = None
    period: str = ""
    parsed: bool = False


def parse_salary(text) -> SalaryParts:
    """
    把薪资文本解析成月薪三元组。

    支持的真实格式（来自厦大就业网实测 + 常见变体）：
        "7000-10000"       -> 7000 ~ 10000 元/月
        "7500-8499"        -> 7500 ~ 8499
        "8000"             -> 8000 ~ 8000
        "8000元/月"        -> 8000
        "10-15万/年"       -> 8333 ~ 12500 元/月（周期记为"年"）
        "年薪20万"         -> 16666 元/月
        "8-12K"            -> 8000 ~ 12000
        "面议" / ""        -> parsed=False（未知）

    「未知」必须与「0 元」区分开：解析失败返回 parsed=False、
    三个数值为 None，调用方保留该条记录而不是当成薪资为 0 剔除。
    """
    raw = "" if text is None else str(text).strip()
    parts = SalaryParts(raw=raw)
    if not raw:
        return parts

    low = raw.lower()
    if any(m in low for m in SALARY_UNKNOWN_MARKERS):
        return parts
    if any(m in raw for m in SALARY_NON_MONTHLY_MARKERS):
        # 时薪/日薪，缺少工时无法折算到月 → 记为未知，不编造
        return parts

    nums = re.findall(r"\d+(?:\.\d+)?", raw)
    if not nums:
        return parts

    # ---- 判断计薪周期与单位量级 ----
    # 两件事分开决定，不能用一个条件串起来：
    #   ① 量级（multiplier）：写"万"就是 10000 倍，写"千/K"就是 1000 倍
    #   ② 周期（period）    ：写"月"就是月薪；否则出现"年"或裸"万"都按年薪
    #      （"10-15万" 是年薪的常见写法，站点上不写"年"字）
    #
    # ⚠️ 曾经的写法是 `is_annual = ("年薪" in raw) or ("万" in raw) or ...`，
    # 它把 "2万/月"（= 20000 元/月）当成年薪除以 12，算成 1667 元——
    # 量级对但周期错，结果既不报错也不为 0，最难发现。
    # 判周期必须先看有没有"月"字。
    has_month = "月" in raw
    is_wan = "万" in raw
    is_thousand = ("k" in low) or ("千" in raw)
    is_annual = (not has_month) and (("年" in raw) or is_wan)

    if is_wan:
        multiplier = 10000.0
    elif is_thousand:
        multiplier = 1000.0
    else:
        multiplier = 1.0
    period = "年" if is_annual else "月"

    values = [float(n) * multiplier for n in nums[:2]]
    if is_annual:
        values = [v / SALARY_MONTHS_PER_YEAR for v in values]

    lo, hi = min(values), max(values)

    # ---- 合理性闸门 ----
    # 月薪低于 100 元基本可判定"这个数字不是薪资"（更像编号/人数字段被误抓），
    # 高于 200 万则几乎一定是解析错位。宁可记为未知，也不要污染分布。
    if lo < 100 or hi > 2_000_000:
        return parts

    # 单值写法（"8000"）时上下限相同，不臆造区间
    parts.salary_min = int(round(lo))
    parts.salary_max = int(round(hi))
    parts.salary_avg = round((lo + hi) / 2, 2)
    parts.period = period
    parts.parsed = True
    return parts


def salary_band(value, bands=None) -> str:
    """把一个最低月薪值映射到薪资分档标签；None 或无效返回空串"""
    if not value:
        return ""
    for lo, hi, label in (bands or SALARY_BANDS):
        if value >= lo and (hi is None or value < hi):
            return label
    return ""


# ===================================================================
# 二、城市三级拆分
# ===================================================================
# 非贪婪匹配，确保"福建省厦门市"拆成 ["福建省", "厦门市"] 而不是 ["福建省厦门市"]
_PROVINCE_RE = re.compile(r"^(.{2,12}?(?:省|自治区|特别行政区))")
_CITY_RE = re.compile(r"^(.{2,12}?(?:市|自治州|地区|盟))")
_DISTRICT_RE = re.compile(r"^(.{2,12}?(?:区|县|旗|市))")


def split_city(text) -> Tuple[str, str, str]:
    """
    把「福建省厦门市思明区」拆成 (省, 市, 区)。

    保留行政区名后缀（"福建省" 而不是 "福建"）：这是**有损才奇怪**的一步——
    去掉后缀要处理"内蒙古自治区""湘西土家族苗族自治州"等特例，
    而这层只需要能分组，有没有后缀都能 GROUP BY，不给自己找麻烦。

    拆不出来的层级返回空串，**不猜**。实测本数据集里 285/294 条
    只到市一级（列表页给的就是"福建省厦门市"），区县要等详情页才有。
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        return "", "", ""

    province = ""
    m = _PROVINCE_RE.match(raw)
    if m:
        province = m.group(1)
        raw = raw[len(province):]

    city = ""
    m = _CITY_RE.match(raw)
    if m:
        city = m.group(1)
        raw = raw[len(city):]

    district = ""
    m = _DISTRICT_RE.match(raw)
    if m:
        district = m.group(1)

    # "市辖区" 是"市本级、未指定到区"的占位写法，不是区名。
    # 当成区名落库会让"按区统计"凭空多出一个巨大的假区。
    if district in CITY_PLACEHOLDER_DISTRICTS:
        district = ""

    return province, city, district


# ===================================================================
# 三、学历归一化
# ===================================================================
def normalize_education(text) -> str:
    """
    把学历文本归一成枚举：不限 / 大专 / 本科 / 硕士 / 博士。

    多值取**最高**一档：岗位写"硕士,博士"说明门槛是硕士起，
    取最低会把"仅招硕博"的岗位误判成本科可投。
    学历为空或无法识别返回空串（未知），不默认成"不限"——
    那会把"没抓到"说成"没有要求"。
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        return ""

    hit = []
    for level in EDUCATION_LEVELS:
        if level == "不限":
            continue
        for alias in EDUCATION_ALIASES.get(level, []):
            if alias and alias.lower() in raw.lower():
                hit.append(level)
                break

    if hit:
        # 取档位最高者；EDUCATION_LEVELS 已按从低到高排列
        return max(hit, key=EDUCATION_LEVELS.index)

    for alias in EDUCATION_ALIASES.get("不限", []):
        if alias and alias in raw:
            return "不限"
    return ""


# ===================================================================
# 四、经验映射
# ===================================================================
def parse_experience(text) -> Tuple[Optional[int], Optional[int], bool]:
    """
    把经验要求映射成年限区间。

    :return: (最少年限, 最多年限, 是否明确"不限")
        年限无法判断时返回 (None, None, False)。

    三种语义要分清，它们不是同一件事：
        "不限"        → (None, None, True)   无门槛
        "应届毕业生"   → (0, 0, False)        0 年即可（对应届生是门槛，对社招是限制）
        "1-3年"       → (1, 3, False)
        ""            → (None, None, False)  未知，不是"不限"

    ⚠️ 在本数据集上这个维度区分度天然很低：厦大就业网是**校园招聘**平台，
    绝大多数岗位写"不限"或"应届毕业生"。这不是清洗失败，是数据集性质——
    同样的规则放到社招数据上才能真正拉开分布。别为了好看去造分布。
    """
    raw = "" if text is None else str(text).strip()
    if not raw:
        return None, None, False

    if "不限" in raw or "无经验" in raw or "经验无要求" in raw:
        return None, None, True

    if "应届" in raw or "毕业生" in raw or "在校" in raw:
        return 0, 0, False

    # 区间："1-3年" / "1~3年" / "1至3年"
    m = re.search(r"(\d+)\s*[-~—至到]\s*(\d+)\s*年", raw)
    if m:
        return int(m.group(1)), int(m.group(2)), False

    # 上不封顶："3年以上" / "3年及以上"
    m = re.search(r"(\d+)\s*年\s*(?:以上|及以上|以上经验)", raw)
    if m:
        return int(m.group(1)), None, False

    # 固定年限："3年"
    m = re.fullmatch(r"(\d+)\s*年", raw)
    if m:
        return int(m.group(1)), int(m.group(1)), False

    return None, None, False


# ===================================================================
# 五、技能标签抽取
# ===================================================================
# 取词来源与优先级。顺序有意义：排在前面的字段先说清"这个岗位是干什么的"，
# source_field 会按这个顺序记录，便于排查误命中。
SKILL_SOURCE_FIELDS = ("title", "job_category", "major_requirement",
                       "language_req")

# 字段名 -> 展示用中文（写进 dwd_job_skill 时人看得懂）
SOURCE_FIELD_LABELS = {
    "title": "岗位名称",
    "job_category": "职能类别",
    "major_requirement": "需求专业",
    "language_req": "语言要求",
}


@dataclass
class SkillHit:
    """一条技能命中记录"""
    skill_id: str
    skill_name: str
    skill_category: str
    source_field: str     # 逗号分隔，按 SKILL_SOURCE_FIELDS 顺序
    hit_text: str         # 逗号分隔的命中别名


def _compile_skill_index(skill_dict) -> List[tuple]:
    """
    把技能词典编译成「按别名长度降序」的匹配表。

    两个细节决定了匹配质量：

    1. **长别名优先**
       同一段文本里只会把最具体的那个别名算作命中来源。
       排序本身不影响"命中哪些 skill_id"（不同 skill 各自独立判断），
       但影响 hit_text 取到哪个别名——优先展示更具体的那个
       （"机器学习" 比 "ai" 更有解释力）。

    2. **短 ASCII 别名加词边界**
       "ai" / "go" / "js" / "it" / "hr" 这类两三个字母的别名，
       直接做子串匹配会误伤：go 命中 google、ai 命中 said、
       it 命中 retail。对纯 ASCII 且长度 ≤ 3 的别名改成正则匹配，
       要求前后不是字母数字。中文别名不需要（不存在分词歧义）。
    """
    entries = []
    for skill_id, name, category, aliases in skill_dict:
        for alias in aliases:
            low = alias.lower()
            pattern = None
            if low.isascii() and len(low) <= 3 and re.fullmatch(r"[a-z0-9+#.]+", low):
                pattern = re.compile(
                    r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9+#])"
                )
            entries.append((low, skill_id, name, category, alias, pattern))

    entries.sort(key=lambda e: -len(e[0]))
    return entries


# 模块级缓存：词典是静态的，编译一次即可
_SKILL_INDEX = None


def _skill_index():
    global _SKILL_INDEX
    if _SKILL_INDEX is None:
        _SKILL_INDEX = _compile_skill_index(SKILL_DICT)
    return _SKILL_INDEX


def extract_skills(row: dict, skill_dict=None) -> List[SkillHit]:
    """
    从一行源数据里抽取技能标签。

    :param row: 含 title / job_category / major_requirement / language_req
    :return: SkillHit 列表；同一 skill 在多个字段命中时会合并成一条

    【为什么要合并而不是每个字段一条】
        下游问的是"这个岗位要求什么技能"。"Java" 同时出现在岗位名和
        专业要求里，说的是同一件事，拆成两行会让"技能热度"重复计数。
        合并后 source_field / hit_text 记录**全部**命中位置，信息不丢。
    """
    index = _skill_index() if skill_dict is None else _compile_skill_index(skill_dict)

    # skill_id -> {"fields": [...], "aliases": [...]}
    merged: Dict[str, dict] = {}
    meta: Dict[str, Tuple[str, str]] = {}

    for source in SKILL_SOURCE_FIELDS:
        text = str(row.get(source) or "").lower()
        if not text:
            continue
        for low_alias, skill_id, name, category, alias, pattern in index:
            matched = bool(pattern.search(text)) if pattern else (low_alias in text)
            if not matched:
                continue
            slot = merged.setdefault(skill_id, {"fields": [], "aliases": []})
            meta[skill_id] = (name, category)
            if source not in slot["fields"]:
                slot["fields"].append(source)
            if alias not in slot["aliases"]:
                slot["aliases"].append(alias)

    hits = []
    for skill_id, slot in merged.items():
        name, category = meta[skill_id]
        # 字段按 SKILL_SOURCE_FIELDS 的定义顺序排序，保证可复现
        fields = sorted(slot["fields"], key=SKILL_SOURCE_FIELDS.index)
        # 别名长的排前面：更能说明问题
        aliases = sorted(slot["aliases"], key=lambda a: (-len(a), a))
        hits.append(SkillHit(
            skill_id=skill_id,
            skill_name=name,
            skill_category=category,
            source_field=",".join(fields),
            hit_text="、".join(aliases),
        ))

    hits.sort(key=lambda h: (h.skill_category, h.skill_id))
    return hits


# ===================================================================
# 六、行变换（纯函数）
# ===================================================================
def _parse_headcount(text) -> Optional[int]:
    """把"16人"解析成 16；解析不出返回 None"""
    raw = "" if text is None else str(text).strip()
    if not raw:
        return None
    m = re.search(r"(\d+)", raw)
    return int(m.group(1)) if m else None


def transform(row: dict) -> dict:
    """
    一行 ODS 源数据 -> 一行 dwd_job_detail。

    纯函数：不查库、不联网、不改入参。这样清洗规则可以被单测
    逐条构造用例验证，也能在没有数据库的环境里跑。

    :param row: core.ods.OdsRepository.build_source_rows() 的一行
    """
    salary = parse_salary(row.get("salary"))
    # 详情页的 city 是完整三级，列表页只到市——优先用更完整的那个
    city_raw = str(row.get("city_detail") or row.get("city") or "")
    province, city, district = split_city(city_raw)
    exp_min, exp_max, exp_unlimited = parse_experience(row.get("experience"))

    return {
        "job_key": row.get("job_key") or "",
        "source_job_id": row.get("source_job_id") or "",
        "company": row.get("company") or "",
        "title": row.get("title") or "",

        "salary_raw": salary.raw,
        "salary_min": salary.salary_min,
        "salary_max": salary.salary_max,
        "salary_avg": salary.salary_avg,
        "salary_period": salary.period,
        "salary_parsed": 1 if salary.parsed else 0,

        "city_raw": city_raw,
        "city_province": province,
        "city_name": city,
        "city_district": district,

        "education_raw": row.get("education") or "",
        "education_level": normalize_education(row.get("education")),

        "experience_raw": row.get("experience") or "",
        "experience_min": exp_min,
        "experience_max": exp_max,
        "experience_unlimited": 1 if exp_unlimited else 0,

        "industry": row.get("industry") or "",
        "company_nature": row.get("company_nature") or "",
        "company_scale": row.get("company_scale") or "",
        "job_category": row.get("job_category") or "",
        "language_req": row.get("language_req") or "",
        "headcount": row.get("headcount") or "",
        "headcount_num": _parse_headcount(row.get("headcount")),

        "publish_date": row.get("publish_date") or "",
        "deadline": row.get("deadline") or "",
        "url": row.get("url") or "",
    }


# ===================================================================
# 七、构建与查询入口
# ===================================================================
class DwdRepository:
    """DWD 明细层的构建与查询入口"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or DB_PATH)
        self._init_db()

    def _init_db(self):
        with connect(self.db_path) as conn:
            conn.execute(CREATE_DETAIL_SQL)
            conn.execute(CREATE_SKILL_SQL)
            for sql in CREATE_INDEX_SQL:
                conn.execute(sql)

    # ---------------------------------------------------------------
    # 构建
    # ---------------------------------------------------------------
    def build(self, rows: Optional[Iterable[dict]] = None) -> dict:
        """
        从 ODS 重建 DWD 两张表。

        :param rows: **ODS 源数据行**（core.ods.OdsRepository.build_source_rows()
                     的产出，或结构相同的字典）；不传则自己从 ODS 读。
                     ⚠️ 不要传 transform() 的结果——构建流程自己会调用
                     transform，传进来会被二次清洗，薪资/城市/学历会
                     全部变成"未知"，而且不报错。
        :return: 构建统计，供命令行打印与验收断言

        幂等性由「先清空再写入」保证，见模块开头的约定 2。
        """
        from core.ods import OdsRepository

        if rows is None:
            rows = OdsRepository(self.db_path).build_source_rows()
        rows = list(rows)

        for row in rows:
            # 把上面那个"⚠️"从注释变成会响的闸门。
            # 二次清洗的后果是薪资/城市/学历静默全变未知——
            # 明细表看起来正常（行数对、字段都在），只是数值全是空的。
            # 这种错只能靠人盯着分布图发现，所以宁可在这里直接炸。
            if "salary_raw" in row and "salary" not in row:
                raise ValueError(
                    "build() 的 rows 参数要的是 ODS 源数据行，"
                    "不是 transform() 的产出。构建流程内部会再调一次 "
                    "transform，传已清洗过的行会让薪资/城市/学历全部变成未知。"
                )

        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        details = []
        skills = []
        for row in rows:
            detail = transform(row)
            if not detail["job_key"]:
                continue        # 没有稳定主键的行进不了明细层（无法与上游对齐）
            detail["dwd_version"] = DWD_VERSION
            detail["built_at"] = stamp
            details.append(detail)
            for hit in extract_skills(row):
                skills.append({
                    "job_key": detail["job_key"],
                    "skill_id": hit.skill_id,
                    "skill_name": hit.skill_name,
                    "skill_category": hit.skill_category,
                    "source_field": hit.source_field,
                    "hit_text": hit.hit_text,
                    "built_at": stamp,
                })

        with connect(self.db_path) as conn:
            conn.execute("DELETE FROM dwd_job_detail")
            conn.execute("DELETE FROM dwd_job_skill")
            if details:
                conn.executemany(
                    f"INSERT INTO dwd_job_detail "
                    f"({', '.join(details[0].keys())}) "
                    f"VALUES ({', '.join(':' + k for k in details[0].keys())})",
                    details,
                )
            if skills:
                conn.executemany(
                    f"INSERT INTO dwd_job_skill "
                    f"({', '.join(skills[0].keys())}) "
                    f"VALUES ({', '.join(':' + k for k in skills[0].keys())})",
                    skills,
                )
            # 「先清空再写入」整体在一个事务里（见 core.dbutil.connect），
            # 中途失败会整体回滚，不会留下半张表。
        return self.summarize(rows=details, skills=skills)

    # ---------------------------------------------------------------
    # 统计
    # ---------------------------------------------------------------
    def summarize(self, rows=None, skills=None) -> dict:
        """
        DWD 质量概览。**不再查一次库**——统计直接从内存里的构建结果算，
        省一次全表扫描，也顺手保证"统计的就是刚写进去的那批"。
        """
        if rows is None:
            with connect(self.db_path) as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM dwd_job_detail")]
        if skills is None:
            with connect(self.db_path) as conn:
                skills = [dict(r) for r in conn.execute("SELECT * FROM dwd_job_skill")]

        total = len(rows)
        if not total:
            return {"jobs": 0, "skills": 0, "avg_skills": 0.0,
                    "salary_parsed": 0, "salary_parsed_rate": 0.0,
                    "city_district": 0, "city_district_rate": 0.0,
                    "education_level": 0, "experience_known": 0,
                    "jobs_with_skill": 0, "distinct_skills": 0}

        def filled(field):
            return sum(1 for r in rows
                       if r[field] is not None and r[field] != "")

        salary_parsed = sum(1 for r in rows if r["salary_parsed"])
        district = filled("city_district")
        edu = filled("education_level")
        # 经验"已知"= 站点给了值（不论是不限还是年限）
        exp_known = filled("experience_raw")
        by_job = {}
        for s in skills:
            by_job[s["job_key"]] = by_job.get(s["job_key"], 0) + 1

        return {
            "jobs": total,
            "skills": len(skills),
            "avg_skills": round(len(skills) / total, 2),
            "jobs_with_skill": len(by_job),
            "jobs_without_skill": total - len(by_job),
            "distinct_skills": len({s["skill_id"] for s in skills}),
            "salary_parsed": salary_parsed,
            "salary_parsed_rate": round(salary_parsed / total * 100, 1),
            "city_district": district,
            "city_district_rate": round(district / total * 100, 1),
            "city_name": filled("city_name"),
            "education_level": edu,
            "education_level_rate": round(edu / total * 100, 1),
            "experience_known": exp_known,
            "experience_known_rate": round(exp_known / total * 100, 1),
        }

    # ---------------------------------------------------------------
    # 查询
    # ---------------------------------------------------------------
    def query_details(self, limit: int = 0) -> List[dict]:
        sql = "SELECT * FROM dwd_job_detail ORDER BY job_key"
        if limit:
            sql += f" LIMIT {int(limit)}"
        with connect(self.db_path) as conn:
            return [dict(r) for r in conn.execute(sql)]

    def top_skills(self, limit: int = 20) -> List[dict]:
        """
        技能热度榜：按"要求该技能的岗位数"降序。

        用 COUNT(DISTINCT job_key) 而不是 COUNT(*) —— 表的主键是
        (job_key, skill_id)，一个岗位对一个技能只有一行，
        但写清楚聚合口径可以避免以后加了别的维度时踩坑。
        """
        sql = """
        SELECT s.skill_id, s.skill_name, s.skill_category,
               COUNT(DISTINCT s.job_key) AS job_count
          FROM dwd_job_skill s
          JOIN dwd_job_detail d ON d.job_key = s.job_key
         GROUP BY s.skill_id, s.skill_name, s.skill_category
         ORDER BY job_count DESC, s.skill_id ASC
         LIMIT ?
        """
        with connect(self.db_path) as conn:
            return [dict(r) for r in conn.execute(sql, (limit,))]

    def salary_by_city(self, limit: int = 10) -> List[dict]:
        """按城市统计薪资：岗位数、平均月薪、区间下界均值"""
        sql = """
        SELECT city_name,
               COUNT(*)                       AS job_count,
               ROUND(AVG(salary_avg), 0)      AS avg_salary,
               MIN(salary_min)                AS min_salary,
               MAX(salary_max)                AS max_salary
          FROM dwd_job_detail
         WHERE city_name <> ''
         GROUP BY city_name
         ORDER BY job_count DESC, city_name ASC
         LIMIT ?
        """
        with connect(self.db_path) as conn:
            return [dict(r) for r in conn.execute(sql, (limit,))]

    def count_details(self) -> int:
        with connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM dwd_job_detail").fetchone()[0]

    def count_skills(self) -> int:
        with connect(self.db_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM dwd_job_skill").fetchone()[0]
