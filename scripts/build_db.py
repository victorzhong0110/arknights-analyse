#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
将 data/raw/excel 下的明日方舟原始 JSON 数据构建为 SQLite 数据库。
输出：db/arknights.db

数据源结构说明（2026/08/06, v76.2.0）：
- character_table.json : 干员（含属性/技能引用/天赋/潜能/信赖/精英化数据）
- token_table.json     : 召唤物（结构与干员一致，source='token'）
- skill_table.json     : 技能描述与各等级数值
- range_table.json     : 攻击范围
- uniequip_table.json  : 模组
- building_data.json   : 基建技能
- gacha_table.json     : 公招标签 / 卡池
- handbook_info_table.json : 干员档案
"""
import json
import os
import sqlite3

BASE = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'data', 'raw', 'excel'))
DEFAULT_DB = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', 'db', 'arknights.db'))
# 可用环境变量 AK_DB_PATH 覆盖数据库输出路径
DB_PATH = os.environ.get('AK_DB_PATH', DEFAULT_DB)

BOOL_COLS = ('stun_immune', 'silence_immune', 'sleep_immune', 'frozen_immune',
             'levitate_immune', 'disarmed_combat_immune', 'feared_immune',
             'palsy_immune', 'attract_immune', 'teleport_immune', 'ground_bound_immune')


def load(name):
    with open(os.path.join(BASE, name), encoding='utf-8') as f:
        return json.load(f)


def jstr(obj):
    return json.dumps(obj, ensure_ascii=False) if obj is not None else None


SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    char_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'character',
    name TEXT, appellation TEXT,
    profession TEXT, sub_profession_id TEXT,
    position TEXT, rarity INTEGER, max_potential_level INTEGER,
    nation_id TEXT, group_id TEXT, team_id TEXT,
    display_number TEXT,
    tag_list TEXT, trait TEXT,
    main_power TEXT, sub_power TEXT, sort_index INTEGER,
    is_not_obtainable INTEGER, is_sp_char INTEGER,
    item_usage TEXT, item_desc TEXT, item_obtain_approach TEXT
);

CREATE TABLE IF NOT EXISTS operator_phases (
    char_id TEXT, phase INTEGER, max_level INTEGER,
    range_id TEXT, prefab_key TEXT, evolve_cost TEXT,
    PRIMARY KEY (char_id, phase)
);

CREATE TABLE IF NOT EXISTS operator_levels (
    char_id TEXT, phase INTEGER, level INTEGER,
    max_hp REAL, atk REAL, def REAL, magic_resistance REAL,
    cost REAL, block_cnt REAL, move_speed REAL, attack_speed REAL,
    base_attack_time REAL, respawn_time REAL,
    hp_recovery_per_sec REAL, sp_recovery_per_sec REAL,
    max_deploy_count REAL, max_deck_stack_cnt REAL,
    taunt_level REAL, mass_level REAL, base_force_level REAL,
    stun_immune INTEGER, silence_immune INTEGER, sleep_immune INTEGER, frozen_immune INTEGER,
    levitate_immune INTEGER, disarmed_combat_immune INTEGER, feared_immune INTEGER,
    palsy_immune INTEGER, attract_immune INTEGER, teleport_immune INTEGER, ground_bound_immune INTEGER,
    PRIMARY KEY (char_id, phase, level)
);

CREATE TABLE IF NOT EXISTS operator_skills (
    char_id TEXT, skill_index INTEGER, skill_id TEXT,
    unlock_phase INTEGER, unlock_level INTEGER,
    level_up_cost TEXT,
    PRIMARY KEY (char_id, skill_index)
);

CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY, icon_id TEXT, hidden INTEGER, level_count INTEGER
);

CREATE TABLE IF NOT EXISTS skill_levels (
    skill_id TEXT, level_index INTEGER,
    name TEXT, range_id TEXT, description TEXT,
    skill_type TEXT, duration_type TEXT,
    sp_data TEXT, duration REAL, blackboard TEXT,
    PRIMARY KEY (skill_id, level_index)
);

CREATE TABLE IF NOT EXISTS talents (
    char_id TEXT, talent_index INTEGER, candidate_index INTEGER,
    unlock_phase INTEGER, unlock_level INTEGER,
    required_potential_rank INTEGER, prefab_key TEXT,
    name TEXT, description TEXT, blackboard TEXT,
    PRIMARY KEY (char_id, talent_index, candidate_index)
);

CREATE TABLE IF NOT EXISTS potentials (
    char_id TEXT, potential_index INTEGER,
    type TEXT, description TEXT, blackboard TEXT,
    PRIMARY KEY (char_id, potential_index)
);

CREATE TABLE IF NOT EXISTS favor (
    char_id TEXT, level INTEGER,
    max_hp REAL, atk REAL, def REAL, magic_resistance REAL,
    PRIMARY KEY (char_id, level)
);

CREATE TABLE IF NOT EXISTS ranges (
    range_id TEXT PRIMARY KEY, direction INTEGER, grids TEXT
);

CREATE TABLE IF NOT EXISTS modules (
    equip_id TEXT PRIMARY KEY, char_id TEXT,
    name TEXT, description TEXT, type TEXT,
    type_icon TEXT, type_name1 TEXT, type_name2 TEXT,
    equip_shining_color TEXT,
    show_evolve_phase INTEGER, unlock_evolve_phase INTEGER,
    show_level INTEGER, unlock_level INTEGER,
    has_unlock_mission INTEGER, mission_list TEXT,
    unlock_favors TEXT, item_cost TEXT, char_equip_order INTEGER,
    is_special_equip INTEGER
);

CREATE TABLE IF NOT EXISTS building (
    char_id TEXT PRIMARY KEY, max_manpower INTEGER,
    buffs TEXT, skill_data TEXT
);

CREATE TABLE IF NOT EXISTS recruit_tags (
    tag_id INTEGER PRIMARY KEY, tag_name TEXT, tag_group INTEGER
);

CREATE TABLE IF NOT EXISTS gacha_pools (
    gacha_pool_id TEXT PRIMARY KEY, gacha_index INTEGER,
    open_time INTEGER, end_time INTEGER,
    name TEXT, summary TEXT, detail TEXT,
    guarantee_name TEXT, guarantee5_avail INTEGER, guarantee5_count INTEGER
);

CREATE TABLE IF NOT EXISTS handbook (
    char_id TEXT PRIMARY KEY,
    info_name TEXT, is_limited INTEGER,
    story_text_audio TEXT, avg_list TEXT
);

CREATE INDEX IF NOT EXISTS idx_ophases_char ON operator_phases(char_id);
CREATE INDEX IF NOT EXISTS idx_olevels_char ON operator_levels(char_id);
CREATE INDEX IF NOT EXISTS idx_oskills_char ON operator_skills(char_id);
CREATE INDEX IF NOT EXISTS idx_oskills_skill ON operator_skills(skill_id);
CREATE INDEX IF NOT EXISTS idx_sklevels_skill ON skill_levels(skill_id);
CREATE INDEX IF NOT EXISTS idx_talents_char ON talents(char_id);
CREATE INDEX IF NOT EXISTS idx_modules_char ON modules(char_id);
"""


def build_characters(cur, data, source):
    """character_table / token_table 共用解析"""
    rows_op = []
    rows_ph = []
    rows_lv = []
    rows_sk = []
    rows_tl = []
    rows_pot = []
    rows_fav = []
    for cid, c in data.items():
        rows_op.append((
            cid, source, c.get('name'), c.get('appellation'),
            c.get('profession'), c.get('subProfessionId'),
            c.get('position'), c.get('rarity'), c.get('maxPotentialLevel'),
            c.get('nationId'), c.get('groupId'), c.get('teamId'),
            c.get('displayNumber'),
            jstr(c.get('tagList')), jstr(c.get('trait')),
            jstr(c.get('mainPower')), jstr(c.get('subPower')), c.get('sortIndex'),
            1 if c.get('isNotObtainable') else 0,
            1 if c.get('isSpChar') else 0,
            c.get('itemUsage'), c.get('itemDesc'), c.get('itemObtainApproach'),
        ))
        for pi, ph in enumerate(c.get('phases') or []):
            rows_ph.append((cid, pi, ph.get('maxLevel'),
                            ph.get('rangeId'), ph.get('characterPrefabKey'),
                            jstr(ph.get('evolveCost'))))
            for kf in ph.get('attributesKeyFrames') or []:
                d = kf.get('data') or {}
                rows_lv.append((cid, pi, kf.get('level'),
                                d.get('maxHp'), d.get('atk'), d.get('def'),
                                d.get('magicResistance'), d.get('cost'),
                                d.get('blockCnt'), d.get('moveSpeed'),
                                d.get('attackSpeed'), d.get('baseAttackTime'),
                                d.get('respawnTime'), d.get('hpRecoveryPerSec'),
                                d.get('spRecoveryPerSec'), d.get('maxDeployCount'),
                                d.get('maxDeckStackCnt'), d.get('tauntLevel'),
                                d.get('massLevel'), d.get('baseForceLevel'),
                                *[1 if d.get(b) else 0 for b in BOOL_COLS]))
        for i, s in enumerate(c.get('skills') or []):
            cond = s.get('unlockCond') or {}
            rows_sk.append((cid, i, s.get('skillId'),
                            cond.get('phase'), cond.get('level'),
                            jstr(s.get('levelUpCostCond'))))
        for i, t in enumerate(c.get('talents') or []):
            for j, cand in enumerate(t.get('candidates') or []):
                cond = cand.get('unlockCondition') or {}
                rows_tl.append((cid, i, j, cond.get('phase'), cond.get('level'),
                                cand.get('requiredPotentialRank'),
                                cand.get('prefabKey'), cand.get('name'),
                                cand.get('description'), jstr(cand.get('blackboard'))))
        for i, p in enumerate(c.get('potentialRanks') or []):
            rows_pot.append((cid, i, p.get('type'), p.get('description'),
                             jstr(p.get('buff'))))
        for kf in c.get('favorKeyFrames') or []:
            d = kf.get('data') or {}
            rows_fav.append((cid, kf.get('level'), d.get('maxHp'), d.get('atk'),
                             d.get('def'), d.get('magicResistance')))
    cur.executemany('INSERT OR REPLACE INTO operators VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows_op)
    cur.executemany('INSERT OR REPLACE INTO operator_phases VALUES (?,?,?,?,?,?)', rows_ph)
    cur.executemany('INSERT OR REPLACE INTO operator_levels (char_id, phase, level, max_hp, atk, def, magic_resistance, cost, block_cnt, move_speed, attack_speed, base_attack_time, respawn_time, hp_recovery_per_sec, sp_recovery_per_sec, max_deploy_count, max_deck_stack_cnt, taunt_level, mass_level, base_force_level, stun_immune, silence_immune, sleep_immune, frozen_immune, levitate_immune, disarmed_combat_immune, feared_immune, palsy_immune, attract_immune, teleport_immune, ground_bound_immune) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows_lv)
    cur.executemany('INSERT OR REPLACE INTO operator_skills VALUES (?,?,?,?,?,?)', rows_sk)
    cur.executemany('INSERT OR REPLACE INTO talents VALUES (?,?,?,?,?,?,?,?,?,?)', rows_tl)
    cur.executemany('INSERT OR REPLACE INTO potentials VALUES (?,?,?,?,?)', rows_pot)
    cur.executemany('INSERT OR REPLACE INTO favor VALUES (?,?,?,?,?,?)', rows_fav)
    return len(rows_op)


def main():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(SCHEMA)

    n_op = build_characters(cur, load('character_table.json'), 'character')
    n_tk = build_characters(cur, load('token_table.json'), 'token')

    # 技能
    skills = load('skill_table.json')
    rows_s = [(sid, s.get('iconId'), 1 if s.get('hidden') else 0, len(s.get('levels') or []))
              for sid, s in skills.items()]
    rows_sl = []
    for sid, s in skills.items():
        for i, lv in enumerate(s.get('levels') or []):
            rows_sl.append((sid, i, lv.get('name'), lv.get('rangeId'),
                            lv.get('description'), lv.get('skillType'),
                            lv.get('durationType'), jstr(lv.get('spData')),
                            lv.get('duration'), jstr(lv.get('blackboard'))))
    cur.executemany('INSERT OR REPLACE INTO skills VALUES (?,?,?,?)', rows_s)
    cur.executemany('INSERT OR REPLACE INTO skill_levels VALUES (?,?,?,?,?,?,?,?,?,?)', rows_sl)

    # 攻击范围
    ranges = load('range_table.json')
    cur.executemany('INSERT OR REPLACE INTO ranges VALUES (?,?,?)',
                    [(rid, r.get('direction'), jstr(r.get('grids')))
                     for rid, r in ranges.items()])

    # 模组
    u = load('uniequip_table.json')
    rows_m = []
    for eid, e in (u.get('equipDict') or {}).items():
        rows_m.append((eid, e.get('charId'), e.get('uniEquipName'),
                       e.get('uniEquipDesc'), e.get('type'), e.get('typeIcon'),
                       e.get('typeName1'), e.get('typeName2'),
                       e.get('equipShiningColor'), e.get('showEvolvePhase'),
                       e.get('unlockEvolvePhase'), e.get('showLevel'),
                       e.get('unlockLevel'),
                       1 if e.get('hasUnlockMission') else 0,
                       jstr(e.get('missionList')), jstr(e.get('unlockFavors')),
                       jstr(e.get('itemCost')), e.get('charEquipOrder'),
                       1 if e.get('isSpecialEquip') else 0))
    cur.executemany('INSERT OR REPLACE INTO modules VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows_m)

    # 基建
    b = load('building_data.json')
    rows_b = []
    for cid, c in (b.get('chars') or {}).items():
        rows_b.append((cid, c.get('maxManpower'),
                       jstr(c.get('buffChar')), jstr(c.get('skillData'))))
    cur.executemany('INSERT OR REPLACE INTO building VALUES (?,?,?,?)', rows_b)

    # 公招标签 / 卡池
    g = load('gacha_table.json')
    cur.executemany('INSERT OR REPLACE INTO recruit_tags VALUES (?,?,?)',
                    [(t.get('tagId'), t.get('tagName'), t.get('tagGroup'))
                     for t in g.get('gachaTags') or []])
    rows_p = []
    for p in g.get('gachaPoolClient') or []:
        rows_p.append((p.get('gachaPoolId'), p.get('gachaIndex'),
                       p.get('openTime'), p.get('endTime'),
                       p.get('gachaPoolName'), p.get('gachaPoolSummary'),
                       p.get('gachaPoolDetail'), p.get('guaranteeName'),
                       p.get('guarantee5Avail'), p.get('guarantee5Count')))
    cur.executemany('INSERT OR REPLACE INTO gacha_pools VALUES (?,?,?,?,?,?,?,?,?,?)', rows_p)

    # 档案
    h = load('handbook_info_table.json')
    rows_h = []
    for cid, v in (h.get('handbookDict') or {}).items():
        rows_h.append((cid, v.get('infoName'), 1 if v.get('isLimited') else 0,
                       jstr(v.get('storyTextAudio')), jstr(v.get('handbookAvgList'))))
    cur.executemany('INSERT OR REPLACE INTO handbook VALUES (?,?,?,?,?)', rows_h)

    conn.commit()

    # 汇总
    print('=== 构建完成:', DB_PATH, '===')
    for table in ['operators', 'operator_phases', 'operator_levels', 'operator_skills',
                  'skills', 'skill_levels', 'talents', 'potentials', 'favor',
                  'ranges', 'modules', 'building', 'recruit_tags', 'gacha_pools', 'handbook']:
        n = cur.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        print(f'{table:20s} {n:>8,}')
    conn.close()


if __name__ == '__main__':
    main()
