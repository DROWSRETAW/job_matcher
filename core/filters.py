# -*- coding: utf-8 -*-
"""
岗位条件筛选模块
----------------
把「用户想看什么样的岗位」抽象成一个可传递、可保存、可测试的条件对象 JobFilter。

职责边界（两个模块各管一件事，不要互相掺和）：
    core/matcher.py  —— 打分：这个岗位对口不对口（客观，与用户偏好无关）
    core/filters.py  —— 筛选：这个岗位我要不要看（主观，取决于用户条件）

筛选条件的取值优先级：
    命令行显式参数  >  --filter-file 指定的方案文件  >  代码默认值

关于「为什么在内存里筛，而不是全写进 SQL」：
    当前库中数据量在 10^3 量级，全量取回再筛选是毫秒级，代价可忽略；
    换来的是条件组合可以被单元测试、被序列化成 JSON 反复复用。
    若数据量涨到 10^5 以上，应把 city / match_score / match_level 这些
    能走索引的条件下推到 SQL，只把薪资解析这类需要正则的条件留在内存。
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from config import TARGET_CITY, PROVINCE_CITIES


# ===================================================================
# 薪资解析
# ===================================================================
def parse_salary_to_monthly(text) -> int:
    """
    把薪资文本解析成「月薪下限（元）」，无法解析时返回 0。

    支持的真实格式（来自厦大就业网实测）：
        "7000-10000"     -> 7000
        "8000"           -> 8000
        "8000元/月"      -> 8000
        "10-15万/年"     -> 8333   （按 10 万/年 折算到月）
        "年薪20万"       -> 16666
        "面议" / ""      -> 0      （未知）

    返回 0 表示「未知」，调用方应把未知薪资的岗位**保留**而不是剔除——
    信息缺失不等于不满足条件，宁可多给用户看一条。
    """
    if text is None:
        return 0
    t = str(text).strip()
    if not t:
        return 0
    if "面议" in t or "待定" in t or "negotiable" in t.lower():
        return 0

    nums = re.findall(r"\d+(?:\.\d+)?", t)
    if not nums:
        return 0

    low = float(nums[0])
    # 按年计薪：出现「万」或「年」，且明确不是按月
    is_annual = ("万" in t) or ("年" in t and "月" not in t)
    if is_annual:
        return int(low * 10000 / 12)
    return int(low)


# ===================================================================
# 筛选条件对象
# ===================================================================
@dataclass
class JobFilter:
    """
    一组岗位筛选条件。所有字段都是可选的，不填即表示不限制。

    :param city:          城市（支持多个，任一命中即保留）。如 ["厦门", "福州"]
    :param min_score:     最低匹配分
    :param levels:        匹配等级集合，如 ["S", "A"]
    :param keyword:       关键词，匹配「公司名 + 岗位名」
    :param major:         专业要求文本必须包含的字样，如 "数学"
    :param education:     学历要求必须包含的字样，如 "本科"
    :param salary_min:    最低月薪（元），用于过滤明显低于预期的岗位
    :param exclude:       排除关键词，命中任一即剔除（如 ["销售", "客服"]）
    :param bachelor_only: 是否只保留本科可投的岗位（剔除仅招硕博的）
    :param top:           最多返回条数，0 表示不限制
    """
    city: List[str] = field(default_factory=list)
    min_score: int = 0
    levels: List[str] = field(default_factory=list)
    keyword: str = ""
    major: str = ""
    education: str = ""
    salary_min: int = 0
    exclude: List[str] = field(default_factory=list)
    bachelor_only: bool = True
    top: int = 0

    # ---------------------------------------------------------------
    # 序列化：让一套条件可以存下来反复用
    # ---------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "city": self.city,
            "min_score": self.min_score,
            "levels": self.levels,
            "keyword": self.keyword,
            "major": self.major,
            "education": self.education,
            "salary_min": self.salary_min,
            "exclude": self.exclude,
            "bachelor_only": self.bachelor_only,
            "top": self.top,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "JobFilter":
        """从字典构造；未知字段直接忽略，避免方案文件多写字段就崩溃"""
        if not data:
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path) -> str:
        """把当前条件保存为 JSON 方案文件"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(p)

    @classmethod
    def load(cls, path) -> "JobFilter":
        """从 JSON 方案文件载入条件；文件不存在时返回空条件（不报错）"""
        p = Path(path)
        if not p.exists():
            return cls()
        return cls.from_dict(json.loads(p.read_text(encoding="utf-8")))

    # ---------------------------------------------------------------
    # 城市优先级排序
    # ---------------------------------------------------------------
    @staticmethod
    def city_priority(city_text: str, target: str) -> int:
        """
        城市优先级排序键，返回值越小越靠前。

            0 = 目标城市（如"厦门"）
            1 = 同省其他城市（福建省的福州/泉州…）
            2 = 其他城市 / 城市未知

        注意：这是**排序**不是**过滤**。全国性平台抓到的非目标城市岗位
        不会因为这一条被丢弃——真正的取舍交给 JobFilter.city 决定。
        """
        city_text = city_text or ""
        if not city_text:
            return 2
        if target and target in city_text:
            return 0
        if target:
            for province, cities in PROVINCE_CITIES.items():
                if target in cities:
                    if province in city_text or any(c in city_text for c in cities):
                        return 1
        return 2

    # ---------------------------------------------------------------
    # 筛选 + 排序
    # ---------------------------------------------------------------
    def _match(self, row: dict) -> bool:
        """判断单条岗位是否满足全部条件（全部条件为 AND 关系）"""
        # ---- 城市 ----
        if self.city:
            job_city = row.get("city") or ""
            if not any(c in job_city for c in self.city):
                return False

        # ---- 匹配分 ----
        if self.min_score and (row.get("match_score") or 0) < self.min_score:
            return False

        # ---- 匹配等级 ----
        if self.levels:
            wanted = {lv.upper() for lv in self.levels}
            if (row.get("match_level") or "").upper() not in wanted:
                return False

        # ---- 关键词（公司名 + 岗位名）----
        if self.keyword:
            haystack = f"{row.get('company', '')} {row.get('title', '')}"
            if self.keyword.lower() not in haystack.lower():
                return False

        # ---- 专业要求包含字样 ----
        if self.major and self.major not in (row.get("major_requirement") or ""):
            return False

        # ---- 学历要求包含字样 ----
        # 学历为空的岗位视为「未知」，保留
        if self.education:
            edu = row.get("education") or ""
            if edu and self.education not in edu:
                return False

        # ---- 仅保留本科可投 ----
        if self.bachelor_only:
            text = (row.get("education") or "") + (row.get("major_requirement") or "")
            if ("硕士" in text or "研究生" in text or "博士" in text) \
                    and "本科" not in text:
                return False

        # ---- 排除关键词 ----
        if self.exclude:
            haystack = " ".join([
                str(row.get("company") or ""),
                str(row.get("title") or ""),
                str(row.get("major_requirement") or ""),
            ])
            if any(x and x in haystack for x in self.exclude):
                return False

        # ---- 最低月薪 ----
        # 薪资未知（面议/空）不剔除：信息缺失不等于不满足
        if self.salary_min:
            monthly = parse_salary_to_monthly(row.get("salary"))
            if monthly and monthly < self.salary_min:
                return False

        return True

    def apply(self, rows: List[dict]) -> List[dict]:
        """
        对岗位字典列表执行「筛选 + 排序」。

        排序规则：目标城市优先 -> 匹配分降序 -> 公司名。
        这里才是「厦门优先」真正生效的地方——以前在 matcher 里排完序
        又被按分数重排覆盖掉，等于没排。

        :param rows: storage.query_jobs() 返回的岗位字典列表
        :return: 筛选排序后的新列表（不修改入参）
        """
        target = self.city[0] if self.city else TARGET_CITY
        kept = [r for r in rows if self._match(r)]
        kept.sort(key=lambda r: (
            self.city_priority(r.get("city") or "", target),
            -(r.get("match_score") or 0),
            r.get("company") or "",
        ))
        if self.top and self.top > 0:
            kept = kept[: self.top]
        return kept

    # ---------------------------------------------------------------
    # 可读描述
    # ---------------------------------------------------------------
    def describe(self) -> List[str]:
        """把当前条件转成人话，用于打印和排查「为什么筛出来是空」"""
        items = []
        if self.city:
            items.append(f"城市={'/'.join(self.city)}")
        if self.min_score:
            items.append(f"匹配分≥{self.min_score}")
        if self.levels:
            items.append(f"等级={'/'.join(self.levels)}")
        if self.keyword:
            items.append(f"关键词「{self.keyword}」")
        if self.major:
            items.append(f"专业要求含「{self.major}」")
        if self.education:
            items.append(f"学历含「{self.education}」")
        if self.salary_min:
            items.append(f"月薪≥{self.salary_min}")
        if self.exclude:
            items.append(f"排除={'/'.join(self.exclude)}")
        if self.bachelor_only:
            items.append("仅本科可投")
        if self.top:
            items.append(f"最多{self.top}条")
        return items or ["（无条件，返回全部）"]
