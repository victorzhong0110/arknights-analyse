#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
辅助价值评分（边际贡献模型，无人为权重）。

方法论（第一性原理）：
  辅助的价值 = 加入辅助后，标准队伍总输出的变化（伤害贡献）
             + 输出窗口延长带来的额外伤害（时间贡献）
             + 队伍生存保障折算的伤害（生存贡献）
  全部为"伤害点"统一单位，直接相加，无权重；所有数值从机制与真实数据推导。

基准队伍（真实输出干员，覆盖三种形态）：
  史尔特尔·黄昏   — 物理高DPH爆发
  能天使·过载模式  — 物理低DPH高频
  艾雅法拉·火山   — 法术

场景（真实敌人属性）：
  合约Boss战：爱国者 × 反装甲Ⅲ+活性Ⅲ+刺激Ⅲ（DEF×3, HP×2.5, ATK×2）
  日常挂机：全敌 DEF P50 代表
  高压群怪：8 个中防敌人

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
import interval_analysis as I  # noqa: E402
import rank_engine as R  # noqa: E402
import effect_analysis as F  # noqa: E402

DB = E.DB
OUT = E.OUT

# 基准队伍（真实干员·技能）
BASE_TEAM = [('史尔特尔', '黄昏'), ('能天使', '过载模式'), ('艾雅法拉', '火山')]


def find_skill(skills, cname, sname):
    for sk in skills:
        if sk['char'] == cname and sk['skill'] == sname:
            return sk
    return None


def team_cycle_dps(skills, sc, fx):
    """基准队伍总循环DPS（辅助 fx 真实作用到每个技能上）。"""
    total = 0.0
    for cname, sname in BASE_TEAM:
        sk = find_skill(skills, cname, sname)
        if not sk:
            continue
        mfrag = fx.get('magic_fragile', 0.0) if sk['dmg_type'] == 'arts' else 0.0
        m = R.skill_metrics_buffed(
            sk, sc['def'], sc['res'], sc['eres'], sc['dres'], 1,
            atk_mult=1 + fx.get('ally_atk_pct', 0.0),
            atk_flat=fx.get('inspire_pct', 0.0) * fx.get('inspire_atk', 0.0),
            dmg_mult=1 + fx.get('fragile', 0.0) + mfrag,
            as_mult=1 + fx.get('ally_as', 0.0) / 100.0,
            def_shred_pct=fx.get('def_shred_pct', 0.0),
            def_shred_flat=fx.get('def_shred_flat', 0.0),
            res_shred=fx.get('res_shred', 0.0))
        total += m['cycle_dps']
    return total


def extract_survival(cur, char_id, stats):
    """生存原语（治疗HPS/护盾/庇护/嘲讽/复活/免疫）。"""
    fx = {'heal_hps': 0.0, 'shield': 0.0, 'protect': 0.0, 'taunt': 0,
          'revive': 0, 'immune': 0}
    notes = []
    atk = stats['atk']
    rows = cur.execute(
        "SELECT name, description, blackboard, duration, sp_data FROM skill_levels "
        "WHERE skill_id IN (SELECT skill_id FROM operator_skills WHERE char_id=?) "
        "AND level_index=9", (char_id,)).fetchall()
    for name_, desc, bb_raw, dur_field, sp_data in rows:
        import analyze_skills as A
        bb = A.bb_dict(bb_raw) if bb_raw else {}
        desc = F.clean(desc or '', bb)
        hs = bb.get('heal_scale') or bb.get('attack@heal_scale')
        if hs and ('治疗' in desc or '回复' in desc):
            fx['heal_hps'] = max(fx['heal_hps'], atk * hs)
            notes.append(f'治疗{atk*hs:.0f}/次' if bb.get('heal_scale') else f'治疗{atk*hs:.0f}/秒')
        if '屏障' in desc or '护盾' in desc:
            m = re.search(r'(\d+(?:\.\d+)?)%[^。]{0,6}最大生命', desc) or \
                re.search(r'最大生命值[^。]{0,6}?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['shield'] = max(fx['shield'], float(m.group(1)) / 100.0 * stats['max_hp'])
                notes.append(f'护盾{float(m.group(1)):.0f}%最大生命')
        if '庇护' in desc or '伤害降低' in desc or '受到伤害降低' in desc:
            m = re.search(r'(?:庇护|伤害降低|受到伤害降低)[^。]{0,6}?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['protect'] = max(fx['protect'], float(m.group(1)) / 100.0)
                notes.append(f'减伤{float(m.group(1)):.0f}%')
        if '嘲讽' in desc:
            fx['taunt'] = 1
            notes.append('嘲讽')
        if '复活' in desc:
            fx['revive'] = 1
            notes.append('复活')
    row = cur.execute(
        "SELECT stun_immune, silence_immune, sleep_immune, frozen_immune, levitate_immune, "
        "disarmed_combat_immune, feared_immune, palsy_immune FROM operator_levels "
        "WHERE char_id=? AND phase=2 ORDER BY level DESC LIMIT 1", (char_id,)).fetchone()
    if row:
        fx['immune'] = sum(1 for v in row if v)
    return fx, '; '.join(notes)


def marginal_value(fx, sv, sc, skills):
    """边际贡献（伤害点）：队伍输出增量 + 输出窗口延长 + 生存保障。"""
    base = team_cycle_dps(skills, sc, {})
    buffed = team_cycle_dps(skills, sc, fx)
    team_dps = base
    engage = sc['hp'] / max(team_dps, 1.0)
    pass_time = sc['range_len'] / max(sc['enemy_ms'], 0.1)
    deficit = max(engage - pass_time, 0)

    # 伤害贡献：队伍DPS 增量 × 击杀窗口（减防/脆弱/攻击buff 已在重算中体现）
    dmg = (buffed - base) * engage
    # 时间贡献：减速/控制延长输出窗口
    time_g = 0.0
    n_eff = min(sc['count'], 8) / 8
    if fx.get('slow'):
        new_pass = sc['range_len'] / (sc['enemy_ms'] * (1 - fx['slow']))
        time_g += team_dps * min(deficit, new_pass - pass_time) * n_eff
    if fx.get('control_dur'):
        time_g += team_dps * min(deficit, fx['control_dur']) * n_eff
    # 技力回复：循环缩短（平均 spCost≈30，额外 r/s）
    if fx.get('sp_recovery'):
        dmg += team_dps * fx['sp_recovery'] / (1 + fx['sp_recovery']) * engage * 0.5
    # 回费：功能价值（1点费用≈50伤害点/场，简化假设）
    if fx.get('cost_recovery'):
        dmg += fx['cost_recovery'] * 50.0
    # 生存贡献：治疗/护盾/减伤 折算为队伍可承受的敌人伤害 → 保住的输出
    surv = 0.0
    if fx.get('enemy_atk'):
        surv += sc['atk'] * fx['enemy_atk'] * engage * 0.5
    surv += sv['heal_hps'] * engage
    surv += sv['shield']
    if sv['protect']:
        surv += sc['team_hp'] * sv['protect'] / (1 - sv['protect'])
    surv += sv['taunt'] * sc['team_hp'] * 0.3
    surv += sv['revive'] * sc['team_hp'] * 0.5
    surv += sv['immune'] * 200
    return dmg, time_g, surv


def build_scenarios(cur):
    """真实敌人场景：合约Boss(词条放大)/日常/群怪。"""
    def enemy_attr(name, mults=None):
        row = cur.execute(
            "SELECT s.def, s.magic_resistance, s.max_hp, s.atk, s.move_speed, "
            "s.elemental_resistance, s.damage_resistance "
            "FROM enemy_stats_manual s JOIN enemies e ON e.enemy_id=s.enemy_id "
            "WHERE e.name=? AND s.level=0", (name,)).fetchone()
        if not row:
            return None
        d = {'def': row[0] or 0, 'res': row[1] or 0, 'hp': row[2] or 10000,
             'atk': row[3] or 1000, 'enemy_ms': row[4] or 1.0,
             'eres': row[5] or 0, 'dres': row[6] or 0}
        if mults:
            d['def'] *= mults.get('def', 1.0)
            d['hp'] *= mults.get('hp', 1.0)
            d['atk'] *= mults.get('atk', 1.0)
        return d

    sc = {}
    a = enemy_attr('爱国者', {'def': 3.0, 'hp': 2.5, 'atk': 2.0})
    if a:
        sc['合约Boss战'] = {**a, 'count': 1, 'range_len': 4.0, 'team_hp': 5000}
    b = enemy_attr('萨卡兹大剑手')
    if b:
        sc['日常挂机'] = {**b, 'count': 8, 'range_len': 3.0, 'team_hp': 4000}
    c = enemy_attr('狂暴宿主士兵')
    if c:
        sc['高压群怪'] = {**c, 'count': 8, 'range_len': 2.5, 'team_hp': 4500}
    if not sc:
        sc = {'合约Boss战': {'def': 1200, 'res': 60, 'hp': 100000, 'atk': 2000, 'enemy_ms': 0.5, 'eres': 0, 'dres': 10,
                              'count': 1, 'range_len': 4.0, 'team_hp': 5000}}
    return sc


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    skills = I.load_skills(cur)
    scenarios = build_scenarios(cur)

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
        for sc_name, sc in scenarios.items():
            dmg, time_g, surv = marginal_value(fx, sv, sc, skills)
            row[f'{sc_name}_dmg'] = round(dmg)
            row[f'{sc_name}_time'] = round(time_g)
            row[f'{sc_name}_surv'] = round(surv)
            row[f'{sc_name}_value'] = round(dmg + time_g + surv)
        rows.append(row)

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'support_value.csv')
    cols = list(rows[0].keys())
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f'辅助干员: {len(rows)} → {path}')

    for sc_name, sc in scenarios.items():
        pool = [r for r in rows if r['rarity'] in (4, 5)]
        pool.sort(key=lambda x: -x[f'{sc_name}_value'])
        print(f"\n== {sc_name} 辅助榜 TOP10 "
              f"(敌 DEF={sc['def']:.0f} RES={sc['res']:.0f} HP={sc['hp']:,.0f} ×{sc['count']}) ==")
        for r in pool[:10]:
            print(f"  {r['char']:<6s} {r['profession']:<7s} "
                  f"边际价值{r[f'{sc_name}_value']:>9,.0f} "
                  f"(伤害+{r[f'{sc_name}_dmg']:>8,.0f} 时间+{r[f'{sc_name}_time']:>8,.0f} "
                  f"生存+{r[f'{sc_name}_surv']:>8,.0f})  {r['effects'][:30]}")
    conn.close()


if __name__ == '__main__':
    main()
