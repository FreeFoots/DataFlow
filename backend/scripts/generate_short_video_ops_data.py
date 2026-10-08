from __future__ import annotations

import csv
import json
import random
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Sequence

from short_video_ops_definitions import DATABASE, ROLE_TABLES, TABLE_META


SEED = 20260829
DATA_START = datetime(2026, 1, 1)
DATA_END = datetime(2026, 8, 28, 23, 59, 59)
DATABASE_DIR = Path(__file__).resolve().parents[1] / "data" / "databases" / DATABASE

REGIONS = {
    "华东": [("上海", "上海"), ("浙江", "杭州"), ("江苏", "南京"), ("山东", "青岛")],
    "华南": [("广东", "广州"), ("广东", "深圳"), ("福建", "厦门"), ("广西", "南宁")],
    "华北": [("北京", "北京"), ("天津", "天津"), ("河北", "石家庄"), ("山西", "太原")],
    "西南": [("四川", "成都"), ("重庆", "重庆"), ("云南", "昆明"), ("贵州", "贵阳")],
    "华中": [("湖北", "武汉"), ("湖南", "长沙"), ("河南", "郑州"), ("江西", "南昌")],
}


class ShortVideoOpsGenerator:
    def __init__(self, output_dir: Path = DATABASE_DIR, seed: int = SEED) -> None:
        self.output_dir = output_dir
        self.random = random.Random(seed)
        self.seed = seed
        self.counts: dict[str, int] = {}
        self.schemas: dict[str, list[str]] = {}
        self.channels: list[dict] = []
        self.users: list[dict] = []
        self.devices: list[dict] = []
        self.campaigns: list[dict] = []
        self.creators: list[dict] = []
        self.categories: list[dict] = []
        self.contents: list[dict] = []
        self.exposures: list[dict] = []
        self.plays: list[dict] = []

    def generate(self) -> dict[str, int]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.generate_users()
        self.generate_growth()
        self.generate_channels()
        self.generate_content()
        self.write_manifest()
        return self.counts

    def write_csv(self, table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]) -> int:
        path = self.output_dir / f"{table}.csv"
        count = 0
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(row)
                count += 1
        self.counts[table] = count
        self.schemas[table] = list(columns)
        return count

    def choice(self, values: Sequence, weights: Sequence[float] | None = None):
        return self.random.choices(values, weights=weights, k=1)[0]

    def random_datetime(self, start: datetime = DATA_START, end: datetime = DATA_END) -> datetime:
        span = max(0, int((end - start).total_seconds()))
        return start + timedelta(seconds=self.random.randint(0, span))

    def location(self) -> tuple[str, str, str]:
        region = self.choice(list(REGIONS), [30, 25, 18, 14, 13])
        province, city = self.random.choice(REGIONS[region])
        return region, province, city

    def generate_users(self) -> None:
        channel_specs = [
            ("CH01", "自然推荐", "organic", 0.82, 0.38),
            ("CH02", "应用商店", "organic", 0.76, 0.33),
            ("CH03", "信息流广告", "paid", 0.67, 0.24),
            ("CH04", "搜索广告", "paid", 0.72, 0.28),
            ("CH05", "达人推广", "paid", 0.78, 0.34),
            ("CH06", "品牌广告", "paid", 0.62, 0.20),
            ("CH07", "好友邀请", "referral", 0.86, 0.43),
            ("CH08", "社交分享", "referral", 0.80, 0.36),
            ("CH09", "线下活动", "offline", 0.64, 0.22),
            ("CH10", "其他渠道", "other", 0.58, 0.17),
        ]
        self.channels = [
            {
                "channel_id": item[0], "channel_name": item[1], "channel_type": item[2],
                "activation_rate": item[3], "day7_rate": item[4],
            }
            for item in channel_specs
        ]
        self.write_csv(
            "acquisition_channels",
            ["channel_id", "channel_name", "channel_type", "owner_team", "enabled"],
            [(item[0], item[1], item[2], "增长运营中心", 1) for item in channel_specs],
        )

        users_rows = []
        profiles_rows = []
        registrations_rows = []
        activations_rows = []
        device_rows = []
        age_groups = ["18岁以下", "18-24岁", "25-30岁", "31-40岁", "41-50岁", "50岁以上"]
        interests = ["搞笑", "生活", "美食", "知识", "体育", "游戏", "音乐", "旅行", "时尚", "亲子"]
        for index in range(1, 12001):
            user_id = f"U{index:07d}"
            registered_at = self.random_datetime()
            channel = self.choice(self.channels, [16, 11, 17, 11, 12, 8, 11, 7, 4, 3])
            region, province, city = self.location()
            platform = self.choice(["Android", "iOS"], [61, 39])
            quality = max(0.05, min(0.98, self.random.gauss(channel["day7_rate"], 0.12)))
            activated = self.random.random() < channel["activation_rate"]
            user = {
                "user_id": user_id,
                "registered_at": registered_at,
                "channel_id": channel["channel_id"],
                "platform": platform,
                "quality": quality,
                "activated": activated,
            }
            self.users.append(user)
            users_rows.append((
                user_id, f"短视频用户{index:06d}", channel["channel_id"],
                registered_at.strftime("%Y-%m-%d %H:%M:%S"),
                self.choice(["active", "inactive", "blocked"], [91, 8, 1]), platform,
            ))
            profiles_rows.append((
                user_id, self.choice(["男", "女", "未知"], [48, 49, 3]),
                self.choice(age_groups, [5, 25, 28, 24, 12, 6]), region, province, city,
                self.choice(["一线", "新一线", "二线", "三线", "四线及以下"], [10, 19, 24, 25, 22]),
                self.random.choice(interests),
                (registered_at + timedelta(days=self.random.randint(0, 60))).strftime("%Y-%m-%d %H:%M:%S"),
            ))
            registrations_rows.append((
                f"REG{index:08d}", user_id, channel["channel_id"],
                registered_at.strftime("%Y-%m-%d %H:%M:%S"),
                self.choice(["手机号", "微信", "Apple", "游客转正"], [53, 34, 8, 5]), platform,
                self.choice(["completed", "completed", "completed", "reviewed"], [50, 25, 20, 5]),
            ))
            activation_time = registered_at + timedelta(minutes=self.random.randint(1, 720))
            activations_rows.append((
                f"ACT{index:08d}", f"REG{index:08d}", user_id,
                int(activated), activation_time.strftime("%Y-%m-%d %H:%M:%S") if activated else "",
                int((activation_time - registered_at).total_seconds()) if activated else "",
                self.choice(["观看3条视频", "观看满5分钟", "完成首次互动"], [44, 34, 22]) if activated else "未达到激活条件",
            ))
            device_count = 1 if self.random.random() < 0.83 else 2
            for _ in range(device_count):
                device_index = len(self.devices) + 1
                device = {
                    "device_id": f"D{device_index:08d}", "user_id": user_id, "device_type": platform,
                }
                self.devices.append(device)
                device_rows.append((
                    device["device_id"], user_id, platform,
                    self.choice(["入门机", "中端机", "高端机"], [25, 51, 24]),
                    self.choice(["9.5.0", "9.6.0", "9.7.0", "9.8.0"], [12, 22, 31, 35]),
                    registered_at.strftime("%Y-%m-%d %H:%M:%S"),
                ))

        self.write_csv("users", ["user_id", "nickname", "register_channel_id", "registered_at", "account_status", "register_platform"], users_rows)
        self.write_csv("user_profiles", ["user_id", "gender", "age_group", "region", "province", "city", "city_tier", "primary_interest", "updated_at"], profiles_rows)
        self.write_csv("user_registrations", ["registration_id", "user_id", "channel_id", "registered_at", "registration_method", "platform", "registration_status"], registrations_rows)
        self.write_csv("user_activations", ["activation_id", "registration_id", "user_id", "is_activated", "activated_at", "activation_seconds", "activation_rule"], activations_rows)
        self.write_csv("user_devices", ["device_id", "user_id", "device_type", "device_level", "app_version", "first_seen_at"], device_rows)

        tags = [
            ("UT01", "新用户", "lifecycle"), ("UT02", "高活跃", "activity"),
            ("UT03", "低活跃", "activity"), ("UT04", "流失风险", "risk"),
            ("UT05", "自然流量", "channel"), ("UT06", "付费流量", "channel"),
            ("UT07", "高互动", "behavior"), ("UT08", "深度观看", "behavior"),
            ("UT09", "内容探索型", "preference"), ("UT10", "垂类偏好", "preference"),
            ("UT11", "召回用户", "lifecycle"), ("UT12", "潜在创作者", "identity"),
        ]
        self.write_csv("user_tags", ["tag_id", "tag_name", "tag_type", "enabled"], [(*item, 1) for item in tags])
        self.write_csv(
            "user_tag_relations", ["relation_id", "user_id", "tag_id", "tag_score", "assigned_at", "source"],
            [
                (f"UTR{index:09d}", self.random.choice(self.users)["user_id"], self.random.choice(tags)[0],
                 round(self.random.uniform(0.55, 0.99), 4), self.random_datetime().strftime("%Y-%m-%d %H:%M:%S"),
                 self.choice(["rule", "model", "manual"], [54, 44, 2]))
                for index in range(1, 24001)
            ],
        )

    def generate_growth(self) -> None:
        activity_rows = []
        retention_rows = []
        session_rows = []
        lifecycle_rows = []
        churn_rows = []
        active_by_day_channel: Counter[tuple[str, str]] = Counter()
        retained_by_day_channel: Counter[tuple[str, str, int]] = Counter()
        registered_by_day_channel: Counter[tuple[str, str]] = Counter()
        activated_by_day_channel: Counter[tuple[str, str]] = Counter()
        devices_by_user: dict[str, list[dict]] = defaultdict(list)
        for device in self.devices:
            devices_by_user[device["user_id"]].append(device)

        for user in self.users:
            registration_date = user["registered_at"].date()
            channel_id = user["channel_id"]
            registered_by_day_channel[(registration_date.isoformat(), channel_id)] += 1
            if user["activated"]:
                activated_by_day_channel[(registration_date.isoformat(), channel_id)] += 1
            max_days = max(0, (DATA_END.date() - registration_date).days)
            active_days: set[int] = set()
            for milestone in (1, 3, 7, 14, 30):
                if milestone > max_days:
                    continue
                probability = user["quality"] * (0.92 ** (milestone / 7))
                retained = self.random.random() < probability
                if retained:
                    active_days.add(milestone)
                    retained_by_day_channel[(registration_date.isoformat(), channel_id, milestone)] += 1
                retention_rows.append((
                    f"RET{len(retention_rows) + 1:09d}", user["user_id"], channel_id,
                    registration_date.isoformat(), milestone,
                    (registration_date + timedelta(days=milestone)).isoformat(), int(retained),
                ))
            extra_count = self.random.randint(1, 15) if user["activated"] else self.random.randint(0, 3)
            for _ in range(extra_count):
                if max_days:
                    active_days.add(self.random.randint(0, max_days))
            for day_offset in sorted(active_days):
                activity_date = registration_date + timedelta(days=day_offset)
                minutes = max(1, int(self.random.gammavariate(2.4, 8 + 18 * user["quality"])))
                sessions = max(1, min(9, int(self.random.gammavariate(1.8, 1.3))))
                plays = max(1, int(minutes * self.random.uniform(1.1, 2.6)))
                interactions = int(plays * self.random.uniform(0.02, 0.18))
                activity_rows.append((
                    f"DA{len(activity_rows) + 1:010d}", user["user_id"], activity_date.isoformat(),
                    minutes, sessions, plays, interactions, int(minutes >= 5),
                ))
                active_by_day_channel[(activity_date.isoformat(), channel_id)] += 1
                for session_number in range(min(sessions, 3)):
                    started = datetime.combine(activity_date, datetime.min.time()) + timedelta(
                        hours=self.random.randint(7, 23), minutes=self.random.randint(0, 59)
                    )
                    duration = max(30, int(minutes * 60 / max(1, sessions)))
                    device = self.random.choice(devices_by_user[user["user_id"]])
                    session_rows.append((
                        f"SES{len(session_rows) + 1:010d}", user["user_id"], device["device_id"], channel_id,
                        started.strftime("%Y-%m-%d %H:%M:%S"),
                        (started + timedelta(seconds=duration)).strftime("%Y-%m-%d %H:%M:%S"), duration,
                        self.choice(["推荐页", "关注页", "搜索页", "消息页"], [70, 14, 10, 6]),
                    ))
            days_since_last = max_days - max(active_days or {0})
            stage = "新用户" if max_days <= 7 else "活跃用户" if days_since_last <= 7 else "沉默用户" if days_since_last <= 30 else "流失用户"
            lifecycle_rows.append((
                f"LCS{len(lifecycle_rows) + 1:08d}", user["user_id"], DATA_END.date().isoformat(), stage,
                len(active_days), days_since_last, round(user["quality"], 4),
            ))
            if stage in {"沉默用户", "流失用户"}:
                churn_rows.append((
                    f"CHURN{len(churn_rows) + 1:08d}", user["user_id"], DATA_END.date().isoformat(),
                    "high" if stage == "流失用户" else "medium", days_since_last,
                    self.choice(["活跃频次下降", "观看时长下降", "连续未登录"], [31, 23, 46]),
                ))

        self.write_csv("user_daily_activity", ["activity_id", "user_id", "activity_date", "active_minutes", "session_count", "play_count", "interaction_count", "is_valid_active"], activity_rows)
        self.write_csv("user_retention", ["retention_id", "user_id", "channel_id", "registration_date", "retention_day", "observation_date", "is_retained"], retention_rows)
        self.write_csv("user_sessions", ["session_id", "user_id", "device_id", "channel_id", "started_at", "ended_at", "duration_seconds", "landing_page"], session_rows)
        self.write_csv("user_lifecycle_snapshots", ["snapshot_id", "user_id", "snapshot_date", "lifecycle_stage", "active_days", "days_since_last_active", "engagement_score"], lifecycle_rows)
        self.write_csv("user_churn_events", ["churn_event_id", "user_id", "identified_date", "churn_level", "inactive_days", "churn_reason"], churn_rows)

        recall_campaigns = []
        for index in range(1, 25):
            started = DATA_START.date() + timedelta(days=self.random.randint(30, 210))
            recall_campaigns.append({
                "id": f"RC{index:04d}", "start": started,
                "channel": self.choice(["push", "sms", "in_app"], [55, 20, 25]),
            })
        self.write_csv(
            "recall_campaigns",
            ["recall_campaign_id", "campaign_name", "target_segment", "touch_channel", "start_date", "end_date", "status"],
            [(item["id"], f"沉默用户召回{index:02d}", self.choice(["沉默7天", "沉默14天", "流失30天"], [45, 35, 20]), item["channel"], item["start"].isoformat(), (item["start"] + timedelta(days=7)).isoformat(), "completed") for index, item in enumerate(recall_campaigns, 1)],
        )
        touches = []
        results = []
        churn_users = [row[1] for row in churn_rows]
        for index in range(1, min(9000, len(churn_users) * 2) + 1):
            campaign = self.random.choice(recall_campaigns)
            user_id = self.random.choice(churn_users)
            sent_at = datetime.combine(campaign["start"], datetime.min.time()) + timedelta(days=self.random.randint(0, 6), hours=self.random.randint(8, 21))
            delivered = self.random.random() < 0.94
            clicked = delivered and self.random.random() < (0.18 if campaign["channel"] == "push" else 0.10)
            touch_id = f"RCT{index:09d}"
            touches.append((touch_id, campaign["id"], user_id, campaign["channel"], sent_at.strftime("%Y-%m-%d %H:%M:%S"), int(delivered), int(clicked)))
            recalled = clicked and self.random.random() < 0.58
            results.append((f"RCR{index:09d}", touch_id, user_id, int(recalled), (sent_at + timedelta(hours=self.random.randint(1, 72))).strftime("%Y-%m-%d %H:%M:%S") if recalled else "", self.random.randint(1, 7) if recalled else 0))
        self.write_csv("recall_touches", ["touch_id", "recall_campaign_id", "user_id", "touch_channel", "sent_at", "delivered", "clicked"], touches)
        self.write_csv("recall_results", ["recall_result_id", "touch_id", "user_id", "recalled", "returned_at", "active_days_after_recall"], results)

        metric_rows = []
        for day_offset in range((DATA_END.date() - DATA_START.date()).days + 1):
            day = (DATA_START.date() + timedelta(days=day_offset)).isoformat()
            for channel in self.channels:
                new_users = registered_by_day_channel[(day, channel["channel_id"])]
                activated = activated_by_day_channel[(day, channel["channel_id"])]
                metric_rows.append((
                    f"GM{len(metric_rows) + 1:08d}", day, channel["channel_id"], new_users, activated,
                    retained_by_day_channel[(day, channel["channel_id"], 1)],
                    retained_by_day_channel[(day, channel["channel_id"], 7)],
                    active_by_day_channel[(day, channel["channel_id"])],
                ))
        self.write_csv("growth_daily_metrics", ["metric_id", "metric_date", "channel_id", "new_users", "activated_users", "day1_retained_users", "day7_retained_users", "daily_active_users"], metric_rows)
        months = sorted({row[1][:7] for row in metric_rows})
        self.write_csv(
            "growth_targets", ["target_id", "target_month", "channel_id", "new_user_target", "activation_rate_target", "day7_retention_target", "owner_name"],
            [
                (f"GT{index:05d}", month, channel["channel_id"], self.random.randint(700, 1800),
                 round(self.random.uniform(0.62, 0.82), 4), round(self.random.uniform(0.22, 0.42), 4),
                 f"增长负责人{channel_index + 1:02d}")
                for index, (month, channel_index, channel) in enumerate(
                    ((m, i, c) for m in months for i, c in enumerate(self.channels)), start=1
                )
            ],
        )

    def generate_channels(self) -> None:
        paid_channels = [item for item in self.channels if item["channel_type"] == "paid"]
        accounts = []
        for index, channel in enumerate(paid_channels, 1):
            for sequence in range(1, 3):
                accounts.append({"id": f"AA{index:02d}{sequence:02d}", "channel_id": channel["channel_id"]})
        self.write_csv("ad_accounts", ["ad_account_id", "account_name", "channel_id", "account_status", "currency"], [(item["id"], f"官方投放账户{item['id']}", item["channel_id"], "active", "CNY") for item in accounts])

        for index in range(1, 101):
            account = self.random.choice(accounts)
            started = DATA_START.date() + timedelta(days=self.random.randint(0, 170))
            self.campaigns.append({
                "id": f"ADC{index:05d}", "account_id": account["id"], "channel_id": account["channel_id"],
                "start": started, "end": min(DATA_END.date(), started + timedelta(days=self.random.randint(25, 90))),
                "budget": self.random.randint(30000, 260000),
            })
        self.write_csv(
            "ad_campaigns", ["ad_campaign_id", "ad_campaign_name", "ad_account_id", "channel_id", "objective", "budget", "start_date", "end_date", "status"],
            [(item["id"], f"拉新计划{index:03d}", item["account_id"], item["channel_id"], self.choice(["注册", "激活", "七日留存"], [50, 32, 18]), item["budget"], item["start"].isoformat(), item["end"].isoformat(), "completed" if item["end"] < DATA_END.date() else "active") for index, item in enumerate(self.campaigns, 1)],
        )
        creative_rows = []
        for campaign in self.campaigns:
            for sequence in range(1, self.random.randint(3, 7)):
                creative_rows.append((f"CR{len(creative_rows) + 1:07d}", campaign["id"], f"素材{campaign['id']}-{sequence}", self.choice(["短视频", "图文", "开屏"], [72, 23, 5]), self.choice(["情绪共鸣", "功能卖点", "达人推荐", "热点跟拍"], [28, 24, 27, 21]), self.choice(["active", "paused"], [88, 12])))
        self.write_csv("ad_creatives", ["creative_id", "ad_campaign_id", "creative_name", "creative_type", "creative_theme", "creative_status"], creative_rows)

        stat_rows = []
        for campaign in self.campaigns:
            channel = next(item for item in self.channels if item["channel_id"] == campaign["channel_id"])
            current = campaign["start"]
            while current <= campaign["end"]:
                impressions = self.random.randint(12000, 95000)
                ctr = self.random.uniform(0.012, 0.052)
                clicks = int(impressions * ctr)
                conversion_rate = channel["activation_rate"] * self.random.uniform(0.10, 0.22)
                conversions = int(clicks * conversion_rate)
                spend = round(impressions / 1000 * self.random.uniform(18, 65), 2)
                stat_rows.append((f"ADS{len(stat_rows) + 1:09d}", campaign["id"], current.isoformat(), impressions, clicks, conversions, spend, round(clicks / impressions, 5), round(spend / max(conversions, 1), 2)))
                current += timedelta(days=1)
        self.write_csv("ad_daily_stats", ["ad_stat_id", "ad_campaign_id", "stat_date", "impressions", "clicks", "conversions", "spend", "click_through_rate", "cost_per_conversion"], stat_rows)

        attribution_rows = []
        conversion_rows = []
        paid_users = [item for item in self.users if item["channel_id"] in {channel["channel_id"] for channel in paid_channels}]
        for index, user in enumerate(paid_users, 1):
            candidates = [item for item in self.campaigns if item["channel_id"] == user["channel_id"] and item["start"] <= user["registered_at"].date() <= item["end"]]
            campaign = self.random.choice(candidates) if candidates else None
            attribution_rows.append((f"ATTR{index:08d}", user["user_id"], user["channel_id"], campaign["id"] if campaign else "", self.choice(["last_click", "first_click", "view_through"], [68, 24, 8]), user["registered_at"].strftime("%Y-%m-%d %H:%M:%S"), round(self.random.uniform(0.62, 0.99), 4)))
            events = ["registration"] + (["activation"] if user["activated"] else [])
            for event_type in events:
                conversion_rows.append((f"CE{len(conversion_rows) + 1:09d}", user["user_id"], user["channel_id"], campaign["id"] if campaign else "", event_type, (user["registered_at"] + timedelta(minutes=self.random.randint(0, 360))).strftime("%Y-%m-%d %H:%M:%S"), 1))
        self.write_csv("channel_attributions", ["attribution_id", "user_id", "channel_id", "ad_campaign_id", "attribution_type", "attributed_at", "confidence_score"], attribution_rows)
        self.write_csv("channel_conversion_events", ["conversion_event_id", "user_id", "channel_id", "ad_campaign_id", "conversion_type", "converted_at", "conversion_value"], conversion_rows)

        months = [f"2026-{month:02d}" for month in range(1, 9)]
        self.write_csv(
            "channel_targets",
            ["channel_target_id", "target_month", "channel_id", "spend_target", "registration_target", "activation_target", "target_cpa"],
            [
                (f"CT{index:05d}", month, channel["channel_id"], self.random.randint(250000, 900000),
                 self.random.randint(800, 2400), self.random.randint(550, 1800),
                 round(self.random.uniform(80, 260), 2))
                for index, (month, channel) in enumerate(
                    ((m, c) for m in months for c in paid_channels), start=1
                )
            ],
        )
        self.write_csv("budget_adjustments", ["adjustment_id", "ad_campaign_id", "adjusted_at", "previous_budget", "new_budget", "adjustment_reason", "operator_name"], [(f"BA{index:06d}", campaign["id"], self.random_datetime(datetime.combine(campaign["start"], datetime.min.time()), datetime.combine(campaign["end"], datetime.max.time())).strftime("%Y-%m-%d %H:%M:%S"), campaign["budget"], int(campaign["budget"] * self.random.uniform(0.65, 1.45)), self.choice(["转化成本偏高", "留存质量较好", "素材衰退", "阶段性加量"], [28, 27, 20, 25]), f"渠道运营{index % 8 + 1:02d}") for index, campaign in enumerate(self.random.choices(self.campaigns, k=700), 1)])

    def generate_content(self) -> None:
        category_specs = [
            ("CAT01", "搞笑", 0.63), ("CAT02", "生活", 0.58), ("CAT03", "美食", 0.66),
            ("CAT04", "知识", 0.72), ("CAT05", "体育", 0.61), ("CAT06", "游戏", 0.57),
            ("CAT07", "音乐", 0.65), ("CAT08", "旅行", 0.69), ("CAT09", "时尚", 0.60),
            ("CAT10", "亲子", 0.70), ("CAT11", "科技", 0.68), ("CAT12", "影视", 0.55),
        ]
        self.categories = [{"id": item[0], "name": item[1], "completion": item[2]} for item in category_specs]
        self.write_csv("content_categories", ["category_id", "category_name", "category_level", "enabled"], [(item[0], item[1], 1, 1) for item in category_specs])

        creator_rows = []
        profile_rows = []
        for index in range(1, 1801):
            creator_id = f"C{index:06d}"
            category = self.random.choice(self.categories)
            level = self.choice(["新星", "成长", "成熟", "头部"], [45, 33, 17, 5])
            quality = max(0.15, min(0.98, self.random.gauss({"新星": 0.47, "成长": 0.58, "成熟": 0.69, "头部": 0.81}[level], 0.12)))
            joined = self.random_datetime(DATA_START - timedelta(days=500), DATA_END)
            creator = {"id": creator_id, "category_id": category["id"], "quality": quality, "joined": joined}
            self.creators.append(creator)
            creator_rows.append((creator_id, f"创作者{index:05d}", level, joined.strftime("%Y-%m-%d %H:%M:%S"), self.choice(["active", "inactive", "restricted"], [91, 7, 2])))
            profile_rows.append((creator_id, category["id"], self.random.randint(30, 1200000), self.random.randint(5, 4000), round(quality, 4), self.choice(["个人", "机构", "媒体"], [86, 10, 4]), DATA_END.date().isoformat()))
        self.write_csv("creators", ["creator_id", "creator_name", "creator_level", "joined_at", "creator_status"], creator_rows)
        self.write_csv("creator_profiles", ["creator_id", "primary_category_id", "follower_count", "published_content_count", "quality_score", "creator_type", "snapshot_date"], profile_rows)

        for index in range(1, 15001):
            creator = self.random.choice(self.creators)
            category = next(item for item in self.categories if item["id"] == creator["category_id"])
            published = self.random_datetime(max(DATA_START, creator["joined"]), DATA_END)
            quality = max(0.08, min(0.99, self.random.gauss((creator["quality"] + category["completion"]) / 2, 0.14)))
            self.contents.append({
                "id": f"V{index:08d}", "creator_id": creator["id"], "category_id": category["id"],
                "published": published, "duration": self.random.randint(8, 180), "quality": quality,
            })
        self.write_csv("contents", ["content_id", "creator_id", "category_id", "content_title", "duration_seconds", "published_at", "content_status", "visibility", "quality_score"], [(item["id"], item["creator_id"], item["category_id"], f"{next(category['name'] for category in self.categories if category['id'] == item['category_id'])}内容{index:06d}", item["duration"], item["published"].strftime("%Y-%m-%d %H:%M:%S"), self.choice(["published", "published", "published", "removed"], [45, 28, 25, 2]), "public", round(item["quality"], 4)) for index, item in enumerate(self.contents, 1)])

        tags = [(f"CTAG{index:03d}", name, tag_type) for index, (name, tag_type) in enumerate([
            ("轻松解压", "emotion"), ("实用技巧", "content"), ("热点跟拍", "trend"),
            ("剧情反转", "content"), ("真实记录", "style"), ("专业讲解", "style"),
            ("高颜值", "style"), ("亲子互动", "audience"), ("年轻人", "audience"),
            ("职场人", "audience"), ("城市生活", "scene"), ("乡村生活", "scene"),
            ("节日热点", "trend"), ("明星娱乐", "trend"), ("赛事热点", "trend"),
            ("测评", "content"), ("教程", "content"), ("挑战赛", "activity"),
        ], 1)]
        self.write_csv("content_tags", ["content_tag_id", "tag_name", "tag_type", "enabled"], [(*item, 1) for item in tags])
        tag_relations = []
        for content in self.contents:
            for tag in self.random.sample(tags, self.random.randint(1, 4)):
                tag_relations.append((f"CTR{len(tag_relations) + 1:09d}", content["id"], tag[0], round(self.random.uniform(0.6, 1), 4), "model"))
        self.write_csv("content_tag_relations", ["content_tag_relation_id", "content_id", "content_tag_id", "confidence_score", "source"], tag_relations)

        content_counts: Counter[str] = Counter()
        content_play_counts: Counter[str] = Counter()
        content_watch_seconds: Counter[str] = Counter()
        creator_play_counts: Counter[str] = Counter()
        creator_interactions: Counter[str] = Counter()
        content_interactions: Counter[str] = Counter()
        creator_content_counts = Counter(item["creator_id"] for item in self.contents)
        exposure_rows = []
        play_rows = []
        interaction_rows = []
        for index in range(1, 140001):
            user = self.random.choice(self.users)
            content = self.random.choice(self.contents)
            exposed_at = self.random_datetime(max(DATA_START, content["published"]), DATA_END)
            position = self.choice(["首页推荐", "关注流", "搜索结果", "同城", "话题页"], [68, 13, 8, 6, 5])
            clicked = self.random.random() < (0.12 + 0.38 * content["quality"])
            exposure_id = f"EXP{index:010d}"
            self.exposures.append({"id": exposure_id, "user_id": user["user_id"], "content": content, "time": exposed_at, "clicked": clicked})
            exposure_rows.append((exposure_id, user["user_id"], content["id"], position, exposed_at.strftime("%Y-%m-%d %H:%M:%S"), int(clicked), self.choice(["兴趣推荐", "热门推荐", "关注关系", "同城推荐"], [55, 24, 13, 8])))
            content_counts[content["id"]] += 1
            if clicked:
                watch = min(content["duration"], max(1, int(content["duration"] * max(0.03, min(1, self.random.gauss(content["quality"], 0.23))))))
                completion = round(watch / content["duration"], 4)
                play_id = f"PLAY{len(play_rows) + 1:010d}"
                play = {"id": play_id, "user_id": user["user_id"], "content": content, "time": exposed_at, "watch": watch}
                self.plays.append(play)
                play_rows.append((play_id, exposure_id, user["user_id"], content["id"], exposed_at.strftime("%Y-%m-%d %H:%M:%S"), watch, completion, int(completion >= 0.95), self.choice(["wifi", "5g", "4g"], [52, 34, 14])))
                content_play_counts[content["id"]] += 1
                content_watch_seconds[content["id"]] += watch
                creator_play_counts[content["creator_id"]] += 1
                interaction_probability = 0.02 + content["quality"] * 0.18
                if self.random.random() < interaction_probability:
                    interaction_type = self.choice(["like", "comment", "share", "follow", "favorite"], [55, 13, 12, 8, 12])
                    interaction_rows.append((f"INT{len(interaction_rows) + 1:010d}", play_id, user["user_id"], content["id"], interaction_type, (exposed_at + timedelta(seconds=watch)).strftime("%Y-%m-%d %H:%M:%S"), 1))
                    creator_interactions[content["creator_id"]] += 1
                    content_interactions[content["id"]] += 1
        self.write_csv("video_exposures", ["exposure_id", "user_id", "content_id", "position_name", "exposed_at", "clicked", "recommendation_source"], exposure_rows)
        self.write_csv("video_plays", ["play_id", "exposure_id", "user_id", "content_id", "played_at", "watch_seconds", "completion_rate", "completed", "network_type"], play_rows)
        self.write_csv("video_interactions", ["interaction_id", "play_id", "user_id", "content_id", "interaction_type", "interacted_at", "valid_interaction"], interaction_rows)

        metric_rows = []
        for content in self.random.sample(self.contents, 9000):
            day = content["published"].date().isoformat()
            exposures = content_counts[content["id"]]
            plays = content_play_counts[content["id"]]
            metric_rows.append((f"CM{len(metric_rows) + 1:08d}", day, content["id"], exposures, plays, content_watch_seconds[content["id"]], content_interactions[content["id"]], round(plays / max(exposures, 1), 4), round(content_watch_seconds[content["id"]] / max(plays * content["duration"], 1), 4)))
        self.write_csv("content_daily_metrics", ["content_metric_id", "metric_date", "content_id", "impressions", "play_count", "watch_seconds", "interaction_count", "play_rate", "completion_rate"], metric_rows)
        creator_metric_rows = [(f"CRM{index:07d}", DATA_END.date().isoformat(), creator["id"], creator_play_counts[creator["id"]], creator_interactions[creator["id"]], creator_content_counts[creator["id"]], round(creator_interactions[creator["id"]] / max(creator_play_counts[creator["id"]], 1), 4)) for index, creator in enumerate(self.creators, 1)]
        self.write_csv("creator_daily_metrics", ["creator_metric_id", "metric_date", "creator_id", "play_count", "interaction_count", "published_count", "interaction_rate"], creator_metric_rows)

        audited = self.random.sample(self.contents, 8000)
        self.write_csv("content_audits", ["audit_id", "content_id", "audited_at", "audit_status", "risk_type", "review_mode"], [(f"AUD{index:08d}", content["id"], (content["published"] + timedelta(minutes=self.random.randint(1, 90))).strftime("%Y-%m-%d %H:%M:%S"), self.choice(["approved", "rejected", "limited"], [94, 3, 3]), self.choice(["无风险", "低俗", "侵权", "虚假信息"], [94, 2, 2, 2]), self.choice(["machine", "human", "mixed"], [69, 8, 23])) for index, content in enumerate(audited, 1)])
        self.write_csv("content_reports", ["report_id", "user_id", "content_id", "report_reason", "reported_at", "process_status"], [(f"RPT{index:08d}", self.random.choice(self.users)["user_id"], self.random.choice(self.contents)["id"], self.choice(["低俗内容", "虚假信息", "侵权搬运", "不感兴趣"], [18, 17, 20, 45]), self.random_datetime().strftime("%Y-%m-%d %H:%M:%S"), self.choice(["processed", "pending", "dismissed"], [74, 8, 18])) for index in range(1, 5001)])

        topics = []
        for index in range(1, 121):
            started = DATA_START.date() + timedelta(days=self.random.randint(0, 220))
            topics.append({"id": f"TOP{index:04d}", "start": started})
        self.write_csv("trending_topics", ["topic_id", "topic_name", "topic_type", "start_date", "end_date", "heat_score", "topic_status"], [(item["id"], f"平台热点话题{index:03d}", self.choice(["社会热点", "节日", "赛事", "娱乐", "平台活动"], [24, 18, 15, 23, 20]), item["start"].isoformat(), (item["start"] + timedelta(days=self.random.randint(2, 12))).isoformat(), self.random.randint(5000, 100000), "ended") for index, item in enumerate(topics, 1)])
        self.write_csv(
            "topic_content_relations",
            ["topic_content_id", "topic_id", "content_id", "joined_at", "ranking", "traffic_boost"],
            [
                (f"TC{index:08d}", topic["id"], content["id"],
                 max(datetime.combine(topic["start"], datetime.min.time()), content["published"]).strftime("%Y-%m-%d %H:%M:%S"),
                 self.random.randint(1, 500), round(self.random.uniform(1, 3.5), 2))
                for index, (topic, content) in enumerate(
                    ((self.random.choice(topics), self.random.choice(self.contents)) for _ in range(4500)), start=1
                )
            ],
        )

    def write_manifest(self) -> None:
        missing = set(TABLE_META) - set(self.counts)
        if missing:
            raise RuntimeError(f"未生成数据表：{', '.join(sorted(missing))}")
        payload = {
            "database": DATABASE,
            "scenario": "短视频平台用户增长、渠道投放与内容运营",
            "seed": self.seed,
            "data_range": {"start": DATA_START.date().isoformat(), "end": DATA_END.date().isoformat()},
            "table_count": len(self.counts),
            "total_rows": sum(self.counts.values()),
            "roles": ROLE_TABLES,
            "tables": {
                table: {
                    "description": TABLE_META[table][0],
                    "domain": TABLE_META[table][1],
                    "row_count": self.counts[table],
                    "columns": self.schemas[table],
                }
                for table in sorted(self.counts)
            },
        }
        (self.output_dir / "_database_manifest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    counts = ShortVideoOpsGenerator().generate()
    print(json.dumps({"database": DATABASE, "tables": len(counts), "rows": sum(counts.values()), "path": str(DATABASE_DIR)}, ensure_ascii=False))
