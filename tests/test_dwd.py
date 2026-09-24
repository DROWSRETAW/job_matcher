# -*- coding: utf-8 -*-
"""
DWD 明细清洗层单元测试
======================

覆盖 P1 步骤 3/4 新增的 `core/dwd.py`：

    1. 薪资解析     —— 月薪/年薪/千元/单值/未知/脏数据的全部分支
    2. 城市三级拆分 —— 省/市/区，占位区名与拆不出的情况
    3. 学历归一     —— 多值取最高、同义词、未知
    4. 经验映射     —— 不限/应届/区间/上不封顶，以及"未知 ≠ 不限"
    5. 技能标签抽取 —— 别名归一、字段合并、短 ASCII 别名的词边界
    6. 行变换       —— 纯函数，不查库不联网
    7. 构建与查询   —— 幂等性、只读 ODS 的硬约束

全部为离线测试，不发起任何网络请求。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import DETAIL_SCHEMA_VERSION  # noqa: E402
from core.dwd import (  # noqa: E402
    DWD_VERSION, DwdRepository, SalaryParts, extract_skills,
    normalize_education, parse_experience, parse_salary, salary_band,
    split_city, transform,
)
from core.models import Job, make_job_key, list_fingerprint  # noqa: E402
from core.ods import OdsRepository  # noqa: E402


# ===================================================================
# 一、薪资解析
# ===================================================================
class TestParseSalary:
    """
    薪资解析的每一个分支都直接决定"薪资分布"这张图对不对。
    最要紧的一条：**解析失败必须记为未知，不能记成 0 元**——
    0 元会同时污染均值和分位数，而且看起来"有数据"，最难发现。
    """

    def test_monthly_range(self):
        s = parse_salary("7000-10000")
        assert s.parsed is True
        assert (s.salary_min, s.salary_max) == (7000, 10000)
        assert s.salary_avg == 8500.0
        assert s.period == "月"

    def test_monthly_single_value(self):
        """单值写法不臆造区间：上下限相同"""
        s = parse_salary("8000")
        assert (s.salary_min, s.salary_max) == (8000, 8000)
        assert s.salary_avg == 8000.0

    def test_monthly_with_unit(self):
        s = parse_salary("8000元/月")
        assert s.parsed is True
        assert s.salary_min == 8000
        assert s.period == "月"

    def test_thousand_suffix(self):
        """"8-12K" 是千元，不是 8 元"""
        s = parse_salary("8-12K")
        assert (s.salary_min, s.salary_max) == (8000, 12000)
        assert s.period == "月"

    def test_chinese_thousand_suffix(self):
        s = parse_salary("8千-12千")
        assert (s.salary_min, s.salary_max) == (8000, 12000)

    def test_annual_wan(self):
        """"10-15万/年" 要折算成月薪，但 period 如实记"年" """
        s = parse_salary("10-15万/年")
        assert s.parsed is True
        assert s.period == "年"
        assert s.salary_min == 8333, "10万/12 = 8333.33"
        assert s.salary_max == 12500, "15万/12 = 12500"
        # 折算后的数值列是月薪 —— 跨岗位聚合时口径才统一
        assert s.salary_min >= 8000 and s.salary_max <= 13000

    def test_annual_prefix(self):
        s = parse_salary("年薪20万")
        assert s.period == "年"
        assert s.salary_min == 16667
        assert s.salary_max == 16667

    def test_annual_plain_number_is_not_multiplied(self):
        """"年薪200000" 已经是元的绝对值，不该再乘 10000"""
        s = parse_salary("年薪200000")
        assert (s.salary_min, s.salary_max) == (16667, 16667)

    def test_wan_with_explicit_month_wins(self):
        """"月" 与 "万" 同时出现时按"万"处理仍成立（如"2万/月"）"""
        s = parse_salary("2万/月")
        assert s.salary_min == 20000, "2万 = 20000 元，且不折年"

    # ---- 未知：必须与 0 元区分开 ----
    @pytest.mark.parametrize("text", ["", None, "面议", "待定", "薪资面谈", "abc", "见详情"])
    def test_unknown_is_not_zero(self, text):
        s = parse_salary(text)
        assert s.parsed is False
        assert s.salary_min is None and s.salary_max is None
        assert s.salary_avg is None
        assert s.period == ""

    def test_non_monthly_is_unknown(self):
        """时薪/日薪缺工时无法折算，记为未知而不是编一个数"""
        for text in ("200元/天", "30元/小时", "1500元/周"):
            assert parse_salary(text).parsed is False, text

    def test_too_small_is_rejected(self):
        """低于 100 元/月基本可判定是编号被误抓"""
        assert parse_salary("50").parsed is False

    def test_too_large_is_rejected(self):
        assert parse_salary("3000000").parsed is False

    def test_raw_is_always_kept(self):
        """原始文本一律保留，供人工核对"""
        assert parse_salary("面议").raw == "面议"
        assert parse_salary("8000").raw == "8000"

    def test_returns_salary_parts(self):
        assert isinstance(parse_salary("8000"), SalaryParts)


class TestSalaryBand:
    def test_bands(self):
        assert salary_band(4000) == "5k以下"
        assert salary_band(5000) == "5-8k"
        assert salary_band(8000) == "8-12k"
        assert salary_band(15000) == "12-20k"
        assert salary_band(30000) == "20k以上"

    def test_boundary_belongs_to_upper_band(self):
        """分档口径写清楚：下界闭、上界开"""
        assert salary_band(7999) == "5-8k"
        assert salary_band(8000) == "8-12k"

    def test_empty_for_unknown(self):
        assert salary_band(None) == ""
        assert salary_band(0) == ""


# ===================================================================
# 二、城市三级拆分
# ===================================================================
class TestSplitCity:
    def test_three_levels(self):
        assert split_city("福建省厦门市思明区") == ("福建省", "厦门市", "思明区")

    def test_city_only(self):
        """实测 285/294 条只到市级 —— 区县留空，不猜"""
        assert split_city("福建省厦门市") == ("福建省", "厦门市", "")

    def test_city_without_province(self):
        assert split_city("厦门市") == ("", "厦门市", "")

    def test_municipality_with_district(self):
        assert split_city("上海市浦东新区") == ("", "上海市", "浦东新区")

    def test_autonomous_region(self):
        assert split_city("内蒙古自治区呼和浩特市新城区") == \
            ("内蒙古自治区", "呼和浩特市", "新城区")

    def test_autonomous_region_with_long_name(self):
        """非贪婪匹配要能处理"新疆维吾尔自治区"这种长省名"""
        province, city, _ = split_city("新疆维吾尔自治区乌鲁木齐市天山区")
        assert province == "新疆维吾尔自治区"
        assert city == "乌鲁木齐市"

    def test_autonomous_prefecture(self):
        assert split_city("湖南省湘西土家族苗族自治州吉首市")[1] == \
            "湘西土家族苗族自治州"

    def test_placeholder_district_is_blanked(self):
        """
        "市辖区"不是区名，是"市本级"的占位写法。
        把它当区名落库，按区统计时会凭空多出一个巨大的假区。
        """
        assert split_city("福建省厦门市市辖区") == ("福建省", "厦门市", "")

    def test_placeholder_only(self):
        assert split_city("市辖区") == ("", "", "")

    def test_unknown_is_empty_not_guessed(self):
        for text in ("", None, "不限", "全国"):
            assert split_city(text) == ("", "", ""), text

    def test_suffixes_are_kept(self):
        """保留后缀是有意的：去后缀要处理一堆民族自治特例，且对分组毫无必要"""
        province, city, district = split_city("广东省深圳市南山区")
        assert province.endswith("省") and city.endswith("市") and district.endswith("区")


# ===================================================================
# 三、学历归一
# ===================================================================
class TestNormalizeEducation:
    def test_plain(self):
        assert normalize_education("本科") == "本科"
        assert normalize_education("大专") == "大专"

    def test_alias(self):
        assert normalize_education("专科") == "大专"
        assert normalize_education("学士") == "本科"
        assert normalize_education("研究生") == "硕士"
        assert normalize_education("phd") == "博士"

    def test_multi_value_takes_highest(self):
        """
        ★ "硕士,博士" 的门槛是硕士起。
        取最低会把"仅招硕博"的岗位误判成本科可投。
        """
        assert normalize_education("硕士,博士") == "博士"
        assert normalize_education("本科、硕士") == "硕士"
        assert normalize_education("大专及以上，本科优先") == "本科"

    def test_and_above_stays_same_level(self):
        assert normalize_education("本科及以上") == "本科"

    def test_no_requirement(self):
        assert normalize_education("不限") == "不限"
        assert normalize_education("其他/无") == "不限"

    def test_unknown_is_empty_not_unlimited(self):
        """★ 抓不到 ≠ 没有要求。默认成"不限"等于把缺失说成放开。"""
        for text in ("", None, "详见公告", "学历要求见附件"):
            assert normalize_education(text) == "", text


# ===================================================================
# 四、经验映射
# ===================================================================
class TestParseExperience:
    def test_unlimited(self):
        assert parse_experience("不限") == (None, None, True)
        assert parse_experience("经验无要求") == (None, None, True)

    def test_fresh_graduate(self):
        """应届生是 0 年门槛，不是"不限" —— 对社招是限制"""
        assert parse_experience("应届毕业生") == (0, 0, False)
        assert parse_experience("在校生") == (0, 0, False)

    def test_range(self):
        assert parse_experience("1-3年") == (1, 3, False)
        assert parse_experience("2~5年") == (2, 5, False)
        assert parse_experience("2至5年") == (2, 5, False)

    def test_open_ended(self):
        assert parse_experience("3年以上") == (3, None, False)
        assert parse_experience("3年及以上") == (3, None, False)

    def test_exact_years(self):
        assert parse_experience("3年") == (3, 3, False)

    def test_unknown_is_not_unlimited(self):
        """★ 未知 ≠ 不限。这两个语义混淆会直接污染"经验门槛"统计。"""
        assert parse_experience("") == (None, None, False)
        assert parse_experience(None) == (None, None, False)
        assert parse_experience("见岗位说明") == (None, None, False)


# ===================================================================
# 五、技能标签抽取
# ===================================================================
class TestExtractSkills:
    def test_matches_title(self):
        hits = extract_skills({"title": "Java开发工程师"})
        ids = {h.skill_id for h in hits}
        assert "java" in ids

    def test_case_insensitive(self):
        assert {h.skill_id for h in extract_skills({"title": "JAVA开发"})} >= {"java"}

    def test_alias_is_normalized_to_one_skill(self):
        """别名（spring boot / mybatis …）要归一到同一个 skill_id"""
        for text in ("Spring Boot开发", "MyBatis开发", "J2EE工程师"):
            ids = {h.skill_id for h in extract_skills({"title": text})}
            assert "java" in ids, text

    def test_dictionary_has_no_duplicate_skill_id(self):
        """同一 skill_id 只能有一条词条，否则归一就白做了"""
        from config import SKILL_DICT
        ids = [e[0] for e in SKILL_DICT]
        assert len(ids) == len(set(ids)), "SKILL_DICT 里 skill_id 重复"

    def test_aliases_are_unique_within_entry(self):
        """同一条目里的别名不该重复（重复不影响结果，但说明词典没人维护）"""
        from config import SKILL_DICT
        for skill_id, _name, _cat, aliases in SKILL_DICT:
            assert len(aliases) == len(set(aliases)), f"{skill_id} 有重复别名"

    def test_short_ascii_alias_respects_word_boundary(self):
        """
        ★ "cpp" 不该命中 "scppd"。

        两三个字母的别名若直接做子串匹配，go 会命中 google、ai 会命中 said、
        it 会命中 retail。对纯 ASCII 短别名加词边界是必须的。
        """
        assert {h.skill_id for h in extract_skills({"title": "scppd"})} == set()
        assert {h.skill_id for h in extract_skills({"title": "cpp"})} == {"cpp"}
        assert {h.skill_id for h in extract_skills({"title": "使用C++开发"})} == {"cpp"}

    def test_word_boundary_blocks_alias_inside_word(self):
        """"ai" 出现在单词内部不算命中，独立成词才算"""
        assert "ai" not in {h.skill_id for h in extract_skills({"title": "said"})}
        assert "ai" in {h.skill_id for h in extract_skills({"title": "AI工程师"})}

    def test_chinese_alias_does_not_need_boundary(self):
        hits = extract_skills({"title": "后端开发工程师"})
        assert "backend" in {h.skill_id for h in hits}

    def test_hits_merge_across_fields(self):
        """
        ★ 同一技能在多个字段命中要合并成一条，否则技能热度会重复计数。
        """
        hits = extract_skills({
            "title": "Java开发",
            "major_requirement": "计算机类、Java方向",
            "job_category": "软件开发",
        })
        java = [h for h in hits if h.skill_id == "java"]
        assert len(java) == 1, "同一技能只应有一条"
        assert java[0].source_field == "title,major_requirement", \
            "命中的字段要全部记下来，按来源优先级排序"

    def test_source_field_follows_priority(self):
        hits = extract_skills({"title": "软件开发", "job_category": "软件开发"})
        software = [h for h in hits if h.skill_id == "software"][0]
        assert software.source_field == "title,job_category"

    def test_hit_text_records_alias(self):
        """要能回答"这个标签凭什么打给它" """
        hits = extract_skills({"title": "Spring Boot开发"})
        java = [h for h in hits if h.skill_id == "java"][0]
        assert "spring boot" in java.hit_text.lower()

    def test_language_field_is_a_source(self):
        hits = extract_skills({"language_req": "英语六级、日语N1"})
        ids = {h.skill_id for h in hits}
        assert {"english", "japanese"} <= ids

    def test_empty_row_gives_nothing(self):
        assert extract_skills({}) == []
        assert extract_skills({"title": "", "job_category": ""}) == []

    def test_result_is_deterministic(self):
        """同样的输入必须给出同样的顺序，否则构建结果不可复现"""
        row = {"title": "Java后端开发", "job_category": "软件开发",
               "major_requirement": "计算机类"}
        assert extract_skills(row) == extract_skills(row)

    def test_skill_meta_is_carried(self):
        hits = extract_skills({"title": "Java开发"})
        java = [h for h in hits if h.skill_id == "java"][0]
        assert java.skill_name == "Java"
        assert java.skill_category == "技术栈"

    @pytest.mark.parametrize("text, expected", [
        ("营销岗", "marketing"),
        ("营销经理", "marketing"),
        ("业务拓展", "sales"),
        ("技术支持工程师", "service"),
        ("海外技术支持工程师", "service"),
        ("党群工作岗", "partyoffice"),
    ])
    def test_real_world_gaps_are_covered(self, text, expected):
        """
        ★ 这几个词是**跑完真实数据后**发现的词典缺口，逐个锁住。

        实跑 294 条岗位时有 5 条一个标签都抽不出来。原因不是抽取逻辑错，
        而是词典里根本没收录「营销」「业务拓展」「技术支持」「党群」
        这几个站点上很常见的写法。补齐后零标签岗位从 5 条降到 0 条。
        """
        ids = {h.skill_id for h in extract_skills({"title": text,
                                                   "job_category": text})}
        assert expected in ids, f"{text} 应该命中 {expected}"

    def test_dictionary_is_not_padded_for_coverage(self):
        """
        反面提醒：别为了让「零标签岗位」这个数字好看，就把明显不是技能的
        词硬塞进词典。这条用例固定住一个**不该命中**的输入。
        """
        assert extract_skills({"title": "驻外越南专员"}) == [], \
            "地名/外派说明不是技能标签，不该被收录"


# ===================================================================
# 六、行变换（纯函数）
# ===================================================================
FULL_ROW = {
    "job_key": "xmu:1", "source_job_id": "1",
    "company": "某公司", "title": "数据分析师",
    "salary": "10-15万/年",
    "city": "福建省厦门市",
    "city_detail": "福建省厦门市思明区",
    "education": "本科",
    "experience": "1-3年",
    "industry": "信息传输、软件和信息技术服务业",
    "company_nature": "国有企业", "company_scale": "500-1000人",
    "job_category": "数据分析", "language_req": "英语",
    "headcount": "16人",
    "major_requirement": "统计学、数学类",
    "publish_date": "2026-09-20", "deadline": "2026-10-01",
    "url": "https://jy.xmu.edu.cn/job/view/id/1",
}


def src_row(jid, **kw) -> dict:
    """
    造一行 **ODS 源数据**——也就是 build(rows=...) 要的形状。

    ⚠️ 注意 build() 要的不是 transform() 的产出：构建流程内部会对每行
    再调一次 transform。传已清洗过的行会被二次清洗，薪资/城市/学历
    会全部变成"未知"，而且不报错（core/dwd.py 的 build() 里有一道
    闸门专门拦这件事）。
    """
    row = dict(FULL_ROW, job_key=f"xmu:{jid}", source_job_id=str(jid),
               company=f"公司{jid}", title=f"岗位{jid}")
    row.update(kw)
    return row


class TestTransform:
    def test_full_row(self):
        d = transform(FULL_ROW)
        assert d["job_key"] == "xmu:1"
        assert d["salary_parsed"] == 1
        assert d["salary_min"] == 8333 and d["salary_period"] == "年"
        assert (d["city_province"], d["city_name"], d["city_district"]) == \
            ("福建省", "厦门市", "思明区")
        assert d["education_level"] == "本科"
        assert (d["experience_min"], d["experience_max"]) == (1, 3)
        assert d["headcount_num"] == 16

    def test_city_detail_wins_over_list_city(self):
        """详情页的完整地点比列表页的市级更完整，优先用它"""
        d = transform({**FULL_ROW, "city": "福建省厦门市",
                       "city_detail": "福建省厦门市集美区"})
        assert d["city_district"] == "集美区"

    def test_falls_back_to_list_city(self):
        """详情没抓到（city_detail 空）时退回列表页的城市，而不是留空"""
        d = transform({**FULL_ROW, "city_detail": ""})
        assert d["city_name"] == "厦门市"

    def test_unknown_salary_is_flagged(self):
        d = transform({**FULL_ROW, "salary": "面议"})
        assert d["salary_parsed"] == 0
        assert d["salary_min"] is None
        assert d["salary_raw"] == "面议", "原始值仍要保留"

    def test_empty_row_does_not_crash(self):
        d = transform({})
        assert d["job_key"] == ""
        assert d["salary_parsed"] == 0
        assert (d["city_province"], d["city_name"], d["city_district"]) == ("", "", "")
        assert d["education_level"] == ""
        assert d["experience_unlimited"] == 0
        assert d["headcount_num"] is None

    def test_does_not_mutate_input(self):
        row = dict(FULL_ROW)
        transform(row)
        assert row == FULL_ROW, "纯函数不该改入参"

    def test_output_keys_match_table_columns(self):
        """transform 的键要与 dwd_job_detail 的列对齐，否则 INSERT 会炸"""
        from core.dwd import CREATE_DETAIL_SQL
        keys = set(transform(FULL_ROW))
        for col in ("job_key", "salary_min", "city_district",
                    "education_level", "experience_unlimited", "headcount_num"):
            assert col in keys, f"建表有 {col}，transform 却没产出"
        assert "dwd_job_detail" in CREATE_DETAIL_SQL


# ===================================================================
# 七、构建与查询
# ===================================================================
def _job(jid, **kw) -> Job:
    """造一个"已抓到详情"的岗位，字段与 ODS 快照对齐"""
    fields = {
        "company": f"公司{jid}", "title": f"岗位{jid}",
        "city": "福建省厦门市", "salary": "8000-12000",
        "education": "本科", "publish_date": "2026-09-20",
        "major_requirement": "数学类", "deadline": "2026-10-01",
        "industry": "制造业", "company_nature": "国有企业",
        "company_scale": "10000人以上",
        "experience": "不限", "job_category": "软件开发",
        "language_req": "英语", "headcount": "3",
        "city_detail": "福建省厦门市思明区",
        "detail_schema_version": DETAIL_SCHEMA_VERSION,
        "source": "厦门大学就业信息网",
        "url": f"https://jy.xmu.edu.cn/job/view/id/{jid}",
        "source_job_id": str(jid), "job_key": make_job_key(str(jid)),
        "detail_fetched": True,
    }
    fields.update(kw)
    job = Job(**fields)
    job.list_hash = list_fingerprint(job)
    return job


class TestDwdBuild:
    def test_build_writes_both_tables(self, tmp_path):
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        stats = dwd.build(rows=[src_row(1), src_row(2)])
        assert stats["jobs"] == 2
        assert dwd.count_details() == 2
        assert stats["skills"] == dwd.count_skills()

    def test_build_is_idempotent(self, tmp_path):
        """
        ★ 同一批数据构建两次，结果必须完全一致。

        DWD 表 100% 由 ODS 推导，没有独立价值，所以采用"先清空再写入"。
        换来的好处是幂等性天然成立，不必额外维护去重逻辑。
        """
        rows = [src_row(1), src_row(2)]
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")

        s1 = dwd.build(rows=rows)
        first = dwd.query_details()
        s2 = dwd.build(rows=rows)
        second = dwd.query_details()

        assert s1["jobs"] == s2["jobs"]
        assert s1["skills"] == s2["skills"]
        keys = ("job_key", "salary_min", "city_district", "education_level")
        assert [tuple(r[k] for k in keys) for r in first] == \
               [tuple(r[k] for k in keys) for r in second]

    def test_build_clears_stale_rows(self, tmp_path):
        """上一轮构建的行必须被清掉，否则旧岗位会永远留在明细表里"""
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        dwd.build(rows=[src_row(1), src_row(2)])
        assert dwd.count_details() == 2

        dwd.build(rows=[src_row(1)])
        assert dwd.count_details() == 1
        assert dwd.query_details()[0]["job_key"] == "xmu:1"

    def test_rows_without_job_key_are_skipped(self, tmp_path):
        """没有稳定主键的行无法与上游对齐，不进明细层"""
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        stats = dwd.build(rows=[src_row(1, job_key="")])
        assert stats["jobs"] == 0
        assert dwd.count_details() == 0

    def test_transformed_rows_are_rejected_loudly(self, tmp_path):
        """
        ★ 把 transform() 的结果喂给 build() 必须**报错**，不能静默出垃圾。

        二次清洗的后果是薪资/城市/学历全部变成"未知"：行数对、字段齐全、
        不抛异常，只有数值全是空的。这种错靠人盯分布图才发现得了，
        所以 build() 里专门设了一道闸门把它变成一句明确的报错。
        """
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        with pytest.raises(ValueError, match="ODS 源数据行"):
            dwd.build(rows=[transform(FULL_ROW)])

    def test_dwd_version_is_stamped(self, tmp_path):
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        dwd.build(rows=[src_row(1)])
        row = dwd.query_details()[0]
        assert row["dwd_version"] == DWD_VERSION
        assert row["built_at"], "要留下构建时间，便于回答「这张表是什么时候算的」"

    def test_empty_build_is_safe(self, tmp_path):
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        stats = dwd.build(rows=[])
        assert stats["jobs"] == 0
        assert stats["avg_skills"] == 0.0

    def test_skill_rows_point_at_real_details(self, tmp_path):
        """技能明细必须挂在存在的岗位行上，否则 JOIN 会丢数据"""
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        dwd.build(rows=[src_row(1), src_row(2)])
        detail_keys = {r["job_key"] for r in dwd.query_details()}

        with_dtype = dwd.top_skills(limit=100)
        assert with_dtype, "样例数据里应该能抽出技能标签"

        from core.dbutil import connect
        with connect(dwd.db_path) as conn:
            skill_keys = {r[0] for r in conn.execute(
                "SELECT DISTINCT job_key FROM dwd_job_skill")}
        assert skill_keys <= detail_keys, "有技能行指向了不存在的岗位"


class TestSummarizeAndQueries:
    @pytest.fixture
    def built(self, tmp_path):
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        rows = [
            src_row(1, salary="8000-12000",
                    city_detail="福建省厦门市思明区"),
            src_row(2, salary="面议", city_detail="福建省厦门市"),
            src_row(3, salary="20-30万/年", city_detail="上海市浦东新区",
                    title="Java开发"),
        ]
        dwd.build(rows=rows)
        return dwd

    def test_summarize_counts(self, built):
        s = built.summarize(rows=built.query_details())
        assert s["jobs"] == 3
        assert s["salary_parsed"] == 2, "面议那条不该算解析成功"
        assert s["salary_parsed_rate"] == 66.7
        assert s["city_district"] == 2, "只有市级的那条拆不出区"
        assert s["education_level"] == 3

    def test_summarize_reports_zero_label_jobs(self, built):
        """零标签岗位的比例是技能词典迭代的抓手，必须在概览里看得见"""
        s = built.summarize(rows=built.query_details())
        assert s["jobs_with_skill"] + s["jobs_without_skill"] == 3
        assert "avg_skills" in s

    def test_top_skills_orders_by_job_count(self, built):
        rows = built.top_skills(limit=10)
        assert rows, "应该有技能命中"
        counts = [r["job_count"] for r in rows]
        assert counts == sorted(counts, reverse=True)

    def test_top_skills_dedups_by_job(self, built):
        """同一岗位同一技能只有一行，热度不能靠重复行虚增"""
        for r in built.top_skills(limit=50):
            assert r["job_count"] <= 3

    def test_salary_by_city(self, built):
        rows = built.salary_by_city(limit=10)
        names = [r["city_name"] for r in rows]
        assert "厦门市" in names and "上海市" in names
        xiamen = [r for r in rows if r["city_name"] == "厦门市"][0]
        assert xiamen["job_count"] == 2
        # 面议那条薪资为 NULL，不该被算成 0 拉低均值
        assert xiamen["avg_salary"] == 10000

    def test_salary_by_city_excludes_unknown_city(self, tmp_path):
        dwd = DwdRepository(db_path=tmp_path / "dwd.db")
        dwd.build(rows=[src_row(9, city="", city_detail="")])
        assert dwd.salary_by_city() == []


class TestBuildFromOds:
    """
    端到端：DWD 从 ODS 取数、且**绝不回写 ODS**。
    这是分层架构里最硬的一条约束（见 core/dwd.py 模块开头的约定 1）。
    """

    def test_build_reads_ods(self, tmp_path):
        db = tmp_path / "chain.db"
        ods = OdsRepository(db_path=db)
        ods.save_snapshots("b1", [_job("1"), _job("2"), _job("3")])

        dwd = DwdRepository(db_path=db)
        stats = dwd.build()

        assert stats["jobs"] == 3, "三个岗位都要进明细层"
        assert stats["salary_parsed"] == 3
        assert stats["city_district"] == 3, "详情页有完整地点，应拆出区县"

    def test_build_does_not_touch_ods(self, tmp_path):
        """★ 硬约束：构建 DWD 前后，ODS 的快照数与岗位数一个都不能变"""
        db = tmp_path / "chain.db"
        ods = OdsRepository(db_path=db)
        ods.save_snapshots("b1", [_job("1"), _job("2")])
        before = (ods.count_snapshots(), ods.count_jobs())

        DwdRepository(db_path=db).build()

        after = (ods.count_snapshots(), ods.count_jobs())
        assert after == before, "DWD 只读 ODS，一个字节都不该动"

    def test_rebuild_reflects_ods_changes(self, tmp_path):
        """ODS 变了，重跑一次构建就应该反映出来（派生层应有的行为）"""
        db = tmp_path / "chain.db"
        ods = OdsRepository(db_path=db)
        ods.save_snapshots("b1", [_job("1", salary="8000-12000")])

        dwd = DwdRepository(db_path=db)
        dwd.build()
        assert dwd.query_details()[0]["salary_min"] == 8000

        ods.save_snapshots("b2", [_job("1", salary="20000-30000")])
        dwd.build()
        row = dwd.query_details()[0]
        assert row["salary_min"] == 20000, "取最新快照"
        assert dwd.count_details() == 1, "岗位数不变，不该重复"

    def test_skipped_snapshot_does_not_blank_fields(self, tmp_path):
        """
        ★ 跳过详情写下的快照（detail_fetched=0）字段是空的。
        DWD 取数必须退回"最近一次真正抓到详情"的那条，
        否则一次增量跳过就会把明细层的新字段全洗空。
        （与 core/ods.py 的 build_source_rows() 兜底逻辑对应）
        """
        db = tmp_path / "chain.db"
        ods = OdsRepository(db_path=db)
        ods.save_snapshots("b1", [_job("1")])

        skipped = _job("1")
        for name in ("experience", "job_category", "language_req",
                     "headcount", "city_detail", "major_requirement"):
            setattr(skipped, name, "")
        skipped.detail_fetched = False
        ods.save_snapshots("b2", [skipped])

        dwd = DwdRepository(db_path=db)
        dwd.build()
        row = dwd.query_details()[0]
        assert row["city_district"] == "思明区", "区县必须从含详情的快照兜底"
        assert row["experience_raw"] == "不限"
        assert row["job_category"] == "软件开发"

    def test_five_new_dimensions_land_in_dwd(self, tmp_path):
        db = tmp_path / "chain.db"
        ods = OdsRepository(db_path=db)
        ods.save_snapshots("b1", [_job("1")])

        dwd = DwdRepository(db_path=db)
        dwd.build()
        row = dwd.query_details()[0]
        assert row["experience_raw"] == "不限"
        assert row["experience_unlimited"] == 1, "站点写「不限」→ 无门槛"
        assert row["job_category"] == "软件开发"
        assert row["language_req"] == "英语"
        assert row["headcount_num"] == 3

    def test_tables_are_created_on_init(self, tmp_path):
        """构造函数就该把两张表建好，查询入口不必先 build 一次"""
        dwd = DwdRepository(db_path=tmp_path / "fresh.db")
        assert dwd.count_details() == 0
        assert dwd.count_skills() == 0
