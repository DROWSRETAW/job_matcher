# -*- coding: utf-8 -*-
"""
检索接口抓取逻辑的单元测试
==========================
覆盖 2026-09-20 抓取策略改造中新增/重写的部分：

    1. SearchProfile 的 URL 构造（码表翻译、多专业逗号编码、分页）
    2. 检索页列表解析 parse_list（9 个字段）
    3. 分页上限解析 _parse_max_page
    4. 详情页锚点定位法 _parse_meta_block（旧版本踩坑点，防止回归）
    5. 需求专业解析 _parse_majors
    6. 内嵌数据两层解码 decode_embedded_html（含异常输入）

全部为离线测试，不发起网络请求。
页面片段取自 2026-09-20 对 jy.xmu.edu.cn 的真实抓取结果。
"""
import base64
import json
import sys
import zlib
from pathlib import Path

import pytest

# 允许直接 `pytest tests/` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import MAJOR_CODES, DEFAULT_MAJOR_KEYS  # noqa: E402
from core.decoder import decode_embedded_html, probe_encoding, unzip_base64  # noqa: E402
from core.models import Job, make_job_key  # noqa: E402
from spiders.xmu_career import (  # noqa: E402
    SearchProfile, XmuCareerSpider, profile_scope, describe_scope,
)
from main import _can_judge_missing  # noqa: E402


# ===================================================================
# 真实页面片段（fixture）
# ===================================================================
LIST_FRAGMENT = """
<div class="job-box"><ul class="list">
<li data-id="2401083">
<div class="right"><img alt="" src="/attachment/xdu/avatar/a.png"/></div>
<div class="left">
  <div class="job">
    <div class="company">
      <a href="/company/view/id/1107806" target="_blank">厦门天马微电子有限公司</a>
      <div><ul><li>制造业</li><li>10000人以上</li></ul></div>
    </div>
    <div class="name">
      <a href="/job/view/id/2401083" target="_blank" title="研发类、智能制造类、职能类等相关岗位">研发类、智能制造类、职能类等相关岗位</a>
      <span>2026-09-20</span>
    </div>
    <div class="salary">
      <p class="text-orange">9000-25000</p>
      <ul><li>福建省厦门市</li><li>全职</li><li>本科</li></ul>
    </div>
  </div>
</div>
<span class="selected-status"></span>
</li>
<li data-id="2401052">
<div class="left">
  <div class="job">
    <div class="company">
      <a href="/company/view/id/1107807" target="_blank">厦门中远海运集装箱运输有限公司</a>
      <div><ul><li>交通运输、仓储和邮政业</li><li>500-1000人</li></ul></div>
    </div>
    <div class="name">
      <a href="/job/view/id/2401052" target="_blank" title="法务风控岗">法务风控岗</a>
      <span>2026-09-20</span>
    </div>
    <div class="salary">
      <p class="text-orange">7000-10000</p>
      <ul><li>福建省厦门市湖里区</li><li>全职</li><li>本科</li></ul>
    </div>
  </div>
</div>
</li>
</ul></div>
"""

PAGINATION_FRAGMENT = """
<div class="right"><div class="pages clearfix"><ul class="page" id="yw1">
<li class="previous "><a href="/job/search/city/350200/do123/jy.xmu.edu.cn/domain/xdu">上一页</a></li>
<li class="page selected"><a href="/job/search/city/350200/do123/jy.xmu.edu.cn/domain/xdu">1</a></li>
<li class="page"><a href="/job/search/city/350200/do123/jy.xmu.edu.cn/domain/xdu/page/2">2</a></li>
<li class="page"><a href="javascript:void(0)">...</a></li>
<li class="page"><a href="/job/search/city/350200/do123/jy.xmu.edu.cn/domain/xdu/page/29">29</a></li>
<li class="next"><a href="/job/search/city/350200/do123/jy.xmu.edu.cn/domain/xdu/page/2">下一页</a></li>
</ul></div></div>
"""


# ===================================================================
# 1. 检索条件与 URL 构造
# ===================================================================
class TestSearchProfile:
    def test_default_matches_luge_profile(self):
        """默认条件应与卢兄的投递口径一致"""
        p = SearchProfile()
        assert p.city == "厦门"
        assert p.education == "本科"
        assert p.category == "全职"
        assert p.majors == DEFAULT_MAJOR_KEYS

    def test_city_code_translation(self):
        """中文城市名要翻译成行政区划代码，站点不认中文"""
        assert SearchProfile(city="厦门").to_path() == (
            "/job/search/city/350200/d_education/101"
            "/d_category/100/d_major/s112003%2Cs112017%2Cs112002"
        )
        assert "/city/350100/" in SearchProfile(city="福州").to_path()

    def test_raw_code_passthrough(self):
        """直接传代码也应可用（便于临时试验）"""
        assert "/city/350200/" in SearchProfile(city="350200").to_path()
        assert "/d_education/101/" in SearchProfile(city="厦门", education="101").to_path()

    def test_major_param_format(self):
        """专业参数格式：每个带 s 前缀，逗号分隔"""
        p = SearchProfile(majors=["信息与计算科学", "数学类", "统计学"])
        assert p._major_param() == "s112003,s112017,s112002"

    def test_major_param_accepts_raw_codes(self):
        p = SearchProfile(majors=["112003", "s112017"])
        assert p._major_param() == "s112003,s112017"

    def test_major_param_ignores_unknown(self):
        assert SearchProfile(majors=["不存在的专业"])._major_param() == ""

    def test_majors_are_url_encoded(self):
        """逗号必须编码成 %2C，否则多专业参数会被截断"""
        url = SearchProfile(majors=["信息与计算科学", "数学类"]).to_path()
        assert "s112003%2Cs112017" in url
        assert "," not in url

    def test_pagination_only_from_page_two(self):
        p = SearchProfile()
        assert "/page/" not in p.to_path(1)
        assert p.to_path(2).endswith("/page/2")
        assert p.to_path(16).endswith("/page/16")

    def test_unlimited_education_and_category_omitted(self):
        """「不限」应完全不出现该参数，而不是传空值"""
        url = SearchProfile(city="厦门", education="不限", category="不限", majors=[]).to_path()
        assert "d_education" not in url
        assert "d_category" not in url

    def test_time_unlimited_omitted(self):
        """站点的 time=0 表示不限，等价于不传，URL 里应省略"""
        assert "/time/" not in SearchProfile(time_range="不限").to_path()
        assert "/time/7" in SearchProfile(time_range="近1周").to_path()

    def test_empty_city_omitted(self):
        assert "/city/" not in SearchProfile(city="").to_path()

    def test_serialization_roundtrip(self, tmp_path):
        prof = SearchProfile(city="福州", education="硕士", majors=["统计学"],
                             time_range="近1周", salary_min=8000)
        f = tmp_path / "search.json"
        prof.save(f)
        loaded = SearchProfile.load(f)
        assert loaded.to_dict() == prof.to_dict()
        assert loaded.to_path(2) == prof.to_path(2)

    def test_load_missing_file_returns_default(self, tmp_path):
        assert SearchProfile.load(tmp_path / "nope.json").to_dict() == SearchProfile().to_dict()

    def test_from_dict_ignores_unknown_keys(self):
        """方案文件多写字段不应导致崩溃"""
        p = SearchProfile.from_dict({"city": "厦门", "未来的字段": 1})
        assert p.city == "厦门"


# ===================================================================
# 2. 检索页列表解析
# ===================================================================
class TestParseList:
    @pytest.fixture
    def spider(self):
        return XmuCareerSpider(fetch_detail=False)

    def test_parses_all_items(self, spider):
        jobs = spider.parse_list(LIST_FRAGMENT, "http://x/job/search")
        assert len(jobs) == 2

    def test_first_item_fields(self, spider):
        job = spider.parse_list(LIST_FRAGMENT, "http://x/job/search")[0]
        assert job.company == "厦门天马微电子有限公司"
        assert job.title == "研发类、智能制造类、职能类等相关岗位"
        assert job.salary == "9000-25000"
        assert job.city == "福建省厦门市"
        assert job.education == "本科"
        assert job.url == "https://jy.xmu.edu.cn/job/view/id/2401083"

    def test_second_item_fields(self, spider):
        """第二条的薪资/城市不能串到第一条去（曾因正则跨层解析出过错）"""
        job = spider.parse_list(LIST_FRAGMENT, "http://x/job/search")[1]
        assert job.title == "法务风控岗"
        assert job.city == "福建省厦门市湖里区"
        assert job.salary == "7000-10000"

    def test_major_left_empty_for_detail_stage(self, spider):
        """列表页本来就没有「需求专业」，必须留空由详情页补"""
        for job in spider.parse_list(LIST_FRAGMENT, "http://x/job/search"):
            assert job.major_requirement == ""

    # ---- 单位行业 / 单位规模（2026-09-23 新增）----
    def test_extracts_company_industry_and_scale(self, spider):
        """
        公司名下方的嵌套 <ul> 是「单位行业 / 单位规模」。

        此前只取了 .salary 那个 ul，这两个字段被静默丢弃，
        直接导致「城市 × 行业」这个分析维度在设计上不可能成立。
        """
        jobs = spider.parse_list(LIST_FRAGMENT, "http://x/job/search")
        assert jobs[0].industry == "制造业"
        assert jobs[0].company_scale == "10000人以上"
        assert jobs[1].industry == "交通运输、仓储和邮政业"
        assert jobs[1].company_scale == "500-1000人"

    def test_company_nature_left_empty_on_list_page(self, spider):
        """
        列表页的「全职」是**工作性质**，不是**单位性质**，不能张冠李戴。

        单位性质（国有企业 / 事业单位…）只有详情页有，列表阶段必须留空，
        由详情页补。若这里错填了「全职」，整列数据都是错的且不易发现。
        """
        for job in spider.parse_list(LIST_FRAGMENT, "http://x/job/search"):
            assert job.company_nature == ""

    def test_two_uls_do_not_cross_contaminate(self, spider):
        """
        ★ 列表项里有两个 <ul>（公司块、薪资块），不能取串。

        若把 `.company ul` 和 `.salary ul` 混用，城市会变成「制造业」、
        学历会变成「10000人以上」——而且长得像正常数据，极难发现。
        """
        jobs = spider.parse_list(LIST_FRAGMENT, "http://x/job/search")
        assert jobs[0].city == "福建省厦门市"      # 来自 .salary ul
        assert jobs[0].education == "本科"
        assert jobs[0].industry == "制造业"        # 来自 .company ul
        assert jobs[0].industry not in (jobs[0].city, jobs[0].education)

    def test_missing_company_ul_does_not_break_other_fields(self, spider):
        """公司块缺失（站点偶发）时，城市/薪资/学历仍要正常解析"""
        fragment = """
        <ul class="list"><li data-id="2401099">
          <div class="left"><div class="job">
            <div class="company"><a href="/company/view/id/1">某公司</a></div>
            <div class="name"><a href="/job/view/id/2401099" title="数据开发">数据开发</a>
              <span>2026-09-20</span></div>
            <div class="salary"><p class="text-orange">9000-12000</p>
              <ul><li>福建省厦门市集美区</li><li>全职</li><li>本科</li></ul></div>
          </div></div>
        </li></ul>
        """
        job = spider.parse_list(fragment, "http://x/job/search")[0]
        assert job.city == "福建省厦门市集美区"
        assert job.salary == "9000-12000"
        assert job.education == "本科"
        assert job.industry == "" and job.company_scale == ""

    def test_no_items_returns_empty(self, spider):
        assert spider.parse_list("<div>暂无数据</div>", "http://x") == []

    def test_jid_extraction(self):
        assert XmuCareerSpider._jid_of(
            "https://jy.xmu.edu.cn/job/view/id/2401083") == "2401083"
        assert XmuCareerSpider._jid_of("") == ""
        assert XmuCareerSpider._jid_of("https://jy.xmu.edu.cn/company/view/id/1") == ""


# ===================================================================
# 3. 分页
# ===================================================================
class TestPagination:
    def test_max_page_from_tail_links(self):
        """分页链接里夹着模板附加段，只应识别 /page/N"""
        assert XmuCareerSpider._parse_max_page(PAGINATION_FRAGMENT) == 29

    def test_single_page_when_no_pagination(self):
        assert XmuCareerSpider._parse_max_page(LIST_FRAGMENT) == 1


# ===================================================================
# 4. 详情页锚点定位法（旧版本踩坑点）
# ===================================================================
class TestMetaBlock:
    def test_parses_fixed_line_order(self):
        """
        真实行序：薪资 / '|' / 城市 / '|' / 性质 / '|' / 学历
        —— '|' 是独立的一行，不是行内分隔符。这是旧版本的解析 bug 来源。
        """
        lines = [
            "27届校招-游戏数值策划",
            "7000-10000",
            "|",
            "福建省厦门市思明区",
            "|",
            "全职",
            "|",
            "本科",
            "职位收藏 投递简历 完善简历",
            "2026-09-03",
            "浏览次数：123",
        ]
        meta = XmuCareerSpider._parse_meta_block(lines)
        assert meta["salary"] == "7000-10000"
        assert meta["city"] == "福建省厦门市思明区"
        assert meta["job_nature"] == "全职"
        assert meta["education"] == "本科"
        assert meta["publish_date"] == "2026-09-03"

    def test_handles_tilde_salary(self):
        meta = XmuCareerSpider._parse_meta_block(["X", "8000~12000", "|", "福建省厦门市"])
        assert meta["salary"] == "8000-12000"

    def test_falls_back_to_negotiable(self):
        meta = XmuCareerSpider._parse_meta_block(["X", "薪资：面议", "|", "福建省厦门市"])
        assert meta["salary"] == "面议"
        assert meta["city"] == "福建省厦门市"

    def test_no_salary_returns_empty(self):
        meta = XmuCareerSpider._parse_meta_block(["X", "Y", "Z"])
        assert meta["salary"] == ""


# ===================================================================
# 5. 需求专业解析
# ===================================================================
class TestParseMajors:
    def test_strips_education_tags(self):
        """去掉【本科】这类学历前缀标签；单逗号分隔保留原样"""
        text = ("需求专业：【本科】汉语言文学,【本科】信息与计算科学,"
                "【本科】数学类 职位详情 单位介绍")
        assert XmuCareerSpider._parse_majors(text) == "汉语言文学,信息与计算科学,数学类"

    def test_collapses_repeated_separators(self):
        """连续多个分隔符会被折叠（站点偶尔出现 ",," 或 "、、"）"""
        text = "需求专业：数学类,,统计学、、计算机类 职位详情"
        assert XmuCareerSpider._parse_majors(text) == "数学类、统计学、计算机类"

    def test_missing_field_returns_empty(self):
        assert XmuCareerSpider._parse_majors("职位详情 无专业字段") == ""

    def test_handles_fullwidth_colon(self):
        text = "需求专业：【本科】统计学 职位详情"
        assert XmuCareerSpider._parse_majors(text) == "统计学"


# ===================================================================
# 6. 单位属性解析（2026-09-23 新增）
# ===================================================================
# 真实结构，取自 2026-09-23 对 jy.xmu.edu.cn 详情页的抓取结果
COMPANY_BLOCK_FRAGMENT = """
<div class="info"><div style="padding-top: 15px;">
  <div class="item"><label class="label">单位性质：</label><span>国有企业</span></div>
  <div class="item"><label class="label">单位行业：</label><span>交通运输、仓储和邮政业</span></div>
  <div class="item"><label class="label">单位规模：</label><span>10000人以上</span></div>
</div></div>
"""

DETAIL_PAGE_FRAGMENT = """
<html><head><title>数据开发工程师-厦门大学就业信息网</title></head><body>
<div>需求专业：【本科】信息与计算科学,【本科】数学类 职位详情 单位介绍 工作地址</div>
<div>7000-10000</div><div>|</div><div>福建省厦门市思明区</div>
<div>|</div><div>全职</div><div>|</div><div>本科</div>
<div>2026-09-03</div><div>厦门某科技有限公司</div>
<div>单位性质：</div><div>国有企业</div>
<div>单位行业：</div><div>制造业</div>
<div>单位规模：</div><div>1000-5000人</div>
</body></html>
"""


class TestCompanyBlock:
    """
    详情页底部的「单位性质 / 单位行业 / 单位规模」。

    这两个字段是 P0 的补齐点：页面上一直有，解析一直没取。
    找不到时的表现是整列为空——不会报错，所以必须有测试锁住。
    """

    @staticmethod
    def _lines(html: str):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        return [l.strip() for l in soup.get_text("\n", strip=True).split("\n")
                if l.strip()]

    def test_structured_label_span(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(COMPANY_BLOCK_FRAGMENT, "lxml")
        block = XmuCareerSpider._parse_company_block(
            soup, self._lines(COMPANY_BLOCK_FRAGMENT))

        assert block["nature"] == "国有企业"
        assert block["industry"] == "交通运输、仓储和邮政业"
        assert block["scale"] == "10000人以上"

    def test_line_fallback_when_classes_change(self):
        """
        站点改版换掉 class 时，按行扫描仍要能取出值。

        只留一条路（结构化选择器）的风险是：改版后整列静默变空。
        留兜底路的收益就是这里——降级成「可能少几条」，而不是全空。
        """
        from bs4 import BeautifulSoup
        lines = ["单位性质：", "国有企业", "单位行业：",
                 "交通运输、仓储和邮政业", "单位规模：", "10000人以上"]
        soup = BeautifulSoup("<div>页面结构变了，没有 label</div>", "lxml")

        block = XmuCareerSpider._parse_company_block(soup, lines)
        assert block["nature"] == "国有企业"
        assert block["industry"] == "交通运输、仓储和邮政业"
        assert block["scale"] == "10000人以上"

    def test_inline_value_on_same_line(self):
        """值写在标签同一行（'单位性质：国有企业'）也要认"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div></div>", "lxml")
        block = XmuCareerSpider._parse_company_block(
            soup, ["单位性质：国有企业", "单位行业：制造业", "单位规模：少于50人"])

        assert block["nature"] == "国有企业"
        assert block["industry"] == "制造业"
        assert block["scale"] == "少于50人"

    def test_missing_block_returns_empty(self):
        """字段确实没有时要返回空，不能瞎凑"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div>什么都没有</div>", "lxml")
        block = XmuCareerSpider._parse_company_block(
            soup, ["职位详情", "单位介绍"])

        assert block == {"nature": "", "industry": "", "scale": ""}

    def test_not_confused_by_nav_words(self):
        """
        ★ 导航里的「事业单位」「单位服务」「单位问卷调查」不能被当成字段。

        用 startswith 匹配 + 只认「单位性质 / 单位行业 / 单位规模」三个前缀，
        正是为了防这一类误命中。
        """
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div></div>", "lxml")
        block = XmuCareerSpider._parse_company_block(
            soup, ["事业单位", "单位服务", "单位问卷调查", "单位介绍"])

        assert block == {"nature": "", "industry": "", "scale": ""}

    def test_parse_wires_fields_into_job(self):
        """
        端到端：解析器算出来的值，必须真的落到 Job 上。

        「解析写好了但没接线」是本项目反复出现的失败模式——
        函数正确、单测也过，但产出对象上字段永远是空的。
        """
        spider = XmuCareerSpider(fetch_detail=False)
        jobs = spider.parse(DETAIL_PAGE_FRAGMENT,
                            "https://jy.xmu.edu.cn/job/view/id/2401083")

        assert len(jobs) == 1
        job = jobs[0]
        assert job.industry == "制造业"
        assert job.company_nature == "国有企业"
        assert job.company_scale == "1000-5000人"


class TestDetailCircuitBreaker:
    """
    详情阶段的止损闸（2026-09-23 新增）。

    背景：一次 296 条的详情抓取约 10 分钟，中途站点抽风时每条失败要耗
    3 次重试约 7 秒，剩余 200 条会白烧一小时，最后还一条都存不下。
    连续失败说明「站点整体不可用」，应当迅速放弃、先落库已抓到的部分。

    这里的测试不打网络：直接把 _fetch_job_page 换成假实现，
    只验证「判定与中止」的逻辑本身。
    """

    @staticmethod
    def _jobs(n: int):
        return [
            Job(company=f"公司{i}", title=f"岗位{i}",
                url=f"https://jy.xmu.edu.cn/job/view/id/{1000 + i}",
                source_job_id=str(1000 + i),
                job_key=make_job_key(str(1000 + i)))
            for i in range(n)
        ]

    def _spider(self, outcomes):
        """
        :param outcomes: 每次调用 _fetch_job_page 的结果，True=成功 / False=失败；
                         用尽后一律失败，且记录实际调用次数
        """
        spider = XmuCareerSpider(fetch_detail=True, detail_delay=0)
        calls = {"n": 0}

        def fake_fetch(jid):
            idx = calls["n"]
            calls["n"] += 1
            ok = outcomes[idx] if idx < len(outcomes) else False
            if not ok:
                return None
            # 与真实 _fetch_job_page 的契约保持一致：成功要自己记一次 detail_ok，
            # 循环体只负责 detail_major_found。少这一笔会让统计与真实运行不符。
            spider.stats["detail_ok"] += 1
            job = Job(company="X", title="Y",
                      major_requirement="数学类",
                      company_nature="国有企业",
                      url=f"https://jy.xmu.edu.cn/job/view/id/{jid}")
            return job

        spider._fetch_job_page = fake_fetch
        return spider, calls

    def test_aborts_after_consecutive_failures(self):
        """★ 连续失败到阈值就中止，不再把剩下的请求发出去"""
        from config import DETAIL_MAX_CONSECUTIVE_FAILURES as LIMIT

        total = LIMIT + 50
        jobs = self._jobs(total)
        spider, calls = self._spider([])          # 全失败

        spider._fetch_detail_loop(jobs)

        assert calls["n"] == LIMIT, "到达阈值后必须立刻停止发请求"
        assert spider.stats["detail_aborted"] == total - LIMIT
        assert all(not j.detail_fetched for j in jobs)

    def test_success_resets_failure_streak(self):
        """
        偶发失败不能触发中止。

        真实情况里中间夹着个别 404 很正常（岗位已被撤下），
        只有「连续」失败才说明站点整体不可用。
        """
        from config import DETAIL_MAX_CONSECUTIVE_FAILURES as LIMIT

        # 每两次成功夹一次失败：连续失败数永远到不了阈值
        outcomes = [True, False] * 60
        jobs = self._jobs(100)
        spider, calls = self._spider(outcomes)

        spider._fetch_detail_loop(jobs)

        assert calls["n"] == 100, "不该中止"
        assert spider.stats["detail_aborted"] == 0
        assert spider.stats["detail_ok"] == 50

    def test_partial_success_is_kept(self):
        """中止前成功抓到的详情必须保留（这是「早停」而不是「丢弃」）"""
        from config import DETAIL_MAX_CONSECUTIVE_FAILURES as LIMIT

        jobs = self._jobs(LIMIT + 20)
        # 先成功 5 条，然后一路失败
        spider, _ = self._spider([True] * 5)
        spider._fetch_detail_loop(jobs)

        assert spider.stats["detail_ok"] == 5
        assert spider.stats["detail_major_found"] == 5
        for j in jobs[:5]:
            assert j.detail_fetched is True
            assert j.major_requirement == "数学类"

    def test_can_be_disabled(self, monkeypatch):
        """阈值设为 0 表示关闭保护：全部失败也要跑完（不静默改变旧行为）"""
        import spiders.xmu_career as mod

        monkeypatch.setattr(mod, "DETAIL_MAX_CONSECUTIVE_FAILURES", 0)

        jobs = self._jobs(50)
        spider, calls = self._spider([])
        spider._fetch_detail_loop(jobs)

        assert calls["n"] == 50
        assert spider.stats["detail_aborted"] == 0


# ===================================================================
# 7. 内嵌数据两层解码
# ===================================================================
def _incompressible(n: int = 600) -> str:
    """
    生成压缩率低的内容。

    必要性：解码器只把长度 >=200 的 base64 串当作候选（避免误匹配普通
    字符串）。若测试内容高度重复（如 "x"*300），zlib 会把它压到几十字节，
    编码后达不到阈值——这是测试用例本身的坑，不是解码器的 bug。
    """
    import random
    rnd = random.Random(20260920)
    return "".join(rnd.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(n))


class TestDecoder:
    @staticmethod
    def _wrap(inner_html: str) -> str:
        """按站点的方式造一个两层编码的页面"""
        lv2 = base64.b64encode(inner_html.encode("utf-8")).decode()
        lv1 = base64.b64encode(zlib.compress(("view2d " + lv2).encode())).decode()
        return f'<script>{{"a"}}$("#c").replaceWith(Base64.decode(unzip("{lv1}")));</script>'

    def test_two_layer_roundtrip(self):
        inner = "<div class='job-box'><ul class='list'>" + \
                "".join(f"<li data-id='{i}'>item{i}</li>" for i in range(30)) + \
                "</ul></div>"
        assert decode_embedded_html(self._wrap(inner)) == inner

    def test_single_layer_returns_layer1(self):
        """只有一层压缩时，应退回第 1 层结果而不是返回空"""
        raw = _incompressible(600)
        lv1 = base64.b64encode(zlib.compress(raw.encode())).decode()
        assert len(lv1) >= 200, "测试内容编码后未达候选阈值，请加大长度"
        page = f'<script>unzip("{lv1}")</script>'
        assert decode_embedded_html(page) == raw

    def test_empty_input(self):
        assert decode_embedded_html("") == ""

    def test_no_candidate(self):
        assert decode_embedded_html("<html>no embedded data</html>") == ""

    def test_bad_base64_does_not_raise(self):
        long_junk = "!" * 300      # 不是合法 base64，且不符合候选正则
        assert decode_embedded_html(f"<script>{long_junk}</script>") == ""

    def test_probe_encoding_reports_state(self):
        inner = _incompressible(600)
        info = probe_encoding(self._wrap(inner))
        assert info["has_candidate"] is True
        assert info["layer1_ok"] is True
        assert info["wrapper"] == "view2d"
        assert info["layer2_ok"] is True
        assert info["layer2_len"] == len(inner)

    def test_unzip_invalid_returns_none(self):
        assert unzip_base64("not-base64!!") is None


# ===================================================================
# 8. 码表完整性
# ===================================================================
class TestCodeTables:
    def test_required_cities(self):
        from config import CITY_CODES
        for name in ["厦门", "福州", "福建", "深圳"]:
            assert name in CITY_CODES
            assert CITY_CODES[name].isdigit()

    def test_luge_major_codes_present(self):
        for name in DEFAULT_MAJOR_KEYS:
            assert name in MAJOR_CODES

    def test_major_codes_are_six_digits(self):
        for name, code in MAJOR_CODES.items():
            assert len(code) == 6 and code.isdigit(), f"{name} 的代码不合法：{code}"

    def test_xinxiyujisuan_kexue_code(self):
        """这个代码错了，整个专业筛选就失效"""
        assert MAJOR_CODES["信息与计算科学"] == "112003"


# ===================================================================
# 9. 检索口径指纹
# ===================================================================
class TestProfileScope:
    def test_same_profile_same_scope(self):
        a = SearchProfile(city="厦门", majors=["数学类"])
        b = SearchProfile(city="厦门", majors=["数学类"])
        assert profile_scope(a) == profile_scope(b)

    def test_major_order_does_not_matter(self):
        """专业是 OR 关系，顺序不影响"问的是哪一批岗位" """
        a = SearchProfile(majors=["数学类", "统计学"])
        b = SearchProfile(majors=["统计学", "数学类"])
        assert profile_scope(a) == profile_scope(b)

    @pytest.mark.parametrize("field, value", [
        ("city", "福州"),
        ("education", "硕士"),
        ("category", "实习"),
        ("time_range", "近1周"),
        ("salary_min", 8000),
        ("nature", "国有企业"),
        ("scale", "500-1000人"),
    ])
    def test_any_scope_field_change_is_detected(self, field, value):
        base = SearchProfile()
        changed = SearchProfile(**{field: value})
        assert profile_scope(base) != profile_scope(changed), \
            f"{field} 变了，口径指纹却没变"

    def test_majors_change_is_detected(self):
        """★ 本次踩到的就是这个：专业口径一变窄，结果数骤降"""
        wide = SearchProfile(majors=[])
        narrow = SearchProfile(majors=list(DEFAULT_MAJOR_KEYS))
        assert profile_scope(wide) != profile_scope(narrow)

    def test_max_pages_is_not_part_of_scope(self):
        """翻页上限不改变"哪些岗位符合条件"，不属于口径"""
        base = SearchProfile(max_pages=None)
        limited = SearchProfile(max_pages=2)
        assert profile_scope(base) == profile_scope(limited)

    def test_accepts_plain_dict(self):
        """批次台账里的 profile_json 反序列化后就是 dict，要能直接比"""
        p = SearchProfile(city="厦门", majors=["数学类"], nature="国有企业")
        assert profile_scope(p.to_dict()) == profile_scope(p)

    def test_accepts_json_string(self):
        """
        ★ 批次台账里存的 profile_json 是**字符串**，不是 dict。
        必须先反序列化再比，否则闸门会直接抛异常（本次踩到过）。
        """
        p = SearchProfile(city="厦门", majors=["数学类"], nature="国有企业")
        raw = json.dumps(p.to_dict(), ensure_ascii=False)
        assert profile_scope(raw) == profile_scope(p)

    def test_dict_with_missing_and_none_fields(self):
        """
        ★ 归一化的意义：老批次的 profile_json 可能缺字段或为 null。
        若直接比较，None vs 默认值会被误判成"口径不同"，
        闸门会把本该成立的下架判定全部挡掉——闸门自己成了故障源。
        """
        p = SearchProfile(city="厦门", majors=["数学类"])
        partial = {"city": "厦门", "majors": ["数学类"],
                   "nature": None, "salary_min": None}
        assert profile_scope(partial) == profile_scope(p)

    @pytest.mark.parametrize("bad", [None, "", "   ", "{}", "{不是json", 123, []])
    def test_unusable_sources_give_empty_scope(self, bad):
        """
        ★ 认不出来就返回空元组，**不能**套一套默认值返回。

        否则"没有可比口径"会被伪装成"口径一致"，闸门就白设了。
        """
        assert profile_scope(bad) == ()

    def test_json_roundtrip_matches(self):
        """profile_json 存的是 JSON，往返一次口径指纹必须不变"""
        import json
        p = SearchProfile(city="厦门", majors=["数学类", "统计学"],
                          nature="国有企业", salary_min=5000)
        restored = json.loads(json.dumps(p.to_dict(), ensure_ascii=False))
        assert profile_scope(restored) == profile_scope(p)

    def test_describe_scope_is_readable(self):
        text = describe_scope(SearchProfile(city="厦门", majors=["数学类"]))
        assert "厦门" in text and "数学类" in text
        assert "不限" in describe_scope(SearchProfile(majors=[]))


# ===================================================================
# 10. 下架判定闸门（main._can_judge_missing 的三条前提）
# ===================================================================
class _FakeSpider:
    """只需要 .stats 里的总页数，用来模拟"翻页有没有触顶" """

    def __init__(self, total_pages=1):
        self.stats = {"total_pages": total_pages}


class _FakeOds:
    """只提供 recent_batches()，喂入构造好的批次台账"""

    def __init__(self, batches):
        self._batches = batches

    def recent_batches(self, limit=20):
        return self._batches[:limit]


def _batch(status, profile):
    import json
    return {"status": status,
            "profile_json": json.dumps(profile.to_dict(), ensure_ascii=False)}


class TestCanJudgeMissing:
    """
    这三条前提决定"会不会把还在招的岗位标成下架"。
    判错方向的代价是单向的：标错了，卢兄会以为岗位没了而放弃投递。
    """

    def _same(self, **kw):
        """上一批与这一批口径完全一致"""
        p = SearchProfile(**kw)
        return p, _FakeOds([_batch("ok", p)])

    def test_all_premises_hold(self):
        profile, ods = self._same()
        can, why = _can_judge_missing(profile, _FakeSpider(), ods)
        assert can is True and why == ""

    def test_time_range_limited_blocks(self):
        """前提 1：检索限定时间时，老岗位不出现属正常"""
        profile = SearchProfile(time_range="近1周")
        ods = _FakeOds([_batch("ok", profile)])
        can, why = _can_judge_missing(profile, _FakeSpider(), ods)
        assert can is False and "近1周" in why

    def test_pagination_truncated_blocks(self):
        """前提 2：翻页触顶时可能只是没翻完"""
        profile, ods = self._same()
        can, why = _can_judge_missing(profile, _FakeSpider(total_pages=60), ods)
        assert can is False and "60" in why

    def test_scope_mismatch_blocks(self):
        """★ 前提 3：口径变窄时，少掉的那些岗位根本没被问过"""
        wide = SearchProfile(majors=[])
        narrow = SearchProfile(majors=list(DEFAULT_MAJOR_KEYS))
        ods = _FakeOds([_batch("ok", wide)])
        can, why = _can_judge_missing(narrow, _FakeSpider(), ods)
        assert can is False, "口径不一致时必须不判定"
        assert "口径" in why

    def test_scope_mismatch_message_names_both_sides(self):
        """提示语要说清两侧口径，否则排查时不知道该改哪个参数"""
        wide = SearchProfile(majors=[])
        narrow = SearchProfile(majors=["数学类"])
        ods = _FakeOds([_batch("ok", wide)])
        _, why = _can_judge_missing(narrow, _FakeSpider(), ods)
        assert "数学类" in why and "不限" in why

    def test_city_change_blocks(self):
        """换城市也是换口径——福州的口径推不出厦门的岗位在不在"""
        xiamen = SearchProfile(city="厦门")
        fuzhou = SearchProfile(city="福州")
        ods = _FakeOds([_batch("ok", xiamen)])
        can, _ = _can_judge_missing(fuzhou, _FakeSpider(), ods)
        assert can is False

    def test_no_comparable_batch_blocks(self):
        """
        ★ 找不到可比口径时**不判定**（宁可漏判也不错杀）。

        触发场景：ODS 里有历史数据，但那批数据是旧版本写的、
        台账里没有 profile_json。此时谁也无法确认口径一致。
        """
        can, why = _can_judge_missing(SearchProfile(), _FakeSpider(),
                                      _FakeOds([]))
        assert can is False and "口径" in why

    def test_failed_batches_are_skipped(self):
        """失败批次的口径不可信，应跳过它继续往前找"""
        good = SearchProfile(majors=[])
        ods = _FakeOds([
            {"status": "running", "profile_json": ""},
            {"status": "failed", "profile_json": "{}"},
            _batch("ok", good),
        ])
        can, _ = _can_judge_missing(SearchProfile(majors=[]), _FakeSpider(), ods)
        assert can is True, "应跳过 running/failed，采用那条 ok 的口径"

    def test_running_current_batch_is_not_used(self):
        """
        当前批在此刻还是 running（close_batch 在判定之后才执行），
        所以它不会被当成"上一批"——否则永远自比自，闸门形同虚设。
        """
        prev = SearchProfile(majors=[])
        now = SearchProfile(majors=["数学类"])
        ods = _FakeOds([
            {"status": "running",
             "profile_json": json.dumps(now.to_dict(), ensure_ascii=False)},
            _batch("ok", prev),
        ])
        can, _ = _can_judge_missing(now, _FakeSpider(), ods)
        assert can is False, "应拿 running 之前的 ok 批次来比"

    def test_default_profile_matches_wide_batch(self):
        """
        反向确认：库里那 294 条是用 majors=[] 建的，
        所以再用 majors=[] 跑时闸门必须放行（不能把正常判定也挡掉）。
        """
        wide = SearchProfile(majors=[])
        ods = _FakeOds([_batch("ok", wide)])
        can, why = _can_judge_missing(SearchProfile(majors=[]),
                                      _FakeSpider(), ods)
        assert can is True, f"正常口径被误挡：{why}"
