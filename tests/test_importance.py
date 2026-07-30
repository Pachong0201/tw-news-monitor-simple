import pytest
import yaml
from pathlib import Path

SAMPLE_RULES = {
    'enabled': True,
    'thresholds': {'critical': 80, 'important': 55, 'normal': 0},
    'display': {'max_highlights': 10},
    'rules': [
        {
            'id': 'test_rule',
            'category': 'test',
            'description': 'Test rule',
            'weight': 1.0,
            'subjects': ['总统', '台湾'],
            'actions': ['宣布', '发表'],
            'scenes': ['520', '双十'],
            'dimensions': {
                'strategic_domain': 30,
                'entity_level': 35,
                'action_intensity': 20,
                'node_significance': 25,
                'information_freshness': 15,
                'spillover_impact': 20,
            },
            'negative': ['参访', '祝贺'],
        },
        {
            'id': 'maritime_rule',
            'category': 'test',
            'description': 'Maritime rule',
            'weight': 1.0,
            'subjects': ['大陆渔船', '海巡'],
            'actions': ['碰撞', '扣留'],
            'scenes': ['金门', '台海中线'],
            'dimensions': {
                'strategic_domain': 30,
                'entity_level': 25,
                'action_intensity': 25,
                'node_significance': 15,
                'information_freshness': 10,
                'spillover_impact': 25,
            },
            'negative': ['一般搁浅'],
        },
    ],
}


class TestScoring:
    def test_single_rule_match(self):
        from app.importance import score_article
        r = score_article('总统宣布520重要政策', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.level in ('critical', 'important')
        assert r.score > 0
        assert 'test_rule' in r.matched_rules
        assert len(r.reasons) > 0

    def test_no_match(self):
        from app.importance import score_article
        r = score_article('今日天气晴朗', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.level == 'normal'
        assert r.score == 0

    def test_negative_keyword(self):
        from app.importance import score_article
        r = score_article('总统参访地方行程', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.level == 'normal'

    def test_critical_level(self):
        from app.importance import score_article
        r = score_article('两岸海上执法冲突 大陆渔船与海巡发生碰撞', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.score >= 80
        assert r.level == 'critical'

    def test_important_level(self):
        from app.importance import score_article
        r = score_article('总统发表两岸政策谈话', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.level in ('critical', 'important')

    def test_normal_level(self):
        from app.importance import score_article
        r = score_article('地方活动预告', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.level == 'normal'

    def test_score_range(self):
        from app.importance import score_article
        r = score_article('总统宣布520重要政策', '中央社', 'politics', '', SAMPLE_RULES)
        assert 0 <= r.score <= 100

    def test_maritime_match(self):
        from app.importance import score_article
        r = score_article('大陆渔船与台湾海巡在金门海域发生碰撞', '中央社', 'politics', '', SAMPLE_RULES)
        assert r.level in ('critical', 'important')
        assert 'maritime_rule' in r.matched_rules

    def test_disabled_returns_normal(self):
        from app.importance import score_article
        config = {'enabled': False}
        r = score_article('总统宣布520政策', '', '', '', config)
        assert r.level == 'normal'
        assert r.score == 0

    def test_score_ceiling(self):
        from app.importance import score_article
        intense_rules = dict(SAMPLE_RULES)
        intense_rules['rules'] = [
            {
                'id': 'max_rule',
                'category': 'test',
                'description': 'Max scoring',
                'weight': 5.0,
                'subjects': ['总统', '台湾', '大陆', '美国'],
                'actions': ['宣布', '发表', '碰撞', '制裁'],
                'scenes': ['520', '金门', '台海'],
                'dimensions': {
                    'strategic_domain': 50,
                    'entity_level': 45,
                    'action_intensity': 50,
                    'node_significance': 45,
                    'information_freshness': 40,
                    'spillover_impact': 50,
                },
                'negative': [],
            }
        ]
        r = score_article('总统在520发表两岸政策宣布重大制裁', '中央社', 'politics', '', intense_rules)
        assert 0 <= r.score <= 100


class TestConfigValidation:
    def test_validate_valid_config(self):
        from app.importance import validate_rules_config
        errs = validate_rules_config(SAMPLE_RULES)
        assert len(errs) == 0

    def test_validate_missing_enabled(self):
        from app.importance import validate_rules_config
        errs = validate_rules_config({'thresholds': {'critical': 80, 'important': 55, 'normal': 0}})
        assert len(errs) > 0

    def test_validate_bad_threshold_order(self):
        from app.importance import validate_rules_config
        cfg = dict(SAMPLE_RULES)
        cfg['thresholds'] = {'critical': 30, 'important': 55, 'normal': 0}
        errs = validate_rules_config(cfg)
        assert len(errs) > 0

    def test_validate_no_rules(self):
        from app.importance import validate_rules_config
        errs = validate_rules_config({'enabled': True, 'thresholds': {'critical': 80, 'important': 55, 'normal': 0}})
        # No rules is valid (just means nothing matches)
        assert len(errs) == 0


class TestImportanceSummary:
    def test_summary_counts(self):
        from app.importance import ImportanceResult, importance_summary
        results = [
            (None, ImportanceResult(score=85, level='critical')),
            (None, ImportanceResult(score=70, level='important')),
            (None, ImportanceResult(score=30, level='normal')),
        ]
        s = importance_summary(results)
        assert 'critical=1' in s
        assert 'important=1' in s
        assert 'normal=1' in s
