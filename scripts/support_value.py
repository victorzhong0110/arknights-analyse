#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
辅助价值评分架构（S1-S4）。

S1 效果原语：从技能/天赋/模组提取可量化原子效果（脆弱/减防/减抗/友方增益/减速/控制/治疗/护盾/庇护/嘲讽/复活/免疫）
S2 转化模型：每种原语 → 边际贡献（伤害增益 ΔDPS / 输出时间增益 ΔOutTime / 生存保障 ΔSurv）
S3 分场景：合约Boss战 / 日常挂机 / 高压群怪（敌人属性+射程-移速模型+队伍参数）
S4 合成：V = w_dmg·ΔDPS + w_time·ΔOutTime + w_surv·ΔSurv，分场景输出辅助榜

用法：python3 scripts/support_value.py
"""
import csv
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluate_operators as E  # noqa: E402
import effect_analysis as F  # noqa: E402

DB = E.DB
OUT = E.OUT

# 场景参数：敌人属性 + 射程-移速模型 + 队伍参数
SCENARIOS = {
    '合约Boss战': dict(def_=1200, res=60, eres=0, dres=10, hp=100000, atk=2000,
                    enemy_ms=0.5, count=1, range_len=4.0, team_dps=3000,
                    team_hp=5000, phys_frac=0.55, w_dmg=0.35, w_time=0.25, w_surv=0.40),
    '日常挂机': dict(def_=300, res=20, eres=0, dres=0, hp=20000, atk=800,
                  enemy_ms=1.0, count=8, range_len=3.0, team_dps=2500,
                  team_hp=4000, phys_frac=0.55, w_dmg=0.50, w_time=0.30, w_surv=0.20),
    '高压群怪': dict(def_=800, res=40, eres=0, dres=0, hp=50000, atk=1200,
                  enemy_ms=1.2, count=8, range_len=2.5, team_dps=3000,
                  team_hp=4500, phys_frac=0.60, w_dmg=0.40, w_time=0.30, w_surv=0.30),
}

BENCH_PHYS_DPH = 1500.0   # 队伍平均物理单发（用于减防收益）
BENCH_ARTS_DPH = 1500.0   # 队伍平均法术单发
BENCH_ATK = 1500.0        # 队伍平均攻击力（用于鼓舞/攻击buff折算）


def extract_survival(cur, char_id, stats):
    """S1: 生存原语（治疗HPS/护盾/庇护/嘲讽/复活/免疫）。"""
    fx = {'heal_hps': 0.0, 'shield': 0.0, 'protect': 0.0, 'taunt': 0,
          'revive': 0, 'immune': 0}
    notes = []
    atk = stats['atk']
    rows = cur.execute(
        "SELECT name, description, blackboard, duration, sp_data FROM skill_levels "
        "WHERE skill_id IN (SELECT skill_id FROM operator_skills WHERE char_id=?) "
        "AND level_index=9", (char_id,)).fetchall()
    for name_, desc, bb_raw, dur_field, sp_data in rows:
        bb = __import__('analyze_skills').bb_dict(bb_raw) if bb_raw else {}
        desc = F.clean(desc or '', bb)
        # 治疗 HPS：heal_scale / 治疗量
        hs = bb.get('heal_scale')
        if hs and '治疗' in desc:
            heal_per = atk * hs
            fx['heal_hps'] = max(fx['heal_hps'], heal_per)
            notes.append(f'治疗{heal_per:.0f}/次')
        # 护盾/屏障
        if '屏障' in desc or '护盾' in desc:
            m = re.search(r'(\d+(?:\.\d+)?)%[^。]{0,6}最大生命', desc) or \
                re.search(r'最大生命值[^。]{0,6}?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['shield'] = max(fx['shield'], float(m.group(1)) / 100.0 * stats['max_hp'])
                notes.append(f'护盾{float(m.group(1)):.0f}%最大生命')
        # 庇护/减伤
        if '庇护' in desc:
            m = re.search(r'庇护[^。]{0,6}?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['protect'] = max(fx['protect'], float(m.group(1)) / 100.0)
                notes.append(f'庇护{float(m.group(1)):.0f}%')
        if '伤害降低' in desc or '受到伤害降低' in desc:
            m = re.search(r'伤害降低[^。]{0,6}?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['protect'] = max(fx['protect'], float(m.group(1)) / 100.0)
                notes.append(f'减伤{float(m.group(1)):.0f}%')
        # 嘲讽
        if '嘲讽' in desc:
            fx['taunt'] = 1
            notes.append('嘲讽')
        # 复活
        if '复活' in desc:
            fx['revive'] = 1
            notes.append('复活')
    # 免疫特质
    row = cur.execute(
        "SELECT stun_immune, silence_immune, sleep_immune, frozen_immune, levitate_immune, "
        "disarmed_combat_immune, feared_immune, palsy_immune FROM operator_levels "
        "WHERE char_id=? AND phase=2 ORDER BY level DESC LIMIT 1", (char_id,)).fetchone()
    if row:
        fx['immune'] = sum(1 for v in row if v)
    return fx, '; '.join(notes)


def convert(fx, sv, s):
    """S2: 原语 → 边际贡献。统一口径：
    - 伤害贡献/时间贡献：'伤害点'（整场击杀窗口内的额外伤害）
    - 生存贡献：'承伤点'（独立维度）
    时间类按'击杀时间缺口'封顶（敌人本来就会在射程内待 pass_time 秒，只有缺口部分有价值）。
    """
    team_dps, D, R = s['team_dps'], s['def_'], s['res']
    engage = s['hp'] / team_dps                       # 击杀所需时间
    pass_time = s['range_len'] / max(s['enemy_ms'], 0.1)  # 敌人在射程内自然停留
    deficit = max(engage - pass_time, 0)              # 输出时间缺口

    dmg = 0.0
    # 增伤类：每百分比×整场窗口
    dmg += team_dps * fx['fragile'] * engage
    shred = fx['def_shred_flat'] + D * fx['def_shred_pct']
    if shred > 0:
        eb = max(BENCH_PHYS_DPH - D, BENCH_PHYS_DPH * 0.05)
        ea = max(BENCH_PHYS_DPH - (D - shred), BENCH_PHYS_DPH * 0.05)
        dmg += team_dps * s['phys_frac'] * max(ea / eb - 1, 0) * engage
    if fx['res_shred']:
        eb = BENCH_ARTS_DPH * max(1 - R / 100, 0)
        ea = BENCH_ARTS_DPH * max(1 - (R - fx['res_shred']) / 100, 0)
        if eb > 0:
            dmg += team_dps * (1 - s['phys_frac']) * max(ea / eb - 1, 0) * engage
    dmg += team_dps * fx['ally_atk_pct'] * engage
    if fx['inspire_pct']:
        dmg += team_dps * (fx['inspire_pct'] * fx['inspire_atk'] / BENCH_ATK) * engage
    if fx['ally_as']:
        dmg += team_dps * fx['ally_as'] / (100 + fx['ally_as']) * 0.8 * engage

    # 时间类：额外输出窗口 × 队伍DPS，封顶在缺口内
    time_g = 0.0
    if fx['slow']:
        new_pass = s['range_len'] / (s['enemy_ms'] * (1 - fx['slow']))
        time_g += team_dps * min(deficit, new_pass - pass_time)
    if fx['control_dur']:
        time_g += team_dps * min(deficit, fx['control_dur']) * min(s['count'], 8) / 8

    # 生存类：承伤点
    surv = 0.0
    if fx['enemy_atk']:
        surv += s['atk'] * fx['enemy_atk'] * engage * 0.5
    surv += sv['heal_hps'] * engage
    surv += sv['shield']
    if sv['protect']:
        surv += s['team_hp'] * sv['protect'] / (1 - sv['protect'])
    surv += sv['taunt'] * s['team_hp'] * 0.3
    surv += sv['revive'] * s['team_hp'] * 0.5
    surv += sv['immune'] * 200
    return dmg, time_g, surv


def minmax(vals):
    lo, hi = min(vals), max(vals)
    return [(v - lo) / (hi - lo) if hi > lo else 0.0 for v in vals]


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    chars = cur.execute(
        "SELECT char_id, name, profession, sub_profession_id, rarity FROM operators "
        "WHERE source='character' AND profession NOT IN ('TOKEN','TRAP') "
        "ORDER BY rarity DESC").fetchall()

    rows = []
    for ch in chars:
        fs = E.final_stats(cur, ch['char_id'])
        if fs is None:
            continue
        stats, mod, _ = fs
        stats['_char_id'] = ch['char_id']
        fx, notes = F.extract_support(cur, ch['char_id'], ch['name'], ch['profession'],
                                      stats['atk'], stats)
        sv, sn = extract_survival(cur, ch['char_id'], stats)
        if not any(fx.values()) and not any(sv.values()):
            continue
        row = {'char': ch['name'], 'profession': ch['profession'],
               'sub_profession': ch['sub_profession_id'], 'rarity': ch['rarity'],
               'effects': (notes + ' | ' + sn).strip(' |')}
        for sc_name, s in SCENARIOS.items():
            dmg, time_g, surv = convert(fx, sv, s)
            row[f'{sc_name}_dmg'] = round(dmg, 1)
            row[f'{sc_name}_time'] = round(time_g, 1)
            row[f'{sc_name}_surv'] = round(surv, 1)
        rows.append(row)

    # 归一化各维度，再按场景权重合成
    for sc_name, s in SCENARIOS.items():
        d = minmax([r[f'{sc_name}_dmg'] for r in rows])
        t = minmax([r[f'{sc_name}_time'] for r in rows])
        v = minmax([r[f'{sc_name}_surv'] for r in rows])
        for i, r in enumerate(rows):
            r[f'{sc_name}_score'] = round(
                s['w_dmg'] * d[i] + s['w_time'] * t[i] + s['w_surv'] * v[i], 4)

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'support_value.csv')
    cols = list(rows[0].keys())
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f'辅助干员数: {len(rows)} → {path}')

    for sc_name in SCENARIOS:
        pool = [r for r in rows if r['rarity'] in (4, 5)]
        pool.sort(key=lambda x: -x[f'{sc_name}_score'])
        print(f'\n== {sc_name} 辅助榜 TOP10 ==')
        for r in pool[:10]:
            print(f"  {r['char']:<6s} {r['profession']:<7s} "
                  f"总分{r[f'{sc_name}_score']:>5.3f} "
                  f"(伤害+{r[f'{sc_name}_dmg']:>8,.0f} 时间+{r[f'{sc_name}_time']:>8,.0f} "
                  f"生存+{r[f'{sc_name}_surv']:>8,.0f})  {r['effects'][:26]}")
    conn.close()


if __name__ == '__main__':
    main()
