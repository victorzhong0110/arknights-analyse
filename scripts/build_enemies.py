#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
敌人 / 危机合约 数据库构建脚本。

数据源：
- enemy_handbook_table.json  敌人图鉴（名字/种族/技能描述/伤害类型/等级）
- crisis_table.json           旧版合约（赛季列表）
- crisis_v2_table.json        新版合约（浊燃作战系列） + 尖灭测试 recalRuneData

敌人 HP/ATK/DEF 等数值在官方 JSON 中不存在（运行时动态加载），
预留 enemy_stats_manual 表供手动录入或后续从 PRTS 等社区源导入。

用法：python3 scripts/build_enemies.py [默认写入项目 db/arknights.db]
       AK_DB_PATH=/tmp/x.db python3 scripts/build_enemies.py
"""
import json
import os
import sqlite3

BASE = '/Users/zhongxudong/Desktop/arknights-analyse/data/raw/excel'
DEFAULT_DB = '/Users/zhongxudong/Desktop/arknights-analyse/db/arknights.db'
DB_PATH = os.environ.get('AK_DB_PATH', DEFAULT_DB)


def load(name):
    with open(os.path.join(BASE, name), encoding='utf-8') as f:
        return json.load(f)


def jstr(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS enemy_races (
    race_id TEXT PRIMARY KEY, race_name TEXT, sort_id INTEGER
);
CREATE TABLE IF NOT EXISTS enemies (
    enemy_id TEXT PRIMARY KEY, enemy_index TEXT, name TEXT,
    enemy_level TEXT, attack_type TEXT, description TEXT,
    is_invalid_killed INTEGER, hide_in_handbook INTEGER, hide_in_stage INTEGER,
    sort_id INTEGER, damage_types TEXT, enemy_tags TEXT
);
CREATE TABLE IF NOT EXISTS enemy_abilities (
    enemy_id TEXT, ability_index INTEGER, text TEXT, text_format INTEGER,
    PRIMARY KEY (enemy_id, ability_index)
);
CREATE TABLE IF NOT EXISTS enemy_linked (
    enemy_id TEXT, linked_enemy_id TEXT, PRIMARY KEY (enemy_id, linked_enemy_id)
);
CREATE TABLE IF NOT EXISTS enemy_stats_manual (
    enemy_id TEXT, source TEXT, level INTEGER,
    max_hp INTEGER, atk INTEGER, def INTEGER, magic_resistance INTEGER,
    move_speed REAL, attack_speed REAL, base_attack_time REAL, range_id TEXT,
    hp_recovery_per_sec REAL, sp_recovery_per_sec REAL,
    damage_type TEXT, weight INTEGER, notes TEXT,
    elemental_resistance INTEGER, damage_resistance INTEGER,
    taunt_level INTEGER, status_resist TEXT,
    PRIMARY KEY (enemy_id, source, level)
);
CREATE TABLE IF NOT EXISTS crisis_seasons (
    season_id TEXT PRIMARY KEY, name TEXT, version TEXT,
    start_ts INTEGER, end_ts INTEGER, medal_group_id TEXT, season_code TEXT
);
CREATE TABLE IF NOT EXISTS crisis_runes_v1 (
    rune_id TEXT PRIMARY KEY, name TEXT, description TEXT, raw_json TEXT
);
CREATE TABLE IF NOT EXISTS crisis_v2_runes (
    level_id TEXT PRIMARY KEY, trigger_key TEXT, selector_json TEXT, blackboard TEXT
);
CREATE TABLE IF NOT EXISTS crisis_score_levels (
    score_threshold INTEGER PRIMARY KEY, appraise_type INTEGER
);
CREATE TABLE IF NOT EXISTS crisis_v2_const (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS recal_rune_seasons (
    season_id TEXT PRIMARY KEY, sort_id INTEGER, season_code TEXT, start_ts INTEGER,
    junior_reward TEXT, senior_reward TEXT, senior_reward_hint TEXT, main_medal_id TEXT
);
CREATE TABLE IF NOT EXISTS recal_rune_const (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_enemies_level ON enemies(enemy_level);
CREATE INDEX IF NOT EXISTS idx_enemy_abilities_e ON enemy_abilities(enemy_id);
CREATE INDEX IF NOT EXISTS idx_crisis_seasons_v ON crisis_seasons(version);
CREATE INDEX IF NOT EXISTS idx_v2_runes_t ON crisis_v2_runes(trigger_key);
"""


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    eh = load('enemy_handbook_table.json')
    cur.executemany('INSERT OR REPLACE INTO enemy_races VALUES (?,?,?)',
                    [(r['id'], r['raceName'], r.get('sortId')) for r in eh['raceData'].values()])
    rows, abil_rows, link_rows = [], [], []
    for eid, e in eh['enemyData'].items():
        rows.append((eid, e.get('enemyIndex'), e.get('name'),
                     e.get('enemyLevel'), e.get('attackType'), e.get('description'),
                     1 if e.get('isInvalidKilled') else 0,
                     1 if e.get('hideInHandbook') else 0,
                     1 if e.get('hideInStage') else 0,
                     e.get('sortId'),
                     jstr(e.get('damageType')), jstr(e.get('enemyTags'))))
        for i, a in enumerate(e.get('abilityList') or []):
            abil_rows.append((eid, i, a.get('text'), a.get('textFormat')))
        for le in e.get('linkEnemies') or []:
            link_rows.append((eid, le))
    cur.executemany('INSERT OR REPLACE INTO enemies VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    cur.executemany('INSERT OR REPLACE INTO enemy_abilities VALUES (?,?,?,?)', abil_rows)
    cur.executemany('INSERT OR REPLACE INTO enemy_linked VALUES (?,?)', link_rows)

    ct = load('crisis_table.json')
    c2 = load('crisis_v2_table.json')
    rows = [(s['seasonId'], s.get('name'), 'v1', s.get('startTs'), s.get('endTs'),
             s.get('medalGroupId'), None) for s in ct.get('seasonInfo', [])]
    for sid, v in c2.get('seasonInfoDataMap', {}).items():
        rows.append((sid, v.get('name'), 'v2', v.get('startTs'), v.get('endTs'),
                     v.get('medalGroupId'), v.get('crisisV2SeasonCode')))
    cur.executemany('INSERT OR REPLACE INTO crisis_seasons VALUES (?,?,?,?,?,?,?)', rows)
    cur.executemany('INSERT OR REPLACE INTO crisis_score_levels VALUES (?,?)',
                    [(int(k), v.get('appraiseType'))
                     for k, v in c2.get('scoreLevelToAppraiseDataMap', {}).items()])
    cur.executemany('INSERT OR REPLACE INTO crisis_v2_const VALUES (?,?)',
                    [(k, jstr(v)) for k, v in c2.get('constData', {}).items()])
    rows = []
    for level_id, items in c2.get('battleCommentRuneData', {}).items():
        for it in items:
            rows.append((level_id, it.get('key'),
                         jstr(it.get('selector')), jstr(it.get('blackboard'))))
    cur.executemany('INSERT OR REPLACE INTO crisis_v2_runes VALUES (?,?,?,?)', rows)

    rr = c2.get('recalRuneData', {})
    if isinstance(rr.get('seasons'), dict):
        cur.executemany('INSERT OR REPLACE INTO recal_rune_seasons VALUES (?,?,?,?,?,?,?,?)',
                        [(sid, v.get('sortId'), v.get('seasonCode'), v.get('startTs'),
                          jstr(v.get('juniorReward')), jstr(v.get('seniorReward')),
                          v.get('seniorRewardHint'), v.get('mainMedalId'))
                         for sid, v in rr['seasons'].items()])
    if isinstance(rr.get('constData'), dict):
        cur.executemany('INSERT OR REPLACE INTO recal_rune_const VALUES (?,?)',
                        [(k, jstr(v)) for k, v in rr['constData'].items()])
    conn.commit()

    print('=== 写入完成 ===')
    for t in ['enemy_races', 'enemies', 'enemy_abilities', 'enemy_linked',
              'enemy_stats_manual', 'crisis_seasons', 'crisis_runes_v1',
              'crisis_v2_runes', 'crisis_score_levels', 'crisis_v2_const',
              'recal_rune_seasons', 'recal_rune_const']:
        n = cur.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        print(f'  {t:25s} {n:>6,}')
    conn.close()


if __name__ == '__main__':
    main()