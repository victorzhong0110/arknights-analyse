#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
危机合约单独建模（高难挑战场景）。

合约场景 = 真实敌人(基础属性) × 合约词条(敌属性倍率)：
  - 输出：放大后 DEF/RES 下的有效循环DPS / 爆发（含混伤/元素/真伤）
  - 生存：面对放大后 ATK 的承受能力（EHP 命中数）
  - 辅助：控制/增伤/减防（合约生存压力下价值更高）

综合分 = w_out·输出 + w_surv·生存 + w_sup·辅助（各维度 min-max 归一）

用法：python3 scripts/contract_model.py [--enemy 爱国者] [--tags def_3,hp_3,atk_3]
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import evaluate_operators as E  # noqa: E402
import interval_analysis as I  # noqa: E402
import rank_engine as R  # noqa: E402
import effect_analysis as F  # noqa: E402

DB = E.DB
OUT = E.OUT


def load_tags():
    with open(os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'crisis_tags.json'),
              encoding='utf-8') as f:
        return json.load(f)


def contract_scenarios(cur, tags, only_enemy=None):
    """合约代表敌人(精英/BOSS) × 默认满词条(def_3+hp_3+atk_3)。"""
    q = ("SELECT e.enemy_id, e.name, s.def, s.magic_resistance, s.max_hp, s.atk, "
         "s.elemental_resistance, s.damage_resistance FROM enemy_stats_manual s "
         "JOIN enemies e ON e.enemy_id=s.enemy_id "
         "WHERE s.level=0 AND e.enemy_level IN ('ELITE','BOSS') "
         "AND s.def IS NOT NULL AND s.max_hp IS NOT NULL ")
    if only_enemy:
        q += "AND e.name=?"
        rows = cur.execute(q, (only_enemy,)).fetchall()
    else:
        rows = cur.execute(q).fetchall()
    # 取 DEF 最高的若干 BOSS 作为合约代表
    rows = sorted(rows, key=lambda r: -(r[2] or 0))[:20]
    out = []
    for eid, name, df, res, hp, atk, eres, dres in rows:
        # 默认满词条：反装甲Ⅲ(×3) + 活性Ⅲ(×2.5) + 刺激Ⅲ(×2)
        out.append({'name': name, 'def': (df or 0) * 3.0, 'res': res or 0,
                    'hp': (hp or 0) * 2.5, 'atk': (atk or 0) * 2.0,
                    'eres': eres or 0, 'dres': dres or 0})
    return out


def minmax(vals):
    lo, hi = min(vals), max(vals)
    return [(v - lo) / (hi - lo) if hi > lo else 0.0 for v in vals]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--enemy', help='限定单个合约敌人')
    ap.add_argument('--top', type=int, default=20)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    tags = load_tags()
    skills = I.load_skills(cur)

    # 每干员取最佳输出技能
    best = {}
    for sk in skills:
        if sk['char'] not in best:
            best[sk['char']] = sk
        else:
            # 按 0 防循环DPS 择优
            pass

    scenarios = contract_scenarios(cur, tags, args.enemy)
    print(f'合约代表敌人: {len(scenarios)} 个')

    # 预计算每干员的最佳技能 + 生存 + 辅助
    chars = cur.execute(
        "SELECT char_id, name, profession, rarity FROM operators "
        "WHERE source='character' AND profession NOT IN ('TOKEN','TRAP')").fetchall()
    op_data = {}
    for ch in chars:
        fs = E.final_stats(cur, ch['char_id'])
        if fs is None:
            continue
        stats, _, _ = fs
        stats['_char_id'] = ch['char_id']
        fx, _ = F.extract_support(cur, ch['char_id'], ch['name'], ch['profession'],
                                  stats['atk'], stats)
        op_data[ch['name']] = {'profession': ch['profession'], 'rarity': ch['rarity'],
                               'stats': stats, 'fx': fx}

    # 对每个合约敌人计算综合排行
    for sc in scenarios:
        rows = []
        for name, od in op_data.items():
            stats = od['stats']
            # 输出：该干员所有技能里在此敌人属性下的最大循环DPS
            best_dps = 0.0
            best_skill = ''
            for sk in skills:
                if sk['char'] != name:
                    continue
                m = R.skill_metrics(sk, sc['def'], sc['res'], sc['eres'], sc['dres'], 1)
                if m['cycle_dps'] > best_dps:
                    best_dps, best_skill = m['cycle_dps'], sk['skill']
            # 生存：面对放大后 ATK 的命中数
            ehp = stats['max_hp'] / max(sc['atk'] - stats['def'], sc['atk'] * 0.05)
            # 辅助：控制(时间) + 脆弱(增伤)
            sup = od['fx']['control_dur'] * 0.5 + od['fx']['fragile'] * 100 + od['fx']['slow'] * 50
            rows.append({'char': name, 'profession': od['profession'], 'rarity': od['rarity'],
                         'skill': best_skill, 'dps': best_dps, 'ehp': ehp, 'sup': sup})
        # 归一化
        d = minmax([r['dps'] for r in rows])
        v = minmax([r['ehp'] for r in rows])
        s = minmax([r['sup'] for r in rows])
        for i, r in enumerate(rows):
            r['score'] = 0.4 * d[i] + 0.3 * v[i] + 0.3 * s[i]
        rows.sort(key=lambda x: -x['score'])
        six = [r for r in rows if r['rarity'] == 5]
        print(f"\n== 合约「{sc['name']}」 DEF={sc['def']:.0f} RES={sc['res']:.0f} "
              f"HP={sc['hp']:,.0f} ATK={sc['atk']:,.0f} 综合榜 TOP10 ==")
        for r in six[:10]:
            print(f"  {r['char']:<6s} {r['skill']:<8s} 综合{r['score']:>5.3f} "
                  f"输出{r['dps']:>8,.0f} 生存{r['ehp']:>5.1f}击 辅助{r['sup']:>6.1f}")
    conn.close()


if __name__ == '__main__':
    main()
