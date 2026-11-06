# -*- coding: utf-8 -*-
"""Anima 抽卡 —— 纯命令行

两个能力，不依赖 ComfyUI：

  girl   随机女性特征（只出 f 里的标签，不含角色身份，可选双女关系）
  roll   一键随机搭配（服装 / 姿势 / 表情 / 场景）

用法::

    python anima_cli.py girl
    python anima_cli.py girl --pool 全部女性 --count 2 --level explicit --seed 42
    python anima_cli.py roll --level suggestive --no-scene
    python anima_cli.py roll --prompt "1girl, silver hair" --json
    echo "1girl, solo" | python anima_cli.py roll --prompt -

作为库调用::

    from anima_cli import random_girl, wardrobe_roll
    r = random_girl(level="explicit", seed=42)
    r["text"], r["report"], r["claim"]

退出码：成功 0；抽取失败（池为空等）1；参数错误 2。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:            # 允许从任意 cwd 调用本脚本
    sys.path.insert(0, HERE)

import wardrobe_rules                # 搭配 / 场景 / 角色 / 分级规则引擎

PRESET_PATH = os.path.join(HERE, "presets", "anima_presets.json")


# ============================================================================
# 一、槽位契约
# ============================================================================

# 输出顺序（与 anima3-prompt Skill 的槽位规范一致）
SLOT_ORDER = [
    "artist", "count", "character", "appearance", "relation",
    "clothing", "pose", "expression", "scene", "free",
]

# policy:
#   sticky      = 首写锁定，后到者不得覆盖（已写入不被自动覆盖）
#   replaceable = 后写覆盖（换主体即整体重算）
SLOT_META = {
    "artist":     {"label": "画师",     "policy": "sticky"},
    "count":      {"label": "人数性别", "policy": "replaceable"},
    "character":  {"label": "角色身份", "policy": "replaceable"},
    "appearance": {"label": "角色特征", "policy": "replaceable",
                   "note": "不受防覆盖保护：换角色即整体替换"},
    "relation":   {"label": "关系/互动", "policy": "replaceable",
                   "note": "双女性时由等级决定：SFW=正常互动 / NSFW=百合"},
    "clothing":   {"label": "服装",     "policy": "sticky"},
    "pose":       {"label": "姿势",     "policy": "sticky"},
    "expression": {"label": "表情",     "policy": "sticky"},
    "scene":      {"label": "场景",     "policy": "sticky"},
    "free":       {"label": "其他/自由", "policy": "replaceable"},
}

TIER_RANK = {"locked": 3, "explicit": 2, "derived": 1, "default": 0}


# ============================================================================
# 二、标签归属词典（用于解析上游纯文本）
# ============================================================================

_PRESET_CACHE = {"mtime": None, "index": None}

COUNT_RE = re.compile(r"^(solo|1girl|1boy|2girls|2boys|3girls|multiple girls|"
                      r"multiple boys|no humans|1other|\d+girls?|\d+boys?)$", re.I)

APPEARANCE_PATTERNS = [
    # 发色/瞳色可带限定词：short white hair / long brown hair / orange eyes
    re.compile(r"^[a-z\-]{1,20}( [a-z\-]{1,20}){0,2} hair$"),
    re.compile(r"^[a-z\-]{1,20}( [a-z\-]{1,20}){0,2} eyes$"),
    re.compile(r"^(twintails|ponytail|braid|bob cut|hime cut|ahoge|sidelocks|"
               r"wavy hair|curly hair|messy hair|wet hair|hair intakes|"
               r"hair between eyes|hair ornament|hair ribbon|hairclip|hair clip|"
               r"mole under eye|eyelashes|tsurime|tareme|heterochromia|"
               r"heart-shaped pupils|fang|pointy ears|animal ears|cat ears|fox ears|"
               r"horns|tail|wings|halo|small breasts|large breasts|medium breasts|"
               r"flat chest|huge breasts|thick thighs|wide hips|narrow waist|"
               r"muscular|slim|petite|chubby|tall|short|pale skin|dark skin|tan|"
               r"tanlines|freckles|skin fang|abs|toned)$"),
]


def _load_presets():
    """读 presets/anima_presets.json，按 mtime 缓存。"""
    try:
        mtime = os.path.getmtime(PRESET_PATH)
    except OSError:
        return {"sections": {}}
    if _PRESET_CACHE["mtime"] != mtime:
        with open(PRESET_PATH, encoding="utf-8") as f:
            _PRESET_CACHE["index"] = json.load(f)
        _PRESET_CACHE["mtime"] = mtime
    return _PRESET_CACHE["index"] or {"sections": {}}


def tag_slot_index():
    """{tag_lower: slot} —— 只索引粘性槽，避免误伤自由词序。"""
    data = _load_presets()
    idx = {}
    for section, slot in (("clothing", "clothing"), ("scene", "scene"),
                          ("pose", "pose"), ("expression", "expression")):
        sec = (data.get("sections") or {}).get(section)
        if not sec:
            continue
        for group in sec.get("groups", []):
            for item in group.get("items", []):
                for tag in item.get("tags", []):
                    idx.setdefault(tag.strip().lower(), slot)
    return idx


def classify_token(token, index=None):
    """给一个逗号分隔的 token 判定槽位。判定不出来 → free。

    两级匹配：
      ① 精确命中索引（词典来自服装/场景/姿势库）
      ② 退一步做整词包含匹配 —— 让 "black highleg leotard" 这类
         词典里没有的复合写法也能靠 "highleg leotard" 认出服装归属。
    """
    t = token.strip()
    if not t:
        return "free"
    low = t.lower()
    if low.startswith("@"):
        return "artist"
    if COUNT_RE.match(low):
        return "count"
    for pat in APPEARANCE_PATTERNS:
        if pat.match(low):
            return "appearance"
    idx = tag_slot_index() if index is None else index
    hit = idx.get(low)
    if hit:
        return hit
    # ② 整词包含：取最长的命中标签，避免 "tail" 之类短词乱抓
    best_len, best_slot = 0, None
    for kw, slot in _loose_index(idx):
        if len(kw) <= best_len:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low):
            best_len, best_slot = len(kw), slot
    return best_slot or "free"


_LOOSE_CACHE = {"key": None, "items": None}


def _loose_index(idx):
    """按长度倒序的 (tag, slot) 列表，用于整词包含匹配。"""
    key = len(idx)
    if _LOOSE_CACHE["key"] != key:
        items = sorted(((k, v) for k, v in idx.items() if len(k) >= 4 and " " in k),
                       key=lambda kv: -len(kv[0]))
        _LOOSE_CACHE["items"] = items
        _LOOSE_CACHE["key"] = key
    return _LOOSE_CACHE["items"]


# ============================================================================
# 三、声明（claim）
# ============================================================================

CLAIM_VERSION = 1


def merge_claims(claims):
    """把多个声明合成一个（不做仲裁），seq 原样保留。"""
    entries = []
    for c in claims:
        if not c:
            continue
        entries.extend(c.get("entries", []))
    return {"v": CLAIM_VERSION, "source": "merged", "entries": entries}


def bump_claims(claim):
    """返回一个晚于该声明所有条目的 seq。"""
    if not claim:
        return 0
    seqs = [e.get("seq", 0) for e in claim.get("entries", [])]
    return (max(seqs) + 1) if seqs else 0


# ============================================================================
# 四、仲裁
# ============================================================================

def arbitrate(entries):
    """按槽位契约仲裁。

    规则（先满足者先胜）：
      R1 提权（force）优先
      R2 层级（locked > explicit > derived > default）
      R3 同层：
           sticky 槽 → seq 最小者胜（首写锁定，后到者不得覆盖）
           replaceable 槽 → seq 最大者胜（随主体重算）
      R4 落选者不删除，登记为 shadowed
    """
    by_slot = {}
    for e in entries:
        by_slot.setdefault(e["slot"], []).append(e)

    decided, shadowed = {}, {}
    for slot, items in by_slot.items():
        policy = SLOT_META.get(slot, {}).get("policy", "replaceable")
        forced = [e for e in items if e.get("force")]
        pool = forced or items
        later_wins = bool(forced) or policy == "replaceable"

        top = max(TIER_RANK.get(e.get("tier", "explicit"), 0) for e in pool)
        pool = [e for e in pool if TIER_RANK.get(e.get("tier", "explicit"), 0) == top]
        winner = (max if later_wins else min)(pool, key=lambda e: e.get("seq", 0))

        decided[slot] = winner
        losers = [e for e in items if e is not winner]
        if losers:
            shadowed[slot] = losers
    return decided, shadowed


def render(decided, order_mode="preserve", first_positions=None):
    """渲染最终文本。

    层级模型（三层权威）：正文层(default) < 插件层(explicit) < 锁定层(locked)

    order_mode:
      preserve（默认）—— 不打乱原有词序，只删除被遮挡的 token；
                        新增标签插在该槽最后一个原词之后。
      normalize       —— 按规范槽位顺序重排。
    """
    first_positions = first_positions or {}
    parts, seen = [], set()

    def take(tags):
        out = []
        for t in tags:
            k = t.strip().lower()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(t.strip())
        return out

    if order_mode == "preserve" and first_positions:
        idx = tag_slot_index()
        orig = [t for t, _ in sorted(first_positions.items(), key=lambda kv: kv[1])]
        win_set = {s: {x.strip().lower() for x in e["tags"]} for s, e in decided.items()}

        kept, last_at = [], {}
        for t in orig:
            slot = classify_token(t, idx)
            entry = decided.get(slot)
            if not entry or t.strip().lower() not in win_set.get(slot, ()):
                continue                      # 该槽被遮挡 → 丢弃
            kept.append(t.strip())
            last_at[slot] = len(kept) - 1

        # 把各槽"新增的"（原句里没有的）标签插到该槽最后出现位置之后；
        # 原句里根本没出现过的槽，其标签统一追加到末尾
        inserts, orphan = {}, []
        for slot, entry in decided.items():
            fresh = [t for t in entry["tags"] if t.strip().lower() not in first_positions]
            if not fresh:
                continue
            if slot in last_at:
                inserts.setdefault(last_at[slot], []).extend(fresh)
            else:
                orphan.extend(fresh)

        out = []
        for i, t in enumerate(kept):
            out.extend(take([t]))
            if i in inserts:
                out.extend(take(inserts[i]))
        out.extend(take(orphan))
        return ", ".join(out)

    for slot in SLOT_ORDER:
        entry = decided.get(slot)
        if entry:
            parts.extend(take(entry["tags"]))
    return ", ".join(parts)


# ============================================================================
# 五、内部工具
# ============================================================================

def _tokenize(text):
    """返回 (tokens, {token_lower: 首次出现序号})。"""
    raw = (text or "").strip()
    if not raw:
        return [], {}
    toks = [t.strip() for t in raw.split(",") if t.strip()]
    pos = {}
    for i, t in enumerate(toks):
        pos.setdefault(t.lower(), i)
    return toks, pos


def _entries_from_tokens(tokens, first_pos, seq, source, tier="explicit"):
    idx = tag_slot_index()
    bucket = {}
    for t in tokens:
        bucket.setdefault(classify_token(t, idx), []).append(t)
    return [{"slot": slot, "tags": tags, "tier": tier, "force": False,
             "seq": seq, "source": source}
            for slot, tags in bucket.items()]


def _normalize_entries(entries, floor=0):
    out = []
    for e in entries:
        out.append({
            "slot": e.get("slot", "free"),
            "tags": list(e.get("tags", [])),
            "tier": e.get("tier", "explicit"),
            "force": bool(e.get("force", False)),
            "seq": float(e.get("seq", 0)) + floor,
            "source": e.get("source", "上游声明"),
        })
    return out


def _collect_upstream(prompt, claim_in):
    """把上游输入变成 entries。

    结构化声明 → 插件层（seq 原样，天然"先写"）；
    纯文本     → 正文层 default（可被本次抽取覆盖）。
    返回 (entries, base_seq, first_pos)。
    """
    tokens, first_pos = _tokenize(prompt)
    entries = []
    if claim_in:
        entries.extend(_normalize_entries(claim_in.get("entries", []), floor=0))
        base_seq = bump_claims(claim_in)
    else:
        base_seq = 0
    if tokens and not claim_in:
        entries.extend(_entries_from_tokens(tokens, first_pos, seq=0,
                                            source="上游正文", tier="default"))
        base_seq = max(base_seq, 1)
    return entries, base_seq, first_pos


# ============================================================================
# 六、抽卡
# ============================================================================

# CLI 友好的关系别名 → 内部取值
RELATION_MAP = {
    "auto": "自动（按等级）", "自动": "自动（按等级）",
    "normal": "正常互动", "sfw": "正常互动", "正常互动": "正常互动",
    "yuri": "百合向", "百合": "百合向", "百合向": "百合向",
    "none": "不加", "off": "不加", "不加": "不加",
}

ORDER_CHOICES = ("preserve", "normalize")


def _resolve_seed(seed):
    """seed < 0 / None → 随机出一个真种子，让日志能复现。"""
    if seed is None or int(seed) < 0:
        return random.randrange(1 << 30)
    return int(seed)


def _read_prompt(prompt):
    """--prompt 支持 '-'(stdin) 与 '@文件'。"""
    if not prompt:
        return ""
    p = prompt.strip()
    if p == "-":
        return sys.stdin.read().strip()
    if p.startswith("@"):
        with open(p[1:], encoding="utf-8") as f:
            return f.read().strip()
    return prompt


def random_girl(pool="热门 TOP200", min_post=0, character_count=1,
                include_features=True, huge_breasts=False, avoid_repeat=True,
                level="suggestive", relation="auto", seed=-1,
                order_mode="preserve", prompt="", claim_in=None):
    """随机抽女性特征。返回 {ok, text, report, claim, seed}。

    · 按 sqrt(同人作品数) 加权 —— 热门角色更容易抽到，但不至于翻来覆去只有那几个
    · avoid_repeat 默认开：最近 40 次抽过的会避开（候选不足时才允许重复）
    · 只输出 f 里的特征标签，**不输出角色身份**：抽到的是"一个女性长什么样"，
      不指向具体角色；t（角色名 + 作品）只写进报告，不写进提示词
    · 输出按槽位拆分：性别词 → count 槽；特征 → appearance 槽
      （刻意不占服装槽 —— 角色自带的装扮不该锁死挑衣服的能力）
    """
    base = _resolve_seed(seed)
    prompt = _read_prompt(prompt)
    # CLI 别名（auto/normal/yuri/none）→ 内部取值
    _rel = str(relation).strip()
    relation = RELATION_MAP.get(_rel.lower(),
                                RELATION_MAP.get(_rel, "自动（按等级）"))
    got = wardrobe_rules.roll_girl(
        pool=pool, min_post=int(min_post),
        count=int(character_count), include_features=bool(include_features),
        seed=base, avoid_repeat=bool(avoid_repeat),
        huge_breasts=bool(huge_breasts))

    if not got.get("ok"):
        return {"ok": False, "text": prompt, "claim": claim_in,
                "report": got.get("error", "抽取失败"), "seed": base}

    # 多条特征合并成一条 entry（同层同序号在仲裁下只会活一个）
    n = len(got["picks"])
    count_tag = ("1girl" if n == 1 else
                 f"{n}girls" if n <= 4 else "multiple girls")
    merged, seen_ch = [], set()
    for p in got["picks"]:
        for t in p["features"]:
            if t.lower() not in seen_ch:
                seen_ch.add(t.lower())
                merged.append(t)

    lines = [f"🎲 随机女性特征 · 池={pool}"
             + f"  候选={got['candidates']}  seed={base}"]
    if huge_breasts:
        lines.append("  ⭐ 巨乳开关：已开启，体型强制为 huge breasts")
    for p in got["picks"]:
        lines.append(f"   · 来源 {p['trigger']}"
                     f"  热度 {p['post_count']}  特征 {len(p['features'])} 个")
    lines.append("  特征 : " + (", ".join(merged) if merged else "无"))
    if got.get("nsfw"):
        lines.append(f"  NSFW 等级 : {got['nsfw']['num']} / "
                     f"{got['nsfw'].get('max', wardrobe_rules.NSFW_MAX)}"
                     f" · {got['nsfw']['label']}（角色资料本身中性，不含裸体标签）")

    # 双女性（抽 2 个及以上）→ 按等级加关系标签
    rel = []
    if len(got["picks"]) >= 2 and relation != "不加":
        mode = wardrobe_rules.resolve_level(level, "suggestive")
        num = wardrobe_rules.NUM_OF.get(mode, 1)
        if relation == "正常互动":
            rel = wardrobe_rules.duo_relation_tags(0)
        elif relation == "百合向":
            # 「百合向」= 至少百合、但不主动升到第 5 级
            rel = wardrobe_rules.duo_relation_tags(min(max(num, 2), 3))
        else:
            rel = wardrobe_rules.duo_relation_for_level(mode)
        lines.append(f"  双女性识别 : 是（{len(got['picks'])} 位）→ 关系标签 {rel}")
    elif len(got["picks"]) >= 2:
        lines.append("  双女性识别 : 是，但关系标签已关闭")

    entries, base_seq, first_pos = _collect_upstream(prompt, claim_in)

    entries.append({"slot": "count", "tags": [count_tag], "tier": "explicit",
                    "force": False, "seq": base_seq,
                    "source": f"随机特征 x{n}"})
    if merged:
        entries.append({"slot": "appearance", "tags": merged, "tier": "explicit",
                        "force": False, "seq": base_seq,
                        "source": f"随机特征 x{n}"})
    if rel:
        entries.append({"slot": "relation", "tags": rel, "tier": "explicit",
                        "force": False, "seq": base_seq,
                        "source": "双女关系"})

    decided, _ = arbitrate(entries)
    text = render(decided, order_mode, first_pos)
    claim = merge_claims([{"v": CLAIM_VERSION, "entries": entries}])
    report = "\n".join(lines)
    return {"ok": True, "text": text, "report": report, "claim": claim, "seed": base}


def wardrobe_roll(enable_outfit=True, enable_pose=True, enable_expression=True,
                  enable_scene=True, level="suggestive", allow_public=False,
                  seed=-1, order_mode="preserve", prompt="", claim_in=None):
    """一键随机搭配（服装 / 姿势 / 表情 / 场景）。返回 {ok, text, report, claim, seed}。

    搭配由 wardrobe_rules 校验（服装合理性 / 场景合理性 / 裸露等级），
    结果按「插件层」写入：压过正文里的散落服装词，但不覆盖上游声明已写的粘性槽。
    """
    base = _resolve_seed(seed)
    prompt = _read_prompt(prompt)
    level = wardrobe_rules.resolve_level(level, "suggestive")
    result = wardrobe_rules.roll(section="all", level=level, seed=base,
                                 allow_public=allow_public,
                                 duo_context=prompt or "")
    outfit = result["outfit"]
    scene = result["scene"]
    pose = result["pose"]
    expression = result["expression"]
    relation = result.get("relation", [])

    lines = [
        f"🎲 seed={base}  目标等级={wardrobe_rules.level_label(level)}"
        f"  实得={result['nsfw']['num']} · {result['nsfw']['label']}",
        f"   服装形态 : {outfit['form']}（{len(outfit['tags'])} 个标签）",
        f"   姿势     : {pose['group']}"
        + (f"／{pose['label']}" if pose.get('label') else "")
        + f"（{len(pose['tags'])} 个标签）",
        f"   表情     : {expression['emotion']}"
        + (f" · 强度 {expression['intensity']}" if expression.get('emotion') else "")
        + f"（{len(expression['tags'])} 个标签）",
        f"   场所     : {scene['place']}／{wardrobe_rules.PRIVACY_LABEL[scene['privacy']]}"
        f"{' · 室内' if scene['indoor'] else ' · 室外'}",
        f"   裸露判据 : {outfit['why'].get('reason', '')}",
    ]
    if result.get("duo", {}).get("duo"):
        lines.append(f"   双女性识别 : 是（{result['duo']['count']} 位）"
                     f" → 关系标签 {relation}")
    else:
        lines.append("   双女性识别 : 否（未加关系标签）")
    lines.append("   ⚠ 违规 : " + str(result["violations"])
                 if result["violations"] else
                 "   ✓ 服装搭配 / 场景 / 裸露等级 三项校验全部通过")

    entries, base_seq, first_pos = _collect_upstream(prompt, claim_in)

    if enable_outfit:
        entries.append({"slot": "clothing", "tags": outfit["tags"],
                        "tier": "explicit", "force": False, "seq": base_seq,
                        "source": f"随机搭配(seed={base})"})
    if enable_pose:
        entries.append({"slot": "pose", "tags": pose["tags"],
                        "tier": "explicit", "force": False, "seq": base_seq + 2,
                        "source": f"随机姿势(seed={base})"})
    if enable_expression:
        entries.append({"slot": "expression", "tags": expression["tags"],
                        "tier": "explicit", "force": False, "seq": base_seq + 3,
                        "source": f"随机表情(seed={base})"})
    if enable_scene:
        entries.append({"slot": "scene", "tags": scene["tags"],
                        "tier": "explicit", "force": False, "seq": base_seq + 1,
                        "source": f"随机场景(seed={base})"})
    if relation:
        entries.append({"slot": "relation", "tags": relation,
                        "tier": "explicit", "force": False, "seq": base_seq,
                        "source": f"双女关系(seed={base})"})

    decided, _ = arbitrate(entries)
    text = render(decided, order_mode, first_pos)
    claim = merge_claims([{"v": CLAIM_VERSION, "entries": entries}])
    report = "\n".join(lines)
    return {"ok": bool(text), "text": text, "report": report, "claim": claim,
            "seed": base}


# ============================================================================
# 七、命令行
# ============================================================================

def _setup_stdio():
    """管道/重定向到 GBK 控制台时不因中文崩掉。"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except Exception:
            pass


def _add_common(p):
    p.add_argument("--level", default="suggestive",
                   help="等级：sfw/suggestive/explicit/hardcore/extreme/any"
                        "，也接受 0-4 或中文（默认 suggestive）")
    p.add_argument("--seed", type=int, default=-1,
                   help="随机种子，-1 = 每次随机（默认）")
    p.add_argument("--order", dest="order_mode", default="preserve",
                   choices=ORDER_CHOICES,
                   help="preserve 保留输入词序 / normalize 按槽位重排 (默认 preserve)")
    p.add_argument("--prompt", default="",
                   help="上游已有提示词（逗号分隔）；'-' 读 stdin，'@文件' 读文件")
    p.add_argument("-r", "--repeat", type=int, default=1,
                   help="重复抽卡次数（seed 为 -1 时每次不同，默认 1）")
    p.add_argument("--json", action="store_true", help="输出 JSON（便于脚本消费）")
    p.add_argument("--text-only", action="store_true", help="只输出提示词正文")
    p.add_argument("--claim-out", default="", metavar="FILE",
                   help="把声明(claim) JSON 写到文件，供下游 --claim-in 复用")
    p.add_argument("--claim-in", default="", metavar="FILE",
                   help="读入上游声明的 JSON 文件")


def _load_claim(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _emit(results, args, kind):
    if args.claim_out and results:
        with open(args.claim_out, "w", encoding="utf-8") as f:
            json.dump(results[-1]["claim"], f, ensure_ascii=False, indent=2)

    if args.json:
        payload = results[0] if len(results) == 1 else results
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return

    for i, r in enumerate(results):
        if not r["ok"]:                     # 失败时报告走 stderr，不因 --text-only 吞掉
            sys.stderr.write(r["report"].rstrip() + "\n")
            continue
        if len(results) > 1:
            sys.stdout.write(f"\n{'=' * 60}\n[{kind}] 第 {i + 1}/{len(results)} 次"
                             f"  seed={r['seed']}\n{'=' * 60}\n")
        if args.text_only:
            sys.stdout.write(r["text"] + "\n")
            continue
        sys.stdout.write(r["report"].rstrip() + "\n")
        sys.stdout.write("-" * 60 + "\n")
        sys.stdout.write(r["text"] + "\n")


def cmd_girl(args):
    results = []
    for _ in range(max(1, args.repeat)):
        r = random_girl(
            pool=args.pool, min_post=args.min_post,
            character_count=args.count, include_features=not args.no_features,
            huge_breasts=args.huge_breasts,
            avoid_repeat=not args.no_avoid_repeat,
            level=args.level, relation=args.relation, seed=args.seed,
            order_mode=args.order_mode, prompt=args.prompt,
            claim_in=_load_claim(args.claim_in))
        results.append(r)
        if not r["ok"]:
            _emit(results, args, "girl")
            return 1
    _emit(results, args, "girl")
    return 0


def cmd_roll(args):
    results = []
    for _ in range(max(1, args.repeat)):
        r = wardrobe_roll(
            enable_outfit=not args.no_outfit,
            enable_pose=not args.no_pose,
            enable_expression=not args.no_expression,
            enable_scene=not args.no_scene,
            level=args.level, allow_public=args.allow_public,
            seed=args.seed, order_mode=args.order_mode, prompt=args.prompt,
            claim_in=_load_claim(args.claim_in))
        results.append(r)
        if not r["ok"]:
            _emit(results, args, "roll")
            return 1
    _emit(results, args, "roll")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="anima_cli",
        description="Anima 抽卡命令行（无需 ComfyUI）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python anima_cli.py girl --count 2 --level explicit\n"
               "  python anima_cli.py roll --no-scene -r 3\n"
               "  python anima_cli.py roll --prompt - --text-only < in.txt\n")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("girl", help="随机女性特征（不含角色身份）")
    g.add_argument("--pool", default="热门 TOP200",
                   choices=list(wardrobe_rules.CHAR_POOLS),
                   help="候选池（默认 热门 TOP200）")
    g.add_argument("--min-post", type=int, default=0, help="最低同人作品数")
    g.add_argument("--count", type=int, default=1, choices=(1, 2, 3),
                   help="抽几条特征（默认 1）")
    g.add_argument("--no-features", action="store_true", help="不带官方特征标签")
    g.add_argument("--huge-breasts", action="store_true", help="强制巨乳体型")
    g.add_argument("--no-avoid-repeat", action="store_true",
                   help="关闭「避开最近 40 次抽过的角色」")
    g.add_argument("--relation", default="auto",
                   choices=("auto", "normal", "yuri", "none"),
                   help="双女关系：auto 按等级 / normal 正常互动 / yuri 百合 / none 不加")
    _add_common(g)
    g.set_defaults(func=cmd_girl)

    r = sub.add_parser("roll", help="一键随机搭配")
    r.add_argument("--no-outfit", action="store_true", help="不抽服装")
    r.add_argument("--no-pose", action="store_true", help="不抽姿势")
    r.add_argument("--no-expression", action="store_true", help="不抽表情")
    r.add_argument("--no-scene", action="store_true", help="不抽场景")
    r.add_argument("--allow-public", action="store_true",
                   help="允许暴露向内容出现在公共场所")
    _add_common(r)
    r.set_defaults(func=cmd_roll)
    return ap


def main(argv=None):
    _setup_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
