#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合测评：2-4 人组合能力计算与排名（≤4人）。

思路：
  - 输出池：六星持续输出 TOP 技能（按 循环DPS+爆条 排序）
  - 辅助池：辅助效果排行前列（脆弱/减防/友方攻击/攻速/减速/控制）
  - 组合：1 输出 + 1..3 辅助，按辅助效果对输出的提升计算组合持续输出
  - 增益口径：脆弱→伤害×(1+fragile)；减防→目标防御削减；友方攻击→atk×(1+增益)；
             友方攻速→间隔缩短（不作用于弹药型）；控制/减速→记入控制分
  - 基准：日常 DEF=300 / 合约 DEF=1200（物理）；法抗固定 20/60

输出：data/analysis/combo_ranking.csv（含 2/3/4 人组合 TOP）
用法：python3 scripts/combo_analysis.py
"""
import csv
import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_skills as A  # noqa: E402
import evaluate_operators as E  # noqa: E402

DB = E.DB
OUT = E.OUT

N_DPS = 25
N_SUP = 18


def load_dps_pool():
    """从 summary 载入六星输出技能（循环DPS+爆条），返回 [{char,skill,atk,mult,hit_times,attack_count,cycle_time,duration,sp_type,respawn,dmg_type,burst,chain...}]"""
    pool = []
    with open(os.path.join(OUT, 'operator_summary.csv'), encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r['rarity'] != '5':
            continue
        cyc = float(r['cycle_dps_eff']) if r['cycle_dps_eff'] else 0
        bst = float(r['burst_dps']) if r['burst_dps'] else 0
        val = cyc + bst
        if val <= 0:
            continue
        pool.append({
            'char': r['char_name'], 'skill': r['skill_name'],
            'dmg_type': r['damage_type'],
            'atk': float(r['atk_final']), 'mult': float(r['mult']),
            'hit_times': float(r['hit_times']), 'attack_count': int(r['attack_count']),
            'duration': float(r['duration']) if r['duration'] else None,
            'sp_cost': float(r['sp_cost']) if r['sp_cost'] else 0,
            'init_sp': float(r['init_sp']) if r['init_sp'] else 0,
            'sp_type': r['sp_type'], 'burst_dps': bst,
            'cycle_dps': cyc, 'base_val': val,
        })
    pool.sort(key=lambda x: -x['base_val'])
    return pool[:N_DPS]


def load_support_pool():
    pool = []
    with open(os.path.join(OUT, 'support_ranking.csv'), encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if r['rarity'] not in ('4', '5'):
            continue
        fx = {
            'fragile': float(r['fragile']), 'def_shred_pct': float(r['def_shred_pct']),
            'def_shred_flat': float(r['def_shred_flat']), 'ally_atk_pct': float(r['ally_atk_pct']),
            'ally_as': float(r['ally_as']), 'slow': float(r['slow']),
            'control_dur': float(r['control_dur']),
            'inspire_pct': float(r['inspire_pct']) if r.get('inspire_pct') else 0.0,
            'inspire_atk': float(r['inspire_atk']) if r.get('inspire_atk') else 0.0,
        }
        if max(fx.values()) <= 0:
            continue
        fx['char'] = r['char_name']
        fx['support_score'] = float(r['support_score'])
        pool.append(fx)
    pool.sort(key=lambda x: -x['support_score'])
    return pool[:N_SUP]


def dps_sustained(dps, def_, res, atk_mult=1.0, dmg_mult=1.0, def_shred_pct=0.0, def_shred_flat=0.0, inspire_flat=0.0):
    """单目标持续输出（含爆条元素分量）。"""
    atk = dps['atk'] * atk_mult + inspire_flat
    per_hit = atk * dps['mult']
    d = max(def_ * (1 - def_shred_pct) - def_shred_flat, 0)
    t = dps['dmg_type']
    if t == 'physical':
        eff = max(per_hit - d, per_hit * 0.05)
    elif t == 'arts':
        eff = per_hit * max(1 - res / 100, 0)
    elif t == 'true':
        eff = per_hit
    else:
        eff = max(per_hit - d, per_hit * 0.05)
    eff *= dmg_mult
    total = eff * dps['hit_times'] * dps['attack_count']
    base = None
    if dps.get('cycle_time'):
        base = total / dps['cycle_time']
    elif dps['duration']:
        base = total / dps['duration']
    if base is None:
        base = total
    return base + dps['burst_dps'] * dmg_mult


def main():
    dps_pool = load_dps_pool()
    sup_pool = load_support_pool()
    print(f'输出池: {len(dps_pool)} | 辅助池: {len(sup_pool)}')

    results = []
    for dps in dps_pool:
        solo = {300: dps_sustained(dps, 300, 20), 1200: dps_sustained(dps, 1200, 60)}
        # 1 辅助
        for s1 in sup_pool:
            if s1['char'] == dps['char']:
                continue
            for def_, res in ((300, 20), (1200, 60)):
                v = dps_sustained(dps, def_, res,
                                  atk_mult=1 + s1['ally_atk_pct'],
                                  dmg_mult=1 + s1['fragile'],
                                  def_shred_pct=s1['def_shred_pct'],
                                  def_shred_flat=s1['def_shred_flat'],
                                  inspire_flat=s1['inspire_pct'] * s1['inspire_atk'])
                results.append({'size': 2, 'dps': dps['char'], 'skill': dps['skill'],
                                'supports': s1['char'], 'def': def_,
                                'combined': round(v), 'uplift': round(v / solo[def_] - 1, 3),
                                'utility': round(s1['control_dur'] + s1['slow'] * 10, 1)})
        # 2 辅助
        for i, s1 in enumerate(sup_pool):
            for s2 in sup_pool[i + 1:]:
                if s1['char'] == dps['char'] or s2['char'] == dps['char'] or s1['char'] == s2['char']:
                    continue
                atk_m = 1 + s1['ally_atk_pct'] + s2['ally_atk_pct']
                dmg_m = 1 + s1['fragile'] + s2['fragile']
                shp = s1['def_shred_pct'] + s2['def_shred_pct']
                shf = s1['def_shred_flat'] + s2['def_shred_flat']
                inspire = s1['inspire_pct'] * s1['inspire_atk'] + s2['inspire_pct'] * s2['inspire_atk']
                for def_, res in ((300, 20), (1200, 60)):
                    v = dps_sustained(dps, def_, res, atk_m, dmg_m, shp, shf, inspire)
                    results.append({'size': 3, 'dps': dps['char'], 'skill': dps['skill'],
                                    'supports': f"{s1['char']}+{s2['char']}", 'def': def_,
                                    'combined': round(v), 'uplift': round(v / solo[def_] - 1, 3),
                                    'utility': round(s1['control_dur'] + s2['control_dur']
                                                     + (s1['slow'] + s2['slow']) * 10, 1)})
        # 3 辅助
        for i, s1 in enumerate(sup_pool):
            for j, s2 in enumerate(sup_pool[i + 1:], i + 1):
                for s3 in sup_pool[j + 1:]:
                    names = {dps['char'], s1['char'], s2['char'], s3['char']}
                    if len(names) < 4:
                        continue
                    atk_m = 1 + s1['ally_atk_pct'] + s2['ally_atk_pct'] + s3['ally_atk_pct']
                    dmg_m = 1 + s1['fragile'] + s2['fragile'] + s3['fragile']
                    shp = s1['def_shred_pct'] + s2['def_shred_pct'] + s3['def_shred_pct']
                    shf = s1['def_shred_flat'] + s2['def_shred_flat'] + s3['def_shred_flat']
                    inspire = sum(x['inspire_pct'] * x['inspire_atk'] for x in (s1, s2, s3))
                    for def_, res in ((300, 20), (1200, 60)):
                        v = dps_sustained(dps, def_, res, atk_m, dmg_m, shp, shf, inspire)
                        results.append({'size': 4, 'dps': dps['char'], 'skill': dps['skill'],
                                        'supports': f"{s1['char']}+{s2['char']}+{s3['char']}",
                                        'def': def_, 'combined': round(v),
                                        'uplift': round(v / solo[def_] - 1, 3),
                                        'utility': round(s1['control_dur'] + s2['control_dur']
                                                         + s3['control_dur']
                                                         + (s1['slow'] + s2['slow'] + s3['slow']) * 10, 1)})

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'combo_ranking.csv')
    cols = ['size', 'dps', 'skill', 'supports', 'def', 'combined', 'uplift', 'utility']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sorted(results, key=lambda x: (-x['combined']))[:3000])
    print(f'组合数: {len(results)} → {path} (TOP3000)')

    for sz, cn in ((2, '双人'), (3, '三人'), (4, '四人')):
        for def_, tag in ((300, '日常DEF300'), (1200, '合约DEF1200')):
            top = [r for r in results if r['size'] == sz and r['def'] == def_]
            top.sort(key=lambda x: -x['combined'])
            print(f'\n== {cn}组合 TOP5（{tag}）==')
            for r in top[:5]:
                print(f"  {r['dps']}「{r['skill']}」+{r['supports']} "
                      f"组合DPS {r['combined']:>8,.0f} (提升{r['uplift']*100:+.0f}%)")


if __name__ == '__main__':
    main()
