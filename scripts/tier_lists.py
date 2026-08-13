#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合约 / 日常 / 挂机 三份榜单（六星为主，含五星）。

口径（精二满级 0潜 专三 满信赖 最佳模组）：
  - 合约榜：0.35×循环DPS@BOSS基准(1200防/60抗) + 0.25×爆发@BOSS + 0.20×生存 + 0.20×辅助
  - 日常榜：0.45×循环DPS@全敌P50(300/20) + 0.25×爆发@P50 + 0.10×生存 + 0.10×辅助 + 0.10×挂机
  - 挂机榜：仅自动(技能type=2)或被动(0)技能，0.60×循环@P50 + 0.20×生存 + 0.10×辅助 + 0.10×爆发
  - 元素干员：循环DPS 含爆条分量（元素DPS+触发+爆条增伤）
  - 分级：S=前8% A=8-25% B=25-60% C=其余

输出：data/analysis/tier_contract.csv / tier_daily.csv / tier_afk.csv
用法：python3 scripts/tier_lists.py
"""
import csv
import os
import sqlite3

DB = os.environ.get('AK_DB_PATH', os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'db', 'arknights.db')))
OUT = os.environ.get('AK_OUT', os.path.normpath(
    os.path.join(os.path.dirname(__file__), '..', 'data', 'analysis')))


def load_eval():
    rows = {}
    with open(os.path.join(OUT, 'operator_eval.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            key = (r['char_name'], r['skill_index'])
            rows.setdefault(key, {})[r['scenario']] = r
    return rows


def load_summary():
    with open(os.path.join(OUT, 'operator_summary.csv'), encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def load_support():
    d = {}
    with open(os.path.join(OUT, 'support_ranking.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            d[r['char_name']] = float(r['support_score'])
    return d


def load_survival():
    d = {}
    with open(os.path.join(OUT, 'survival_ranking.csv'), encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            d[r['char_name']] = float(r['survival_score'])
    return d


def norm(vals):
    lo, hi = min(vals), max(vals)
    return [(v - lo) / (hi - lo) if hi > lo else 0.0 for v in vals]


def tier_by_rank(idx, n):
    p = idx / max(n, 1)
    return 'S' if p < 0.08 else 'A' if p < 0.25 else 'B' if p < 0.60 else 'C'


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    # 技能类型：M3 的 skill_type（0被动/1手动/2自动）——列是 TEXT，转 int
    stype = {}
    for cid, idx, t in cur.execute(
            "SELECT os.char_id, os.skill_index, sl.skill_type FROM operator_skills os "
            "JOIN skill_levels sl ON sl.skill_id=os.skill_id AND sl.level_index=9"):
        stype[(cid, idx)] = int(t)

    sup = load_support()
    surv = load_survival()
    summary = load_summary()
    ev = load_eval()

    rows = []
    seen = set()
    for r in summary:
        cname, sidx = r['char_name'], r['skill_index']
        if (cname, sidx) in seen:
            continue
        seen.add((cname, sidx))
        cid = None
        for row in cur.execute("SELECT char_id FROM operators WHERE name=?", (cname,)):
            cid = row[0]
            break
        s = stype.get((cid, int(sidx)), 1)
        e = ev.get((cname, sidx), {})
        p50 = e.get('all_p50') or {}
        boss = e.get('boss_p75') or {}

        def f(row, key):
            v = row.get(key)
            return float(v) if v else 0.0

        cyc_p50 = f(p50, 'cycle_dps_eff') + f(p50, 'burst_dps')
        bur_p50 = f(p50, 'burst_eff')
        cyc_boss = f(boss, 'cycle_dps_eff') + f(boss, 'burst_dps')
        bur_boss = f(boss, 'burst_eff')
        rows.append({
            'char': cname, 'profession': r['profession'], 'rarity': r['rarity'],
            'skill': r['skill_name'], 'skill_index': int(sidx),
            'skill_type': s, 'afk': 1 if s in (0, 2) else 0,
            'cycle_p50': cyc_p50, 'burst_p50': bur_p50,
            'cycle_boss': cyc_boss, 'burst_boss': bur_boss,
            'support': sup.get(cname, 0.0), 'survival': surv.get(cname, 0.0),
        })

    # 每干员取最佳技能（按各榜自己的口径）
    def best_by(rows, key_fn):
        best = {}
        for r in rows:
            k = r['char']
            if k not in best or key_fn(r) > key_fn(best[k]):
                best[k] = r
        return best

    def build(metric, pool_filter, w, fname):
        pool = [r for r in rows if pool_filter(r)]
        best = best_by(pool, metric)
        items = list(best.values())
        cyc = norm([metric(x) for x in items])
        sv = norm([x['survival'] for x in items])
        sp = norm([x['support'] for x in items])
        afk = norm([x['afk'] for x in items])
        if w.get('survival') is None:
            sv = [0.0] * len(items)
        out = []
        for i, x in enumerate(items):
            score = w['cycle'] * cyc[i] + w.get('burst', 0) * norm(
                [x['burst_p50'] if fname != 'contract' else x['burst_boss'] for x in items])[i] \
                + w.get('survival', 0) * sv[i] + w.get('support', 0) * sp[i] \
                + w.get('afk', 0) * afk[i]
            out.append({'char': x['char'], 'profession': x['profession'],
                        'rarity': x['rarity'], 'skill': x['skill'],
                        'skill_type': x['skill_type'], 'score': round(score, 3),
                        'cycle': round(x['cycle_p50'] if fname != 'contract' else x['cycle_boss']),
                        'burst': round(x['burst_p50'] if fname != 'contract' else x['burst_boss']),
                        'survival': round(x['survival']), 'support': round(x['support'])})
        out.sort(key=lambda x: -x['score'])
        for i, x in enumerate(out):
            x['tier'] = tier_by_rank(i, len(out))
        path = os.path.join(OUT, fname)
        with open(path, 'w', newline='', encoding='utf-8-sig') as f:
            wcsv = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            wcsv.writeheader()
            wcsv.writerows(out)
        return out

    six = lambda r: r['rarity'] == '5'
    contract = build(lambda r: r['cycle_boss'] + r['burst_boss'] * 0.6, six,
                     {'cycle': 0.35, 'burst': 0.25, 'survival': 0.20, 'support': 0.20},
                     'tier_contract.csv')
    daily = build(lambda r: r['cycle_p50'] + r['burst_p50'] * 0.5, six,
                  {'cycle': 0.45, 'burst': 0.25, 'survival': 0.10, 'support': 0.10, 'afk': 0.10},
                  'tier_daily.csv')
    afk = build(lambda r: r['cycle_p50'], lambda r: six(r) and r['afk'],
                {'cycle': 0.60, 'burst': 0.10, 'survival': 0.20, 'support': 0.10},
                'tier_afk.csv')

    for name, lst, cn in (('合约榜', contract, '合约'), ('日常榜', daily, '日常'), ('挂机榜', afk, '挂机')):
        print(f'\n== {name} ==')
        for t in ('S', 'A'):
            items = [x for x in lst if x['tier'] == t]
            if items:
                print(f'  {t}: ' + ' '.join(x['char'] for x in items))
    conn.close()


if __name__ == '__main__':
    main()
