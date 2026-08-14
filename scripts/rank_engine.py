#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
核心数据排行引擎（S0）。

设计（剪枝关键）：物理/法术/真伤/元素的排行互相独立——
  物理技能 → 只随 DEF 变；法术 → 只随 RES；真伤 → 恒定；元素 → 只随 ERES/DRES。
因此按轴分别计算排名后，给定敌人状态时可合并出全角色排行，无需全网格笛卡尔积。

产出（SQLite 表）：
  skill_metrics       每个技能的静态指标（DPH/类型/循环时间等）
  rank_physical       DEF 0..8000(×合约倍率) 各点 TOP-N 排行
  rank_arts           RES 0..100 各点 TOP-N
  rank_elemental      (ERES,DRES) 0..100 各点 TOP-N
  rank_true           真伤/无类型恒定排行
  advantage_intervals 各轴各指标第一名优势区间（稀疏，合并段）

用法：python3 scripts/rank_engine.py
      AK_TOPN=50 AK_DB=... python3 scripts/rank_engine.py
"""
import csv
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_skills as A  # noqa: E402
import interval_analysis as I  # noqa: E402

DB = I.DB
TOPN = int(os.environ.get('AK_TOPN', '50'))

# 合约词条倍率（方案A）：DEF/RES/HP/ATK 的放大系数（含"无词条"档）
DEF_MULTS = [1.0, 1.5, 2.0, 3.0]       # 反装甲 无/Ⅰ/Ⅱ/Ⅲ
RES_SETS = [None, 50, 75]              # 反法术 无/50/75
HP_MULTS = [1.0, 1.25, 1.6, 2.5]       # 活性 无/Ⅰ/Ⅱ/Ⅲ
ATK_MULTS = [1.0, 1.2, 1.55, 2.0]      # 刺激 无/Ⅰ/Ⅱ/Ⅲ

MAX_TARGETS = 8


def effective_hit(sk, def_, res):
    """单次命中有效伤害（含无视防御/刻俄柏 def_extra）。"""
    per_hit = sk['atk'] * sk['mult']
    t = sk['dmg_type']
    if t == 'physical':
        eff_def = max(def_ - sk['pen_fixed'] - def_ * sk['pen_pct'], 0)
        main = max(per_hit - eff_def, per_hit * 0.05)
    elif t == 'arts':
        main = per_hit * max(1 - max(res - sk['res_pen'], 0) / 100, 0)
    elif t == 'true':
        main = per_hit
    else:
        eff_def = max(def_ - sk['pen_fixed'] - def_ * sk['pen_pct'], 0)
        main = max(per_hit - eff_def, per_hit * 0.05)
    extra = 0.0
    if sk['def_extra']:
        extra = def_ * sk['def_extra'] * max(1 - res / 100, 0)
    return main, extra


def elemental_dps(sk, eres, dres):
    """元素爆条伤害 DPS（受元素抗性 eres 削减；损伤抵抗 dres 延迟爆条）。"""
    acc = sk['elemental_dps'] or 0
    if acc <= 0:
        return 0.0
    acc = acc * (1 - dres / 100)
    t = A.BURST_THRESHOLD / acc if acc > 0 else None
    if not t:
        return 0.0
    cd = A.BURST_COOLDOWN.get(sk.get('element_type') or 'neural', 10.0)
    trig = A.BURST_TRIGGER.get(sk.get('element_type') or 'neural', 0.0) / (t + cd)
    extra = (sk.get('extra_raw') or 0) * cd / (t + cd)
    return (trig + extra) * (1 - eres / 100)


def skill_metrics(sk, def_, res, eres, dres, targets):
    """核心指标：总伤(单/多)、技能DPS、循环DPS、DPH、TTK、爆条时间。"""
    main, extra = effective_hit(sk, def_, res)
    per_atk = main * sk['hit_times'] + extra * sk['hit_times']
    multi = per_atk
    if sk['chain_times'] and sk['chain_scale']:
        chain_hits = min(max(targets - 1, 0), sk['chain_times'])
        multi += sk['atk'] * sk['chain_scale'] * chain_hits * max(1 - res / 100, 0)
    if sk['max_target'] > 1:
        multi = max(multi, (main + extra) * sk['hit_times'] * min(sk['max_target'], targets))
    burst = per_atk * sk['attack_count']
    burst_multi = multi * sk['attack_count']
    elem = elemental_dps(sk, eres, dres)
    cyc = sk.get('cycle_time')
    dur = sk.get('duration')
    if cyc:
        cycle_dps = burst / cyc + elem
        active_dps = burst / dur + elem if dur else burst + elem
    elif dur:
        cycle_dps = burst / dur + elem
        active_dps = cycle_dps
    else:
        cycle_dps = burst + elem
        active_dps = cycle_dps
    acc = sk['elemental_dps'] * (1 - dres / 100)
    t_burst = A.BURST_THRESHOLD / acc if acc > 0 and sk['elemental_dps'] else None
    return {
        'burst': burst, 'burst_multi': burst_multi, 'active_dps': active_dps,
        'cycle_dps': cycle_dps, 'dph': sk['atk'] * sk['mult'],
        'elem': elem, 'time_to_burst': t_burst,
    }


def topn_rows(skills, axis_vals, metric_key, keyfn):
    """对每个轴值，算所有技能 metric，取 TOP-N。返回 [(axis_val, rank, char, skill, value)]"""
    out = []
    for av in axis_vals:
        scored = []
        for sk in skills:
            m = keyfn(sk, av)
            if m and m[1] > 0:
                scored.append((sk['char'], sk['skill'], m[0], m[1]))
        scored.sort(key=lambda x: -x[3])
        for rank, (c, s, _, v) in enumerate(scored[:TOPN], 1):
            out.append((av, rank, c, s, round(v, 1)))
    return out


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    skills = I.load_skills(cur)
    phys = [s for s in skills if s['dmg_type'] in ('physical', 'mixed')]
    arts = [s for s in skills if s['dmg_type'] == 'arts']
    true = [s for s in skills if s['dmg_type'] in ('true', 'none')]
    elem = [s for s in skills if s['elemental_dps'] > 0]
    print(f'技能: 物理{len(phys)} 法术{len(arts)} 真伤{len(true)} 元素{len(elem)}')

    # 建表
    cur.executescript("""
    DROP TABLE IF EXISTS skill_metrics;
    DROP TABLE IF EXISTS rank_physical;
    DROP TABLE IF EXISTS rank_arts;
    DROP TABLE IF EXISTS rank_elemental;
    DROP TABLE IF EXISTS rank_true;
    DROP TABLE IF EXISTS advantage_intervals;
    CREATE TABLE skill_metrics (
        char TEXT, skill TEXT, dmg_type TEXT, profession TEXT, rarity INTEGER,
        dph REAL, cycle_time REAL, duration REAL, chain_times INTEGER, max_target INTEGER
    );
    CREATE TABLE rank_physical (def INTEGER, rank INTEGER, char TEXT, skill TEXT, metric TEXT, value REAL);
    CREATE TABLE rank_arts (res INTEGER, rank INTEGER, char TEXT, skill TEXT, metric TEXT, value REAL);
    CREATE TABLE rank_elemental (eres INTEGER, dres INTEGER, rank INTEGER, char TEXT, skill TEXT, metric TEXT, value REAL);
    CREATE TABLE rank_true (rank INTEGER, char TEXT, skill TEXT, metric TEXT, value REAL);
    CREATE TABLE advantage_intervals (
        axis TEXT, metric TEXT, start_val REAL, end_val REAL, char TEXT, skill TEXT, value REAL
    );
    CREATE INDEX idx_rphys ON rank_physical(def, metric, rank);
    CREATE INDEX idx_rarts ON rank_arts(res, metric, rank);
    """)

    # skill_metrics 静态表
    for sk in skills:
        cur.execute("INSERT INTO skill_metrics VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (sk['char'], sk['skill'], sk['dmg_type'], sk['profession'], sk['rarity'],
                     round(sk['atk'] * sk['mult'], 1), sk.get('cycle_time'), sk.get('duration'),
                     sk['chain_times'] or 0, sk['max_target'] or 1))

    def all_defs():
        out = set()
        for m in DEF_MULTS:
            for d in range(0, I.DEF_MAX + 1, 10):
                out.add(min(int(d * m), 40000))
        return sorted(out)

    # 物理排行：DEF × 合约倍率
    def phys_kf(sk, d):
        m = skill_metrics(sk, d, 0, 0, 0, 1)
        return ('cycle_dps', m['cycle_dps'])
    rows = topn_rows(phys, all_defs(), 'cycle_dps', phys_kf)
    cur.executemany("INSERT INTO rank_physical VALUES (?,?,?,?,?,?)",
                    [(d, r, c, s, 'cycle_dps', v) for d, r, c, s, v in rows])
    # 物理爆发排行
    def phys_burst_kf(sk, d):
        m = skill_metrics(sk, d, 0, 0, 0, 1)
        return ('burst', m['burst'])
    rows = topn_rows(phys, all_defs(), 'burst', phys_burst_kf)
    cur.executemany("INSERT INTO rank_physical VALUES (?,?,?,?,?,?)",
                    [(d, r, c, s, 'burst', v) for d, r, c, s, v in rows])

    # 法术排行：RES
    def arts_kf(sk, r):
        m = skill_metrics(sk, 0, r, 0, 0, 1)
        return ('cycle_dps', m['cycle_dps'])
    rows = topn_rows(arts, list(range(0, 101)), 'cycle_dps', arts_kf)
    cur.executemany("INSERT INTO rank_arts VALUES (?,?,?,?,?,?)",
                    [(r_, r, c, s, 'cycle_dps', v) for r_, r, c, s, v in rows])

    # 元素排行：(ERES, DRES)
    elem_rows = []
    for eres in range(0, 101, 5):
        for dres in range(0, 101, 5):
            scored = []
            for sk in elem:
                m = skill_metrics(sk, 0, 0, eres, dres, 1)
                if m['cycle_dps'] > 0:
                    scored.append((sk['char'], sk['skill'], m['cycle_dps']))
            scored.sort(key=lambda x: -x[2])
            for rank, (c, s, v) in enumerate(scored[:TOPN], 1):
                elem_rows.append((eres, dres, rank, c, s, 'cycle_dps', round(v, 1)))
    cur.executemany("INSERT INTO rank_elemental VALUES (?,?,?,?,?,?,?)", elem_rows)

    # 真伤/无类型恒定排行
    true_rows = []
    scored = []
    for sk in true:
        m = skill_metrics(sk, 0, 0, 0, 0, 1)
        if m['cycle_dps'] > 0:
            scored.append((sk['char'], sk['skill'], m['cycle_dps']))
    scored.sort(key=lambda x: -x[2])
    for rank, (c, s, v) in enumerate(scored[:TOPN], 1):
        true_rows.append((rank, c, s, 'cycle_dps', round(v, 1)))
    cur.executemany("INSERT INTO rank_true VALUES (?,?,?,?,?)", true_rows)

    # 优势区间：从 interval_analysis 的 winners CSV 灌入（步长1的#1段）
    winner_files = [('def', 'cycle_dps', 'interval_phys_winners.csv'),
                    ('def', 'burst', 'interval_phys_burst_winners.csv'),
                    ('res', 'cycle_dps', 'interval_arts_winners.csv'),
                    ('el', 'cycle_dps', 'interval_element_winners.csv')]
    for axis, metric, fn in winner_files:
        p = os.path.join(I.OUT, fn)
        if not os.path.exists(p):
            continue
        with open(p, encoding='utf-8-sig') as f:
            for r in csv.DictReader(f):
                cur.execute("INSERT INTO advantage_intervals VALUES (?,?,?,?,?,?,?)",
                            (axis, metric, float(r['from']), float(r['to']),
                             r['winner'], r['skill'], float(r['dps'])))

    conn.commit()
    for t in ('skill_metrics', 'rank_physical', 'rank_arts', 'rank_elemental', 'rank_true', 'advantage_intervals'):
        n = cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'{t:24s} {n:>10,}')
    conn.close()


if __name__ == '__main__':
    main()
