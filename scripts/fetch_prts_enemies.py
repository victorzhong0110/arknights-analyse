#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PRTS Wiki 敌人数值抓取 → 导入 enemy_stats_manual。

数据流：
  1. 从 enemies 表查敌人列表
  2. PRTS MediaWiki API 拉 wikitext（结构化模板）
  3. 解析 {{敌人信息/levelcontent}} 模板：每个级别增量式字段
  4. 写 enemy_stats_manual（enemy_id, source='prts', level, ...）

用法：
  AK_LIMIT=5 python3 fetch_prts_enemies.py            # 试抓 5 个
  python3 fetch_prts_enemies.py                        # 全部
  AK_DRY_RUN=1 python3 fetch_prts_enemies.py           # 只解析不入库
  AK_FORCE=1 python3 fetch_prts_enemies.py             # 强制重抓已存在
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

DB = '/Users/zhongxudong/Desktop/arknights-analyse/db/arknights.db'
API = 'https://prts.wiki/api.php'
UA = 'arknights-analyse/0.1 (https://github.com/victorzhong0110/arknights-analyse)'


def api_get(params, retries=3):
    q = urllib.parse.urlencode(params)
    url = f'{API}?{q}'
    last_err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:
            last_err = e
            time.sleep(2 + i)
    raise last_err


def search_page(name):
    """敌人名通常即 PRTS 页面标题；搜索 API 不可靠（会返回 /spine 等模型页），直接按名抓取。"""
    return name


def fetch_wikitext(page_title, retries=4):
    """抓取页面 wikitext。api.php 会被 403，改用移动端 action=raw；带重试退避。"""
    url = 'https://m.prts.wiki/index.php?title=%s&action=raw' % urllib.parse.quote(page_title)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode('utf-8')
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def split_fields(body):
    """按 | 分割模板字段，跳过 {{...}} 嵌套模板内的 |。"""
    fields, depth, cur = [], 0, ''
    i = 0
    while i < len(body):
        if body.startswith('{{', i):
            depth += 1
            cur += '{{'
            i += 2
        elif body.startswith('}}', i):
            depth -= 1
            cur += '}}'
            i += 2
        elif body[i] == '|' and depth == 0:
            fields.append(cur)
            cur = ''
            i += 1
        else:
            cur += body[i]
            i += 1
    fields.append(cur)
    return fields


def parse_templates(wikitext, name):
    """按 {{name ...}} 深度计数匹配，提取所有模板体（处理嵌套模板）。"""
    results, start_tag, idx = [], '{{' + name, 0
    while True:
        i = wikitext.find(start_tag, idx)
        if i < 0:
            break
        j = i + len(start_tag)
        depth, k = 1, j
        while k < len(wikitext) and depth > 0:
            if wikitext.startswith('{{', k):
                depth += 1
                k += 2
            elif wikitext.startswith('}}', k):
                depth -= 1
                k += 2
            else:
                k += 1
        results.append(wikitext[j:k - 2])  # 去掉结尾 }}
        idx = k
    return results


def parse_levels(wikitext):
    """从 wikitext 解析所有 {{敌人信息/levelcontent}} 模板。"""
    levels = []
    for body in parse_templates(wikitext, '敌人信息/levelcontent'):
        d = {'_index': 0}
        for fm in split_fields(body):
            if '=' in fm:
                key, val = fm.split('=', 1)
                d[key.strip()] = val.strip()
        try:
            d['_index'] = int(d.get('index', '0'))
        except Exception:
            pass
        levels.append(d)
    common = {}
    for body in parse_templates(wikitext, '敌人信息/common2'):
        for fm in split_fields(body):
            if '=' in fm:
                key, val = fm.split('=', 1)
                common[key.strip()] = val.strip()
    return levels, common


def to_int(s):
    if s in (None, '', '无', '—'):
        return None
    try:
        return int(s)
    except Exception:
        try:
            return int(float(s))
        except Exception:
            return None


def to_float(s):
    if s in (None, '', '无', '—'):
        return None
    try:
        return float(s)
    except Exception:
        return None


def levels_to_rows(levels):
    """增量式级别展开为完整行。"""
    rows = []
    last = {}
    for lv in levels:
        cur = dict(last)
        cur['_index'] = lv['_index']
        for k, v in lv.items():
            if k == 'index':
                continue
            cur[k] = v
        rows.append(cur)
        last = cur
    return rows


def to_resist(s):
    """异常状态抗性：无→0，有/免疫→1，空→None。"""
    if s in (None, '', '无'):
        return 0
    if s in ('有', '免疫', '是'):
        return 1
    return None


STATUS_RESIST_FIELDS = ('眩晕抗性', '沉默抗性', '沉睡抗性', '冻结抗性', '浮空抗性',
                        '战栗抗性', '恐惧抗性', '麻痹抗性', '诱导抗性', '传送抗性', '缚地抗性')


def fetch_one(enemy_id, name):
    page = search_page(name)
    wt = fetch_wikitext(page)
    if '敌人信息' not in wt:
        return []  # 无敌人模板（模型页/无页面）
    levels, common = parse_levels(wt)
    rows = levels_to_rows(levels)
    out = []
    for r in rows:
        level = r['_index']
        status_resist = {f: to_resist(r.get(f)) for f in STATUS_RESIST_FIELDS}
        row = {
            'enemy_id': enemy_id,
            'source': 'prts',
            'level': level,
            'max_hp': to_int(r.get('最大生命值')),
            'atk': to_int(r.get('攻击力')),
            'def': to_int(r.get('防御力')),
            'magic_resistance': to_int(r.get('法术抗性')),
            'move_speed': to_float(r.get('移动速度')),
            'attack_speed': to_float(r.get('攻击速度')),
            'base_attack_time': to_float(r.get('攻击间隔')),
            'range_id': r.get('攻击范围半径') or None,
            'hp_recovery_per_sec': to_float(r.get('生命恢复速度')),
            'sp_recovery_per_sec': to_float(r.get('sp恢复速度')),
            'damage_type': common.get('伤害类型'),
            'weight': to_int(r.get('重量等级')),
            'elemental_resistance': to_int(r.get('元素抗性')),
            'damage_resistance': to_int(r.get('损伤抵抗')),
            'taunt_level': to_int(r.get('基础嘲讽等级')),
            'status_resist': json.dumps(status_resist, ensure_ascii=False),
            'notes': None,
        }
        if all(v is None for k, v in row.items()
               if k not in ('enemy_id', 'source', 'level', 'status_resist')):
            continue
        out.append(row)
    return out


def main():
    limit = int(os.environ.get('AK_LIMIT', '99999'))
    dry = os.environ.get('AK_DRY_RUN') == '1'
    force = os.environ.get('AK_FORCE') == '1'

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    # 扩展列（元素抗性/损伤抵抗/嘲讽/异常状态抗性）
    for col, typ in (('elemental_resistance', 'INTEGER'), ('damage_resistance', 'INTEGER'),
                     ('taunt_level', 'INTEGER'), ('status_resist', 'TEXT')):
        try:
            cur.execute(f'ALTER TABLE enemy_stats_manual ADD COLUMN {col} {typ}')
        except Exception:
            pass

    targets = cur.execute(
        "SELECT enemy_id, name FROM enemies WHERE name NOT LIKE '%鸭%' "
        "AND enemy_id NOT IN (SELECT enemy_id FROM enemy_stats_manual "
        "WHERE elemental_resistance IS NOT NULL AND damage_resistance IS NOT NULL) "
        "ORDER BY sort_id LIMIT ?", (limit,)).fetchall()
    print(f'目标敌人: {len(targets)} 个')

    n_ok = n_skip = n_err = 0
    for enemy_id, name in targets:
        if not force:
            ex = cur.execute("SELECT 1 FROM enemy_stats_manual "
                             "WHERE enemy_id=? AND source='prts' LIMIT 1",
                             (enemy_id,)).fetchone()
            if ex:
                n_skip += 1
                continue
        try:
            rows = fetch_one(enemy_id, name)
            if not rows:
                n_err += 1
                continue
            for r in rows:
                if dry:
                    print(f'  DRY {enemy_id} {name} L{r["level"]}: '
                          f'HP={r["max_hp"]} ATK={r["atk"]} DEF={r["def"]} '
                          f'法抗={r["magic_resistance"]} 移速={r["move_speed"]} 攻速={r["attack_speed"]}')
                else:
                    cur.execute("""
                        INSERT OR REPLACE INTO enemy_stats_manual
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (r['enemy_id'], r['source'], r['level'],
                          r['max_hp'], r['atk'], r['def'], r['magic_resistance'],
                          r['move_speed'], r['attack_speed'], r['base_attack_time'],
                          r['range_id'], r['hp_recovery_per_sec'],
                          r['sp_recovery_per_sec'], r['damage_type'],
                          r['weight'], r['notes'],
                          r['elemental_resistance'], r['damage_resistance'],
                          r['taunt_level'], r['status_resist']))
            n_ok += 1
            sys.stdout.write(f'\r  成功: {n_ok}  跳过: {n_skip}  失败: {n_err}')
            sys.stdout.flush()
        except Exception as e:
            n_err += 1
            print(f'\n  ERR {enemy_id} {name}: {e}')
        time.sleep(1.2)
    if not dry:
        conn.commit()
    conn.close()
    print(f'\n完成: 成功 {n_ok}, 跳过 {n_skip}, 失败 {n_err}')


if __name__ == '__main__':
    main()