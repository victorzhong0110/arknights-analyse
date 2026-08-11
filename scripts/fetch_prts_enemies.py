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
    """查 wiki page 名（优先全匹配，回退第一条搜索结果）。"""
    try:
        data = api_get({'action': 'query', 'format': 'json',
                        'list': 'search', 'srsearch': name, 'srlimit': 5})
        hits = data.get('query', {}).get('search', [])
        for h in hits:
            if h.get('title') == name:
                return name
        if hits:
            return hits[0]['title']
    except Exception:
        pass
    return name


def fetch_wikitext(page_title):
    data = api_get({'action': 'parse', 'format': 'json', 'page': page_title,
                    'prop': 'wikitext', 'formatversion': '2'})
    return data.get('parse', {}).get('wikitext', '')


def parse_levels(wikitext):
    """从 wikitext 解析所有 {{敌人信息/levelcontent}} 模板。"""
    levels = []
    for m in re.finditer(
            r'{{敌人信息/levelcontent\s*(.*?)}}\s*\n', wikitext, re.DOTALL):
        body = m.group(1)
        d = {'_index': 0}
        for fm in re.finditer(r'\|(\S+?)=([^|]*?)(?=\||$)', body):
            key = fm.group(1).strip()
            val = fm.group(2).strip()
            d[key] = val
        try:
            d['_index'] = int(d.get('index', '0'))
        except Exception:
            pass
        levels.append(d)
    common = {}
    m = re.search(r'{{敌人信息/common2\s*(.*?)}}\s*\n', wikitext, re.DOTALL)
    if m:
        for fm in re.finditer(r'\|(\S+?)=([^|]*?)(?=\||$)', m.group(1)):
            common[fm.group(1).strip()] = fm.group(2).strip()
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


def fetch_one(enemy_id, name):
    page = search_page(name)
    wt = fetch_wikitext(page)
    levels, common = parse_levels(wt)
    rows = levels_to_rows(levels)
    out = []
    for r in rows:
        level = r['_index']
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
            'notes': None,
        }
        if all(v is None for k, v in row.items()
               if k not in ('enemy_id', 'source', 'level')):
            continue
        out.append(row)
    return out


def main():
    limit = int(os.environ.get('AK_LIMIT', '99999'))
    dry = os.environ.get('AK_DRY_RUN') == '1'
    force = os.environ.get('AK_FORCE') == '1'

    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    targets = cur.execute(
        "SELECT enemy_id, name FROM enemies WHERE enemy_level IN ('ELITE','BOSS') "
        "AND name NOT LIKE '%鸭%' ORDER BY sort_id LIMIT ?", (limit,)).fetchall()
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
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (r['enemy_id'], r['source'], r['level'],
                          r['max_hp'], r['atk'], r['def'], r['magic_resistance'],
                          r['move_speed'], r['attack_speed'], r['base_attack_time'],
                          r['range_id'], r['hp_recovery_per_sec'],
                          r['sp_recovery_per_sec'], r['damage_type'],
                          r['weight'], r['notes']))
            n_ok += 1
            sys.stdout.write(f'\r  成功: {n_ok}  跳过: {n_skip}  失败: {n_err}')
            sys.stdout.flush()
        except Exception as e:
            n_err += 1
            print(f'\n  ERR {enemy_id} {name}: {e}')
        time.sleep(0.4)
    if not dry:
        conn.commit()
    conn.close()
    print(f'\n完成: 成功 {n_ok}, 跳过 {n_skip}, 失败 {n_err}')


if __name__ == '__main__':
    main()