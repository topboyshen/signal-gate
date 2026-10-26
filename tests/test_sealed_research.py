"""Offline sealed-relay contract tests. No production artifacts or network."""
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('sealed_content_test', Path(__file__).resolve().parents[1] / 'app/digest_content.py')
content = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(content)
NOW = datetime(2026, 9, 10, 8, 15, tzinfo=content.TZ)


def payload(now=NOW):
    day = now.date().isoformat()
    market = (now.date() - timedelta(days=3 if now.weekday() == 0 else 1)).isoformat()
    return {
        'source': 'nng8', 'as_of': market, 'target_date': day,
        'produced_at': now.replace(hour=8, minute=2).isoformat(),
        'research_contract': {
            'version': 1, 'status': 'ready', 'report_date': day, 'as_of': market,
            'completed_at': now.replace(hour=8, minute=1).isoformat(),
            'report_sha256': 'a' * 64, 'last_run_sha256': 'b' * 64,
            'context_sha256': 'c' * 64, 'data_status': 'partial',
            'semantic_review': 'not_human_reviewed', 'publication_gate': 'passed',
            'evidence_review': 'passed',
        },
        'research_summary': {
            'conclusion': '[推断] 合成研究结论，仍需验证。',
            'evidence': ['[来源 2] 合成支持证据'],
            'counter_evidence': ['[来源 3] 合成反证'],
            'risks': ['关键数据缺失，不能升级为事实。'],
            'sources': ['2. 合成来源甲；数据时间：' + market, '3. 合成来源乙；数据时间：' + market],
            'limitations': ['合成研究；未经人工语义审核。'],
        },
        'text': 'THIS_LEGACY_TEXT_MUST_NEVER_LEAK',
        'markdown': 'THIS_LEGACY_MARKDOWN_MUST_NEVER_LEAK',
    }


class SealedResearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='sealed-research-offline-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.folder = self.base / 'inbox/nng8'
        self.folder.mkdir(parents=True)

    def render(self, data, now=NOW, markdown=b'STALE_MD_FALLBACK_MUST_NEVER_LEAK'):
        filename = self.folder / 'digest-latest.json'
        filename.write_text(json.dumps(data, ensure_ascii=False))
        (self.folder / 'digest-latest.md').write_bytes(markdown)
        before = {file.name: file.read_bytes() for file in self.folder.iterdir()}
        renderer = content.Renderer(self.base, self.base / 'absent.db', now)
        # Isolate contract/render validation after the canonical record reader.
        # Real receiver/storage/no-alias behavior is covered in test_research_receiver.
        raw = json.dumps(data, ensure_ascii=False).encode()
        record = {'stored_at': now.isoformat(), 'content_sha256': hashlib.sha256(raw).hexdigest(),
                  'record_id': 'rr_' + 'a' * 32}
        with patch.object(socket, 'socket', side_effect=AssertionError('network forbidden')), \
             patch.object(content.research_receipts, 'read_record', return_value=(raw, record)):
            lines = renderer.highlights('nng8')
        self.assertEqual(before, {file.name: file.read_bytes() for file in self.folder.iterdir()})
        text = '\n'.join(lines)
        self.assertNotIn('MUST_NEVER_LEAK', text)
        return text, renderer

    def test_current_completed_contract_preserves_analysis_and_limits(self):
        text, renderer = self.render(payload())
        for value in ('[推断]', '合成支持证据', '合成反证', '关键数据缺失', '部分可用', '未经人工审核', '合成来源甲'):
            self.assertIn(value, text)
        self.assertEqual(renderer.issues, [])

    def test_generated_bullet_markers_are_not_doubled(self):
        data = payload()
        data['research_summary']['conclusion'] = '- [事实] [来源 2] 合成结论待核实。'
        data['research_summary']['evidence'] = ['- [事实] [来源 2] 合成结论待核实。',
                                               '- [来源 3] 独立证据']
        text, renderer = self.render(data)
        self.assertEqual(text.count('合成结论待核实'), 1, '结论不得在支持证据里重复出现')
        self.assertNotIn('- - ', text, '不得出现双层列表符号')
        self.assertIn('- [来源 3] 独立证据', text)
        self.assertEqual(renderer.issues, [])

    def test_record_missing_contract_is_invalid_and_never_uses_old_text_or_markdown(self):
        data = payload(); del data['research_contract']
        text, renderer = self.render(data)
        self.assertIn('旧稿未纳入', text)
        self.assertEqual(renderer.issues, ['nng8:invalid'])

    def test_strict_research_never_reads_legacy_markdown_twin(self):
        text, renderer = self.render(payload(), markdown=b'\xff\xfe old invalid markdown')
        self.assertIn('合成研究结论', text)
        self.assertEqual(renderer.issues, [])

    def test_yesterday_report_rejected_even_if_received_and_produced_today(self):
        data = payload(); data['research_contract']['report_date'] = '2026-09-09'
        data['target_date'] = '2026-09-09'
        text, renderer = self.render(data)
        self.assertNotIn('合成研究结论', text)
        self.assertIn('nng8:invalid', renderer.issues)

    def test_gate_hash_type_and_scope_fail_closed(self):
        cases = [('version', True), ('status', 'pending'), ('report_sha256', 'bad'),
                 ('last_run_sha256', 'A' * 64), ('context_sha256', None),
                 ('publication_gate', 'failed'), ('evidence_review', 'unknown'),
                 ('data_status', 'unknown'), ('semantic_review', 'automatic_passed'),
                 ('as_of', '2026-09-10')]
        for key, value in cases:
            with self.subTest(key=key):
                data = payload(); data['research_contract'][key] = value
                text, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)
                self.assertNotIn('合成研究结论', text)

    def test_completion_and_production_must_be_current_and_ordered(self):
        for field, value in [('completed_at', '2026-09-09T08:01:00+08:00'),
                             ('completed_at', '2026-09-10T08:03:00+08:00'),
                             ('completed_at', '2026-09-10T08:01:00'),
                             ('produced_at', '2026-09-10T09:00:00+08:00')]:
            with self.subTest(field=field, value=value):
                data = payload()
                (data if field == 'produced_at' else data['research_contract'])[field] = value
                text, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_summary_cannot_drop_counterevidence_risks_or_source_mapping(self):
        for field in ('counter_evidence', 'risks', 'sources', 'limitations'):
            with self.subTest(field=field):
                data = payload(); data['research_summary'][field] = []
                text, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_oversize_or_malformed_content_not_silently_truncated(self):
        for field, value in [('conclusion', '长' * 1201), ('risks', ['x'] * 25), ('sources', {}), ('evidence', [True])]:
            with self.subTest(field=field):
                data = payload(); data['research_summary'][field] = value
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_nested_credentials_and_private_links_still_hidden(self):
        data = payload(); data['research_summary']['risks'].append('secret=HIDDEN https://example.com/subscription/PRIVATE')
        text, renderer = self.render(data)
        self.assertEqual(renderer.issues, [])
        self.assertNotIn('HIDDEN', text); self.assertNotIn('PRIVATE', text)

    def test_weekend_does_not_expect_unscheduled_upstream_or_reuse_old_research(self):
        text, renderer = self.render(payload(), datetime(2026, 9, 12, 8, 15, tzinfo=content.TZ))
        self.assertIn('周末没有计划', text)
        self.assertEqual(renderer.issues, [])

    def test_monday_uses_sealed_previous_session_not_72_hour_market_age(self):
        now = datetime(2026, 9, 14, 8, 15, tzinfo=content.TZ)
        text, renderer = self.render(payload(now), now)
        self.assertIn('行情截至 2026-09-11', text)
        self.assertEqual(renderer.issues, [])

    def test_duplicate_sources_are_not_accepted_as_unambiguous_evidence(self):
        data = payload(); data['research_summary']['sources'].append('2. 另一冲突来源')
        _, renderer = self.render(data)
        self.assertIn('nng8:invalid', renderer.issues)

    def test_inference_cannot_pass_with_plain_text_and_no_sources(self):
        data = payload()
        data['research_summary'].update(evidence=['无来源的支持'], counter_evidence=['无来源的反证'], sources=[])
        text, renderer = self.render(data)
        self.assertIn('nng8:invalid', renderer.issues)
        self.assertNotIn('合成研究结论', text)

    def test_conclusion_requires_explicit_fact_or_inference_label(self):
        for conclusion in ('未标记的结论', '[情景] 合成情景', '前缀隐藏 [推断] 合成结论'):
            with self.subTest(conclusion=conclusion):
                data = payload(); data['research_summary']['conclusion'] = conclusion
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_grouped_references_preserve_all_mapped_sources(self):
        for separator in ('、', ',', '，', ' ', ' , ', '、 '):
            with self.subTest(separator=separator):
                data = payload()
                data['research_summary']['evidence'] = ['合成证据 [来源 2' + separator + '3]']
                text, renderer = self.render(data)
                self.assertEqual(renderer.issues, [])
                self.assertIn('合成来源甲', text)
                self.assertIn('合成来源乙', text)

    def test_unknown_grouped_reference_is_not_silently_ignored(self):
        for separator in ('、', ',', '，', ' '):
            with self.subTest(separator=separator):
                data = payload()
                data['research_summary']['evidence'] = ['合成证据 [来源 2' + separator + '999]']
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_invalid_or_unclosed_source_markers_fail_closed(self):
        for marker in ('[来源 0]', '[来源 02]', '[来源 -2]', '[来源 2、]', '[来源 2/3]', '[来源 2', '[来源 2,,3]'):
            with self.subTest(marker=marker):
                data = payload(); data['research_summary']['risks'].append('合成风险 ' + marker)
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_each_source_requires_positive_number_and_description(self):
        for entry in ('无法解析的来源', '0. 来源', '02. 来源', '-1. 来源', '4.', '[来源 4]', '[来源 2、3] 来源'):
            with self.subTest(entry=entry):
                data = payload(); data['research_summary']['sources'].append(entry)
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_each_inference_evidence_and_counter_requires_reference(self):
        for field in ('evidence', 'counter_evidence'):
            with self.subTest(field=field):
                data = payload(); data['research_summary'][field].append('未提供引用的附加证据')
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_risk_placeholder_keeps_explicit_missing_counterevidence_limit(self):
        data = payload()
        data['research_summary']['conclusion'] = '[推断] 风险：合成风险结论。'
        data['research_summary']['counter_evidence'] = ['反证：未提供结构化反证来源，不升级为事实结论。']
        text, renderer = self.render(data)
        self.assertEqual(renderer.issues, [])
        self.assertIn('固定缺失提示不代表反证已核验', text)
        self.assertIn('未提供结构化反证来源', text)

    def test_arbitrary_or_nonrisk_counter_placeholder_is_rejected(self):
        cases = [('[推断] 摘要：合成结论。', '未提供结构化反证来源，不升级为事实结论。'),
                 ('[推断] 风险：合成结论。', '暂无反证，因此已经验证')]
        for conclusion, counter in cases:
            with self.subTest(counter=counter):
                data = payload(); data['research_summary'].update(conclusion=conclusion, counter_evidence=[counter])
                _, renderer = self.render(data)
                self.assertIn('nng8:invalid', renderer.issues)

    def test_fact_only_summary_may_omit_counter_but_not_claim_references(self):
        data = payload()
        data['research_summary'].update(conclusion='[事实] 合成基础行情。', counter_evidence=[])
        text, renderer = self.render(data)
        self.assertEqual(renderer.issues, [])
        self.assertIn('[事实]', text)
        data['research_summary']['evidence'] = []
        data['research_summary']['risks'] = ['仅风险处出现来源 [来源 2]']
        _, renderer = self.render(data)
        self.assertIn('nng8:invalid', renderer.issues)

    def test_schedule_follows_research_generation(self):
        timer = (Path(__file__).resolve().parents[1] / 'systemd/signal-gate-digest.timer').read_text()
        self.assertIn('OnCalendar=*-*-* 08:15:00 Asia/Shanghai', timer)
        self.assertNotIn('OnCalendar=*-*-* 08:00:', timer)


if __name__ == '__main__':
    unittest.main()
