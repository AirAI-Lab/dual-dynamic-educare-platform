"""双动态成绩、三色预警、三缺失画像与低利害成长指引计算引擎。"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from typing import Any, Iterable


def safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None:
        return None
    if denominator == 0 or math.isnan(numerator) or math.isnan(denominator):
        return 0.0
    return numerator / denominator


def clamp(value: float | None, lower: float, upper: float, default: float = 0.0) -> float:
    if value is None or math.isnan(value):
        return default
    return max(lower, min(upper, float(value)))


def parse_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def score_color(value: float, settings: dict[str, Any]) -> str:
    green = float(settings.get("green_threshold", 90))
    blue = float(settings.get("blue_threshold", 60))
    if value >= green:
        return "green"
    if value >= blue:
        return "blue"
    return "red"


def coefficient_color(value: float | None, settings: dict[str, Any]) -> str:
    if value is None:
        return "unknown"
    if value < float(settings.get("coefficient_red_threshold", 0.50)):
        return "red"
    if value < float(settings.get("coefficient_blue_threshold", 0.80)):
        return "blue"
    return "green"


def state_type(idg: float, adg: float, settings: dict[str, Any]) -> str:
    green = float(settings.get("green_threshold", 90))
    blue = float(settings.get("blue_threshold", 60))
    if idg < blue and adg < blue:
        return "持续困难型"
    if idg >= blue and adg < blue:
        return "进步改善型"
    if idg < blue and adg >= blue:
        return "短期波动型"
    if idg >= green and adg >= green:
        return "成绩领跑型"
    return "稳定达标型"


def weighted_adg(
    history: Iterable[tuple[str, float]], current_date: str, current_idg: float,
    settings: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    """近期权重高的 ADG。返回值和参与计算的周期明细，便于逐点验收。"""
    merged: dict[str, float] = {}
    for period_date, value in history:
        if value is not None:
            merged[str(period_date)[:10]] = float(value)
    merged[str(current_date)[:10]] = float(current_idg)

    window = int(settings.get("adg_window_days", 42))
    current_day = parse_date(current_date)
    ordered = [
        (parse_date(period), value)
        for period, value in merged.items()
        if 0 <= (current_day - parse_date(period)).days <= window
    ]
    ordered.sort(key=lambda item: item[0])
    periods = [
        {"date": item[0].isoformat(), "idg": round(item[1], 4)} for item in ordered
    ]
    if not ordered:
        return round(float(current_idg), 2), periods

    weights_recent_to_old = [
        float(value)
        for value in settings.get("adg_weights_recent_to_old", [0.50, 0.30, 0.20])
        if float(value) >= 0
    ]
    if not weights_recent_to_old:
        weights_recent_to_old = [0.50, 0.30, 0.20]
    selected = ordered[-len(weights_recent_to_old) :]
    # ordered 为远→近，而配置权重为近→远，因此这里反转后对齐。
    selected_weights = list(reversed(weights_recent_to_old))[: len(selected)]
    weight_sum = sum(selected_weights)
    if weight_sum == 0:
        result = sum(value for _, value in selected) / len(selected)
    else:
        result = sum(value * weight for (_, value), weight in zip(selected, selected_weights)) / weight_sum
    return round(result, 2), periods


def _warning(
    warnings: list[dict[str, str]], scope: str, field: str, message: str
) -> None:
    warnings.append(
        {
            "scope": scope,
            "field": field,
            "severity": "warning" if scope == "observation" else "high",
            "message": message,
        }
    )


def compute_measure(measure: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """将一条过程性观测记录计算为 IDG 及其证据链。"""
    flags: list[dict[str, str]] = []
    evidence: dict[str, dict[str, Any]] = {}

    total_score = measure.get("total_score")
    full_score = measure.get("full_score")
    course_percent = clamp(safe_ratio(total_score, full_score), 0, 100) * 100
    if safe_ratio(total_score, full_score) is not None and safe_ratio(total_score, full_score) > 1:
        flags.append({"type": "score_exceeded", "message": "学生得分超过满分，已按 100 截断"})
    evidence["课程百分得分"] = {"numerator": total_score, "denominator": full_score}

    total_pages = measure.get("total_pages")
    effective_pages = measure.get("effective_pages")
    page_ratio = safe_ratio(effective_pages, total_pages)
    page_stay = measure.get("page_stay_seconds")
    completion_seconds = measure.get("page_completion_seconds")
    rush_seconds = float(settings.get("anti_cheat_rush_seconds", 30))
    min_stay = float(settings.get("anti_cheat_min_page_stay_seconds", 5))
    rush_filtered = False

    if page_stay is not None and page_stay < min_stay:
        effective_pages = 0
        page_ratio = safe_ratio(0, total_pages)
        flags.append(
            {
                "type": "invalid_short_stay",
                "message": f"单页停留低于 {min_stay:g} 秒，本周期有效页数不计入",
            }
        )
    if (
        completion_seconds is not None
        and completion_seconds > 0
        and completion_seconds <= rush_seconds
        and page_ratio == 1
    ):
        page_ratio = 0.0
        rush_filtered = True
        flags.append(
            {
                "type": "rush_learning",
                "message": f"{completion_seconds:g} 秒完成全部页面，触发刷课过滤，课件系数归零",
            }
        )
    evidence["课件有效页"] = {
        "numerator": effective_pages,
        "denominator": total_pages,
    }

    interaction_count = measure.get("interaction_count")
    interaction_standard = measure.get("interaction_standard")
    interaction_ratio = safe_ratio(interaction_count, interaction_standard)
    interaction_coeff = (
        None if interaction_ratio is None else clamp(interaction_ratio, 0, 1)
    )
    if rush_filtered:
        interaction_ratio = 0.0
        interaction_coeff = 0.0
    if interaction_ratio is not None:
        interaction_ratio = clamp(interaction_ratio, 0, 1)
        evidence["有效交互"] = {
            "numerator": interaction_count,
            "denominator": interaction_standard,
        }

    page_weight = float(settings.get("material_page_weight", 0.6))
    interaction_weight = float(settings.get("material_interaction_weight", 0.4))
    page_ratio_clamped = clamp(page_ratio, 0, 1)
    if interaction_ratio is None:
        material_coeff = page_ratio_clamped
        flags.append(
            {
                "type": "interaction_missing",
                "message": "交互分母缺失，课件系数降级为有效页单因子",
            }
        )
    else:
        material_coeff = clamp(
            page_ratio_clamped * page_weight + interaction_ratio * interaction_weight,
            0,
            1,
        )

    announcement_complete = measure.get("announcement_complete")
    total_announcements = measure.get("total_announcements")
    read_seconds = measure.get("announcement_read_seconds")
    media_loaded = measure.get("announcement_media_loaded")
    min_announcement_stay = float(settings.get("announcement_min_stay_seconds", 15))
    if read_seconds is not None and announcement_complete is not None:
        if media_loaded is not None and not bool(media_loaded):
            announcement_complete = 0
            flags.append(
                {
                    "type": "announcement_media_not_loaded",
                    "message": "公告图文未加载完成，完整阅读条数不计入",
                }
            )
        else:
            announcement_complete = min(
                float(announcement_complete),
                math.floor(float(read_seconds) / min_announcement_stay),
            )
    announcement_ratio = safe_ratio(announcement_complete, total_announcements)
    announcement_coeff = clamp(announcement_ratio, 0, 1)
    evidence["公告完整阅读"] = {
        "numerator": announcement_complete,
        "denominator": total_announcements,
    }

    video_coeff = safe_ratio(measure.get("video_seconds"), measure.get("total_video_seconds"))
    if video_coeff is not None:
        video_coeff = clamp(video_coeff, 0, 1)
        evidence["视频观看"] = {
            "numerator": measure.get("video_seconds"),
            "denominator": measure.get("total_video_seconds"),
        }

    idg = clamp(course_percent * material_coeff * announcement_coeff, 0, 100)

    warnings: list[dict[str, str]] = []
    for field, label, value in (
        ("material_coeff", "课件阅读系数", material_coeff),
        ("announcement_coeff", "公告阅读系数", announcement_coeff),
    ):
        color = coefficient_color(value, settings)
        if color == "red":
            _warning(
                warnings,
                "coefficient",
                field,
                f"{label}低于 {settings.get('coefficient_red_threshold', 0.5):.2f}（红区）",
            )
        elif color == "blue":
            _warning(
                warnings,
                "coefficient",
                field,
                f"{label}低于 {settings.get('coefficient_blue_threshold', 0.8):.2f}（蓝区）",
            )

    observation_threshold = float(settings.get("observation_threshold", 1.0))
    for field, label, value in (
        ("video_coeff", "视频观看系数", video_coeff),
        ("interaction_coeff", "互动系数", interaction_coeff),
    ):
        if value is not None and value < observation_threshold:
            _warning(
                warnings,
                "observation",
                field,
                f"{label}低于 1.00（观测标红，不进入 IDG）",
            )

    if material_coeff < 1 or announcement_coeff < 1:
        flags.append({"type": "debt_uncleared", "message": "存在未完成学习任务，欠账不销"})

    return {
        "course_percent": round(course_percent, 2),
        "material_coeff": round(material_coeff, 4),
        "announcement_coeff": round(announcement_coeff, 4),
        "video_coeff": None if video_coeff is None else round(video_coeff, 4),
        "interaction_coeff": None
        if interaction_coeff is None
        else round(interaction_coeff, 4),
        "idg": round(idg, 2),
        "evidence": evidence,
        "flags": flags,
        "warnings": warnings,
    }


def pre_warnings(
    metric_history: list[dict[str, Any]], settings: dict[str, Any]
) -> list[dict[str, str]]:
    """系数连续 3 次蓝区或单次骤降 >0.30 的前置预警。"""
    result: list[dict[str, str]] = []
    if not metric_history:
        return result
    blue = float(settings.get("coefficient_blue_threshold", 0.8))
    for field, label in (
        ("material_coeff", "课件阅读系数"),
        ("announcement_coeff", "公告阅读系数"),
    ):
        values = [item.get(field) for item in metric_history[-3:]]
        if len(values) == 3 and all(value is not None and value < blue for value in values):
            result.append(
                {
                    "scope": "pre_warning",
                    "field": field,
                    "severity": "high",
                    "message": f"{label}连续 3 次低于 {blue:.2f}，需 48 小时内介入",
                }
            )
        if len(metric_history) >= 2:
            previous = metric_history[-2].get(field)
            current = metric_history[-1].get(field)
            if previous is not None and current is not None and previous - current > 0.30:
                result.append(
                    {
                        "scope": "pre_warning",
                        "field": field,
                        "severity": "high",
                        "message": f"{label}较上一周期骤降超过 0.30",
                    }
                )
    return result


def build_profile(metrics: dict[str, Any], adg: float) -> dict[str, Any]:
    """生成不少于 5 维的成长画像与三缺失提示；仅作教育导航，不作心理诊断。"""
    evidence = metrics.get("evidence", {})
    page = evidence.get("课件有效页", {})
    announcement = evidence.get("公告完整阅读", {})
    interaction = evidence.get("有效交互", {})
    video = evidence.get("视频观看", {})

    observed_values = [
        value
        for value in (metrics.get("interaction_coeff"), metrics.get("video_coeff"))
        if value is not None
    ]
    observed = round(sum(observed_values) / len(observed_values), 4) if observed_values else None
    trend = clamp(0.5 + (metrics["idg"] - adg) / 100.0, 0, 1)
    debt_flags = [
        flag for flag in metrics.get("flags", []) if flag.get("type") == "debt_uncleared"
    ]

    dimensions = [
        {
            "name": "信息关注",
            "value": metrics["announcement_coeff"],
            "evidence": "公告完整阅读 / 公告总条数",
        },
        {
            "name": "课件执行",
            "value": metrics["material_coeff"],
            "evidence": "有效页与有效交互（如有）",
        },
        {
            "name": "任务质量",
            "value": round(metrics["course_percent"] / 100, 4),
            "evidence": "学生实得分 / 课程总分",
        },
        {
            "name": "过程参与",
            "value": observed,
            "evidence": "视频/互动观测系数均值（不进 IDG）",
        },
        {
            "name": "成长趋势",
            "value": round(trend, 4),
            "evidence": "以平均动态成绩为起点基线，比较本次 IDG 变化",
        },
        {
            "name": "欠账清偿",
            "value": 0 if debt_flags else 1,
            "evidence": "课件/公告任务是否全部完成",
        },
    ]

    missing: list[dict[str, Any]] = []
    if metrics["announcement_coeff"] < 0.8:
        missing.append(
            {
                "type": "关注力",
                "evidence": f"公告 {announcement.get('numerator')}/{announcement.get('denominator')}",
            }
        )
    if metrics["material_coeff"] < 0.8 or metrics["course_percent"] < 60:
        missing.append(
            {
                "type": "自律性",
                "evidence": f"课件 {page.get('numerator')}/{page.get('denominator')}；课程百分得分 {metrics['course_percent']}",
            }
        )
    if (observed is not None and observed < 0.8) or trend < 0.45:
        detail = f"过程参与 {observed_text(observed)}；趋势 {trend:.2f}"
        missing.append({"type": "内驱力", "evidence": detail})

    return {
        "baseline": round(adg, 2),
        "dimensions": dimensions,
        "three_missing": missing,
        "note": "三缺失画像仅用于学习支持分流，不是心理诊断，不向学生端展示心理评价结果。",
    }


def observed_text(value: float | None) -> str:
    return "缺失" if value is None else f"{value:.2f}"


GUIDANCE = {
    "red": (
        "本次即时动态成绩为红标。请先查看分子/分母证据：补做遗漏习题与课件、"
        "回看错题，并设定今天可完成的最小行动目标；如持续困难，请主动联系老师。"
    ),
    "blue": (
        "本次即时动态成绩为蓝标，距离绿标不远。建议优先补齐未读公告/课件，"
        "保持每天查看课程通知，并用自己的平均动态曲线观察趋势。"
    ),
    "green": (
        "本次即时动态成绩为绿标。请继续保持过程投入，可用成长曲线复盘方法，"
        "尝试帮助同伴讲解一个知识点，形成稳定的学习习惯。"
    ),
}


def guidance_for(color: str, metrics: dict[str, Any]) -> str:
    base = GUIDANCE.get(color, GUIDANCE["blue"])
    candidates = [
        ("公告阅读系数", metrics["announcement_coeff"]),
        ("课件阅读系数", metrics["material_coeff"]),
        ("课程百分得分", metrics["course_percent"] / 100),
    ]
    weakest_name, weakest_value = min(candidates, key=lambda item: item[1])
    if weakest_value < 0.8:
        return f"{base} 当前最可改变点是{weakest_name}。"
    return base
