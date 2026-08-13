#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
六星干员技能理论总伤 / DPS 计算（纸面数据）。

口径（假设，非官方）：
- E2 满级基础攻击力，不含信赖/潜能；目标 0 防 0 抗；专精三(M3, level_index=9)
- 单次命中 = ATK × 攻击倍率
  倍率来源优先级: attack@atk_scale > atk_scale > damage_scale
  若无上述键但存在 atk(0<atk<=5) 视为 +atk% → 倍率 = 1 + atk
- 每次攻击命中数（连击）: attack@times 优先；times 仅在描述含「连射/连击」语义时使用
- 攻击间隔优先级:
  attack_speed 键        → bat × 100/(100+as)
  base_attack_time 修正  → bat + 修正
  攻击型键存在            → bat（普攻间隔）
  interval 键+描述含持续攻击 → 该 interval
  否则视为单发(次数=1)
- 攻击次数: 有 duration 且有间隔 → floor(dur/间隔)；否则 1（或 cnt）
- 总伤 = 单次命中 × 连击 × 攻击次数；技能DPS = 总伤/duration（有则算）
- 循环DPS = 总伤/((spCost-initSp)+duration)，仅自动回复(spType=1)
用法:
  AK_CHAR=<干员名> python3 analyze_skills.py   # 单干员详细输出（默认 能天使）
  python3 analyze_skills.py                    # 全量输出 CSV/JSON 到 AK_OUT
"""
import csv
import json
import math
import os
import re
import sqlite3

DB = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'db', 'arknights.db'))
OUT_DIR = os.environ.get('AK_OUT', '/tmp/ak_out')
CHAR_NAME = os.environ.get('AK_CHAR')  # None → 全量
TARGET_DEF = float(os.environ.get('AK_DEF', '0'))   # 目标物理防御
TARGET_RES = float(os.environ.get('AK_RES', '0'))   # 目标法术抗性

MULT_KEYS = ('attack@atk_scale', 'atk_scale', 'damage_scale', 'attack@damage_scale')
COMBO_HINTS = ('连射', '连击', '{times}次', '{times}连', '发射{times}枚', '{attack@times}连', '{attack@times}次')

# 元素损伤（爆条）模型参数：标准阈值 1000 元素损伤值；爆条状态持续约 10 秒
BURST_THRESHOLD = 1000.0
BURST_STATE_DUR = 10.0

# 职业默认伤害类型：无法从描述判断时的兜底
PROF_DEFAULT_TYPE = {
    'PIONEER': 'physical', 'WARRIOR': 'physical', 'SNIPER': 'physical',
    'TANK': 'physical', 'SPECIAL': 'physical',
    'CASTER': 'arts', 'SUPPORT': 'arts', 'MEDIC': 'none',
}


def detect_damage_type(desc, profession):
    """从技能描述识别伤害类型: physical / arts / true / mixed / none"""
    has_phys = '物理伤害' in desc or '物理溅射伤害' in desc
    has_arts = '法术伤害' in desc or '法术溅射伤害' in desc
    has_true = ('真实伤害' in desc or '造成真实' in desc
                or re.search(r'伤害类型[^。\n]{0,14}真实', desc) is not None)
    if has_true and (has_phys or has_arts):
        return 'mixed'
    if has_true:
        return 'true'
    if has_phys and has_arts:
        return 'mixed'
    if has_phys:
        return 'physical'
    if has_arts:
        return 'arts'
    return PROF_DEFAULT_TYPE.get(profession, 'none')


def bb_dict(blackboard):
    if not blackboard:
        return {}
    return {kv['key']: kv.get('value') for kv in json.loads(blackboard)}


def get_num(d, *keys):
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return None


def find_key(d, pattern, prefer=None):
    """按正则找第一个数值键；prefer 精确键优先。"""
    if prefer and prefer in d and isinstance(d[prefer], (int, float)):
        return prefer
    for k in sorted(d):
        if pattern.search(k) and isinstance(d[k], (int, float)):
            return k
    return None


CN_NUM = {'两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def compute(ch, s, bb, sp, atk, bat):
    desc = s['description'] or ''
    rec = {
        'char_name': ch['name'], 'profession': ch['profession'],
        'skill_index': s['skill_index'], 'skill_name': s['name'],
        'atk_base': atk, 'base_attack_time': bat,
        'computed': 'yes', 'reason': '',
    }

    # 攻击倍率：atk 攻击力加成 与 atk_scale 键 相乘（如 爆裂黎明 +180% × 2.2）
    atk_buff = bb.get('atk')
    buff_mult = 1 + atk_buff if (atk_buff is not None and 0 < atk_buff <= 5) else 1.0
    mult = get_num(bb, *MULT_KEYS)
    if mult is None:
        # 变体倍率：仅取 attack@ 前缀、且非辅助倍率键（attack@s3_atk_scale / attack@atk_scale_3）
        AUX = ('min_', 'max_', 'splash_', 'extra_', 'proj_', 'chain.', 'bounce_',
               'main_', 'fin_', 'ex_', 'base_', 'element_', 'ep_', 'burn_',
               'cannon_', 'append_')
        for k in sorted(bb):
            if (k.startswith('attack@') and ('atk_scale' in k or 'damage_scale' in k)
                    and not any(a in k for a in AUX) and isinstance(bb[k], (int, float))):
                mult = bb[k]
                break
    if mult is not None:
        mult = mult * buff_mult
    elif buff_mult != 1.0:
        mult = buff_mult
    rec['mult'] = mult

    # 伤害类型
    dmg_type = detect_damage_type(desc, ch['profession'])
    rec['damage_type'] = dmg_type

    # 连击数（每次攻击命中次数）
    hit_times = 1
    if 'attack@times' in bb and bb['attack@times']:
        hit_times = bb['attack@times']
    elif 'times' in bb and bb['times'] and any(w in desc for w in COMBO_HINTS):
        hit_times = bb['times']
    else:
        k = find_key(bb, re.compile(r'max_hit_num|hit_num'))
        if k:
            hit_times = bb[k]
        else:
            m = re.search(r'([两三四五六七八九十\d]+)次伤害', desc)
            if m:
                t = m.group(1)
                hit_times = CN_NUM.get(t, int(t) if t.isdigit() else 1)
    # 弹跳链（attack@chain.atk_scale + chain_times）：主击打主目标，链击弹跳至其他敌人
    chain_scale = bb.get('attack@chain.atk_scale')
    chain_times = int(bb.get('chain_times') or 0)
    has_chain = chain_scale is not None and chain_times > 0
    if has_chain:
        rec['chain_times'] = chain_times
        rec['chain_scale'] = chain_scale
        hit_times = 1  # 有链时 attack@times 是总命中数（主1+链N），主目标命中视为 1
    rec['hit_times'] = hit_times

    # 目标数
    rec['max_target'] = get_num(bb, 'attack@max_target', 'max_target') or 1

    # 持续时间
    dur = None
    if s['duration'] and s['duration'] > 0:
        dur = s['duration']
    elif bb.get('duration') and bb['duration'] > 0:
        dur = bb['duration']
    rec['duration'] = dur

    # 攻击间隔与次数
    interval = None
    attack_count = None
    attack_speed = bb.get('attack_speed')
    bat_mod = bb.get('base_attack_time')
    interval_bb = bb.get('interval')
    cnt = bb.get('cnt')
    is_attack_type = any(k in bb for k in ('attack@atk_scale', 'attack@times', 'attack@max_target'))

    if dur is not None and dur > 0:
        if attack_speed:
            # 攻速加成与 base_attack_time 修正叠加：interval = (bat + bat_mod) * 100/(100+as)
            base_int = (bat + bat_mod) if bat_mod is not None else bat
            interval = base_int * 100.0 / (100.0 + attack_speed)
            if interval <= 0:
                interval = 0.1
        elif bat_mod is not None:
            interval = bat + bat_mod
            if interval <= 0:
                interval = 0.1
        elif is_attack_type:
            interval = bat
        elif interval_bb and interval_bb > 0 and any(w in desc for w in ('攻击间隔', '每秒', '持续造成', '不断')):
            interval = interval_bb
        attack_count = math.floor(dur / interval) if interval else 1
    else:
        # 弹药型技能：attack@*trigger_time = 弹药数（攻击次数）
        tk = find_key(bb, re.compile(r'trigger_time'), prefer='attack@trigger_time')
        if tk:
            ammo = int(bb[tk])
            m = re.search(r'消耗(\d+)发', desc)
            ammo_per_attack = int(m.group(1)) if m else 1
            attack_count = max(int(ammo / ammo_per_attack), 1)
            # 实际持续时间 ≈ 攻击次数 × 攻击间隔（供技能DPS/循环DPS使用）
            if attack_speed:
                base_int = (bat + bat_mod) if bat_mod is not None else bat
                interval = base_int * 100.0 / (100.0 + attack_speed)
            elif bat_mod is not None:
                interval = bat + bat_mod
            elif is_attack_type:
                interval = bat
            if interval and interval > 0:
                dur = attack_count * interval
                rec['duration'] = dur
        else:
            attack_count = int(cnt) if cnt else 1
    rec['interval'] = interval
    rec['attack_count'] = attack_count

    if mult is None or attack_count is None:
        rec['computed'] = 'no'
        rec['reason'] = '无攻击倍率(辅助/特殊型)' if mult is None else '无法确定攻击次数'
        return rec

    per_hit = atk * mult
    per_attack = per_hit * hit_times
    total = per_attack * attack_count

    # 有效伤害（考虑目标防御/法抗）
    if dmg_type == 'physical':
        eff_hit = max(per_hit - TARGET_DEF, per_hit * 0.05)  # 物理保底 5%
    elif dmg_type == 'arts':
        eff_hit = per_hit * max(1 - TARGET_RES / 100, 0)     # 法术抗性
    elif dmg_type == 'true':
        eff_hit = per_hit                                    # 真伤无视防御
    else:
        eff_hit = None                                       # 混合/未知，不折算
    rec['per_hit_effective'] = eff_hit
    rec['effective_total_damage'] = eff_hit * hit_times * attack_count if eff_hit is not None else None

    rec['per_hit'] = per_hit
    rec['per_attack'] = per_attack
    rec['total_damage'] = total
    if has_chain:
        # 链击总伤（弹跳至其他目标）
        rec['aoe_total_damage'] = total + atk * chain_scale * chain_times * attack_count
    elif rec['max_target'] and rec['max_target'] > 1:
        rec['aoe_total_damage'] = total * rec['max_target']

    # 元素损伤（爆条）模型
    ep_ratio = bb.get('attack@ep_damage_ratio') or bb.get('ep_damage_ratio')
    el_scale = bb.get('attack@element_atk_scale') or bb.get('element_atk_scale')
    if ep_ratio:
        hits_total = attack_count * hit_times
        if has_chain:
            hits_total += attack_count * chain_times
        active = dur if (dur and dur > 0) else (attack_count * interval if interval else None)
        rec['elemental_total'] = atk * ep_ratio * hits_total
        if active and active > 0:
            elemental_dps = atk * ep_ratio * hits_total / active
            rec['elemental_dps'] = elemental_dps
            t_burst = BURST_THRESHOLD / elemental_dps if elemental_dps > 0 else None
            rec['time_to_burst'] = t_burst
            if el_scale and t_burst:
                extra_dps = atk * el_scale * hits_total / active  # 爆条期间每秒额外元素伤害
                cycle_extra = extra_dps * BURST_STATE_DUR / (t_burst + BURST_STATE_DUR)
                rec['burst_extra_dps'] = cycle_extra
                rec['burst_dps'] = elemental_dps + cycle_extra

    if dur and dur > 0:
        rec['active_dps'] = total / dur

    sp_type = sp.get('spType')
    rec['sp_type'] = sp_type
    rec['sp_cost'] = sp.get('spCost')
    rec['init_sp'] = sp.get('initSp')
    if sp_type == 1 and sp.get('spCost'):
        charge = max(sp['spCost'] - (sp.get('initSp') or 0), 0)
        cycle_time = charge + (dur if (dur and dur > 0) else 0)
        if cycle_time > 0:
            rec['cycle_time'] = cycle_time
            rec['cycle_dps'] = total / cycle_time
    return rec


def fmt(r, key):
    v = r.get(key)
    if not isinstance(v, (int, float)):
        return '-'
    if abs(v) < 100:
        return f'{v:g}'
    return f'{v:,.0f}'


def print_detail(r):
    print(f"■ {r['char_name']} S{r['skill_index']+1}「{r['skill_name']}」")
    if r['computed'] == 'no':
        print(f"   未计算: {r['reason']}")
        return
    type_cn = {'physical': '物理', 'arts': '法术', 'true': '真伤', 'mixed': '混合', 'none': '未知'}.get(r['damage_type'], '?')
    print(f"   伤害类型={type_cn}  倍率={r['mult']:g}  连击={r['hit_times']:g}  目标数={r['max_target']:g}"
          f"  持续={fmt(r, 'duration')}s  间隔={fmt(r, 'interval')}s  攻击次数={fmt(r, 'attack_count')}")
    print(f"   单次命中={fmt(r, 'per_hit')}" +
          (f"  (对 防御{TARGET_DEF:g}/法抗{TARGET_RES:g} → {fmt(r, 'per_hit_effective')})" if r.get('per_hit_effective') is not None else ''))
    print(f"   单目标总伤={fmt(r, 'total_damage')}" +
          (f"  (对 防御{TARGET_DEF:g}/法抗{TARGET_RES:g} → {fmt(r, 'effective_total_damage')})" if r.get('effective_total_damage') is not None else '')
          + (f"  满目标 {fmt(r, 'aoe_total_damage')}" if r.get('aoe_total_damage') else ''))
    if r.get('active_dps'):
        print(f"   技能DPS={fmt(r, 'active_dps')}")
    if r.get('cycle_dps'):
        print(f"   循环DPS={fmt(r, 'cycle_dps')}  (SP{r['sp_cost']}/初{r['init_sp']})")


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where = "AND o.name = ?" if CHAR_NAME else ""
    params = (CHAR_NAME,) if CHAR_NAME else ()
    chars = cur.execute(f"""
        SELECT char_id, name, profession FROM operators o
        WHERE rarity = 5 AND source = 'character'
          AND profession NOT IN ('TOKEN', 'TRAP') {where}
        ORDER BY o.sort_index
    """, params).fetchall()

    results = []
    for ch in chars:
        base = cur.execute("""
            SELECT atk, base_attack_time FROM operator_levels
            WHERE char_id = ? AND phase = 2 ORDER BY level DESC LIMIT 1
        """, (ch['char_id'],)).fetchone()
        if not base:
            continue
        skills = cur.execute("""
            SELECT os.skill_index, sl.name, sl.skill_type, sl.duration_type,
                   sl.description, sl.duration, sl.sp_data, sl.blackboard
            FROM operator_skills os
            JOIN skill_levels sl ON sl.skill_id = os.skill_id AND sl.level_index = 9
            WHERE os.char_id = ? ORDER BY os.skill_index
        """, (ch['char_id'],)).fetchall()
        for s in skills:
            bb = bb_dict(s['blackboard'])
            sp = json.loads(s['sp_data']) if s['sp_data'] else {}
            r = compute(ch, s, bb, sp, base['atk'], base['base_attack_time'])
            results.append(r)

    if CHAR_NAME:
        for r in results:
            print_detail(r)
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    cols = ['char_name', 'profession', 'skill_index', 'skill_name', 'computed', 'reason',
            'damage_type', 'atk_base', 'mult', 'hit_times', 'max_target', 'duration', 'interval',
            'attack_count', 'per_hit', 'per_attack', 'total_damage', 'aoe_total_damage',
            'per_hit_effective', 'effective_total_damage',
            'active_dps', 'cycle_dps', 'sp_type', 'sp_cost', 'init_sp']
    csv_path = os.path.join(OUT_DIR, 'skill_damage.csv')
    with open(csv_path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k) for k in cols})
    with open(os.path.join(OUT_DIR, 'skill_damage.json'), 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    ok = [r for r in results if r['computed'] == 'yes']
    print(f'技能总数: {len(results)} | 可计算: {len(ok)} | 未计算: {len(results)-len(ok)}')
    print(f'目标参数: 防御={TARGET_DEF:g}  法抗={TARGET_RES:g}')
    print()
    print('== 单目标总伤 TOP 15 ==')
    for r in sorted(ok, key=lambda x: -(x['total_damage'] or 0))[:15]:
        aoe = f" (满目标 {fmt(r,'aoe_total_damage')})" if r.get('aoe_total_damage') else ''
        print(f"  {r['char_name']} S{r['skill_index']+1} {r['skill_name']:<10s} 总伤 {fmt(r,'total_damage')}{aoe}")
    print()
    print('== 技能内 DPS TOP 15 ==')
    for r in sorted(ok, key=lambda x: -(x['active_dps'] or 0))[:15]:
        print(f"  {r['char_name']} S{r['skill_index']+1} {r['skill_name']:<10s} 技能DPS {fmt(r,'active_dps')}")
    print()
    print('== 循环 DPS TOP 15（自动回复）==')
    cyc = [r for r in ok if r['cycle_dps']]
    for r in sorted(cyc, key=lambda x: -(x['cycle_dps'] or 0))[:15]:
        print(f"  {r['char_name']} S{r['skill_index']+1} {r['skill_name']:<10s} 循环DPS {fmt(r,'cycle_dps')}  技能DPS {fmt(r,'active_dps')}")
    print('\nCSV:', csv_path)


if __name__ == '__main__':
    main()
