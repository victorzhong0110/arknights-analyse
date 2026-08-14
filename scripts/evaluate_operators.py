#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全干员强度数据测评引擎（纸面理论值）。

口径：
- 属性：E2 满级基础 + 信赖200%(favor lv50) + 满潜(potentials) + 最佳模组 3 阶段(module_levels)
- 天赋：取 E2 解锁的最高候选；若模组加强该天赋则用模组升级版（含满潜候选）
- 技能：专精三 M3 (level_index=9)；伤害 = 单次命中 × 连击 × 攻击次数
- 场景：物理技能 → DEF 0 / 全敌P50(300) / P75(650) / P90(1100) / BOSS P75(1200)
        法术技能 → RES 0 / 全敌P50(20) / P75(50) / P90(60) / BOSS P75(60)
        真伤     → 仅 0/0
- 物理保底 5%、法术抗性线性减伤、真伤无视防御

输出：
  data/analysis/operator_eval.csv      每干员×技能×场景（长表）
  data/analysis/operator_summary.csv   每干员×技能基准摘要
用法：python3 scripts/evaluate_operators.py
      AK_CHAR=能天使 AK_DB_PATH=... AK_OUT=... python3 scripts/evaluate_operators.py
"""
import csv
import json
import math
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_skills as A  # noqa: E402

DB = os.environ.get('AK_DB_PATH',
                    os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'db', 'arknights.db')))
OUT = os.environ.get('AK_OUT',
                     os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'data', 'analysis')))
ONLY = os.environ.get('AK_CHAR')  # 单干员调试

# Arknights AttributeType 枚举
ATTR_TYPE = {
    0: 'max_hp', 1: 'atk', 2: 'def', 3: 'magic_resistance', 4: 'cost',
    5: 'block_cnt', 6: 'move_speed', 7: 'attack_speed', 8: 'base_attack_time',
    9: 'respawn_time', 10: 'hp_recovery_per_sec', 11: 'sp_recovery_per_sec',
    12: 'max_deploy_count', 13: 'max_deck_stack_cnt',
}
# formulaItem: 0 ADD / 1 ADD_PERCENT / 3 FINAL_ADD / 4 FINAL_ADD_PERCENT
FLAT_F = (0, 3)
PCT_F = (1, 4)

# 场景定义：(def, res, label)
SCEN_PHYS = [(0, 0, 'base'), (300, 0, 'all_p50'), (650, 0, 'all_p75'),
             (1100, 0, 'all_p90'), (1200, 60, 'boss_p75')]
SCEN_ARTS = [(0, 0, 'base'), (0, 20, 'all_p50'), (0, 50, 'all_p75'),
             (0, 60, 'all_p90'), (0, 70, 'boss_p75')]
SCEN_TRUE = [(0, 0, 'base')]


def jload(s):
    return json.loads(s) if s else None


def bb_dict(blackboard):
    return {kv['key']: kv.get('value') for kv in (blackboard or [])}


def aggregate_potentials(pot_rows):
    """潜能的 attributeModifiers → {attr: {'flat': x, 'pct': y}}"""
    out = {}
    for (blackboard,) in pot_rows:
        bb = jload(blackboard)
        if not bb:
            continue
        for m in (bb.get('attributes') or {}).get('attributeModifiers') or []:
            at = ATTR_TYPE.get(m.get('attributeType'))
            if not at:
                continue
            fi = m.get('formulaItem')
            v = m.get('value') or 0
            d = out.setdefault(at, {'flat': 0.0, 'pct': 0.0})
            if fi in FLAT_F:
                d['flat'] += v
            elif fi in PCT_F:
                d['pct'] += v / 100.0 if abs(v) > 1 else v
            # formulaItem 2 (ADD_AND_ADD_PERCENT) 少见，忽略
    return out


def talent_stat(bb):
    """天赋黑板 → 可叠加到面板的数值 {stat: {'flat':..,'pct':..}}"""
    out = {}
    for k, v in bb.items():
        if k in ('atk', 'max_hp', 'def', 'magic_resistance'):
            pct = (isinstance(v, float) and 0 < v < 1) or (isinstance(v, int) and v == 0)
            out[k] = {'flat': 0.0 if pct else (v or 0), 'pct': v if pct else 0.0}
        elif k in ('attack_speed', 'cost', 'move_speed', 'block_cnt',
                   'max_deploy_count', 'respawn_time'):
            out[k] = {'flat': v or 0, 'pct': 0.0}
    return out


def merge_stat(a, b):
    for k, v in b.items():
        d = a.setdefault(k, {'flat': 0.0, 'pct': 0.0})
        d['flat'] += v.get('flat', 0.0)
        d['pct'] += v.get('pct', 0.0)
    return a


# 计算口径：精二满级 + 信赖200% + 最佳模组3阶段 + 潜能(默认0潜)
USE_POT = os.environ.get('AK_POT', '0') == '1'   # AK_POT=1 时启用满潜


def best_talent_candidates(cur, char_id, module_talent_upgrades):
    """每个天赋取最佳候选（E2 解锁；0潜时仅 required_potential_rank=0）。
    模组加强优先。返回 {talent_index: stat}"""
    result = {}
    rows = cur.execute(
        "SELECT talent_index, unlock_phase, required_potential_rank, blackboard "
        "FROM talents WHERE char_id=? ORDER BY talent_index, candidate_index",
        (char_id,)).fetchall()
    by_talent = {}
    for t_index, phase, pot_rank, bb in rows:
        by_talent.setdefault(t_index, []).append((phase, pot_rank, bb))
    for t_index, cands in by_talent.items():
        cands.sort(key=lambda x: (x[0], x[1]))  # unlock_phase 优先, 其次潜能
        if not USE_POT:
            cands = [c for c in cands if c[1] == 0]  # 0潜：仅基础候选
        _, _, bb = cands[-1]
        result[t_index] = talent_stat(bb_dict(jload(bb)))

    # 模组天赋升级覆盖（0潜取 rank=0 候选）
    for tu in module_talent_upgrades or []:
        t_index = tu.get('talent_index')
        if t_index is None:
            continue
        cand = tu
        if not USE_POT and tu.get('required_potential_rank') not in (0, None):
            continue
        bb = bb_dict(cand.get('blackboard') or [])
        result[t_index] = talent_stat(bb)
    return result


def pick_module(cur, char_id):
    """选择最佳模组（ADVANCED, 3阶段, 取 atk 最高）；返回 (module_id, name, stats, talent_upgrades) 或 None"""
    best = None
    for mid, name, atk in cur.execute(
            "SELECT m.equip_id, m.name, ml.atk FROM modules m "
            "JOIN module_levels ml ON ml.equip_id = m.equip_id AND ml.equip_level = 3 "
            "WHERE m.char_id = ? AND m.type != 'INITIAL' ORDER BY ml.atk DESC",
            (char_id,)).fetchall():
        if best is None or (atk or 0) > best[2]:
            best = (mid, name, atk or 0)
    if best is None:
        return None
    mid, name, _ = best
    row = cur.execute(
        "SELECT max_hp, atk, def, magic_resistance, attack_speed, cost, "
        "respawn_time, block_cnt, move_speed, talent_upgrades "
        "FROM module_levels WHERE equip_id=? AND equip_level=3", (mid,)).fetchone()
    stats = {}
    for col in ('max_hp', 'atk', 'def', 'magic_resistance', 'attack_speed',
                'cost', 'respawn_time', 'block_cnt', 'move_speed'):
        v = row[col]
        if v:
            stats[col] = {'flat': v, 'pct': 0.0}
    return (mid, name, stats, jload(row['talent_upgrades']))


def final_stats(cur, char_id):
    """计算最终面板。返回 (stats dict, module_info, talent_summary)"""
    base = cur.execute(
        "SELECT max_hp, atk, def, magic_resistance, base_attack_time, move_speed, "
        "cost, block_cnt "
        "FROM operator_levels WHERE char_id=? AND phase=2 ORDER BY level DESC LIMIT 1",
        (char_id,)).fetchone()
    if not base:
        return None
    b_max_hp, b_atk, b_def, b_res, b_bat, b_ms, b_cost, b_block = base

    trust = cur.execute(
        "SELECT max_hp, atk, def, magic_resistance FROM favor "
        "WHERE char_id=? ORDER BY level DESC LIMIT 1", (char_id,)).fetchone() or (0, 0, 0, 0)
    t_max_hp, t_atk, t_def, t_res = trust

    pots = aggregate_potentials(
        cur.execute("SELECT blackboard FROM potentials WHERE char_id=?", (char_id,)).fetchall()) \
        if USE_POT else {}

    mod = pick_module(cur, char_id)
    mod_stats = mod[2] if mod else {}

    # 天赋（含模组加强）
    talents = best_talent_candidates(cur, char_id, mod[3] if mod else None)
    talent_merged = {}
    for st in talents.values():
        merge_stat(talent_merged, st)

    def compose(b, t, pot_key, mod_key, talent_key):
        flat = (b or 0) + (t or 0) + pots.get(pot_key, {}).get('flat', 0.0) \
            + mod_stats.get(mod_key, {}).get('flat', 0.0) \
            + talent_merged.get(talent_key, {}).get('flat', 0.0)
        pct = pots.get(pot_key, {}).get('pct', 0.0) \
            + mod_stats.get(mod_key, {}).get('pct', 0.0) \
            + talent_merged.get(talent_key, {}).get('pct', 0.0)
        return flat * (1 + pct)

    stats = {
        'max_hp': compose(b_max_hp, t_max_hp, 'max_hp', 'max_hp', 'max_hp'),
        'atk': compose(b_atk, t_atk, 'atk', 'atk', 'atk'),
        'def': compose(b_def, t_def, 'def', 'def', 'def'),
        'magic_resistance': compose(b_res, t_res, 'magic_resistance', 'magic_resistance', 'magic_resistance'),
        'base_attack_time': b_bat,
        'cost': (b_cost or 0) + pots.get('cost', {}).get('flat', 0.0)
        + talent_merged.get('cost', {}).get('flat', 0.0)
        + mod_stats.get('cost', {}).get('flat', 0.0),
        'block_cnt': (b_block or 0) + mod_stats.get('block_cnt', {}).get('flat', 0.0),
        'attack_speed_bonus': talent_merged.get('attack_speed', {}).get('flat', 0.0)
        + mod_stats.get('attack_speed', {}).get('flat', 0.0),
        'cost_bonus': talent_merged.get('cost', {}).get('flat', 0.0)
        + mod_stats.get('cost', {}).get('flat', 0.0)
        + pots.get('cost', {}).get('flat', 0.0),
    }
    stats['base_atk'] = b_atk
    stats['trust_atk'] = t_atk
    stats['pot_atk'] = pots.get('atk', {}).get('flat', 0.0)
    stats['mod_atk'] = mod_stats.get('atk', {}).get('flat', 0.0)
    stats['tal_atk_pct'] = talent_merged.get('atk', {}).get('pct', 0.0)
    return stats, mod, talents


def skill_scenarios(dmg_type):
    if dmg_type == 'physical':
        return SCEN_PHYS
    if dmg_type == 'arts':
        return SCEN_ARTS
    return SCEN_TRUE  # true / mixed / none


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = "AND o.name = ?" if ONLY else ""
    params = (ONLY,) if ONLY else ()
    chars = cur.execute(f"""
        SELECT char_id, name, profession, sub_profession_id, rarity
        FROM operators o
        WHERE source='character' AND profession NOT IN ('TOKEN','TRAP') {where}
        ORDER BY o.rarity DESC, o.sort_index
    """, params).fetchall()

    eval_rows, sum_rows, stat_rows = [], [], []
    n_ok = 0
    for ch in chars:
        fs = final_stats(cur, ch['char_id'])
        if fs is None:
            continue
        stats, mod, talents = fs
        mod_id, mod_name = (mod[0], mod[1]) if mod else (None, None)

        skills = cur.execute("""
            SELECT os.skill_index, sl.name, sl.description, sl.duration,
                   sl.sp_data, sl.blackboard
            FROM operator_skills os
            JOIN skill_levels sl ON sl.skill_id = os.skill_id AND sl.level_index = 9
            WHERE os.char_id = ? ORDER BY os.skill_index
        """, (ch['char_id'],)).fetchall()
        if not skills:
            continue

        chd = {'char_id': ch['char_id'], 'name': ch['name'], 'profession': ch['profession']}
        best = {'burst': 0, 'dps': 0, 'cycle': 0}
        stat_row = {
            'char_name': ch['name'], 'profession': ch['profession'],
            'sub_profession': ch['sub_profession_id'], 'rarity': ch['rarity'],
            'max_hp': int(stats['max_hp']), 'atk': round(stats['atk'], 1),
            'def': round(stats['def'], 1), 'magic_resistance': round(stats['magic_resistance'], 1),
            'base_attack_time': stats['base_attack_time'],
            'attack_speed_bonus': stats['attack_speed_bonus'],
            'cost': round(stats['cost'], 1), 'block_cnt': stats['block_cnt'],
            'module_used': mod_name,
        }
        stat_rows.append(stat_row)
        for s in skills:
            bb = A.bb_dict(s['blackboard'])
            # 治疗判定：医疗/辅助的回复型技能（heal_scale 或描述含 治疗/恢复 且非伤害）
            heal_like = ('heal_scale' in bb or 'attack@heal_scale' in bb
                         or '治疗' in s['description'] or '恢复' in s['description'])
            dmg_type = A.detect_damage_type(s['description'], ch['profession'])
            # 医疗技能：heal_scale → atk_scale（治疗量 = atk × 倍率）
            if (ch['profession'] == 'MEDIC' and 'heal_scale' in bb
                    and 'atk_scale' not in bb and 'damage_scale' not in bb):
                bb['atk_scale'] = bb.pop('heal_scale')
            # 注入天赋/模组攻速加成
            as_bonus = stats['attack_speed_bonus']
            skill_as = bb.get('attack_speed') or 0
            if as_bonus or skill_as:
                bb['attack_speed'] = skill_as + as_bonus
            elif 'attack_speed' in bb:
                del bb['attack_speed']
            sp = json.loads(s['sp_data']) if s['sp_data'] else {}
            scen = skill_scenarios(dmg_type)
            computed_any = False
            for d, r, label in scen:
                A.TARGET_DEF = d
                A.TARGET_RES = r
                rec = A.compute(chd, s, bb, sp, stats['atk'], stats['base_attack_time'])
                if rec['computed'] != 'yes':
                    break
                computed_any = True
                eff = rec.get('effective_total_damage')
                is_heal = rec['damage_type'] == 'none' and rec['mult'] is not None and heal_like
                row = {
                    'char_name': ch['name'], 'profession': ch['profession'],
                    'sub_profession': ch['sub_profession_id'], 'rarity': ch['rarity'],
                    'skill_index': rec['skill_index'], 'skill_name': rec['skill_name'],
                    'damage_type': rec['damage_type'], 'scenario': label,
                    'target_def': d, 'target_res': r,
                    'atk_final': round(stats['atk'], 1),
                    'atk_base': stats['base_atk'], 'trust_atk': stats['trust_atk'],
                    'pot_atk': stats['pot_atk'], 'mod_atk': stats['mod_atk'],
                    'tal_atk_pct': stats['tal_atk_pct'],
                    'mult': rec['mult'], 'hit_times': rec['hit_times'],
                    'max_target': rec['max_target'], 'duration': rec['duration'],
                    'interval': rec['interval'], 'attack_count': rec['attack_count'],
                    'per_hit_raw': round(rec['per_hit'], 1),
                    'per_hit_eff': round(rec['per_hit_effective'], 1) if rec['per_hit_effective'] is not None else None,
                    'burst_raw': int(rec['total_damage']),
                    'burst_eff': int(eff) if eff is not None else None,
                    'dps_eff': round(eff / rec['duration'], 1) if (eff and rec['duration']) else None,
                    'cycle_dps_eff': round(eff / rec['cycle_time'], 1) if (eff and rec.get('cycle_time')) else None,
                    'cycle_dps_avg_eff': round((eff + (rec.get('idle_damage') or 0)) / rec['cycle_time'], 1)
                    if (eff and rec.get('cycle_time')) else None,
                    'heal_total': int(rec['total_damage']) if is_heal else None,
                    'heal_dps': round(rec['total_damage'] / rec['duration'], 1) if (is_heal and rec['duration']) else None,
                    'heal_cycle_dps': round(rec['total_damage'] / rec['cycle_time'], 1)
                    if (is_heal and rec.get('cycle_time')) else None,
                    'chain_times': rec.get('chain_times'),
                    'aoe_total_raw': int(rec['aoe_total_damage']) if rec.get('aoe_total_damage') else None,
                    'elemental_total': int(rec['elemental_total']) if rec.get('elemental_total') else None,
                    'element_type': rec.get('element_type'),
                    'elemental_dps': round(rec['elemental_dps'], 1) if rec.get('elemental_dps') else None,
                    'time_to_burst': round(rec['time_to_burst'], 2) if rec.get('time_to_burst') else None,
                    'time_to_burst_boss': round(rec['time_to_burst_boss'], 2) if rec.get('time_to_burst_boss') else None,
                    'burst_trigger_dps': round(rec['burst_trigger_dps'], 1) if rec.get('burst_trigger_dps') else None,
                    'burst_extra_dps': round(rec['burst_extra_dps'], 1) if rec.get('burst_extra_dps') else None,
                    'burst_dps': round(rec['burst_dps'], 1) if rec.get('burst_dps') else None,
                    'sp_type': rec['sp_type'], 'sp_cost': rec['sp_cost'], 'init_sp': rec['init_sp'],
                    'module_used': mod_name,
                }
                eval_rows.append(row)
                if label == 'base':
                    sum_rows.append(dict(row))
                    if rec['total_damage'] and rec['total_damage'] > best['burst']:
                        best['burst'] = rec['total_damage']
                    if rec.get('active_dps') and rec['active_dps'] > best['dps']:
                        best['dps'] = rec['active_dps']
                    if rec.get('cycle_dps') and rec['cycle_dps'] > best['cycle']:
                        best['cycle'] = rec['cycle_dps']
            if computed_any:
                n_ok += 1
        if ONLY:
            print(f"■ {ch['name']} [{ch['profession']}] 最终面板: "
                  f"ATK={stats['atk']:.0f} (基础{stats['base_atk']:.0f}+信赖{stats['trust_atk']:.0f}"
                  f"+潜能{stats['pot_atk']:.0f}+模组{stats['mod_atk']:.0f}) ×(1+{stats['tal_atk_pct']*100:.0f}%)"
                  f"  攻速加成+{stats['attack_speed_bonus']:.0f}  模组: {mod_name}")

    os.makedirs(OUT, exist_ok=True)
    cols = list(eval_rows[0].keys())
    epath = os.path.join(OUT, 'operator_eval.csv')
    with open(epath, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(eval_rows)
    spath = os.path.join(OUT, 'operator_summary.csv')
    with open(spath, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(sum_rows)
    tpath = os.path.join(OUT, 'operator_stats.csv')
    with open(tpath, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(stat_rows[0].keys()))
        w.writeheader()
        w.writerows(stat_rows)
    print(f'干员数: {n_ok} | eval 行: {len(eval_rows)} | summary 行: {len(sum_rows)} | stats 行: {len(stat_rows)}')
    print('CSV:', epath)
    print('CSV:', spath)
    print('CSV:', tpath)

    # 排名概览：按职业 × 循环DPS（base 场景）
    if not ONLY:
        prof_cn = {'SNIPER': '狙击', 'CASTER': '术师', 'WARRIOR': '近卫', 'SPECIAL': '特种',
                   'PIONEER': '先锋', 'SUPPORT': '辅助', 'TANK': '重装', 'MEDIC': '医疗'}
        for prof in ('SNIPER', 'CASTER', 'WARRIOR', 'SPECIAL', 'PIONEER', 'SUPPORT', 'TANK', 'MEDIC'):
            if prof == 'MEDIC':
                pool = [r for r in sum_rows if r['rarity'] == 5 and r['heal_dps']]
                top = sorted(pool, key=lambda x: -(x['heal_dps'] or 0))[:8]
                if not top:
                    continue
                print(f'\n== 六星医疗 治疗DPS TOP8 ==')
                for r in top:
                    print(f"  {r['char_name']:<8s} {r['skill_name']:<8s} "
                          f"治疗DPS {r['heal_dps']:>8,.0f}  单次 {r['per_hit_raw']:>7,.0f}×{r['attack_count']}次")
            else:
                pool = [r for r in sum_rows if r['rarity'] == 5 and r['profession'] == prof
                        and r['cycle_dps_eff'] and r['dps_eff'] and r['burst_eff']]
                top = sorted(pool, key=lambda x: -(x['cycle_dps_eff'] or 0))[:8]
                if not top:
                    continue
                print(f'\n== 六星{prof_cn[prof]}（循环DPS TOP8）==')
                for r in top:
                    print(f"  {r['char_name']:<8s} {r['skill_name']:<8s} {r['damage_type']:<8s} "
                          f"循环 {r['cycle_dps_eff']:>7,.0f}  技能 {r['dps_eff']:>7,.0f}  爆发 {r['burst_eff']:>9,.0f}")
    conn.close()


if __name__ == '__main__':
    main()
