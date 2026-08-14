import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ANCHORS_JS = ROOT / "static" / "assistant_turn_anchors.js"
FEEDBACK_JS = ROOT / "static" / "message_feedback.js"
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
SW_JS = (ROOT / "static" / "sw.js").read_text(encoding="utf-8")


def _run_node(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for message feedback frontend tests")
    script = f"""
const fs=require('fs');
const vm=require('vm');
const anchors=fs.readFileSync({json.dumps(str(ANCHORS_JS))},'utf8');
const feedback=fs.readFileSync({json.dumps(str(FEEDBACK_JS))},'utf8');
const sandbox={{console}};
sandbox.window=sandbox;
sandbox.globalThis=sandbox;
sandbox.t=(key)=>key;
sandbox.esc=(value)=>String(value);
sandbox.li=(name)=>`<svg data-icon="${{name}}"></svg>`;
sandbox.S={{
  session:{{session_id:'session-one'}},
  messages:[{{role:'assistant',content:'answer',timestamp:12,_feedback:null}}]
}};
sandbox.renderMessages=()=>{{ sandbox.renderCount=(sandbox.renderCount||0)+1; }};
vm.createContext(sandbox);
vm.runInContext(anchors,sandbox,{{filename:'assistant_turn_anchors.js'}});
vm.runInContext(feedback,sandbox,{{filename:'message_feedback.js'}});
(async()=>{{
  let output;
  {case}
  process.stdout.write(JSON.stringify(output));
}})().catch(error=>{{console.error(error);process.exit(1);}});
"""
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def test_feedback_controls_follow_three_state_contract_and_send_stable_ref():
    result = _run_node(
        """
const calls=[];
sandbox.api=async(path,options)=>{
  calls.push({path,payload:JSON.parse(options.body)});
  return {ok:true,feedback:JSON.parse(options.body).feedback};
};
const buttons=[
  {disabled:false,setAttribute(){},removeAttribute(){}},
  {disabled:false,setAttribute(){},removeAttribute(){}},
];
const row={dataset:{msgIdx:'0'},querySelectorAll:()=>buttons};
const button={closest:()=>row};
await sandbox.toggleMessageFeedback(button,'like');
await sandbox.toggleMessageFeedback(button,'like');
await sandbox.toggleMessageFeedback(button,'dislike');
output={
  feedback:sandbox.S.messages[0]._feedback,
  payloads:calls.map(call=>call.payload),
  paths:calls.map(call=>call.path),
  renderCount:sandbox.renderCount,
};
"""
    )
    assert result["feedback"] == "dislike"
    assert result["paths"] == ["/api/message-feedback"] * 3
    assert [payload["feedback"] for payload in result["payloads"]] == ["like", None, "dislike"]
    assert all(payload["session_id"] == "session-one" for payload in result["payloads"])
    assert all(payload["message_ref"].startswith('{"role":"assistant"') for payload in result["payloads"])
    assert result["renderCount"] == 3


def test_feedback_request_failure_keeps_previous_state_and_reenables_controls():
    result = _run_node(
        """
sandbox.S.messages[0]._feedback='like';
sandbox.api=async()=>{ throw new Error('offline'); };
sandbox.showToast=()=>{ sandbox.toastCount=(sandbox.toastCount||0)+1; };
const controls=[
  {disabled:false,setAttribute(){},removeAttribute(){}},
  {disabled:false,setAttribute(){},removeAttribute(){}},
];
const row={dataset:{msgIdx:'0'},querySelectorAll:()=>controls};
await sandbox.toggleMessageFeedback({closest:()=>row},'dislike');
output={
  feedback:sandbox.S.messages[0]._feedback,
  renderCount:sandbox.renderCount||0,
  toastCount:sandbox.toastCount||0,
  controlsEnabled:controls.every(control=>control.disabled===false),
};
"""
    )
    assert result == {
        "feedback": "like",
        "renderCount": 0,
        "toastCount": 1,
        "controlsEnabled": True,
    }


def test_feedback_buttons_render_only_for_eligible_assistant_answers():
    result = _run_node(
        """
const html=sandbox.messageFeedbackButtonsHtml(
  {role:'assistant',content:'answer',timestamp:12,_feedback:'like'},
  true
);
output={
  html,
  hidden:sandbox.messageFeedbackButtonsHtml({role:'assistant',content:'answer'},false),
};
"""
    )
    assert 'data-feedback="like"' in result["html"]
    assert 'data-feedback="dislike"' in result["html"]
    assert 'aria-pressed="true"' in result["html"]
    assert result["hidden"] == ""


def test_feedback_eligibility_is_limited_to_settled_final_assistant_answers():
    result = _run_node(
        """
const eligible=sandbox.messageFeedbackEligible;
const finalContext={isTurnFinalAssistant:true,isProcessWakeup:false,displayContent:'answer'};
output={
  final:eligible({role:'assistant',content:'answer'},finalContext),
  user:eligible({role:'user',content:'question'},finalContext),
  live:eligible({role:'assistant',content:'answer',_live:true},finalContext),
  error:eligible({role:'assistant',content:'answer',_error:true},finalContext),
  toolOnly:eligible(
    {role:'assistant',content:'working',tool_calls:[{id:'tool-1'}]},
    finalContext
  ),
  textAfterTool:eligible(
    {role:'assistant',content:[
      {type:'tool_use',id:'tool-1'},
      {type:'output_text',text:'answer'},
    ]},
    finalContext
  ),
  intermediate:eligible(
    {role:'assistant',content:'answer'},
    {...finalContext,isTurnFinalAssistant:false}
  ),
  empty:eligible(
    {role:'assistant',content:''},
    {...finalContext,displayContent:'  '}
  ),
};
"""
    )
    assert result == {
        "final": True,
        "user": False,
        "live": False,
        "error": False,
        "toolOnly": False,
        "textAfterTool": True,
        "intermediate": False,
        "empty": False,
    }


def test_feedback_module_is_loaded_before_ui_and_cached_for_pwa():
    feedback_script = 'static/message_feedback.js?v=__WEBUI_VERSION__'
    assert feedback_script in INDEX_HTML
    assert INDEX_HTML.index(feedback_script) < INDEX_HTML.index('static/ui.js?v=__WEBUI_VERSION__')
    assert "'./static/message_feedback.js' + VQ" in SW_JS
    assert '.msg-feedback-btn[aria-pressed="true"]' in STYLE_CSS
    assert '@media(max-width:640px)' in STYLE_CSS
