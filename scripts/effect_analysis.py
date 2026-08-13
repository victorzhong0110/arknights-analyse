#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
辅助效果 & 生存能力分析。

辅助效果（来自技能M3/天赋描述+黑板）：
  - 脆弱/增伤（目标受到伤害提升）
  - 减防（防御下降，百分比或固定值）
  - 减速/停顿/冻结/晕眩/沉睡/麻痹/浮空（控制时长）
  - 友方攻击力/攻速增益
  - 攻击范围大小（ranges.grids 格数）

生存能力：
  - 有效生命 EHP：面对基准 ATK 攻击能承受的命中数（物理=HP/max(ATK-DEF, 5%)，法术=HP/(ATK×(1-法抗%))）
  - 免疫特质（operator_levels 免疫字段）、隐匿/无敌/复活/浮空等描述

输出：data/analysis/support_ranking.csv, survival_ranking.csv
用法：python3 scripts/effect_analysis.py
      AK_DB_PATH=... AK_OUT=... python3 scripts/effect_analysis.py
"""
import csv
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import analyze_skills as A  # noqa: E402
import evaluate_operators as E  # noqa: E402

DB = E.DB
OUT = E.OUT

BENCH_ATK = 2000.0  # 基准敌人攻击力（合约 BOSS 级）


def parse_pct(text, default=None):
    m = re.search(r'(\d+(?:\.\d+)?)%', text)
    return float(m.group(1)) / 100.0 if m else default


def range_size(cur, char_id):
    row = cur.execute(
        "SELECT range_id FROM operator_phases WHERE char_id=? AND phase=2 LIMIT 1",
        (char_id,)).fetchone()
    if not row or not row[0]:
        return None
    r = cur.execute("SELECT grids FROM ranges WHERE range_id=?", (row[0],)).fetchone()
    if not r or not r[0]:
        return None
    try:
        return len(json.loads(r[0]))
    except Exception:
        return None


PLACEHOLDER_RE = re.compile(r'\{(\w+):([^}]*)\}')


def fill(text, bb):
    """把 {key:fmt} 占位符代入黑板真实值（百分比转百分号形式）。"""
    def rep(m):
        k, fmt = m.group(1), m.group(2)
        v = (bb or {}).get(k)
        if v is None:
            return fmt
        if '%' in fmt:
            return f'{v * 100:g}%'
        return f'{v:g}'
    return PLACEHOLDER_RE.sub(rep, text or '')


def clean(desc, bb):
    """清洗描述：占位符代入真实值 + 剥离所有 <...> 标记。"""
    d = fill(desc, bb)
    return re.sub(r'<[^>]*>', '', d)


def extract_support(cur, char_id, name, profession, caster_atk=None):
    """返回 (effects dict, 说明字符串)"""
    fx = {'fragile': 0.0, 'def_shred_pct': 0.0, 'def_shred_flat': 0.0,
          'slow': 0.0, 'control_dur': 0.0, 'ally_atk_pct': 0.0,
          'ally_as': 0.0, 'enemy_atk': 0.0, 'inspire_pct': 0.0,
          'inspire_atk': caster_atk or 0.0, 'stun': 0.0, 'bind': 0.0,
          'frozen': 0.0, 'sleep': 0.0, 'palsy': 0.0, 'float': 0.0, 'talent': 0.0}
    notes = []
    rows = cur.execute(
        "SELECT name, description, blackboard FROM skill_levels "
        "WHERE skill_id IN (SELECT skill_id FROM operator_skills WHERE char_id=?) "
        "AND level_index=9", (char_id,)).fetchall()
    trows = cur.execute(
        "SELECT name, description FROM talents WHERE char_id=?", (char_id,)).fetchall()

    for name_, desc, bb in rows + [(t[0], t[1], None) for t in trows]:
        desc = clean(desc or '', A.bb_dict(bb) if bb else {})
        if '脆弱' in desc:
            m = re.search(r'受到.{0,2}伤害(?:提升|增加|提高)[^。]*?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['fragile'] = max(fx['fragile'], float(m.group(1)) / 100.0)
                notes.append(f'脆弱+{m.group(1)}%')
            else:
                m = re.search(r'(\d+(?:\.\d+)?)%[^。]{0,8}脆弱|脆弱[^。]{0,8}?(\d+(?:\.\d+)?)%', desc)
                if m:
                    v = float(m.group(1) or m.group(2)) / 100.0
                    fx['fragile'] = max(fx['fragile'], v)
                    notes.append(f'脆弱+{v*100:.0f}%')
        if '防御力下降' in desc:
            m = re.search(r'防御(?:力)?下降[^。]*?(\d+(?:\.\d+)?)%', desc)
            if m and ('敌人' in desc or '目标' in desc or '敌方' in desc):
                fx['def_shred_pct'] = max(fx['def_shred_pct'], float(m.group(1)) / 100.0)
                notes.append(f'减防{m.group(1)}%')
        for kw, key in (('晕眩', 'stun'), ('眩晕', 'stun'), ('停顿', 'bind'),
                        ('冻结', 'frozen'), ('沉睡', 'sleep'), ('麻痹', 'palsy'),
                        ('浮空', 'float')):
            if kw in desc:
                m = re.search(rf'{kw}[^。]*?(\d+(?:\.\d+)?)秒', desc)
                if m:
                    dur = float(m.group(1))
                    if dur > fx[key]:
                        fx['control_dur'] += (dur - fx[key]) * 0.5
                        fx[key] = dur
                        notes.append(f'{kw}{m.group(1)}s')
        if ('移动速度' in desc and ('下降' in desc or '降低' in desc)) or '减速' in desc:
            m = re.search(r'移动速度[^。]*?(\d+(?:\.\d+)?)%', desc)
            if m:
                fx['slow'] = max(fx['slow'], float(m.group(1)) / 100.0)
                notes.append(f'减速{m.group(1)}%')
        # 友方增益（精确句式）：
        # 1) 鼓舞：友方单位获得…相当于施法者X%攻击力的鼓舞（平值加成 = 施法者ATK×X%）
        if '鼓舞' in desc and ('获得' in desc or '友方单位' in desc):
            v = (A.bb_dict(bb) if bb else {}).get('atk')
            if isinstance(v, (int, float)) and 0 < v <= 5:
                fx['inspire_pct'] = max(fx['inspire_pct'], v)
                notes.append(f'鼓舞(施法者攻击力+{v*100:.0f}%)')
            else:
                m = re.search(r'鼓舞.{0,8}?(\d+(?:\.\d+)?)%', desc)
                if m:
                    fx['inspire_pct'] = max(fx['inspire_pct'], float(m.group(1)) / 100.0)
                    notes.append(f'鼓舞(施法者攻击力+{m.group(1)}%)')
        # 2) 直接：我方单位…攻击力+X%（同句且无标点隔断，紧邻处无"自身"）
        m = re.search(r'我方单位[^，。]{0,12}攻击力[^，。]{0,6}?[+提升增加提高][^，。]{0,4}?(\d+(?:\.\d+)?)%', desc)
        if m and '自身' not in desc[max(0, desc.find('攻击力') - 8):desc.find('攻击力')]:
            v = float(m.group(1)) / 100.0
            fx['ally_atk_pct'] = max(fx['ally_atk_pct'], v)
            notes.append(f'友方攻击+{m.group(1)}%')
        m = re.search(r'我方单位[^，。]{0,12}攻击速度[^，。]{0,6}?[+提升增加提高][^，。]{0,4}?(\d+)', desc)
        if m:
            fx['ally_as'] = max(fx['ally_as'], float(m.group(1)))
            notes.append(f'友方攻速+{m.group(1)}')
    # 黑板直接数值（减防/减速/减攻）——"敌人…防御力-" 句式 或 attack@ 前缀，排除自身减益
    for name_, desc, bb in rows:
        if not bb:
            continue
        desc = desc or ''
        for kv in json.loads(bb):
            k, v = kv.get('key'), kv.get('value')
            if not isinstance(v, (int, float)):
                continue
            enemy_debuff = re.search(r'敌人[^。]{0,18}防御(?:力)?\s*[-−]', desc) is not None
            if k == 'def' and v < 0 and (k.startswith('attack@') or enemy_debuff):
                if abs(v) < 1:
                    fx['def_shred_pct'] = max(fx['def_shred_pct'], abs(v))
                else:
                    fx['def_shred_flat'] = max(fx['def_shred_flat'], abs(v))
            if k == 'atk' and v < 0 and re.search(r'敌人[^。]{0,18}攻击(?:力)?\s*[-−]', desc):
                fx['enemy_atk'] = max(fx['enemy_atk'], abs(v))
            if k == 'move_speed' and v < 0:
                fx['slow'] = max(fx['slow'], abs(v))
    return fx, '; '.join(notes)


def survival_score(cur, stats):
    hp, df, res = stats['max_hp'], stats['def'], stats['magic_resistance']
    n_phys = hp / max(BENCH_ATK - df, BENCH_ATK * 0.05)
    n_arts = hp / (BENCH_ATK * max(1 - res / 100, 0.05))
    ehp = (n_phys + n_arts) / 2
    # 免疫特质
    row = cur.execute(
        "SELECT stun_immune, silence_immune, sleep_immune, frozen_immune, levitate_immune, "
        "disarmed_combat_immune, feared_immune, palsy_immune, attract_immune, teleport_immune, "
        "ground_bound_immune, block_cnt FROM operator_levels "
        "WHERE char_id=? AND phase=2 ORDER BY level DESC LIMIT 1",
        (stats['_char_id'],)).fetchone()
    traits = 0
    notes = []
    if row:
        for i, name in enumerate(('晕眩免疫', '沉默免疫', '沉睡免疫', '冻结免疫', '浮空免疫',
                                  '缴械免疫', '恐惧免疫', '麻痹免疫', '吸引免疫', '传送免疫', '束缚免疫')):
            if row[i]:
                traits += 1
                notes.append(name)
    # 特殊描述
    desc_rows = cur.execute(
        "SELECT description FROM talents WHERE char_id=? UNION ALL "
        "SELECT description FROM skill_levels WHERE skill_id IN "
        "(SELECT skill_id FROM operator_skills WHERE char_id=?) AND level_index=9",
        (stats['_char_id'], stats['_char_id'])).fetchall()
    special = 0
    for (d,) in desc_rows:
        d = d or ''
        if '隐匿' in d:
            special += 2
            notes.append('隐匿')
        if '无敌' in d:
            special += 3
            notes.append('无敌')
        if '复活' in d:
            special += 3
            notes.append('复活')
        if '不可选中' in d:
            special += 2
            notes.append('不可选中')
        if '浮空' in d and '免疫' not in d:
            special += 1
            notes.append('浮空')
    return {
        'n_phys': round(n_phys, 1), 'n_arts': round(n_arts, 1),
        'ehp': round(ehp, 1), 'trait_pts': traits + special,
        'notes': '; '.join(notes),
    }


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    chars = cur.execute(
        "SELECT char_id, name, profession, sub_profession_id, rarity FROM operators "
        "WHERE source='character' AND profession NOT IN ('TOKEN','TRAP') "
        "ORDER BY rarity DESC").fetchall()

    sup_rows, surv_rows = [], []
    for ch in chars:
        fs = E.final_stats(cur, ch['char_id'])
        if fs is None:
            continue
        stats, mod, _ = fs
        stats['_char_id'] = ch['char_id']
        fx, notes = extract_support(cur, ch['char_id'], ch['name'], ch['profession'], stats['atk'])
        rs = range_size(cur, ch['char_id'])
        sv = survival_score(cur, stats)
        common = {
            'char_name': ch['name'], 'profession': ch['profession'],
            'sub_profession': ch['sub_profession_id'], 'rarity': ch['rarity'],
            'atk': round(stats['atk'], 1), 'max_hp': int(stats['max_hp']),
            'def': round(stats['def'], 1), 'magic_resistance': round(stats['magic_resistance'], 1),
            'range_size': rs,
        }
        sup_rows.append({
            **common,
            'fragile': fx['fragile'], 'def_shred_pct': fx['def_shred_pct'],
            'def_shred_flat': fx['def_shred_flat'], 'slow': fx['slow'],
            'ally_atk_pct': fx['ally_atk_pct'], 'ally_as': fx['ally_as'],
            'enemy_atk': fx['enemy_atk'],
            'inspire_pct': fx['inspire_pct'], 'inspire_atk': round(fx['inspire_atk'], 1),
            'control_dur': round(fx['control_dur'], 1),
            'support_score': round(fx['fragile'] * 100 + fx['def_shred_pct'] * 60
                                   + fx['slow'] * 40 + fx['ally_atk_pct'] * 50
                                   + fx['ally_as'] * 0.5 + fx['control_dur']
                                   + fx['enemy_atk'] * 40
                                   + fx['inspire_pct'] * fx['inspire_atk'] * 0.08, 1),
            'effects': notes,
        })
        surv_rows.append({
            **common,
            'n_phys_hits': sv['n_phys'], 'n_arts_hits': sv['n_arts'],
            'survival_score': round(sv['ehp'] * 100 + sv['trait_pts'] * 5, 1),
            'trait_pts': sv['trait_pts'], 'traits': sv['notes'],
        })

    os.makedirs(OUT, exist_ok=True)
    spath = os.path.join(OUT, 'support_ranking.csv')
    with open(spath, 'w', newline='', encoding='utf-8-sig') as f:
        cols = ['char_name', 'profession', 'sub_profession', 'rarity', 'atk', 'max_hp',
                'def', 'magic_resistance', 'range_size', 'fragile', 'def_shred_pct',
                'def_shred_flat', 'slow', 'ally_atk_pct', 'ally_as', 'control_dur',
                'inspire_pct', 'inspire_atk',
                'support_score', 'effects']
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(sup_rows)
    vpath = os.path.join(OUT, 'survival_ranking.csv')
    with open(vpath, 'w', newline='', encoding='utf-8-sig') as f:
        cols = ['char_name', 'profession', 'sub_profession', 'rarity', 'atk', 'max_hp',
                'def', 'magic_resistance', 'range_size', 'n_phys_hits', 'n_arts_hits',
                'survival_score', 'trait_pts', 'traits']
        w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
        w.writeheader()
        w.writerows(surv_rows)
    print(f'干员: {len(sup_rows)} | 辅助榜: {spath} | 生存榜: {vpath}')

    print('\n== 辅助效果 TOP12（六星）==')
    for r in sorted([x for x in sup_rows if x['rarity'] == 5],
                    key=lambda x: -x['support_score'])[:12]:
        print(f"  {r['char_name']:<6s} {r['profession']:<7s} 范围{r['range_size'] or '-':<4} "
              f"评分{r['support_score']:>6.1f}  {r['effects'][:44]}")
    print('\n== 生存能力 TOP10（六星）==')
    for r in sorted([x for x in surv_rows if x['rarity'] == 5],
                    key=lambda x: -x['survival_score'])[:10]:
        print(f"  {r['char_name']:<6s} {r['profession']:<7s} EHP{r['survival_score']:>8.1f} "
              f"物抗{r['n_phys_hits']:>5.1f}击 法抗{r['n_arts_hits']:>5.1f}击 特质{r['trait_pts']:>2d} {r['traits'][:36]}")
    conn.close()


if __name__ == '__main__':
    main()
