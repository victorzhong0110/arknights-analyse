#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v2 危机合约词条反向解析：把 selector+blackboard 翻译成可读中文描述。

伤害计算基础（用于本解析）：
- 攻击伤害 = 攻击力 × 倍率
- 物理: 有效 = max(伤害 - 防御, 伤害 × 5%)  // 5% 保底
- 法术: 有效 = 伤害 × (1 - 法抗/100)
- 真伤: 有效 = 伤害
- 攻击速度 = 100 + 黑板 as_add, 攻击间隔 = base / (1 + as_add/100)
- atk_scale 倍率: 黑板键 atk_scale / damage_scale
- 持续时间 = blackboard duration

解析策略：
1. selector → 影响范围描述（职业/敌人/范围/高低台/空中地面）
2. blackboard[].key → 操作类型描述（atk 加成 / max_hp 加成 / cost 加成 等）
3. value 数值 + 百分比 / 绝对值格式化
4. valueStr 是引用名 → 输出 "[引用 X]" 占位（PRTS 指标详情页可补充实际描述）

已知限制：v2 合约词条的真实数值在客户端二级查找表里（blackboard.valueStr 引用），
官方 JSON 不包含可读描述。selector + 键名映射能覆盖大部分语义。

用法：python3 explain_v2_runes.py            输出所有 v2 词条
"""
import json
import sqlite3

DB = '/Users/zhongxudong/Desktop/arknights-analyse/db/arknights.db'

# 职业码位掩码（bit OR）
PROFESSION_BITS = {
    1: '先锋', 2: '近卫', 4: '重装', 8: '狙击', 16: '术师',
    32: '特种', 64: '医疗', 128: '辅助', 256: '召唤物',
    512: '核心',
}
SIDE_TYPES = {1: '敌方', 2: '我方', 3: '中立', 4: '可部署位', 7: '全局'}
HEIGHT_TYPES = {1: '高台位', 2: '地面位', 3: '高台+地面'}

BB_KEY_TPL = {
    'atk': '攻击力{op}',
    'max_hp': '生命上限{op}',
    'def': '防御力{op}',
    'magic_resistance': '法术抗性{op}',
    'cost': '部署费用{op}',
    'move_speed': '移速{op}',
    'attack_speed': '攻击速度{op}',
    'base_attack_time': '攻击间隔{op}',
    'hp_recovery_per_sec': '生命回复{op}',
    'sp_recovery_per_sec': '自然回sp{op}',
    'respawn_time': '再部署时间{op}',
    'max_deploy_count': '同组可部署数{op}',
    'atk_scale': '伤害倍率{op}',
    'damage_scale': '受到的伤害{op}',
    'attack@atk_scale': '攻击伤害倍率{op}',
    'attack@damage_scale': '受到的伤害{op}',
    'duration': '持续时间{op}',
    'prob': '触发概率{op}',
    'stun': '晕眩时长{op}',
    'sluggish': '减速持续{op}',
    'block_cnt': '阻挡数{op}',
    'taunt_level': '嘲讽等级{op}',
    'force': '推力{op}',
}
# 哪些键是“百分比式”输出（v <= 2 视为倍率；v > 2 视为百分比）
PCT_KEYS = {'attack_speed', 'atk_scale', 'damage_scale',
            'attack@atk_scale', 'attack@damage_scale', 'prob'}


def decode_profession_mask(mask):
    if mask == 1023 or mask == 0:
        return '所有职业'
    parts = [name for bit, name in PROFESSION_BITS.items() if mask & bit]
    return '/'.join(parts) if parts else f'未识别({mask})'


def describe_selector(sel):
    if not sel:
        return '影响范围: 未知'
    parts = []
    if 'sideType' in sel:
        parts.append(SIDE_TYPES.get(sel['sideType'], f"side{sel['sideType']}"))
    if 'professionMask' in sel:
        parts.append(f"职业: {decode_profession_mask(sel['professionMask'])}")
    if 'heightTypeMask' in sel:
        parts.append(f"位置: {HEIGHT_TYPES.get(sel['heightTypeMask'], '?')}")
    if sel.get('enemyIdFilter'):
        parts.append(f"敌人过滤: {', '.join(sel['enemyIdFilter'][:5])}")
    if sel.get('charIdFilter'):
        parts.append(f"干员过滤: {', '.join(sel['charIdFilter'][:5])}")
    if sel.get('groupTagFilter'):
        parts.append(f"分组: {sel['groupTagFilter']}")
    return ' | '.join(parts) if parts else '影响范围: 默认(全图所有我方)'


def format_val(key, v):
    if v is None:
        return '?'
    if key in PCT_KEYS:
        if abs(v) <= 2:
            return f'×{v:g}'
        return f'{v * 100:g}%'
    return f'{v:g}' if isinstance(v, (int, float)) else str(v)


def describe_blackboard(bb):
    if not bb:
        return []
    descs = []
    for kv in bb:
        key = kv.get('key', '?')
        value = kv.get('value')
        value_str = kv.get('valueStr')
        if value_str:
            descs.append(f"[{key}] → 引用 {value_str}（需查 PRTS 指标详情）")
            continue
        if key in BB_KEY_TPL:
            disp = format_val(key, value)
            descs.append(BB_KEY_TPL[key].format(op=disp))
        else:
            descs.append(f"[{key}] = {value}")
    return descs


def describe_rune(level_id, trigger_key, sel_json, bb_json):
    sel = json.loads(sel_json) if sel_json else {}
    bb = json.loads(bb_json) if bb_json else []
    scope = describe_selector(sel)
    effects = describe_blackboard(bb)
    head = f"【{level_id}】({trigger_key})\n  范围: {scope}"
    if effects:
        return head + "\n  效果:\n    - " + '\n    - '.join(effects)
    return head + "\n  效果: 无（需查 PRTS）"


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    rows = cur.execute("SELECT level_id, trigger_key, selector_json, blackboard "
                       "FROM crisis_v2_runes ORDER BY level_id").fetchall()
    print(f'v2 词条总数: {len(rows)}\n')
    for r in rows:
        print(describe_rune(*r))
        print()
    conn.close()


if __name__ == '__main__':
    main()