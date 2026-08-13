#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
敌人防御/法抗基准：从 enemy_stats_manual（PRTS 全量）计算百分位基准。

输出：data/analysis/enemy_benchmark.csv
用法：python3 scripts/enemy_benchmark.py
      AK_DB_PATH=/tmp/x.db AK_OUT=<dir> python3 scripts/enemy_benchmark.py
"""
import csv
import os
import sqlite3

DB = os.environ.get('AK_DB_PATH',
                    os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'db', 'arknights.db')))
OUT = os.environ.get('AK_OUT',
                     os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'data', 'analysis')))

PCTS = (25, 50, 75, 90, 95)


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # 按敌人类别 + 全体 统计 level=0（常规难度）的 DEF / 法抗
    groups = [('ALL', None)]
    for lv in ('NORMAL', 'ELITE', 'BOSS'):
        groups.append((lv, lv))

    rows = []
    for gname, enemy_level in groups:
        if enemy_level is None:
            where, params = '', ()
        else:
            where, params = ('JOIN enemies e ON e.enemy_id = s.enemy_id '
                             'AND e.enemy_level = ?'), (enemy_level,)
        q = f"""
            SELECT s.def, s.magic_resistance, s.max_hp
            FROM enemy_stats_manual s {where}
            WHERE s.level = 0 AND s.def IS NOT NULL AND s.magic_resistance IS NOT NULL
        """
        data = cur.execute(q, params).fetchall()
        if not data:
            continue
        defs = sorted(d[0] for d in data)
        res = sorted(d[1] for d in data)
        hps = sorted(d[2] for d in data if d[2] is not None)
        row = {'group': gname, 'n': len(data)}
        for p in PCTS:
            row[f'def_p{p}'] = round(percentile(defs, p), 1)
            row[f'res_p{p}'] = round(percentile(res, p), 1)
        row['def_mean'] = round(sum(defs) / len(defs), 1)
        row['res_mean'] = round(sum(res) / len(res), 1)
        if hps:
            row['hp_p50'] = int(percentile(hps, 50))
            row['hp_mean'] = int(sum(hps) / len(hps))
        rows.append(row)
        print(f"{gname:8s} n={len(data):5d}  DEF P25/50/75/90 = "
              f"{row['def_p25']}/{row['def_p50']}/{row['def_p75']}/{row['def_p90']}  "
              f"RES P25/50/75/90 = {row['res_p25']}/{row['res_p50']}/{row['res_p75']}/{row['res_p90']}")

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'enemy_benchmark.csv')
    cols = ['group', 'n'] + [f'{k}_p{p}' for k in ('def', 'res') for p in PCTS] \
        + ['def_mean', 'res_mean', 'hp_p50', 'hp_mean']
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    print('CSV:', path)
    conn.close()


if __name__ == '__main__':
    main()
