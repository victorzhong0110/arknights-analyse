#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心数据排行查询工具（S0）。

给定敌人状态（真实敌人 或 显式属性 + 合约词条），输出全角色各核心指标 TOP-N 排行。

用法：
  python3 scripts/query_rankings.py --enemy 爱国者              # 按敌人名
  python3 scripts/query_rankings.py --enemy-id enemy_1011_bgtr  # 按 enemy_id
  python3 scripts/query_rankings.py --def 1200 --res 50 --hp 50000   # 显式属性
  python3 scripts/query_rankings.py --enemy 爱国者 --tags def_3,hp_3  # 词条(反装甲Ⅲ+活性Ⅲ)
  python3 scripts/query_rankings.py --enemy 爱国者 --top 20 --targets 8
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interval_analysis as I  # noqa: E402
import rank_engine as R  # noqa: E402

DB = I.DB


def load_tags():
    with open(os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'crisis_tags.json'), encoding='utf-8') as f:
        return json.load(f)


def enemy_from_db(cur, enemy_id):
    row = cur.execute(
        "SELECT e.name, s.def, s.magic_resistance, s.elemental_resistance, s.damage_resistance, "
        "s.max_hp, s.atk, s.move_speed FROM enemy_stats_manual s "
        "JOIN enemies e ON e.enemy_id=s.enemy_id WHERE s.enemy_id=? AND s.level=0",
        (enemy_id,)).fetchone()
    if not row:
        return None
    return {'name': row[0], 'def': row[1] or 0, 'res': row[2] or 0,
            'eres': row[3] or 0, 'dres': row[4] or 0, 'hp': row[5] or 10000,
            'atk': row[6] or 1000, 'ms': row[7] or 1.0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--enemy', help='敌人名')
    ap.add_argument('--enemy-id', help='敌人 enemy_id')
    ap.add_argument('--defense', type=float, default=None)
    ap.add_argument('--res', type=float, default=0)
    ap.add_argument('--eres', type=float, default=0)
    ap.add_argument('--dres', type=float, default=0)
    ap.add_argument('--hp', type=float, default=None)
    ap.add_argument('--atk', type=float, default=1000)
    ap.add_argument('--tags', help='逗号分隔词条 id，如 def_3,hp_3')
    ap.add_argument('--top', type=int, default=20)
    ap.add_argument('--targets', type=int, default=1)
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    tags = load_tags()

    if args.enemy:
        eid = cur.execute("SELECT enemy_id FROM enemies WHERE name=?", (args.enemy,)).fetchone()
        if not eid:
            print(f'未找到敌人: {args.enemy}'); return
        st = enemy_from_db(cur, eid[0])
    elif args.enemy_id:
        st = enemy_from_db(cur, args.enemy_id)
    else:
        st = {'name': '自定义敌人', 'def': args.defense or 0, 'res': args.res,
              'eres': args.eres, 'dres': args.dres, 'hp': args.hp or 10000,
              'atk': args.atk, 'ms': 1.0}
    if not st:
        print('未找到敌人'); return

    # 应用合约词条
    applied = []
    if args.tags:
        for tid in args.tags.split(','):
            t = next((x for x in tags['enemy_stat_tags'] if x['id'] == tid), None)
            if not t:
                print(f'未知词条: {tid}'); return
            applied.append(t['name'])
            st['hp'] *= t.get('enemy_hp_mult', 1.0)
            st['atk'] *= t.get('enemy_atk_mult', 1.0)
            st['def'] *= t.get('enemy_def_mult', 1.0)
            st['ms'] *= t.get('enemy_ms_mult', 1.0)
            if t.get('enemy_res_set'):
                st['res'] = t['enemy_res_set']

    skills = I.load_skills(cur)
    print(f"敌人: {st['name']}  DEF={st['def']:.0f} RES={st['res']:.0f} "
          f"ERES={st['eres']:.0f} DRES={st['dres']:.0f} HP={st['hp']:,.0f} "
          f"ATK={st['atk']:,.0f}" + (f" 词条[{', '.join(applied)}]" if applied else ''))

    # 计算全角色指标
    scored = []
    for sk in skills:
        m = R.skill_metrics(sk, st['def'], st['res'], st['eres'], st['dres'], args.targets)
        ttk = st['hp'] / m['cycle_dps'] if m['cycle_dps'] > 0 else None
        scored.append((sk, m, ttk))

    tables = [
        ('循环DPS', 'cycle_dps', True),
        ('技能DPS', 'active_dps', True),
        ('单目标总伤', 'burst', True),
        (f'满{args.targets}目标总伤', 'burst_multi', True),
        ('DPH(破甲线)', 'dph', True),
        ('TTK(秒)', 'ttk', False),
    ]
    for label, key, desc in tables:
        def val(x):
            if key == 'ttk':
                return x[2]
            return x[1][key]
        ordered = sorted(scored, key=lambda x: -val(x) if desc else val(x))
        ordered = [x for x in ordered if val(x)]
        print(f'\n== {label} TOP{min(args.top, len(ordered))} ==')
        for sk, m, ttk in ordered[:args.top]:
            v = val((sk, m, ttk))
            elem = f" 元素{m['elem']:.0f}" if m['elem'] > 0 else ''
            print(f"  {sk['char']:<6s} {sk['skill']:<8s} {sk['dmg_type']:<8s} "
                  f"{v:>10,.1f}{elem}")
    conn.close()


if __name__ == '__main__':
    main()
