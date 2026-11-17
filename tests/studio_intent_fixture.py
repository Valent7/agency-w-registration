"""Offline intent responses used while exercising the real approval UI."""
SCRIPT = '''
def _test_interpret(raw, kind, project, **kwargs):
    return {'main_idea':raw,'desired_result':raw,'audience':'Аудитория проекта',
            'style':'Ясно','emotional_effect':'Понимание'}
def _test_compile(intent, project, **kwargs):
    return {'production_prompt':intent['main_idea'],'approved_intent':dict(intent),
            'production_plan':[],'quality_criteria':[]}
'''
PATCHES = ", patch('content_studio_intent_ui.interpret_intent', side_effect=_test_interpret), patch('content_studio_intent_ui.compile_production_prompt', side_effect=_test_compile)"

def instrument(script):
    at = script.index('with patch')
    script = script[:at] + SCRIPT + '\n' + script[at:]
    at = script.index(':\n', script.index('with patch', at + len(SCRIPT)))
    return script[:at] + PATCHES + script[at:]

def approve_intent(app):
    next(b for b in app.button if b.label == '✅ Да, именно это').click().run()
    return app
