#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
优势区间分析：按敌人实际属性扫描，找出每个区间的最优输出。

实际范围来源：
  - 物理防御：enemy_stats_manual 实际值 0~8000（"阿米娅"，炉芯终曲 8000 / 哈兰杜汗 5000 / 磐蟹 4000 等）
  - 法术抗性：0~100（实际最大 90+）
  - 元素抗性：0~100

特殊机制：
  - 刻俄柏 天赋"剥壳"：每击额外造成目标防御力 X% 的法术伤害 → 高防区间强势
  - 赤刃明霄陈 S3"赤霄·天喟"：敌人当前生命值 X% 的法术伤害（处决类）→ 单独按敌人 HP 基准输出

输出：
  data/analysis/interval_phys.csv    物理技能 每防御点 有效持续DPS（全技能网格）
  data/analysis/interval_phys_winners.csv  物理区间归属（谁在哪个 DEF 区间最优）
  data/analysis/interval_arts.csv / _winners.csv  法术（法抗扫描）
  data/analysis/interval_element.csv / _winners.csv  元素（元素抗性扫描）
用法：python3 scripts/interval_analysis.py
"""
import csv
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_skills as A  # noqa: E402
import evaluate_operators as E  # noqa: E402

DB = E.DB
OUT = E.OUT

DEF_MAX = 8000      # 敌人表实际最大防御
DEF_STEP = 1        # 精确到 1 点防御
RES_MAX = 100
RES_STEP = 1
EL_MAX = 100
EL_STEP = 1
BENCH_DEFS = (0, 150, 300, 650, 1100, 2000, 4000, 8000)  # 基准防御点

HP_BENCH = (30000, 100000, 500000)  # 赤霄·天喟 处决基准：精英/BOSS/超BOSS 生命


def sustained_metric(rec):
    """持续输出口径：优先循环DPS（有cycle_time），否则技能DPS，否则爆发。"""
    if rec.get('cycle_time'):
        return rec['cycle_dps'], 'cycle'
    if rec.get('duration'):
        return rec.get('active_dps') or rec['total_damage'] / rec['duration'], 'dps'
    return rec['total_damage'], 'burst'


def load_skills(cur):
    """载入所有可计算技能 + 特殊天赋标记。返回 list of dict。"""
    rows = cur.execute(
        "SELECT char_id, name, profession, sub_profession_id, rarity FROM operators "
        "WHERE source='character' AND profession NOT IN ('TOKEN','TRAP') "
        "ORDER BY rarity DESC").fetchall()
    out = []
    for ch in rows:
        fs = E.final_stats(cur, ch['char_id'])
        if fs is None:
            continue
        stats, mod, talents = fs
        skills = cur.execute(
            "SELECT os.skill_index, sl.name, sl.description, sl.duration, sl.sp_data, sl.blackboard "
            "FROM operator_skills os JOIN skill_levels sl ON sl.skill_id=os.skill_id "
            "AND sl.level_index=9 WHERE os.char_id=? ORDER BY os.skill_index",
            (ch['char_id'],)).fetchall()
        # 特殊天赋：目标防御力X%额外法术伤害（刻俄柏 剥壳）
        def_extra = 0.0
        for (tdesc,) in cur.execute(
                "SELECT description FROM talents WHERE char_id=?", (ch['char_id'],)).fetchall():
            m = __import__('re').search(r'相当于其防御力(\d+)%', tdesc or '')
            if m:
                def_extra = max(def_extra, float(m.group(1)) / 100.0)
        for s in skills:
            bb = A.bb_dict(s['blackboard'])
            dmg_type = A.detect_damage_type(s['description'], ch['profession'])
            if dmg_type == 'none':
                continue
            # 与 evaluate 一致：注入天赋/模组攻速加成
            as_bonus = stats['attack_speed_bonus']
            skill_as = bb.get('attack_speed') or 0
            if as_bonus or skill_as:
                bb['attack_speed'] = skill_as + as_bonus
            sp = json.loads(s['sp_data']) if s['sp_data'] else {}
            A.TARGET_DEF = 0
            A.TARGET_RES = 0
            rec = A.compute({'char_id': ch['char_id'], 'name': ch['name'],
                             'profession': ch['profession']}, s, bb, sp,
                            stats['atk'], stats['base_attack_time'])
            if rec['computed'] != 'yes':
                continue
            out.append({
                'char': ch['name'], 'skill': rec['skill_name'], 'profession': ch['profession'],
                'sub': ch['sub_profession_id'], 'rarity': ch['rarity'],
                'dmg_type': dmg_type, 'atk': stats['atk'],
                'mult': rec['mult'], 'hit_times': rec['hit_times'],
                'attack_count': rec['attack_count'],
                'chain_times': rec.get('chain_times') or 0,
                'chain_scale': rec.get('chain_scale'),
                'max_target': rec.get('max_target') or 1,
                'cycle_time': rec.get('cycle_time'), 'duration': rec.get('duration'),
                'sp_type': rec.get('sp_type'),
                'respawn': (cur.execute(
                    "SELECT respawn_time FROM operator_levels WHERE char_id=? AND phase=2 "
                    "ORDER BY level DESC LIMIT 1", (ch['char_id'],)).fetchone() or (None,))[0],
                'burst_raw': rec['total_damage'],
                'elemental_dps': rec.get('elemental_dps') or 0,
                'burst_dps': rec.get('burst_dps') or 0,
                'element_type': rec.get('element_type'),
                'extra_raw': (rec.get('burst_extra_dps') or 0) * ((rec.get('time_to_burst') or 0) + 10.0) / 10.0,
                'pen_fixed': bb.get('def_penetrate_fixed') or bb.get('attack@def_penetrate_fixed') or 0,
                'pen_pct': bb.get('def_penetrate') or 0,
                'res_pen': bb.get('magic_resist_penetrate_fixed') or 0,
                'def_extra': def_extra,          # 刻俄柏类：目标防御力X%法术伤害
                'hp_ratio': bb.get('hp_ratio'),  # 赤霄天喟类：当前生命值X%法术伤害
                'min_scale': bb.get('projectile_min_atk_scale'),
                'res': stats['magic_resistance'],
            })
    return out


def eff_hit(sk, d, r, el=0):
    """单次命中有效伤害（含无视防御/刻俄柏 def_extra / 元素抗性）。"""
    per_hit = sk['atk'] * sk['mult']
    t = sk['dmg_type']
    if t == 'physical':
        eff_def = max(d - sk['pen_fixed'] - d * sk['pen_pct'], 0)
        main = max(per_hit - eff_def, per_hit * 0.05)
    elif t == 'arts':
        main = per_hit * max(1 - max(r - sk['res_pen'], 0) / 100, 0)
    elif t == 'true':
        main = per_hit
    else:  # mixed 按物理
        eff_def = max(d - sk['pen_fixed'] - d * sk['pen_pct'], 0)
        main = max(per_hit - eff_def, per_hit * 0.05)
    extra = 0.0
    if sk['def_extra']:
        extra = d * sk['def_extra'] * max(1 - r / 100, 0)  # 额外法术伤害
    elem = sk['burst_dps'] * max(1 - el / 100, 0) if el else sk['burst_dps']
    return main, extra, elem


def sustained(sk, d, r, el=0):
    """单目标持续DPS（含元素损伤分量；链击不计入单目标）。"""
    main, extra, elem = eff_hit(sk, d, r, el)
    per_atk = main * sk['hit_times'] + extra * sk['hit_times']
    total = per_atk * sk['attack_count']
    base = None
    if sk['cycle_time']:
        base = total / sk['cycle_time']
    elif sk['sp_type'] == 8 and sk['respawn']:
        # 部署回复（快活）：周期 ≈ 再部署时间
        base = total / max(sk['respawn'], sk['duration'] or 0)
    elif sk['duration']:
        base = total / sk['duration']
    if base is None:
        return None  # 无时间维度的单发技能 → 不属于持续口径
    return base + elem


def burst_total(sk, d, r, el=0):
    main, extra, elem = eff_hit(sk, d, r, el)
    per_atk = main * sk['hit_times'] + extra * sk['hit_times']
    return per_atk * sk['attack_count']


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    skills = load_skills(cur)
    phys = [s for s in skills if s['dmg_type'] in ('physical', 'mixed')]
    arts = [s for s in skills if s['dmg_type'] in ('arts', 'mixed')]
    elem = [s for s in skills if s['elemental_dps'] > 0]
    print(f'技能: 总{len(skills)} 物理{len(phys)} 法术{len(arts)} 元素{len(elem)}')
    os.makedirs(OUT, exist_ok=True)

    def sweep(skills, points, axis, fname, wname, metric='sustained'):
        grid, segs = [], []
        prev_winner = None
        for v in points:
            best = None
            for sk in skills:
                if axis == 'def':
                    val = (sustained if metric == 'sustained' else burst_total)(sk, v, 0, 0)
                elif axis == 'res':
                    val = (sustained if metric == 'sustained' else burst_total)(sk, 0, v, 0)
                else:
                    val = (sustained if metric == 'sustained' else burst_total)(sk, 0, 0, v)
                if val is None:
                    continue
                if best is None or val > best[2]:
                    best = (sk['char'], sk['skill'], val)
            if best is None:
                continue
            cur = (best[0], best[1])
            grid.append({'value': v, 'winner': best[0], 'skill': best[1],
                         'dps': round(best[2], 1)})
            if prev_winner is None:
                seg_start, seg_val = v, best[2]
            elif cur != prev_winner:
                segs.append({'from': seg_start, 'to': v, 'winner': prev_winner[0],
                             'skill': prev_winner[1], 'dps': round(seg_val, 1)})
                seg_start, seg_val = v, best[2]
            prev_winner = cur
        if prev_winner:
            segs.append({'from': seg_start, 'to': points[-1], 'winner': prev_winner[0],
                         'skill': prev_winner[1], 'dps': round(seg_val, 1)})
        with open(os.path.join(OUT, fname), 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=['value', 'winner', 'skill', 'dps'])
            w.writeheader()
            w.writerows(grid)
        with open(os.path.join(OUT, wname), 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=['from', 'to', 'winner', 'skill', 'dps'])
            w.writeheader()
            w.writerows(segs)
        return grid, segs

    # 持续输出（有循环/持续时间的技能）
    all_dmg = [s for s in skills if s['dmg_type'] in ('physical', 'arts', 'true', 'mixed')]
    phys_sus = [s for s in phys if s['cycle_time'] or s['duration']]
    arts_sus = [s for s in arts if s['cycle_time'] or s['duration']]
    # 物抗扫描：全伤害类型（法术/真伤/元素不随物抗衰减）
    all_sus = [s for s in all_dmg if (s['cycle_time'] or s['duration'])]
    g_p, w_p = sweep(all_sus, range(0, DEF_MAX + 1, DEF_STEP), 'def',
                     'interval_phys.csv', 'interval_phys_winners.csv', 'sustained')
    g_a, w_a = sweep(arts_sus, range(0, RES_MAX + 1, RES_STEP), 'res',
                     'interval_arts.csv', 'interval_arts_winners.csv', 'sustained')
    g_e, w_e = sweep([s for s in skills if s['elemental_dps'] > 0],
                     range(0, EL_MAX + 1, EL_STEP), 'el',
                     'interval_element.csv', 'interval_element_winners.csv', 'sustained')
    # 单发爆发（无时间维度技能）单独扫描（全伤害类型）
    burst_all = [s for s in all_dmg if not s['cycle_time'] and not s['duration']]
    _, w_pb = sweep(burst_all, range(0, DEF_MAX + 1, DEF_STEP), 'def',
                    'interval_phys_burst.csv', 'interval_phys_burst_winners.csv', 'burst')
    _, w_ab = sweep([s for s in arts if not s['cycle_time'] and not s['duration']],
                    range(0, RES_MAX + 1, RES_STEP), 'res',
                    'interval_arts_burst.csv', 'interval_arts_burst_winners.csv', 'burst')

    # 损伤抵抗扫描（影响元素损伤积累→爆条快慢）
    elem_skills = [s for s in skills if s['elemental_dps'] > 0]
    dres_grid = []
    for v in range(0, 101, 1):
        best = None
        for sk in elem_skills:
            acc = sk['elemental_dps'] * (1 - v / 100)
            t = A.BURST_THRESHOLD / acc if acc > 0 else None
            if not t:
                continue
            cd = A.BURST_COOLDOWN.get(sk.get('element_type') or 'neural', 10.0)
            trig = A.BURST_TRIGGER.get(sk.get('element_type') or 'neural', 0.0) / (t + cd)
            extra = sk.get('extra_raw', 0) * cd / (t + cd)
            val = trig + extra
            if best is None or val > best[2]:
                best = (sk['char'], sk['skill'], val)
        if best:
            dres_grid.append({'value': v, 'winner': best[0], 'skill': best[1], 'dps': round(best[2], 1)})
    with open(os.path.join(OUT, 'interval_damage_res.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['value', 'winner', 'skill', 'dps'])
        w.writeheader(); w.writerows(dres_grid)
    segs, prev = [], None
    for r in dres_grid:
        cur = (r['winner'], r['skill'])
        if prev is None: seg_start, seg_val = r['value'], r['dps']
        elif cur != prev:
            segs.append((seg_start, r['value'], prev[0], prev[1], seg_val))
            seg_start, seg_val = r['value'], r['dps']
        prev = cur
    if prev: segs.append((seg_start, 100, prev[0], prev[1], seg_val))
    print('\n== 损伤抵抗 0-100 优势区间（爆条快慢维度）==')
    for a, b, w, s, d in segs:
        print(f"  损伤抵抗 {a:>3d}-{b:>3d}: {w}「{s}」 {d:>8,.1f}")

    # 处决类（赤霄·天喟）：按敌人 HP 基准
    exec_rows = []
    for sk in skills:
        if sk['hp_ratio'] and sk['min_scale']:
            for hp in HP_BENCH:
                dmg = max(hp * sk['hp_ratio'], sk['atk'] * sk['min_scale'])
                exec_rows.append({'char': sk['char'], 'skill': sk['skill'],
                                  'enemy_hp': hp, 'damage': int(dmg)})
    with open(os.path.join(OUT, 'interval_execute.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=['char', 'skill', 'enemy_hp', 'damage'])
        w.writeheader()
        w.writerows(exec_rows)

    print('\n== 物理防御 0-8000 持续输出优势区间 ==')
    for wseg in w_p:
        print(f"  DEF {wseg['from']:>5d}-{wseg['to']:>5d}: {wseg['winner']}「{wseg['skill']}」"
              f" 持续DPS {wseg['dps']:>9,.0f}")
    print('\n== 法术抗性 0-100 持续输出优势区间 ==')
    for wseg in w_a:
        print(f"  法抗 {wseg['from']:>3d}-{wseg['to']:>3d}: {wseg['winner']}「{wseg['skill']}」"
              f" {wseg['dps']:>9,.0f}")
    print('\n== 元素抗性 0-100 持续输出优势区间 ==')
    for wseg in w_e:
        print(f"  元素抗 {wseg['from']:>3d}-{wseg['to']:>3d}: {wseg['winner']}「{wseg['skill']}」"
              f" {wseg['dps']:>9,.0f}")
    print('\n== 物理防御 0-8000 单发爆发优势区间 ==')
    for wseg in w_pb:
        print(f"  DEF {wseg['from']:>5d}-{wseg['to']:>5d}: {wseg['winner']}「{wseg['skill']}」"
              f" 爆发 {wseg['dps']:>9,.0f}")
    if exec_rows:
        print('\n== 处决类（当前生命值%伤害）==')
        for r in exec_rows:
            print(f"  {r['char']}「{r['skill']}」 对HP {r['enemy_hp']:>7,} → {r['damage']:>10,}")

    # 逐干员 DPS 基准剖面 + 优势区间（全伤害类型，物理按防御扫描）
    profiles = []
    for sk in all_sus:
        row = {'char': sk['char'], 'skill': sk['skill'], 'dmg_type': sk['dmg_type'],
               'profession': sk['profession'], 'rarity': sk['rarity']}
        for d in BENCH_DEFS:
            row[f'dps@{d}'] = round(sustained(sk, d, 20, 0))  # 法抗固定20
        # 该技能在防御扫描中的最优区间
        best_def, best_dps = 0, None
        for d in range(0, DEF_MAX + 1, DEF_STEP):
            v = sustained(sk, d, 20, 0)
            if best_dps is None or v > best_dps:
                best_dps, best_def = v, d
        row['peak_dps'] = round(best_dps)
        row['peak_def'] = best_def
        profiles.append(row)
    with open(os.path.join(OUT, 'interval_operator_summary.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        cols = ['char', 'skill', 'dmg_type', 'profession', 'rarity'] \
            + [f'dps@{d}' for d in BENCH_DEFS] + ['peak_dps', 'peak_def']
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(profiles)
    print(f'\n逐干员优势区间剖面 → interval_operator_summary.csv ({len(profiles)} 技能)')
    conn.close()


if __name__ == '__main__':
    main()
