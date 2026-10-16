"""Writing follows explicit Skill and product facts, not available footage."""
from scheduler.script_plan import _generation_prompt, script_system_prompt


def test_footage_changes_cannot_change_writer_request():
    evidence = {'brief': '介绍本期产品', 'claims': [], 'documents': {'产品': '规格为20片。'}}
    original = _generation_prompt(evidence, {'candidates': []})
    changed = _generation_prompt(
        dict(evidence, materials=[{'description': '仅有包装镜头', 'script_point': '只能讲包装'}]),
        {'candidates': [{'usable': True, 'use': 'detail', 'product_description': '只拍了瓶身'}]})
    assert changed == original
    library_only = _generation_prompt(evidence, {'candidates': [
        {'usable': True, 'use': 'detail', 'product_description': '只拍了瓶身'}]})
    assert library_only == original
    assert '规格为20片' in original


def test_generic_writer_does_not_add_industry_effect_restrictions():
    rules = '本次指定的测试规则原文。'
    system = script_system_prompt({'persona': '老X', 'prompt_rules': {'script': []}},
                                  {'id': 'test', 'version': '1.0.0', 'rules': rules})
    prompt = _generation_prompt({'brief': '介绍产品', 'claims': []}, {'candidates': []})
    assert rules in system
    for extra in ('画面支持', '不扮演医生', '不做诊断', '不给用药建议',
                  '禁止：保证', '资料未说明的作用不推断', '资料没写的功能、成分作用不写、不推断'):
        assert extra not in system + prompt
    assert '事实只依据本次商品资料' in system
