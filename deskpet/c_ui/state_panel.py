"""Render cached observation facts and submit explicit, expiring corrections."""
from __future__ import annotations

import json

from PyQt6.QtWidgets import QGroupBox, QPushButton, QVBoxLayout

from deskpet.contracts import UiSpec
from .renderer import UiSpecRenderer
from .reminder import display_time
from .work_panel import label

STATES = {'working': '正在专注', 'available': '可以打扰', 'away': '离开', 'unknown': '暂时无法判断'}
TTL = {'5 秒': 5, '30 秒': 30, '5 分钟': 300, '30 分钟': 1800, '1 小时': 3600}
ORIGINS = {'user': '用户手动设置', 'automation': '自动测试输入', 'scripted': '脚本场景输入'}
SOURCES = {'scripted': '模拟采样', 'live': '桌面采样', 'rule': '规则估计', 'model': '模型估计', 'user': '用户设置'}
RAW_STATUS = {'fresh': '有效', 'expired': '已过期', 'future': '尚未生效', 'missing': '缺失', 'error': '采样失败'}
SIGNALS = {'foreground': '前台应用', 'idle': '空闲时长', 'idle_s': '空闲秒数', 'application': '应用', 'input_active': '输入活跃', 'cursor': '光标', 'windows': '窗口'}
COMPONENTS = (
    dict(component_id='intro', type='text', label='状态来自当前观察；有期限的纠正优先，到期后重新采用有效估计。'),
    dict(component_id='effective', type='card', label='当前状态与基础估计', data_key='effective'),
    dict(component_id='raw', type='text', data_key='raw'),
    dict(component_id='evidence', type='list', data_key='evidence'),
    dict(component_id='correction', type='text', data_key='correction'),
    dict(component_id='state', type='select', label='你当前的状态', options=list(STATES.values()), data_key='state'),
    dict(component_id='reason', type='text_input', label='纠正原因（必填）', data_key='reason'),
    dict(component_id='ttl', type='select', label='持续时间', options=list(TTL), data_key='ttl_s'),
    dict(component_id='apply', type='button', label='应用临时纠正', action_id='state.override'),
    dict(component_id='clear', type='button', label='清除纠正，恢复估计', action_id='state.clear_override'),
)


def _time(value):
    return display_time(value) if value else '无有效期限'


def build_state_spec(snapshot):
    observation = snapshot['observation']
    frame = snapshot.get('observation_frame') or {}
    effective = observation['estimate']
    estimated = frame.get('estimated') or effective
    correction = frame.get('correction')
    correction_status = frame.get('correction_status', 'none')
    source = '临时纠正' if effective['source'] == 'user' else '系统估计'
    summary = f"当前：{STATES[effective['state']]} · {source}\n基础估计：{STATES[estimated['state']]} · 有效至 {_time(estimated.get('valid_until'))}"
    if correction and correction_status == 'active':
        correction_text = f"{ORIGINS[correction['origin']]}：{STATES[correction['state']]}\n原因：{correction['reason']}\n设置于 {_time(correction['created_at'])}，截止 {_time(correction['valid_until'])}"
    else:
        correction_text = {'expired': '纠正已到期，已恢复当前可用的估计。', 'cleared': '纠正已清除，已恢复当前可用的估计。'}.get(correction_status, '当前没有生效的纠正。')
    raw = frame.get('raw')
    if raw:
        capabilities = '、'.join(SIGNALS.get(k,k) for k, v in raw.get('capabilities', {}).items() if v) or '无可用能力'
        raw_text = f"原始采样：{SOURCES.get(raw['source'],raw['source'])} · {RAW_STATUS.get(frame.get('raw_status'),'暂不明确')}\n采集 {_time(raw['sampled_at'])}，截止 {_time(raw['valid_until'])}\n能力：{capabilities}"
        if raw.get('errors'):
            raw_text += '\n采样缺口：' + '、'.join(raw['errors'])
    else:
        raw_text = '尚无有效原始采样，当前状态可能无法判断。'
    if frame.get('error'):
        raw_text += '\n观察诊断：' + frame['error']
    evidence = []
    for e in estimated.get('evidence_details', []):
        value = json.dumps(e.get('value'), ensure_ascii=False)
        evidence.append(f"{SIGNALS.get(e['signal'],e['signal'])}：{value} · {SOURCES.get(e['source'],e['source'])} · 截止 {_time(e['valid_until'])}")
    if not evidence:
        evidence = list(estimated.get('evidence', [])) or ['无可用判断依据']
    if estimated.get('based_on_sample_id'):
        evidence.insert(0, '依据样本：' + estimated['based_on_sample_id'])
    return UiSpec(surface_id='c-observation', surface_revision=observation['context_revision'],
        components=COMPONENTS, data=dict(effective=summary, raw=raw_text, evidence=evidence,
            correction=correction_text, state='正在专注', reason='', ttl_s='30 分钟'))


class StatePanel(QGroupBox):
    def __init__(self, *, submit, parent=None):
        super().__init__('当前状态', parent)
        self.submit = submit
        layout = QVBoxLayout(self)
        self.summary = label('等待状态数据…')
        layout.addWidget(self.summary)
        self.toggle = QPushButton('查看依据与纠正状态')
        self.toggle.setCheckable(True)
        self.toggle.setEnabled(False)
        layout.addWidget(self.toggle)
        self.renderer = UiSpecRenderer(submit=self._submit, allowed_actions={'state.override', 'state.clear_override'})
        layout.addWidget(self.renderer)
        self.renderer.hide()
        self.toggle.toggled.connect(self.renderer.setVisible)

    def _submit(self, action):
        if action.action_id == 'state.override':
            values = action.payload
            reason = values.get('reason', '').strip()
            if not reason:
                raise ValueError('请填写纠正原因，输入会保留。')
            state = next((k for k, title in STATES.items() if title == values.get('state')), None)
            if state is None or values.get('ttl_s') not in TTL:
                raise ValueError('请选择合法状态与有效期。')
            payload = dict(state=state, reason=reason, ttl_s=TTL[values['ttl_s']])
        elif action.action_id == 'state.clear_override':
            payload = {}
        else:
            raise ValueError('此状态界面不支持该操作。')
        return self.submit(action.model_copy(update={'payload': payload}))

    def render_snapshot(self, snapshot):
        if not snapshot:
            self.toggle.setEnabled(False)
            return
        spec = build_state_spec(snapshot)
        self.summary.setText(spec.data['effective'].split('\n')[0])
        self.renderer.render_spec(spec)
        active = (snapshot.get('observation_frame') or {}).get('correction_status') == 'active'
        self.renderer.enable_action('state.clear_override', active)
        self.toggle.setEnabled(True)
