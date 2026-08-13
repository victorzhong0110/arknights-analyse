#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导入模组各阶段数值 → module_levels 表。

数据源：data/raw/excel/battle_equip_table.json
（模组阶段数据所在文件：phases[].attributeBlackboard = 属性加成，
 phases[].parts[].overrideTraitDataBundle / addOrOverrideTalentDataBundle = 特性/天赋加强）

用法：python3 scripts/import_module_levels.py
      AK_DB_PATH=/tmp/x.db python3 scripts/import_module_levels.py
"""
import json
import os
import re
import sqlite3

BASE = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'excel'))
DEFAULT_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'db', 'arknights.db'))
DB_PATH = os.environ.get('AK_DB_PATH', DEFAULT_DB)

STAT_COLS = ('max_hp', 'atk', 'def', 'magic_resistance', 'attack_speed',
             'cost', 'respawn_time', 'block_cnt', 'move_speed')

SCHEMA = """
CREATE TABLE IF NOT EXISTS module_levels (
    equip_id TEXT, equip_level INTEGER,
    max_hp REAL, atk REAL, def REAL, magic_resistance REAL,
    attack_speed REAL, cost REAL, respawn_time REAL, block_cnt REAL,
    move_speed REAL,
    other_stats TEXT,
    trait_desc TEXT,
    talent_desc TEXT,
    talent_upgrades TEXT,
    raw_json TEXT,
    PRIMARY KEY (equip_id, equip_level)
);
CREATE INDEX IF NOT EXISTS idx_module_levels_equip ON module_levels(equip_id);
"""

PLACEHOLDER_RE = re.compile(r'\{(\w+):([^}]*)\}')


def jstr(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def fmt_placeholder(key, fmt, bb):
    """把 {key:fmt} 占位符替换为黑板真实值。fmt 形如 '0%' / '0' / '0.0'。"""
    val = None
    for kv in (bb or []):
        if kv.get('key') == key:
            val = kv.get('value')
            break
    if val is None:
        return fmt
    pct = fmt.rstrip('%').endswith('%') or '%' in fmt
    if pct:
        return f'{val * 100:g}%'
    if isinstance(val, float) and val == int(val):
        return str(int(val))
    return f'{val:g}'


def fill_placeholders(text, bb):
    if not text:
        return text
    return PLACEHOLDER_RE.sub(lambda m: fmt_placeholder(m.group(1), m.group(2), bb), text)


def cand_entries(bundle, kind):
    """kind: 'trait' | 'talent' — 提取候选（描述 + 黑板），并做占位符代入。"""
    cands = (bundle or {}).get('candidates') or []
    out = []
    for c in cands:
        bb = c.get('blackboard') or []
        desc = c.get('additionalDescription') or c.get('overrideDescripton') \
            or c.get('upgradeDescription') or c.get('description')
        entry = {
            'kind': kind,
            'talent_index': c.get('talentIndex'),
            'name': c.get('name'),
            'description': fill_placeholders(desc, bb),
            'blackboard': bb,
            'required_potential_rank': c.get('requiredPotentialRank'),
        }
        out.append(entry)
    return out


def main():
    with open(os.path.join(BASE, 'battle_equip_table.json'), encoding='utf-8') as f:
        data = json.load(f)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute('DROP TABLE IF EXISTS module_levels')
    cur.executescript(SCHEMA)

    rows = []
    n_equip = 0
    for eid, e in data.items():
        n_equip += 1
        for ph in e.get('phases') or []:
            lv = ph.get('equipLevel')
            stats = {}
            for ab in ph.get('attributeBlackboard') or []:
                k = ab.get('key')
                v = ab.get('value')
                if k in STAT_COLS:
                    stats[k] = v
                else:
                    stats.setdefault('_other', []).append({'key': k, 'value': v})
            trait_descs, talent_descs, talent_upgrades = [], [], []
            for p in ph.get('parts') or []:
                for ent in cand_entries(p.get('overrideTraitDataBundle'), 'trait'):
                    if ent['description']:
                        trait_descs.append(ent['description'])
                for ent in cand_entries(p.get('addOrOverrideTalentDataBundle'), 'talent'):
                    if ent['description']:
                        talent_descs.append(ent['description'])
                    talent_upgrades.append(ent)
            rows.append((
                eid, lv,
                stats.get('max_hp'), stats.get('atk'), stats.get('def'),
                stats.get('magic_resistance'), stats.get('attack_speed'),
                stats.get('cost'), stats.get('respawn_time'), stats.get('block_cnt'),
                stats.get('move_speed'),
                jstr(stats.get('_other')),
                ' | '.join(trait_descs) if trait_descs else None,
                ' | '.join(talent_descs) if talent_descs else None,
                jstr(talent_upgrades) if talent_upgrades else None,
                jstr(ph),
            ))
    cur.executemany(
        'INSERT OR REPLACE INTO module_levels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    conn.commit()
    n = cur.execute('SELECT COUNT(*) FROM module_levels').fetchone()[0]
    n3 = cur.execute('SELECT COUNT(*) FROM module_levels WHERE equip_level=3').fetchone()[0]
    print(f'模块数: {n_equip} | module_levels 行数: {n} (3 阶段: {n3})')
    print('样例 (能天使):')
    for r in cur.execute(
            "SELECT equip_id, equip_level, atk, max_hp, trait_desc, talent_desc "
            "FROM module_levels WHERE equip_id LIKE 'uniequip_00%_angel' ORDER BY equip_id, equip_level"):
        print('  ', r)
    conn.close()


if __name__ == '__main__':
    main()
