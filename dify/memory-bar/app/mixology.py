"""调酒逻辑：把客人今晚说的话，调成一杯有名字的酒。

输入是 seekdb 混合检索出的「客人记忆」与「风味卡」，
输出是可直接展示的 Markdown 草稿（Dify 的 LLM 节点只负责润色，不负责编造）。
"""

from __future__ import annotations

import hashlib
import random
from datetime import date

MOOD_KEYWORDS: list[tuple[str, list[str]]] = [
    ("疲惫", ["加班", "累", "困", "熬夜", "疲惫", "忙", "deadline", "通宵", "开会"]),
    ("想家", ["想家", "妈妈", "爸爸", "爸妈", "老家", "故乡", "家书", "桂花"]),
    ("开心", ["开心", "高兴", "升职", "庆祝", "成功", "哈哈", "好运", "中奖"]),
    ("焦虑", ["焦虑", "紧张", "担心", "不安", "压力", "失眠"]),
    ("孤独", ["孤独", "一个人", "没人", "孤单", "空荡"]),
    ("心动", ["喜欢", "心动", "约会", "恋爱", "表白", "她", "他"]),
    ("遗憾", ["分手", "失恋", "错过", "遗憾", "离开", "再见"]),
    ("平静", ["平静", "安静", "散步", "发呆", "江边", "海边", "雨天"]),
]

PREFIX_POOL = [
    "晚风", "凌晨", "旧巷", "雨夜", "月光", "南岸",
    "打烊前", "第七码头", "慢车", "北纬三十度",
]
SUFFIX_POOL = [
    "归途", "小夜曲", "备忘录", "回声", "摇篮曲",
    "余温", "慢板", "气泡", "拾光", "停靠",
]
GLASS_BY_MOOD = {
    "疲惫": "厚底威士忌杯",
    "想家": "粗陶杯",
    "开心": "香槟杯",
    "焦虑": "高球杯",
    "孤独": "直身杯",
    "心动": "马天尼杯",
    "遗憾": "古典杯",
    "平静": "薄玻璃盏",
}
ABV_POOL = ["无酒精", "低度", "微醺", "半醉"]

SHELF_HINTS = ["酒柜", "酒单", "我的记忆", "我上次", "还记得我吗", "我存过"]
CLOSE_HINTS = ["打烊", "再见", "晚安", "拜拜", "今天到这", "先走了"]


def detect_intent(query: str) -> str:
    text = query.strip()
    if any(hint in text for hint in SHELF_HINTS):
        return "shelf"
    if any(hint in text for hint in CLOSE_HINTS) and len(text) <= 24:
        return "close"
    return "brew"


def detect_mood(query: str) -> str:
    for mood, keywords in MOOD_KEYWORDS:
        if any(keyword in query for keyword in keywords):
            return mood
    return ""


def shorten(text: str, limit: int = 34) -> str:
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit] + "…"


def _rng(user_id: str, query: str) -> random.Random:
    seed_text = f"{user_id}|{query}|{date.today().isoformat()}"
    seed = int(hashlib.md5(seed_text.encode("utf-8")).hexdigest()[:8], 16)
    return random.Random(seed)


def _format_date(iso_text: str | None) -> str:
    if not iso_text:
        return "某天"
    try:
        month, day = iso_text[5:7], iso_text[8:10]
        return f"{int(month)}月{int(day)}日"
    except (ValueError, IndexError):
        return iso_text[:10]


def _callback(memories: list[dict]) -> str:
    if not memories:
        return "这是你的第一杯。今晚这句话，会替你记住。"
    memory = memories[0]
    return (
        f"{_format_date(memory.get('created_at'))}你说过「{shorten(memory['content'])}」。"
        "今晚它沉在杯底，慢慢地散开。"
    )


def _mood_of(flavors: list[dict], query: str) -> str:
    explicit = detect_mood(query)
    if explicit:
        return explicit
    for flavor in flavors:
        if flavor.get("mood"):
            return flavor["mood"]
    return "平静"


def render_cocktail(
    user_id: str,
    query: str,
    memories: list[dict],
    flavors: list[dict],
    stats: dict,
) -> dict:
    rng = _rng(user_id, query)
    mood = _mood_of(flavors, query)
    name = f"「{rng.choice(PREFIX_POOL)}{rng.choice(SUFFIX_POOL)}」"

    picked = flavors[:2]
    recipe_lines: list[str] = []
    recipe_meta: list[dict] = []
    for flavor in picked:
        recipe_lines.append(f"- **{flavor['title']}** —— {flavor.get('content', '')}")
        recipe_meta.append({"flavor": flavor["title"], "line": flavor.get("content", "")})
    recipe_lines.append(f"- **你今晚的原话** —— “{shorten(query, 60)}”")

    if flavors:
        toast = flavors[0].get("content", "")
    else:
        toast = "杯子先空着，等你把今天说完，味道才会来。"

    draft = "\n".join(
        [
            f"🍸 **今晚的酒：{name}**",
            f"*{GLASS_BY_MOOD.get(mood, '直身杯')} · {rng.choice(ABV_POOL)} · 今夜特调*",
            "",
            "**配方**",
            *recipe_lines,
            "",
            f"**酒语**：{toast}",
            "",
            f"**回甘**：{_callback(memories)}",
            "",
            f"`今晚第 {stats['glasses_today'] + 1} 杯 · 酒柜里 {stats['memories']} 段记忆 "
            f"· seekdb 混合检索命中 {len(memories) + len(flavors)} 条`",
        ]
    )

    return {
        "intent": "brew",
        "draft": draft,
        "cocktail": {"name": name, "mood": mood, "recipe": recipe_meta},
    }


def render_shelf(memories: list[dict], stats: dict) -> dict:
    if not memories:
        draft = (
            "📜 **你的酒柜**\n\n"
            "还空着。把你今晚的故事说给我听，我就替你收好。\n\n"
            f"`酒柜 0 段记忆 · 今晚第 {stats['glasses_today'] + 1} 杯`"
        )
        return {"intent": "shelf", "draft": draft, "cocktail": None}

    lines = [
        f"📜 **你的酒柜**（{len(memories)} 段记忆）",
        "",
    ]
    for memory in memories:
        lines.append(
            f"- `{_format_date(memory.get('created_at'))}` {shorten(memory['content'], 40)}"
        )
    lines.extend(
        [
            "",
            "这些都被 seekdb 收在杯底，随时可以再翻出来。",
            "",
            f"`酒柜 {stats['memories']} 段记忆 · 累计 {stats['glasses_total']} 杯`",
        ]
    )
    return {"intent": "shelf", "draft": "\n".join(lines), "cocktail": None}


def render_close(memories: list[dict], stats: dict) -> dict:
    farewell = "明天见。路灯会记得你走过的方向。"
    if memories:
        farewell = (
            f"带走今晚这杯的话：{_format_date(memories[0].get('created_at'))}"
            f"你说过「{shorten(memories[0]['content'])}」。\n\n{farewell}"
        )
    draft = "\n".join(
        [
            "🌙 **打烊了。**",
            "",
            f"今晚你在这里存下 {stats['memories']} 段记忆，喝过 {stats['glasses_total']} 杯。",
            "",
            farewell,
        ]
    )
    return {"intent": "close", "draft": draft, "cocktail": None}
