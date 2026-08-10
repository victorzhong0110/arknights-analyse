#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能天使三个技能多目标参数分析：对比不同目标防御/法抗下的有效总伤与 DPS"""
import csv
import json
import os
import sys

SCRIPTS = '/Users/zhongxudong/Desktop/arknights-analyse/scripts'
sys.path.insert(0, SCRIPTS)
import analyze_skills as A  # noqa: E402

DB = A.DB
OUT = os.environ.get('AK_OUT', '/tmp/ak_out')

DEFS = [0, 200, 400, 600, 800]
RESS = [0, 20, 40]

import sqlite3  # noqa: E402

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

base = cur.execute("SELECT atk, base_attack_time FROM operator_levels "
                   "WHERE char_id=(SELECT char_id FROM operators WHERE name='能天使') "
                   "AND phase=2 ORDER BY level DESC LIMIT 1").fetchone()
skills = cur.execute("""
    SELECT os.skill_index, sl.name, sl.skill_type, sl.duration_type,
           sl.description, sl.duration, sl.sp_data, sl.blackboard
    FROM operator_skills os
    JOIN operators o ON o.char_id = os.char_id AND o.name = '能天使'
    JOIN skill_levels sl ON sl.skill_id = os.skill_id AND sl.level_index = 9
    ORDER BY os.skill_index
""").fetchall()

recs = []
for s in skills:
    bb = A.bb_dict(s['blackboard'])
    sp = json.loads(s['sp_data']) if s['sp_data'] else {}
    ch = {'char_id': 'char_103_angel', 'name': '能天使', 'profession': 'SNIPER'}
    for d in DEFS:
        for r_ in RESS:
            A.TARGET_DEF = d
            A.TARGET_RES = r_
            rec = A.compute(ch, s, bb, sp, base['atk'], base['base_attack_time'])
            if rec['computed'] != 'yes':
                continue
            eff = rec.get('effective_total_damage')
            eff_dps = eff / rec['duration'] if (eff and rec['duration']) else None
            eff_cycle = None
            if eff and rec.get('cycle_dps') is not None:
                sp_cost = rec.get('sp_cost') or 0
                init = rec.get('init_sp') or 0
                dur = rec['duration']
                ct = max(sp_cost - init, 0) + (dur if dur else 0)
                if ct > 0:
                    eff_cycle = eff / ct
            recs.append({
                'skill_index': s['skill_index'], 'skill_name': s['name'],
                'target_def': d, 'target_res': r_,
                'attack_count': rec['attack_count'],
                'per_hit_raw': round(rec['per_hit'], 1),
                'per_hit_eff': round(rec.get('per_hit_effective') or 0, 1),
                'total_raw': int(rec['total_damage']),
                'total_eff': int(eff) if eff else None,
                'dps_eff': round(eff_dps, 1) if eff_dps else None,
                'cycle_dps_eff': round(eff_cycle, 1) if eff_cycle else None,
            })

os.makedirs(OUT, exist_ok=True)
csv_path = os.path.join(OUT, 'angel_skills.csv')
with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=list(recs[0].keys()))
    w.writeheader()
    w.writerows(recs)

# 打印概览（每技能一行，防御变化）
names = {0: 'S1 冲锋', 1: 'S2 扫射', 2: 'S3 过载'}
for i in range(3):
    print(f"== {names[i]} ==")
    print(f"{'防御':>4} {'法抗':>4} {'每击原始':>8} {'每击有效':>8} {'总伤原始':>9} {'总伤有效':>9} {'技能DPS':>8} {'循环DPS':>8}")
    for r in [x for x in recs if x['skill_index'] == i]:
        def f(v):
            return '       -' if v is None else f'{v:>8,.1f}'
        print(f"{r['target_def']:>4} {r['target_res']:>4} {r['per_hit_raw']:>8.1f} {r['per_hit_eff']:>8.1f} "
              f"{r['total_raw']:>9,} {r['total_eff']:>9,} {f(r['dps_eff'])} {f(r['cycle_dps_eff'])}")
    print()
print('CSV:', csv_path)
